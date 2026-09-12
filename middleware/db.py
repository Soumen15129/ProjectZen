"""
db.py — Tier 3 interface: all SQLite read/write operations
Uses aiosqlite for async access from FastAPI.
"""

import aiosqlite
import json
import os
from datetime import datetime
from typing import Optional, List, Dict, Any

from paths import DB_PATH as _DB_PATH, ensure_dirs
ensure_dirs()
DB_PATH = str(_DB_PATH)


async def init_db():
    """Create tables and FTS index on startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id              TEXT PRIMARY KEY,
                file_name       TEXT NOT NULL,
                template        TEXT NOT NULL,
                output_format   TEXT NOT NULL,
                file_path       TEXT NOT NULL,
                size_kb         REAL NOT NULL DEFAULT 0,
                username        TEXT NOT NULL DEFAULT 'unknown',
                created_at      TEXT NOT NULL,
                content_summary TEXT DEFAULT ''
            )
        """)

        # Full-text search virtual table
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
            USING fts5(
                id UNINDEXED,
                file_name,
                template,
                content_summary,
                username,
                output_format UNINDEXED,
                created_at UNINDEXED
            )
        """)

        # Trigger: keep FTS in sync on insert
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS documents_ai
            AFTER INSERT ON documents BEGIN
                INSERT INTO documents_fts(
                    id, file_name, template, content_summary,
                    username, output_format, created_at
                )
                VALUES (
                    new.id, new.file_name, new.template, new.content_summary,
                    new.username, new.output_format, new.created_at
                );
            END
        """)

        # Trigger: keep FTS in sync on update
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS documents_au
            AFTER UPDATE ON documents BEGIN
                UPDATE documents_fts
                SET file_name       = new.file_name,
                    template        = new.template,
                    content_summary = new.content_summary,
                    username        = new.username,
                    output_format   = new.output_format,
                    created_at      = new.created_at
                WHERE id = old.id;
            END
        """)

        await db.commit()
    print(f"✅ DB ready: {DB_PATH}")


async def save_document(
    id: str,
    file_name: str,
    template: str,
    output_format: str,
    file_path: str,
    size_kb: float,
    username: str,
    content_summary: str,
) -> bool:
    """Insert a new document record."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO documents
                    (id, file_name, template, output_format, file_path,
                     size_kb, username, created_at, content_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    id, file_name, template, output_format, file_path,
                    size_kb, username or "unknown",
                    datetime.utcnow().isoformat(),
                    (content_summary or "")[:12000],
                ),
            )
            await db.commit()
        return True
    except Exception as e:
        print(f"⚠ DB save error: {e}")
        return False


async def get_document(file_id: str) -> Optional[Dict[str, Any]]:
    """Fetch one document by ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM documents WHERE id = ?", (file_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_documents(
    username: Optional[str] = None,
    output_format: Optional[str] = None,
    template: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """List documents with optional filters."""
    sql = "SELECT * FROM documents WHERE 1=1"
    params: list = []

    if username:
        sql += " AND username = ?"
        params.append(username)
    if output_format:
        sql += " AND output_format = ?"
        params.append(output_format)
    if template:
        sql += " AND template LIKE ?"
        params.append(f"%{template}%")

    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def search_documents(
    query: str,
    output_format: Optional[str] = None,
    username: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Full-text search using FTS5 table, then join back to documents
    for full metadata. Falls back to LIKE search if FTS returns nothing.
    """
    results: List[Dict[str, Any]] = []

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # --- FTS search ---
        try:
            fts_sql = """
                SELECT d.*
                FROM documents d
                JOIN documents_fts f ON d.id = f.id
                WHERE documents_fts MATCH ?
            """
            fts_params: list = [query]

            if output_format:
                fts_sql += " AND d.output_format = ?"
                fts_params.append(output_format)
            if username:
                fts_sql += " AND d.username = ?"
                fts_params.append(username)
            if date_from:
                fts_sql += " AND d.created_at >= ?"
                fts_params.append(date_from)
            if date_to:
                fts_sql += " AND d.created_at <= ?"
                fts_params.append(date_to)

            fts_sql += " ORDER BY d.created_at DESC LIMIT ?"
            fts_params.append(limit)

            async with db.execute(fts_sql, fts_params) as cur:
                rows = await cur.fetchall()
                results = [dict(r) for r in rows]
        except Exception as fts_err:
            print(f"   FTS error (falling back to LIKE): {fts_err}")

        # --- LIKE fallback ---
        if not results:
            like_sql = """
                SELECT * FROM documents
                WHERE (
                    file_name       LIKE ? OR
                    template        LIKE ? OR
                    content_summary LIKE ? OR
                    username        LIKE ?
                )
            """
            pattern = f"%{query}%"
            like_params: list = [pattern, pattern, pattern, pattern]

            if output_format:
                like_sql += " AND output_format = ?"
                like_params.append(output_format)
            if username:
                like_sql += " AND username = ?"
                like_params.append(username)
            if date_from:
                like_sql += " AND created_at >= ?"
                like_params.append(date_from)
            if date_to:
                like_sql += " AND created_at <= ?"
                like_params.append(date_to)

            like_sql += " ORDER BY created_at DESC LIMIT ?"
            like_params.append(limit)

            async with db.execute(like_sql, like_params) as cur:
                rows = await cur.fetchall()
                results = [dict(r) for r in rows]

    return results


async def get_content_summary(file_id: str) -> str:
    """Return the stored content_summary for a document (used for grounding)."""
    doc = await get_document(file_id)
    return doc.get("content_summary", "") if doc else ""


# ═══════════════════════════════════════════════════════════════════════════════
# CASCADE TABLES  — added as addon; existing tables untouched
# ═══════════════════════════════════════════════════════════════════════════════

async def init_cascade_tables():
    """
    Create the three cascade addon tables if they do not exist.
    Called once at startup alongside init_db().

    Tables:
      cascade_sessions    — one row per cascade run (new generation or delta)
      cascade_documents   — one row per generated doc per session (versioned)
      doc_format_config   — admin-configurable output format per document type
    """
    async with aiosqlite.connect(DB_PATH) as db:

        # ── cascade_sessions ────────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cascade_sessions (
                session_id      TEXT PRIMARY KEY,
                mode            TEXT NOT NULL DEFAULT 'new',
                username        TEXT NOT NULL DEFAULT 'unknown',
                input_file_name TEXT NOT NULL DEFAULT '',
                selected_nodes  TEXT NOT NULL DEFAULT '[]',
                status          TEXT NOT NULL DEFAULT 'running',
                total_docs      INTEGER NOT NULL DEFAULT 0,
                completed_docs  INTEGER NOT NULL DEFAULT 0,
                delta_node_id   TEXT,
                delta_summary   TEXT,
                error           TEXT,
                created_at      TEXT NOT NULL,
                completed_at    TEXT
            )
        """)

        # ── cascade_documents ───────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cascade_documents (
                id              TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                node_id         TEXT NOT NULL,
                file_id         TEXT NOT NULL,
                file_name       TEXT NOT NULL,
                output_format   TEXT NOT NULL,
                version         INTEGER NOT NULL DEFAULT 1,
                status          TEXT NOT NULL DEFAULT 'draft',
                is_finalised    INTEGER NOT NULL DEFAULT 0,
                finalised_at    TEXT,
                finalised_by    TEXT,
                delta_notes     TEXT,
                model_used      TEXT,
                created_at      TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES cascade_sessions(session_id),
                FOREIGN KEY (file_id)    REFERENCES documents(id)
            )
        """)

        # ── doc_format_config ───────────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS doc_format_config (
                node_id         TEXT PRIMARY KEY,
                output_format   TEXT NOT NULL DEFAULT 'docx',
                is_default_sel  INTEGER NOT NULL DEFAULT 1,
                label           TEXT NOT NULL DEFAULT '',
                updated_by      TEXT NOT NULL DEFAULT 'system',
                updated_at      TEXT NOT NULL
            )
        """)

        await db.commit()

    print(f"✅ Cascade tables ready")


async def seed_format_config_defaults():
    """
    Insert default output formats from knowledge_graph.py if the
    doc_format_config table is empty. Idempotent — safe to call on every start.
    """
    from knowledge_graph import DEFAULT_OUTPUT_FORMATS, get_node

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM doc_format_config") as cur:
            row  = await cur.fetchone()
            count = row[0] if row else 0

        if count == 0:
            now = datetime.utcnow().isoformat()
            rows = []
            for node_id, fmt in DEFAULT_OUTPUT_FORMATS.items():
                node  = get_node(node_id)
                label = node["label"] if node else node_id
                rows.append((node_id, fmt, 1, label, "system", now))
            await db.executemany(
                """INSERT OR IGNORE INTO doc_format_config
                   (node_id, output_format, is_default_sel, label, updated_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                rows,
            )
            await db.commit()
            print(f"   ✅ doc_format_config seeded with {len(rows)} defaults")


# ── cascade_sessions CRUD ─────────────────────────────────────────────────────

async def save_cascade_session(
    session_id: str,
    mode: str,
    username: str,
    input_file_name: str,
    selected_nodes: List[str],
    total_docs: int,
    delta_node_id: Optional[str] = None,
) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT OR REPLACE INTO cascade_sessions
                   (session_id, mode, username, input_file_name, selected_nodes,
                    status, total_docs, completed_docs, delta_node_id, created_at)
                   VALUES (?, ?, ?, ?, ?, 'running', ?, 0, ?, ?)""",
                (
                    session_id, mode, username or "unknown", input_file_name,
                    json.dumps(selected_nodes), total_docs, delta_node_id,
                    datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()
        return True
    except Exception as e:
        print(f"⚠ save_cascade_session error: {e}")
        return False


async def update_cascade_session_status(
    session_id: str,
    status: str,
    completed_docs: Optional[int] = None,
    delta_summary: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            sets = ["status = ?"]
            params: list = [status]
            if completed_docs is not None:
                sets.append("completed_docs = ?")
                params.append(completed_docs)
            if delta_summary is not None:
                sets.append("delta_summary = ?")
                params.append(delta_summary[:4000])
            if error is not None:
                sets.append("error = ?")
                params.append(error[:2000])
            if status in ("complete", "failed"):
                sets.append("completed_at = ?")
                params.append(datetime.utcnow().isoformat())
            params.append(session_id)
            await db.execute(
                f"UPDATE cascade_sessions SET {', '.join(sets)} WHERE session_id = ?",
                params,
            )
            await db.commit()
        return True
    except Exception as e:
        print(f"⚠ update_cascade_session_status error: {e}")
        return False


async def get_cascade_session(session_id: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM cascade_sessions WHERE session_id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["selected_nodes"] = json.loads(d.get("selected_nodes") or "[]")
            except Exception:
                d["selected_nodes"] = []
            return d


async def list_cascade_sessions(
    username: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM cascade_sessions WHERE 1=1"
    params: list = []
    if username:
        sql += " AND username = ?"
        params.append(username)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["selected_nodes"] = json.loads(d.get("selected_nodes") or "[]")
                except Exception:
                    d["selected_nodes"] = []
                result.append(d)
            return result


# ── cascade_documents CRUD ────────────────────────────────────────────────────

async def save_cascade_document(
    id: str,
    session_id: str,
    node_id: str,
    file_id: str,
    file_name: str,
    output_format: str,
    version: int,
    model_used: str = "",
    delta_notes: str = "",
) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT OR REPLACE INTO cascade_documents
                   (id, session_id, node_id, file_id, file_name, output_format,
                    version, status, is_finalised, model_used, delta_notes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?, ?)""",
                (
                    id, session_id, node_id, file_id, file_name, output_format,
                    version, model_used, delta_notes[:2000],
                    datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()
        return True
    except Exception as e:
        print(f"⚠ save_cascade_document error: {e}")
        return False


async def get_cascade_doc_by_node_version(
    session_id: str,
    node_id: str,
    version: int,
) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM cascade_documents
               WHERE session_id = ? AND node_id = ? AND version = ?
               ORDER BY created_at DESC LIMIT 1""",
            (session_id, node_id, version),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_cascade_doc_current_version(session_id: str, node_id: str) -> int:
    """Return the highest version number generated for this node in the session."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT MAX(version) FROM cascade_documents
               WHERE session_id = ? AND node_id = ?""",
            (session_id, node_id),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] else 0


async def is_cascade_doc_finalised(session_id: str, node_id: str) -> bool:
    """True if the LATEST version of this doc in the session is finalised."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT is_finalised FROM cascade_documents
               WHERE session_id = ? AND node_id = ?
               ORDER BY version DESC LIMIT 1""",
            (session_id, node_id),
        ) as cur:
            row = await cur.fetchone()
            return bool(row[0]) if row else False


async def finalise_cascade_document(
    session_id: str,
    node_id: str,
    version: int,
    finalised_by: str,
) -> bool:
    """Mark a specific version of a cascade document as finalised."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE cascade_documents
                   SET is_finalised = 1, status = 'finalised',
                       finalised_at = ?, finalised_by = ?
                   WHERE session_id = ? AND node_id = ? AND version = ?""",
                (datetime.utcnow().isoformat(), finalised_by, session_id, node_id, version),
            )
            await db.commit()
        return True
    except Exception as e:
        print(f"⚠ finalise_cascade_document error: {e}")
        return False


async def list_cascade_documents(
    session_id: str,
    node_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all cascade documents for a session, optionally filtered by node."""
    sql = "SELECT * FROM cascade_documents WHERE session_id = ?"
    params: list = [session_id]
    if node_id:
        sql += " AND node_id = ?"
        params.append(node_id)
    sql += " ORDER BY node_id, version ASC"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── doc_format_config CRUD ────────────────────────────────────────────────────

async def get_format_config(node_id: str) -> Optional[str]:
    """Return the configured output format for a document type."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT output_format FROM doc_format_config WHERE node_id = ?", (node_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def list_format_configs() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM doc_format_config ORDER BY node_id"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def update_format_config(
    node_id: str,
    output_format: str,
    is_default_sel: int,
    updated_by: str,
) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT OR REPLACE INTO doc_format_config
                   (node_id, output_format, is_default_sel, label, updated_by, updated_at)
                   VALUES (
                       ?,
                       ?,
                       ?,
                       COALESCE((SELECT label FROM doc_format_config WHERE node_id = ?), ?),
                       ?,
                       ?
                   )""",
                (
                    node_id, output_format, is_default_sel,
                    node_id, node_id,
                    updated_by, datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()
        return True
    except Exception as e:
        print(f"⚠ update_format_config error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# GROUNDING DOCS TABLE — admin-uploaded reference docs per knowledge graph node
# ═══════════════════════════════════════════════════════════════════════════════

async def init_grounding_table():
    """Create grounding_docs table if it does not exist. Called once at startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS grounding_docs (
                node_id       TEXT PRIMARY KEY,
                ref_id        TEXT NOT NULL,
                file_name     TEXT NOT NULL,
                file_ext      TEXT NOT NULL,
                size_kb       REAL NOT NULL DEFAULT 0,
                uploaded_by   TEXT NOT NULL DEFAULT 'admin',
                uploaded_at   TEXT NOT NULL
            )
        """)
        # Adhoc template config: the per-template system prompt and reference file
        # that Admin -> Template Config collects. The UI for this has existed since
        # the beginning but had no server side at all — the prompt went to
        # localStorage (per-browser, invisible to everyone else) and the reference
        # file POSTed a payload /upload-ref does not accept, so it 422'd and fell
        # back to localStorage too. Nothing ever read either one back. Keyed by the
        # template LABEL because that is what the adhoc dropdown sends.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS adhoc_template_config (
                template      TEXT PRIMARY KEY,
                node_id       TEXT,
                system_prompt TEXT NOT NULL DEFAULT '',
                ref_id        TEXT,
                file_name     TEXT,
                file_ext      TEXT,
                size_kb       REAL NOT NULL DEFAULT 0,
                redactions    TEXT NOT NULL DEFAULT '',
                updated_by    TEXT NOT NULL DEFAULT 'admin',
                updated_at    TEXT NOT NULL
            )
        """)
        # Workbook multi-slot table: config-workbook supports up to 4 reference files
        await db.execute("""
            CREATE TABLE IF NOT EXISTS workbook_slots (
                slot          INTEGER NOT NULL,
                ref_id        TEXT NOT NULL,
                file_name     TEXT NOT NULL,
                file_ext      TEXT NOT NULL,
                size_kb       REAL NOT NULL DEFAULT 0,
                uploaded_by   TEXT NOT NULL DEFAULT 'admin',
                uploaded_at   TEXT NOT NULL,
                PRIMARY KEY (slot)
            )
        """)
        await db.commit()
    print("✅ Grounding docs table ready")


async def save_grounding_doc(
    node_id: str,
    ref_id: str,
    file_name: str,
    file_ext: str,
    size_kb: float,
    uploaded_by: str,
) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT OR REPLACE INTO grounding_docs
                   (node_id, ref_id, file_name, file_ext, size_kb, uploaded_by, uploaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    node_id, ref_id, file_name, file_ext, size_kb,
                    uploaded_by or "admin", datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()
        return True
    except Exception as e:
        print(f"⚠ save_grounding_doc error: {e}")
        return False


async def get_grounding_doc(node_id: str) -> Optional[Dict[str, Any]]:
    """Return the grounding doc record for a node, or None if not set."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM grounding_docs WHERE node_id = ?", (node_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_grounding_docs() -> List[Dict[str, Any]]:
    """Return all grounding doc records."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM grounding_docs ORDER BY uploaded_at DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def delete_grounding_doc(node_id: str) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM grounding_docs WHERE node_id = ?", (node_id,)
            )
            await db.commit()
        return True
    except Exception as e:
        print(f"⚠ delete_grounding_doc error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# WORKBOOK SLOTS  — up to 4 reference workbooks for Configuration Workbook node
# ═══════════════════════════════════════════════════════════════════════════════

MAX_WORKBOOK_SLOTS = 4


async def save_workbook_slot(
    slot: int,
    ref_id: str,
    file_name: str,
    file_ext: str,
    size_kb: float,
    uploaded_by: str,
) -> bool:
    if slot < 1 or slot > MAX_WORKBOOK_SLOTS:
        return False
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT OR REPLACE INTO workbook_slots
                   (slot, ref_id, file_name, file_ext, size_kb, uploaded_by, uploaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (slot, ref_id, file_name, file_ext, size_kb,
                 uploaded_by or "admin", datetime.utcnow().isoformat()),
            )
            await db.commit()
        return True
    except Exception as e:
        print(f"⚠ save_workbook_slot error: {e}")
        return False


async def get_workbook_slot(slot: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM workbook_slots WHERE slot = ?", (slot,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_workbook_slots() -> List[Dict[str, Any]]:
    """Return all occupied workbook slots (1-4)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM workbook_slots ORDER BY slot ASC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def delete_workbook_slot(slot: int) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM workbook_slots WHERE slot = ?", (slot,))
            await db.commit()
        return True
    except Exception as e:
        print(f"⚠ delete_workbook_slot error: {e}")
        return False


# ── Adhoc template config ────────────────────────────────────────────────────

async def save_adhoc_config(template: str, node_id: str = "", system_prompt: str = "",
                            ref_id: str = "", file_name: str = "", file_ext: str = "",
                            size_kb: float = 0.0, redactions: str = "",
                            updated_by: str = "admin") -> None:
    """
    Upsert one template's config. A save that carries no new reference file KEEPS the
    existing one — an admin editing the prompt must not silently drop the reference.
    """
    from datetime import datetime as _dt
    async with aiosqlite.connect(DB_PATH) as db:
        if ref_id:
            await db.execute("""
                INSERT INTO adhoc_template_config
                    (template,node_id,system_prompt,ref_id,file_name,file_ext,
                     size_kb,redactions,updated_by,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(template) DO UPDATE SET
                    node_id=excluded.node_id, system_prompt=excluded.system_prompt,
                    ref_id=excluded.ref_id, file_name=excluded.file_name,
                    file_ext=excluded.file_ext, size_kb=excluded.size_kb,
                    redactions=excluded.redactions, updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
            """, (template, node_id, system_prompt, ref_id, file_name, file_ext,
                  size_kb, redactions, updated_by, _dt.utcnow().isoformat()))
        else:
            await db.execute("""
                INSERT INTO adhoc_template_config
                    (template,node_id,system_prompt,updated_by,updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(template) DO UPDATE SET
                    node_id=excluded.node_id, system_prompt=excluded.system_prompt,
                    updated_by=excluded.updated_by, updated_at=excluded.updated_at
            """, (template, node_id, system_prompt, updated_by, _dt.utcnow().isoformat()))
        await db.commit()


async def get_adhoc_config(template: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM adhoc_template_config WHERE template = ?", (template,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_adhoc_configs():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM adhoc_template_config ORDER BY template"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_adhoc_config(template: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM adhoc_template_config WHERE template = ?", (template,))
        await db.commit()
        return cur.rowcount > 0
