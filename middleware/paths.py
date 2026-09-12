"""
paths.py — where ProjectZen keeps its data. One resolver, used by every module.

WHY THIS EXISTS
---------------
app.py, db.py and cascade_agent.py each derived their own paths from
`Path(__file__).parent.parent`, which puts the SQLite database, the generated
documents and the grounding references INSIDE the code directory.

That is fine for a git checkout and wrong for an installed application. Installed
under %LOCALAPPDATA%\\Programs or C:\\Program Files, the code directory is either
read-only or requires admin to write, so the app fails the first time it tries to
save a document. Three copies of the same wrong assumption also meant any fix had
to be made three times, consistently.

RESOLUTION ORDER
----------------
1. PROJECTZEN_DATA_DIR, if set — explicit override for tests and admins.
2. A `storage/` directory that ALREADY EXISTS next to the code — this is what a
   developer checkout looks like, and it must keep working untouched. Existing
   installs therefore see no change at all.
3. The per-user application data directory, which is correct for an installed app
   and needs no elevation:
       Windows  %LOCALAPPDATA%\\ProjectZen
       macOS    ~/Library/Application Support/ProjectZen
       Linux    $XDG_DATA_HOME/projectzen  (or ~/.local/share/projectzen)

Rule 2 is what makes this a safe change rather than a migration: nothing moves for
anyone who already has data.
"""

import os
import sys
from pathlib import Path

_CODE_ROOT = Path(__file__).resolve().parent.parent


def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / "ProjectZen"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ProjectZen"
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(str(Path.home()), ".local", "share")
    return Path(base) / "projectzen"


def data_dir() -> Path:
    override = os.environ.get("PROJECTZEN_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    checkout = _CODE_ROOT / "storage"
    if checkout.exists():                 # developer checkout — leave it alone
        return _CODE_ROOT
    return _user_data_dir()


DATA_DIR  = data_dir()
STORAGE   = DATA_DIR / "storage"
STORE_DIR = STORAGE / "document_store"
REFS_DIR  = STORE_DIR / "refs"
DB_PATH   = STORAGE / "documents.db"

# One JSONL trace per generation run (see runlog.py). Lives under the resolved data
# directory for the same reason everything else does: an installed copy cannot write
# to its own program folder.
RUNLOG_DIR = STORAGE / "runlogs"

# The bundled seed that ships with the installer: reference documents plus the
# grounding_docs rows that map them to graph nodes. Sits INSIDE middleware/ so it
# travels with the code the installer packages, and is read-only at runtime.
SEED_DIR = Path(__file__).resolve().parent / "seed"


def ensure_dirs() -> None:
    for p in (STORAGE, STORE_DIR, REFS_DIR, RUNLOG_DIR):
        p.mkdir(parents=True, exist_ok=True)
