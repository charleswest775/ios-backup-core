"""The library still imports when the OS refuses to load a native library.

Windows' Smart App Control blocks DLLs that aren't signed. pycryptodome, which
iphone_backup_decrypt uses, then raises OSError rather than ImportError while it
is being imported, which made `import ios_backup_core` itself fail
(openextract#169). Unencrypted backups don't need it, so they must still open.
"""

import os
import subprocess
import sys
import textwrap

ROOT = os.path.join(os.path.dirname(__file__), "..")

# Runs in a fresh interpreter so the blocked library doesn't leak into other tests.
SCRIPT = textwrap.dedent("""
    import sys
    import Crypto.Util._raw_api as raw_api

    def blocked(name, cdecl):
        raise OSError("[WinError 4551] An Application Control policy has blocked this file")

    raw_api.load_lib = blocked
    sys.path.insert(0, sys.argv[1])
    import ios_backup_core.backup as backup
    print(f"_HAS_DECRYPT={backup._HAS_DECRYPT}")
""")


def test_imports_without_decryption_when_pycryptodome_is_blocked():
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, os.path.join(ROOT, "src")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "_HAS_DECRYPT=False" in result.stdout
