"""
prompts.py — prompt builders for the LLM reviewers.

Every builder has the SAME signature and receives a single ReviewInputs object, so a
reviewer can never again be instructed to check against material it was not given.

═══════════════════════════════════════════════════════════════════════════════
v1's quietest bug
═══════════════════════════════════════════════════════════════════════════════
reviewer_consistency.py's prompt said:

    "Check: terminology used consistently (matches research glossary), ..."

...and its prompt body was built from `draft_json` alone. No research_text. No schema.
It was told to check against a glossary it was never shown. One of the four reviewers
could not perform its stated function, and because reviewers fail open (return [] on
error) it produced zero findings and looked like a clean pass.

v2 gives consistency the CASCADE glossary — which is the right ground truth anyway,
since consistency across 31 documents is the thing that actually matters.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..config import (CTX_DRAFT, CTX_RESEARCH, CTX_PLAN, CTX_GLOSSARY, CTX_SOURCE,
                      CTX_GROUNDING)
from ..jsonutil import truncate

_JSON_CONTRACT = (
    'Return ONLY this JSON, nothing else:\n'
    '{"findings":[{"severity":"CRITICAL|MAJOR|MINOR|NIT","section":"...",'
    '"issue":"...","fix":"..."}]}\n'
    "An empty findings array is a valid and expected answer for good work. Do not "
    "invent findings to appear thorough. Reserve CRITICAL for something that would "
    "embarrass the delivery team in front of the client; MAJOR for a real defect; "
    "MINOR and NIT for polish."
)


@dataclass
class ReviewInputs:
    """Everything any reviewer could need. Builders take what they need and ignore the rest."""
    draft_json: str
    node: Dict[str, Any]
    schema: str = ""
    plan_text: str = ""
    research_text: str = ""
    cascade_glossary: str = ""
    output_format: str = ""
    delta_items: List[Dict[str, Any]] = field(default_factory=list)
    previous_content: str = ""
    # ── Ground truth. Added because NO reviewer could previously see either of these. ──
    # Every check ran against artefacts derived from the same inputs (research pack,
    # plan, glossary), so the panel could catch a document that contradicted itself but
    # never one that ignored the source or the template. Measured consequence: the
    # generator omitted four modules that are named in the first 400 characters of the
    # source document, and the panel passed it.
    source_text: str = ""      # the actual input document (SP51/SOW and parents)
    grounding_text: str = ""   # the admin-uploaded reference template

    # Set when this reviewer runs as a FORK of the generation session. The source,
    # grounding, research pack, plan and glossary are then already in the branch's
    # history as cache — re-sending them would pay full price for context the
    # session has already bought.
    #
    # The DRAFT is still sent explicitly even in this mode. The history holds the
    # model's raw emission, whereas the draft that ships has been through
    # _sanitize_plan (which rewrites strings). Reviewers must judge the artefact
    # that ships, so correctness wins over the extra tokens here.
    in_session: bool = False


_IN_SESSION_NOTE = (
    "CONTEXT ALREADY IN THIS CONVERSATION (above): the source document, the "
    "reference template, the research pack, the section plan and the terminology "
    "contract. Treat them as your ground truth. They are not missing — do not ask "
    "for them, and do not excuse a finding on the basis that you cannot see them.\n\n"
)


def _block(inp: ReviewInputs, label: str, text: str, limit: int) -> str:
    """A context block, or nothing when the branch already holds it in history."""
    if inp.in_session:
        return ""
    return f"{label}\n{truncate(text, limit)}\n\n"


SYSTEMS = {
    "accuracy": (
        "You are a principal Salesforce delivery consultant doing a technical review. "
        "You judge substance, not style. You do not rewrite. You are hard to satisfy "
        "but you do not manufacture problems."
    ),
    "alignment": (
        "You are a delivery quality auditor. You check a document against the plan it "
        "was built from, item by item. Coverage gaps are your only concern."
    ),
    "consistency": (
        "You are the terminology and coherence auditor for a multi-document engagement. "
        "Your job is to stop 31 separately written documents from drifting apart."
    ),
    "delta_alignment": (
        "You are verifying a targeted change to an existing document. You care about "
        "two things only: did the requested change land, and was anything else disturbed."
    ),
}


def accuracy(inp: ReviewInputs) -> str:
    return (
        f"ACCURACY REVIEW of a '{inp.node['label']}' document "
        f"(tier {inp.node.get('tier','')}, phase {inp.node.get('phase','')}).\n\n"
        + (_IN_SESSION_NOTE if inp.in_session else "")
        + _block(inp, "GROUND TRUTH — researched facts:", inp.research_text, CTX_RESEARCH)
        + _block(inp,
                 "REFERENCE TEMPLATE — how this deliverable is built (form only; it "
                 "belongs to a DIFFERENT project, so its scope, names and figures are "
                 "NOT ground truth):", inp.grounding_text, CTX_GROUNDING)
        + f"DRAFT UNDER REVIEW (JSON):\n{truncate(inp.draft_json, CTX_DRAFT)}\n\n"
        "Check:\n"
        "- Factual correctness against the ground truth above. Figures, dates, names, "
        "scope boundaries — do they match?\n"
        "- Fabrication: any entity, system, date, role or number that appears in the "
        "draft but nowhere in the ground truth is a CRITICAL finding.\n"
        "- CONTAMINATION: any name, figure, date or entity carried over from the "
        "REFERENCE TEMPLATE that does not belong to THIS project is a CRITICAL "
        "finding. The reference is a worked example, not a source of facts.\n"
        "- FIDELITY OF FORM: does the draft follow the reference's conventions — "
        "column/heading names, hierarchy, ID and date formats, density of detail? "
        "Departures from its FORM are MAJOR; departures from its SCOPE are correct "
        "and must NOT be reported.\n"
        "- Is the reasoning sound and are the recommended approaches actually feasible "
        "for this engagement?\n"
        "- Domain terminology used correctly, not just consistently.\n\n"
        f"{_JSON_CONTRACT}"
    )


def alignment(inp: ReviewInputs) -> str:
    return (
        f"ALIGNMENT REVIEW of a '{inp.node['label']}' document.\n\n"
        + (_IN_SESSION_NOTE if inp.in_session else "")
        + _block(inp, "THE SOURCE DOCUMENT (the authority on what is in scope):",
                 inp.source_text, CTX_SOURCE)
        + _block(inp, "THE PLAN IT WAS SUPPOSED TO EXECUTE:", inp.plan_text, CTX_PLAN)
        + f"DRAFT UNDER REVIEW (JSON):\n{truncate(inp.draft_json, CTX_DRAFT)}\n\n"
        "FIRST — SCOPE COVERAGE against the SOURCE (do this before anything else, and "
        "do it exhaustively):\n"
        "- List every module, workstream, country, phase and deliverable named in the "
        "source document.\n"
        "- For each one, find the section/sheet that covers it in the draft.\n"
        "- Anything named in the source with NO corresponding section is a CRITICAL "
        "finding. Report it as a missing section and name it.\n"
        "- A section that exists in the draft but is not supported by the source (for "
        "example carried over from a reference template belonging to another project) "
        "is a MAJOR finding.\n"
        "- Judge coverage against the SOURCE, not against the plan. If the plan itself "
        "omitted something the source names, that is still a CRITICAL finding — the "
        "plan is not the authority here, the source is.\n\n"
        "THEN — plan execution:\n"
        "- Work through the plan's TRACEABILITY map line by line; is every planned "
        "section present, at the planned volume?\n"
        "- Was any requirement silently dropped?\n"
        "- Did the author redesign anything the plan had already decided?\n"
        "- Any unresolved `[PLAN ISSUE: ...]` marker carried into the output?\n\n"
        f"{_JSON_CONTRACT}"
    )


def consistency(inp: ReviewInputs) -> str:
    return (
        f"CONSISTENCY REVIEW of a '{inp.node['label']}' document.\n\n"
        "This document is one of ~31 generated independently for the same engagement. "
        "They must read as one coherent set, so the binding terminology contract below "
        "is the ground truth — not this document's own internal habits.\n\n"
        + (_IN_SESSION_NOTE if inp.in_session else "")
        + _block(inp, "PROJECT TERMINOLOGY CONTRACT:", inp.cascade_glossary, CTX_GLOSSARY)
        + f"DRAFT UNDER REVIEW (JSON):\n{truncate(inp.draft_json, CTX_DRAFT)}\n\n"
        "Check:\n"
        "- Every entity, system, role, environment and phase name matches its CANONICAL "
        "form in the contract. A variant that the contract explicitly replaces is a "
        "MAJOR finding, even where the meaning is obvious.\n"
        "- Date, version and ID formats follow the contract's conventions.\n"
        "- No FORBIDDEN PHRASING from the contract appears.\n"
        "- Internally: no section contradicts another, cross-references resolve, and "
        "the same concept is not named two ways within this document.\n\n"
        f"{_JSON_CONTRACT}"
    )


def delta_alignment(inp: ReviewInputs) -> str:
    import json as _json
    return (
        f"DELTA VERIFICATION for a '{inp.node['label']}' document that was just "
        "regenerated to absorb a change from an upstream document.\n\n"
        f"THE CHANGE THAT WAS SUPPOSED TO LAND:\n"
        f"{truncate(_json.dumps(inp.delta_items, indent=2), 3000)}\n\n"
        f"PREVIOUS VERSION (summary):\n{truncate(inp.previous_content, CTX_SOURCE)}\n\n"
        f"NEW VERSION (JSON):\n{truncate(inp.draft_json, CTX_DRAFT)}\n\n"
        "Two questions only:\n"
        "1. LANDED — is every item of the change above actually reflected in the new "
        "version? Anything missed is CRITICAL.\n"
        "2. COLLATERAL — was anything NOT related to the change altered, dropped or "
        "reworded between the previous and new version? Unrequested change is MAJOR; "
        "dropped content is CRITICAL.\n\n"
        "Do not review general quality here. Only landed-ness and collateral damage.\n\n"
        f"{_JSON_CONTRACT}"
    )


BUILDERS = {
    "accuracy":        accuracy,
    "alignment":       alignment,
    "consistency":     consistency,
    "delta_alignment": delta_alignment,
}
