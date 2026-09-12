"""
plan.py — Stage 2: section plan mapped to the target JSON schema.

Changes from v1:
  * llm_client.complete(), no max_tokens.
  * Carries the cascade glossary through, so the plan is already written in the
    canonical vocabulary and the generator inherits it.
  * Adds Oracle's "glossary usage plan" item — which terms apply in which section.
  * Adds the field-count contract derived from _FORMAT_DETAIL, so the plan commits to
    a concrete number of sections/sheets/slides and the alignment reviewer has a
    countable target instead of a vibe.

The plan is the contract.  Its traceability map is what the alignment reviewer checks
against, which is why alignment is the reviewer with the highest catch rate on
dropped-requirement failures.
"""

from typing import Any, Dict

from ..config import STAGE_MODELS, MAX_ATTEMPTS, CTX_GROUNDING, CTX_RESEARCH, CTX_SCHEMA
from ..glossary import as_prompt_block
from ..jsonutil import truncate
from ..llm import complete
from ..refusal import is_refusal

_SYSTEM = (
    "You are a delivery architect writing the build contract for a document. You do "
    "not write content. You write a plan so specific that two different authors would "
    "produce substantially the same document from it."
)


_DEFAULT_SCOPE_RULE = (
    "SCOPE RULE — read before planning anything:\n"
    "The TEMPLATE STRUCTURE above comes from a DIFFERENT project. Take its form "
    "from it (layout, column names, conventions, depth) and its SCOPE from the "
    "research pack's IN-SCOPE INVENTORY. Produce one section/sheet for every item "
    "in that inventory, including items the template has no equivalent for, and "
    "drop template sections this project does not include. Copying the template's "
    "section list is a CRITICAL planning failure — the generator follows this plan "
    "literally, so a section missing here is missing from the deliverable.\n\n"
)


def build_prompt(node: dict, research_pack: str, grounding_text: str, schema: str,
                 detail: str = "", cascade_glossary: str = "",
                 scope_rule: str = "", instructions: str = "") -> str:
    """
    `instructions` are the explicit orders for THIS document — the admin template
    prompt plus the user's own request. Measured: they reached the generate stage but
    not this one, so a plan built without them committed to five slides including a
    "Key Contacts" slide the user had explicitly excluded, and the generator then had
    to fight its own plan. Empty for cascade, which has no per-document instructions.

    `scope_rule` replaces the default SCOPE RULE when supplied.

    It exists because the default rule — "one section for every inventory item, and
    copying the template's section list is a CRITICAL planning failure" — is exactly
    right for a document that must cover a scope inventory, and exactly wrong for a
    dense deck that is meant to mirror a reference's five slides. Measured: with a
    VOLUME REQUIREMENT asking for four slides AND an admin instruction asking for four,
    the planner still produced ten, one per in-scope workstream, because this rule is
    more forceful and more specific than either.

    Defaults to "" so every existing caller — all of cascade — builds a byte-identical
    prompt.
    """
    grounding = ""
    if grounding_text:
        grounding = f"\nTEMPLATE STRUCTURE:\n{truncate(grounding_text, CTX_GROUNDING)}\n"
    detail_block = f"\nVOLUME REQUIREMENT: {detail}\n" if detail else ""
    scope_block = scope_rule if scope_rule else _DEFAULT_SCOPE_RULE
    # Placed LAST before the deliverables list and labelled as overriding, because
    # everything above it is derived guidance while this is what was actually asked for.
    instr_block = (
        "EXPLICIT INSTRUCTIONS FOR THIS DOCUMENT — these OVERRIDE the template "
        "structure, the volume requirement and your own judgement wherever they "
        "conflict. If they name a section count, that count is the answer. If they "
        "exclude a section, it does not appear in the plan at all:\n"
        f"{truncate(instructions, 4000)}\n\n"
    ) if instructions.strip() else ""

    return (
        f"You are planning a '{node['label']}' document BEFORE it is written.\n"
        "Do NOT write content. Produce the plan its author will fill in exactly.\n\n"
        f"RESEARCH PACK:\n{truncate(research_pack, CTX_RESEARCH)}\n"
        f"{grounding}"
        f"{as_prompt_block(cascade_glossary)}"
        f"\nTARGET JSON SCHEMA (the document is emitted as this):\n{truncate(schema, CTX_SCHEMA)}\n"
        f"{detail_block}\n"
        f"{scope_block}"
        f"{instr_block}"
        "PRODUCE (markdown, compact):\n"
        "1. SECTION OUTLINE — one entry per section/sheet/slide the document will "
        "contain, honouring the VOLUME REQUIREMENT exactly. Each entry: the heading, "
        "and 2-4 sentences on what it will contain. Every schema field must be "
        "accounted for; if one is not applicable, say so and why. State explicitly "
        "which inventory item each section serves, and flag any section carried over "
        "from the template that no inventory item justifies.\n"
        "2. TRACEABILITY — a two-column map: each requirement / constraint / source "
        "point -> the section that satisfies it. Nothing from the research pack's KEY "
        "FACTS may be left unaccounted for. This map is what the document will be "
        "audited against.\n"
        "3. GLOSSARY USAGE — which canonical terms apply in which sections.\n"
        "4. DESIGN DECISIONS — the choices being made, each with a one-line rationale.\n"
        "5. ASSUMPTIONS & OPEN QUESTIONS — flagged clearly, not buried.\n\n"
        "Rules: the plan must be mechanical to execute. If a section cannot be filled "
        "from the available facts, say `[PLAN ISSUE: ...]` rather than planning "
        "invented content — the reviewers will pick it up and it can be fixed in rework."
    )


async def run_plan(node: dict, research_pack: str, grounding_text: str, schema: str,
                   detail: str = "", cascade_glossary: str = "", conv=None,
                   scope_rule: str = "", instructions: str = "") -> str:
    """`conv`: optional shared Conversation (see run_research). Running plan as a turn
    after research means the research pack is already in history — it does not have to
    be re-sent, and the model plans with the full reasoning behind it rather than a
    text summary of it."""
    prompt = build_prompt(node, research_pack, grounding_text, schema, detail,
                          cascade_glossary, scope_rule, instructions)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if conv is not None:
                return await conv.turn(prompt, model=STAGE_MODELS["plan"])
            return await complete(prompt, system=_SYSTEM, model=STAGE_MODELS["plan"])
        except Exception as exc:
            if is_refusal(exc):
                raise            # a refusal is not a content failure
            if attempt == MAX_ATTEMPTS:
                return ""    # fail open — generation falls back to context-only
    return ""
