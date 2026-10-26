"""
test_prompt_identity.py — prove that input-budget changes do not alter what the model sees.

    python tests/test_prompt_identity.py --record    # write the baseline (run BEFORE a change)
    python tests/test_prompt_identity.py             # compare against it  (run AFTER)

Every prompt the pipeline sends is captured through a stub transport (no real LLM
calls) and hashed. The inputs are synthetic, numbered line by line, and deliberately
LARGER than every cap — 8 sources of 100K, 100K of grounding, an oversized project
context. That matters: a cap that a test never exceeds is a cap the test cannot see,
so a refactor that broke one would pass silently.

Scenarios marked CHANGES_EXPECTED are adhoc paths that a fix is meant to alter; every
other scenario must stay byte-identical, and cascade scenarios must ALWAYS do so.
"""
import os, sys, json, hashlib, re, asyncio, types, contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # middleware/

# ── Capturing stub transport, installed before anything imports llm_client ──────
CAPTURED = []


def _classify(p: str) -> str:
    if "Extract all project facts" in p:
        return "agent2"
    if "Return ONLY this JSON" in p:
        return "review"
    if "HARD RULES" in p:
        return "generate"
    return "text"


_CTX = json.dumps({"project_name": "Synthetic", "client_name": "Client",
                   "delivery_partner": "Accenture", "in_scope_modules": ["EC", "Payroll"]})
_PLAN = json.dumps({"title": "Doc", "subtitle": "v1", "sections": [
    {"heading": f"S{i}", "level": 1, "paragraphs": ["content one", "content two"],
     "bullets": []} for i in range(8)]})
_MD = "## 1. DELIVERABLE\nx\n## 2. FACTS\ny\n"


async def complete(prompt, system=None, model=None, on_chunk=None, **kw):
    kind = _classify(prompt)
    CAPTURED.append({"kind": kind, "system": system or "", "prompt": prompt})
    out = {"agent2": _CTX, "review": '{"findings": []}', "generate": _PLAN}.get(kind, _MD)
    if on_chunk:
        await on_chunk(out)
    return out


class Conversation:
    session_id = None                      # no fork id -> reviewers use complete()
    model = None

    def __init__(self, model=None, **kw):
        self.model = model

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def turn(self, prompt, model=None, on_chunk=None):
        return await complete(prompt, model=model, on_chunk=on_chunk)


@contextlib.contextmanager
def usage_scope(*a, **k):
    yield


class LlmUnavailable(Exception):
    pass


async def _noop(*a, **k):
    return ""


stub = types.ModuleType("llm_client")
stub.complete, stub.Conversation, stub.usage_scope = complete, Conversation, usage_scope
stub.usage_summary = lambda *a, **k: {}
stub.LlmUnavailable = LlmUnavailable
stub.FALLBACK_MODEL = "claude-sonnet-4-6"
stub.ToolSpec = lambda *a, **k: None
stub.run_tool_agent = stub.author_file = stub.preflight = _noop
sys.modules["llm_client"] = stub


# ── Deterministic synthetic inputs (no client data) ──────────────────────────────
def text(tag: str, n_chars: int) -> str:
    out, size, i = [], 0, 0
    while size < n_chars:
        i += 1
        line = f"{tag} line {i:06d}: requirement detail for item {i} under {tag}"
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)[:n_chars]


def sheet_text(tag: str, n_chars: int) -> str:
    """Shaped like extractor.py's xlsx output so the structured parser engages."""
    rows = ["[Config " + tag + "]", "Field ID,Label,Type,Required,Picklist"]
    size, i = sum(len(r) + 1 for r in rows), 0
    while size < n_chars:
        i += 1
        row = f"{tag}_field_{i:05d},Label {i},String,Yes,Opt{i % 7}"
        rows.append(row)
        size += len(row) + 1
    return "\n".join(rows)[:n_chars]


BIG_CTX = {"project_name": "Synthetic", "client_name": "Client",
           "delivery_partner": "Accenture",
           "in_scope_modules": [f"Module{i:03d}" for i in range(400)],
           "notes": text("CTX", 30_000)}
PARENTS8 = {f"src{i}": text(f"SRC{i}", 100_000) for i in range(8)}
GROUND = text("GRD", 100_000)
SCHEMA = ('{"title":"string","subtitle":"string","sections":[{"heading":"string",'
          '"level":1,"paragraphs":["string"],"bullets":["string"]}]}')


def norm(s: str) -> str:
    s = re.sub(r"\d{4}-\d{2}-\d{2}", "<DATE>", s)
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                  "<UUID>", s)


def digest(items):
    return [{"kind": c["kind"], "len": len(c["prompt"]),
             "sha": hashlib.sha256(
                 norm(c["system"] + "\x00" + c["prompt"]).encode("utf-8")).hexdigest()[:20]}
            for c in items]


async def _emit(*a, **k):
    pass


# ── Scenarios ────────────────────────────────────────────────────────────────────
async def scenarios():
    import cascade_agent as ca
    from quality.pipeline import run_quality_pipeline
    from quality.glossary import build_cascade_glossary
    import adhoc_pipeline
    from input_merge import merge
    res = {}

    def take(name):
        res[name] = digest(CAPTURED)
        CAPTURED.clear()

    # CASCADE — must ALWAYS be byte-identical
    node = {"label": "RTM", "tier": "T1", "phase": "Design", "type": "matrix",
            "owner": "PMO", "trigger": "x", "notes": ""}
    CAPTURED.append({"kind": "direct", "system": "", "prompt": ca._build_gen_prompt(
        "rtm", node, BIG_CTX, PARENTS8, "docx", GROUND)})
    take("cascade/build_gen_prompt")

    CAPTURED.append({"kind": "direct", "system": "", "prompt": ca._build_gen_prompt(
        "rtm", node, BIG_CTX, PARENTS8, "docx", GROUND,
        delta_items=[{"change": "date moved"}], existing_content=text("OLD", 10_000),
        version=2)})
    take("cascade/build_gen_prompt_delta")

    await ca.extract_project_context(text("SOW", 150_000), "pid-c3")
    take("cascade/extract_project_context")

    await build_cascade_glossary("pid-c5", BIG_CTX, text("GLO", 150_000), emit_event_fn=_emit)
    take("cascade/glossary")

    async def gen(p, conv=None):
        return _PLAN

    await run_quality_pipeline(
        session_id="pid-c4", node=node, node_id="rtm", version=1,
        project_ctx=BIG_CTX, parent_contents=PARENTS8, grounding_text=GROUND,
        schema=SCHEMA, output_format="docx", detail="Generate sections.",
        base_prompt="GEN\n\nHARD RULES:\n1. Return ONLY valid JSON.",
        stream_generate_fn=gen, sanitize_fn=lambda x: x, emit_event_fn=_emit)
    take("cascade/quality_pipeline_T1")

    # ADHOC — called exactly as app.py calls it: merged text plus the per-file parts.
    # The single-file scenarios are PROTECTED: supplying parts must reproduce the
    # baseline recorded before parts existed.
    async def adhoc(name, inp, parts, fmt="docx"):
        kw = {"input_parts": parts} if _accepts_parts(adhoc_pipeline.run) else {}
        await adhoc_pipeline.run(template="Test Script", node_id="test-script",
                                 output_format=fmt, user_prompt="Generate the document.",
                                 input_text=inp, **kw)
        take(name)

    small = text("SMALL", 20_000)
    await adhoc("adhoc/small_single_20k", small, [{"name": "small.txt", "text": small}])
    one = sheet_text("ONE", 150_000)
    await adhoc("adhoc/large_single_150k_sheet", one, [{"name": "one.xlsx", "text": one}])
    files = [{"name": f"f{i}.xlsx", "ext": "xlsx", "text": sheet_text(f"MF{i}", 40_000)}
             for i in range(3)]
    await adhoc("adhoc/multi_file_CHANGES_EXPECTED", merge(files)["text"],
                [{"name": f["name"], "text": f["text"]} for f in files])
    prev = sheet_text("PREV", 60_000)
    await adhoc("adhoc/delta_prefixed_CHANGES_EXPECTED",
                "PREVIOUS VERSION OF THIS DOCUMENT - 'old.xlsx'\nCarry forward.\n"
                + "-" * 70 + "\n" + prev,
                [{"name": "old.xlsx", "text": prev}])
    return res


def _accepts_parts(fn):
    import inspect
    return "input_parts" in inspect.signature(fn).parameters


# Scenarios a later, deliberate change was meant to alter. The baseline is NEVER
# re-recorded: it is the original code's output, so cascade keeps being compared with
# what it did before any of this work began.
#   Phase 2 raised the adhoc window, so a 150K single file is now read in full.
PHASE2_EXPECTED = {"adhoc/large_single_150k_sheet"}


def main():
    path = os.path.join(HERE, "fixtures", "prompt_baseline.json")
    with open(os.devnull, "w", encoding="utf-8") as dn, contextlib.redirect_stdout(dn):
        res = asyncio.run(scenarios())
    if "--record" in sys.argv:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
        for k, v in res.items():
            print(f"recorded {k:46} {len(v):>3} prompts  "
                  f"{sum(x['len'] for x in v):>10,} chars")
        return 0
    with open(path, encoding="utf-8") as fh:
        base = json.load(fh)
    fail = 0
    for k in base:
        b, n = base[k], res.get(k, [])
        same = [x["sha"] for x in b] == [x["sha"] for x in n]
        expected = "CHANGES_EXPECTED" in k or k in PHASE2_EXPECTED
        tag = "IDENTICAL" if same else ("changed (expected)" if expected else "*** CHANGED ***")
        if not same and not expected:
            fail += 1
        print(f"{tag:20} {k:46} {len(n):>3} prompts")
        if not same:
            for i, (x, y) in enumerate(zip(b, n)):
                if x["sha"] != y["sha"]:
                    print(f"      first diff at prompt {i} ({x['kind']}): "
                          f"len {x['len']:,} -> {y['len']:,}")
                    break
            if len(b) != len(n):
                print(f"      prompt count {len(b)} -> {len(n)}")
    print("\nRESULT:", "PASS - nothing outside the expected scenarios changed" if not fail
          else f"FAIL - {fail} protected scenario(s) changed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
