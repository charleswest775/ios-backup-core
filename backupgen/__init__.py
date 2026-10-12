"""backupgen: writes realistic synthetic iPhone backups for testing.

Development tooling only: it lives outside ``src/`` so it never ships in the
ios-backup-core wheel. Table definitions are harvested from real backups and
the iOS Simulator (``harvest.py``, definitions only, never rows); the content
comes from persona packs (``personas/``), fictionalized and time-shifted
histories with sourced timelines.

    python -m backupgen list
    python -m backupgen generate einstein --snapshot ios15 --out build/
    python -m backupgen harvest --sqlite sms.db --ios 26.5 --build 23F77 --source "iOS Simulator"
"""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SCHEMAS_DIR = PACKAGE_DIR / "schemas"
PERSONAS_DIR = PACKAGE_DIR / "personas"
