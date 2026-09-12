"""
seed_data.py — install the bundled reference documents on first run.

WHY THIS IS NECESSARY
---------------------
The installer ships the grounding reference documents, but copying the files alone
is not enough. `_resolve_grounding()` finds a reference by looking up the
`grounding_docs` table to map a node_id to a stored ref_id, and the files are stored
under UUID names. Its filename fallback (`{node_id}-grounding.*`) matches none of
them — verified: 0 of 9.

So without the table rows the shipped references are invisible, every document is
generated with no reference at all, and the output degrades to exactly the generic
result the reference system exists to prevent. Files and rows must arrive together.

WHAT SHIPS
----------
    seed/refs/<ref_id>.<ext>   the reference documents
    seed/seed.json             grounding_docs rows + doc_format_config rows

Deliberately NOT shipped: `documents`, `cascade_sessions`, `cascade_documents` —
that is the packager's own generated history, not configuration.

WHEN IT RUNS
------------
On startup, once. Import is skipped entirely if the user already has grounding rows,
so it can never overwrite a reference someone has uploaded themselves, and re-running
the installer does not clobber local changes.
"""

import json
import shutil
from typing import Any, Dict

import aiosqlite

from paths import DB_PATH, REFS_DIR, SEED_DIR, ensure_dirs


async def _insert_rows(db: aiosqlite.Connection, table: str, rows: list) -> int:
    """
    Insert seed rows using whatever columns the LIVE table declares.

    Column-by-column INSERTs were brittle: naming only (node_id, output_format)
    tripped `NOT NULL constraint failed: doc_format_config.updated_at`, and any
    future column would break it again. Reading the schema at runtime and
    intersecting it with the seed keeps this working across schema changes, in
    either direction.
    """
    if not rows:
        return 0
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        live_cols = [r[1] for r in await cur.fetchall()]
    if not live_cols:
        return 0

    n = 0
    for row in rows:
        cols = [c for c in live_cols if c in row]
        if not cols:
            continue
        await db.execute(
            f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
            f"VALUES ({','.join('?' for _ in cols)})",
            [row[c] for c in cols])
        n += 1
    return n


async def _table_is_empty(db: aiosqlite.Connection, table: str) -> bool:
    try:
        async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            row = await cur.fetchone()
            return not row or row[0] == 0
    except Exception:
        return False          # table missing -> let init_* create it first


async def install_seed() -> Dict[str, Any]:
    """
    Copy bundled references into the user's store and register them.
    Returns a small report. Never raises — a missing seed is not an error, it just
    means this build shipped without references.
    """
    report = {"seed_present": False, "refs_copied": 0,
              "grounding_rows": 0, "format_rows": 0, "skipped": ""}

    manifest = SEED_DIR / "seed.json"
    if not manifest.exists():
        return report
    report["seed_present"] = True

    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        report["skipped"] = f"unreadable seed.json: {exc}"
        return report

    ensure_dirs()

    async with aiosqlite.connect(str(DB_PATH)) as db:
        if not await _table_is_empty(db, "grounding_docs"):
            report["skipped"] = "grounding already configured — left untouched"
            return report

        # Files first: a row pointing at a missing file is worse than neither.
        src_dir = SEED_DIR / "refs"
        for row in data.get("grounding_docs", []):
            for src in src_dir.glob(f"{row['ref_id']}*"):
                dest = REFS_DIR / src.name
                if not dest.exists():
                    shutil.copy2(src, dest)
                    report["refs_copied"] += 1

        report["grounding_rows"] = await _insert_rows(
            db, "grounding_docs", data.get("grounding_docs", []))

        # Per-node output formats are configuration, not user data, so they ship too
        # — but only when the user has not already chosen their own.
        if await _table_is_empty(db, "doc_format_config"):
            report["format_rows"] = await _insert_rows(
                db, "doc_format_config", data.get("doc_format_config", []))

        await db.commit()

    return report
