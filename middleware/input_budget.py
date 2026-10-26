"""
input_budget.py — how much source text each stage may read, held separately for adhoc.

WHY THIS EXISTS
---------------
The limits on source text were scattered across four modules and set against four
different assumptions: 250,000 per file and 500,000 combined on the adhoc input side,
then 40,000 for scope extraction, 80,000 per source for generation and 40,000 for
research and review inside code that cascade also runs. Adhoc borrowed cascade's own
constant for its source window, so neither could change without moving the other —
and when adhoc's extraction ceiling was raised, the window behind it silently became
the binding limit.

Two rules make the separation safe:

  * ADHOC-OWNED limits (per-file extraction, combined input, the merged input handed
    on as a source) run only on the adhoc path, so they are owned here outright.

  * SHARED limits live in cascade_agent.py and quality/*, which cascade also runs.
    Those call sites keep their existing constant as the default and ask
    override(field, default) for the value. Outside an adhoc scope() there is no
    override, so they receive their own constant back — cascade reads exactly the
    numbers it always read, from exactly the same place.

Every override below is None, and every adhoc-owned value equals today's number, so
introducing this module changes nothing. tests/test_prompt_identity.py proves it.
Raising adhoc is a separate step, sized from a measured run.
"""

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

# cascade_agent.extract_project_context slices its source with this same literal.
# Kept as a name here only so the coverage report can state it;
# tests/test_budget_mechanism.py fails if the two ever drift apart.
SCOPE_EXTRACTION_CHARS = 40_000


@dataclass(frozen=True)
class AdhocBudget:
    # ── Adhoc-owned ─────────────────────────────────────────────────────────────
    per_file_chars: int = 250_000     # extraction ceiling per uploaded file
    merged_chars: int = 500_000       # combined input accepted from all files
    # The adhoc reading WINDOW: how much of the combined input every stage reads.
    #
    # Was 80,000 here and 40,000 in research and review — sized for a ~21K SOW, so a
    # 92K three-file input had research and review reading under half of it. 300,000
    # chars is roughly 75-100K tokens. Measured on this seat, single Opus calls of
    # ~240-249K tokens complete, so the window is not near a model limit; it is set
    # where the largest Sonnet prompt (the alignment reviewer: source + draft + plan +
    # research) still stays under 200K tokens even if Sonnet's window were 200K.
    #
    # Input that exceeds it is FITTED in app.py — low-value sheets dropped first, then
    # every file given a fair share — so no stage ever cuts it positionally.
    parent_chars: int = 300_000       # merged input handed to the pipeline as a source
    # Field-level blocks from several workbooks, combined. Applies only when TWO OR
    # MORE files contribute — one file reproduces exactly the block it always did. The
    # parser caps each file itself (12 tabs, 60 fields), but nothing bounded N of them.
    structured_chars: int = 120_000

    # ── Overrides for shared code. None = use the shared constant (cascade's) ───
    # Every source-reading stage gets the same window as parent_chars, so no stage
    # reads less of the input than generation does — the reviewer that checks scope
    # coverage must be able to see everything the generator was given.
    agent2_chars: Optional[int] = 300_000       # extract_project_context (cascade: 40,000)
    gen_source_chars: Optional[int] = 300_000   # _build_gen_prompt (cascade: 80,000)
    gen_max_sources: Optional[int] = None       # adhoc passes one source; keep cascade's 6
    # Larger inputs yield larger project-context JSON, which was cut mid-structure.
    gen_ctx_chars: Optional[int] = 40_000       # _build_gen_prompt (cascade: 20,000)
    stage_source_chars: Optional[int] = 300_000 # research + alignment (cascade: 40,000)
    stage_max_sources: Optional[int] = None     # adhoc passes one source; keep cascade's 6
    stage_ctx_chars: Optional[int] = 40_000     # research project context (cascade: 20,000)
    # Longest string a generated cell may hold (cascade: 120). Left None here; only
    # adhoc_pipeline.long_cell_budget() sets it, for the templates it names.
    cell_chars: Optional[int] = None


ADHOC = AdhocBudget()

# The budget in force for the current task. Context variables are copied into every
# task asyncio.gather creates, so all six reviewers see the same budget as the stage
# that launched them. Nothing in the pipeline hands work to a thread pool, where a
# context variable would NOT follow — keep it that way, or copy the context across.
_ACTIVE: ContextVar[Optional[AdhocBudget]] = ContextVar("projectzen_input_budget",
                                                       default=None)


@contextlib.contextmanager
def scope(budget: AdhocBudget = ADHOC) -> Iterator[AdhocBudget]:
    """Apply `budget` to every shared call site reached inside this block."""
    token = _ACTIVE.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE.reset(token)


def override(field: str, default: int) -> int:
    """
    The limit a shared call site should apply.

    Returns `default` — the call site's own constant — unless an adhoc scope is active
    AND that scope sets the field. Cascade never enters a scope, so it always gets its
    own constant back.
    """
    budget = _ACTIVE.get()
    if budget is None:
        return default
    value = getattr(budget, field, None)
    return default if value is None else value


def active() -> Optional[AdhocBudget]:
    """The budget in force, or None outside an adhoc scope (i.e. on cascade)."""
    return _ACTIVE.get()


# ── Coverage: how much of each file the model actually reads ─────────────────────

def adhoc_windows(budget: AdhocBudget = ADHOC) -> Dict[str, int]:
    """
    How many leading characters of the adhoc input each kind of step reads.

      generation — the step that writes the document
      analysis   — scope extraction, research and the alignment reviewer; the
                   smallest of them, since that is what bounds what is checked
    """
    from cascade_agent import MAX_SOURCE_CHARS      # read-only, for reporting
    from quality.config import CTX_SOURCE

    def pick(value: Optional[int], default: int) -> int:
        return default if value is None else value

    return {
        "generation": min(budget.parent_chars,
                          pick(budget.gen_source_chars, MAX_SOURCE_CHARS)),
        "analysis": min(pick(budget.stage_source_chars, CTX_SOURCE),
                        pick(budget.agent2_chars, SCOPE_EXTRACTION_CHARS)),
    }


def visibility(final_text: str, parts: List[Dict[str, Any]],
               windows: Dict[str, int]) -> List[Dict[str, Any]]:
    """
    Per file: what percentage of it sits inside each window of `final_text`.

    Each part's text appears verbatim in final_text — merge() places files under
    headers, Delta Update places the previous version first — so its position is
    found rather than assumed. Searching forward from the end of the previous match
    keeps two identical files from being reported as the same one. A part that cannot
    be found is skipped rather than guessed at.
    """
    rows: List[Dict[str, Any]] = []
    cursor = 0
    for part in parts or []:
        text = part.get("text") or ""
        if not text:
            continue
        start = final_text.find(text, cursor)
        if start < 0:
            continue
        end = start + len(text)
        cursor = end
        row: Dict[str, Any] = {"name": part.get("name") or "input", "chars": len(text)}
        for key, width in windows.items():
            seen = max(0, min(end, width) - start)
            row[key] = round(100.0 * seen / len(text), 1)
        rows.append(row)
    return rows
