"""
cascade_agent.py — Cascade Generation Orchestrator (Agent 5)
             + Context Extraction Agent (Agent 2)
             + Document Generator Agent (Agent 3)

Uses LangGraph StateGraph for workflow orchestration, Anthropic SDK directly
for all Claude API calls. SSE events are emitted to the frontend in real-time.

Graph topology (new generation mode):
  START → extract_context → plan_waves → generate_wave ──┐
                                              ↑           │ (more waves)
                                              └───────────┘
                                              → END (all waves done)

Graph topology (delta mode):
  START → apply_delta_wave ──┐
              ↑              │ (more delta waves)
              └──────────────┘
              → END (all delta waves done)

Public entry points:
  run_new_cascade(...)         — start a full cascade generation
  run_delta_cascade(...)       — apply a delta to downstream docs
"""

import asyncio
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure stdout handles unicode on Windows (cp1252 can't encode emoji)
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from langgraph.graph import StateGraph, END, START
from langgraph.checkpoint.memory import MemorySaver

from llm_client import complete

try:
    from llm_client import usage_scope, usage_summary
except ImportError:                       # a transport without telemetry
    import contextlib

    @contextlib.contextmanager
    def usage_scope(_label):              # type: ignore[misc]
        yield

    def usage_summary(_prefix=""):        # type: ignore[misc]
        return {"calls": 0}

# The durable per-run trace. Stubbed the same way usage_scope is, so a missing or
# broken runlog degrades to no logging rather than taking generation down with it.
try:
    import runlog
except Exception:                         # pragma: no cover - defensive
    import contextlib as _cl
    import types as _ty

    def _noop(*_a, **_k):
        return None

    @_cl.contextmanager
    def _noop_scope(*_a, **_k):
        yield

    async def _anoop(*_a, **_k):
        return None

    runlog = _ty.SimpleNamespace(                       # type: ignore[assignment]
        start_run=_noop, finish_run=_noop, emit=_noop, record_error=_noop,
        node_scope=_noop_scope, stage_scope=_noop_scope, emit_env=_anoop,
    )
from context_store import (
    CascadeState,
    make_initial_state,
    emit_event,
    signal_sse_done,
)
from knowledge_graph import (
    compute_bfs_waves,
    get_default_format,
    get_model_for_node,
    get_node,
    get_parents,
    get_change_impact,
    get_all_node_ids,
)
from db import (
    save_document,
    get_document,
    save_cascade_document,
    save_cascade_session,
    update_cascade_session_status,
    list_cascade_documents,
    get_cascade_doc_by_node_version,
    get_cascade_doc_current_version,
    get_format_config,
    finalise_cascade_document,
)
from extractor import extract_text
from quality.pipeline import run_quality_pipeline

# ── Paths ──────────────────────────────────────────────────────────────────────
from paths import DATA_DIR as BASE_DIR, STORE_DIR, REFS_DIR

OPUS_MODEL   = "claude-opus-4-8"
SONNET_MODEL = "claude-sonnet-4-6"
# ── Context budgets (chars) ───────────────────────────────────────────────────
# These replace the previous 2,500-6,000 char ceilings, which were starving
# generation. Measured on a real run: a 41,852-char reference workbook reached the
# model as 4,000 chars (9.6% — it never saw sheets 2-10), and the uploaded SP51
# reached it as 2,500 chars (~3%). The model was not producing thin documents
# because it reasoned poorly; it was producing them because it could not see the
# source material.
#
# extract_text() already caps extraction at 80,000 chars, so that is the real
# upper bound here, not these numbers. Claude Opus has a 1M-token context window,
# so these are comfortable; the practical cost is latency, not capacity.
MAX_SOURCE_CHARS      = 80000   # the uploaded root document (SP51 / SOW)
MAX_PARENT_CHARS      = 8000    # a sibling doc generated earlier (its stored
                                # content_summary is itself capped at 8000)
MAX_GROUNDING_CHARS   = 80000   # admin-uploaded reference template, as extracted
MAX_GROUNDING_PROMPT  = 60000   # how much of it reaches the generation prompt
MAX_CTX_CHARS         = 20000   # the extracted project-context JSON

# NOTE: MAX_TOKS_CTX / MAX_TOKS_GEN used to live here. They were dead constants —
# llm_client.complete() drives the `claude` CLI via the Agent SDK, and
# ClaudeAgentOptions exposes no token ceiling (only max_turns / max_budget_usd).

# LangGraph checkpointer (in-memory; sessions live for the server lifetime)
_checkpointer = MemorySaver()

# ── Wave concurrency ──────────────────────────────────────────────────────────
# A wave generates up to 4 documents at once, and each document is no longer a
# single CLI process: it opens a generation session, forks one per reviewer, and may
# run an authoring agent. Peak is therefore roughly 4 x (1 + reviewers + 1) — around
# 16 concurrent `claude` processes.
#
# That is survivable on one laptop, but end users share an Enterprise org. Several
# colleagues each running a cascade multiplies this against org-level limits that a
# single Pro account never encounters. Capping documents (not processes) keeps the
# knob understandable; 4 preserves today's behaviour exactly, so this is a ceiling
# to lower on a shared org rather than a change in default behaviour.
MAX_PARALLEL_DOCS = int(os.environ.get("PROJECTZEN_MAX_PARALLEL_DOCS") or 4)

_DOC_SEM: Optional[asyncio.Semaphore] = None


def _doc_semaphore() -> asyncio.Semaphore:
    """Created lazily: a Semaphore binds to the running loop, and this module is
    imported long before uvicorn's loop exists."""
    global _DOC_SEM
    if _DOC_SEM is None:
        _DOC_SEM = asyncio.Semaphore(max(1, MAX_PARALLEL_DOCS))
    return _DOC_SEM


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_str(v: Any, maxlen: int = 120) -> str:
    if v is None:
        return ""
    s = str(v)
    s = "".join(c if 32 <= ord(c) < 127 else " " for c in s)
    return s[:maxlen].strip()


def _clean_json(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```\s*$", "", s)
    start = s.find("{")
    end   = s.rfind("}")
    if start != -1 and end > start:
        s = s[start:end + 1]
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def _sanitize_plan(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_sanitize_plan(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_plan(v) for k, v in obj.items()}
    if isinstance(obj, str):
        return _safe_str(obj)
    if isinstance(obj, float):
        return obj if obj == obj else 0
    return obj


def _make_file_name(node_id: str, label: str, fmt: str, version: int) -> str:
    safe = re.sub(r"\s+", "_", label)
    safe = re.sub(r"[()/:*?\"<>|+]", "", safe)
    safe = re.sub(r"_+", "_", safe).strip("_")[:50]
    ts   = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{safe}_v{version}_{ts}.{fmt.lower()}"


async def _resolve_grounding(node_id: str) -> Tuple[str, str]:
    """
    DB-first: look up admin-uploaded grounding doc from grounding_docs table.
    Falls back to legacy file naming convention for backwards compatibility.
    Returns (extracted_text[:6000], absolute_file_path).
    file_path is "" when no grounding doc is found.
    """
    # DB-first: admin-uploaded via Document Grounding manager
    try:
        from db import get_grounding_doc
        record = await get_grounding_doc(node_id)
        if record:
            matches = list(REFS_DIR.glob(f"{record['ref_id']}*"))
            if matches:
                fpath = str(matches[0])
                try:
                    return extract_text(fpath)[:MAX_GROUNDING_CHARS], fpath
                except Exception:
                    return "", fpath
    except Exception as e:
        print(f"   ⚠ DB grounding lookup failed for {node_id}: {e}")

    # Fallback: legacy file naming convention in REFS_DIR
    for pattern in [f"{node_id}-grounding.*", f"{node_id}_grounding.*"]:
        matches = list(REFS_DIR.glob(pattern))
        if matches:
            fpath = str(matches[0])
            try:
                return extract_text(fpath)[:MAX_GROUNDING_CHARS], fpath
            except Exception:
                return "", fpath
    return "", ""


async def _get_parent_contents(
    node_id: str,
    generated_docs: Dict[str, Any],
) -> Dict[str, str]:
    """
    Fetch content of parent documents available to this node.
    Covers two sources:
      1. The root/input document — stored as a synthetic entry (no file_id, _is_input=True).
      2. Docs generated earlier in this cascade session.
    Returns {parent_node_id: content_text}.
    """
    parents = get_parents(node_id)
    result:  Dict[str, str] = {}
    for parent_id in parents:
        if parent_id not in generated_docs:
            continue
        entry = generated_docs[parent_id]
        if entry.get("_is_input"):
            # The user-uploaded root document (SP51/SOW). This is the single richest
            # input any document has, so it gets a much larger budget than a sibling
            # summary — it was previously capped at 3,000 chars, which is why
            # generated documents read as generic.
            result[parent_id] = entry.get("content_summary", "")[:MAX_SOURCE_CHARS]
        else:
            file_id = entry.get("file_id", "")
            if file_id:
                doc = await get_document(file_id)
                if doc:
                    # A doc generated earlier in this cascade; its stored
                    # content_summary is already capped at 8,000 by save_document.
                    result[parent_id] = (doc.get("content_summary") or "")[:MAX_PARENT_CHARS]
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 2 — CONTEXT EXTRACTION  (structured Claude Opus call)
# ═══════════════════════════════════════════════════════════════════════════════

_CTX_SCHEMA = """{
  "project_name": "string",
  "client_name": "string",
  "delivery_partner": "string",
  "project_type": "string (e.g. Salesforce Sales Cloud Implementation)",
  "scope_summary": "string (2-3 sentences)",
  "in_scope_modules": ["string"],
  "out_of_scope": ["string"],
  "key_stakeholders": [{"name": "string", "role": "string", "org": "string"}],
  "timeline": {"start": "string", "end": "string", "go_live": "string", "key_milestones": "string"},
  "integration_points": ["string"],
  "data_migration_scope": "string",
  "key_requirements": ["string (max 20 items)"],
  "geographic_scope": "string",
  "budget_reference": "string",
  "delivery_methodology": "string",
  "additional_context": "string"
}"""

_CTX_SYSTEM = (
    "You are a Salesforce project delivery expert extracting structured project facts "
    "from a source document for use in automated document generation. "
    "Extract ALL available facts. Be specific and complete. "
    "Return ONLY valid JSON matching the schema exactly. No markdown."
)


async def extract_project_context(input_text: str, session_id: str) -> Dict[str, Any]:
    """Agent 2: Extract structured project context from the input document."""
    prompt = (
        f"Extract all project facts from the document below into the JSON schema.\n\n"
        f"SCHEMA:\n{_CTX_SCHEMA}\n\n"
        f"SOURCE DOCUMENT:\n{input_text[:40000]}\n\n"
        f"Return ONLY valid JSON. No markdown. No explanation."
    )

    await emit_event(session_id, "status", {"message": "Extracting project context..."})

    for attempt in range(1, 4):
        try:
            raw = await complete(prompt, system=_CTX_SYSTEM, model=OPUS_MODEL)
            ctx = json.loads(_clean_json(raw))
            print(f"   ✅ Context extracted: {ctx.get('project_name', '?')} / {ctx.get('client_name', '?')}")
            return ctx
        except Exception as e:
            print(f"   ⚠ Context extraction attempt {attempt}: {e}")

    # Fallback: minimal context from raw text
    return {
        "project_name":         "Project (extracted from input)",
        "client_name":          "Client",
        "delivery_partner":     "Accenture",
        "project_type":         "Salesforce Implementation",
        "scope_summary":        input_text[:400],
        "in_scope_modules":     [],
        "out_of_scope":         [],
        "key_stakeholders":     [],
        "timeline":             {},
        "integration_points":   [],
        "data_migration_scope": "",
        "key_requirements":     [],
        "geographic_scope":     "",
        "budget_reference":     "",
        "delivery_methodology": "Agile",
        "additional_context":   "",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT 3 — DOCUMENT GENERATOR  (per-document Claude call with streaming)
# ═══════════════════════════════════════════════════════════════════════════════

_FORMAT_SCHEMAS = {
    "xlsx": '{"title":"string","sheets":[{"name":"string","headers":["string"],"rows":[["string"]],"has_totals":false}],"chart_sheet":0,"chart_data_col":1}',
    "docx": '{"title":"string","subtitle":"string","sections":[{"heading":"string","level":1,"paragraphs":["string"],"bullets":["string"],"table":{"headers":["string"],"rows":[["string"]]}}]}',
    "pdf":  '{"title":"string","subtitle":"string","sections":[{"heading":"string","paragraphs":["string"],"table":{"headers":["string"],"rows":[["string"]]}}]}',
    "pptx": '{"title":"string","subtitle":"string","slides":[{"type":"title","title":"string","subtitle":"string"},{"type":"bullets","title":"string","bullets":["string"]},{"type":"table","title":"string","headers":["string"],"rows":[["string"]]}]}',
    "xml":  '{"title":"string","subtitle":"string","sheets":[{"name":"string","headers":["string"],"rows":[["string"]],"has_totals":false}]}',
}

# Volume targets. Raised from the originals (xlsx was "5-8 sheets, 10-25 rows"),
# which sat far below real delivery artefacts — a genuine reference Project Plan in
# this estate is 10 sheets / 417 rows. These stay deliberately in step with
# MIN_VOLUME in quality/reviewers/structure.py: the prompt must ask for at least
# what the validator enforces, or every document fails a check it was never told
# to satisfy. If you raise one, raise the other.
#
# "If the REFERENCE TEMPLATE is larger than these numbers, follow the reference"
# matters more than the numbers themselves — the ranges are a floor for documents
# that have no grounding template, not a ceiling for those that do.
_FORMAT_DETAIL = {
    "xlsx": "Generate 6-10 sheets. Each sheet: 15-40 data rows. Extract real project data for every cell. If a REFERENCE TEMPLATE is supplied, match its sheet set and row depth instead of these minimums.",
    "docx": "Generate 8-14 sections. Each section: 3-6 paragraphs. Tables: 10-25 rows. Use specific project facts. If a REFERENCE TEMPLATE is supplied, match its section set and depth instead of these minimums.",
    "pdf":  "Generate 8-14 sections. Each: 3-5 paragraphs. Include tables where appropriate. If a REFERENCE TEMPLATE is supplied, match its structure and depth.",
    "pptx": "Generate 10-18 slides. Mix title, bullets, and table slide types. If a REFERENCE TEMPLATE is supplied, match its deck structure.",
    "xml":  "Generate 4-6 entities. Each: 10-20 records. Use reference schema field names.",
}


def _build_gen_prompt(
    node_id:        str,
    node:           Dict,
    project_ctx:    Dict,
    parent_contents:Dict[str, str],
    output_format:  str,
    grounding_text: str,
    delta_items:    Optional[List[Dict]] = None,
    existing_content: Optional[str] = None,
    version:        int = 1,
    extra_blocks:   str = "",
    detail_override: str = "",
) -> str:
    """
    Build the generation prompt for Agent 3.

    extra_blocks: optional research/plan/glossary material from the quality pipeline.
    It is interpolated immediately BEFORE the HARD RULES block so the last thing the
    model reads is still "Return ONLY the JSON object" — appending it after that
    measurably degrades JSON adherence on a path that already needs a 3-attempt retry.
    Defaults to "", so every existing caller is unaffected.
    """

    proj_json = json.dumps(project_ctx, indent=2)[:MAX_CTX_CHARS]
    impact    = get_change_impact(node_id)

    parent_block = ""
    if parent_contents:
        parent_block = "\n\nPARENT DOCUMENTS (inputs to this document — use their data):\n"
        for pid, content in list(parent_contents.items())[:6]:
            pnode  = get_node(pid)
            plabel = pnode["label"] if pnode else pid
            parent_block += f"\n--- {plabel} ---\n{content[:MAX_SOURCE_CHARS]}\n"

    grounding_block = ""
    if grounding_text:
        grounding_block = (
            "\n\nREFERENCE TEMPLATE — this is a document from a PREVIOUS engagement. "
            "It shows you HOW this deliverable is built, not WHAT belongs in it.\n\n"
            "COPY FROM THE REFERENCE (the form):\n"
            "- Sheet/section layout and the shape of the hierarchy\n"
            "- Column names, heading style, naming and ID conventions\n"
            "- Tone, level of detail, and how densely each area is populated\n\n"
            "DERIVE FROM THE INPUT DOCUMENT (the substance):\n"
            "- Which modules, workstreams, phases, countries and deliverables actually "
            "exist. The reference describes a DIFFERENT project — its scope is not "
            "this project's scope.\n"
            "- Add a sheet/section for every in-scope item named in the input, EVEN IF "
            "the reference has no equivalent one. Enumerate the in-scope modules, "
            "workstreams and countries from PROJECT CONTEXT above BEFORE you start, "
            "and give each one its own sheet/section. Then count them again before "
            "you return. Dropping one because the reference happens to lack it is the "
            "single most common way this task fails, and the result is a plan for "
            "contracted work that will never be scheduled or costed.\n"
            "- Omit anything the reference covers that this project does not include.\n"
            "- Use this project's real names, dates, figures and owners — never the "
            "reference's.\n\n"
            "Mirroring the reference's scope instead of the input's is a CRITICAL "
            "failure: it produces a plan for the wrong project that merely looks "
            "correct.\n\n"
            "REFERENCE TEMPLATE CONTENT:\n"
            f"{grounding_text[:MAX_GROUNDING_PROMPT]}\n"
        )

    delta_block = ""
    if delta_items and existing_content:
        delta_block = (
            f"\n\nDELTA UPDATE — This is VERSION {version} (update, not new generation).\n"
            f"RULE: Preserve ALL existing content unchanged EXCEPT the sections/fields listed below.\n"
            f"Only regenerate the affected parts. All other content must be identical to v{version-1}.\n\n"
            f"EXISTING CONTENT (v{version-1}):\n{existing_content[:4000]}\n\n"
            f"DELTA CHANGES TO APPLY:\n{json.dumps(delta_items, indent=2)}\n"
        )

    version_history_note = ""
    if version > 1:
        version_history_note = (
            f"\n\nVERSION HISTORY SECTION:\n"
            f"At the end of the document, include a 'Version History' section/sheet with:\n"
            f"v{version} | {datetime.utcnow().strftime('%Y-%m-%d')} | AI / {project_ctx.get('delivery_partner','Accenture')} | "
            f"{'Delta update applied' if delta_items else 'Update'}\n"
        )
    else:
        version_history_note = (
            f"\n\nVERSION HISTORY SECTION:\n"
            f"At the end of the document, include a 'Version History' section/sheet with:\n"
            f"v1 | {datetime.utcnow().strftime('%Y-%m-%d')} | AI / {project_ctx.get('delivery_partner','Accenture')} | Initial generation\n"
        )

    schema  = _FORMAT_SCHEMAS.get(output_format, _FORMAT_SCHEMAS["docx"])
    detail  = detail_override or _FORMAT_DETAIL.get(output_format, _FORMAT_DETAIL["docx"])

    return (
        f"You are a senior Salesforce project consultant generating a professional "
        f"'{node['label']}' document for a client-facing implementation project.\n\n"
        f"DOCUMENT METADATA:\n"
        f"  Type:    {node['type']}\n"
        f"  Phase:   {node['phase']}\n"
        f"  Owner:   {node['owner']}\n"
        f"  Tier:    {node['tier']} (T1=critical, T4=low)\n"
        f"  Trigger: {node['trigger']}\n"
        f"  Notes:   {node['notes']}\n"
        f"  Version: v{version}\n\n"
        f"  Change impact: {impact.get('impactType','')}\n"
        f"  Governance:    {impact.get('governance','')}\n\n"
        f"PROJECT CONTEXT:\n{proj_json}\n"
        f"{parent_block}"
        f"{grounding_block}"
        f"{delta_block}"
        f"{version_history_note}\n"
        f"OUTPUT FORMAT: {output_format.upper()}\n\n"
        f"QUALITY REQUIREMENTS:\n"
        f"- Enterprise-grade, client-ready. This document goes directly to the client.\n"
        f"- Use REAL project facts from the context above. No generic placeholders.\n"
        f"- Every section, row, and field must be traceable to project requirements.\n"
        f"- Professional Accenture delivery standard English.\n"
        f"- Complete and comprehensive — a junior consultant must be able to use this immediately.\n\n"
        f"JSON SCHEMA (follow exactly):\n{schema}\n\n"
        f"DETAIL REQUIREMENTS: {detail}\n\n"
        f"{extra_blocks}"
        f"HARD RULES:\n"
        f"1. Return ONLY valid JSON. No markdown. No explanation. No backticks.\n"
        f"2. Every string: max 120 chars, ASCII only.\n"
        f"3. No trailing commas.\n"
        f"4. Numbers in values arrays must be JSON numbers, not strings.\n"
        f"5. Return ONLY the JSON object, nothing else."
    )


async def generate_single_document(
    session_id:   str,
    node_id:      str,
    project_ctx:  Dict,
    generated_docs: Dict[str, Any],
    version:      int = 1,
    delta_items:  Optional[List[Dict]] = None,
    existing_content: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Agent 3: Generate one document. Opens this document's slice of the run trace,
    then delegates unchanged.

    A WRAPPER, not an edit to the body. The scope has to be entered inside the
    per-document coroutine — ContextVars copy per asyncio task, so opening it around
    the gather in node_generate_wave would attribute all four concurrent documents in
    a wave to whichever one set it last. Wrapping keeps that correct without
    re-indenting 150 lines of working code, where a whitespace-only diff would have
    hidden the real change.
    """
    with runlog.node_scope(node_id, version=version, is_delta=bool(delta_items)):
        return await _generate_single_document(
            session_id, node_id, project_ctx, generated_docs,
            version, delta_items, existing_content,
        )


async def _generate_single_document(
    session_id:   str,
    node_id:      str,
    project_ctx:  Dict,
    generated_docs: Dict[str, Any],
    version:      int = 1,
    delta_items:  Optional[List[Dict]] = None,
    existing_content: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Streams content to the SSE queue for the typewriter effect in the frontend.
    Returns a GeneratedDoc dict on success, None on failure.
    """
    node = get_node(node_id)
    if not node:
        return None

    # Resolve output format (admin config > knowledge graph default)
    output_format = await get_format_config(node_id) or get_default_format(node_id)

    # Select model by tier
    model = get_model_for_node(node_id)

    # Get grounding file (text for Claude prompt + path for template cloning)
    grounding_text, grounding_path = await _resolve_grounding(node_id)

    # Get parent document contents
    parent_contents = await _get_parent_contents(node_id, generated_docs)

    schema = _FORMAT_SCHEMAS.get(output_format, _FORMAT_SCHEMAS["docx"])
    detail = _FORMAT_DETAIL.get(output_format, _FORMAT_DETAIL["docx"])

    # Replace the generic "15-40 rows per sheet" range with the reference's ACTUAL
    # per-sheet depth where we can measure it. The flat range is what produced
    # twelve module sheets of identical 24 rows against a reference that varies
    # 22-28 and puts 288 rows in its primary sheet. Falls back to the range when
    # there is no measurable reference.
    try:
        from authoring import reference_depth_hint
        _hint = reference_depth_hint(grounding_path)
        if _hint:
            detail = detail + "\n\n" + _hint
    except Exception:
        pass

    async def _on_chunk(delta: str) -> None:
        await emit_event(session_id, "content_chunk", {
            "node_id": node_id,
            "chunk":   delta,
        })

    def _build_prompt(extra_blocks: str = "") -> str:
        return _build_gen_prompt(
            node_id=node_id,
            node=node,
            project_ctx=project_ctx,
            parent_contents=parent_contents,
            output_format=output_format,
            grounding_text=grounding_text,
            delta_items=delta_items,
            existing_content=existing_content,
            version=version,
            extra_blocks=extra_blocks,
            detail_override=detail,
        )

    # The existing generation call and 3-attempt retry, unchanged — just made callable
    # so the quality pipeline can drive it. content_chunk still streams from here.
    async def _stream_generate(prompt: str, conv=None) -> str:
        # `conv` is the quality pipeline's shared Conversation. When present, generation
        # runs as a turn in the same session as research and plan — the subprocess spawn
        # is already paid, and the model still has the research and plan turns in
        # history rather than a truncated text copy of them. When absent (delta path,
        # or if the session failed to start) this falls back to an independent call,
        # which is exactly today's behaviour.
        for attempt in range(1, 4):
            print(f"   📄 {node['label']} v{version} | model={model} | attempt={attempt}")
            try:
                if conv is not None:
                    raw = await conv.turn(prompt, model=model, on_chunk=_on_chunk)
                else:
                    raw = await complete(prompt, model=model, on_chunk=_on_chunk)
                return json.dumps(_sanitize_plan(json.loads(_clean_json(raw))))
            except Exception as e:
                print(f"   ⚠ Gen attempt {attempt} failed: {str(e)[:120]}")
                if attempt == 3:
                    raise
        raise RuntimeError("generation exhausted retries")

    # Draft production is delegated to the quality pipeline (research -> plan ->
    # generate -> review -> bounded rework). Depth is tier-scoped; every stage fails
    # open, so a failure anywhere inside degrades to today's single-pass behaviour.
    # Every LLM call made for this document is tagged with this label, so what the
    # document cost can be read off the run instead of inferred afterwards. The tag
    # is a ContextVar, which is what makes it correct when a wave generates four
    # documents concurrently on the same event loop.
    usage_tag = f"{node_id}/v{version}"

    # Everything resolved above is what a support investigation wants to know before
    # it reads a single token count: which model, which format, and whether grounding
    # actually attached. "Grounding silently did not resolve" is a real failure mode
    # and is invisible in the output.
    runlog.emit(
        "node_meta",
        label=node["label"], tier=node.get("tier"), model=model,
        output_format=output_format, version=version,
        grounding_resolved=bool(grounding_text),
        grounding_chars=len(grounding_text or ""),
        grounding_ref=os.path.basename(grounding_path) if grounding_path else "",
        parents=sorted(parent_contents.keys())
                if isinstance(parent_contents, dict) else None,
    )

    try:
        with usage_scope(usage_tag):
            draft_json = await run_quality_pipeline(
                session_id=session_id,
                node=node,
                node_id=node_id,
                version=version,
                project_ctx=project_ctx,
                parent_contents=parent_contents,
                grounding_text=grounding_text,
                schema=schema,
                output_format=output_format,
                detail=detail,
                build_prompt_fn=_build_prompt,
                stream_generate_fn=_stream_generate,
                sanitize_fn=_sanitize_plan,
                emit_event_fn=emit_event,
                on_chunk=_on_chunk,
                is_delta=bool(delta_items),
                delta_items=delta_items,
                existing_content=existing_content or "",
                checkpoint_fns=None,
            )
        plan = json.loads(draft_json)
    except Exception as e:
        # A REFUSAL (usage limit, expired sign-in, billing) is not a content failure
        # and must be reported as itself. Previously it arrived here as an opaque JSON
        # parse error, because the limit notice had been passed down the pipeline as
        # though it were the research pack.
        reason = getattr(e, "reason", "") or ""
        detail = str(e)[:200]
        if reason:
            print(f"   [llm] {reason}: {detail}")
        else:
            print(f"   Generation failed: {detail[:120]}")
        # The full exception — type, message, traceback tail, and the LlmUnavailable
        # reason/detail/is_retryable. The SSE event below is truncated for the UI; this
        # is the copy the investigation actually reads.
        runlog.record_error(e, where=f"pipeline/{node_id}")
        await emit_event(session_id, "node_error", {
            "node_id": node_id,
            "error":   detail,
            "reason":  reason,          # "" for ordinary failures
        })
        if reason:
            # Stop the whole run: every later document would hit the same wall.
            raise
        return None

    # Generate file from plan
    from templates import generate as templates_generate
    file_name = _make_file_name(node_id, node["label"], output_format, version)
    out_path  = str(STORE_DIR / file_name)

    # ── Authoring attempt (Section C) ─────────────────────────────────────────
    # The JSON plan above has been researched, planned, reviewed and reworked — the
    # content is as good as this pipeline can make it. What it cannot express is
    # FORM: formulas, column widths, number formats, conditional fills. So before
    # falling back to the JSON renderer, give Claude the reviewed plan plus the
    # reference FILE and let it build the artefact directly.
    #
    # This never lowers the floor. The authored file must contain at least the
    # sheets and rows the reviewed plan specifies or it is rejected, and any
    # rejection, error or timeout drops straight through to templates_generate()
    # below — the exact path taken today.
    authored = False
    try:
        import authoring
        if authoring.is_enabled(output_format):
            await emit_event(session_id, "qp_authoring_start", {
                "node_id": node_id, "format": output_format,
            })
            authored, reason, stats = await authoring.author_document(
                plan=plan,
                output_format=output_format,
                out_path=out_path,
                node_label=node["label"],
                grounding_path=grounding_path,
                model=model,
                run_id=f"{session_id}_{node_id}_v{version}",
                emit=emit_event,
                session_id=session_id,
                node_id=node_id,
            )
            if authored:
                print(f"   [authoring] {node['label']}: {stats.get('containers')} sheets, "
                      f"{stats.get('rows')} rows, {stats.get('formulas')} formulas, "
                      f"{stats.get('colours')} colours in {stats.get('seconds')}s")
            else:
                print(f"   [authoring] declined ({reason}); using template renderer")
            await emit_event(session_id, "qp_authoring_done", {
                "node_id": node_id, "accepted": authored,
                "reason": reason, "stats": stats,
            })
            # Whether Claude built the file directly or we fell back to the JSON
            # renderer is the single biggest determinant of output fidelity, and
            # today it is only visible in a console line nobody keeps.
            runlog.emit("authoring", accepted=authored,
                        reason=str(reason)[:200], stats=stats)
    except Exception as author_err:
        # Authoring must never be able to fail a generation that would have worked.
        print(f"   [authoring] unavailable: {str(author_err)[:120]}")
        runlog.record_error(author_err, where=f"authoring/{node_id}")
        authored = False

    if not authored:
        try:
            templates_generate(output_format, plan, out_path, "", grounding_path=grounding_path)
        except Exception as gen_err:
            print(f"   ⚠ Template generation failed: {gen_err}")
            await emit_event(session_id, "node_error", {
                "node_id": node_id,
                "error":   str(gen_err)[:200],
            })
            return None

    size_kb = round(os.path.getsize(out_path) / 1024, 1)

    # What this document actually cost. Reported per stage so an expensive stage is
    # identifiable rather than a single total that has to be bisected by hand.
    try:
        u = usage_summary(usage_tag)
        if u.get("calls"):
            print(f"   [usage] {node['label']} v{version}: {u['calls']} calls | "
                  f"in={u['billable_input']:,} out={u['output']:,} | "
                  f"cache hit {u['cache_hit_pct']}% | ${u['cost_usd']} | {u['seconds']}s")
            for stage, s in sorted(u.get("by_stage", {}).items(),
                                   key=lambda kv: -kv[1]["billable_input"])[:6]:
                print(f"            {stage:36} {s['calls']:>2} calls  "
                      f"in={s['billable_input']:>8,}  out={s['output']:>7,}  ${s['cost_usd']}")
            await emit_event(session_id, "qp_usage", {"node_id": node_id, "usage": u})
            runlog.emit("usage", usage=u)
    except Exception:
        pass

    runlog.emit("artifact", file_name=file_name, path=out_path,
                size_kb=size_kb, output_format=output_format, authored=authored)

    # Extract content summary for DB storage and downstream grounding
    try:
        content_summary = extract_text(out_path, output_format)[:8000]
    except Exception:
        content_summary = f"{node['label']} v{version} — {project_ctx.get('project_name','')}"

    # Save to documents table (existing)
    doc_id = str(uuid.uuid4())
    await save_document(
        id=doc_id,
        file_name=file_name,
        template=node["label"],
        output_format=output_format,
        file_path=out_path,
        size_kb=size_kb,
        username="cascade",
        content_summary=content_summary,
    )

    # Save to cascade_documents table (new)
    cascade_doc_id = str(uuid.uuid4())
    delta_notes    = json.dumps(delta_items)[:2000] if delta_items else ""
    await save_cascade_document(
        id=cascade_doc_id,
        session_id=session_id,
        node_id=node_id,
        file_id=doc_id,
        file_name=file_name,
        output_format=output_format,
        version=version,
        model_used=model,
        delta_notes=delta_notes,
    )

    return {
        "node_id":        node_id,
        "file_id":        doc_id,
        "file_name":      file_name,
        "output_format":  output_format,
        "content_summary":content_summary,
        "version":        version,
        "model_used":     model,
        "is_finalised":   False,
        "finalised_at":   None,
        "finalised_by":   None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LANGGRAPH NODES
# ═══════════════════════════════════════════════════════════════════════════════

async def node_extract_context(state: CascadeState) -> Dict:
    """LangGraph node: Agent 2 — extract project context from input document."""
    session_id = state["session_id"]
    await emit_event(session_id, "node_generating", {"node_id": "__context__"})

    ctx = await extract_project_context(state["input_text"], session_id)

    await emit_event(session_id, "context_ready", {
        "project_name": ctx.get("project_name", ""),
        "client_name":  ctx.get("client_name", ""),
        "scope":        ctx.get("scope_summary", "")[:200],
    })

    # ── Cascade-wide terminology contract, built ONCE and used by every document ──
    # The documents in a cascade are generated independently and in parallel, so
    # without a shared vocabulary they drift apart — one calls it the "Integration
    # Layer", another "middleware tier" — and each passes its own consistency check.
    # One extra call per cascade (not per document) fixes that for all of them.
    # Wrapped in try/except: if it fails, generation proceeds exactly as before.
    try:
        from quality.glossary import build_cascade_glossary
        await build_cascade_glossary(
            session_id=session_id,
            project_ctx=ctx,
            source_text=state.get("input_text", "") or "",
            emit_event_fn=emit_event,
        )
    except Exception as e:
        print(f"   ⚠ Cascade glossary skipped: {str(e)[:120]}")

    return {"project_context": ctx}


async def node_plan_waves(state: CascadeState) -> Dict:
    """LangGraph node: compute BFS wave order from selected nodes."""
    session_id     = state["session_id"]
    selected_nodes = state["selected_nodes"]

    waves = compute_bfs_waves(selected_nodes)
    total = sum(len(w) for w in waves)

    await emit_event(session_id, "waves_planned", {
        "total_waves": len(waves),
        "total_docs":  total,
        "waves":       [[{"node_id": n, "label": (get_node(n) or {}).get("label", n)} for n in w] for w in waves],
    })
    print(f"   🗂 Waves planned: {len(waves)} waves, {total} docs")

    return {
        "wave_order":       waves,
        "current_wave_idx": 0,
        "total_docs":       total,
    }


async def node_generate_wave(state: CascadeState) -> Dict:
    """
    LangGraph node: Agent 3 — generate all documents in the current wave in parallel.
    Emits SSE events for each document: node_queued, node_generating, node_complete.
    """
    session_id   = state["session_id"]
    wave_idx     = state["current_wave_idx"]
    wave_order   = state["wave_order"]
    project_ctx  = state["project_context"]
    generated    = dict(state["generated_docs"])
    completed    = state["completed_docs"]

    if wave_idx >= len(wave_order):
        return {"is_complete": True}

    current_wave = wave_order[wave_idx]
    wave_num     = wave_idx + 1
    total_waves  = len(wave_order)

    await emit_event(session_id, "wave_start", {
        "wave":       wave_num,
        "total_waves":total_waves,
        "node_ids":   current_wave,
        "labels":     [(get_node(n) or {}).get("label", n) for n in current_wave],
    })

    # Queue all nodes in this wave
    for nid in current_wave:
        await emit_event(session_id, "node_queued", {"node_id": nid, "wave": wave_num})

    # Generate all nodes in this wave in parallel, bounded by MAX_PARALLEL_DOCS.
    async def _gen_one(nid: str):
        async with _doc_semaphore():
            await emit_event(session_id, "node_generating", {"node_id": nid})
            result = await generate_single_document(
                session_id=session_id,
                node_id=nid,
                project_ctx=project_ctx,
                generated_docs=generated,
                version=1,
            )
            return nid, result

    tasks   = [asyncio.create_task(_gen_one(nid)) for nid in current_wave]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for item in results:
        if isinstance(item, Exception):
            # A refusal (usage limit, sign-in, billing) applies to every remaining
            # document too, so it propagates and the run is marked failed with the
            # real reason. return_exceptions=True would otherwise bury it here and
            # the cascade would report success having produced nothing.
            if getattr(item, "reason", ""):
                raise item
            print(f"   Wave {wave_num} task error: {item}")
            continue
        nid, doc = item
        if doc:
            generated[nid]  = doc
            completed      += 1
            await emit_event(session_id, "node_complete", {
                "node_id":       nid,
                "file_id":       doc["file_id"],
                "file_name":     doc["file_name"],
                "output_format": doc["output_format"],
                "version":       doc["version"],
                "size_kb":       0,
                "wave":          wave_num,
            })
            await update_cascade_session_status(
                session_id, "running", completed_docs=completed
            )

    await emit_event(session_id, "wave_complete", {
        "wave":        wave_num,
        "total_waves": total_waves,
        "completed":   completed,
        "total":       state["total_docs"],
    })

    next_wave_idx = wave_idx + 1
    is_complete   = next_wave_idx >= len(wave_order)

    return {
        "generated_docs":   generated,
        "completed_docs":   completed,
        "current_wave_idx": next_wave_idx,
        "is_complete":      is_complete,
    }


async def node_apply_delta_wave(state: CascadeState) -> Dict:
    """
    LangGraph node: Apply delta changes to one wave of affected downstream documents.
    Only called in delta mode, after user has confirmed scope.
    """
    session_id    = state["session_id"]
    delta_waves   = state.get("delta_waves") or []
    wave_idx      = state.get("current_delta_wave", 0)
    project_ctx   = state["project_context"]
    generated     = dict(state["generated_docs"])
    completed     = state["completed_docs"]
    delta_items   = state.get("delta_items") or []
    user_nodes    = set(state.get("user_delta_nodes") or [])
    fin_choices   = state.get("user_finalised_choices") or {}

    if wave_idx >= len(delta_waves):
        return {"is_complete": True}

    current_wave = [n for n in delta_waves[wave_idx] if n in user_nodes]
    wave_num     = wave_idx + 1

    await emit_event(session_id, "delta_wave_start", {
        "wave":     wave_num,
        "node_ids": current_wave,
    })

    async def _apply_one(nid: str):
        node = get_node(nid)
        if not node:
            return nid, None

        # Same ceiling as the new-generation path (see MAX_PARALLEL_DOCS). Acquired
        # after the cheap early-outs so a skipped node never holds a slot.
        from db import is_cascade_doc_finalised
        is_final = await is_cascade_doc_finalised(session_id, nid)
        if is_final and not fin_choices.get(nid, False):
            await emit_event(session_id, "node_skipped_finalised", {"node_id": nid})
            return nid, None

        # Get current version + content for delta
        cur_ver = await get_cascade_doc_current_version(session_id, nid)
        new_ver = cur_ver + 1

        existing_content = ""
        if cur_ver > 0:
            old_cascade = await get_cascade_doc_by_node_version(session_id, nid, cur_ver)
            if old_cascade:
                old_doc = await get_document(old_cascade["file_id"])
                if old_doc:
                    existing_content = old_doc.get("content_summary", "")[:4000]

        async with _doc_semaphore():
            await emit_event(session_id, "node_updating", {
                "node_id":      nid,
                "from_version": cur_ver,
                "to_version":   new_ver,
            })

            result = await generate_single_document(
                session_id=session_id,
                node_id=nid,
                project_ctx=project_ctx,
                generated_docs=generated,
                version=new_ver,
                delta_items=delta_items,
                existing_content=existing_content,
            )
            return nid, result

    tasks   = [asyncio.create_task(_apply_one(nid)) for nid in current_wave]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for item in results:
        if isinstance(item, Exception):
            # Same rule as the new-generation wave: a refusal blocks every remaining
            # document, so surface it rather than burying it in return_exceptions.
            if getattr(item, "reason", ""):
                raise item
            print(f"   Delta wave {wave_num} error: {item}")
            continue
        nid, doc = item
        if doc:
            generated[nid]  = doc
            completed      += 1
            await emit_event(session_id, "node_updated", {
                "node_id":       nid,
                "file_id":       doc["file_id"],
                "file_name":     doc["file_name"],
                "output_format": doc["output_format"],
                "version":       doc["version"],
            })

    next_delta_wave = wave_idx + 1
    is_complete     = next_delta_wave >= len(delta_waves)

    return {
        "generated_docs":    generated,
        "completed_docs":    completed,
        "current_delta_wave":next_delta_wave,
        "is_complete":       is_complete,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LANGGRAPH ROUTING (conditional edges)
# ═══════════════════════════════════════════════════════════════════════════════

def route_after_wave(state: CascadeState) -> str:
    if state.get("error"):
        return END
    if state.get("is_complete") or state["current_wave_idx"] >= len(state["wave_order"]):
        return END
    return "generate_wave"


def route_after_delta_wave(state: CascadeState) -> str:
    if state.get("error"):
        return END
    if state.get("is_complete"):
        return END
    delta_waves = state.get("delta_waves") or []
    if state.get("current_delta_wave", 0) >= len(delta_waves):
        return END
    return "apply_delta_wave"


# ═══════════════════════════════════════════════════════════════════════════════
# LANGGRAPH GRAPH CONSTRUCTION
# ═══════════════════════════════════════════════════════════════════════════════

def _build_new_generation_graph() -> StateGraph:
    g = StateGraph(CascadeState)
    g.add_node("extract_context", node_extract_context)
    g.add_node("plan_waves",      node_plan_waves)
    g.add_node("generate_wave",   node_generate_wave)
    g.add_edge(START,             "extract_context")
    g.add_edge("extract_context", "plan_waves")
    g.add_edge("plan_waves",      "generate_wave")
    g.add_conditional_edges("generate_wave", route_after_wave)
    return g


def _build_delta_graph() -> StateGraph:
    g = StateGraph(CascadeState)
    g.add_node("apply_delta_wave", node_apply_delta_wave)
    g.add_edge(START, "apply_delta_wave")
    g.add_conditional_edges("apply_delta_wave", route_after_delta_wave)
    return g


_new_gen_graph   = _build_new_generation_graph().compile(checkpointer=_checkpointer)
_delta_graph     = _build_delta_graph().compile(checkpointer=_checkpointer)


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINTS
# ═══════════════════════════════════════════════════════════════════════════════

async def run_new_cascade(
    session_id:      str,
    input_text:      str,
    input_file_name: str,
    selected_nodes:  List[str],
    username:        str,
    root_node_id:    Optional[str] = None,
) -> None:
    """
    Entry point for a full cascade generation (mode='new').
    Runs asynchronously — caller should fire-and-forget or await in background task.
    SSE events are streamed to the frontend via the session queue.
    """
    print(f"\n🚀 Cascade NEW | session={session_id} | docs={len(selected_nodes)} | user={username}")

    # Save session record
    await save_cascade_session(
        session_id=session_id,
        mode="new",
        username=username,
        input_file_name=input_file_name,
        selected_nodes=selected_nodes,
        total_docs=len(selected_nodes),
    )

    await emit_event(session_id, "session_start", {
        "session_id":   session_id,
        "total_docs":   len(selected_nodes),
        "mode":         "new",
        "input_file":   input_file_name,
    })

    initial_state = make_initial_state(
        session_id=session_id,
        mode="new",
        username=username,
        input_text=input_text,
        input_file_name=input_file_name,
        selected_nodes=selected_nodes,
    )

    # Inject the user-uploaded root document as a synthetic parent entry so that
    # direct children of the root can reference its content via _get_parent_contents().
    # No generation happens — this is purely the text the user already imported.
    if root_node_id:
        initial_state["generated_docs"][root_node_id] = {
            "node_id":        root_node_id,
            "file_id":        "",
            "file_name":      input_file_name,
            "output_format":  "",
            "content_summary": input_text[:MAX_SOURCE_CHARS],
            "version":        0,
            "model_used":     "",
            "is_finalised":   False,
            "finalised_at":   None,
            "finalised_by":   None,
            "_is_input":      True,
        }

    config = {"configurable": {"thread_id": session_id}}

    # Open the durable trace for this run. Started here rather than inside the graph
    # so that a failure in graph construction is still captured, and closed in the
    # finally below so an interrupted run is recorded as interrupted rather than
    # silently absent. See runlog.py.
    runlog.start_run(
        "cascade",
        session_id=session_id,
        mode="new",
        username=username,
        input_file=input_file_name,
        input_chars=len(input_text or ""),
        selected_nodes=selected_nodes,
        total_docs=len(selected_nodes),
    )
    await runlog.emit_env()

    try:
        await _new_gen_graph.ainvoke(initial_state, config=config)
        await update_cascade_session_status(session_id, "complete")
        await emit_event(session_id, "session_complete", {
            "session_id": session_id,
            "docs_generated": len(selected_nodes),
        })
        runlog.finish_run("ok", docs_generated=len(selected_nodes))
    except Exception as e:
        print(f"   ⚠ Cascade error: {e}")
        runlog.record_error(e, where="run_new_cascade")
        await update_cascade_session_status(session_id, "failed", error=str(e)[:500])
        await emit_event(session_id, "session_error", {"error": str(e)[:300]})
        runlog.finish_run("failed", error=str(e)[:300])
    finally:
        signal_sse_done(session_id)


async def run_delta_cascade(
    session_id:          str,
    source_session_id:   str,
    delta_node_id:       str,
    delta_waves:         List[List[str]],
    delta_items:         List[Dict],
    delta_summary:       str,
    user_delta_nodes:    List[str],
    user_finalised_choices: Dict[str, bool],
    project_context:     Dict,
    generated_docs:      Dict[str, Any],
    username:            str,
) -> None:
    """
    Entry point for a delta cascade update (mode='delta').
    Called after user has confirmed which downstream docs to update.
    """
    print(f"\n⚡ Cascade DELTA | session={session_id} | nodes={len(user_delta_nodes)}")

    # Save delta session record
    await save_cascade_session(
        session_id=session_id,
        mode="delta",
        username=username,
        input_file_name=f"delta_{delta_node_id}",
        selected_nodes=user_delta_nodes,
        total_docs=len(user_delta_nodes),
        delta_node_id=delta_node_id,
    )
    await update_cascade_session_status(
        session_id, "running", delta_summary=delta_summary
    )

    await emit_event(session_id, "delta_start", {
        "session_id":   session_id,
        "source_node":  delta_node_id,
        "total_nodes":  len(user_delta_nodes),
        "delta_items":  delta_items,
    })

    state = CascadeState(
        session_id=session_id,
        mode="delta",
        username=username,
        input_text="",
        input_file_name=f"delta_{delta_node_id}",
        delta_node_id=delta_node_id,
        delta_from_version=None,
        delta_new_text=None,
        delta_input_type=None,
        project_context=project_context,
        selected_nodes=user_delta_nodes,
        wave_order=[],
        current_wave_idx=0,
        generated_docs=generated_docs,
        delta_items=delta_items,
        affected_nodes=None,
        user_delta_nodes=user_delta_nodes,
        user_finalised_choices=user_finalised_choices,
        delta_waves=delta_waves,
        current_delta_wave=0,
        total_docs=len(user_delta_nodes),
        completed_docs=0,
        error=None,
        is_complete=False,
    )

    config = {"configurable": {"thread_id": session_id}}

    runlog.start_run(
        "delta",
        session_id=session_id,
        mode="delta",
        username=username,
        source_session=source_session_id,
        delta_node=delta_node_id,
        input_file=f"delta_{delta_node_id}",
        selected_nodes=user_delta_nodes,
        total_docs=len(user_delta_nodes),
        delta_summary=str(delta_summary)[:400],
    )
    await runlog.emit_env()

    try:
        await _delta_graph.ainvoke(state, config=config)
        await update_cascade_session_status(session_id, "complete")
        await emit_event(session_id, "delta_complete", {
            "session_id":    session_id,
            "updated_nodes": user_delta_nodes,
        })
        runlog.finish_run("ok", docs_generated=len(user_delta_nodes))
    except Exception as e:
        print(f"   ⚠ Delta cascade error: {e}")
        runlog.record_error(e, where="run_delta_cascade")
        await update_cascade_session_status(session_id, "failed", error=str(e)[:500])
        await emit_event(session_id, "session_error", {"error": str(e)[:300]})
        runlog.finish_run("failed", error=str(e)[:300])
    finally:
        signal_sse_done(session_id)
