"""
tests/llm_client.py — STUB transport for the smoke test. Makes ZERO real LLM calls.

Why this file exists
--------------------
The shipped package's tests/README.md referred to "the stub in this folder", but no
stub was included. test_smoke.py does `llm_client.CALLS.clear()`, and the real
middleware/llm_client.py has no CALLS attribute — so the documented command
(`python tests/test_smoke.py`, "all five checks must pass") raised AttributeError on
the first line of main() before any check ran.

How it wins the import
----------------------
test_smoke.py is executed as `python tests/test_smoke.py` from middleware/, so
sys.path[0] is this directory and `import llm_client` resolves here rather than to
middleware/llm_client.py. quality/llm.py's `from llm_client import complete` picks up
this stub for the same reason. middleware/ is appended AFTER, so `quality.*` still
resolves normally.

Behaviour
---------
Responses are shaped by what the caller asked for, so each stage gets something it can
actually parse. One reviewer returns a single MAJOR finding on its first call and
nothing afterwards — that drives exactly one rework round, which is what exercises the
rework + streaming path rather than short-circuiting at APPROVED.
"""

import json
from typing import Awaitable, Callable, Optional

# Every call made during a test run: [{"model", "kind", "system", "prompt_len"}, ...]
CALLS = []

_REVIEW_MARKER = "Return ONLY this JSON"
_REWORK_MARKER = "Apply ONLY the fixes listed below"
_PATCH_MARKER  = "by emitting a JSON Patch"

_DRAFT = json.dumps({
    "title": "RTM",
    "subtitle": "v1",
    "sections": [
        {"heading": f"S{i}", "level": 1,
         "paragraphs": ["corrected content here", "and more"], "bullets": []}
        for i in range(6)
    ],
})

_MARKDOWN = (
    "## 1. DELIVERABLE DEFINITION\nA traceability matrix for the engagement.\n"
    "## 2. BEST PRACTICES\nOne requirement per row, uniquely identified.\n"
    "## 3. KEY FACTS & CONSTRAINTS\nGo-live 2026-09-01. Scope: EMEA.\n"
    "## 4. TERMINOLOGY ADDENDUM\nNone required.\n"
    "## 5. RISKS / OPEN QUESTIONS\nSource is silent on UAT duration.\n"
)

_reviews_served = {"n": 0}


def reset() -> None:
    """Clear recorded calls and the reviewer counter between test phases."""
    CALLS.clear()
    _reviews_served["n"] = 0


def _classify(prompt: str) -> str:
    if _PATCH_MARKER in prompt:
        return "patch"
    if _REWORK_MARKER in prompt:
        return "rework"
    if _REVIEW_MARKER in prompt:
        return "review"
    return "text"


def _canned(kind: str) -> str:
    if kind == "patch":
        # A valid op against the draft the stub itself returns, so the
        # smoke test exercises patch mode rather than the fallback.
        return json.dumps({"ops": [{"op": "replace",
                                    "path": "/sections/0/paragraphs/0",
                                    "value": "corrected content here"}]})
    if kind == "rework":
        return _DRAFT
    if kind == "review":
        _reviews_served["n"] += 1
        if _reviews_served["n"] == 1:
            # First review only — forces one rework round so that path is exercised.
            return json.dumps({"findings": [{
                "severity": "MAJOR",
                "section": "sections[0]",
                "issue": "Stubbed finding to exercise the rework loop.",
                "fix": "Rewrite section 0.",
            }]})
        return json.dumps({"findings": []})
    return _MARKDOWN


async def complete(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    """Mirrors middleware/llm_client.complete's signature exactly."""
    kind = _classify(prompt)
    CALLS.append({
        "model": model,
        "kind": kind,
        "system": (system or "")[:50],
        "prompt_len": len(prompt),
    })
    text = _canned(kind)
    if on_chunk is not None:
        for i in range(0, len(text), 80):
            await on_chunk(text[i:i + 80])
    return text


class Conversation:
    """
    Stub of llm_client.Conversation — the shared multi-turn session.

    Records turns so a test can assert the pipeline reused ONE session instead of
    making independent calls. `SESSIONS` counts how many were opened; in the real
    transport each one costs a ~28s subprocess spawn, so the pipeline opening more
    than one per document would be the regression this stub exists to catch.
    """

    def __init__(self, model=None, system=None, effort=None, max_turns=32):
        self.model = model
        self.turns = 0

    async def __aenter__(self):
        SESSIONS.append(self)
        return self

    async def __aexit__(self, *exc):
        return None

    async def turn(self, prompt, model=None, on_chunk=None):
        self.turns += 1
        if model:
            self.model = model
        return await complete(prompt, model=model, on_chunk=on_chunk)


SESSIONS = []
