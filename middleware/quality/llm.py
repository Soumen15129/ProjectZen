"""
llm.py — the package's ONLY seam to ProjectZen's LLM transport.

v1 BUG (fatal): stage_research.py, stage_plan.py, stage_rework.py and reviewer_base.py
each did `import anthropic` / `anthropic.AsyncAnthropic()`.  In current ProjectZen:
  * there is no `anthropic` import anywhere in middleware/,
  * `anthropic` is not in requirements.txt (only claude-agent-sdk>=0.2.0),
  * cascade_agent.py calls `from llm_client import complete`,
  * there is deliberately no ANTHROPIC_API_KEY — auth is the local `claude` CLI's
    Enterprise login, which the raw anthropic SDK cannot use.
So those four files failed at IMPORT time, not at auth time.

Everything in this package calls through here.  Swapping transport again later means
editing this one file.

Note on max_tokens: llm_client.complete() takes (prompt, system, model, on_chunk).
ClaudeAgentOptions exposes no token ceiling at that layer, only max_turns /
max_budget_usd — which is why every MAX_TOKS_* constant from v1 is gone rather than
"fixed".  cascade_agent.py's own MAX_TOKS_CTX / MAX_TOKS_GEN are already dead
constants today (declared at lines 83-84, referenced nowhere).
"""

from typing import Awaitable, Callable, Optional

try:
    # Normal case: package lives at middleware/quality/, so middleware/ is importable.
    from llm_client import complete as _complete           # type: ignore
except ImportError:  # pragma: no cover
    try:
        from ..llm_client import complete as _complete     # type: ignore
    except Exception as exc:  # pragma: no cover
        raise ImportError(
            "quality/llm.py could not import llm_client.complete. Install this package "
            "as middleware/quality/ (next to llm_client.py), or edit the import above."
        ) from exc


async def complete(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    """Pass-through to llm_client.complete. Kept as a named seam, not an alias."""
    return await _complete(prompt, system=system, model=model, on_chunk=on_chunk)
