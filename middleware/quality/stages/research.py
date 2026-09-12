"""
research.py — Stage 1: per-document research pack.

Changes from v1:
  * Calls llm_client.complete() (was anthropic.AsyncAnthropic — fatal import error).
  * No max_tokens.
  * Receives the CASCADE glossary and is told to EXTEND it, not invent a rival one.
    v1's per-document glossary was the mechanism by which 31 documents drifted apart.
  * Gains a `system` prompt (v1 sent everything as one user turn).
  * Fails open — on error returns "", generation proceeds exactly as today.
"""

from typing import Any, Dict

from ..config import STAGE_MODELS, MAX_ATTEMPTS, CTX_GROUNDING, CTX_SOURCE, CTX_PROJECT
from ..glossary import as_prompt_block
from ..jsonutil import truncate
from ..llm import complete
from ..refusal import is_refusal

_SYSTEM = (
    "You are a senior Salesforce delivery consultant assembling source material for "
    "another consultant to write from. You do not write the deliverable itself. You "
    "produce dense, specific, evidence-based notes with no filler."
)


def build_prompt(node: dict, project_ctx: Dict[str, Any], parent_contents: Dict[str, str],
                 grounding_text: str, cascade_glossary: str = "") -> str:
    parents = ""
    for pid, content in list((parent_contents or {}).items())[:6]:
        parents += f"\n--- parent document: {pid} ---\n{truncate(content, CTX_SOURCE)}\n"

    grounding = ""
    if grounding_text:
        grounding = f"\nREFERENCE TEMPLATE (structure to respect):\n{truncate(grounding_text, CTX_GROUNDING)}\n"

    return (
        f"You are researching before a '{node['label']}' document is written "
        f"(phase: {node.get('phase','')}, type: {node.get('type','')}, "
        f"tier: {node.get('tier','')}).\n\n"
        "Do NOT write the document. Produce the research pack its author will build from.\n\n"
        f"PROJECT CONTEXT:\n{truncate(project_ctx, CTX_PROJECT)}\n"
        f"{parents}{grounding}"
        f"{as_prompt_block(cascade_glossary)}\n"
        "PRODUCE (markdown, compact):\n"
        "0. IN-SCOPE INVENTORY — enumerate, FROM THE PROJECT CONTEXT AND PARENT "
        "DOCUMENTS ONLY, every module, workstream, country, phase and deliverable "
        "that is in scope for THIS engagement. The REFERENCE TEMPLATE above is from a "
        "different project and its scope does not apply — use it for form, never for "
        "scope. List each item once, with the wording the source uses. If the source "
        "names something the reference has no equivalent for, it still belongs here. "
        "Downstream stages build the document's structure from this list, so an "
        "omission here silently drops a whole section from the deliverable.\n"
        "1. DELIVERABLE DEFINITION — what this specific document must contain to be "
        "signed off by the client, in your own words.\n"
        "2. BEST PRACTICES — the delivery standards and patterns that apply to THIS "
        "document type. Be concrete; name the practice, say why it applies here.\n"
        "3. KEY FACTS & CONSTRAINTS — every fact from the context above that this "
        "document must respect. Quote figures, dates, names and scope boundaries exactly.\n"
        "4. TERMINOLOGY ADDENDUM — terms this document needs that the PROJECT "
        "TERMINOLOGY CONTRACT above does not already cover. EXTEND the contract, never "
        "contradict it. If the contract covers everything, write 'None required'.\n"
        "5. RISKS / OPEN QUESTIONS — what is ambiguous or missing, flagged clearly.\n\n"
        "Rules: specific to THIS project's facts. No generic consulting filler. If the "
        "context does not support a claim, say so rather than inventing it."
    )


async def run_research(node: dict, project_ctx: Dict[str, Any],
                       parent_contents: Dict[str, str], grounding_text: str,
                       cascade_glossary: str = "", conv=None) -> str:
    """`conv`: an optional shared llm_client.Conversation. When given, this stage runs
    as a turn in that session so the ~28s subprocess spawn is paid once for the whole
    research -> plan -> generate chain, and the context it sends stays in history for
    the later turns instead of being re-sent."""
    prompt = build_prompt(node, project_ctx, parent_contents, grounding_text, cascade_glossary)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if conv is not None:
                return await conv.turn(prompt, model=STAGE_MODELS["research"])
            return await complete(prompt, system=_SYSTEM, model=STAGE_MODELS["research"])
        except Exception as exc:
            if is_refusal(exc):
                raise            # a refusal is not a content failure
            if attempt == MAX_ATTEMPTS:
                return ""    # fail open — generator still works from context alone
    return ""
