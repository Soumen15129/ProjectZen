"""
generate.py — Stage 3: the generation stage.

Intentionally thin. It does NOT reimplement generation — cascade_agent's existing
streaming call, 3-attempt retry, _clean_json, _sanitize_plan and content_chunk SSE
events all stay exactly as they are. This module only produces the extra prompt
material and puts it in the RIGHT PLACE.

═══════════════════════════════════════════════════════════════════════════════
v1's most damaging bug — and it is a silent one
═══════════════════════════════════════════════════════════════════════════════
_build_gen_prompt ends with:

    HARD RULES:
    1. Return ONLY valid JSON. No markdown. No explanation. No backticks.
    ...
    5. Return ONLY the JSON object, nothing else.

v1's augment_generation_prompt did `return base_prompt + blocks`, appending up to
9,000 characters of research and plan text AFTER that. So the last thing the model
read was no longer "emit JSON only" — it was a wall of markdown prose. On the one
path that already needs a 3-attempt retry to survive parse failures.

Its own docstring says to insert "right before the HARD RULES" — which its own brief
made impossible, because hard constraint #3 forbids modifying _build_gen_prompt.
The two instructions contradict each other.

═══════════════════════════════════════════════════════════════════════════════
v2's fix
═══════════════════════════════════════════════════════════════════════════════
PREFERRED — a 2-line additive change to _build_gen_prompt (see patches/):
    add an optional `extra_blocks: str = ""` parameter and interpolate it
    immediately before the "HARD RULES:" line. Signature stays backward compatible;
    every existing caller keeps working untouched.

FALLBACK — splice_before_hard_rules() below, if you truly cannot touch that function.
It string-splits on the literal "HARD RULES:" marker and inserts there. It is exact
today, and it degrades safely (appends, exactly like v1, and returns a flag saying so)
if that marker ever moves.
"""

from typing import Tuple

from ..glossary import as_prompt_block
from ..config import CTX_RESEARCH, CTX_PLAN
from ..jsonutil import truncate

_MARKER = "HARD RULES:"


def build_extra_blocks(plan_text: str, research_text: str, cascade_glossary: str = "") -> str:
    """
    The material to inject into the generation prompt, in dependency order:
    terminology contract -> researched facts -> the plan to execute.

    Ordered deliberately: the plan is last of the three because it is the operative
    instruction, and it sits directly above HARD RULES so the model's final two inputs
    are "follow this plan" and "emit only JSON".
    """
    blocks = ""

    if cascade_glossary:
        blocks += as_prompt_block(cascade_glossary)

    if research_text:
        blocks += (
            "\nRESEARCHED FACTS (authoritative — these were verified against the "
            "source material before this document was planned; use them verbatim "
            "where they apply):\n"
            f"{truncate(research_text, CTX_RESEARCH)}\n"
        )

    if plan_text:
        blocks += (
            "\nAPPROVED PLAN (this is your contract — do NOT redesign it). Produce "
            "exactly the sections it lists, in its order, at its stated volume. Every "
            "item in its TRACEABILITY map must be visibly satisfied by the section it "
            "is mapped to. Where it records a design decision, implement that decision "
            "rather than substituting your own:\n"
            f"{truncate(plan_text, CTX_PLAN)}\n"
        )

    return blocks


def splice_before_hard_rules(base_prompt: str, extra_blocks: str) -> Tuple[str, bool]:
    """
    Fallback insertion for when _build_gen_prompt cannot be modified.

    Returns (prompt, spliced_ok). spliced_ok is False when the marker was not found,
    in which case the blocks were appended (v1 behaviour) and you should expect
    slightly worse JSON adherence — log it and fix the marker.
    """
    if not extra_blocks:
        return base_prompt, True

    idx = base_prompt.rfind(_MARKER)
    if idx == -1:
        return base_prompt + extra_blocks, False

    return base_prompt[:idx] + extra_blocks + "\n" + base_prompt[idx:], True
