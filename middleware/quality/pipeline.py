"""
pipeline.py — the single coordination point nested inside generate_single_document().

CONTRACT (unchanged from v1, and it was the right contract):
    generate_single_document keeps its signature and its GeneratedDoc return type.
    This runs INSIDE it, replacing only "build one prompt, make one call" with
    "research -> plan -> generate(plan) -> review -> rework".
    Everything after draft production — templates.generate, versioning, library
    storage, the return dict — is untouched.

WHAT CHANGED FROM v1
  * llm_client.complete() throughout; no anthropic SDK, no max_tokens.
  * Checkpoint keys are session+node+version+stage qualified and cannot be built any
    other way (v1 used bare "research"/"plan", so document 3's research pack was
    served to document 17).
  * Extra prompt material is inserted BEFORE the HARD RULES, not appended after them.
  * Rework streams via on_chunk, so the frontend shows the corrected document rather
    than silently saving a different one than the user watched.
  * Depth is tier-scoped, so a cascade still finishes this afternoon.
  * The delta path gets structure validation plus one delta-alignment reviewer instead
    of no quality assurance at all.
  * A cascade-wide terminology contract is threaded into research, generation and the
    consistency reviewer.

FAILURE POLICY
    Every stage fails open. If research, plan, glossary or any reviewer errors, the
    pipeline degrades to exactly today's single-pass behaviour and the document still
    generates. There is no path where adding this package can fail a cascade that
    would otherwise have succeeded.
"""

from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import checkpoint as ck
from .config import SSE, profile_for
from .glossary import get_cascade_glossary
from .reviewers.panel import run_panel
from .reviewers.prompts import ReviewInputs
from .stages.generate import build_extra_blocks, splice_before_hard_rules
from .stages.plan import run_plan
from .stages.research import run_research
from .stages.rework import no_progress, run_rework_round


async def _noop_emit(*_args, **_kwargs) -> None:
    return None


def _usage_scope(label: str):
    """
    Tag LLM calls with the stage that made them. Degrades to a no-op context manager
    if the transport has no telemetry (the test stub), so this can never be the
    reason a document fails to generate.
    """
    try:
        from ..llm_client import usage_scope
        return usage_scope(label)
    except Exception:
        try:
            from llm_client import usage_scope       # type: ignore
            return usage_scope(label)
        except Exception:
            import contextlib
            return contextlib.nullcontext()


async def run_quality_pipeline(
    *,
    session_id: str,
    node: Dict[str, Any],
    node_id: str,
    version: int,
    project_ctx: Dict[str, Any],
    parent_contents: Dict[str, str],
    grounding_text: str,
    schema: str,
    output_format: str,
    detail: str = "",
    # Replaces the planner's default SCOPE RULE. Empty for every existing caller, so
    # cascade's plan prompt is byte-identical; adhoc supplies one for reference-shaped
    # decks, where "one section per inventory item" is the wrong contract.
    plan_scope_rule: str = "",
    # The explicit orders for this document (admin template prompt + user request).
    # Cascade has none — its nodes are defined by the graph, not by a per-run brief.
    plan_instructions: str = "",
    # Replaces MIN_VOLUME for the structure validator. None = the shared table, so
    # cascade's floors are untouched. Adhoc measures its reference instead.
    volume_floors: Optional[Dict[str, Any]] = None,
    # Forces a depth profile instead of tier routing. DEPTH_BY_TIER exists to keep a
    # 31-node cascade finishing in usable wall-clock on a Pro seat; a single adhoc
    # document has no such budget to protect, and inheriting T2 -> "light" silently
    # cost it the research stage — the planner then logged "RESEARCH PACK IS EMPTY"
    # and took its scope from the reference, which belongs to a different project.
    depth_override: str = "",
    base_prompt: str = "",
    build_prompt_fn: Optional[Callable[..., str]] = None,
    stream_generate_fn: Callable[[str], Awaitable[str]] = None,
    sanitize_fn: Callable[[Any], Any] = None,
    emit_event_fn: Callable[..., Awaitable[None]] = None,
    on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
    is_delta: bool = False,
    delta_items: Optional[List[Dict[str, Any]]] = None,
    existing_content: str = "",
    checkpoint_fns: Optional[Dict[str, Callable]] = None,
) -> str:
    """
    Returns the final draft JSON string. The caller writes the file, versions it, and
    stores it in the library exactly as it does today.

    build_prompt_fn (PREFERRED): a callable that accepts `extra_blocks=` and returns
      the full generation prompt with those blocks placed before HARD RULES. Pass
      `functools.partial(_build_gen_prompt, ...)` after applying the 2-line patch in
      patches/cascade_agent.patch.md.
    base_prompt (FALLBACK): a pre-built prompt string; the blocks are spliced in at
      the "HARD RULES:" marker.
    """
    emit = emit_event_fn or _noop_emit
    store = ck.Store(checkpoint_fns)
    prof = profile_for(node, is_delta=is_delta)
    if depth_override:
        from .config import DEPTH_PROFILES
        if depth_override in DEPTH_PROFILES:
            prof = dict(DEPTH_PROFILES[depth_override])
            prof["_name"] = depth_override
    tag = f"{node_id}/v{version}"

    await emit(session_id, SSE["depth"], {
        "node_id": node_id, "profile": prof["_name"], "tier": node.get("tier", ""),
        "reviewers": prof["reviewers"], "max_rework": prof["rework"],
    })

    glossary = await get_cascade_glossary(session_id, store)

    # ── One session for research -> plan -> generate ──────────────────────────
    # These three stages are a genuine chain: each builds on the last. Running them
    # as separate calls paid the ~28s subprocess spawn three times AND re-sent the
    # same grounding + source on each. Measured: 3 separate calls ~84s vs 31.8s for
    # 3 turns in one session, with the later turns not re-sending prior context.
    #
    # Fails open like everything else: if the session cannot be created, conv stays
    # None and every stage falls back to its own independent call.
    conv = None
    conv_cm = None
    gen_sid = None
    gen_model = None
    if prof["research"] or prof["plan"]:
        try:
            from ..llm_client import Conversation          # type: ignore
        except Exception:
            try:
                from llm_client import Conversation        # type: ignore
            except Exception:
                Conversation = None                        # type: ignore
        if Conversation is not None:
            try:
                conv_cm = Conversation(model=None)
                conv = await conv_cm.__aenter__()
            except Exception as e:
                print(f"   ⚠ Shared session unavailable, using per-call mode: {str(e)[:100]}")
                conv, conv_cm = None, None

    try:
        # ── Stage 1: RESEARCH ─────────────────────────────────────────────────
        research = ""
        if prof["research"]:
            await emit(session_id, SSE["research_start"], {"node_id": node_id})
            with _usage_scope(f"{tag}/research"):
                research = await store.cached(
                    ck.doc_key(session_id, node_id, version, "research"),
                    lambda: run_research(node, project_ctx, parent_contents,
                                         grounding_text, glossary, conv=conv),
                )
            await emit(session_id, SSE["research_done"],
                       {"node_id": node_id, "chars": len(research)})

        # ── Stage 2: PLAN ─────────────────────────────────────────────────────
        plan = ""
        if prof["plan"]:
            await emit(session_id, SSE["plan_start"], {"node_id": node_id})
            with _usage_scope(f"{tag}/plan"):
                plan = await store.cached(
                    ck.doc_key(session_id, node_id, version, "plan"),
                    lambda: run_plan(node, research, grounding_text, schema, detail,
                                     glossary, conv=conv, scope_rule=plan_scope_rule,
                                     instructions=plan_instructions),
                )
            await emit(session_id, SSE["plan_done"],
                       {"node_id": node_id, "chars": len(plan)})

        # ── Stage 3: GENERATE (today's proven streaming path, now plan-guided) ─
        extra = build_extra_blocks(plan, research, glossary)

        if build_prompt_fn is not None:
            gen_prompt = build_prompt_fn(extra_blocks=extra)
        else:
            gen_prompt, spliced = splice_before_hard_rules(base_prompt, extra)
            if not spliced and extra:
                await emit(session_id, "qp_warning", {
                    "node_id": node_id,
                    "warning": "HARD RULES marker not found; blocks appended (degraded).",
                })

        # Generation joins the same session when the caller supports it, so it
        # inherits the research and plan turns instead of being handed a summary.
        with _usage_scope(f"{tag}/generate"):
            try:
                draft_json = await stream_generate_fn(gen_prompt, conv=conv)
            except TypeError:
                draft_json = await stream_generate_fn(gen_prompt)

        # Remember the branch point BEFORE the session closes. Sessions persist, so
        # the reviewers can fork from this id afterwards without holding it open.
        try:
            gen_sid = getattr(conv, "session_id", None) if conv is not None else None
            gen_model = getattr(conv, "model", None) if conv is not None else None
        except Exception:
            gen_sid, gen_model = None, None
    finally:
        if conv_cm is not None:
            try:
                await conv_cm.__aexit__(None, None, None)
            except Exception:
                pass

    # ── Stage 4 + 5: REVIEW then bounded REWORK ───────────────────────────────
    # The source document, as the reviewers' authority on scope. parent_contents holds
    # the uploaded root document (and any earlier cascade docs feeding this node); it is
    # what research and generation were built from, so it is the right ground truth for
    # a coverage check.
    source_text = "\n\n".join(
        f"--- {pid} ---\n{txt}" for pid, txt in (parent_contents or {}).items() if txt
    )

    inputs = ReviewInputs(
        draft_json=draft_json,
        node=node,
        schema=schema,
        plan_text=plan,
        research_text=research,
        cascade_glossary=glossary,
        output_format=output_format,
        delta_items=delta_items or [],
        previous_content=existing_content or "",
        source_text=source_text,
        grounding_text=grounding_text or "",
    )

    max_rounds = int(prof["rework"])
    prev_blocking = 10 ** 9
    review: Dict[str, Any] = {}

    for rnd in range(0, max_rounds + 1):
        inputs.draft_json = draft_json

        await emit(session_id, SSE["review_start"], {"node_id": node_id, "round": rnd})
        # Per-reviewer progress. The panel runs its reviewers in parallel, so a
        # single "review started" event left the UI unable to say which of them
        # was still working. Observational only.
        async def _reviewer_state(rname: str, state: str, n: int) -> None:
            await emit(session_id, "qp_reviewer", {
                "node_id": node_id, "reviewer": rname,
                "state": state, "findings": n, "round": rnd,
            })

        with _usage_scope(f"{tag}/review"):
            review = await run_panel(inputs, prof["reviewers"],
                                     include_structure=True, fork_sid=gen_sid,
                                     fork_model=gen_model,
                                     on_state=_reviewer_state,
                                     volume_floors=volume_floors)
        await emit(session_id, SSE["review_done"], {
            "node_id": node_id, "round": rnd,
            "decision": review["decision"],
            "findings": len(review["findings"]),
            "blocking": review["blocking"],
            "by_reviewer": review["by_reviewer"],
        })

        if review["decision"] in ("APPROVED", "CONDITIONAL"):
            break
        if rnd >= max_rounds:
            break
        if no_progress(prev_blocking, review["blocking"]):
            break
        prev_blocking = review["blocking"]

        await emit(session_id, SSE["rework_start"],
                   {"node_id": node_id, "round": rnd + 1})
        with _usage_scope(f"{tag}/rework"):
            draft_json = await run_rework_round(
                node, draft_json, review["findings"], schema, sanitize_fn,
                on_chunk=on_chunk,      # rework now streams to the frontend
            )
        await emit(session_id, SSE["rework_done"],
                   {"node_id": node_id, "round": rnd + 1})

    # Persist the final findings so the UI (or a later audit) can show what was
    # accepted and what was merely conditional.
    try:
        import json as _json
        await store.set(
            ck.doc_key(session_id, node_id, version, "findings"),
            _json.dumps({
                "decision": review.get("decision", "APPROVED"),
                "findings": review.get("findings", []),
                "profile": prof["_name"],
            }),
        )
    except Exception:
        pass

    await emit(session_id, SSE["quality_pass"], {
        "node_id": node_id,
        "decision": review.get("decision", "APPROVED"),
        "open_findings": len(review.get("findings", [])),
        "profile": prof["_name"],
    })

    return draft_json
