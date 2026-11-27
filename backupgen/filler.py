"""Filler: rows that make a backup's Manifest.db big.

A persona's own data makes far fewer rows. A phone's Manifest.db can have hundreds of
thousands (OE-132's had 400,000), most of them apps' caches and settings in folders nested several
deep, and a reader that looks a file up by reading every row (OE-132) is quick on the first and slow
on the second.
The filler adds that bulk and nothing else: it's left out of the answer key, draws only from its
own random numbers (so no data type's databases, files or answer key change), and is added only
when asked for, backup by backup, so default backups stay as they are:

    generate(..., filler={"files": 20000, "domains": 300, "seed": 1})
    python -m backupgen generate testpersona --snapshot ios26 --password test --filler '{"files": 20000}'

Options (FILLER_OPTIONS, with their defaults):

- ``files``: how many file rows (flags 1) it adds, each a tiny file of 1 to 64 bytes.
- ``domains``: how many domains they're spread over: MediaDomain, then one made-up app's
  AppDomain-, AppDomainPlugin- and AppDomainGroup- domains after another (org.example.app0001, ...).
- ``seed``: its random numbers (names, sizes, contents), apart from the snapshot's seed.

It adds folders, nested, until they're about FOLDER_SHARE of the rows it adds (each domain's own
root row counts), some of them empty. With 50 files or more it also adds a
few rows readers get wrong: symbolic links (flags 4, an MBFile with a Target, no stored file), a
file 24 folders deep, a path longer than Windows's 260-character limit, and names in other scripts, some in Unicode's
decomposed form (NFD: "e" then U+0301 rather than "é"), which a lookup has to match byte for
byte, beside composed ones (NFC).

The backup's own files still move, as with any added file (writers/context.py): Manifest.db's
inode numbers and, in an encrypted backup, the keys of the files after each new one.
"""

from __future__ import annotations

import random
import unicodedata
from typing import Optional

FILLER_OPTIONS = {"files": 0, "domains": 100, "seed": 0}
FOLDER_SHARE = 0.30  # of the rows the filler adds
SPECIALS_FROM = 50  # files: below this, only plain files and folders
MEDIA_ROOT = "Library/Caches/com.example.filler"  # MediaDomain's filler lives here, apart from real data

_TOP = ("Library", "Documents", "tmp", "SystemData")
_LIBRARY = ("Caches", "Preferences", "Application Support", "SplashBoard", "Cookies", "WebKit", "HTTPStorages",
            "Saved Application State", "Logs")
_DEEPER = ("Snapshots", "fsCachedData", "Images", "Thumbnails", "data", "store", "downloads", "sessions",
           "offline", "assets", "Media", "avatars", "drafts", "index", "blobs", "metadata", "v2", "shared")
_EXTENSIONS = (".plist", ".db", ".json", ".png", ".jpg", ".dat", ".log", ".cache", "", ".sqlite-shm", ".ktx")


class FillerError(ValueError):
    pass


def check_filler(filler: Optional[dict]) -> dict:
    """The filler options merged with their defaults, or {} for none. Refuses unknown keys and
    values that aren't whole numbers (domains at least 1)."""
    if filler is None:
        return {}
    if not isinstance(filler, dict):
        raise FillerError(f"filler should be a JSON object like {{\"files\": 20000}}, not {filler!r}")
    unknown = sorted(filler.keys() - FILLER_OPTIONS.keys())
    if unknown:
        raise FillerError(f"filler: unknown key {unknown[0]!r} (one of: {', '.join(FILLER_OPTIONS)})")
    merged = {**FILLER_OPTIONS, **filler}
    for key, value in merged.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise FillerError(f"filler: {key} should be a whole number, 0 or more, not {value!r}")
    if merged["domains"] < 1:
        raise FillerError("filler: domains should be 1 or more")
    return merged


def filler_domains(count: int) -> list[str]:
    """MediaDomain, then each made-up app's three domains in turn, ``count`` in all."""
    out = ["MediaDomain"]
    app = 0
    while len(out) < count:
        app += 1
        bundle = f"org.example.app{app:04d}"
        for domain in (f"AppDomain-{bundle}", f"AppDomainPlugin-{bundle}.widget", f"AppDomainGroup-group.{bundle}"):
            if len(out) < count:
                out.append(domain)
    return out


def _root(domain: str) -> str:
    return MEDIA_ROOT if domain == "MediaDomain" else ""


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


class _Filler:
    def __init__(self, writer, options: dict):
        self.writer, self.options = writer, options
        self.rng = random.Random(f"backupgen/filler/{options['seed']}")
        self.domains = filler_domains(options["domains"])
        self.dirs: dict[str, list[str]] = {d: [_root(d)] for d in self.domains}
        self.taken: set[tuple[str, str]] = set()
        # The filler's own folders, the ones it needed whether or not the pack has them too, so the
        # same options make the same rows next to any pack.
        self.folder_rows: set[tuple[str, str]] = set()
        self.links = 0

    @property
    def folders(self) -> int:
        return len(self.folder_rows)

    # ── rows ─────────────────────────────────────────────────────────────────

    def _needs(self, domain: str, path: str, *, itself: bool) -> None:
        """Count ``path``'s folders (and ``path`` itself, for a folder) as the filler's."""
        parts = path.split("/") if path else []
        for depth in range(len(parts) + (1 if itself else 0)):
            self.folder_rows.add((domain, "/".join(parts[:depth])))

    def _free(self, domain: str, path: str) -> None:
        """Refuse to put a file or link where the pack has a row: add_file would replace it silently."""
        if self.writer.has_row(domain, path):
            raise FillerError(f"filler: {domain} {path!r} is already in the backup (a pack can't use "
                              f"{MEDIA_ROOT!r} in MediaDomain or org.example.app domains)")

    def directory(self, domain: str, path: str) -> None:
        self.writer.add_directory(domain, path)
        self._needs(domain, path, itself=True)
        self.taken.add((domain, path))

    def file(self, domain: str, path: str) -> None:
        self._free(domain, path)
        self.writer.add_file(domain, path, self.rng.randbytes(self.rng.randint(1, 64)))
        self._needs(domain, path, itself=False)
        self.taken.add((domain, path))

    def link(self, domain: str, path: str, target: str) -> None:
        self._free(domain, path)
        self.writer.add_symlink(domain, path, target)
        self._needs(domain, path, itself=False)
        self.links += 1
        self.taken.add((domain, path))

    def unique(self, domain: str, path: str) -> str:
        """``path``, or with " 2", " 3"... before its extension until no row in the filler has it."""
        if (domain, path) not in self.taken:
            return path
        head, dot, ext = path.rpartition(".")
        stem, suffix = (head, dot + ext) if dot and "/" not in ext else (path, "")
        n = 2
        while (domain, f"{stem} {n}{suffix}") in self.taken:
            n += 1
        return f"{stem} {n}{suffix}"

    # ── names ────────────────────────────────────────────────────────────────

    def folder_name(self, parent: str, domain: str) -> str:
        """A folder's name below the top: Library's own folders, then caches' kinds of names."""
        below_root = parent[len(_root(domain)):].strip("/")
        roll = self.rng.random()
        if below_root == "Library" and roll < 0.8:
            return self.rng.choice(_LIBRARY)
        if roll < 0.4:
            return self.rng.choice(_DEEPER)
        if roll < 0.75:
            return self.rng.randbytes(self.rng.choice((4, 8))).hex()
        return f"{self.rng.randbytes(4).hex()}-{self.rng.randbytes(2).hex()}-{self.rng.randbytes(2).hex()}".upper()

    def file_name(self) -> str:
        return self.rng.randbytes(self.rng.choice((4, 6, 10))).hex() + self.rng.choice(_EXTENSIONS)

    # ── the parts ────────────────────────────────────────────────────────────

    def specials(self) -> int:
        """The rows readers get wrong; returns how many files they are."""
        apps = self.domains[1:] or self.domains
        first, second, third = apps[0], apps[min(1, len(apps) - 1)], apps[min(2, len(apps) - 1)]
        files = [
            (first, "Library/Caches/deep/" + "/".join(f"level{i:02d}" for i in range(1, 25)) + "/bottom.dat"),
            (second, "Documents/" + "/".join(f"a folder name long enough that this path runs past Windows's limit "
                                             f"of 260 characters, part {i}" for i in range(1, 5)) + "/the file at the end.json"),
            (third, "Documents/Größe/Übersicht.txt"),
            (third, "Documents/日本語のメモ/写真.dat"),
            (third, "Documents/Ελληνικά/σημείωση.txt"),
            (third, "Documents/emoji 🎻/notes 📝.txt"),
            (third, "Documents/Café/menu.txt"),  # composed (NFC)
            (third, unicodedata.normalize("NFD", "Library/Café/menu.txt")),  # decomposed, in another folder
            (third, unicodedata.normalize("NFD", "Documents/Ångström.txt")),
        ]
        for domain, path in files:
            self.file(domain, _join(_root(domain), path))
        links = [
            (first, "Library/Caches/latest", "deep/level01"),
            (second, "Documents/current.json", "../Library/Preferences/settings.plist"),
            (third, "Library/Shared", f"/private/var/mobile/Containers/Shared/AppGroup/{self.rng.randbytes(16).hex().upper()}"),
            (self.domains[0], "today", "/private/var/mobile/Media/DCIM"),  # MediaDomain: under MEDIA_ROOT
            (apps[-1], "tmp/link to nowhere", "../nowhere"),
        ]
        for domain, path, target in links:
            self.link(domain, _join(_root(domain), path), target)
        for domain, path in files:  # their folders can hold plain files too
            self.dirs[domain].append(_join(_root(domain), path.rsplit("/", 1)[0]))
        return len(files)

    def add(self) -> None:
        files = self.options["files"]
        if not files:
            return
        for domain in self.domains:
            self.directory(domain, _root(domain))
        special = self.specials() if files >= SPECIALS_FROM else 0
        # Folders until they're about FOLDER_SHARE of what the filler adds, files and links included.
        wanted = round((files + self.links) * FOLDER_SHARE / (1 - FOLDER_SHARE))
        while self.folders < wanted:
            domain = self.rng.choice(self.domains)
            parent = self.rng.choice(self.dirs[domain])
            if parent == "" and domain != "MediaDomain":  # an app's container has only _TOP's folders at the top
                free = [top for top in _TOP if (domain, top) not in self.taken]
                if free:
                    path = self.rng.choice(free)
                    self.directory(domain, path)
                    self.dirs[domain].append(path)
                    continue
                parent = self.rng.choice(_TOP)
            for _ in range(4):  # another name before numbering one ("Caches 2")
                path = _join(parent, self.folder_name(parent, domain))
                if (domain, path) not in self.taken:
                    break
            path = self.unique(domain, path)
            self.directory(domain, path)
            self.dirs[domain].append(path)
        for _ in range(files - special):
            domain = self.rng.choice(self.domains)
            parent = self.rng.choice(self.dirs[domain])
            self.file(domain, self.unique(domain, _join(parent, self.file_name())))


def add_filler(writer, filler: dict) -> None:
    """Add the filler (options already checked: check_filler) to the backup ``writer`` is writing."""
    _Filler(writer, filler).add()
