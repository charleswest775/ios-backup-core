"""backupgen gives each data type its own random numbers (generate.area_random).

So adding a chat, a contact, a note, a voicemail, a photo, a WhatsApp chat or a Safari visit to a
pack changes only the contents of that data type's part of the backup and of its answer key (the
contact goes before a card the People album links to, so Photos is checked for leaks through
ctx.contact_guids); the same snapshot comes out byte for byte the same in any
process, whatever PYTHONHASHSEED is; --seed (and plain versus encrypted) still changes every
data type that follows the seed; and Calendar and Voice Memos, which keep the fixed seeds they
had before, are as they were. The packs here are copies of the test persona (placeholder photos)
and Maren's (her photo downloads replaced by small generated images), so nothing needs the
network.
"""

import dataclasses
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen import PERSONAS_DIR, media  # noqa: E402
from backupgen.__main__ import main  # noqa: E402
from backupgen.generate import ENCRYPTED_ONLY, EXPECTED, STEPS, area_random, generate  # noqa: E402
from backupgen.persona import load_persona  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402

PASSWORD = "pw"


def _fake_fetch(url, sha1):
    """A small JPEG coloured by the photo's hash, in place of a download."""
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (64, 48), "#" + hashlib.sha1(url.encode()).hexdigest()[:6]).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture(autouse=True)
def _no_downloads(monkeypatch):
    monkeypatch.setattr(media, "fetch", _fake_fetch)


def _contents(out: Path, password=None) -> dict:
    """Every file in the backup under ``out``, decrypted: (domain, path) -> bytes."""
    backup_dir = next(p for p in out.iterdir() if p.is_dir())
    accessor = open_local_backup(str(backup_dir), password=password)
    try:
        files = {}
        for row in accessor.list_files():
            local = accessor.get_file(row["path"], row["domain"]) if row["path"] else None
            if local and os.path.isfile(local):
                files[(row["domain"], row["path"])] = Path(local).read_bytes()
        return files
    finally:
        accessor.cleanup()


def _expected(out: Path) -> dict:
    return json.loads((out / EXPECTED).read_text(encoding="utf-8"))


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _tree(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def _pack(root: Path, name: str = "testpersona") -> Path:
    folder = root / name
    shutil.copytree(PERSONAS_DIR / name, folder)
    return folder


# ── adding to one data type changes nothing else ────────────────────────────────


def _add_chat(pack: Path) -> None:
    # "added" sorts before every other chat, so all of Messages' draws move.
    (pack / "chats" / "added.txt").write_text(
        "chat: added\nwith: riley, casey\nname: Added\n\n== 2024-03-01\n"
        "10:00 riley: A chat the streams test added.\n10:02 me: Seen it.\n10:03 casey +like\n", encoding="utf-8")


def _add_note(pack: Path) -> None:
    (pack / "notes" / "added.txt").write_text(
        "created: 2021-01-05 09:00\nmodified: 2021-01-05 09:30\n\nAdded note\n[ ] One\n[x] Two\n", encoding="utf-8")


def _add_voicemail(pack: Path) -> None:
    path = pack / "voicemails.txt"
    path.write_text("== 2021-01-04\n10:00 casey 12s: A voicemail the streams test added.\n"
                    + path.read_text(encoding="utf-8"), encoding="utf-8")


def _add_photo(pack: Path) -> None:
    path = pack / "photos.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["photos"].insert(0, {"id": "added", "taken": "2021-01-04T09:00", "place": "No location",
                              "placeholder": {"size": [640, 480], "color": "#88aacc"}})
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _add_contact(pack: Path) -> None:
    # Straight after the owner, before riley, whose card the People album's "riley" group links to. A number
    # nobody in the pack calls from, so call history and voicemail name nobody new.
    path = pack / "persona.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["contacts"].insert(1, {"id": "added", "first": "Added", "last": "Card",
                                "phones": [["mobile", "+44 7700 900077"]], "emails": [["home", "added@example.org"]]})
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _add_whatsapp_chat(pack: Path) -> None:
    # The test persona has no WhatsApp, so this also takes "whatsapp" out of "absent".
    (pack / "whatsapp").mkdir()
    (pack / "whatsapp" / "people.txt").write_text("riley - Riley R.\n", encoding="utf-8")
    (pack / "whatsapp" / "riley.txt").write_text(
        "chat: riley\nwith: riley\n\n== 2024-03-02\n10:00 notice: encrypted\n10:01 riley: On WhatsApp now\n"
        "10:02 me [photo:london-1] The one from London\n10:03 riley [voice:9s]\n"
        "10:04 me [location:51.50,-0.12 London]\n10:05 riley [call:voice 2m10s]\n10:06 riley +👍\n", encoding="utf-8")


def _add_visit(pack: Path) -> None:
    path = pack / "safari.txt"
    path.write_text("== 2021-01-04\n09:00 https://example.org/added | Added by the streams test\n"
                    + path.read_text(encoding="utf-8"), encoding="utf-8")


def _messages_file(domain: str, path: str) -> bool:
    return (domain, path) == ("HomeDomain", "Library/SMS/sms.db") or (
        domain == "MediaDomain" and path.startswith("Library/SMS/Attachments/"))


# data type: (what to add to the pack, the answer-key section it shows in, the sections that may
# change, the backup files that may change). "absent" may lose that data type, nothing else.
CASES = {
    "messages": (_add_chat, "messages", {"conversations", "messages"}, _messages_file),
    "contacts": (_add_contact, "contacts", {"contacts"},
                 lambda domain, path: domain == "HomeDomain" and path.startswith("Library/AddressBook/")),
    "notes": (_add_note, "notes", {"notes"},
              lambda domain, path: domain == "AppDomainGroup-group.com.apple.notes"),
    # A voicemail with no missed call in calls.txt adds that call to call history (data from the
    # pack, not random numbers), so Calls may change too.
    "voicemail": (_add_voicemail, "voicemails", {"voicemails", "calls"},
                  lambda domain, path: (domain == "HomeDomain" and path.startswith("Library/Voicemail/"))
                  or (domain, path) == ("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata")),
    "photos": (_add_photo, "photos", {"photos", "photo_people"},
               lambda domain, path: domain == "CameraRollDomain"),
    "whatsapp": (_add_whatsapp_chat, "whatsapp", {"whatsapp"},
                 lambda domain, path: domain == "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"),
    # Only encrypted backups hold Safari history: the plain backup doesn't change at all.
    "browser_history": (_add_visit, "browser_history", {"browser_history"},
                        lambda domain, path: (domain, path) == ("HomeDomain", "Library/Safari/History.db")),
}


def _without(absent: dict, kind: str) -> dict:
    return {k: v for k, v in absent.items() if k != kind}


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    """The test persona's iOS 26 snapshot, plain and encrypted: (answer key, decrypted files) each."""
    root = tmp_path_factory.mktemp("baseline")
    persona = load_persona("testpersona", root=_pack(root / "packs").parent)
    out = {}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(media, "fetch", _fake_fetch)
        for password in (None, PASSWORD):
            folder = root / ("encrypted" if password else "plain")
            generate("testpersona", "ios26", folder, password=password, persona=persona)
            out[password] = (_expected(folder), _contents(folder, password))
    return out


@pytest.mark.parametrize("area", sorted(CASES))
@pytest.mark.parametrize("password", [None, PASSWORD], ids=["plain", "encrypted"])
def test_adding_to_one_data_type_changes_nothing_else(tmp_path, baseline, area, password):
    add, shown_in, may_change, may_change_file = CASES[area]
    pack = _pack(tmp_path / "packs")
    add(pack)
    out = tmp_path / "out"
    generate("testpersona", "ios26", out, password=password, persona=load_persona("testpersona", root=pack.parent))
    before, before_files = baseline[password]
    after, after_files = _expected(out), _contents(out, password)

    if area in ENCRYPTED_ONLY and password is None:
        assert after == before and after_files == before_files
        return
    assert _canonical(after[shown_in]) != _canonical(before[shown_in]), "the addition should show in the answer key"
    assert after.keys() == before.keys()
    assert _without(after["absent"], area) == _without(before["absent"], area)
    changed = sorted(key for key in before if key != "absent" and _canonical(before[key]) != _canonical(after[key]))
    assert set(changed) <= may_change, f"adding to {area} changed {changed}"

    changed_files = sorted(key for key in before_files.keys() | after_files.keys()
                           if before_files.get(key) != after_files.get(key))
    assert changed_files, "the addition should change a file"
    assert [key for key in changed_files if not may_change_file(*key)] == []


def test_each_step_starts_from_its_own_stream(tmp_path, monkeypatch):
    """Whatever the steps before it drew, each step's ctx.rng is new and made from (seed, data type).

    Comparing backups alone can't show this for a big pack: two shifted parsings of one
    Mersenne Twister stream by draws of varying length (randrange rejects values) often fall
    back into step, so a shared stream can look separate there.
    """
    import backupgen.generate as gen

    seen = []

    def spy(step):
        def wrapped(ctx):
            seen.append((ctx.kind, ctx.rng))
            assert ctx.kind == step.kind
            assert ctx.rng.getstate() == area_random(12345, step.kind).getstate(), step.kind
            return step.run(ctx)
        return wrapped

    monkeypatch.setattr(gen, "STEPS", tuple(dataclasses.replace(step, run=spy(step)) for step in STEPS))
    gen.generate("maren", "ios26", tmp_path, password=PASSWORD, seed=12345)
    assert [kind for kind, _ in seen] == [step.kind for step in STEPS]
    assert len({id(rng) for _, rng in seen}) == len(STEPS)


def test_adding_a_chat_to_maren_changes_nothing_else(tmp_path):
    """Maren's pack has every data type, WhatsApp, Calendar and Voice Memos included."""
    pack = _pack(tmp_path / "packs", "maren")
    generate("maren", "ios26", tmp_path / "before", password=PASSWORD, persona=load_persona("maren", root=pack.parent))
    (pack / "chats" / "aaa-added.txt").write_text(
        "chat: aaa-added\nwith: theo, leah\nname: Added\n\n== 2025-03-01\n"
        "10:00 theo: A chat the streams test added.\n10:02 me: Seen it.\n", encoding="utf-8")
    generate("maren", "ios26", tmp_path / "after", password=PASSWORD, persona=load_persona("maren", root=pack.parent))
    before, after = _expected(tmp_path / "before"), _expected(tmp_path / "after")
    assert len(after["messages"]) == len(before["messages"]) + 2
    changed = sorted(key for key in before if _canonical(before[key]) != _canonical(after[key]))
    assert changed == ["conversations", "messages"]
    before_files, after_files = _contents(tmp_path / "before", PASSWORD), _contents(tmp_path / "after", PASSWORD)
    changed_files = [key for key in before_files.keys() | after_files.keys() if before_files.get(key) != after_files.get(key)]
    assert changed_files and [key for key in changed_files if not _messages_file(*key)] == []


# ── the same in every process ────────────────────────────────────────────────────

_CHILD = """
import hashlib, io, sys
from pathlib import Path
from backupgen import media
from backupgen.generate import generate
from backupgen.persona import load_persona

def fetch(url, sha1):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (64, 48), "#" + hashlib.sha1(url.encode()).hexdigest()[:6]).save(out, "JPEG")
    return out.getvalue()

media.fetch = fetch
persona, snapshot, out, password = sys.argv[1:5]
generate(persona, snapshot, Path(out), password=password or None)
"""


@pytest.mark.parametrize("persona,password", [("testpersona", None), ("testpersona", PASSWORD), ("maren", PASSWORD)])
def test_the_same_snapshot_in_two_processes_is_byte_identical(tmp_path, persona, password):
    trees = []
    for hash_seed in ("0", "4242"):
        out = tmp_path / hash_seed
        path = [os.path.join(ROOT, "src"), ROOT] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": os.pathsep.join(path)}
        subprocess.run([sys.executable, "-c", _CHILD, persona, "ios26", str(out), password or ""],
                       env=env, check=True, cwd=tmp_path)
        trees.append(_tree(out))
    assert trees[0].keys() == trees[1].keys()
    assert [name for name in trees[0] if trees[0][name] != trees[1][name]] == []


# ── the seed still decides the data ──────────────────────────────────────────────

# Each data type's database, which holds that data type's random numbers (ids, seconds, names).
_DATABASES = {
    "messages": ("HomeDomain", "Library/SMS/sms.db"),
    "contacts": ("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb"),
    "calls": ("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata"),
    "voicemail": ("HomeDomain", "Library/Voicemail/voicemail.db"),
    "notes": ("AppDomainGroup-group.com.apple.notes", "NoteStore.sqlite"),
    "browser_history": ("HomeDomain", "Library/Safari/History.db"),
    "photos": ("CameraRollDomain", "Media/PhotoData/Photos.sqlite"),
}


def _cli(out: Path, *args: str) -> Path:
    assert main(["generate", "testpersona", "--snapshot", "ios26", "--out", str(out), "--password", PASSWORD,
                 *args]) == 0
    return out / "testpersona-ios26-encrypted"


def test_seed_changes_every_data_type_that_follows_it(tmp_path):
    one, again, two = _cli(tmp_path / "1", "--seed", "1"), _cli(tmp_path / "1b", "--seed", "1"), _cli(tmp_path / "2", "--seed", "2")
    assert _tree(one) == _tree(again)
    files_one, files_two = _contents(one, PASSWORD), _contents(two, PASSWORD)
    assert {area for area, key in _DATABASES.items() if files_one[key] == files_two[key]} == set()
    key_one, key_two = _expected(one), _expected(two)
    for section in ("messages", "calls", "browser_history", "photos"):
        assert key_one[section] != key_two[section], section


def test_plain_and_encrypted_backups_still_have_their_own_seeds(tmp_path):
    generate("testpersona", "ios26", tmp_path / "plain")
    generate("testpersona", "ios26", tmp_path / "enc", password=PASSWORD)
    plain, enc = _expected(tmp_path / "plain"), _expected(tmp_path / "enc")
    assert plain["messages"] != enc["messages"] and plain["photos"] != enc["photos"]


def test_each_data_type_has_its_own_stream():
    def draws(seed, area):
        rng = area_random(seed, area)
        return [rng.getrandbits(64) for _ in range(4)]

    areas = [step.kind for step in STEPS]
    assert len({tuple(draws(1, area)) for area in areas}) == len(areas)
    assert draws(1, "messages") == draws(1, "messages") != draws(2, "messages")


# ── Calendar and Voice Memos keep their own seeds ────────────────────────────────

# Maren's iOS 26 calendar and voice_memos answer-key sections as the commit before per-data-type
# random numbers wrote them (sha256 of _canonical), plain and encrypted alike. A change that means
# to alter her Calendar or Voice Memos data (calendar.json, voice_memos.txt or their writers)
# updates these.
MAREN_CALENDAR = "d07724a15a085baa24ec50735b8ce6046dae91cd1d273dbc91d0f5f5020fb3a3"
MAREN_VOICE_MEMOS = "a995f5ef15b927ae2f878f9e6421f3eff987d77e398c4cffd0103101b9f4e195"


def test_calendar_and_voice_memos_are_as_they_were(tmp_path):
    generate("maren", "ios26", tmp_path / "plain")
    generate("maren", "ios26", tmp_path / "seeded", password=PASSWORD, seed=12345)
    for folder in ("plain", "seeded"):
        expected = _expected(tmp_path / folder)
        assert hashlib.sha256(_canonical(expected["calendar"]).encode()).hexdigest() == MAREN_CALENDAR, folder
        assert hashlib.sha256(_canonical(expected["voice_memos"]).encode()).hexdigest() == MAREN_VOICE_MEMOS, folder
