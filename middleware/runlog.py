"""
runlog.py — one durable JSONL trace per generation run.

WHY THIS EXISTS
---------------
When a user reports "it failed", the only evidence today is a red badge in the UI.
The numbers that would explain it already exist — llm_client.LEDGER records stage,
tokens, cost, duration and an error flag for every single LLM call — but three
things make that ledger useless for support:

  1. It is a module-level list. A restart erases it, and a restart is exactly what
     a user does after a failure. The run you need is the one you can no longer see.
  2. It carries no identity or clock: no run, no document, no timestamp. You cannot
     tell which document a call belonged to or reconstruct the order of events.
  3. `is_error` is a bare boolean. The exception, the refusal, the LlmUnavailable
     detail — the single field that answers "why" — is thrown away.

This module fixes all three by persisting an append-only event stream to disk as it
happens, so the trace survives the crash that produced it.

DESIGN NOTES
------------
JSONL FILE PER RUN, NOT ROWS IN documents.db. Three reasons:

  * A 4-wide cascade wave writes concurrently; line appends to a private file do
    not contend with the application's own SQLite connection.
  * A PARTIAL FILE IS STILL EVIDENCE. If the process dies mid-run, every line
    written so far is on disk. A transaction that never committed gives you nothing,
    and the crash case is precisely the one worth capturing.
  * The file is already the artifact the user downloads. No export step.

EVERY WRITE IS FLUSHED. The volume is low — a 50-minute cascade produces a few
hundred events, not a few hundred thousand — so buying crash-durability with an
fflush per line is the right trade here.

TELEMETRY MUST NEVER FAIL A GENERATION. Every public function swallows its own
exceptions, exactly as llm_client._record already does. A logging bug must not be
able to turn a working run into a failed one; the worst outcome allowed here is a
thinner log.

CONTEXTVARS, NOT GLOBALS. A cascade wave generates up to 4 documents concurrently
on one event loop. A global "current node" would cross-attribute between them and
produce numbers worse than none. ContextVar copies per asyncio task, so each
document's calls are attributed correctly — the same reasoning, and the same
mechanism, as llm_client._stage_label.
"""

import contextlib
import contextvars
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List

from paths import RUNLOG_DIR

# Retention. Surfaced in the UI so users know their logs are not kept forever.
MAX_RUNS = 50
MAX_AGE_DAYS = 30

# ── Ambient run/node identity ────────────────────────────────────────────────
_run_id:  contextvars.ContextVar[str] = contextvars.ContextVar("pz_run", default="")
_node_id: contextvars.ContextVar[str] = contextvars.ContextVar("pz_node", default="")

# run_id -> open append handle. Guarded because a run may be started from one task
# and written to from several; dict mutation itself is atomic under the GIL, but the
# open/close pairing is not.
_handles: Dict[str, Any] = {}
_lock = threading.Lock()

# Runs whose handle has been closed. Bounded — see finish_run.
_closed: set = set()

# run_id -> monotonic start, for durations that a clock change cannot distort.
_started: Dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _path_for(run_id: str) -> Path:
    return RUNLOG_DIR / f"{run_id}.jsonl"


def new_run_id(kind: str) -> str:
    """
    Sortable, collision-proof, filename-safe.

    Leading UTC timestamp means `sorted(listdir())` IS chronological order — an
    invariant both the history list ("newest first") and the retention sweep ("drop
    the oldest") depend on.

    MILLISECONDS ARE LOAD-BEARING, not decoration. With second precision, two runs
    started in the same second fall through to the next token — the kind — so an
    "adhoc" run sorted before a "cascade" one regardless of which came first. That
    showed history out of order and, worse, could make prune() delete the newer of
    the two. Sub-second precision keeps the tie-break inside the clock.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return f"{stamp}-{kind}-{uuid.uuid4().hex[:6]}"


# ── Writing ──────────────────────────────────────────────────────────────────
def _write(run_id: str, payload: Dict[str, Any]) -> None:
    """
    Append one event. Silent on every failure — see module docstring.

    A CLOSED RUN IS WRITTEN WITHOUT CACHING THE HANDLE. Background work can outlive
    the run that started it — the account probe is exactly that — and if a late event
    reopened the cached handle it would sit open forever, leaking a descriptor and
    pinning the file against retention. Open, append, close: the event still lands.
    """
    if not run_id:
        return
    try:
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        with _lock:
            fh = _handles.get(run_id)
            if fh is not None:
                fh.write(line)
                fh.flush()
                return
            closed = run_id in _closed
        RUNLOG_DIR.mkdir(parents=True, exist_ok=True)
        if closed:
            with open(_path_for(run_id), "a", encoding="utf-8") as late:
                late.write(line)
            return
        with _lock:
            fh = _handles.get(run_id)
            if fh is None:
                fh = open(_path_for(run_id), "a", encoding="utf-8")
                _handles[run_id] = fh
            fh.write(line)
            fh.flush()
    except Exception:
        pass


def emit(t: str, **fields: Any) -> None:
    """
    Record one event against the ambient run.

    `t` is the event type; the node and stage are filled in from context so callers
    never have to thread them through. Used directly for the things that have no
    lifecycle of their own — grounding resolution, reviewer decisions, validation
    outcomes — and indirectly by every helper below.
    """
    rid = _run_id.get()
    if not rid:
        return
    ev: Dict[str, Any] = {"t": t, "ts": _now_iso(), "run_id": rid}
    node = _node_id.get()
    if node:
        ev["node_id"] = node
    ev.update(fields)
    _write(rid, ev)


# ── Run lifecycle ────────────────────────────────────────────────────────────
def start_run(kind: str, **meta: Any) -> str:
    """
    Open a trace and make it ambient for this context. Returns the run id.

    `kind` is "cascade" or "adhoc" — both generation paths log, because "users are
    failing to generate" does not tell you which one they used.
    """
    rid = ""
    try:
        rid = new_run_id(kind)
        _run_id.set(rid)
        _started[rid] = time.monotonic()
        _write(rid, {
            "t": "run_start",
            "ts": _now_iso(),
            "run_id": rid,
            "kind": kind,
            "schema": 1,          # so a later reader can tell old traces from new
            "meta": meta,
        })
    except Exception:
        pass
    return rid


def finish_run(status: str = "ok", **extra: Any) -> None:
    """Close the trace. Safe to call twice; safe to call with no run open."""
    rid = _run_id.get()
    if not rid:
        return
    try:
        started = _started.pop(rid, None)
        _write(rid, {
            "t": "run_end",
            "ts": _now_iso(),
            "run_id": rid,
            "status": status,
            "duration_ms": int((time.monotonic() - started) * 1000) if started else None,
            **extra,
        })
    except Exception:
        pass
    finally:
        try:
            with _lock:
                fh = _handles.pop(rid, None)
                _closed.add(rid)
                if len(_closed) > 200:          # long-lived server: keep it bounded
                    for old in list(_closed)[:100]:
                        _closed.discard(old)
            if fh:
                fh.close()
        except Exception:
            pass
        try:
            prune()
        except Exception:
            pass


@contextlib.contextmanager
def run_scope(kind: str, **meta: Any) -> Iterator[str]:
    """
    `with run_scope("cascade", input="SOW.docx") as run_id:` — the trace closes on the
    way out whether the body succeeded or raised, and a raise is recorded as the run
    status rather than being swallowed.
    """
    rid = start_run(kind, **meta)
    try:
        yield rid
    except BaseException as exc:
        emit("run_error", error=_describe(exc))
        finish_run("failed", error=_describe(exc))
        raise
    else:
        finish_run("ok")


# ── Node (document) lifecycle ────────────────────────────────────────────────
def start_node(node_id: str, **meta: Any) -> None:
    _node_id.set(node_id)
    emit("node_start", **meta)


def finish_node(status: str = "ok", **extra: Any) -> None:
    emit("node_end", status=status, **extra)
    _node_id.set("")


@contextlib.contextmanager
def node_scope(node_id: str, **meta: Any) -> Iterator[None]:
    """
    Wrap one document's work.

    MUST be entered INSIDE the task that generates the document, not around the
    asyncio.gather that launches the wave — the ContextVar is copied per task, and
    entering it outside would attribute all four documents in a wave to the last one
    to set it.
    """
    started = time.monotonic()
    token = _node_id.set(node_id)
    try:
        emit("node_start", **meta)
        yield
    except BaseException as exc:
        emit("node_end", status="failed",
             duration_ms=int((time.monotonic() - started) * 1000),
             error=_describe(exc))
        raise
    else:
        emit("node_end", status="ok",
             duration_ms=int((time.monotonic() - started) * 1000))
    finally:
        _node_id.reset(token)


@contextlib.contextmanager
def stage_scope(stage: str, **meta: Any) -> Iterator[None]:
    """
    Time one stage of one document — research, plan, generate, review, rework.

    This is what turns "the run took 50 minutes" into "the run took 50 minutes and
    41 of them were the generate stage of three documents".
    """
    started = time.monotonic()
    emit("stage_start", stage=stage, **meta)
    try:
        yield
    except BaseException as exc:
        emit("stage_end", stage=stage, status="failed",
             duration_ms=int((time.monotonic() - started) * 1000),
             error=_describe(exc))
        raise
    else:
        emit("stage_end", stage=stage, status="ok",
             duration_ms=int((time.monotonic() - started) * 1000))


# ── LLM calls ────────────────────────────────────────────────────────────────
def record_llm_call(row: Dict[str, Any]) -> None:
    """
    Persist one ledger row. Called from llm_client._record, which already builds the
    dict — this adds the run, the node and the wall clock the in-memory ledger lacks.

    `billable_input` is precomputed here rather than left to the reader: cache WRITES
    bill at a premium and cache READS at a large discount, so raw input_tokens alone
    says almost nothing about what a call actually cost.
    """
    try:
        # Merged into a dict, NOT passed as `billable_input=..., **row`. If the caller
        # ever supplied that key itself, the keyword form raised "got multiple values
        # for keyword argument" and the except below swallowed it — the call vanished
        # from the trace with no error anywhere. A logging path that silently discards
        # the very events it exists to record is the worst possible failure, so the
        # shape that cannot collide is the one to use.
        payload = dict(row)
        payload["billable_input"] = (int(row.get("input") or 0)
                                     + int(row.get("cache_create") or 0))
        emit("llm_call", **payload)
    except Exception:
        pass


# ── Failure description ──────────────────────────────────────────────────────
def _describe(exc: BaseException) -> Dict[str, Any]:
    """
    Turn an exception into the fields a support investigation actually needs.

    LlmUnavailable carries reason/detail/is_retryable — the difference between "rate
    limited, try again" and "this seat cannot use this model" — and losing that
    distinction is how a rate limit gets mistaken for a broken install.
    """
    import traceback

    out: Dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc)[:600],
    }
    for attr in ("reason", "detail", "is_retryable"):
        val = getattr(exc, attr, None)
        if val is not None:
            out[attr] = val if isinstance(val, bool) else str(val)[:400]
    try:
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        out["traceback"] = "".join(tb)[-2500:]      # tail: the frames that matter
    except Exception:
        pass
    return out


def record_error(exc: BaseException, where: str = "") -> None:
    """Log a failure without owning the control flow — the caller still raises."""
    emit("error", where=where, error=_describe(exc))


# ── Retention ────────────────────────────────────────────────────────────────
def prune(max_runs: int = MAX_RUNS, max_age_days: int = MAX_AGE_DAYS) -> int:
    """
    Enforce the retention policy stated in the UI: newest `max_runs`, nothing older
    than `max_age_days`. Returns how many traces were removed.

    Runs on every finish_run. Cheap — a directory listing and at most a few unlinks —
    and it keeps 45-minute runs from filling a laptop over a few months.
    """
    removed = 0
    try:
        if not RUNLOG_DIR.exists():
            return 0
        files = sorted(RUNLOG_DIR.glob("*.jsonl"))       # names sort chronologically
        open_now = set(_handles.keys())

        stale: List[Path] = []
        cutoff = time.time() - max_age_days * 86400
        for p in files:
            if p.stem in open_now:
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    stale.append(p)
            except Exception:
                pass

        survivors = [p for p in files if p not in stale and p.stem not in open_now]
        if len(survivors) > max_runs:
            stale.extend(survivors[: len(survivors) - max_runs])

        for p in stale:
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed


# ── Read side (used by the routes in a later section) ────────────────────────
def current_run_id() -> str:
    """The ambient run, or "" outside one."""
    return _run_id.get()


def list_runs() -> List[Dict[str, Any]]:
    """
    Index of stored traces, newest first, built from each file's first and last line.

    Reading two lines beats parsing whole files: a header and a footer are all the
    history list renders, and this stays fast with 50 traces of a few hundred events.
    """
    out: List[Dict[str, Any]] = []
    try:
        for p in sorted(RUNLOG_DIR.glob("*.jsonl"), reverse=True):
            try:
                head, tail = _head_and_tail(p)
                row: Dict[str, Any] = {
                    "run_id": p.stem,
                    "bytes": p.stat().st_size,
                    "started": (head or {}).get("ts"),
                    "kind": (head or {}).get("kind"),
                    "meta": (head or {}).get("meta") or {},
                    "status": "running",
                    "ended": None,
                    "duration_ms": None,
                }
                if tail and tail.get("t") == "run_end":
                    row["status"] = tail.get("status") or "ok"
                    row["ended"] = tail.get("ts")
                    row["duration_ms"] = tail.get("duration_ms")
                elif p.stem not in _handles:
                    # No run_end and nobody is writing: the process died mid-run.
                    # Saying so is the point of the feature.
                    row["status"] = "interrupted"
                out.append(row)
            except Exception:
                continue
    except Exception:
        pass
    return out


def read_run(run_id: str) -> List[Dict[str, Any]]:
    """Every event of one trace, in order. Bad lines are skipped, not fatal."""
    events: List[Dict[str, Any]] = []
    try:
        p = _path_for(run_id)
        if not p.exists():
            return events
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return events


def _head_and_tail(p: Path):
    """First and last parseable JSON line, without reading the middle of the file."""
    head = tail = None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            first = fh.readline().strip()
            if first:
                try:
                    head = json.loads(first)
                except Exception:
                    head = None
        size = p.stat().st_size
        with open(p, "rb") as fb:
            fb.seek(max(0, size - 8192))
            chunk = fb.read().decode("utf-8", "replace")
        for line in reversed([l for l in chunk.splitlines() if l.strip()]):
            try:
                tail = json.loads(line)
                break
            except Exception:
                continue
    except Exception:
        pass
    return head, tail


# ── Environment snapshot ─────────────────────────────────────────────────────
# Taken once per run. Answers the questions a support investigation asks first and
# that no amount of per-call telemetry can reconstruct afterwards: which build, which
# Python, which Claude CLI, and — the one that cost three days on the SSO ticket —
# WHICH ACCOUNT, on what plan, signed in which way.

# The account probe shells out to the Claude CLI, which is a Node binary: MEASURED AT
# ~6 SECONDS of process startup on a warm machine. Against a 50-minute cascade that is
# noise, but an adhoc generation can finish in ten seconds and it would dominate.
#
# So it is never on the critical path. The result is cached process-wide, warmed at
# server startup (see warm_account), and any run that starts before the warm-up
# finishes emits its account as a separate event moments later instead of waiting.
_ACCOUNT_CACHE: Dict[str, Any] = {}
_ACCOUNT_TTL_S = 900.0

# find_cli.find() imports claude_agent_sdk, which costs ~1.4s the first time it runs
# in a process. The server has already imported it via llm_client long before any run
# starts, so this is normally free — but caching removes the cliff entirely rather
# than depending on import order staying the way it is today. The path cannot change
# while the process lives.
_CLI_PATH: Dict[str, str] = {}


def _cli_path() -> str:
    if "p" not in _CLI_PATH:
        try:
            from find_cli import find
            _CLI_PATH["p"] = find() or ""
        except Exception:
            _CLI_PATH["p"] = ""
    return _CLI_PATH["p"]


def _claude_account(force: bool = False) -> Dict[str, Any]:
    """
    `claude auth status --json` -> email / org / plan / auth method.

    Deliberately identifying. You asked for name and email because a trace you cannot
    attribute to a person is one you cannot follow up on — and the account IS the
    failure in a whole class of cases: wrong login flow, seat without Claude Code,
    expired session, usage limit. The SSO ticket would have been one line of this.
    """
    import subprocess

    cached = _ACCOUNT_CACHE.get("data")
    if cached and not force and (time.monotonic() - _ACCOUNT_CACHE.get("at", 0)) < _ACCOUNT_TTL_S:
        return cached

    out: Dict[str, Any] = {}
    try:
        cli = _cli_path()
        if not cli:
            out = {"error": "no CLI found"}
        else:
            proc = subprocess.run([cli, "auth", "status", "--json"],
                                  capture_output=True, text=True, timeout=30)
            data = json.loads((proc.stdout or "").strip() or "{}")
            for k in ("loggedIn", "authMethod", "apiProvider", "email",
                      "orgName", "orgId", "subscriptionType"):
                if k in data:
                    out[k] = data[k]
    except Exception as exc:
        out = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    _ACCOUNT_CACHE["data"] = out
    _ACCOUNT_CACHE["at"] = time.monotonic()
    return out


def account_cached() -> Dict[str, Any]:
    """The cached account, or {} if the warm-up has not landed yet. Never blocks."""
    cached = _ACCOUNT_CACHE.get("data")
    if cached and (time.monotonic() - _ACCOUNT_CACHE.get("at", 0)) < _ACCOUNT_TTL_S:
        return cached
    return {}


async def warm_account() -> None:
    """
    Populate the account cache off the critical path.

    Called once at server startup, so by the time a user generates anything the ~6s
    CLI probe is already paid and every run gets the account for free.
    """
    import asyncio
    try:
        await asyncio.to_thread(_claude_account, True)
    except Exception:
        pass


def _sync_env_snapshot() -> Dict[str, Any]:
    """Blocking half of env_snapshot(); run off the event loop."""
    import getpass
    import platform
    import shutil as _sh

    snap: Dict[str, Any] = {"retention": {"max_runs": MAX_RUNS, "max_age_days": MAX_AGE_DAYS}}
    try:
        snap["os"] = f"{platform.system()} {platform.release()} ({platform.version()})"
        snap["python"] = platform.python_version()
        snap["machine"] = platform.machine()
        snap["hostname"] = platform.node()
    except Exception:
        pass
    try:
        snap["windows_user"] = getpass.getuser()
    except Exception:
        pass
    try:
        from paths import DATA_DIR, STORAGE
        snap["data_dir"] = str(DATA_DIR)
        total, used, free = _sh.disk_usage(str(STORAGE))
        snap["disk_free_gb"] = round(free / 1024 ** 3, 1)
    except Exception:
        pass
    try:
        cli = _cli_path()
        snap["claude_cli"] = cli or "not found"
        snap["claude_cli_source"] = ("bundled with claude-agent-sdk"
                                     if "_bundled" in (cli or "") else
                                     ("PATH" if cli else "not found"))
    except Exception:
        pass
    try:
        import os as _os
        # NOT the value — only whether one is present. An API key in the environment
        # changes which credential the SDK uses and has caused "works for me" reports
        # that nothing else explains.
        snap["api_key_env_present"] = bool(_os.environ.get("ANTHROPIC_API_KEY"))
        snap["env_flags"] = {k: v for k, v in _os.environ.items()
                             if k.startswith("PROJECTZEN_")}
    except Exception:
        pass
    try:
        from llm_client import FALLBACK_MODEL
        snap["fallback_model"] = FALLBACK_MODEL
    except Exception:
        pass
    # Cached only — never the blocking probe. See the note above _ACCOUNT_CACHE.
    snap["claude_account"] = account_cached()
    return snap


async def env_snapshot() -> Dict[str, Any]:
    """Everything except the account probe. Local calls only; microseconds."""
    try:
        return _sync_env_snapshot()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}


async def emit_env() -> None:
    """
    Record the environment against the ambient run, without ever making the run wait.

    If the account cache is cold (server only just started), the account is fetched in
    the background and lands as its own `account` event a few seconds later. Event
    ORDER in the file carries no meaning — readers look events up by type — so a late
    arrival costs nothing, whereas a 6-second stall at the head of every generation
    would have been paid by every user on every run.
    """
    import asyncio
    try:
        emit("env", env=await env_snapshot())
        if not account_cached():
            async def _late() -> None:
                try:
                    acct = await asyncio.to_thread(_claude_account, True)
                    emit("account", claude_account=acct)
                except Exception:
                    pass
            # create_task copies the current context, so the ambient run id travels
            # with it and the event is attributed to the right run.
            asyncio.create_task(_late())
    except Exception:
        pass


def valid_run_id(run_id: str) -> bool:
    """
    Gate for anything that turns a client-supplied id into a path.

    The download route hands this straight to the filesystem, so it is restricted to
    the shape new_run_id() produces — no separators, no dots, no traversal.
    """
    if not run_id or len(run_id) > 80:
        return False
    return all(c.isalnum() or c in "-_" for c in run_id)
