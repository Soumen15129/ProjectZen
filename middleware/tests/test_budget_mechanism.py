"""
test_budget_mechanism.py — prove the adhoc input budget actually takes effect, reaches
every stage, and cannot leak into cascade.

    python tests/test_budget_mechanism.py

test_prompt_identity.py proves the budget changes nothing while it is switched off.
This proves the other half, with deliberately tiny override values so the effect is
unmistakable:

  1. inside an adhoc scope, every shared call site applies the override
  2. the override reaches the reviewers, which run as parallel asyncio tasks
  3. a cascade run executing AT THE SAME TIME as an adhoc run, in the same process,
     still gets cascade's own limits — the case a live server actually produces
"""
import os, sys, asyncio, importlib.util, contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "identity", os.path.join(HERE, "test_prompt_identity.py"))
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)                       # installs the capturing stub

import input_budget
from input_budget import AdhocBudget

TINY = AdhocBudget(agent2_chars=3_456, gen_source_chars=2_345, gen_max_sources=2,
                   gen_ctx_chars=777, stage_source_chars=1_234, stage_max_sources=2)
SRC0 = T.PARENTS8["src0"]
FAILS = []


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def cut_at(prompt, content, n):
    """True when exactly the first n chars of content are present, and no more."""
    return content[:n] in prompt and content[:n + 60] not in prompt


async def gen_prompt():
    import cascade_agent as ca
    node = {"label": "RTM", "tier": "T1", "phase": "D", "type": "m",
            "owner": "PMO", "trigger": "x", "notes": ""}
    return ca._build_gen_prompt("rtm", node, T.BIG_CTX, T.PARENTS8, "docx", T.GROUND)


async def pipeline(sid):
    from quality.pipeline import run_quality_pipeline
    node = {"label": "RTM", "tier": "T1", "phase": "D", "type": "m",
            "owner": "PMO", "trigger": "x", "notes": ""}

    async def gen(p, conv=None):
        return T._PLAN

    T.CAPTURED.clear()
    await run_quality_pipeline(
        session_id=sid, node=node, node_id="rtm", version=1, project_ctx={},
        parent_contents=T.PARENTS8, grounding_text="", schema=T.SCHEMA,
        output_format="docx", base_prompt="G\n\nHARD RULES:\n1. JSON.",
        stream_generate_fn=gen, sanitize_fn=lambda x: x, emit_event_fn=T._emit)
    return list(T.CAPTURED)


def research_of(calls):
    return next(c["prompt"] for c in calls if "parent document:" in c["prompt"])


def alignment_of(calls):
    return next(c["prompt"] for c in calls if "ALIGNMENT REVIEW" in c["prompt"])


async def main():
    import cascade_agent as ca

    print("\n1. Override applies inside an adhoc scope (generation + scope extraction)")
    with input_budget.scope(TINY):
        g = await gen_prompt()
        T.CAPTURED.clear()
        await ca.extract_project_context(T.text("SOW", 20_000), "m-a2")
        a2 = T.CAPTURED[-1]["prompt"]
    check("generation source cut to 2,345", cut_at(g, SRC0, 2_345))
    check("generation shows only 2 sources", "SRC1 line" in g and "SRC2 line" not in g)
    check("scope extraction cut to 3,456", cut_at(a2, T.text("SOW", 20_000), 3_456))

    print("\n2. Override reaches research and the parallel reviewers")
    with input_budget.scope(TINY):
        calls = await pipeline("m-p")
    check("research source cut to 1,234", cut_at(research_of(calls), SRC0, 1_234))
    check("research shows only 2 sources",
          "SRC1 line" in research_of(calls) and "SRC2 line" not in research_of(calls))
    al = alignment_of(calls)
    check("alignment reviewer (parallel task) cut to 1,234",
          SRC0[:1_000] in al and SRC0[:1_300] not in al)

    print("\n3. Outside any scope, cascade keeps its own limits")
    g = await gen_prompt()
    check("generation source at cascade's 80,000", cut_at(g, SRC0, 80_000))
    check("generation shows cascade's 6 sources", "SRC5 line" in g and "SRC6 line" not in g)
    calls = await pipeline("m-c")
    check("research at cascade's 40,000", cut_at(research_of(calls), SRC0, 40_000))

    print("\n4. Cascade and adhoc running AT THE SAME TIME in one process")
    results = {}

    async def adhoc_run():
        with input_budget.scope(TINY):
            await asyncio.sleep(0)               # force interleaving with cascade
            results["adhoc"] = await gen_prompt()

    async def cascade_run():
        await asyncio.sleep(0)
        results["cascade"] = await gen_prompt()

    await asyncio.gather(adhoc_run(), cascade_run(), adhoc_run(), cascade_run())
    check("concurrent adhoc got 2,345", cut_at(results["adhoc"], SRC0, 2_345))
    check("concurrent cascade still got 80,000", cut_at(results["cascade"], SRC0, 80_000))
    check("scope is cleared after exit", input_budget.active() is None)

    print("\n5. Limits moved into input_budget kept their values")
    import input_merge
    check("combined-input ceiling still 500,000", input_merge.MAX_MERGED_CHARS == 500_000)
    check("per-file ceiling still 250,000", input_budget.ADHOC.per_file_chars == 250_000)
    check("adhoc window is 300,000 (Phase 2)", input_budget.ADHOC.parent_chars == 300_000)

    print("\n6. Coverage report states the limits the code actually applies")
    T.CAPTURED.clear()
    sow = T.text("SOW", 60_000)
    await ca.extract_project_context(sow, "m-drift")
    check("SCOPE_EXTRACTION_CHARS matches extract_project_context's real cut",
          cut_at(T.CAPTURED[-1]["prompt"], sow, input_budget.SCOPE_EXTRACTION_CHARS))
    aw = input_budget.adhoc_windows()
    check(f"adhoc windows: {aw}", aw == {"generation": 300_000, "analysis": 300_000})

    print("\n7. Coverage maths on real layouts (fixed windows, independent of config)")
    w = {"generation": 80_000, "analysis": 40_000}
    vis = input_budget.visibility
    one = T.text("ONE", 50_000)
    r = vis(one, [{"name": "a", "text": one}], w)[0]
    check(f"single 50K file: writing 100%, analysis 80% -> {r['generation']}, {r['analysis']}",
          r["generation"] == 100.0 and r["analysis"] == 80.0)
    files = [{"name": f"f{i}", "ext": "txt", "text": T.text(f"F{i}", 30_000)} for i in range(3)]
    merged = input_merge.merge(files)["text"]
    rows = vis(merged, [{"name": f["name"], "text": f["text"]} for f in files], w)
    check("merged 3x30K: first file fully read, third file 0% in analysis",
          rows[0]["analysis"] == 100.0 and rows[2]["analysis"] == 0.0)
    check("merged 3x30K: every file located", [x["name"] for x in rows] == ["f0", "f1", "f2"])
    dup = T.text("DUP", 10_000)
    rows = vis(dup + "\n" + dup, [{"name": "d1", "text": dup}, {"name": "d2", "text": dup}], w)
    check("two identical files are reported separately", [x["name"] for x in rows] == ["d1", "d2"])

    print("\n8. Boundary: inputs up to 39,930 chars are byte-identical to before Phase 2")
    # 39,930, not 40,000: the alignment reviewer wraps the source in a 70-character
    # header ("--- Source Input Document (...) ---") BEFORE applying the old 40,000
    # limit, so the old cut bit 70 characters early. Measured, not assumed.
    # PHASE1 reproduces the pre-Phase-2 limits exactly (no overrides, 80K window), which
    # Phase 1 proved identical to the original code. Any input at or under 40,000 must
    # therefore produce the same prompts under both budgets.
    import adhoc_pipeline
    PHASE1 = AdhocBudget(parent_chars=80_000, agent2_chars=None, gen_source_chars=None,
                         gen_ctx_chars=None, stage_source_chars=None, stage_ctx_chars=None)

    async def prompts_under(budget, inp):
        T.CAPTURED.clear()
        with input_budget.scope(budget):
            await adhoc_pipeline._run("Test Script", "test-script", "docx",
                                      "Generate.", inp, input_parts=[{"name": "a", "text": inp}])
        return [T.norm(c["prompt"]) for c in T.CAPTURED]

    for n in (39_930, 12_345):
        inp = T.text("EDGE", n)
        same = await prompts_under(PHASE1, inp) == await prompts_under(input_budget.ADHOC, inp)
        check(f"{n:,}-char input: identical prompts before and after Phase 2", same)
    inp = T.text("EDGE", 39_931)
    diff = await prompts_under(PHASE1, inp) != await prompts_under(input_budget.ADHOC, inp)
    check("39,931-char input differs (control: the test can see a change)", diff)

    print("\n9. Consecutive runs never reuse each other's research or plan")
    # The bug: adhoc keyed the stage cache by template, so run 2 of a template in the
    # same process got run 1's research and plan. Two different inputs, back to back.
    async def research_for(inp):
        T.CAPTURED.clear()
        await adhoc_pipeline.run("Test Script", "test-script", "docx", "Generate.", inp,
                                 input_parts=[{"name": "a", "text": inp}])
        return [c["prompt"] for c in T.CAPTURED if "parent document:" in c["prompt"]]

    first = await research_for(T.text("ECINPUT", 5_000))
    second = await research_for(T.text("COMPINPUT", 5_000))
    check("run 1 researched its input", bool(first) and "ECINPUT line" in first[0])
    check("run 2 ran research again (not served from run 1)", bool(second))
    check("run 2 researched ITS OWN input, not run 1's",
          bool(second) and "COMPINPUT line" in second[0] and "ECINPUT line" not in second[0])

    print("\nRESULT:", "PASS" if not FAILS else f"FAIL ({len(FAILS)}): {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    # The pipeline prints progress lines; this test's own lines start with two spaces
    # plus PASS/FAIL, a digit, or RESULT, so filter with:  | grep -E "PASS|FAIL|RESULT|^[0-9]"
    sys.exit(asyncio.run(main()))
