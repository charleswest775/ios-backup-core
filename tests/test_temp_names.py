"""Files decrypted out of an encrypted backup get names every OS accepts.

They used to be named after their path in the backup, so a message attachment
called "How was an iPhone set up?.pdf" couldn't be written on Windows, which
forbids ? : * " < > | in file names, and it was reported missing. Long paths
also ran past Windows' 260-character limit (and macOS's 255 per name).
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from backupgen.container import BackupWriter, Device  # noqa: E402
from ios_backup_core.backup import open_local_backup  # noqa: E402

FOLDER = "Library/SMS/Attachments/68/08/430548CB-6E2D-4C4B-9C1A-2F3E4D5C6B7A"
FILES = {
    # Seen in a real backup (public corpus pub-hickman-13.3.1).
    f"{FOLDER}/How was an iPhone set up?.pdf": b"%PDF-1.4 a question",
    f'{FOLDER}/Call at 10:30 *today* "v2" <final> | notes.txt': b"every forbidden character",
    f"{FOLDER}/{'a long folder name ' * 8}/{'a very long file name ' * 10}.jpeg": b"long",
}
WINDOWS_FORBIDDEN = set('<>:"/\\|?*')


@pytest.fixture(scope="module")
def accessor(tmp_path_factory):
    device = Device(name="Test iPhone", product_type="iPhone12,1", product_name="iPhone 11",
                    ios="13.3.1", build="17D50", phone_number="+44 7700 900001")
    writer = BackupWriter(tmp_path_factory.mktemp("out"), device,
                          datetime(2020, 2, 1, tzinfo=timezone.utc), "13.3.1",
                          password="pw", seed=1)
    for rel_path, data in FILES.items():
        writer.add_file("MediaDomain", rel_path, data)
    # The same path in another domain is a different file.
    writer.add_file("HomeDomain", next(iter(FILES)), b"another domain")
    acc = open_local_backup(str(writer.finalize()), password="pw")
    yield acc
    acc.cleanup()


@pytest.mark.parametrize("relative_path", FILES)
def test_decrypted_files_get_names_windows_accepts(accessor, relative_path):
    path = accessor.get_file(relative_path, domain="MediaDomain")
    assert path and Path(path).read_bytes() == FILES[relative_path]
    name = os.path.basename(path)
    assert not WINDOWS_FORBIDDEN & set(name), name
    assert len(name) < 100, name
    # Callers tell videos from photos by the extension, so it's kept.
    assert os.path.splitext(name)[1] == os.path.splitext(relative_path)[1]


def test_the_same_path_in_two_domains_stays_two_files(accessor):
    rel_path = next(iter(FILES))
    media = accessor.get_file(rel_path, domain="MediaDomain")
    home = accessor.get_file(rel_path, domain="HomeDomain")
    assert media != home
    assert Path(media).read_bytes() == FILES[rel_path]
    assert Path(home).read_bytes() == b"another domain"
