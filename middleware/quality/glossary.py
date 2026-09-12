"""
glossary.py — the CASCADE-LEVEL shared glossary.

THE most valuable addition in this package, and the one thing the Oracle /
Sequential-Specialists design structurally cannot provide.

Why:
    Oracle produces ONE artifact, so a per-artifact glossary makes that artifact
    internally consistent and the job is done.  ProjectZen produces 31 documents in
    16 waves from one SOW.  A per-document glossary makes each document internally
    consistent AND MUTUALLY INCONSISTENT — the Solution Design says "Integration
    Layer", the Interface Spec says "middleware tier", the Test Strategy says
    "integration bus", and every one of them passes its own consistency review.

    That drift is ProjectZen's actual hardest quality problem, and v1 did not touch it:
    its stage_research.py ran per document and produced an independent glossary each
    time, and its consistency reviewer checked a draft against that same document's own
    research pack — intra-document scope only, identical to Oracle's.

How:
    Built ONCE per cascade, straight after context extraction, from the project context
    and the source document text.  Threaded into: every document's research prompt,
    every document's generation prompt, and the consistency reviewer.  One extra Opus
    call per cascade (not per document) — negligible against 31 documents.

Cost: 1 call.  Effect: every document in the estate uses the same words for the same
things, which is the difference between 31 documents and one coherent document set.
"""

from typing import Any, Dict, Optional

from . import checkpoint as ck
from .config import (STAGE_MODELS, MAX_ATTEMPTS, CASCADE_GLOSSARY, SSE,
                     CTX_PROJECT, CTX_SOURCE, CTX_GLOSSARY)
from .jsonutil import truncate
from .llm import complete

_SYSTEM = (
    "You are the terminology authority for a multi-document Salesforce delivery "
    "engagement. Your output governs the wording of every document produced for this "
    "project. Be decisive: pick ONE canonical term per concept and never offer "
    "alternatives."
)


def _build_prompt(project_ctx: Dict[str, Any], source_text: str) -> str:
    return (
        "A full set of delivery documents (project plan, RTM, solution design, "
        "functional specs, test strategy, cutover plan and more) is about to be "
        "generated for the engagement below. They will be written INDEPENDENTLY and "
        "in parallel, so they will drift apart in terminology unless you fix the "
        "vocabulary now.\n\n"
        f"PROJECT CONTEXT:\n{truncate(project_ctx, CTX_PROJECT)}\n\n"
        f"SOURCE DOCUMENT (extract):\n{truncate(source_text, CTX_SOURCE)}\n\n"
        "PRODUCE a project-wide terminology contract in markdown:\n\n"
        "1. CANONICAL ENTITY NAMES — the exact strings every document must use for the "
        "client, the delivery partner, each system, each environment, each workstream, "
        "each phase. One line each: `canonical  <-  variants seen in the source`.\n"
        "2. CANONICAL DOMAIN TERMS — for every concept that could be phrased more than "
        "one way (integration layer, middleware, interface, data migration object, "
        "cutover, hypercare, defect severity, etc.), the ONE term to use and the "
        "variants it replaces.\n"
        "3. ROLE & OWNERSHIP NAMES — canonical titles for each named role/stakeholder.\n"
        "4. DATE, VERSION & ID CONVENTIONS — how dates, versions, requirement IDs, "
        "interface IDs and test case IDs must be formatted, with one worked example each.\n"
        "5. FORBIDDEN PHRASINGS — informal or ambiguous phrases that must never appear.\n\n"
        "Rules: derive everything from the actual source above — no invented entities. "
        "Where the source is genuinely silent, say `NOT SPECIFIED` rather than guessing. "
        "Be compact: this text is injected into every downstream prompt, so no prose, "
        "no preamble, no commentary. Tables and one-liners only."
    )


async def build_cascade_glossary(
    session_id: str,
    project_ctx: Dict[str, Any],
    source_text: str = "",
    store: Optional[ck.Store] = None,
    emit_event_fn=None,
) -> str:
    """
    Build (or fetch from checkpoint) the cascade-wide glossary.

    Fails OPEN: on any error returns "" and generation proceeds exactly as it does
    today.  This can never break a cascade.
    """
    if not CASCADE_GLOSSARY:
        return ""

    store = store or ck.Store()
    key = ck.cascade_key(session_id, "glossary")

    cached = await store.get(key)
    if cached:
        return cached

    if emit_event_fn:
        await emit_event_fn(session_id, SSE["glossary_start"], {})

    prompt = _build_prompt(project_ctx, source_text)
    text = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            text = await complete(
                prompt, system=_SYSTEM, model=STAGE_MODELS["glossary"]
            )
            break
        except Exception:
            if attempt == MAX_ATTEMPTS:
                text = ""

    if text:
        await store.set(key, text)

    if emit_event_fn:
        await emit_event_fn(session_id, SSE["glossary_done"], {"chars": len(text)})

    return text


async def get_cascade_glossary(
    session_id: str, store: Optional[ck.Store] = None
) -> str:
    """Read-only fetch used by per-document stages. Returns "" if not built."""
    store = store or ck.Store()
    return (await store.get(ck.cascade_key(session_id, "glossary"))) or ""


def as_prompt_block(glossary: str, limit: int = CTX_GLOSSARY) -> str:
    """Render the glossary for injection into a downstream prompt."""
    if not glossary:
        return ""
    return (
        "\n\nPROJECT TERMINOLOGY CONTRACT (binding — every other document in this "
        "engagement uses these exact terms; deviating from them creates inconsistency "
        "across the document set):\n"
        f"{truncate(glossary, limit)}\n"
    )
