"""
rework.py — Stage 5: bounded targeted-fix loop.

Fixes from v1:
  1. `from cascade_config import MAX_TOKS_GEN` — cascade_config.py does not exist as a
     module anywhere in ProjectZen. This was a phantom import, not a wrong symbol, and
     it is simply gone (no token ceiling exists in the Agent SDK transport).
  2. `import anthropic` -> llm_client.complete().
  3. Its private _strip() (which did not match cascade_agent._clean_json) -> shared
     jsonutil.clean_json.
  4. THE UX BUG: v1's rework produced a corrected draft with NO streaming. content_chunk
     only ever fired inside stage 3. So the user watched draft v0 type out, and the file
     written to disk was silently draft v3. v1's own regression checklist item
     "content_chunk still streams" passed while what the user was shown was stale.
     v2 threads on_chunk through rework, so the frontend sees the corrected document
     being written.
  5. Findings are grouped by section before being sent, so the model receives a coherent
     work order instead of a flat severity-sorted list that jumps between sections.
"""

import json
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..config import STAGE_MODELS, MAX_ATTEMPTS, CTX_DRAFT, CTX_SCHEMA
from ..jsonutil import clean_json, truncate
from ..llm import complete
from ..patch import apply_patch, outline
from ..refusal import is_refusal

_SYSTEM = (
    "You are performing a surgical correction to a structured JSON document. You "
    "change only what the findings require and you preserve every other byte of "
    "content exactly. You are not an editor and not a rewriter."
)

_PATCH_SYSTEM = (
    "You are performing a surgical correction to a structured JSON document by "
    "emitting a JSON Patch. You emit the minimum set of operations that resolve the "
    "findings, and nothing else. You never touch content the findings do not name."
)

# PATCH mode returns only the operations that change; FULL mode re-emits the whole
# document (the original behaviour, kept as the automatic fallback).
_MODE = (os.environ.get("PROJECTZEN_REWORK") or "patch").strip().lower()


def _group_by_section(findings: List[Dict[str, Any]]) -> str:
    """Group blocking findings by section so the fix prompt reads as a work order."""
    by_section: Dict[str, List[Dict[str, Any]]] = {}
    for f in findings:
        by_section.setdefault(f.get("section") or "(unspecified)", []).append(f)

    out = ""
    for section, items in by_section.items():
        out += f"\n### Section: {section}\n"
        for f in items:
            out += (
                f"- [{f.get('severity','MAJOR')}] ({f.get('reviewer','?')}) "
                f"{f.get('issue','')}\n"
                f"  FIX: {f.get('fix','')}\n"
            )
    return out


def build_prompt(node: dict, draft_json: str, findings: List[Dict[str, Any]],
                 schema: str) -> str:
    blocking = [f for f in findings if f.get("severity") in ("CRITICAL", "MAJOR")]
    return (
        f"Correct a '{node['label']}' document. Apply ONLY the fixes listed below.\n"
        "Preserve every other field, value, ordering and wording EXACTLY as given. "
        "Do not improve, reword, expand or reorganise anything the findings do not "
        "name. An unrequested change is a defect.\n\n"
        f"CURRENT DRAFT (JSON):\n{truncate(draft_json, CTX_DRAFT)}\n\n"
        f"FINDINGS TO FIX (grouped by section):\n{_group_by_section(blocking)}\n\n"
        f"SCHEMA (the result must still conform):\n{truncate(schema, CTX_SCHEMA)}\n\n"
        "HARD RULES:\n"
        "1. Return ONLY valid JSON. No markdown. No explanation. No backticks.\n"
        "2. Every string: max 120 chars, ASCII only.\n"
        "3. No trailing commas.\n"
        "4. Numbers in values arrays must be JSON numbers, not strings.\n"
        "5. Return ONLY the JSON object, nothing else."
    )


def build_patch_prompt(node: dict, draft_json: str, findings: List[Dict[str, Any]],
                       doc: Any) -> str:
    """
    The draft still goes in as INPUT (the model must see what it is correcting, and
    input is the cheap, cacheable side). What changes is the OUTPUT: a handful of
    operations instead of a full re-emission of the document.

    The addressable outline is what makes this reliable — without the real JSON
    Pointers in front of it the model invents paths and the ops miss.
    """
    blocking = [f for f in findings if f.get("severity") in ("CRITICAL", "MAJOR")]
    return (
        f"Correct a '{node['label']}' document by emitting a JSON Patch.\n\n"
        f"CURRENT DOCUMENT (JSON):\n{truncate(draft_json, CTX_DRAFT)}\n\n"
        f"ADDRESSABLE PATHS (use these EXACT pointers):\n{outline(doc)}\n\n"
        f"FINDINGS TO FIX (grouped by section):\n{_group_by_section(blocking)}\n\n"
        "Return ONLY this JSON object:\n"
        '{"ops":[{"op":"replace","path":"/a/0/b","value":"new value"}]}\n\n'
        "HARD RULES:\n"
        "1. Supported ops: replace, add, remove. Nothing else.\n"
        "2. Paths must be JSON Pointers that EXIST in the outline above. Use "
        "`/sheets/2/rows/-` to append a row.\n"
        "3. Emit the MINIMUM set of ops that resolves the findings. Do not touch "
        "anything the findings do not name — an unrequested change is a defect.\n"
        "4. Do NOT return the document. Return only the ops object.\n"
        "5. Every string value: max 120 chars, ASCII only.\n"
        "6. Valid JSON only. No markdown, no backticks, no explanation.\n"
    )


async def _stream_out(text: str, on_chunk) -> None:
    """
    Replay the corrected document to the frontend.

    In FULL mode the model's own output stream was the document, so on_chunk could be
    passed straight through. In PATCH mode the model streams pointers, which are not
    the document — piping those to the preview would resurrect the exact bug fix #4
    above was written to kill. So the patch is applied locally and the RESULT is
    streamed instead.
    """
    if on_chunk is None:
        return
    for i in range(0, len(text), 512):
        await on_chunk(text[i:i + 512])


async def _run_full_rework(node, draft_json, findings, schema, sanitize_fn, on_chunk):
    """The original behaviour: re-emit the whole document. Fallback path."""
    prompt = build_prompt(node, draft_json, findings, schema)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = await complete(prompt, system=_SYSTEM,
                                 model=STAGE_MODELS["rework"], on_chunk=on_chunk)
            fixed = json.loads(clean_json(raw))
            return json.dumps(sanitize_fn(fixed))
        except Exception as exc:
            if is_refusal(exc):
                raise                       # refusal, not a bad correction
            if attempt == MAX_ATTEMPTS:
                return draft_json           # keep the last good draft
    return draft_json


async def run_rework_round(
    node: dict,
    draft_json: str,
    findings: List[Dict[str, Any]],
    schema: str,
    sanitize_fn: Callable[[Any], Any],
    on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    """
    One targeted fix pass. Returns the corrected draft JSON string, or the previous
    draft unchanged if every attempt fails (never returns garbage).

    PATCH mode first: a correction round previously re-emitted the entire document,
    ~18K output tokens to change a few sentences, and output is the expensive side of
    the bill. Emitting only the operations makes the round proportional to the size
    of the fix rather than the size of the document.

    Falls back to the full rewrite whenever patching cannot be trusted: unparseable
    ops, no op applied, more misses than hits, or a result that lost content. The
    fallback is byte-for-byte the previous implementation, so the worst case here is
    one wasted call, never a worse document.
    """
    if _MODE != "patch":
        return await _run_full_rework(node, draft_json, findings, schema,
                                      sanitize_fn, on_chunk)

    try:
        doc = json.loads(draft_json)
    except Exception:
        return await _run_full_rework(node, draft_json, findings, schema,
                                      sanitize_fn, on_chunk)

    prompt = build_patch_prompt(node, draft_json, findings, doc)

    # Deliberately fewer attempts than the full path. Re-sending an identical prompt
    # rarely turns a model that would not emit ops into one that will, and every
    # wasted attempt re-sends the whole draft. One retry covers transient truncation;
    # past that the full rewrite is the cheaper certainty.
    patch_attempts = min(2, MAX_ATTEMPTS)
    for attempt in range(1, patch_attempts + 1):
        try:
            # No on_chunk here: the model is streaming pointers, not the document.
            raw = await complete(prompt, system=_PATCH_SYSTEM,
                                 model=STAGE_MODELS["rework"])
            ops = json.loads(clean_json(raw)).get("ops") or []
            if not isinstance(ops, list) or not ops:
                raise ValueError("no operations returned")

            fixed, applied, failures = apply_patch(doc, ops)

            if applied == 0:
                raise ValueError(f"every op missed: {failures[:3]}")
            if len(failures) > applied:
                raise ValueError(f"{len(failures)} misses vs {applied} hits")

            out = json.dumps(sanitize_fn(fixed))

            # A rework corrects; it does not quietly delete. If the result collapsed
            # and no removal was requested, something addressed the wrong node.
            asked_remove = any(o.get("op") == "remove" for o in ops)
            if not asked_remove and len(out) < len(draft_json) * 0.9:
                raise ValueError("patch lost content without a remove op")

            if failures:
                print(f"   [rework] patch: {applied} applied, "
                      f"{len(failures)} skipped ({failures[0][:70]})")
            await _stream_out(out, on_chunk)
            return out

        except Exception as e:
            if is_refusal(e):
                raise
            if attempt == patch_attempts:
                print(f"   [rework] patch failed ({str(e)[:90]}); full rewrite")
                return await _run_full_rework(node, draft_json, findings, schema,
                                              sanitize_fn, on_chunk)
    return draft_json


def no_progress(prev_blocking: int, new_blocking: int) -> bool:
    """Stop the loop when blocking findings stopped decreasing."""
    return new_blocking >= prev_blocking
