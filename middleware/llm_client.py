"""
llm_client.py — LLM invocation adapter (Claude Agent SDK, enterprise seat).

Single seam between the rest of this app and "how we talk to Claude". Every
call goes through the Claude Agent SDK, which drives the locally-installed
`claude` CLI already authenticated against the user's Claude Enterprise seat
(`claude /login`) — see the credential guard below, which ENFORCES that rather
than assuming it.

Two entry points cover every call site in this codebase:
  complete()       — one-shot / streamed text completion, no tools.
  run_tool_agent()  — a genuine tool-use loop where Claude decides which of
                      the given tools to call (Ask Library, Delta Analysis).

Nothing downstream of these two functions (templates.py, db.py, the SSE
plumbing, the frontend) needs to know the transport changed.
"""

import contextlib
import contextvars
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional

# ── Credential guard ─────────────────────────────────────────────────────────
# ProjectZen must consume the USER'S OWN claude.ai seat, never an API key. That is
# the whole basis of the Enterprise deployment: the customer disabled API keys, and
# generation is billed to the signed-in person, not to a console account.
#
# This was previously only an ASSUMPTION stated in the docstring above. It is not
# safe as one — measured on this machine, an ambient key silently wins:
#
#   $ ANTHROPIC_API_KEY=<invalid> python -c "...complete('ping')"
#   ⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY or another auth
#     source is set and TAKES PRECEDENCE OVER YOUR CLAUDE.AI LOGIN
#   -> the call failed, i.e. the key was used, not the login
#
# So on a corporate machine that exports a key for unrelated tooling, every
# generation would quietly bill that key instead of the user's seat. The SDK spawns
# the `claude` CLI as a subprocess which inherits this environment, so stripping the
# variables HERE — at import, before any call site can run — covers complete(),
# Conversation, author_file() and the tool-use loops in one place.
#
# ANTHROPIC_BASE_URL is deliberately left alone: an enterprise gateway is a
# legitimate reason to redirect, and it is not a billing credential.
for _var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    if os.environ.pop(_var, None):
        # ASCII only: this line prints on a Windows cp1252 console, where a non-ASCII
        # character raises UnicodeEncodeError and would take down startup.
        print(f"   [auth] {_var} was set in the environment and has been ignored. "
              f"ProjectZen bills generation to your Claude sign-in, not an API key.")

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

# ── Refusals ─────────────────────────────────────────────────────────────────
# The SDK distinguishes "the model answered" from "the API refused to answer", and
# ProjectZen previously ignored the difference: complete() just concatenated
# TextBlocks. When an account hit its usage limit, the ~72-character limit notice
# was returned as though it were the research pack. Research "succeeded" with 72
# chars, the plan "succeeded" with 72 chars, generation then could not parse the
# same notice as JSON, retried three times, and the user saw a bare "failed" with
# no cause anywhere — while the session sat in the database as still "running".
#
# A refusal is NOT a content failure and must never be handled by the fail-open
# paths: retrying a rate limit three times only burns the window. It raises.

_REFUSAL_TEXT = {
    "rate_limit": "Claude usage limit reached for this account.",
    "authentication_failed": "Not signed in to Claude, or the session expired.",
    "billing_error": "This Claude account cannot make requests (billing).",
    "invalid_request": "Claude rejected the request as invalid.",
    "server_error": "Claude had a server-side error.",
    "unknown": "Claude could not complete the request.",
}


class LlmTurnLimit(RuntimeError):
    """
    The call hit OUR turn ceiling, not a refusal from Claude.

    Kept separate from LlmUnavailable on purpose. Everything downstream asks
    `is_refusal()` to decide between "stop the run and show the user why" and "fall
    open to a simpler path", and a turn limit is emphatically the second. Measured on a
    real Governance Matrix run: a 26 MB input produced a long plan, the single turn was
    not enough, and "Reached maximum number of turns (1)" was classified as a refusal —
    so the route returned HTTP 503 after seven minutes and never tried the fallback
    that would have produced a document.
    """


class LlmUnavailable(RuntimeError):
    """
    Claude refused or could not serve the request — distinct from a bad answer.

    `reason` is the SDK's own classification ('rate_limit',
    'authentication_failed', 'billing_error', 'invalid_request', 'server_error',
    'unknown'); `detail` is whatever text came back, for display.
    """

    __slots__ = ("reason", "detail")

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason or "unknown"
        self.detail = (detail or "").strip()
        msg = _REFUSAL_TEXT.get(self.reason, _REFUSAL_TEXT["unknown"])
        if self.detail:
            msg = f"{msg} ({self.detail[:160]})"
        super().__init__(msg)

    @property
    def is_retryable(self) -> bool:
        """A transient server hiccup is worth another attempt; a usage limit or a
        missing sign-in is not — retrying those just wastes the user's time."""
        return self.reason == "server_error"


def _check_assistant(message: "AssistantMessage") -> None:
    err = getattr(message, "error", None)
    if err:
        text = " ".join(b.text for b in message.content if isinstance(b, TextBlock))
        raise LlmUnavailable(str(err), text)


def _check_result(rm: "ResultMessage", text: str = "") -> None:
    """
    Second line of defence. Some refusals surface only on the ResultMessage, and
    `subtype` carries the classification when `error` on the assistant turn does not.
    """
    if not getattr(rm, "is_error", False):
        return
    reason = "unknown"
    sub = (getattr(rm, "subtype", "") or "").lower()
    status = getattr(rm, "api_error_status", None)
    if "rate" in sub or "limit" in sub or status == 429:
        reason = "rate_limit"
    elif "auth" in sub or status in (401, 403):
        reason = "authentication_failed"
    elif isinstance(status, int) and status >= 500:
        reason = "server_error"
    detail = " ".join(getattr(rm, "errors", None) or []) or (rm.result or "") or text

    # A turn ceiling is our own limit and is retryable — never a refusal.
    if "max_turns" in sub or "maximum number of turns" in (detail or "").lower():
        raise LlmTurnLimit(detail or "reached the maximum number of turns")

    raise LlmUnavailable(reason, detail)


# ── Model resolution ─────────────────────────────────────────────────────────
# The app hardcodes claude-opus-4-8 for T1/T2 nodes (10 of 31, including Project
# Plan). If an end user's Enterprise org does not expose that model, every one of
# those documents fails outright. fallback_model lets the CLI degrade to a model
# the seat definitely has instead of erroring — a document generated on Sonnet is
# vastly better than no document at all.
FALLBACK_MODEL = os.environ.get("PROJECTZEN_FALLBACK_MODEL") or "claude-sonnet-4-6"


# ── Usage telemetry ──────────────────────────────────────────────────────────
# Every optimisation so far has been argued from measurements taken by hand in a
# scratch script. This makes the real pipeline self-reporting: each call records
# what it actually cost, tagged with the stage that made it, so cost regressions
# show up in the run itself instead of being inferred afterwards.
#
# A ContextVar (not a plain global) because a cascade wave generates up to 4
# documents concurrently on one event loop — a global label would cross-attribute
# between them, and the resulting numbers would be worse than none.

_stage_label: contextvars.ContextVar[str] = contextvars.ContextVar("pz_stage", default="")

# Append-only ledger of every call. list.append is atomic under asyncio, so no lock.
LEDGER: List[Dict[str, Any]] = []


@contextlib.contextmanager
def usage_scope(label: str) -> Iterator[None]:
    """Tag every LLM call made inside this block with `label` (e.g. 'rtm/research')."""
    token = _stage_label.set(label)
    try:
        yield
    finally:
        _stage_label.reset(token)


def _record(rm: "ResultMessage", kind: str) -> None:
    """Fold one ResultMessage into the ledger. Never raises — telemetry must not
    be able to fail a generation."""
    try:
        u = rm.usage or {}

        # ── Per-model usage, straight from the CLI ───────────────────────────
        #
        # `rm.usage` is ONE flat total that cannot tell an Opus token from a
        # Sonnet token, and `model` was hardcoded None — so no run's cost could
        # be attributed to a model at all. That is not a curiosity:
        #
        #   * The pilot runs on an enterprise seat where nothing is metered, but
        #     the hosted platform this becomes is billed per token. Cost per
        #     document is its COGS, and COGS cannot be modelled from a figure
        #     that hides which model produced it.
        #   * `costUSD` here comes from the CLI's own pricing table, so cost
        #     stops depending on us guessing a rate per model.
        #   * FALLBACK_MODEL can silently serve Sonnet where Opus was asked for
        #     (see its note above). `canonicalModel` is the only way to see that
        #     happen; today it is invisible.
        #
        # getattr rather than attribute access, everything coerced to plain
        # types, and its own except: an older SDK without the field, or a value
        # that will not serialise, must cost this telemetry row at most — never
        # the generation that produced it.
        model_usage: Dict[str, Any] = {}
        try:
            for name, m in dict(getattr(rm, "model_usage", None) or {}).items():
                m = dict(m or {})
                model_usage[str(name)] = {
                    "input":        int(m.get("inputTokens") or 0),
                    "output":       int(m.get("outputTokens") or 0),
                    "cache_create": int(m.get("cacheCreationInputTokens") or 0),
                    "cache_read":   int(m.get("cacheReadInputTokens") or 0),
                    "cost_usd":     float(m.get("costUSD") or 0.0),
                    "canonical":    str(m.get("canonicalModel") or ""),
                    "provider":     str(m.get("provider") or ""),
                }
        except Exception:
            model_usage = {}

        # Joined when one call used more than one model, so the column never
        # implies a single model served work that two of them shared.
        model_name = "+".join(sorted(model_usage)) if model_usage else None

        row = {
            "stage":        _stage_label.get(),
            "kind":         kind,
            "model":        model_name,
            "input":        int(u.get("input_tokens") or 0),
            "output":       int(u.get("output_tokens") or 0),
            "cache_create": int(u.get("cache_creation_input_tokens") or 0),
            "cache_read":   int(u.get("cache_read_input_tokens") or 0),
            "cost_usd":     float(rm.total_cost_usd or 0.0),
            "duration_ms":  int(rm.duration_ms or 0),
            "num_turns":    int(rm.num_turns or 0),
            "is_error":     bool(rm.is_error),
            "model_usage":  model_usage,
        }
        LEDGER.append(row)

        # Mirror the same row to the durable per-run trace. The in-memory ledger dies
        # with the process, which is useless for diagnosing a user's failed run — see
        # runlog.py. Imported lazily and guarded separately so that neither a missing
        # module nor a disk problem can cost us the in-memory row above, which several
        # callers read synchronously.
        try:
            from runlog import record_llm_call
            extra = {}
            for src, dst in (("session_id", "claude_session"),
                             ("api_error_status", "api_error_status"),
                             ("subtype", "subtype")):
                val = getattr(rm, src, None)     # getattr, not attribute access: one
                if val:                          # missing field must not cost the row
                    extra[dst] = val
            record_llm_call({**row, **extra})
        except Exception:
            pass
    except Exception:
        pass


def usage_summary(prefix: str = "") -> Dict[str, Any]:
    """
    Totals across ledger entries whose stage starts with `prefix` ("" = everything).

    `billable_input` is the number that actually matters: cache WRITES are billed at
    a premium and cache READS at a large discount, so raw input_tokens alone tells
    you almost nothing about what a run cost.
    """
    rows = [r for r in LEDGER if r["stage"].startswith(prefix)] if prefix else list(LEDGER)
    if not rows:
        return {"calls": 0}
    total = {
        "calls":        len(rows),
        "input":        sum(r["input"] for r in rows),
        "output":       sum(r["output"] for r in rows),
        "cache_create": sum(r["cache_create"] for r in rows),
        "cache_read":   sum(r["cache_read"] for r in rows),
        "cost_usd":     round(sum(r["cost_usd"] for r in rows), 4),
        "seconds":      round(sum(r["duration_ms"] for r in rows) / 1000.0, 1),
        "errors":       sum(1 for r in rows if r["is_error"]),
    }
    total["billable_input"] = total["input"] + total["cache_create"]
    reads = total["cache_read"] + total["cache_create"]
    total["cache_hit_pct"] = round(100.0 * total["cache_read"] / reads, 1) if reads else 0.0
    by_stage: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        s = by_stage.setdefault(r["stage"] or "(untagged)",
                                {"calls": 0, "output": 0, "billable_input": 0, "cost_usd": 0.0})
        s["calls"] += 1
        s["output"] += r["output"]
        s["billable_input"] += r["input"] + r["cache_create"]
        s["cost_usd"] = round(s["cost_usd"] + r["cost_usd"], 4)
    total["by_stage"] = by_stage
    return total


def reset_usage(prefix: str = "") -> None:
    """Drop ledger entries (all, or just one stage prefix)."""
    if not prefix:
        LEDGER.clear()
        return
    keep = [r for r in LEDGER if not r["stage"].startswith(prefix)]
    LEDGER.clear()
    LEDGER.extend(keep)

# Scratch working directory the CLI is confined to. No call site in this app
# needs filesystem/bash access — this just bounds the blast radius in case a
# builtin tool is ever invoked despite disallowed_tools below.
_SCRATCH_DIR = Path(__file__).parent / ".llm_scratch"
_SCRATCH_DIR.mkdir(exist_ok=True)

# Claude Code's own builtin tools — irrelevant to plain content generation /
# custom-tool agents, and disabled so a call can never touch the local
# filesystem or network beyond the tools we explicitly register.
_NO_BUILTIN_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]

# Turns allowed for a plain completion. Not a licence to loop — tools are disabled, so
# the only thing an extra turn can do is finish an answer that ran long.
COMPLETE_MAX_TURNS = max(1, int(os.environ.get("PROJECTZEN_COMPLETE_TURNS") or 4))


async def complete(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
) -> str:
    """
    One-shot text completion — no tools, no filesystem access.

    If on_chunk is given, streams token deltas through it as they arrive
    (for SSE "typewriter" progress) and returns the same text accumulated
    from those deltas. Without on_chunk, returns the final assistant message
    text in one piece.
    """
    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model,
        fallback_model=FALLBACK_MODEL,
        cwd=str(_SCRATCH_DIR),
        permission_mode="bypassPermissions",
        disallowed_tools=_NO_BUILTIN_TOOLS,
        # More than one turn, because a single turn is not always enough to FINISH one
        # completion. Every builtin tool is disabled here, so additional turns cannot
        # do anything except continue the answer — there is no agentic loop to run away
        # with. At 1, a long plan for a large input aborted outright with "Reached
        # maximum number of turns (1)" after seven minutes of work.
        max_turns=COMPLETE_MAX_TURNS,
        include_partial_messages=on_chunk is not None,
    )

    text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            _check_assistant(message)          # refusal -> LlmUnavailable
        if isinstance(message, ResultMessage):
            _record(message, "complete")
            _check_result(message, text)
        elif on_chunk is not None and isinstance(message, StreamEvent):
            event = message.event or {}
            if event.get("type") != "content_block_delta":
                continue
            delta = event.get("delta") or {}
            if delta.get("type") != "text_delta":
                continue  # skip thinking_delta / signature_delta — not response content
            chunk = delta.get("text") or ""
            if chunk:
                text += chunk
                await on_chunk(chunk)
        elif isinstance(message, AssistantMessage) and on_chunk is None:
            for block in message.content:
                if isinstance(block, TextBlock):
                    text += block.text
    return text


class Conversation:
    """
    A persistent multi-turn session — the antidote to per-call subprocess spawning.

    Every `complete()` call runs `query()`, which spawns a fresh `claude` CLI
    subprocess: Node boot, CLI init, credential read, tool setup, session create.
    Measured on this machine that fixed cost is ~28s per call, for a one-token reply.
    A document pipeline making 6-8 calls therefore burns 3-5 minutes before any
    reasoning happens, and re-sends the same grounding + source context every time.

    Measured with a 30,000-char context block:

        3 separate query() calls .......... ~84s
        3 turns in one Conversation ....... 31.8s   (22.5s connect + 4.4 + 1.5 + 2.8)

    The spawn is paid once, and later turns do not re-send what is already in the
    conversation history — turn 2 answered in 1.5s with 30K chars already in context.

    Use it for stages that genuinely follow one another (research -> plan -> generate).
    Do NOT use one Conversation for the reviewer panel: reviewers are meant to judge
    independently, and sharing a session would let each one see the previous
    reviewers' findings.
    """

    __slots__ = ("_client", "_options", "_model", "_session_id")

    def __init__(
        self,
        model: Optional[str] = None,
        system: Optional[str] = None,
        effort: Optional[str] = None,
        max_turns: int = 32,
        resume: Optional[str] = None,
        fork: bool = False,
    ) -> None:
        opts: Dict[str, Any] = dict(
            system_prompt=system,
            model=model,
            fallback_model=FALLBACK_MODEL,
            cwd=str(_SCRATCH_DIR),
            permission_mode="bypassPermissions",
            disallowed_tools=_NO_BUILTIN_TOOLS,
            max_turns=max_turns,
            include_partial_messages=True,
        )
        if effort:
            opts["effort"] = effort
        if resume:
            # Branch from an existing session instead of starting cold.
            opts["resume"] = resume
            opts["fork_session"] = bool(fork)
        self._options = ClaudeAgentOptions(**opts)
        self._client: Optional[ClaudeSDKClient] = None
        self._model = model
        self._session_id: Optional[str] = None

    @property
    def session_id(self) -> Optional[str]:
        """Set once the first turn completes. Pass to `fork_from` to branch."""
        return self._session_id

    @property
    def model(self) -> Optional[str]:
        """The model this session last ran on. A fork MUST reuse it — see fork_from."""
        return self._model

    async def __aenter__(self) -> "Conversation":
        self._client = ClaudeSDKClient(options=self._options)
        await self._client.connect()
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

    async def turn(
        self,
        prompt: str,
        model: Optional[str] = None,
        on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> str:
        """
        One turn in the ongoing conversation. Returns the assistant's text.

        `model` switches the model for this turn onward. Note that prompt caching is
        per-model, so switching mid-conversation forfeits cache reuse from the
        previous model's turns — group same-model stages together.
        """
        if self._client is None:
            raise RuntimeError("Conversation used outside its async context manager")

        if model and model != self._model:
            await self._client.set_model(model)
            self._model = model

        await self._client.query(prompt)

        text = ""
        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                _check_assistant(message)      # refusal -> LlmUnavailable
            if isinstance(message, ResultMessage):
                self._session_id = message.session_id or self._session_id
                _record(message, "turn")
                _check_result(message, text)
            elif on_chunk is not None and isinstance(message, StreamEvent):
                event = message.event or {}
                if event.get("type") != "content_block_delta":
                    continue
                delta = event.get("delta") or {}
                if delta.get("type") != "text_delta":
                    continue          # skip thinking/signature deltas
                chunk = delta.get("text") or ""
                if chunk:
                    text += chunk
                    await on_chunk(chunk)
            elif isinstance(message, AssistantMessage) and on_chunk is None:
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text += block.text
        return text


# The builtin tools an authoring run needs: inspect the reference workbook, write a
# build script, run it, check the result. This is the ONLY call path in this app that
# grants the model filesystem and shell access, and it is confined to a per-run
# sandbox directory (see author_file's `workdir`).
_AUTHOR_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]


def _describe_step(tool_name: str, tool_input: Any) -> str:
    """
    A short, human phrase for one authoring tool call — what a person watching would
    want to read. Tool names like "Write" mean nothing to a delivery consultant;
    "writing the build script" does.
    """
    inp = tool_input if isinstance(tool_input, dict) else {}
    name = os.path.basename(str(inp.get("file_path") or "")) or ""
    cmd = str(inp.get("command") or "")

    if tool_name == "Write":
        return f"writing {name}" if name else "writing the build script"
    if tool_name == "Edit":
        return f"refining {name}" if name else "refining the build script"
    if tool_name == "Read":
        return f"reading {name}" if name else "reading the content contract"
    if tool_name in ("Bash", "PowerShell"):
        low = cmd.lower()
        if "build" in low:
            return "running the build script"
        if any(k in low for k in ("openpyxl", "load_workbook", "verify", "check")):
            return "verifying the workbook"
        return "running a command"
    if tool_name in ("Glob", "Grep"):
        return "locating files"
    return tool_name.lower()


async def author_file(
    brief: str,
    workdir: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_turns: int = 24,
    max_budget_usd: Optional[float] = None,
    on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
    on_step: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> str:
    """
    Let Claude BUILD a real file instead of describing one as JSON.

    Why this exists
    ---------------
    templates.py renders a JSON plan. That plan has no field for a formula, a
    conditional format, a column width, a merged header band or a per-sheet colour —
    so no matter how good the model's reasoning is, those cannot survive the trip.
    Measured on the same deliverable: Claude Desktop produced 32 colours / 1,087
    formulas / 13 sheets; this app's JSON path produced 2 colours / 0 formulas.
    The difference is not model quality, it is the schema bottleneck.

    Here the model gets Write + Bash inside `workdir` and authors the file with
    openpyxl (or python-docx / python-pptx) directly, the same way it does in Desktop.

    Sandboxing
    ----------
    `cwd` is `workdir`, which the caller creates per run and populates with only the
    reference file. `setting_sources` is left unset so the repo's CLAUDE.md and
    project settings are NOT loaded into this agent.

    Returns the final assistant text. The CALLER is responsible for checking that the
    expected output file exists and passes a validation gate — a transcript that
    claims success proves nothing.
    """
    opts: Dict[str, Any] = dict(
        system_prompt=system,
        model=model,
        fallback_model=FALLBACK_MODEL,
        cwd=str(workdir),
        permission_mode="bypassPermissions",
        allowed_tools=_AUTHOR_TOOLS,
        max_turns=max_turns,
        include_partial_messages=on_chunk is not None,
    )
    # A spend ceiling is a better bound than a clock. The same 600s runaway costs
    # wildly different amounts on a fast org versus a slow one, so wall-clock alone
    # does not actually bound what an authoring attempt can consume.
    if max_budget_usd:
        opts["max_budget_usd"] = float(max_budget_usd)
    options = ClaudeAgentOptions(**opts)

    text = ""
    step_no = 0
    async for message in query(prompt=brief, options=options):
        # Heartbeat. An authoring run is the longest stage in the pipeline — 10 to 30
        # minutes — and previously reported nothing between start and finish, so the
        # UI could not distinguish "building your workbook" from "hung". These tool
        # calls were already being iterated here and discarded; surfacing them costs
        # nothing and changes nothing about what gets built.
        if on_step is not None and isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    step_no += 1
                    try:
                        await on_step({
                            "step": step_no,
                            "tool": block.name,
                            "detail": _describe_step(block.name, block.input),
                        })
                    except Exception:
                        pass          # telemetry must never break an authoring run

        if isinstance(message, AssistantMessage):
            _check_assistant(message)
        if isinstance(message, ResultMessage):
            _record(message, "author")
            _check_result(message, text)
        elif on_chunk is not None and isinstance(message, StreamEvent):
            event = message.event or {}
            if event.get("type") != "content_block_delta":
                continue
            delta = event.get("delta") or {}
            if delta.get("type") != "text_delta":
                continue
            chunk = delta.get("text") or ""
            if chunk:
                text += chunk
                await on_chunk(chunk)
        elif isinstance(message, AssistantMessage) and on_chunk is None:
            for block in message.content:
                if isinstance(block, TextBlock):
                    text += block.text
    return text


async def preflight(timeout_s: float = 90.0) -> Dict[str, Any]:
    """
    Can this process actually talk to Claude? Returns a diagnosis, never raises.

    Worth having because every failure mode here looks the same from the outside —
    documents just fail — while the causes are completely different and only one of
    them is a bug:

      * `claude` CLI not installed (needs Node)  -> the container case
      * installed but not logged in              -> user must run `claude` once
      * logged in but the model is unavailable   -> Enterprise plan lacks it

    Exposed via GET /health/llm so a user (or a deployment) gets a straight answer
    instead of inferring it from a failed generation.
    """
    import shutil as _shutil

    # The SDK SHIPS its own claude executable and prefers it over PATH, so probing
    # PATH alone reports "not installed" on a perfectly healthy pip-only machine —
    # which is exactly what an installed copy looks like. Check the bundled binary
    # first, the same order _find_cli() uses.
    bundled = ""
    try:
        import claude_agent_sdk as _sdk
        for _name in ("claude.exe", "claude"):
            _p = Path(_sdk.__file__).parent / "_bundled" / _name
            if _p.exists():
                bundled = str(_p)
                break
    except Exception:
        pass

    on_path = _shutil.which("claude") or ""

    info: Dict[str, Any] = {
        "ok": False,
        "cli_found": bool(bundled or on_path),
        "cli_source": "bundled with claude-agent-sdk" if bundled
                      else ("PATH" if on_path else "not found"),
        "cli_path": bundled or on_path,
        "node_found": bool(_shutil.which("node")),   # informational: not required
        "api_key_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "fallback_model": FALLBACK_MODEL,
        "detail": "",
    }
    if not info["cli_found"]:
        info["detail"] = (
            "No Claude CLI available. ProjectZen authenticates through the CLI using "
            "your Claude Enterprise seat — there is no API-key path. Normally the CLI "
            "ships inside claude-agent-sdk, so this usually means the dependency is "
            "missing or was installed without its bundled binary. Reinstall with "
            "`pip install --force-reinstall claude-agent-sdk`."
        )
        return info

    try:
        import asyncio as _asyncio
        text = await _asyncio.wait_for(
            complete("Reply with exactly: PONG", model=FALLBACK_MODEL),
            timeout=timeout_s,
        )
        info["ok"] = "PONG" in (text or "").upper()
        info["detail"] = "Reachable." if info["ok"] else f"Unexpected reply: {text[:80]!r}"
    except Exception as exc:                       # noqa: BLE001 - diagnosis, not control flow
        info["detail"] = (
            f"CLI present but the call failed ({type(exc).__name__}: {str(exc)[:120]}). "
            "Most often this means you are not signed in — run `claude` once in a "
            "terminal and complete the login."
        )
    return info


def fork_from(
    session_id: str,
    model: Optional[str] = None,
    system: Optional[str] = None,
    max_turns: int = 8,
) -> "Conversation":
    """
    A Conversation branching from an existing session's state.

    This is what lets the reviewers be cheap WITHOUT being compromised. A reviewer
    needs the draft, the source and the grounding in context — around 30K tokens
    that the generation session has already paid for. Re-sending it as an
    independent call re-writes the whole thing cold (measured: 13,849 tokens of
    cache_create per reviewer, for context that was already cached moments before).

    Forking gives the branch that context as a cache READ, while its history starts
    clean — so sibling reviewers cannot see each other's findings and stay genuinely
    independent. Measured on a 29,679-token base session:

        fork A .... cache_create 25, cache_read 29,679   (saw base context)
        fork B .... cache_create  0, cache_read 29,704   (blind to fork A's turn)

    The base session does NOT need to be open: sessions persist, so the generation
    conversation can close normally and still be forked from afterwards.

    CRITICAL — `model` must match the model the base session ran on. Prompt caching is
    per-model, so a fork that switches model re-processes the entire inherited history
    cold and ends up costing MORE than an independent call. Measured on the same base:

        fork inheriting opus ..... cache_create     19, cache_read 29,967
        fork switching to sonnet . cache_create 20,412, cache_read  4,712

    Pass the base Conversation's `.model`, not a cheaper one.
    """
    return Conversation(model=model, system=system, max_turns=max_turns,
                        resume=session_id, fork=True)


class ToolSpec:
    """One tool `run_tool_agent` can offer Claude, backed by an existing
    async handler(args_dict) -> str (the same signature every _execute_tool
    in this codebase already has)."""

    __slots__ = ("name", "description", "input_schema", "handler")

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable[[Dict[str, Any]], Awaitable[str]],
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler


async def run_tool_agent(
    prompt: str,
    tools: List[ToolSpec],
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_turns: int = 8,
) -> str:
    """
    Runs a real tool-use agent loop: Claude decides which of `tools` to call
    and in what order, over the local enterprise-seat session. Returns the
    final assistant text answer.
    """

    def _wrap(spec: ToolSpec):
        async def _run(args: Dict[str, Any]) -> Dict[str, Any]:
            result = await spec.handler(args)
            return {"content": [{"type": "text", "text": str(result)}]}
        return tool(spec.name, spec.description, spec.input_schema)(_run)

    wrapped_tools = [_wrap(spec) for spec in tools]
    server = create_sdk_mcp_server(name="app_tools", tools=wrapped_tools)
    allowed = [f"mcp__app_tools__{spec.name}" for spec in tools]

    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model,
        fallback_model=FALLBACK_MODEL,
        cwd=str(_SCRATCH_DIR),
        permission_mode="bypassPermissions",
        disallowed_tools=_NO_BUILTIN_TOOLS,
        mcp_servers={"app_tools": server},
        allowed_tools=allowed,
        max_turns=max_turns,
    )

    final_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            _record(message, "tool_agent")
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    final_text = block.text
    return final_text
