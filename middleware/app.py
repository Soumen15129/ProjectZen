"""
app.py — ProjectZen Middleware (Tier 2)
FastAPI server. All routes. Entry point.

Run with:
    uvicorn app:app --host 0.0.0.0 --port 8000 --reload

Routes:
    POST /generate          — generate xlsx/docx/pdf/pptx/xml
    POST /ask               — agentic Q&A grounded in document library
    POST /search            — direct keyword/filter search
    POST /upload-ref        — upload reference file
    GET  /download/{id}     — download a generated file
    GET  /documents         — list documents with filters
    GET  /ref/{ref_id}      — serve a reference file
    GET  /health            — health check
"""

import os
import re
import sys
import uuid
import json
import base64
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Startup and progress messages across middleware/ use emoji (✅, ⚠, 🚀, 📄 ...).
# On Windows, Python only picks UTF-8 for stdout when it is an interactive console;
# when it is a pipe or a redirected file it falls back to the cp1252 locale codec and
# the first emoji raises UnicodeEncodeError. That happens inside init_db()'s "✅ DB
# ready" during the startup event, which FastAPI surfaces as "Application startup
# failed. Exiting." — so the server dies whenever its output is redirected (log file,
# service wrapper, CI, container), while working fine when double-clicked into a
# console. cascade_agent.py guards its own stdout, but app.py imports it lazily inside
# the routes, so that guard is not in effect at startup. Doing it here covers every
# module, regardless of import order.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import aiofiles
from fastapi import FastAPI, HTTPException, Request, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from llm_client import complete
from db import (
    init_db, save_document, list_documents, search_documents, get_document,
    # cascade addon
    init_cascade_tables, seed_format_config_defaults,
    save_cascade_session, update_cascade_session_status,
    get_cascade_session, list_cascade_sessions,
    list_cascade_documents, finalise_cascade_document,
    get_cascade_doc_current_version,
    list_format_configs, update_format_config,
    get_cascade_doc_by_node_version,
    # grounding addon
    init_grounding_table, save_grounding_doc, get_grounding_doc,
    list_grounding_docs, delete_grounding_doc,
    # workbook multi-slot
    save_workbook_slot, get_workbook_slot, list_workbook_slots, delete_workbook_slot,
    MAX_WORKBOOK_SLOTS,
    # adhoc template config (Admin -> Template Config)
    save_adhoc_config, get_adhoc_config, list_adhoc_configs, delete_adhoc_config,
)
from extractor import extract_text, extract_from_bytes
from templates import generate, sanitize_xml_ref
from agent import ask_library
from knowledge_graph import (
    get_graph_data_for_frontend, get_all_node_ids, get_node,
    get_nodes_grouped_by_phase, compute_bfs_waves, get_downstream_nodes,
    DEFAULT_OUTPUT_FORMATS,
)

# ── Paths ─────────────────────────────────────────────────────────────────
# Data location is resolved centrally so an installed copy writes to per-user
# app data instead of its own (possibly read-only) program directory.
from paths import DATA_DIR as BASE_DIR, STORE_DIR, REFS_DIR
STORE_DIR.mkdir(parents=True, exist_ok=True)
REFS_DIR.mkdir(parents=True, exist_ok=True)

MODEL    = "claude-opus-4-8"
MAX_TOKS = 32000

# ── App ───────────────────────────────────────────────────────────────────
app = FastAPI(title="ProjectZen Middleware", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await init_db()
    await init_cascade_tables()
    await seed_format_config_defaults()
    await init_grounding_table()

    # First run of an installed copy: register the bundled reference documents.
    # No-op when the user already has grounding configured.
    try:
        from seed_data import install_seed
        rep = await install_seed()
        if rep.get("grounding_rows"):
            print(f"   Seed  : {rep['grounding_rows']} reference docs registered, "
                  f"{rep['refs_copied']} files installed")
        elif rep.get("skipped"):
            print(f"   Seed  : {rep['skipped']}")
    except Exception as _e:
        print(f"   Seed  : skipped ({str(_e)[:90]})")

    # Warm the run-log's account cache in the background. The probe shells out to the
    # Claude CLI (a Node binary, ~6s of startup), so paying it once here means no
    # generation ever waits for it — an adhoc run can finish in less time than the
    # probe takes.
    try:
        import asyncio as _asyncio
        import runlog as _runlog
        _asyncio.create_task(_runlog.warm_account())
        print(f"   Logs  : {_runlog.RUNLOG_DIR}  "
              f"(kept {_runlog.MAX_AGE_DAYS} days / last {_runlog.MAX_RUNS} runs)")
    except Exception as _e:
        print(f"   Logs  : disabled ({str(_e)[:90]})")

    print(f"🚀 ProjectZen Middleware started")
    print(f"   Store : {STORE_DIR}")
    print(f"   Refs  : {REFS_DIR}")
    print(f"   Model : {MODEL}")


# ═══════════════════════════════════════════════════════════════════════════
# Pydantic models
# ═══════════════════════════════════════════════════════════════════════════

class FilePayload(BaseModel):
    type:    str                    # "base64" | "text" | "refId"
    ext:     Optional[str] = None
    content: Optional[str] = None   # base64 string or raw text
    name:    Optional[str] = None
    mime:    Optional[str] = None
    refId:   Optional[str] = None


class GenerateRequest(BaseModel):
    userPrompt:     str
    outputFormat:   str = "xlsx"
    fileName:       Optional[str] = None
    fileData:       Optional[FilePayload] = None
    # Multi-file input. `fileData` is KEPT and still honoured so every existing
    # caller — including the CSF XML route and anything outside this repo — keeps
    # working unchanged; when both are supplied the list wins and fileData is
    # folded in as its first entry.
    fileDataList:   List[FilePayload] = []
    # Graph node this template maps to. Selects the model TIER for the quality
    # pipeline; the adhoc label is still what appears in prompts and filenames.
    nodeId:         Optional[str] = ""
    refData:        Optional[FilePayload] = None
    mirrorAspects:  List[str] = []
    assistantMemory: Optional[str] = None
    isRefinement:   bool = False
    username:       Optional[str] = "unknown"


class AskRequest(BaseModel):
    question: str
    username: Optional[str] = None


class SearchRequest(BaseModel):
    query:        str
    format:       Optional[str] = None
    username:     Optional[str] = None
    date_from:    Optional[str] = None
    date_to:      Optional[str] = None
    limit:        int = 20


class UploadRefRequest(BaseModel):
    content:  str
    name:     str
    ext:      str
    mime:     Optional[str] = None
    refType:  str = "base64"


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def safe_cell(v: Any, maxlen: int = 100) -> str:
    if v is None:
        return ""
    s = str(v)
    s = "".join(c if 32 <= ord(c) < 127 else " " for c in s)
    return s[:maxlen].strip()


def sanitize_for_json(text: str) -> str:
    if not text:
        return ""
    s = "".join(c if 32 <= ord(c) < 127 or c == "\n" else " " for c in text)
    s = s.replace('"', "'").replace("\\", " ")
    s = re.sub(r"[\r\n]+", " | ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()[:40000]


def sanitize_plan(obj: Any) -> Any:
    if isinstance(obj, list):
        return [sanitize_plan(i) for i in obj]
    if isinstance(obj, dict):
        return {k: sanitize_plan(v) for k, v in obj.items()}
    if isinstance(obj, str):
        return safe_cell(obj)
    if isinstance(obj, float):
        return obj if obj == obj else 0   # NaN check
    return obj


def clean_json(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```\s*$", "", s)
    start = s.find("{")
    end   = s.rfind("}")
    if start != -1 and end > start:
        s = s[start:end + 1]
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def make_file_name(template_name: str, output_format: str) -> str:
    safe = re.sub(r"\s*→\s*", "_to_", template_name or "Document")
    safe = re.sub(r"[\s]+", "_", safe)
    safe = re.sub(r"[()/:*?\"<>|]", "", safe)
    safe = re.sub(r"_+", "_", safe).strip("_")[:60]
    now  = datetime.utcnow()
    ts   = now.strftime("%Y-%m-%d_%H-%M-%S")
    ext  = output_format.lower()
    return f"{safe}_{ts}.{ext}"


async def _resolve_inputs(req: "GenerateRequest"):
    """
    Extract every supplied input file and merge them into one source text.

    Returns (text, report). The report travels back to the browser so the user is
    told which files contributed, which yielded nothing, and which formats extract
    poorly — a .mpp that silently produced a bag of strings should not look the same
    to the user as a clean .docx.

    Single-file requests take the original path byte for byte: no headers, no
    wrapper, nothing about the prompt changes. Multi-file support must not alter
    single-file output.
    """
    from input_merge import merge, InputTooLarge

    payloads: List[FilePayload] = []
    if req.fileData:
        payloads.append(req.fileData)
    for fd in (req.fileDataList or []):
        # Guard against the browser sending the same file in both fields.
        if req.fileData and fd.name and fd.name == req.fileData.name:
            continue
        payloads.append(fd)

    if not payloads:
        return "", {"files": [], "warnings": [], "skipped": []}

    # Adhoc reads far more of each file than the shared 80,000-character default.
    # A 27 MB SP051 deck is a real input here, and cutting it at 80K was dropping
    # scope that the pipeline then could not possibly plan for. Cascade keeps the
    # old ceiling: it was measured and tuned against it, and is out of scope.
    per_file_cap = 250_000

    extracted = []
    truncated = []
    for fd in payloads:
        try:
            text = await resolve_file(fd, max_chars=per_file_cap)
        except Exception as exc:                      # one bad file must not kill the run
            print(f"   ⚠ extraction failed for {fd.name}: {str(exc)[:120]}")
            text = ""
        # EVERY extractor caps its output at 80,000 characters and says nothing.
        # A 27 MB deck therefore arrives as its first 80K characters with no signal
        # anywhere, which is exactly how scope goes missing unnoticed. We cannot see
        # inside the extractor, but a result landing exactly on the ceiling is the
        # tell — and the user is far better served by a warning that is occasionally
        # a false positive than by silence that is routinely a real loss.
        if len(text) >= per_file_cap:
            truncated.append(fd.name or "(unnamed)")
        extracted.append({"name": fd.name or "(unnamed)",
                          "ext": (fd.ext or "").lower(), "text": text})

    try:
        merged = merge(extracted)
    except InputTooLarge as exc:
        # Deliberately surfaced rather than silently trimmed: quiet truncation is how
        # scope goes missing without anyone noticing.
        raise HTTPException(413, exc.message)

    for name in truncated:
        merged["warnings"].append({
            "file": name, "ext": "",
            "message": "This file is very large and only its first "
                       f"{per_file_cap:,} characters "
                       "were read. Content beyond that point was not used. Consider "
                       "splitting it, or removing sections that are not relevant to "
                       "this document.",
        })
    for w in merged["warnings"]:
        print(f"   ⚠ input: {w['file']} — {w['message'][:90]}")
    return merged["text"], {k: merged[k] for k in ("files", "skipped", "warnings")}


# Reference types accepted from either side. Each must be readable AS A STRUCTURE and
# emittable as output, because the reference decides the output format. Legacy
# .ppt/.doc/.xls are excluded deliberately: python-pptx, python-docx and openpyxl
# cannot open them, so accepting one would lock output to a format we could never
# produce from a file we could never read.
REF_EXTS = {"xlsx", "docx", "pptx", "pdf", "xml"}
LEGACY_REF_EXTS = {"ppt": "pptx", "doc": "docx", "xls": "xlsx"}


def _materialise_reference(fd: Optional[FilePayload]) -> str:
    """
    Write a user-supplied reference to disk and PII-scrub it. Returns "" on failure.

    Scrubbed for the same reason admin references are: once this file is grounding,
    its author metadata, emails and phone numbers are being fed to the model and can
    surface in generated output. The rule is about the GROUNDED document, and a user
    upload that becomes grounding is one.

    Failure is never fatal — the caller still has the extracted text, so the worst
    case is the previous behaviour rather than a failed generation.
    """
    if not fd or fd.type != "base64" or not fd.content:
        return ""
    ext = (fd.ext or "").lower().lstrip(".")
    if ext not in REF_EXTS:
        print(f"   ⚠ reference .{ext} is not an accepted reference type; ignored")
        return ""
    try:
        import base64 as _b64, tempfile as _tf, time as _t
        # Sweep older copies instead of wrapping the whole route in try/finally.
        # The file has to outlive plan generation, authoring AND rendering, all of
        # which read grounding_path, so deleting it inline is not an option — and
        # re-indenting a working 150-line route to add a finally is how unrelated
        # breakage gets introduced. An hour is far longer than any run.
        try:
            _tmp = _tf.gettempdir()
            for _old in os.listdir(_tmp):
                if _old.startswith("pz_userref_"):
                    _p = os.path.join(_tmp, _old)
                    if _t.time() - os.path.getmtime(_p) > 3600:
                        os.unlink(_p)
        except Exception:
            pass
        raw = _b64.b64decode(fd.content)
        fd_no, path = _tf.mkstemp(prefix="pz_userref_", suffix=f".{ext}")
        with os.fdopen(fd_no, "wb") as fh:
            fh.write(raw)
        try:
            from pii_scrub import scrub_file
            rep = scrub_file(path)
            red = rep.get("redactions") or {}
            if red:
                print(f"   🔒 user reference scrubbed {red}")
        except Exception as exc:
            print(f"   ⚠ user reference could not be scrubbed ({type(exc).__name__}); "
                  f"not using it as grounding")
            try:
                os.unlink(path)
            except OSError:
                pass
            return ""
        return path
    except Exception as exc:
        print(f"   ⚠ user reference could not be prepared ({type(exc).__name__})")
        return ""


# Template name the Data Model Agent portal posts. Routing on the template rather
# than on keywords in the prompt: keyword sniffing is what made the CSF route fire
# unpredictably, and this portal has exactly one template by design.
DATA_MODEL_TEMPLATE = "Data Model"


def _first_payload(req: "GenerateRequest") -> Optional[FilePayload]:
    """
    The single input file, wherever the client put it.

    The multi-file frontend posts through fileDataList; the legacy field stays empty.
    Routes that genuinely take one file must look in both or they silently stop
    firing for the very inputs they exist to handle.
    """
    if req.fileData:
        return req.fileData
    for fd in (req.fileDataList or []):
        return fd
    return None


async def resolve_file(fd: Optional[FilePayload],
                       max_chars: int = 80000) -> str:
    """
    Resolve a FilePayload to extracted text.

    `max_chars` defaults to the historical 80,000 so every existing caller behaves
    exactly as before; only the adhoc input path raises it.
    """
    if not fd:
        return ""
    if fd.type == "text":
        return (fd.content or "")[:max_chars]
    if fd.type == "base64" and fd.content and fd.ext:
        raw = base64.b64decode(fd.content)
        return extract_from_bytes(raw, fd.ext, max_chars=max_chars)
    if fd.type == "refId" and fd.refId:
        files = list(REFS_DIR.glob(f"{fd.refId}*"))
        if not files:
            print(f"   ⚠ refId not found: {fd.refId}")
            return ""
        return extract_text(str(files[0]), max_chars=max_chars)
    return ""


# ── FIX C: XML prompt labelling ───────────────────────────────────────────

def build_json_prompt(
    user_prompt: str,
    output_format: str,
    safe_input: str,
    safe_ref: str,
    mirror_aspects: List[str],
    attempt: int,
    is_refinement: bool = False,
    raw_ref_text: str = "",
) -> str:
    parts   = user_prompt.split("\n\nAdditional user instructions:\n")
    sys_p   = f"SYSTEM INSTRUCTIONS:\n{parts[0][:3000]}\n" if parts[0] else ""
    usr_p   = f"USER PERSONALIZATION:\n{parts[1][:1500]}\n" if len(parts) > 1 else ""
    req_blk = (sys_p + usr_p) or f"REQUEST: {user_prompt[:4000]}\n"

    input_blk = f"\nINPUT CONTENT (extract all details):\n{safe_input}\n" if safe_input else ""

    # FIX C: XML reference gets explicit schema label
    if output_format == "xml" and raw_ref_text:
        schema_snippet = sanitize_xml_ref(raw_ref_text)
        ref_blk = (
            f"\nEXACT XML SCHEMA TO FOLLOW:\n{schema_snippet}\n\n"
            "CRITICAL XML INSTRUCTIONS:\n"
            "- Use the SAME root element tag name as the reference\n"
            "- Use the SAME entity/field tag names as the reference\n"
            "- Populate Records with data from INPUT CONTENT\n"
        )
    elif safe_ref:
        ref_blk = f"\nREFERENCE STYLE:\n{safe_ref[:8000]}\n"
    else:
        ref_blk = ""

    mirror  = f"Mirror these aspects: {', '.join(mirror_aspects)}." if mirror_aspects else ""
    warn    = "\n!! PREVIOUS ATTEMPT PRODUCED INVALID JSON. Be extra careful.\n" if attempt > 1 else ""
    refine  = "\nREFINEMENT MODE: Extend/modify the previous document. Output COMPLETE JSON.\n" if is_refinement else ""

    schemas = {
        "xml":  '{"title":"string","subtitle":"string","sheets":[{"name":"string","headers":["string"],"rows":[["string"]],"has_totals":false}],"sections":[{"heading":"string","paragraphs":["string"],"bullets":["string"],"table":{"headers":["string"],"rows":[["string"]]}}]}',
        "xlsx": '{"title":"string","sheets":[{"name":"string","headers":["string"],"rows":[["string"]],"has_totals":false}],"chart_sheet":0,"chart_data_col":1}',
        "docx": '{"title":"string","subtitle":"string","sections":[{"heading":"string","level":1,"paragraphs":["string"],"bullets":["string"],"table":{"headers":["string"],"rows":[["string"]]}}]}',
        "pdf":  '{"title":"string","subtitle":"string","sections":[{"heading":"string","paragraphs":["string"],"table":{"headers":["string"],"rows":[["string"]]}}]}',
        "pptx": '{"title":"string","subtitle":"string","slides":[{"type":"title","title":"string","subtitle":"string"},{"type":"bullets","title":"string","bullets":["string"]},{"type":"table","title":"string","headers":["string"],"rows":[["string"]]},{"type":"chart","title":"string","categories":["string"],"values":[1,2,3],"series_name":"string"}]}',
    }

    detail = {
        "xml":  "Generate AT LEAST 3-5 entities. Each entity: 8-15 records. Use reference field names exactly.",
        "xlsx": "Generate AT LEAST 5-7 sheets. Each sheet: 10-25 data rows. Extract real tasks, dates, owners from input.",
        "docx": "Generate AT LEAST 6-10 sections. Each: 2-4 paragraphs. Tables: 8-15 rows.",
        "pdf":  "Generate AT LEAST 6-10 sections. Each: 2-4 paragraphs.",
        "pptx": "Generate AT LEAST 10-15 slides. Mix title, bullets, table, chart types.",
    }

    return (
        "Return ONLY a valid JSON object. No markdown. No explanation. No backticks.\n"
        + warn + refine + "\n"
        + req_blk + "\n"
        + mirror + "\n"
        + input_blk + "\n"
        + ref_blk + "\n"
        + f"\nSchema for {output_format.upper()} (follow exactly):\n"
        + schemas.get(output_format, schemas["docx"]) + "\n\n"
        + detail.get(output_format, detail["docx"]) + "\n\n"
        + "HARD RULES:\n"
        + "1. Every string value: max 80 chars, ASCII only.\n"
        + "2. No trailing commas.\n"
        + "3. Numbers must be JSON numbers in values arrays.\n"
        + "4. Return ONLY the JSON object."
    )


async def get_content_plan(
    user_prompt: str,
    output_format: str,
    input_text: str,
    ref_text: str,
    mirror_aspects: List[str],
    assistant_memory: Optional[str],
    is_refinement: bool,
    raw_ref_text: str,
) -> Dict:
    safe_input = sanitize_for_json(input_text)
    safe_ref   = "" if output_format == "xml" else sanitize_for_json(ref_text)

    for attempt in range(1, 4):
        print(f"   → Plan attempt {attempt}/3 ({MODEL})...")
        prompt = build_json_prompt(
            user_prompt, output_format, safe_input, safe_ref,
            mirror_aspects, attempt, is_refinement, raw_ref_text,
        )
        if assistant_memory:
            prompt = (
                "You previously generated a document. Summary:\n"
                f"{assistant_memory}\n\n" + prompt
            )

        raw = ""
        try:
            raw = await complete(prompt, model=MODEL)
        except Exception as api_err:
            print(f"   → API error: {str(api_err)[:200]}")
            if attempt == 3:
                raise
            continue

        try:
            plan = json.loads(clean_json(raw))
            return sanitize_plan(plan)
        except Exception as parse_err:
            print(f"   → Parse failed (attempt {attempt}): {str(parse_err)[:100]}")

    # Fallback plan
    return {
        "title": user_prompt[:50],
        "sheets": [{"name": "Data", "headers": ["Item", "Details"], "rows": [["1", "See source"]], "has_totals": False}],
        "sections": [{"heading": "Summary", "level": 1, "paragraphs": ["Document generated."], "bullets": [], "table": None}],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/generate")
async def route_generate(req: GenerateRequest, request: Request):
    """
    Adhoc generation. Traced exactly like a cascade run — "users are failing to
    generate files" does not say which of the two paths they used, so both log.

    A wrapper for the same reason generate_single_document has one: the trace scope
    has to cover the whole call including its failure exits, and the alternative was
    re-indenting a working route.
    """
    import runlog

    _names = ([req.fileData.name] if req.fileData else []) + \
             [f.name for f in (req.fileDataList or []) if f.name]

    # WHICH DOCUMENT was generated, recorded in the run header.
    #
    # Without this the trace says a user ran "adhoc" and produced a file, but not
    # which template — so a log cannot be attributed to the portal it came from, and
    # "show me only the Governance Documentation runs" has nothing to filter on.
    # Parsed from the same "Template: X" line every other consumer reads, so there is
    # one definition of the template name rather than two that can drift.
    _tm = re.search(r"^Template:\s*(.+)$", req.userPrompt or "", re.MULTILINE)
    runlog.start_run(
        "adhoc",
        username=req.username or "unknown",
        template=_tm.group(1).strip() if _tm else "",
        node_id=(req.nodeId or "").strip(),
        output_format=req.outputFormat,
        input_file=", ".join(dict.fromkeys(n for n in _names if n)),
        input_files=list(dict.fromkeys(n for n in _names if n)),
        has_reference=bool(req.refData),
        is_refinement=bool(req.isRefinement),
        prompt_chars=len(req.userPrompt or ""),
    )
    await runlog.emit_env()
    try:
        result = await _route_generate(req, request)
    except HTTPException as exc:
        # A 4xx/5xx returned to the browser is still a failed run to the user.
        runlog.record_error(exc, where="route_generate")
        runlog.finish_run("failed", status_code=exc.status_code,
                          error=str(exc.detail)[:300])
        raise
    except Exception as exc:
        runlog.record_error(exc, where="route_generate")
        runlog.finish_run("failed", error=str(exc)[:300])
        raise
    runlog.finish_run("ok", file_name=result.get("fileName"),
                      size_kb=result.get("sizeKB"))
    return result


async def _route_generate(req: GenerateRequest, request: Request):
    if not req.userPrompt:
        raise HTTPException(400, "userPrompt is required")

    # Report EVERY input file, not just the legacy single-file field. Reading only
    # req.fileData printed "input=none" for a request that carried a full SOW in
    # fileDataList, which made a dropped-input bug look like a user who forgot to
    # attach anything.
    _in = [f.name or "(unnamed)"
           for f in ([req.fileData] if req.fileData else []) + list(req.fileDataList or [])]
    print(f"\n📄 /generate | format={req.outputFormat} | "
          f"input={', '.join(_in) if _in else 'none'}")

    # ── Data Model Agent portal: one workbook in, SuccessFactors data-model XML out ──
    _tm = re.search(r"^Template:\s*(.+)$", req.userPrompt, re.MULTILINE)
    if _tm and _tm.group(1).strip() == DATA_MODEL_TEMPLATE:
        return await _handle_data_model(req, request)

    # ── Detect CSF XML: Excel input + XML output + CSF keyword in prompt/template ──
    _fp = _first_payload(req)
    is_csf = (
        req.outputFormat == "xml"
        and _fp is not None
        and _fp.ext and _fp.ext.lower() in ("xlsx", "xls")
        and any(kw in (req.userPrompt + (req.fileName or "")).lower()
                for kw in ("csf", "country specific", "country-specific",
                           "successfactor", "data model csf", "hris"))
    )

    if is_csf:
        return await _handle_csf_xml(req, request)

    # ── Standard generation flow ───────────────────────────────────────────
    # One engagement's scope arrives spread across a SOW, a workbook, a deck and a
    # process map. Accepting a single file meant the rest of the scope never reached
    # the pipeline at all — the same class of failure as a section silently missing
    # from the deliverable. See input_merge.py.
    input_text, input_report = await _resolve_inputs(req)

    raw_ref_text = ""
    ref_text     = ""
    user_ref_path = ""          # the user's own reference, materialised on disk

    if req.refData:
        if req.outputFormat == "xml":
            raw_ref_text = await resolve_file(req.refData)
        else:
            ref_text = await resolve_file(req.refData)

        # A user reference must be REAL grounding, not just extracted text.
        #
        # It used to reach the model as text only, which quietly made it the weakest
        # kind of reference: `grounding_path` drives the authoring stage (real
        # formulas, fills, column widths) and the depth hint, and text carries none
        # of that. Since the user's file now OVERRIDES the admin template, leaving it
        # text-only would mean attaching your own reference silently downgraded the
        # output — the opposite of why you attached it.
        user_ref_path = _materialise_reference(req.refData)

    # ── Adhoc grounding, applied silently ────────────────────────────────
    # Admin -> Template Config holds a system prompt and a (PII-scrubbed) reference
    # per template. The user never sees or selects it; it simply raises the floor.
    # A reference the USER uploaded still wins — their file is about their project.
    _tmpl = re.search(r"^Template:\s*(.+)$", req.userPrompt, re.MULTILINE)
    _tmpl_name = _tmpl.group(1).strip() if _tmpl else ""
    admin_prompt = ""
    grounding_path = ""
    if _tmpl_name:
        try:
            _cfg = await get_adhoc_config(_tmpl_name)
        except Exception:
            _cfg = None
        if _cfg:
            admin_prompt = _cfg.get("system_prompt") or ""
            if _cfg.get("ref_id"):
                _hits = list(REFS_DIR.glob(f"{_cfg['ref_id']}*"))
                if _hits:
                    grounding_path = str(_hits[0])
                    if not ref_text and not raw_ref_text:
                        ref_text = extract_text(grounding_path)
                        print(f"   Grounding: {_cfg.get('file_name')} "
                              f"({len(ref_text):,} chars) applied to '{_tmpl_name}'")

    # The user's own file wins outright — it is about THEIR project, where the admin
    # template is about a previous engagement. Overriding the path (not just the text)
    # is what makes that real: authoring, the depth hint and the colour extraction all
    # read grounding_path, so a half-override would analyse the admin's file while the
    # prompt described the user's.
    if user_ref_path:
        grounding_path = user_ref_path
        print(f"   Grounding: user-supplied {os.path.basename(user_ref_path)} "
              f"overrides the template reference")

    # ── Output format follows the governing reference ────────────────────
    #
    # The UI locks the format, but the lock is only as good as the page the user has
    # open: a tab left open across an admin change, or any direct API call, can still
    # ask for a format the reference cannot support. Deriving it here makes the
    # guarantee structural rather than cosmetic.
    #
    # Only ever narrows to the reference's own type, and only for the document
    # formats — the XML/CSF and Data Model routes have already returned by this point.
    format_note = ""
    if grounding_path:
        _ref_ext = os.path.splitext(grounding_path)[1].lower().lstrip(".")
        if _ref_ext in REF_EXTS and _ref_ext != (req.outputFormat or "").lower():
            format_note = (
                f"Output format set to {_ref_ext.upper()} to match the reference "
                f"document. Generating as {(req.outputFormat or '?').upper()} would "
                f"discard the layout and styling the reference provides.")
            print(f"   ⚠ outputFormat {req.outputFormat!r} -> {_ref_ext!r} "
                  f"(follows reference)")
            req.outputFormat = _ref_ext

    # Admin guidance leads, the user's own prompt follows and therefore wins on any
    # point they contradict — the user is describing their project, the admin is
    # describing the house style.
    effective_prompt = (admin_prompt + "\n\n" + req.userPrompt
                        if admin_prompt else req.userPrompt)

    # ── Quality pipeline (same agents as cascade) ────────────────────────
    # Adhoc used to make ONE call and render the result, while cascade ran
    # research -> plan -> generate -> review -> rework and then let Claude author the
    # file directly. Same templates, same button, materially weaker output. This runs
    # the identical machinery for a single document; it FAILS OPEN to the original
    # single-shot call below, so the worst case is today's behaviour.
    plan = None
    pipeline_model = ""
    _node_id = (req.nodeId or "").strip()
    if _tmpl_name and req.outputFormat in ("xlsx", "docx", "pptx", "pdf"):
        try:
            import adhoc_pipeline
            _res = await adhoc_pipeline.run(
                template=_tmpl_name, node_id=_node_id,
                output_format=req.outputFormat, user_prompt=effective_prompt,
                input_text=input_text, grounding_text=ref_text,
                grounding_path=grounding_path,
            )
            if _res:
                plan, pipeline_model = _res
                print(f"   [adhoc] pipeline produced '{_tmpl_name}' via {pipeline_model}")
        except Exception as _pe:
            from quality.refusal import is_refusal
            if is_refusal(_pe):
                raise HTTPException(503, str(_pe)[:300])
            print(f"   [adhoc] pipeline error ({str(_pe)[:110]}); single-shot")

    if plan is None:
        plan = await get_content_plan(
            effective_prompt, req.outputFormat, input_text, ref_text,
            req.mirrorAspects, req.assistantMemory, req.isRefinement, raw_ref_text,
        )

    ref_schema = ""
    if req.outputFormat == "xml" and raw_ref_text:
        plan["refSchema"] = raw_ref_text[:20000]
        ref_schema        = raw_ref_text

    template_match = re.search(r"^Template:\s*(.+)$", req.userPrompt, re.MULTILINE)
    template_name  = template_match.group(1).strip() if template_match else (req.fileName or plan.get("title", "Document"))
    out_name       = make_file_name(template_name, req.outputFormat)
    out_path       = str(STORE_DIR / out_name)

    try:
        # AUTHORING FIRST, renderer second — the cascade order.
        # The reviewed plan carries the content; what it cannot express is FORM:
        # formulas, column widths, number formats, conditional fills. Giving Claude
        # the plan plus the reference FILE and letting it build the artefact directly
        # is what produced 60 formulas and inherited styling on the cascade side.
        # Never lowers the floor: any rejection, error or timeout drops through to
        # the renderer below, which is the path adhoc has always taken.
        _authored = False
        authoring_note = ""
        if pipeline_model:
            try:
                import authoring, runlog as _rl
                if authoring.is_enabled(req.outputFormat):
                    _authored, _why, _stats = await authoring.author_document(
                        plan=plan, output_format=req.outputFormat, out_path=out_path,
                        node_label=template_name, grounding_path=grounding_path,
                        model=pipeline_model, run_id=f"adhoc_{uuid.uuid4().hex[:8]}",
                        emit=None, session_id="", node_id=_node_id or "adhoc",
                    )
                    print(f"   [authoring] {'accepted' if _authored else 'declined'}"
                          + (f" ({_why})" if not _authored else f" {_stats}"))

                    # RECORD THE OUTCOME EITHER WAY.
                    #
                    # A declined attempt used to leave no trace at all. A real run spent
                    # 29.5 minutes cloning a 15-slide deck, hit its timeout, was killed
                    # before any ResultMessage arrived — so not even an llm_call row was
                    # written — and the trace showed a clean `status=ok` with an eight
                    # minute total. The user was handed the basic renderer's output with
                    # nothing anywhere saying the good path had been tried and lost.
                    _rl.emit("authoring", accepted=bool(_authored),
                             reason=_why or "", stats=_stats or {})
                    if not _authored:
                        authoring_note = (
                            f"The high-fidelity path did not complete ({_why}), so this "
                            f"file was produced by the standard renderer. It follows the "
                            f"reference's colours, fonts and slide size, but not its "
                            f"per-slide design.")
                else:
                    _rl.emit("authoring", accepted=False,
                             reason=f"not enabled for .{req.outputFormat}", stats={})
            except Exception as _ae:
                print(f"   [authoring] unavailable: {str(_ae)[:110]}")
                _authored = False
                authoring_note = (f"The high-fidelity path was unavailable "
                                  f"({type(_ae).__name__}); the standard renderer was used.")
                try:
                    import runlog as _rl2
                    _rl2.record_error(_ae, where="authoring")
                except Exception:
                    pass

        if not _authored:
            # grounding_path was never passed here, so adhoc output got no template
            # cloning at all - no inherited fills, widths or number formats. Cascade
            # has always passed it; this brings adhoc to the same floor.
            generate(req.outputFormat, plan, out_path, ref_schema,
                     grounding_path=grounding_path or None)
    except Exception as gen_err:
        raise HTTPException(500, f"Generation failed: {str(gen_err)[:400]}")

    size_kb = round(os.path.getsize(out_path) / 1024, 1)

    content_summary = ""
    try:
        content_summary = extract_text(out_path, req.outputFormat)[:8000]
    except Exception:
        content_summary = f"Title: {plan.get('title', template_name)}"

    doc_id = str(uuid.uuid4())
    await save_document(
        id             = doc_id,
        file_name      = out_name,
        template       = template_name,
        output_format  = req.outputFormat,
        file_path      = out_path,
        size_kb        = size_kb,
        username       = req.username or "unknown",
        content_summary= content_summary,
    )

    base_url = str(request.base_url).rstrip("/")
    return {
        "success":      True,
        "fileId":       doc_id,
        "fileName":     out_name,
        "outputFormat": req.outputFormat,
        "sizeKB":       size_kb,
        "downloadUrl":  f"{base_url}/download/{doc_id}?name={out_name}",
        "preview":      f"✅ File ready!\nFile: {out_name}\nSize: {size_kb} KB | Format: {req.outputFormat.upper()}"
                        + (f"\n\nℹ {format_note}" if format_note else "")
                        + (f"\n\n⚠ {authoring_note}" if authoring_note else ""),
        # What actually went in. The user picked the files; they should be told which
        # ones contributed and which quietly produced nothing.
        "inputReport":  input_report,
        # Set only when the requested format was overridden to match the reference.
        # Silently producing a different file type than asked for would be worse than
        # the mismatch it prevents.
        "formatNote":   format_note or None,
        # Which engine produced the file, and why if it was not the best one. A user
        # comparing two outputs needs to know they came from different paths.
        "authoring":    {"used": bool(_authored), "note": authoring_note or None},
    }


async def _handle_data_model(req: GenerateRequest, request: Request):
    """
    Data Model Agent: one .xlsx workbook in, SuccessFactors data-model XML out.

    The agent decides WHICH of the four models the workbook carries data for, so the
    output is one .xml when a single model is found and a .zip when several are —
    Succession and Corporate are separate SAP artefacts with different DOCTYPEs and
    cannot validly be merged.

    Diagnostics travel back to the browser rather than being logged and forgotten:
    "which models did it find, how was each produced, did any fail validation" is the
    difference between a file the user can trust and one they have to open and audit.
    """
    import tempfile, base64
    import data_model_agent as dma

    fd = _first_payload(req)
    if fd is None:
        raise HTTPException(400, "A configuration workbook (.xlsx) is required.")
    if (fd.ext or "").lower() not in ("xlsx", "xlsm"):
        raise HTTPException(
            400, "The Data Model Agent reads Excel workbooks only. "
                 f"'{fd.name or 'that file'}' is not an .xlsx file.")

    print("   🧬 Data Model Agent — inspecting workbook")

    wb_tmp, made_tmp = "", False
    if fd.type == "base64" and fd.content:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(base64.b64decode(fd.content))
            wb_tmp, made_tmp = tmp.name, True
    elif fd.type == "refId" and fd.refId:
        hits = list(REFS_DIR.glob(f"{fd.refId}*"))
        if not hits:
            raise HTTPException(400, "Reference file not found")
        wb_tmp = str(hits[0])
    else:
        raise HTTPException(400, "Workbook content required for data-model export")

    base_name = os.path.splitext(make_file_name("Data_Model", "xml"))[0]

    # node_scope, so this run renders in the Logs tab the way a cascade document does.
    # The report groups stages and artefacts UNDER A NODE and drops stage events that
    # carry no node id — without this the per-model timings would be written to the
    # trace and then silently omitted from the very screen they exist for.
    import runlog
    try:
        with runlog.node_scope("data-model"):
            runlog.emit("node_meta", label="Data Model Agent", tier="T1",
                        model=dma.MODEL, output_format="xml",
                        grounding_resolved=None, grounding_ref="four reference models")
            result = await dma.run(wb_tmp, req.userPrompt, str(STORE_DIR),
                                   base_name=base_name)
    finally:
        if made_tmp:
            try:
                os.unlink(wb_tmp)
            except OSError:
                pass

    if not result["files"]:
        # Nothing generated is a real answer, not a 500 — the workbook may simply not
        # hold data-model data, or the references may not be configured yet.
        raise HTTPException(422, " ".join(result["notes"] + result["problems"]) or
                            "No data model could be produced from this workbook.")

    out_path, kind = dma.package(result["files"], str(STORE_DIR), base_name)
    out_name = os.path.basename(out_path)
    size_kb  = round(os.path.getsize(out_path) / 1024, 1)
    runlog.emit("artifact", node_id="data-model", file_name=out_name,
                size_kb=size_kb, output_format=kind,
                authored=[f["key"] for f in result["files"]])

    produced = ", ".join(f["label"] for f in result["files"])
    invalid  = [f for f in result["files"] if not f["valid"]]

    try:
        with open(out_path, "r", encoding="utf-8") as fh:
            content_summary = fh.read(8000)
    except Exception:
        content_summary = f"SuccessFactors data model export: {produced}"

    doc_id = str(uuid.uuid4())
    await save_document(
        id              = doc_id,
        file_name       = out_name,
        template        = DATA_MODEL_TEMPLATE,
        output_format   = kind,
        file_path       = out_path,
        size_kb         = size_kb,
        username        = req.username or "unknown",
        content_summary = content_summary,
    )

    lines = [f"✅ Data model export ready!", f"File: {out_name}",
             f"Size: {size_kb} KB", f"Models generated: {produced}"]
    for f in result["files"]:
        how = "exact converter" if f["method"] == "deterministic" else "AI agent"
        mark = "✓" if f["valid"] else "⚠"
        lines.append(f"  {mark} {f['label']} — {how}")
    if invalid:
        lines.append("")
        lines.append("⚠ Structure warnings — review before importing into SAP:")
        for f in invalid:
            lines.append(f"  • {f['label']}: " + "; ".join(f["problems"]))
    for n in result["notes"] + result["problems"]:
        lines.append(f"  • {n}")

    base_url = str(request.base_url).rstrip("/")
    return {
        "success":       True,
        "fileId":        doc_id,
        "fileName":      out_name,
        "outputFormat":  kind,
        "sizeKB":        size_kb,
        "downloadUrl":   f"{base_url}/download/{doc_id}?name={out_name}",
        "preview":       "\n".join(lines),
        "dataModel":     {
            "detected":  result["detected"],
            "files":     [{k: f[k] for k in ("key", "label", "name", "method",
                                             "valid", "problems")}
                          for f in result["files"]],
            "notes":     result["notes"],
            "problems":  result["problems"],
        },
    }


async def _handle_csf_xml(req: GenerateRequest, request: Request):
    """
    Dedicated handler for Excel → CSF XML conversion.
    Bypasses Claude entirely — reads CSF sheets directly from the workbook
    and produces the exact SAP SF country-specific-fields XML structure.
    """
    import tempfile, base64

    print(f"   🌍 CSF XML route detected — bypassing Claude, reading CSF sheets directly")

    # Write the uploaded Excel to a temp file
    fd = _first_payload(req)
    if fd is None:
        raise HTTPException(400, "Excel file content required for CSF XML generation")
    if fd.type == "base64" and fd.content:
        raw = base64.b64decode(fd.content)
        suffix = f".{fd.ext or 'xlsx'}"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            excel_tmp = tmp.name
    elif fd.type == "refId" and fd.refId:
        files = list(REFS_DIR.glob(f"{fd.refId}*"))
        if not files:
            raise HTTPException(400, "Reference file not found")
        excel_tmp = str(files[0])
    else:
        raise HTTPException(400, "Excel file content required for CSF XML generation")

    template_match = re.search(r"^Template:\s*(.+)$", req.userPrompt, re.MULTILINE)
    template_name  = template_match.group(1).strip() if template_match else (req.fileName or "CSF_DataModel")
    out_name       = make_file_name(template_name, "xml")
    out_path       = str(STORE_DIR / out_name)

    try:
        from templates import generate_csf_xml
        generate_csf_xml(excel_tmp, out_path)
    except Exception as gen_err:
        raise HTTPException(500, f"CSF XML generation failed: {str(gen_err)[:400]}")
    finally:
        # Clean up temp file if we created it
        if fd.type == "base64":
            try:
                os.unlink(excel_tmp)
            except Exception:
                pass

    size_kb = round(os.path.getsize(out_path) / 1024, 1)

    # Read a snippet for content_summary
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            content_summary = f.read(8000)
    except Exception:
        content_summary = f"CSF XML: {template_name}"

    doc_id = str(uuid.uuid4())
    await save_document(
        id             = doc_id,
        file_name      = out_name,
        template       = template_name,
        output_format  = "xml",
        file_path      = out_path,
        size_kb        = size_kb,
        username       = req.username or "unknown",
        content_summary= content_summary,
    )

    base_url = str(request.base_url).rstrip("/")
    return {
        "success":      True,
        "fileId":       doc_id,
        "fileName":     out_name,
        "outputFormat": "xml",
        "sizeKB":       size_kb,
        "downloadUrl":  f"{base_url}/download/{doc_id}?name={out_name}",
        "preview":      (
            f"✅ CSF XML ready!\nFile: {out_name}\n"
            f"Size: {size_kb} KB | SAP SuccessFactors country-specific-fields format"
        ),
    }


@app.post("/ask")
async def route_ask(req: AskRequest, request: Request):
    """Agentic Q&A grounded in document library."""
    if not req.question.strip():
        raise HTTPException(400, "question is required")

    base_url = str(request.base_url).rstrip("/")
    result   = await ask_library(req.question, req.username, base_url)

    return {
        "success":        True,
        "answer":         result["answer"],
        "documents":      result["documents"],
        "toolCallsMade":  result["tool_calls_made"],
    }


@app.post("/search")
async def route_search(req: SearchRequest):
    """Direct keyword/filter search — no agent loop."""
    results = await search_documents(
        query         = req.query,
        output_format = req.format,
        username      = req.username,
        date_from     = req.date_from,
        date_to       = req.date_to,
        limit         = req.limit,
    )
    return {"success": True, "count": len(results), "documents": results}


@app.post("/upload-ref")
async def route_upload_ref(req: UploadRefRequest):
    """Store an uploaded reference file and return its refId."""
    ref_id    = str(uuid.uuid4())
    safe_ext  = re.sub(r"[^a-zA-Z0-9]", "", req.ext)
    file_name = f"{ref_id}.{safe_ext}"
    file_path = REFS_DIR / file_name

    if req.refType == "text":
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(req.content)
    else:
        raw = base64.b64decode(req.content)
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(raw)

    size_kb = round(os.path.getsize(file_path) / 1024, 1)
    print(f"   📎 Ref saved: {req.name} ({size_kb} KB) → {file_name}")

    return {
        "success": True,
        "refId":   ref_id,
        "fileName": req.name,
        "ext":      safe_ext,
        "sizeKB":   size_kb,
    }


@app.get("/download/{file_id}")
async def route_download(file_id: str, name: Optional[str] = Query(None)):
    """Download a generated file by its document ID."""
    doc = await get_document(file_id)
    if not doc:
        raise HTTPException(404, "Document not found")

    file_path = doc["file_path"]
    if not os.path.exists(file_path):
        raise HTTPException(404, "File not found on disk")

    return FileResponse(
        path            = file_path,
        filename        = name or doc["file_name"],
        media_type      = "application/octet-stream",
    )


@app.get("/documents")
async def route_documents(
    username: Optional[str] = None,
    format:   Optional[str] = None,
    template: Optional[str] = None,
    limit:    int = 100,
):
    docs = await list_documents(username=username, output_format=format, template=template, limit=limit)
    return {"success": True, "count": len(docs), "documents": docs}


@app.get("/ref/{ref_id}")
async def route_ref(ref_id: str):
    """Serve a reference file."""
    files = list(REFS_DIR.glob(f"{ref_id}*"))
    if not files:
        raise HTTPException(404, "Reference file not found")
    return FileResponse(path=str(files[0]), media_type="application/octet-stream")


@app.get("/health")
async def route_health():
    from db import DB_PATH
    import aiosqlite
    doc_count = 0
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM documents") as cur:
                row = await cur.fetchone()
                doc_count = row[0] if row else 0
    except Exception:
        pass

    return {
        "status":           "ok",
        "model":            MODEL,
        "version":          "1.0.0",
        "documents_stored": doc_count,
        "store_dir":        str(STORE_DIR),
        "timestamp":        datetime.utcnow().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Run logs — the Logs tab
#
# Read-only over runlog's JSONL traces. Every one of these works on a RUNNING run as
# well as a finished one: events are flushed whole as they happen, so a mid-run read
# returns a complete prefix. That is what lets a user export while a generation is
# still going and show you where it is stuck.
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/logs/runs")
async def route_logs_runs():
    """Index for the History section: newest first."""
    import runlog
    return {
        "runs": runlog.list_runs(),
        "retention": {"max_runs": runlog.MAX_RUNS, "max_age_days": runlog.MAX_AGE_DAYS},
        "log_dir": str(runlog.RUNLOG_DIR),
    }


@app.get("/logs/current")
async def route_logs_current():
    """
    The run the Current section shows: whatever is in flight, else the most recent
    one. Falling back to the last run means the panel is never an empty box.
    """
    import runlog
    import runlog_report
    runs = runlog.list_runs()
    if not runs:
        return {"run": None, "live": False}
    live = next((r for r in runs if r.get("status") == "running"), None)
    target = live or runs[0]
    events = runlog.read_run(target["run_id"])
    return {
        "run": runlog_report.summarise(events),
        "live": bool(live),
        "retention": {"max_runs": runlog.MAX_RUNS, "max_age_days": runlog.MAX_AGE_DAYS},
    }


@app.get("/logs/runs/{run_id}")
async def route_logs_run(run_id: str):
    """Parsed detail for one run."""
    import runlog
    import runlog_report
    if not runlog.valid_run_id(run_id):
        raise HTTPException(400, "invalid run id")
    events = runlog.read_run(run_id)
    if not events:
        raise HTTPException(404, "no such run")
    return runlog_report.summarise(events)


@app.get("/logs/runs/{run_id}/raw")
async def route_logs_run_raw(run_id: str):
    """The unparsed event list, for the detail view's raw tail."""
    import runlog
    if not runlog.valid_run_id(run_id):
        raise HTTPException(400, "invalid run id")
    events = runlog.read_run(run_id)
    if not events:
        raise HTTPException(404, "no such run")
    return {"run_id": run_id, "events": events}


@app.get("/logs/runs/{run_id}/download")
async def route_logs_download(run_id: str, desktop: bool = True):
    """
    Download one run as a zip — raw JSONL, rendered report, summary, server logs.

    Also writes a copy to Desktop\\ProjectZen Logs by default, because the browser
    drops downloads in a folder the user then has to go find. `?desktop=false` skips
    that.
    """
    import runlog
    import runlog_report
    if not runlog.valid_run_id(run_id):
        raise HTTPException(400, "invalid run id")
    if not runlog.read_run(run_id):
        raise HTTPException(404, "no such run")

    data = runlog_report.build_zip([run_id])
    saved = runlog_report.save_to_desktop([run_id]) if desktop else None
    name = runlog_report.zip_name([run_id])
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if saved:
        headers["X-Saved-To"] = saved
    return Response(content=data, media_type="application/zip", headers=headers)


class LogExportRequest(BaseModel):
    runIds: List[str] = []
    desktop: bool = True


@app.post("/logs/export")
async def route_logs_export(req: LogExportRequest):
    """
    Export a selection of runs as one zip — one, several, or all of history.

    Chasing an intermittent failure means comparing the attempt that worked with the
    one that did not, so bundling several runs together is the useful shape.
    """
    import runlog
    import runlog_report
    ids = [r for r in (req.runIds or []) if runlog.valid_run_id(r)]
    if not ids:
        raise HTTPException(400, "no valid run ids")
    data = runlog_report.build_zip(ids)
    saved = runlog_report.save_to_desktop(ids) if req.desktop else None
    name = runlog_report.zip_name(ids)
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if saved:
        headers["X-Saved-To"] = saved
    return Response(content=data, media_type="application/zip", headers=headers)


# ═══════════════════════════════════════════════════════════════════════════
# Admin -> Template Config  (adhoc grounding)
#
# The UI for this shipped long ago with no server behind it: the system prompt went
# to localStorage and the reference file POSTed a shape /upload-ref rejects with 422.
# Nothing was ever read back at generation time. These routes are the missing half.
#
# Every uploaded reference is PII-SCRUBBED BEFORE IT IS STORED. A grounding document
# is another client's deliverable reused as a template, and templates.generate()
# clones cells out of it straight into a delivered file — so cleaning at upload is
# what makes every downstream consumer safe by construction.
# ═══════════════════════════════════════════════════════════════════════════

class AdhocConfigRequest(BaseModel):
    template:     str
    nodeId:       Optional[str] = ""
    systemPrompt: Optional[str] = ""
    refData:      Optional[str] = None      # base64
    refFileName:  Optional[str] = None
    username:     Optional[str] = "admin"


@app.get("/admin/adhoc-config")
async def route_adhoc_config_list():
    rows = await list_adhoc_configs()
    return {"configs": rows, "count": len(rows)}


@app.get("/admin/adhoc-config/{template:path}")
async def route_adhoc_config_get(template: str):
    cfg = await get_adhoc_config(template)
    if not cfg:
        return {"template": template, "configured": False}
    return {**cfg, "configured": True}


@app.post("/admin/adhoc-config")
async def route_adhoc_config_save(req: AdhocConfigRequest):
    if not req.template:
        raise HTTPException(400, "template is required")

    ref_id = file_name = file_ext = ""
    size_kb = 0.0
    redactions = ""

    if req.refData:
        import json as _json
        from pii_scrub import scrub_file
        ref_id   = str(uuid.uuid4())
        file_name = req.refFileName or "reference"
        file_ext  = re.sub(r"[^a-zA-Z0-9]", "", file_name.rsplit(".", 1)[-1])[:8] or "bin"
        path = REFS_DIR / f"{ref_id}.{file_ext}"
        raw = base64.b64decode(req.refData)
        async with aiofiles.open(path, "wb") as f:
            await f.write(raw)
        rep = scrub_file(str(path))
        redactions = _json.dumps(rep.get("redactions") or {})
        size_kb = round(os.path.getsize(path) / 1024, 1)
        print(f"   Template ref: {file_name} ({size_kb} KB) scrubbed {rep.get('redactions')}")

    await save_adhoc_config(
        template=req.template, node_id=req.nodeId or "",
        system_prompt=req.systemPrompt or "", ref_id=ref_id,
        file_name=file_name, file_ext=file_ext, size_kb=size_kb,
        redactions=redactions, updated_by=req.username or "admin",
    )
    return {"success": True, "template": req.template,
            "refStored": bool(ref_id), "redactions": redactions}


@app.delete("/admin/adhoc-config/{template:path}")
async def route_adhoc_config_delete(template: str):
    return {"success": await delete_adhoc_config(template)}


@app.get("/health/llm")
async def route_health_llm():
    """
    Whether this process can actually reach Claude, and if not, why.

    Every cause below surfaces identically ("generation failed") without this:
    no CLI, CLI but no login, or a model the seat cannot use. Makes one real call,
    so it is a diagnostic endpoint rather than a liveness probe.
    """
    from llm_client import preflight
    info = await preflight()
    return {**info, "timestamp": datetime.utcnow().isoformat()}


@app.get("/health/memory")
async def route_health_memory():
    """In-process checkpoint store occupancy (bounded — see quality/checkpoint.py)."""
    try:
        from quality.checkpoint import mem_stats
        return {"checkpoint": mem_stats(), "timestamp": datetime.utcnow().isoformat()}
    except Exception as exc:
        return {"error": str(exc)[:160]}


# ═══════════════════════════════════════════════════════════════════════════════
# ACCOUNT ROUTES  (the profile popup — read-only, no state anywhere)
# ═══════════════════════════════════════════════════════════════════════════════
@app.get("/account")
async def route_account():
    """
    Who is actually signed in — the real Claude account, not a placeholder.

    runlog already probes this for every trace, so the identity was on disk long
    before the UI wanted it; this route only publishes what the cache holds.

    `orgId` is deliberately not returned: it is a raw UUID with nothing to say to a
    person reading a profile card.
    """
    import runlog

    acct = runlog.account_cached()
    if not acct:
        # Cache cold (first seconds after startup) or past its 15-minute TTL. The
        # probe shells out to the Claude CLI and costs a few seconds, so it runs off
        # the event loop — but it DOES run, because a profile card that renders blank
        # is worse than one that takes a moment to appear.
        try:
            acct = await asyncio.to_thread(runlog._claude_account, True)
        except Exception as exc:
            acct = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    return {
        "loggedIn":    bool(acct.get("loggedIn")),
        "email":       acct.get("email") or "",
        "orgName":     acct.get("orgName") or "",
        "plan":        acct.get("subscriptionType") or "",
        "authMethod":  acct.get("authMethod") or "",
        "apiProvider": acct.get("apiProvider") or "",
        "error":       acct.get("error") or "",
    }


@app.get("/account/usage")
async def route_account_usage():
    """
    ProjectZen spend for the SIGNED-IN ACCOUNT, bucketed into today / 7 days / 30 days.

    Scoped to the account on purpose. This folded every trace in the folder at first,
    which meant a shared machine reported one person's spend as another's and made
    the figure identical no matter who signed in. Traces record their own account, so
    the fix is a filter — see usage_totals().

    Signed out (or the CLI cannot be read) returns no figures at all rather than the
    machine-wide total: an unattributed number here is exactly the bug being fixed.

    Folded off the event loop — bounded to 50 files by retention, but still file I/O
    that has no business blocking the server while a generation is streaming.
    """
    import runlog
    import runlog_report

    acct = runlog.account_cached()
    if not acct:
        try:
            acct = await asyncio.to_thread(runlog._claude_account, True)
        except Exception:
            acct = {}
    email = (acct or {}).get("email") or ""
    if not email:
        return {"error": "not signed in", "account": ""}

    try:
        return await asyncio.to_thread(runlog_report.usage_totals, email)
    except Exception as exc:
        # The popup degrades to "couldn't read usage" rather than showing an error
        # page — nothing here is important enough to interrupt anyone.
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}", "account": email}


# ═══════════════════════════════════════════════════════════════════════════════
# CASCADE ADDON ROUTES  (user login only — enforced by frontend)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Pydantic models for cascade ───────────────────────────────────────────────

class CascadeStartRequest(BaseModel):
    fileData:      FilePayload
    selectedNodes: List[str] = []      # empty = all 32
    rootNodeId:    Optional[str] = None  # root/input doc node ID (excluded from generation)
    username:      Optional[str] = "unknown"


class DeltaAnalyseRequest(BaseModel):
    sessionId:     str                 # Source session containing the old version
    nodeId:        str                 # Which document changed
    fromVersion:   int = 1             # Version to compare against
    inputType:     str = "full_doc"    # "full_doc" | "delta_section" | "prompt"
    fileData:      Optional[FilePayload] = None   # For full_doc / delta_section
    promptText:    Optional[str] = None           # For prompt mode
    username:      Optional[str] = "unknown"


class DeltaApplyRequest(BaseModel):
    sourceSessionId:      str
    deltaSessionId:       str
    deltaNodeId:          str
    selectedNodes:        List[str]              # Nodes user confirmed to update
    finalisedDecisions:   Dict[str, bool] = {}   # node_id -> True = include
    username:             Optional[str] = "unknown"


class FinaliseDocRequest(BaseModel):
    sessionId: str
    nodeId:    str
    version:   int
    username:  Optional[str] = "unknown"


class FormatConfigUpdateRequest(BaseModel):
    nodeId:        str
    outputFormat:  str
    isDefaultSel:  int = 1
    username:      Optional[str] = "admin"


class GroundingUploadRequest(BaseModel):
    nodeId:   str
    content:  str            # base64-encoded file bytes
    name:     str            # original file name shown to user
    ext:      str            # file extension (xlsx / docx / pdf / xml)
    mime:     Optional[str] = None
    username: Optional[str] = "admin"


# ── GET /cascade/graph-data ───────────────────────────────────────────────────

@app.get("/cascade/graph-data")
async def route_cascade_graph_data():
    """Return the full knowledge graph data for the frontend Cytoscape.js visualization."""
    return {"success": True, "data": get_graph_data_for_frontend()}


# ── POST /cascade/start ───────────────────────────────────────────────────────

@app.post("/cascade/start")
async def route_cascade_start(req: CascadeStartRequest, request: Request):
    """
    Start a new cascade generation session.
    Extracts text from the uploaded document, then fires the cascade in the background.
    Returns session_id immediately — client connects to /cascade/stream/{session_id} for SSE.
    """
    if not req.fileData:
        raise HTTPException(400, "fileData is required")

    # Extract input document text
    input_text = await resolve_file(req.fileData)
    if not input_text.strip():
        raise HTTPException(400, "Could not extract text from the uploaded document")

    # Determine selected nodes
    selected = req.selectedNodes if req.selectedNodes else get_all_node_ids()
    # Validate node ids
    valid_ids  = set(get_all_node_ids())
    selected   = [n for n in selected if n in valid_ids]
    if not selected:
        raise HTTPException(400, "No valid document nodes selected")

    session_id = str(uuid.uuid4())
    file_name  = req.fileData.name or "input_document"

    # Set up SSE queue before launching background task
    from context_store import create_sse_queue
    create_sse_queue(session_id)

    # Launch cascade in background (non-blocking)
    from cascade_agent import run_new_cascade
    asyncio.create_task(run_new_cascade(
        session_id=session_id,
        input_text=input_text,
        input_file_name=file_name,
        selected_nodes=selected,
        root_node_id=req.rootNodeId or None,
        username=req.username or "unknown",
    ))

    return {
        "success":    True,
        "sessionId":  session_id,
        "totalDocs":  len(selected),
        "streamUrl":  f"/cascade/stream/{session_id}",
        "message":    f"Cascade started for {len(selected)} documents.",
    }


# ── GET /cascade/stream/{session_id} — SSE endpoint ──────────────────────────

@app.get("/cascade/stream/{session_id}")
async def route_cascade_stream(session_id: str):
    """
    Server-Sent Events stream for a cascade session.
    Drains the asyncio.Queue set up by /cascade/start.
    Each event: data: {json}\n\n
    """
    from context_store import get_sse_queue, cleanup_sse_queue

    async def event_generator():
        q = get_sse_queue(session_id)
        if not q:
            yield f"data: {json.dumps({'event': 'error', 'data': {'message': 'Session not found'}})}\n\n"
            return

        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    # Keep-alive ping
                    yield f": ping\n\n"
                    continue

                if item is None:
                    yield f"data: {json.dumps({'event': 'stream_end', 'data': {}})}\n\n"
                    break

                event_type = item.get("event", "message")
                data       = item.get("data", {})
                yield f"data: {json.dumps({'event': event_type, 'data': data})}\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            cleanup_sse_queue(session_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── POST /cascade/delta/analyse ───────────────────────────────────────────────

@app.post("/cascade/delta/analyse")
async def route_delta_analyse(req: DeltaAnalyseRequest):
    """
    Run Agent 4 (Delta Analysis) to identify what changed and downstream impact.
    Returns delta_items and affected_nodes for the user to review and confirm.
    This is synchronous (waits for analysis to complete, typically 10-30s).
    """
    # Resolve new document text
    new_text = ""
    if req.inputType == "prompt":
        new_text = req.promptText or ""
    elif req.fileData:
        new_text = await resolve_file(req.fileData)

    if not new_text.strip():
        raise HTTPException(400, "No change content provided (prompt, file, or delta section required)")

    from delta_engine import analyse_delta
    result = await analyse_delta(
        session_id=req.sessionId,
        node_id=req.nodeId,
        from_version=req.fromVersion,
        new_content=new_text,
        input_type=req.inputType,
    )

    return {
        "success":       True,
        "deltaItems":    result.delta_items,
        "affectedNodes": result.affected_nodes,
        "deltaSummary":  result.delta_summary,
    }


# ── POST /cascade/delta/apply ─────────────────────────────────────────────────

@app.post("/cascade/delta/apply")
async def route_delta_apply(req: DeltaApplyRequest, request: Request):
    """
    After user confirms scope, start the delta cascade.
    Returns a new session_id for the delta run — client streams from /cascade/stream/{id}.
    """
    if not req.selectedNodes:
        raise HTTPException(400, "No nodes selected for delta update")

    # Load source session's project context and generated docs
    source_session = await get_cascade_session(req.sourceSessionId)
    if not source_session:
        raise HTTPException(404, f"Source session '{req.sourceSessionId}' not found")

    # Load project context from the LangGraph checkpointer state
    # Fallback: rebuild minimal context from session metadata
    from cascade_agent import _checkpointer
    config = {"configurable": {"thread_id": req.sourceSessionId}}
    try:
        snap = await _checkpointer.aget(config)
        state_vals = snap.values if snap else {}
    except Exception:
        state_vals = {}

    project_context = state_vals.get("project_context", {"project_name": "Project", "client_name": "Client", "delivery_partner": "Accenture"})
    generated_docs  = state_vals.get("generated_docs", {})

    # Compute delta waves for selected nodes
    delta_waves = compute_bfs_waves(req.selectedNodes)

    # Create new session_id for the delta run
    delta_session_id = str(uuid.uuid4())

    # Retrieve delta_items from the analyse step (caller must pass them)
    # For now we generate minimal delta_items placeholder
    delta_items   = []
    delta_summary = f"Delta update to {req.deltaNodeId}"

    # Set up SSE queue
    from context_store import create_sse_queue
    create_sse_queue(delta_session_id)

    from cascade_agent import run_delta_cascade
    asyncio.create_task(run_delta_cascade(
        session_id=delta_session_id,
        source_session_id=req.sourceSessionId,
        delta_node_id=req.deltaNodeId,
        delta_waves=delta_waves,
        delta_items=delta_items,
        delta_summary=delta_summary,
        user_delta_nodes=req.selectedNodes,
        user_finalised_choices=req.finalisedDecisions,
        project_context=project_context,
        generated_docs=generated_docs,
        username=req.username or "unknown",
    ))

    return {
        "success":        True,
        "deltaSessionId": delta_session_id,
        "streamUrl":      f"/cascade/stream/{delta_session_id}",
        "totalNodes":     len(req.selectedNodes),
        "deltaWaves":     len(delta_waves),
    }


# ── GET /cascade/session/{session_id} ─────────────────────────────────────────

@app.get("/cascade/session/{session_id}")
async def route_cascade_session(session_id: str, request: Request):
    """Return session metadata + all generated documents."""
    session = await get_cascade_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    docs     = await list_cascade_documents(session_id)
    base_url = str(request.base_url).rstrip("/")

    # Enrich docs with download URLs
    for d in docs:
        d["download_url"] = f"{base_url}/download/{d['file_id']}?name={d['file_name']}"

    return {"success": True, "session": session, "documents": docs}


# ── GET /cascade/sessions ─────────────────────────────────────────────────────

@app.get("/cascade/sessions")
async def route_cascade_sessions(username: Optional[str] = None, limit: int = 50):
    sessions = await list_cascade_sessions(username=username, limit=limit)
    return {"success": True, "count": len(sessions), "sessions": sessions}


# ── POST /cascade/finalise ────────────────────────────────────────────────────

@app.post("/cascade/finalise")
async def route_finalise_doc(req: FinaliseDocRequest):
    """Mark a specific document version as finalised (client signed-off)."""
    ok = await finalise_cascade_document(
        session_id=req.sessionId,
        node_id=req.nodeId,
        version=req.version,
        finalised_by=req.username or "unknown",
    )
    if not ok:
        raise HTTPException(500, "Failed to finalise document")
    return {"success": True, "message": f"{req.nodeId} v{req.version} marked as finalised"}


# ── GET /cascade/format-config ────────────────────────────────────────────────

@app.get("/cascade/format-config")
async def route_get_format_config():
    """Return admin-configurable output format settings for all document types."""
    configs = await list_format_configs()
    return {"success": True, "configs": configs}


# ── PUT /cascade/format-config ────────────────────────────────────────────────

@app.put("/cascade/format-config")
async def route_update_format_config(req: FormatConfigUpdateRequest):
    """Admin: update output format and default selection for a document type."""
    valid_formats = {"xlsx", "docx", "pdf", "pptx", "xml"}
    if req.outputFormat not in valid_formats:
        raise HTTPException(400, f"outputFormat must be one of {valid_formats}")
    valid_nodes = set(get_all_node_ids())
    if req.nodeId not in valid_nodes:
        raise HTTPException(400, f"nodeId '{req.nodeId}' not recognised")

    ok = await update_format_config(
        node_id=req.nodeId,
        output_format=req.outputFormat,
        is_default_sel=req.isDefaultSel,
        updated_by=req.username or "admin",
    )
    if not ok:
        raise HTTPException(500, "Failed to update format config")
    return {"success": True, "message": f"{req.nodeId} → {req.outputFormat} saved"}


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN GROUNDING ROUTES  (admin login only — enforced by frontend)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/grounding")
async def route_admin_grounding_list():
    """
    Return all 32 knowledge graph nodes with their grounding document status.
    Used by the admin Document Grounding panel to colour-code the graph.
    """
    from knowledge_graph import get_all_node_ids, get_node
    docs = await list_grounding_docs()
    grounded_map = {d["node_id"]: d for d in docs}

    nodes = []
    for node_id in get_all_node_ids():
        node      = get_node(node_id)
        grounding = grounded_map.get(node_id)
        nodes.append({
            "node_id":    node_id,
            "label":      node["label"] if node else node_id,
            "phase":      node["phase"] if node else "",
            "tier":       node["tier"]  if node else "",
            "is_grounded": grounding is not None,
            "grounding":   grounding,
        })
    grounded_count = sum(1 for n in nodes if n["is_grounded"])

    # ── Workbook Generator, appended as one more entry in the same list ──────
    #
    # This loop is driven by get_all_node_ids(), so the `workbook:MOD:name` rows
    # are invisible to it — which is exactly why they are safe to keep in the
    # same table, and also why this entry has to be added deliberately rather
    # than appearing on its own. It carries a marker so the admin page knows to
    # open the module/workbook picker instead of a single upload box.
    try:
        import workbook_taxonomy as T
        wb_rows = [d for d in docs if T.is_workbook_row(d.get("node_id", ""))]
        seen = {(T.parse_grounding_id(d["node_id"]) or {}).get("module", "") + "/" +
                (T.parse_grounding_id(d["node_id"]) or {}).get("workbook", "")
                for d in wb_rows}
        nodes.append({
            "node_id":     "workbook-generator",
            "label":       "Workbook Generator",
            "phase":       "Configuration",
            "tier":        "",
            "is_grounded": bool(wb_rows),
            "grounding":   None,
            "kind":        "workbook",          # tells the UI to use the picker
            "modules":     T.all_modules(),
            "file_count":  len(wb_rows),
            "workbook_count": len(seen),
        })
        if wb_rows:
            grounded_count += 1
    except Exception as exc:
        print(f"   ⚠ workbook taxonomy unavailable: {exc}")

    return {
        "success":       True,
        "nodes":         nodes,
        "total":         len(nodes),
        "grounded_count": grounded_count,
    }


@app.post("/admin/grounding/upload")
async def route_admin_grounding_upload(req: GroundingUploadRequest):
    """
    Upload a grounding reference document for a specific knowledge graph node.
    Replaces any existing grounding doc for that node.
    File is saved to REFS_DIR; record is stored in grounding_docs table.
    """
    from knowledge_graph import get_all_node_ids
    valid_nodes = set(get_all_node_ids())
    if req.nodeId not in valid_nodes:
        raise HTTPException(400, f"nodeId '{req.nodeId}' not recognised")

    safe_ext  = re.sub(r"[^a-zA-Z0-9]", "", req.ext)
    ref_id    = str(uuid.uuid4())
    file_path = REFS_DIR / f"{ref_id}.{safe_ext}"

    try:
        raw = base64.b64decode(req.content)
    except Exception:
        raise HTTPException(400, "Invalid base64 content")

    async with aiofiles.open(file_path, "wb") as f:
        await f.write(raw)

    # Scrub BEFORE the file is registered. A grounding document is another client's
    # deliverable reused as a template, and templates.generate() clones cells out of
    # it straight into a delivered file - so cleaning at upload is what makes every
    # downstream consumer safe without each one having to remember.
    _redactions = {}
    try:
        from pii_scrub import scrub_file
        _rep = scrub_file(str(file_path))
        _redactions = _rep.get("redactions") or {}
        if _redactions:
            print(f"   Grounding scrub: {req.fileName} -> {_redactions}")
    except Exception as _e:
        print(f"   Grounding scrub skipped: {str(_e)[:90]}")

    size_kb = round(os.path.getsize(file_path) / 1024, 1)

    # Remove old grounding file from disk before replacing DB record
    old = await get_grounding_doc(req.nodeId)
    if old:
        for old_file in REFS_DIR.glob(f"{old['ref_id']}*"):
            try:
                os.unlink(old_file)
            except Exception:
                pass

    ok = await save_grounding_doc(
        node_id     = req.nodeId,
        ref_id      = ref_id,
        file_name   = req.name,
        file_ext    = safe_ext,
        size_kb     = size_kb,
        uploaded_by = req.username or "admin",
    )
    if not ok:
        raise HTTPException(500, "Failed to save grounding document record")

    print(f"   🔗 Grounding doc saved: {req.nodeId} ← {req.name} ({size_kb} KB)")
    return {
        "success":  True,
        "nodeId":   req.nodeId,
        "refId":    ref_id,
        "fileName": req.name,
        "sizeKB":   size_kb,
    }


@app.delete("/admin/grounding/{node_id}")
async def route_admin_grounding_delete(node_id: str):
    """Remove the grounding document for a knowledge graph node."""
    grounding = await get_grounding_doc(node_id)
    if not grounding:
        raise HTTPException(404, f"No grounding document found for node '{node_id}'")

    for old_file in REFS_DIR.glob(f"{grounding['ref_id']}*"):
        try:
            os.unlink(old_file)
        except Exception:
            pass

    ok = await delete_grounding_doc(node_id)
    if not ok:
        raise HTTPException(500, "Failed to remove grounding document record")

    print(f"   🗑 Grounding doc removed: {node_id}")
    return {"success": True, "message": f"Grounding document removed for '{node_id}'"}


# ═══════════════════════════════════════════════════════════════════════════════
# WORKBOOK MULTI-SLOT ENDPOINTS
# Configuration Workbook node supports up to 4 grounding reference workbooks
# ═══════════════════════════════════════════════════════════════════════════════

class WorkbookSlotUploadRequest(BaseModel):
    slot:     int             # 1–4
    content:  str             # base64-encoded xlsx bytes
    name:     str             # original file name
    ext:      str = "xlsx"
    username: Optional[str] = "admin"


@app.get("/admin/workbook-slots")
async def route_list_workbook_slots():
    """Return status of all 4 configuration workbook grounding slots."""
    occupied = {r["slot"]: r for r in await list_workbook_slots()}
    slots = []
    for i in range(1, MAX_WORKBOOK_SLOTS + 1):
        if i in occupied:
            s = occupied[i]
            slots.append({
                "slot": i,
                "occupied": True,
                "file_name": s["file_name"],
                "file_ext": s["file_ext"],
                "ref_id": s["ref_id"],
                "size_kb": s["size_kb"],
                "uploaded_by": s["uploaded_by"],
                "uploaded_at": s["uploaded_at"],
            })
        else:
            slots.append({"slot": i, "occupied": False})
    return {"success": True, "slots": slots}


@app.post("/admin/workbook-slots/upload")
async def route_upload_workbook_slot(req: WorkbookSlotUploadRequest):
    """Upload a reference workbook for one of the 4 config-workbook grounding slots."""
    if req.slot < 1 or req.slot > MAX_WORKBOOK_SLOTS:
        raise HTTPException(400, f"slot must be 1–{MAX_WORKBOOK_SLOTS}")

    safe_ext  = re.sub(r"[^a-zA-Z0-9]", "", req.ext) or "xlsx"
    ref_id    = str(uuid.uuid4())
    file_path = REFS_DIR / f"{ref_id}.{safe_ext}"

    try:
        raw = base64.b64decode(req.content)
    except Exception:
        raise HTTPException(400, "Invalid base64 content")

    async with aiofiles.open(file_path, "wb") as f:
        await f.write(raw)

    size_kb = round(os.path.getsize(file_path) / 1024, 1)

    # Delete old file for this slot
    old = await get_workbook_slot(req.slot)
    if old:
        for old_file in REFS_DIR.glob(f"{old['ref_id']}*"):
            try:
                os.unlink(old_file)
            except Exception:
                pass

    ok = await save_workbook_slot(
        slot=req.slot, ref_id=ref_id, file_name=req.name,
        file_ext=safe_ext, size_kb=size_kb, uploaded_by=req.username or "admin",
    )
    if not ok:
        raise HTTPException(500, "Failed to save workbook slot record")

    print(f"   📊 Workbook slot {req.slot} saved: {req.name} ({size_kb} KB)")
    return {"success": True, "slot": req.slot, "refId": ref_id,
            "fileName": req.name, "sizeKB": size_kb}


@app.delete("/admin/workbook-slots/{slot}")
async def route_delete_workbook_slot(slot: int):
    """Remove the grounding workbook from a specific slot."""
    existing = await get_workbook_slot(slot)
    if not existing:
        raise HTTPException(404, f"No workbook in slot {slot}")
    for old_file in REFS_DIR.glob(f"{existing['ref_id']}*"):
        try:
            os.unlink(old_file)
        except Exception:
            pass
    await delete_workbook_slot(slot)
    print(f"   🗑 Workbook slot {slot} removed")
    return {"success": True, "slot": slot}


# ═══════════════════════════════════════════════════════════════════════════════
# WORKBOOK GENERATOR ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

class ExtractCountriesRequest(BaseModel):
    fileData: str       # base64
    fileName: str


class GenerateWorkbookRequest(BaseModel):
    countries: list     # [{"name": "Germany", "iso3": "DEU"}, ...]
    username:  Optional[str] = "user"
    # When present, generate from this one grounded workbook. Absent = the
    # legacy "merge every numbered slot" behaviour, left working untouched.
    module:    Optional[str] = None
    workbook:  Optional[str] = None


@app.post("/workbook/extract-countries")
async def route_extract_countries(req: ExtractCountriesRequest):
    """Extract in-scope countries from a SOW or SP51 document using Claude AI."""
    from workbook_processor import extract_countries_from_document
    countries = await extract_countries_from_document(req.fileData, req.fileName)
    return {"success": True, "countries": countries, "count": len(countries)}


@app.get("/workbook/detect-countries")
async def route_detect_countries():
    """
    Scan all grounded reference workbooks and return the distinct countries
    actually present in their CSF sheets — used by the manual country-
    selection flow (no SOW required).
    """
    from workbook_processor import detect_countries_in_workbooks

    slots = await list_workbook_slots()
    if not slots:
        raise HTTPException(404, "No reference workbooks have been uploaded. Ask Admin to upload grounding workbooks.")

    slot_files = [
        {"slot": s["slot"], "ref_id": s["ref_id"], "file_name": s["file_name"], "file_ext": s["file_ext"]}
        for s in slots
    ]
    countries = detect_countries_in_workbooks(slot_files, REFS_DIR)
    return {"success": True, "countries": countries, "count": len(countries)}


# ═══════════════════════════════════════════════════════════════════════════════
# WORKBOOK LIBRARY — module -> workbook -> (countries)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Grounded workbooks live in `grounding_docs` under ids like
# `workbook:EC:employee-data`, NOT in a numbered slot table. That choice is what
# keeps this feature isolated: cascade resolves grounding by exact graph node id,
# and /admin/grounding enumerates knowledge-graph nodes, so neither can ever see
# a `workbook:` row. Verified: 0 collisions between the 54 workbook ids and the
# 31 graph node ids.

class WorkbookGroundRequest(BaseModel):
    module:    str
    workbook:  str
    content:   str                            # base64 file bytes
    name:      str
    ext:       str = "xlsx"
    countryFiltered: Optional[bool] = None    # admin override; None = trust detection
    username:  Optional[str] = "admin"


class ResolveCountryRequest(BaseModel):
    module:   str
    workbook: str
    query:    str


async def _workbook_files(module: str, workbook: str) -> List[Dict[str, Any]]:
    """Every grounded file for one (module, workbook), in upload order."""
    import workbook_taxonomy as T
    from db import list_grounding_docs
    out = []
    for row in await list_grounding_docs():
        info = T.parse_grounding_id(row.get("node_id", ""))
        if not info:
            continue
        if info["module"].upper() != str(module).upper():
            continue
        if info["workbook"].lower() != str(workbook).lower():
            continue
        hits = list(REFS_DIR.glob(str(row["ref_id"]) + "*"))
        out.append({**row, "index": info["index"],
                    "path": str(hits[0]) if hits else ""})
    return sorted(out, key=lambda r: r["index"])


def _override_flag(files: List[Dict[str, Any]]) -> Optional[bool]:
    """
    The admin's country-filter override, if one was recorded.

    Stored as a suffix on `uploaded_by` so the feature needs no schema change:
    'admin|cf=1'. None means no override, i.e. trust detection.
    """
    for f in files:
        tag = f.get("uploaded_by")
        if isinstance(tag, str) and "|cf=" in tag:
            return tag.split("|cf=")[-1].strip() == "1"
    return None


@app.get("/workbook/modules")
async def route_workbook_modules():
    """Dropdown 1 — every module, with how many of its workbooks are grounded."""
    import workbook_taxonomy as T
    from db import list_grounding_docs
    grounded = set()
    for row in await list_grounding_docs():
        info = T.parse_grounding_id(row.get("node_id", ""))
        if info:
            grounded.add((info["module"].upper(), info["workbook"].lower()))
    mods = []
    for m in T.all_modules():
        n = sum(1 for w in T.workbooks_for(m["id"])
                if (m["id"].upper(), w["id"].lower()) in grounded)
        mods.append({**m, "grounded_count": n})
    return {"success": True, "modules": mods}


@app.get("/workbook/modules/{module}/workbooks")
async def route_workbook_list(module: str):
    """Dropdown 2 — the workbooks of one module and whether each is grounded."""
    import workbook_taxonomy as T
    if not T.get_module(module):
        raise HTTPException(404, "Unknown module '" + str(module) + "'")
    out = []
    for w in T.workbooks_for(module):
        files = await _workbook_files(module, w["id"])
        out.append({**w, "grounded": bool(files), "file_count": len(files),
                    "files": [{"name": f["file_name"], "size_kb": f["size_kb"]}
                              for f in files]})
    return {"success": True, "module": module, "workbooks": out}


@app.get("/workbook/countries/{module}/{workbook}")
async def route_workbook_countries(module: str, workbook: str):
    """
    The country dictionary the user's typing is matched against — read from the
    grounded file itself, never from a static list. `filterable` decides whether
    the page shows a country box at all; the admin override beats detection.
    """
    import workbook_taxonomy as T
    from workbook_processor import build_country_index, clean_download_name
    files = await _workbook_files(module, workbook)
    if not files:
        raise HTTPException(404, "No workbook has been grounded for this selection.")
    idx = build_country_index([f["path"] for f in files if f["path"]])
    wb  = T.get_workbook(module, workbook) or {}
    override = _override_flag(files)
    filterable = idx["filterable"] if override is None else override
    return {"success": True, "module": module, "workbook": workbook,
            "label": wb.get("label", workbook),
            "filterable": filterable, "detected": idx["filterable"],
            "override": override,
            "csf_sheets": idx["csf_sheets"], "sheets": idx["sheets"],
            "countries": idx["countries"], "count": len(idx["countries"]),
            "files": [{"name": clean_download_name(f["file_name"]),
                       "raw_name": f["file_name"], "size_kb": f["size_kb"]}
                      for f in files]}


@app.post("/workbook/resolve-country")
async def route_resolve_country(req: ResolveCountryRequest):
    """
    Resolve one typed country against THIS workbook's countries.

    Returns a status the page acts on: 'exact' applies silently; 'suggest' and
    'ambiguous' ask the user; 'absent' and 'unknown' explain. A typo is never
    auto-applied — Iran/Iraq and Austria/Australia are one edit apart and both
    occur in the supplied reference data.
    """
    from workbook_processor import build_country_index, resolve_country
    files = await _workbook_files(req.module, req.workbook)
    if not files:
        raise HTTPException(404, "No workbook has been grounded for this selection.")
    idx = build_country_index([f["path"] for f in files if f["path"]])
    return {"success": True, **resolve_country(req.query, idx["countries"])}


@app.post("/admin/workbook/ground")
async def route_workbook_ground(req: WorkbookGroundRequest):
    """Ground a reference workbook under (module, workbook). Appends if one exists."""
    import workbook_taxonomy as T
    from db import save_grounding_doc
    from workbook_processor import build_country_index
    if not T.get_workbook(req.module, req.workbook):
        raise HTTPException(404, "Unknown workbook '" + str(req.module) + "/" + str(req.workbook) + "'")
    try:
        raw = base64.b64decode(req.content)
    except Exception:
        raise HTTPException(400, "Invalid base64 content")

    safe_ext = re.sub(r"[^a-zA-Z0-9]", "", req.ext) or "xlsx"
    ref_id   = str(uuid.uuid4())
    path     = REFS_DIR / (ref_id + "." + safe_ext)
    async with aiofiles.open(path, "wb") as f:
        await f.write(raw)
    size_kb = round(os.path.getsize(path) / 1024, 1)

    existing = await _workbook_files(req.module, req.workbook)
    index    = (max(f["index"] for f in existing) + 1) if existing else 1
    node_id  = T.grounding_id(req.module, req.workbook, index)

    tag = req.username or "admin"
    if req.countryFiltered is not None:
        tag = tag + "|cf=" + ("1" if req.countryFiltered else "0")

    await save_grounding_doc(node_id, ref_id, req.name, safe_ext, size_kb, tag)
    idx = build_country_index([str(path)])
    return {"success": True, "node_id": node_id, "index": index,
            "detected_filterable": idx["filterable"],
            "csf_sheets": idx["csf_sheets"], "countries": len(idx["countries"])}


@app.delete("/admin/workbook/ground/{module}/{workbook}")
async def route_workbook_unground(module: str, workbook: str):
    """Remove every grounded file for one (module, workbook)."""
    from db import delete_grounding_doc
    files = await _workbook_files(module, workbook)
    if not files:
        raise HTTPException(404, "Nothing grounded for this selection.")
    for f in files:
        try:
            if f.get("path") and os.path.exists(f["path"]):
                os.remove(f["path"])
        except Exception:
            pass
        await delete_grounding_doc(f["node_id"])
    return {"success": True, "removed": len(files)}


@app.get("/workbook/download/{module}/{workbook}")
async def route_workbook_download(module: str, workbook: str):
    """
    Plain download for a workbook that is NOT country-partitioned.

    Eight of the ten reference workbooks supplied have no country-scoped sheet
    at all — PMGM, RCM, Security Matrix, Position Management, Transactions — so
    there is nothing to filter and "generating" one would hand back the file it
    started from. Those are served straight, as a zip when the workbook has more
    than one grounded file.
    """
    import workbook_taxonomy as T
    from workbook_processor import clean_download_name
    files = await _workbook_files(module, workbook)
    files = [f for f in files if f.get("path") and os.path.exists(f["path"])]
    if not files:
        raise HTTPException(404, "No workbook has been grounded for this selection.")

    label = (T.get_workbook(module, workbook) or {}).get("label", workbook)
    safe  = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{module}_{label}").strip("_")

    # Delivered names are tidied the same way the generated ones are. The
    # accelerator prefix records where OUR template came from and means nothing
    # to whoever receives the file.
    if len(files) == 1:
        f = files[0]
        return FileResponse(f["path"], filename=clean_download_name(f["file_name"]),
                            media_type="application/octet-stream")

    import zipfile, io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f["path"], arcname=clean_download_name(f["file_name"]))
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'})


@app.get("/admin/workbook/suggest")
async def route_workbook_suggest(name: str):
    """Guess (module, workbook) from a file name so the admin starts pre-filled."""
    import workbook_taxonomy as T
    return {"success": True, "suggestion": T.suggest_workbook(name)}


@app.post("/workbook/generate/start")
async def route_generate_workbook_start(req: GenerateWorkbookRequest):
    """
    Start a background Configuration Workbook generation job — one output
    file per occupied grounding slot (1-4), never merged. Returns a job_id
    immediately; client connects to /workbook/generate/stream/{job_id} for
    live progress (SSE), then downloads results once complete.

    Generation is CPU-bound and can take several minutes per slot (each
    grounded workbook is loaded in fully writable mode to preserve exact
    fidelity — see workbook_processor.load_workbook_for_editing), which is
    why this doesn't block on a single request/response the way /generate
    routes elsewhere in this file do.
    """
    from workbook_processor import create_workbook_job, run_workbook_generation

    if not req.countries:
        raise HTTPException(400, "No countries provided")

    # New path: one specific (module, workbook) chosen on the page. The legacy
    # path — merge whatever sits in the numbered slots — is kept intact for any
    # caller that does not send a module, so nothing that works today changes.
    if getattr(req, "module", None) and getattr(req, "workbook", None):
        files = await _workbook_files(req.module, req.workbook)
        if not files:
            raise HTTPException(404, "No workbook has been grounded for this selection.")
        slot_files = [
            {"slot": f["index"], "ref_id": f["ref_id"],
             "file_name": f["file_name"], "file_ext": f["file_ext"]}
            for f in files
        ]
    else:
        slots = await list_workbook_slots()
        if not slots:
            raise HTTPException(404, "No reference workbooks have been uploaded. Ask Admin to upload grounding workbooks.")
        slot_files = [
            {"slot": s["slot"], "ref_id": s["ref_id"], "file_name": s["file_name"], "file_ext": s["file_ext"]}
            for s in slots
        ]

    job_id = str(uuid.uuid4())
    create_workbook_job(job_id)
    asyncio.create_task(run_workbook_generation(job_id, req.countries, slot_files, REFS_DIR))

    return {
        "success":   True,
        "jobId":     job_id,
        "totalSlots": len(slot_files),
        "streamUrl": f"/workbook/generate/stream/{job_id}",
    }


@app.get("/workbook/generate/stream/{job_id}")
async def route_generate_workbook_stream(job_id: str):
    """
    Server-Sent Events stream for a workbook generation job.
    Drains the asyncio.Queue set up by /workbook/generate/start.
    Each event: data: {json}\n\n — mirrors the /cascade/stream pattern.
    """
    from workbook_processor import get_workbook_queue, cleanup_workbook_queue

    async def event_generator():
        q = get_workbook_queue(job_id)
        if not q:
            yield f"data: {json.dumps({'event': 'error', 'data': {'message': 'Job not found'}})}\n\n"
            return

        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    yield f": ping\n\n"
                    continue

                if item is None:
                    yield f"data: {json.dumps({'event': 'stream_end', 'data': {}})}\n\n"
                    break

                event_type = item.get("event", "message")
                data       = item.get("data", {})
                yield f"data: {json.dumps({'event': event_type, 'data': data})}\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            cleanup_workbook_queue(job_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/workbook/generate/download/{job_id}/zip")
async def route_download_workbook_zip(job_id: str):
    """Download all generated files for a job as one ZIP archive."""
    import io
    import zipfile
    from fastapi.responses import Response
    from workbook_processor import get_workbook_result

    result = get_workbook_result(job_id)
    if not result or not result.get("slots"):
        raise HTTPException(404, "Job not found, still running, or results have expired")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for slot in result["slots"]:
            zf.writestr(slot["file_name"], slot["bytes"])

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="Config_Workbooks.zip"'},
    )


@app.get("/workbook/generate/download/{job_id}/{slot}")
async def route_download_workbook_slot(job_id: str, slot: int):
    """Download a single generated file for one grounding slot."""
    from fastapi.responses import Response
    from workbook_processor import get_workbook_result

    result = get_workbook_result(job_id)
    if not result or not result.get("slots"):
        raise HTTPException(404, "Job not found, still running, or results have expired")

    match = next((s for s in result["slots"] if s["slot"] == slot), None)
    if not match:
        raise HTTPException(404, f"Slot {slot} not found in this job's results")

    return Response(
        content=match["bytes"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{match["file_name"]}"'},
    )


# ═══════════════════════════════════════════════════════════════════════════
# STATIC FRONTEND  (installed builds)
# ═══════════════════════════════════════════════════════════════════════════
# Mounted LAST so every API route above is matched first — Starlette resolves
# routes in registration order, and a mount at "/" would otherwise shadow them.
#
# For an installed copy this replaces opening the HTML off disk. Serving the UI
# from the same origin as the API means the browser needs no CORS exemption and
# the page can discover the port it is already talking to, which matters now that
# the launcher picks a free port instead of assuming 8001.
#
# Silently skipped when frontend/ is absent (e.g. a backend-only checkout).
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="ui")
    print(f"   UI    : {_FRONTEND_DIR}")
