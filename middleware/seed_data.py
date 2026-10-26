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
    seed/refs/<ref_id>.<ext>   the reference documents (BOTH grounding stores)
    seed/seed.json             grounding_docs + doc_format_config + adhoc_template_config

Two grounding stores ship, not one. `grounding_docs` feeds cascade; the ADHOC portals
and the Admin > Template Config screen read `adhoc_template_config`, which also carries
the per-template system prompts. Shipping only the first left a fresh install with no
reference and no prompt for Governance Matrix, Weekly Status Report, Test Script and
every other adhoc template — the files were on disk, but nothing resolved them.

Deliberately NOT shipped: `documents`, `cascade_sessions`, `cascade_documents` —
that is the packager's own generated history, not configuration.

WHEN IT RUNS
------------
On startup, once. Each store is gated INDEPENDENTLY on its own table being empty, so
a store the user has already populated is never overwritten, and re-running the
installer never clobbers local changes.
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
              "grounding_rows": 0, "format_rows": 0, "adhoc_rows": 0, "skipped": ""}

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
    src_dir = SEED_DIR / "refs"

    def _copy_refs(rows) -> int:
        """Copy a store's referenced files into REFS_DIR. Files first: a row pointing
        at a missing file is worse than neither. Idempotent — never overwrites."""
        copied = 0
        for row in rows:
            rid = (row.get("ref_id") or "").strip()
            if not rid:
                continue
            for src in src_dir.glob(f"{rid}*"):
                dest = REFS_DIR / src.name
                if not dest.exists():
                    shutil.copy2(src, dest)
                    copied += 1
        return copied

    async with aiosqlite.connect(str(DB_PATH)) as db:
        skips = []

        # ── Cascade grounding store ──────────────────────────────────────────
        if await _table_is_empty(db, "grounding_docs"):
            report["refs_copied"] += _copy_refs(data.get("grounding_docs", []))
            report["grounding_rows"] = await _insert_rows(
                db, "grounding_docs", data.get("grounding_docs", []))
        else:
            skips.append("grounding_docs")

        # Per-node output formats are configuration, not user data, so they ship too
        # — but only when the user has not already chosen their own.
        if await _table_is_empty(db, "doc_format_config"):
            report["format_rows"] = await _insert_rows(
                db, "doc_format_config", data.get("doc_format_config", []))

        # ── Adhoc grounding store ────────────────────────────────────────────
        # Gated on ITS OWN emptiness, independently of grounding_docs: the two stores
        # are separate, and a user who has configured one but not the other must still
        # receive the seed for the empty one. init_cascade_tables() creates this table
        # at startup, so _table_is_empty returns True (empty) rather than erroring on a
        # fresh DB; on an older DB without it the helper returns False and we skip.
        if await _table_is_empty(db, "adhoc_template_config"):
            report["refs_copied"] += _copy_refs(data.get("adhoc_template_config", []))
            report["adhoc_rows"] = await _insert_rows(
                db, "adhoc_template_config", data.get("adhoc_template_config", []))
        else:
            skips.append("adhoc_template_config")

        await db.commit()

    if skips:
        report["skipped"] = "already configured, left untouched: " + ", ".join(skips)
    return report
