import os, sys
# sys.path[0] is this tests/ directory, so `import llm_client` resolves to the stub
# beside this file rather than middleware/llm_client.py — no real LLM calls are made.
# middleware/ is appended (not inserted at 0) so `quality.*` resolves while the stub
# keeps priority for llm_client.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio, json, llm_client
from quality.pipeline import run_quality_pipeline
from quality.glossary import build_cascade_glossary
from quality.reviewers.structure import review_structure
from quality import checkpoint as ck

SCHEMA = '{"title":"string","subtitle":"string","sections":[{"heading":"string","level":1,"paragraphs":["string"],"bullets":["string"]}]}'
EVENTS = []
async def emit(sid, ev, data): EVENTS.append(ev)

# ---- 1. structure validator (pure python, no LLM) ----
bad = json.dumps({"title": "X", "sections": []})
f = review_structure(bad, SCHEMA, "docx")
print("1 structure findings:", [(x["severity"], x["issue"][:42]) for x in f])

ph = json.dumps({"title":"X","subtitle":"TBD - to be determined","sections":[{"heading":"H","level":1,"paragraphs":["Lorem ipsum dolor"],"bullets":[]}]})
print("1b placeholder caught:", any("Placeholder" in x["issue"] for x in review_structure(ph, SCHEMA, "docx")))

# ---- 2. checkpoint keys cannot collide ----
print("2 keyA:", ck.doc_key("s1","rtm",1,"research"))
print("2 keyB:", ck.doc_key("s1","solution-design",1,"research"))
print("2 distinct:", ck.doc_key("s1","rtm",1,"research") != ck.doc_key("s1","solution-design",1,"research"))
try:
    ck.doc_key("", "rtm", 1, "research"); print("2 guard: FAIL")
except ValueError: print("2 guard: OK (unqualified key rejected)")

# ---- 3. full pipeline, T1 node ----
async def main():
    llm_client.CALLS.clear()
    await build_cascade_glossary("s1", {"project_name":"CCEP"}, "source text", emit_event_fn=emit)
    node = {"label":"RTM","tier":"T1","phase":"Design","type":"matrix"}
    chunks = []
    async def on_chunk(c): chunks.append(c)
    async def stream_gen(p):
        
        if "APPROVED PLAN" in p: assert p.rindex("APPROVED PLAN") < p.rindex("HARD RULES:"), "ORDERING BUG"
        return json.dumps({"title":"RTM","subtitle":"v1","sections":[
            {"heading":"S%d"%i,"level":1,"paragraphs":["real content here","and more"],"bullets":[]} for i in range(6)]})
    out = await run_quality_pipeline(
        session_id="s1", node=node, node_id="rtm", version=1,
        project_ctx={"project_name":"CCEP"}, parent_contents={"sow":"..."},
        grounding_text="", schema=SCHEMA, output_format="docx",
        detail="Generate 6-12 sections.",
        base_prompt="GEN PROMPT BODY\n\nHARD RULES:\n1. Return ONLY valid JSON.\n5. Return ONLY the JSON object, nothing else.",
        stream_generate_fn=stream_gen, sanitize_fn=lambda x: x,
        emit_event_fn=emit, on_chunk=on_chunk, is_delta=False,
    )
    print("3 T1 total LLM calls:", len(llm_client.CALLS))
    print("3 models used:", sorted({c['model'] for c in llm_client.CALLS}))
    print("3 output parses:", bool(json.loads(out)))
    print("3 ordering assertion passed (plan before HARD RULES)")

    # ---- 4. T4 minimal node = zero LLM reviewers ----
    llm_client.CALLS.clear()
    n4 = {"label":"Onboarding Pack","tier":"T4","phase":"Mobilise","type":"doc"}
    await run_quality_pipeline(
        session_id="s1", node=n4, node_id="onb", version=1,
        project_ctx={}, parent_contents={}, grounding_text="", schema=SCHEMA,
        output_format="docx", base_prompt="B\n\nHARD RULES:\n5. only JSON",
        stream_generate_fn=stream_gen, sanitize_fn=lambda x: x, emit_event_fn=emit)
    print("4 T4 LLM calls:", len(llm_client.CALLS), "(structure validation is free)")

    # ---- 5. delta path still gets a reviewer ----
    llm_client.CALLS.clear()
    await run_quality_pipeline(
        session_id="s1", node=node, node_id="rtm", version=2,
        project_ctx={}, parent_contents={}, grounding_text="", schema=SCHEMA,
        output_format="docx", base_prompt="B\n\nHARD RULES:\n5. only JSON",
        stream_generate_fn=stream_gen, sanitize_fn=lambda x: x, emit_event_fn=emit,
        is_delta=True, delta_items=[{"change":"date moved"}], existing_content="old")
    print("5 delta LLM calls:", len(llm_client.CALLS), "(v1 was 0 reviewers, always)")
    print("5 events seen:", sorted(set(EVENTS)))

asyncio.run(main())
