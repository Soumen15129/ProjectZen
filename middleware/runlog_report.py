"""
runlog_report.py — turn a raw event trace into something a person can read.

runlog.py captures; this module interprets. Kept separate so the capture path stays
small and dependency-free: nothing here ever runs during a generation.

Two consumers, one summariser:
  * the Logs tab, which renders `summarise()` as the run detail view
  * the exported .md report, which renders the same structure as text

Sharing `summarise()` is deliberate. The number the user reads in the UI and the
number you read in the exported report come from the same code, so a support
conversation can never turn into an argument about which one is right.

THE EXPORT WORKS ON A RUNNING TRACE. Every event is flushed as it happens and each
line is written whole, so reading a file mid-run yields a complete prefix and never a
torn record (measured: 5,029 events read while a writer was appending, zero torn
lines). That is what makes "export now and tell me where it is stuck" possible.
"""

import io
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import runlog


# ── Desktop resolution ───────────────────────────────────────────────────────
def desktop_dir() -> Path:
    """
    The user's real Desktop.

    NOT `Path.home() / "Desktop"`. Under enterprise folder redirection — which is the
    normal case here — Desktop lives inside OneDrive and `%USERPROFILE%\\Desktop` does
    not exist at all, so the naive path silently writes somewhere nobody looks, or
    fails. The shell-folder registry value is the only reliable source on Windows.
    """
    if sys.platform == "win32":
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                path = Path(winreg.QueryValueEx(k, "Desktop")[0])
                if path.exists():
                    return path
        except Exception:
            pass
    for candidate in (Path.home() / "Desktop", Path.home()):
        if candidate.exists():
            return candidate
    return Path.home()


def export_dir() -> Path:
    """Where exported zips land: a folder on the Desktop the user can actually find."""
    d = desktop_dir() / "ProjectZen Logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


# ── Analysis ─────────────────────────────────────────────────────────────────
def _ms(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def summarise(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Fold a raw trace into the shape both the UI and the report render.

    Answers, in order: what was this run, who ran it, what did each document cost,
    where did the time go, and what failed.
    """
    head = next((e for e in events if e.get("t") == "run_start"), {}) or {}
    tail = next((e for e in reversed(events) if e.get("t") == "run_end"), None)
    env  = next((e.get("env") for e in events if e.get("t") == "env"), {}) or {}

    # The account may arrive as its own late event when the cache was cold at start.
    account = env.get("claude_account") or {}
    if not account:
        late = next((e for e in reversed(events) if e.get("t") == "account"), None)
        if late:
            account = late.get("claude_account") or {}

    out: Dict[str, Any] = {
        "run_id":      head.get("run_id") or "",
        "kind":        head.get("kind") or "",
        "started":     head.get("ts"),
        "ended":       (tail or {}).get("ts"),
        "duration_ms": (tail or {}).get("duration_ms"),
        "status":      (tail or {}).get("status") or "running",
        "meta":        head.get("meta") or {},
        "env":         env,
        "account":     account,
        "events":      len(events),
    }

    # ── per document ──────────────────────────────────────────────────────────
    nodes: Dict[str, Dict[str, Any]] = {}

    def node_of(nid: str) -> Dict[str, Any]:
        return nodes.setdefault(nid, {
            "node_id": nid, "label": nid, "status": "running",
            "duration_ms": None, "model": None, "tier": None,
            "output_format": None, "grounding": None, "authored": None,
            "artifact": None, "stages": [], "calls": 0,
            "billable_input": 0, "output": 0, "cache_read": 0,
            "cost_usd": 0.0, "errors": [],
        })

    totals = {"calls": 0, "billable_input": 0, "output": 0,
              "cache_read": 0, "cache_create": 0, "cost_usd": 0.0, "llm_ms": 0}
    stage_totals: Dict[str, Dict[str, Any]] = {}
    failures: List[Dict[str, Any]] = []

    for e in events:
        t = e.get("t")
        nid = e.get("node_id") or ""

        if t == "node_meta" and nid:
            n = node_of(nid)
            n["label"] = e.get("label") or nid
            n["model"] = e.get("model")
            n["tier"] = e.get("tier")
            n["output_format"] = e.get("output_format")
            n["grounding"] = {
                "resolved": e.get("grounding_resolved"),
                "chars": e.get("grounding_chars"),
                "reference": e.get("grounding_ref"),
                "parents": e.get("parents"),
            }
        elif t == "node_end" and nid:
            n = node_of(nid)
            n["status"] = e.get("status") or "ok"
            n["duration_ms"] = _ms(e.get("duration_ms"))
            if e.get("error"):
                n["errors"].append(e["error"])
                failures.append({"node_id": nid, "where": "node", "error": e["error"]})
        elif t == "stage_end" and nid:
            node_of(nid)["stages"].append({
                "stage": e.get("stage"),
                "status": e.get("status"),
                "duration_ms": _ms(e.get("duration_ms")),
            })
        elif t == "authoring" and nid:
            n = node_of(nid)
            n["authored"] = e.get("accepted")
            n["authoring_reason"] = e.get("reason")
            n["authoring_stats"] = e.get("stats")
        elif t == "artifact" and nid:
            node_of(nid)["artifact"] = {
                "file_name": e.get("file_name"), "size_kb": e.get("size_kb"),
                "format": e.get("output_format"), "authored": e.get("authored"),
            }
        elif t == "llm_call":
            bi = int(e.get("billable_input") or 0)
            totals["calls"] += 1
            totals["billable_input"] += bi
            totals["output"] += int(e.get("output") or 0)
            totals["cache_read"] += int(e.get("cache_read") or 0)
            totals["cache_create"] += int(e.get("cache_create") or 0)
            totals["cost_usd"] += float(e.get("cost_usd") or 0.0)
            totals["llm_ms"] += _ms(e.get("duration_ms"))

            stage = e.get("stage") or "(untagged)"
            s = stage_totals.setdefault(stage, {"calls": 0, "billable_input": 0,
                                                "output": 0, "cost_usd": 0.0, "ms": 0})
            s["calls"] += 1
            s["billable_input"] += bi
            s["output"] += int(e.get("output") or 0)
            s["cost_usd"] += float(e.get("cost_usd") or 0.0)
            s["ms"] += _ms(e.get("duration_ms"))

            if nid:
                n = node_of(nid)
                n["calls"] += 1
                n["billable_input"] += bi
                n["output"] += int(e.get("output") or 0)
                n["cache_read"] += int(e.get("cache_read") or 0)
                n["cost_usd"] += float(e.get("cost_usd") or 0.0)
            if e.get("is_error"):
                failures.append({"node_id": nid, "where": f"llm/{stage}",
                                 "error": {"type": "LLM call reported an error",
                                           "message": str(e.get("subtype") or "")}})
        elif t in ("error", "run_error"):
            failures.append({"node_id": nid, "where": e.get("where") or t,
                             "error": e.get("error") or {}})

    for n in nodes.values():
        n["cost_usd"] = round(n["cost_usd"], 4)
    totals["cost_usd"] = round(totals["cost_usd"], 4)
    for s in stage_totals.values():
        s["cost_usd"] = round(s["cost_usd"], 4)

    # Cache economics: reads are ~0.1x, writes ~1.25x. A low ratio on a long run is
    # the difference between a $1 run and a $10 one.
    served = totals["cache_read"] + totals["billable_input"]
    totals["cache_hit_pct"] = round(totals["cache_read"] / served * 100, 1) if served else 0.0

    wall = _ms(out["duration_ms"])
    out["totals"] = totals
    out["nodes"] = sorted(nodes.values(), key=lambda n: -(n["duration_ms"] or 0))
    out["stages"] = sorted(
        ({"stage": k, **v} for k, v in stage_totals.items()),
        key=lambda s: -s["ms"],
    )
    out["failures"] = failures
    out["wall_ms"] = wall
    # Model time is SUMMED ACROSS CALLS, and a cascade wave runs up to four documents
    # at once — so llm_ms legitimately exceeds wall time on a concurrent run.
    # Subtracting one from the other to get "orchestration" is therefore only
    # meaningful when the run was effectively serial. Report the ratio instead and let
    # it speak for itself: ~1.0 means the run was model-bound end to end, well above
    # 1.0 means concurrency was working, well below means the time went somewhere
    # other than waiting for Claude.
    out["concurrency"] = round(totals["llm_ms"] / wall, 2) if wall else None
    out["orchestration_ms"] = (max(0, wall - totals["llm_ms"])
                               if wall and totals["llm_ms"] <= wall else None)
    requested = len(out["meta"].get("selected_nodes") or []) or len(nodes)
    succeeded = sum(1 for n in nodes.values() if n["status"] == "ok")
    failed    = sum(1 for n in nodes.values() if n["status"] == "failed")

    # An ADHOC run produces one document and emits no node_* events at all — its
    # trace is run_start / env / authoring / run_end and nothing else. Counting
    # nodes therefore reported "0 / 0 documents" on every successful adhoc
    # generation, displayed beside a green OK badge and a downloadable file. To
    # anyone reading it — a client watching a pilot, most of all — that says the
    # run produced nothing, which is the opposite of what happened.
    #
    # run_end already carries the fact (status, file_name), so use it whenever
    # there are no per-node events to count. Cascade is untouched: it emits
    # node_end per document, so `nodes` is populated and this never fires.
    if not nodes:
        requested = requested or 1
        if out["status"] == "ok":
            succeeded = 1 if (tail or {}).get("file_name") else 0
        elif out["status"] != "running":
            failed = 1

    out["counts"] = {
        "requested": requested,
        "succeeded": succeeded,
        "failed":    failed,
    }
    return out


# ── Human-readable report ────────────────────────────────────────────────────
def _dur(ms: Optional[int]) -> str:
    if not ms:
        return "-"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s // 60)}m {int(s % 60)}s"


def render_report(events: List[Dict[str, Any]]) -> str:
    """The .md a user can read without knowing anything about the internals."""
    s = summarise(events)
    L: List[str] = []
    a = L.append

    a(f"# ProjectZen run report — {s['run_id']}")
    a("")
    a(f"- **Status**: {s['status']}")
    a(f"- **Type**: {s['kind']}")
    a(f"- **Started**: {s['started']}")
    a(f"- **Duration**: {_dur(s['wall_ms'])}")
    if s["status"] == "running":
        a("- _This run was still in progress when the log was exported._")
    a("")

    acct = s.get("account") or {}
    env = s.get("env") or {}
    a("## Who and where")
    a("")
    a(f"- **Claude account**: {acct.get('email', 'unknown')} "
      f"({acct.get('subscriptionType') or acct.get('orgName') or 'plan unknown'})")
    a(f"- **Sign-in method**: {acct.get('authMethod', 'unknown')}")
    a(f"- **Windows user**: {env.get('windows_user', '?')} on {env.get('hostname', '?')}")
    a(f"- **OS / Python**: {env.get('os', '?')} / {env.get('python', '?')}")
    a(f"- **Claude CLI**: {env.get('claude_cli_source', '?')}")
    a(f"- **API key in environment**: {env.get('api_key_env_present')}")
    a(f"- **Free disk**: {env.get('disk_free_gb', '?')} GB")
    a("")

    m = s["meta"]
    a("## What was asked for")
    a("")
    for k in ("input_file", "username", "output_format", "total_docs", "mode"):
        if m.get(k) is not None:
            a(f"- **{k}**: {m[k]}")
    if m.get("selected_nodes"):
        a(f"- **documents**: {', '.join(m['selected_nodes'])}")
    a("")

    t = s["totals"]
    c = s["counts"]
    a("## Summary")
    a("")
    a(f"- Documents: {c['succeeded']} succeeded, {c['failed']} failed "
      f"of {c['requested']} requested")
    a(f"- LLM calls: {t['calls']}  |  cost ${t['cost_usd']}")
    a(f"- Tokens: {t['billable_input']:,} billable in / {t['output']:,} out  "
      f"|  cache hit {t['cache_hit_pct']}%")
    if s.get("orchestration_ms") is not None:
        a(f"- Time in the model: {_dur(t['llm_ms'])} of {_dur(s['wall_ms'])} wall "
          f"— {_dur(s['orchestration_ms'])} spent elsewhere")
    else:
        a(f"- Time in the model: {_dur(t['llm_ms'])} summed across calls, "
          f"{_dur(s['wall_ms'])} wall (x{s.get('concurrency')} concurrency — "
          f"documents ran in parallel)")
    a("")

    if s["failures"]:
        a("## Failures")
        a("")
        for f in s["failures"]:
            err = f.get("error") or {}
            a(f"### {f.get('node_id') or 'run'} — {f.get('where')}")
            a("")
            a(f"- **{err.get('type', 'error')}**: {err.get('message', '')}")
            for k in ("reason", "detail", "is_retryable"):
                if err.get(k) is not None:
                    a(f"- **{k}**: {err[k]}")
            if err.get("traceback"):
                a("")
                a("```")
                a(err["traceback"].strip())
                a("```")
            a("")

    a("## Documents")
    a("")
    a("| Document | Status | Time | Model | Calls | Billable in | Out | Cost |")
    a("|---|---|---|---|---|---|---|---|")
    for n in s["nodes"]:
        a(f"| {n['label']} | {n['status']} | {_dur(n['duration_ms'])} | "
          f"{n['model'] or '-'} | {n['calls']} | {n['billable_input']:,} | "
          f"{n['output']:,} | ${n['cost_usd']} |")
    a("")

    for n in s["nodes"]:
        if not n["stages"] and not n["grounding"]:
            continue
        a(f"### {n['label']}")
        a("")
        g = n.get("grounding") or {}
        a(f"- Grounding: {'attached' if g.get('resolved') else 'NOT attached'}"
          + (f" ({g.get('chars'):,} chars from {g.get('reference')})" if g.get("resolved") else ""))
        if n.get("authored") is not None:
            a(f"- Authoring: {'Claude built the file directly' if n['authored'] else 'fell back to template renderer'}"
              + (f" — {n.get('authoring_reason')}" if not n["authored"] and n.get("authoring_reason") else ""))
        if n.get("artifact"):
            art = n["artifact"]
            a(f"- Output: {art.get('file_name')} ({art.get('size_kb')} KB)")
        if n["stages"]:
            a("")
            a("| Stage | Status | Time |")
            a("|---|---|---|")
            for st in n["stages"]:
                a(f"| {st['stage']} | {st['status']} | {_dur(st['duration_ms'])} |")
        a("")

    if s["stages"]:
        a("## Where the model time went")
        a("")
        a("| Stage | Calls | Time | Billable in | Out | Cost |")
        a("|---|---|---|---|---|---|")
        for st in s["stages"][:20]:
            a(f"| {st['stage']} | {st['calls']} | {_dur(st['ms'])} | "
              f"{st['billable_input']:,} | {st['output']:,} | ${st['cost_usd']} |")
        a("")

    a("---")
    a(f"_Logs are kept for {runlog.MAX_AGE_DAYS} days or the last "
      f"{runlog.MAX_RUNS} runs, whichever comes first._")
    return "\n".join(L)


# ── Packaging ────────────────────────────────────────────────────────────────
def _server_logs(limit_bytes: int = 2_000_000) -> List[Path]:
    """
    The launcher's uvicorn stdout/stderr logs. Tracebacks that escape the application
    land there and nowhere else, so a support bundle without them is half a bundle.
    """
    try:
        import os
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            return []
        d = Path(base) / "ProjectZen" / "logs"
        if not d.exists():
            return []
        files = sorted(d.glob("server-*.log*"), key=lambda p: p.stat().st_mtime,
                       reverse=True)[:4]
        return [p for p in files if p.stat().st_size <= limit_bytes]
    except Exception:
        return []


def build_zip(run_ids: List[str], include_server_logs: bool = True) -> bytes:
    """
    Package one or more runs into a single zip.

    Per run: the raw JSONL (what an investigation reads), a rendered .md report (what
    the user reads), and the environment as its own file. Plus the server logs once,
    shared across runs.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        manifest: List[Dict[str, Any]] = []
        for rid in run_ids:
            if not runlog.valid_run_id(rid):
                continue
            events = runlog.read_run(rid)
            if not events:
                continue
            s = summarise(events)
            folder = rid if len(run_ids) > 1 else ""
            prefix = f"{folder}/" if folder else ""

            src = runlog.RUNLOG_DIR / f"{rid}.jsonl"
            if src.exists():
                # Read through runlog rather than z.write(src): the file may still be
                # open for append, and this keeps the snapshot consistent with the
                # report rendered from the same events.
                z.writestr(f"{prefix}run.jsonl",
                           "\n".join(json.dumps(e, ensure_ascii=False, default=str)
                                     for e in events) + "\n")
            z.writestr(f"{prefix}report.md", render_report(events))
            z.writestr(f"{prefix}summary.json",
                       json.dumps(s, indent=2, ensure_ascii=False, default=str))
            manifest.append({
                "run_id": rid, "status": s["status"], "kind": s["kind"],
                "started": s["started"], "duration_ms": s["wall_ms"],
                "documents": s["counts"], "cost_usd": s["totals"]["cost_usd"],
            })

        if include_server_logs:
            for p in _server_logs():
                try:
                    z.write(p, f"server-logs/{p.name}")
                except Exception:
                    pass

        z.writestr("manifest.json", json.dumps({
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "runs": manifest,
            "retention": {"max_runs": runlog.MAX_RUNS,
                          "max_age_days": runlog.MAX_AGE_DAYS},
        }, indent=2))
    return buf.getvalue()


def zip_name(run_ids: List[str]) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if len(run_ids) == 1:
        return f"projectzen-log-{run_ids[0]}.zip"
    return f"projectzen-logs-{len(run_ids)}runs-{stamp}.zip"


def save_to_desktop(run_ids: List[str]) -> Optional[str]:
    """
    Also drop the zip in Desktop\\ProjectZen Logs so the user does not have to hunt
    through the browser's download folder. Returns the path, or None if it failed.
    """
    try:
        data = build_zip(run_ids)
        target = export_dir() / zip_name(run_ids)
        target.write_bytes(data)
        return str(target)
    except Exception:
        return None


# ── Usage roll-up ────────────────────────────────────────────────────────────
# Feeds the profile popup's "usage" panel. Deliberately NOT built on summarise():
# that folds a trace into a per-document breakdown, and parsing 50 traces into full
# summaries to add up four fields is work nobody ever sees. This reads the llm_call
# rows and nothing else.
#
# WHAT THE NUMBERS MEAN, because it is easy to overclaim them:
#   * `cost_usd` is what the Claude SDK reported for each call, not an estimate we
#     derived from a price table that will drift.
#   * The scope is THIS INSTALL's traces for ONE account — not that account's total
#     Claude usage across Claude Code and claude.ai. Nothing here can see that: the
#     CLI exposes only `auth login|logout|status`, and org-wide usage lives behind
#     the Admin API, which needs an admin key ProjectZen does not hold.
#   * The buckets are bounded by retention (MAX_RUNS / MAX_AGE_DAYS), so "30 days"
#     and "everything stored" are normally the same figure. `bounded` is returned so
#     the UI can say "last 30 days" rather than implying a lifetime total.
#
# WHY THE `email` FILTER EXISTS. Without it this summed every trace in the folder no
# matter who produced it, so signing in as a different account showed an identical
# figure — the run logs on disk had not changed. On a shared machine that meant one
# person's spend was reported as another's. Every trace records the account that made
# it, so attribution is a filter, not a migration.
_USAGE_BUCKETS = (("today", None), ("week", 7), ("month", 30))


def _trace_account(events: List[Dict[str, Any]]) -> str:
    """
    The account that produced a trace. `env` carries it when the cache was warm at
    run start; a late `account` event carries it when it was not — summarise() looks
    in both places for the same reason.
    """
    for e in events:
        if e.get("t") == "env":
            a = (e.get("env") or {}).get("claude_account") or {}
            if a.get("email"):
                return str(a["email"])
    for e in events:
        if e.get("t") == "account":
            a = e.get("claude_account") or {}
            if a.get("email"):
                return str(a["email"])
    return ""


def usage_totals(email: Optional[str] = None) -> Dict[str, Any]:
    """
    Spend and token totals bucketed by age, for one account.

    `email` scopes the roll-up to the traces that account produced. Passing None
    keeps the old behaviour — every stored trace — which is only ever what a
    support export wants, never what a user is shown.
    """
    from datetime import timezone

    now = datetime.now(timezone.utc)
    today = now.date()
    want = (email or "").strip().lower()

    def blank() -> Dict[str, Any]:
        return {"calls": 0, "cost_usd": 0.0, "billable_input": 0,
                "output": 0, "cache_read": 0, "runs": 0}

    periods: Dict[str, Dict[str, Any]] = {k: blank() for k, _ in _USAGE_BUCKETS}
    runs_seen: Dict[str, set] = {k: set() for k, _ in _USAGE_BUCKETS}
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    files = 0
    matched = 0

    for path in runlog.RUNLOG_DIR.glob("*.jsonl"):
        files += 1
        run_id = path.stem
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Read the trace up front rather than streaming: the account is only known
        # once an `env` (or late `account`) event has been seen, and a call must not
        # be counted before we know whose it is. Traces are a few hundred lines.
        events: List[Dict[str, Any]] = []
        with fh:
            for line in fh:
                # One malformed line must not cost the whole roll-up — a trace can be
                # mid-write, and a torn tail is expected rather than exceptional.
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue

        if want and _trace_account(events).strip().lower() != want:
            continue
        matched += 1

        for e in events:
            if e.get("t") != "llm_call":
                continue
            try:
                ts = datetime.fromisoformat(str(e.get("ts") or ""))
            except Exception:
                continue

            first_ts = ts if first_ts is None or ts < first_ts else first_ts
            last_ts  = ts if last_ts  is None or ts > last_ts  else last_ts

            cost = float(e.get("cost_usd") or 0.0)
            bi   = int(e.get("billable_input") or 0)
            out  = int(e.get("output") or 0)
            cr   = int(e.get("cache_read") or 0)
            age_days = (now - ts).total_seconds() / 86400.0

            for key, window in _USAGE_BUCKETS:
                if window is None:
                    if ts.date() != today:
                        continue
                elif age_days > window:
                    continue
                b = periods[key]
                b["calls"] += 1
                b["cost_usd"] += cost
                b["billable_input"] += bi
                b["output"] += out
                b["cache_read"] += cr
                runs_seen[key].add(run_id)

    for key in periods:
        b = periods[key]
        b["cost_usd"] = round(b["cost_usd"], 4)
        b["runs"] = len(runs_seen[key])
        # Cache reads bill at a large discount, so the hit rate is the single number
        # that explains why a spend figure is as low as it is.
        served = b["cache_read"] + b["billable_input"]
        b["cache_hit_pct"] = round(b["cache_read"] / served * 100, 1) if served else 0.0

    return {
        "periods": periods,
        "account": email or "",
        "matched_runs": matched,
        "stored_runs": files,
        "first_call": first_ts.isoformat() if first_ts else None,
        "last_call": last_ts.isoformat() if last_ts else None,
        "bounded": {"max_runs": runlog.MAX_RUNS, "max_age_days": runlog.MAX_AGE_DAYS},
    }
