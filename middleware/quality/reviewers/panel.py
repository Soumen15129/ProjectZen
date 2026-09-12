"""
panel.py — runs the reviewer panel and merges findings into a decision.

Parallelism deliberately mirrors node_generate_wave's own asyncio.gather pattern, so
the concurrency model of the codebase stays uniform.

Concurrency note (measured, not assumed): ProjectZen's graph is 31 nodes in 16 waves
with a MAXIMUM WAVE WIDTH OF 4. Each llm_client.complete() drives a `claude` CLI
subprocess. So peak concurrency is 4 documents x len(reviewers) CLI processes.
With v2's tier scoping that is at most 4 x 3 = 12 — comfortable on a laptop. Under
v1's "all four reviewers on all 31 documents" it was 16 concurrent CLI processes, and
the real problem was never the burst, it was the 16 SEQUENTIAL waves each waiting on a
full research->plan->generate->review->rework chain.

Structure validation runs unconditionally and costs nothing, so a document is never
completely unreviewed — including on the "minimal" depth profile and the delta path.
"""

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..config import MAX_ATTEMPTS, REVIEWER_MODELS, REWORK_ON_SEVERITIES
from ..jsonutil import loads_or_none
from ..llm import complete
from .prompts import BUILDERS, SYSTEMS, ReviewInputs
from .structure import review_structure
from ..refusal import is_refusal

SEVERITY_ORDER = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2, "NIT": 3}


def _fork_from():
    """llm_client.fork_from if the transport provides it, else None."""
    try:
        from ...llm_client import fork_from
        return fork_from
    except Exception:
        try:
            from llm_client import fork_from      # type: ignore
            return fork_from
        except Exception:
            return None


async def _run_one(name: str, prompt: str, system: str,
                   fork_sid: Optional[str] = None,
                   fork_model: Optional[str] = None,
                   on_state: Optional[Callable[[str, str, int], Awaitable[None]]] = None
                   ) -> List[Dict[str, Any]]:
    """
    Run a single LLM reviewer. Fails open — a broken reviewer must not block a doc.

    With `fork_sid`, the reviewer runs as a FORK of the generation session: the
    source, grounding, research and plan are already in its history as cache, and its
    branch cannot see the other reviewers' turns. Independence is preserved; only the
    bill changes. Any failure to fork falls straight through to the independent call.
    """
    if on_state is not None:
        try:
            await on_state(name, "start", 0)
        except Exception:
            pass

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if fork_sid:
                fork = _fork_from()
                if fork is not None:
                    # Two things must match the base session or the cache is lost:
                    #
                    #  * the MODEL — caching is per-model.
                    #  * the SYSTEM PROMPT — it sits at the very start of the cached
                    #    prefix, so giving the fork the reviewer's own system prompt
                    #    invalidates everything after it. Measured: doing that left
                    #    cache_read at 4,712 (system+tools only) and made the fork
                    #    cost MORE than an independent call.
                    #
                    # So the reviewer's role goes at the top of the USER turn instead,
                    # where it costs a few dozen tokens and breaks nothing.
                    conv = fork(fork_sid, model=fork_model, system=None, max_turns=2)
                    async with conv:
                        raw = await conv.turn(f"{system}\n\n{prompt}" if system else prompt)
                else:
                    raw = await complete(prompt, system=system,
                                         model=REVIEWER_MODELS.get(name))
            else:
                raw = await complete(
                    prompt, system=system, model=REVIEWER_MODELS.get(name)
                )
            data = loads_or_none(raw)
            if not isinstance(data, dict):
                raise ValueError("reviewer did not return a JSON object")
            findings = data.get("findings") or []
            if not isinstance(findings, list):
                findings = []
            clean: List[Dict[str, Any]] = []
            for f in findings:
                if not isinstance(f, dict):
                    continue
                sev = str(f.get("severity", "MINOR")).upper()
                if sev not in SEVERITY_ORDER:
                    sev = "MINOR"
                clean.append({
                    "severity": sev,
                    "section": str(f.get("section", ""))[:120],
                    "issue": str(f.get("issue", ""))[:400],
                    "fix": str(f.get("fix", ""))[:400],
                    "reviewer": name,
                })
            if on_state is not None:
                try:
                    await on_state(name, "done", len(clean))
                except Exception:
                    pass
            return clean
        except Exception as exc:
            if is_refusal(exc):
                raise            # a refused review is not an approved document
            if attempt == MAX_ATTEMPTS:
                if on_state is not None:
                    try:
                        await on_state(name, "failed", 0)
                    except Exception:
                        pass
                return []
    return []


async def run_panel(
    inp: ReviewInputs,
    reviewers: List[str],
    include_structure: bool = True,
    fork_sid: Optional[str] = None,
    fork_model: Optional[str] = None,
    on_state: Optional[Callable[[str, str, int], Awaitable[None]]] = None,
    volume_floors: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Returns {"findings": [...severity-sorted...], "decision": "APPROVED|CONDITIONAL|REWORK",
             "blocking": int, "by_reviewer": {name: count}}.
    """
    findings: List[Dict[str, Any]] = []

    # ── Free, deterministic, always-on ────────────────────────────────────────
    if include_structure:
        findings.extend(review_structure(inp.draft_json, inp.schema, inp.output_format,
                                         volume_floors))

    # ── LLM reviewers, in parallel ────────────────────────────────────────────
    # in_session tells the builders to omit context the fork already holds. It is set
    # only when a fork is genuinely available, so the non-fork path still receives
    # every block exactly as before.
    can_fork = bool(fork_sid and fork_model and _fork_from() is not None)
    if not can_fork:
        fork_sid = None
    inp.in_session = can_fork

    tasks = []
    names = []
    for name in reviewers:
        builder = BUILDERS.get(name)
        if builder is None:
            continue
        names.append(name)
        tasks.append(asyncio.create_task(
            _run_one(name, builder(inp), SYSTEMS.get(name, ""),
                     fork_sid=fork_sid, fork_model=fork_model,
                     on_state=on_state)
        ))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                findings.extend(r)
            elif isinstance(r, BaseException) and is_refusal(r):
                # return_exceptions=True would otherwise bury this and the
                # document would ship as though the panel had approved it.
                raise r

    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity", "NIT"), 3))
    blocking = [f for f in findings if f.get("severity") in REWORK_ON_SEVERITIES]

    if blocking:
        decision = "REWORK"
    elif findings:
        decision = "CONDITIONAL"      # only MINOR/NIT — deliver and report them
    else:
        decision = "APPROVED"

    by_reviewer: Dict[str, int] = {}
    for f in findings:
        by_reviewer[f.get("reviewer", "?")] = by_reviewer.get(f.get("reviewer", "?"), 0) + 1

    return {
        "findings": findings,
        "decision": decision,
        "blocking": len(blocking),
        "by_reviewer": by_reviewer,
    }
