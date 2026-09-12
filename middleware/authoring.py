"""
authoring.py — Section C: let Claude BUILD the file, with templates.py as the floor.

THE PROBLEM THIS SOLVES
-----------------------
Everything upstream of here is good: research, plan, generation, four reviewers,
bounded rework. All of it produces a JSON plan. Then templates.py renders that plan.

The JSON schema for xlsx is:

    {"sheets":[{"name","headers","rows","has_totals"}], "chart_sheet", "chart_data_col"}

There is no field for a formula, a column width, a conditional format, a merged
header band, a freeze pane, a number format, or a per-sheet colour. So those cannot
reach the file no matter how well the model reasons. Measured on the same
deliverable (SP051 Project Plan):

    Claude Desktop  ..... 13 sheets, 32 colours, 1,087 formulas
    ProjectZen JSON ..... 10 sheets,  2 colours,     0 formulas

The 2 colours are `_xlsx_extract_theme()` in templates.py reading exactly two hex
values out of the reference. That is the whole of the visual fidelity budget.

Note the asymmetry this fixes: generate_docx() and generate_pptx() OPEN the reference
and clone it, inheriting its styles wholesale. generate_xlsx() cannot — there is no
equivalent "clone the workbook" move — so xlsx is the format where the JSON
bottleneck actually bites. That is why authoring defaults to xlsx only.

WHAT THIS DOES
--------------
Hands the already-reviewed plan (the WHAT) plus the reference workbook (the HOW) to
a Claude agent with Write + Bash in a throwaway sandbox, and lets it author the file
with openpyxl the same way it does in Desktop.

WHY THIS IS SAFE TO ADD
-----------------------
It is a strict improvement attempt with a floor, not a replacement:

  * The content contract is unchanged. Authoring renders the SAME reviewed plan;
    it does not get to decide scope. Research/plan/review/rework all still run.
  * The output must pass a validation gate that compares it against what
    templates.generate() would have produced. Fewer sheets or fewer rows than the
    plan = rejected.
  * On rejection, on exception, on timeout, on a missing file — the caller falls
    back to templates.generate() and the result is byte-for-byte today's behaviour.

There is no path where enabling this produces a worse file than the JSON path. The
worst case is the wasted time of the attempt.

COST
----
An authoring run is an agentic loop (inspect -> write script -> run -> verify),
typically 6-12 turns. It is the most expensive single step in the pipeline, which is
why it is scoped to formats that benefit and can be switched off entirely:

    PROJECTZEN_AUTHORING = off | xlsx | all        (default: xlsx)
"""

import asyncio
import collections
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# ── Configuration ────────────────────────────────────────────────────────────
# "xlsx" is the default because docx/pptx already clone their reference in
# templates.py and have far less to gain, while paying the same agentic cost.
# Default is "deck" (xlsx + pptx). Set PROJECTZEN_AUTHORING=xlsx to go back to
# workbooks only, "all" to include docx, or "off" to disable authoring entirely.
_MODE = (os.environ.get("PROJECTZEN_AUTHORING") or "deck").strip().lower()

_ENABLED_FORMATS = {
    "off":  set(),
    "xlsx": {"xlsx"},
    # pptx is on by DEFAULT and docx is not, which is an evidence split rather than an
    # oversight. A cloned deck was measured against a real reference and against a
    # Claude Desktop deck built the same way; docx has never been measured because no
    # Word reference exists anywhere in this estate to measure against. Turning on a
    # path whose output nobody has looked at is how the from-scratch regression got in.
    "deck": {"xlsx", "pptx"},
    "all":  {"xlsx", "docx", "pptx"},
}.get(_MODE, {"xlsx", "pptx"})

# ── Budget ───────────────────────────────────────────────────────────────────
# The original flat 600s was calibrated on a 3-sheet fixture (which finished in
# 195s) and then applied to a 15-sheet Project Plan, where it expired at 9m43s
# having produced nothing. That is the worst possible outcome: full cost, zero
# output, and a 10-minute delay before the fallback even starts.
#
# Two changes. First, the clock scales with how much there is to build. Second,
# and more important, a run that CANNOT fit in the allowance is skipped outright
# rather than started and abandoned — never pay for an attempt that cannot finish.
# Calibrated against an observed run, not guessed. With the model rules in place a
# 3-sheet fixture spent 385s in a single thinking block before writing anything,
# wrote build.py at 490s and ran it at 498s. The old 240+60/sheet gave that run 420s
# and killed it seconds before its first write. Base covers the up-front reasoning;
# the per-sheet term covers the incremental build-and-verify loop.
AUTHOR_BASE_S      = int(os.environ.get("PROJECTZEN_AUTHORING_BASE") or 420)
AUTHOR_PER_SHEET_S = int(os.environ.get("PROJECTZEN_AUTHORING_PER_SHEET") or 90)
AUTHOR_MAX_S       = int(os.environ.get("PROJECTZEN_AUTHORING_MAX") or 2400)
AUTHOR_MAX_TURNS   = int(os.environ.get("PROJECTZEN_AUTHORING_TURNS") or 60)

# A spend ceiling bounds what wall-clock cannot: the same elapsed time costs very
# different amounts depending on org throughput. 0 disables the cap.
AUTHOR_MAX_USD = float(os.environ.get("PROJECTZEN_AUTHORING_MAX_USD") or 0)


# Cloning a deck costs more per unit than building a sheet, and the original rates were
# calibrated only on xlsx.
#
# Measured: a 15-slide Governance Matrix clone was given 420 + 90x15 = 1770s, ran to
# that ceiling, was killed, and every second of it was thrown away — the user got the
# basic renderer after a 29.5 minute wait. Repopulating a cloned slide means reading its
# existing shapes, matching them to content and rewriting each run, which is simply more
# work than emitting a fresh sheet. A separate, higher rate and ceiling for decks; xlsx
# keeps exactly the numbers it was tuned with.
DECK_BASE_S      = int(os.environ.get("PROJECTZEN_AUTHORING_DECK_BASE") or 600)
DECK_PER_SLIDE_S = int(os.environ.get("PROJECTZEN_AUTHORING_DECK_PER_SLIDE") or 220)
DECK_MAX_S       = int(os.environ.get("PROJECTZEN_AUTHORING_DECK_MAX") or 5400)

# Build time tracks CONTENT, not page count — and pricing it per slide shipped a
# corrupt deck. Measured on two real runs:
#
#   5 slides,  344 rows  ->  620s   (completed comfortably)
#   6 slides,  855 rows  -> >1486s  (killed at its 1480s budget, mid-write)
#
# Both were priced from the PLAN's 4 slides, so both got the same 1480s while the
# second had 2.5x the content. The agent repopulates shape by shape, so a row is the
# unit of work; a slide is only the container it sits in. The salvaged half-built file
# from that timeout is what PowerPoint refused to open.
#
# The row term ADDS to the existing per-slide rate rather than replacing part of it.
# Lowering per-slide to compensate looked tidier and was wrong: it cut a 15-slide deck
# from 65 to 40 minutes whenever a caller passes no row count, which is a silent
# reduction in exactly the case that first motivated the deck budget. Adding is
# strictly more generous than before, everywhere.
#
# Deliberately generous: over-budgeting costs nothing when the agent finishes early,
# whereas under-budgeting destroys the entire attempt — and, as this run proved, can
# ship a half-written file. 4 slides / 90 planned rows now gets 2380s against the
# ~1500s it actually needed.
DECK_PER_ROW_S   = int(os.environ.get("PROJECTZEN_AUTHORING_DECK_PER_ROW") or 10)


def budget_for(containers: int, output_format: str = "xlsx",
               rows: int = 0) -> Tuple[int, str]:
    """
    Seconds to allow for `containers` sheets/sections/slides, and "" if it fits.
    A non-empty second value means: do not start, and here is why.

    `rows` is the planned DATA ROW count. It applies to decks only, where the agent
    repopulates shape by shape and the row is the real unit of work — see the note
    above DECK_PER_ROW_S. Defaults to 0, so xlsx and every existing caller price
    exactly as before.
    """
    fmt = (output_format or "").lower()
    if fmt in ("pptx", "docx"):
        base, per, ceiling = DECK_BASE_S, DECK_PER_SLIDE_S, DECK_MAX_S
        unit = "slides/sections"
    else:
        base, per, ceiling = AUTHOR_BASE_S, AUTHOR_PER_SHEET_S, AUTHOR_MAX_S
        unit = "sheets"
    need = base + per * max(0, containers)
    if fmt in ("pptx", "docx"):
        # The row term CLAMPS rather than refuses. Refusing means no authoring at all
        # and the plain renderer instead — a guaranteed loss of the cloned design. A
        # content-rich deck that might finish inside the ceiling is worth attempting,
        # and the timeout-salvage gate below decides whether what came back is usable.
        # Only the structural term (base + slides) can still refuse outright, because
        # that one says the job cannot fit however long it runs.
        if base + per * max(0, containers) > ceiling:
            return 0, (f"{containers} {unit} needs ~{need}s but the ceiling is "
                       f"{ceiling}s; skipped rather than started and abandoned")
        return min(ceiling, need + DECK_PER_ROW_S * max(0, int(rows or 0))), ""
    if need > ceiling:
        return 0, (f"{containers} {unit} needs ~{need}s but the ceiling is "
                   f"{ceiling}s; skipped rather than started and abandoned")
    return need, ""

_SANDBOX_ROOT = Path(__file__).parent / ".llm_scratch" / "authoring"

_LIB_FOR_FORMAT = {
    "xlsx": "openpyxl",
    "docx": "python-docx",
    "pptx": "python-pptx",
}


def is_enabled(output_format: str) -> bool:
    return (output_format or "").lower() in _ENABLED_FORMATS


def _safe_rmtree(path: Path, attempts: int = 4) -> bool:
    """
    Remove a sandbox, tolerating Windows' refusal to delete a directory that is
    still some process's working directory.

    The CLI subprocess runs with cwd=sandbox. On Windows that directory cannot be
    unlinked until the process has fully exited, and the exit lags the last message
    we receive. The original `rmtree(ignore_errors=True)` therefore deleted the
    CONTENTS and silently left the directory behind — which is exactly what the
    empty orphan sandbox on disk turned out to be. Retry briefly, then give up and
    let the next run's sweep collect it.
    """
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.25 * (i + 1))
    return False


def sweep_stale_sandboxes(max_age_s: int = 3600) -> int:
    """Collect sandboxes a previous run could not delete. Best-effort, never raises."""
    removed = 0
    try:
        if not _SANDBOX_ROOT.exists():
            return 0
        cutoff = time.time() - max_age_s
        for child in _SANDBOX_ROOT.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    if _safe_rmtree(child, attempts=1):
                        removed += 1
            except Exception:
                continue
    except Exception:
        pass
    return removed


# ── Expectations derived from the reviewed plan ──────────────────────────────

def _plan_expectations(plan: Dict[str, Any], output_format: str) -> Dict[str, Any]:
    """
    What templates.generate() would have produced from this plan. The authored file
    has to match or beat it, otherwise authoring has lost content and is rejected.
    """
    fmt = (output_format or "").lower()
    if fmt in ("xlsx", "xml"):
        sheets = [s for s in (plan.get("sheets") or []) if isinstance(s, dict)]
        per = [len(s.get("rows") or []) for s in sheets]
        return {
            "containers": len(sheets),
            "names": [str(s.get("name") or "") for s in sheets],
            "rows": sum(per),
            "rows_per_container": per,
        }
    if fmt == "pptx":
        slides = [s for s in (plan.get("slides") or []) if isinstance(s, dict)]
        per = [len(s.get("rows") or []) for s in slides]
        return {
            "containers": len(slides),
            "names": [str(s.get("title") or "") for s in slides],
            "rows": sum(per),
            "rows_per_container": per,
        }
    sections = [s for s in (plan.get("sections") or []) if isinstance(s, dict)]
    per = []
    for s in sections:
        n = len(s.get("paragraphs") or []) + len(s.get("bullets") or [])
        tbl = s.get("table") or {}
        if isinstance(tbl, dict):
            n += len(tbl.get("rows") or [])
        per.append(n)
    return {
        "containers": len(sections),
        "names": [str(s.get("heading") or "") for s in sections],
        "rows": sum(per),
        "rows_per_container": per,
    }


def _norm(name: str) -> str:
    """Sheet names for matching: case/punctuation-insensitive. Excel truncates sheet
    names at 31 chars, so a planned name longer than that will legitimately come back
    shortened — compare on a common prefix rather than demanding equality."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())[:24]


# ── Validation gate ──────────────────────────────────────────────────────────

def _inspect_xlsx(path: str) -> Dict[str, Any]:
    import datetime as _dt
    from openpyxl import load_workbook
    wb = load_workbook(path)                      # NOT data_only — we want formulas
    sheets, per_rows, rows = [], [], 0
    formulas = xsheet = typed_dates = text_dates = 0
    colours, numfmts = set(), set()
    # A date written as text is the specific defect that made the last output inert,
    # so it is counted rather than merely hoped for.
    _TEXTDATE = re.compile(r"^\s*\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}\s*$|^\s*\d{4}-\d{2}-\d{2}\s*$")
    for ws in wb.worksheets:
        sheets.append(ws.title)
        used = 0
        for row in ws.iter_rows():
            if any(c.value not in (None, "") for c in row):
                used += 1
            for c in row:
                v = c.value
                if isinstance(v, str) and v.startswith("="):
                    formulas += 1
                    if "!" in v:
                        xsheet += 1
                elif isinstance(v, (_dt.date, _dt.datetime)):
                    typed_dates += 1
                elif isinstance(v, str) and _TEXTDATE.match(v):
                    text_dates += 1
                if c.number_format and c.number_format != "General":
                    numfmts.add(c.number_format)
                try:
                    fill = c.fill
                    if fill is not None and fill.fgColor is not None and fill.fgColor.rgb:
                        colours.add(str(fill.fgColor.rgb))
                except Exception:
                    pass
        per_rows.append(used)
        rows += used
    dv = sum(len(ws.data_validations.dataValidation) for ws in wb.worksheets)
    wb.close()
    return {"containers": len(sheets), "names": sheets, "rows": rows,
            "rows_per_container": per_rows, "formulas": formulas,
            "cross_sheet": xsheet, "typed_dates": typed_dates,
            "text_dates": text_dates, "validations": dv,
            "numfmts": len(numfmts), "colours": len(colours)}


def _inspect_docx(path: str) -> Dict[str, Any]:
    from docx import Document
    doc = Document(path)
    heads: List[str] = []
    per: List[int] = []
    body = 0
    for p in doc.paragraphs:
        if (p.style.name or "").startswith("Heading"):
            heads.append(p.text)
            per.append(0)
        elif p.text.strip():
            body += 1
            if per:
                per[-1] += 1
    tbl = sum(len(t.rows) for t in doc.tables)
    body += tbl
    if per:
        per[-1] += tbl          # tables are not addressable to a heading; attribute last
    return {"containers": len(heads) or 1, "names": heads, "rows": body,
            "rows_per_container": per or [body], "formulas": 0, "colours": 0}


def _inspect_pptx(path: str) -> Dict[str, Any]:
    from pptx import Presentation
    prs = Presentation(path)
    names, per, rows = [], [], 0
    for slide in prs.slides:
        title, n = "", 0
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text and not title:
                    title = text.split("\n")[0]
                n += sum(1 for p in shape.text_frame.paragraphs if p.text.strip())
        names.append(title)
        per.append(n)
        rows += n
    return {"containers": len(names), "names": names, "rows": rows,
            "rows_per_container": per, "formulas": 0, "colours": 0}


_INSPECTORS = {"xlsx": _inspect_xlsx, "docx": _inspect_docx, "pptx": _inspect_pptx}


def design_fidelity(path: str, reference: str, output_format: str
                    ) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Did the reference's DESIGN survive into the output?

    Content volume was the only thing checked before, and it approved a deck that had
    discarded every slide master, all 62 layouts and all 31 images — because those are
    not content. For a cloned artefact this is the check that matters: the words are
    supposed to change and the design is not.

    Returns (ok, reason, measurements). Never raises; an unmeasurable pair passes,
    because a broken check must not block a good file.
    """
    fmt = (output_format or "").lower()
    if fmt not in ("pptx", "docx") or not reference or not os.path.exists(reference):
        return True, "", {}
    try:
        import zipfile

        def look(p):
            z = zipfile.ZipFile(p)
            names = z.namelist()
            info = {
                "media":   len([n for n in names if "/media/" in n]),
                "masters": len([n for n in names if "slideMasters/slideMaster" in n
                                or n.endswith("word/styles.xml")]),
                "layouts": len([n for n in names if "slideLayouts/slideLayout" in n]),
                "themes":  len([n for n in names if "/theme/theme" in n]),
            }
            if fmt == "pptx":
                pres = z.read("ppt/presentation.xml").decode("utf-8", "replace")
                m = re.search(r'sldSz[^/]*cx="(\d+)"[^/]*cy="(\d+)"', pres)
                info["canvas"] = (m.group(1), m.group(2)) if m else None
            z.close()
            return info

        r, o = look(reference), look(path)
        stats = {"reference": r, "output": o}
        problems = []
        if fmt == "pptx" and r.get("canvas") and o.get("canvas") != r.get("canvas"):
            problems.append(f"slide size changed ({o.get('canvas')} vs {r.get('canvas')})")
        if r["masters"] and o["masters"] < r["masters"]:
            problems.append(f"slide masters dropped ({o['masters']} of {r['masters']})")
        if r["layouts"] and o["layouts"] < max(1, int(r["layouts"] * 0.5)):
            problems.append(f"layouts dropped ({o['layouts']} of {r['layouts']})")
        if r["media"] and o["media"] < max(1, int(r["media"] * 0.5)):
            problems.append(f"embedded images dropped ({o['media']} of {r['media']})")
        if problems:
            return False, "design not preserved: " + "; ".join(problems), stats
        return True, "", stats
    except Exception as exc:
        return True, f"design check skipped ({type(exc).__name__})", {}


def package_integrity(path: str, output_format: str) -> Tuple[bool, str]:
    """
    Is this a structurally sound OPC package? Returns (ok, reason).

    Used only on a file salvaged from a timeout, where the build was interrupted and
    the package may be half-written. A completed run does not need it.

    HONEST LIMITATION: the deck that PowerPoint refused to open after the 1480s
    timeout PASSES every check in here — I verified zip integrity, relationship
    resolution, content-type overrides, XML well-formedness, shape-id uniqueness and
    attribute ranges against that exact file and found nothing. So this is defence in
    depth, not the fix. The fix is not timing out (see DECK_PER_ROW_S). What this does
    guarantee is that the cruder failures — a truncated archive, a dangling
    relationship, a part missing its content type — can never ship.

    Never raises: an unreadable package is a failure, not an exception.
    """
    fmt = (output_format or "").lower()
    try:
        import zipfile
        from xml.etree import ElementTree as ET

        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                return False, "archive contains a corrupt member"
            names = set(z.namelist())
            if "[Content_Types].xml" not in names:
                return False, "package has no [Content_Types].xml"

            for n in names:
                if n.endswith((".xml", ".rels")):
                    try:
                        ET.fromstring(z.read(n))
                    except Exception as e:
                        return False, f"{n} is not well-formed XML ({str(e)[:60]})"

            ct = z.read("[Content_Types].xml").decode("utf8", "replace")
            for n in names:
                if n.startswith("ppt/slides/slide") and n.endswith(".xml"):
                    if f"/{n}" not in ct:
                        return False, f"{n} has no content-type override"

            for n in names:
                if not n.endswith(".rels"):
                    continue
                folder = n.rsplit("_rels/", 1)[0]
                for m in re.finditer(r'Target="([^"]+)"([^>]*)', z.read(n).decode(
                        "utf8", "replace")):
                    target, rest = m.group(1), m.group(2)
                    if "External" in rest or target.startswith(("http", "mailto")):
                        continue
                    resolved = os.path.normpath(
                        os.path.join(folder, target)).replace("\\", "/")
                    if resolved not in names:
                        return False, f"{n} points at missing part {target}"

        # Round-trip: the library re-serialises every part, which surfaces internal
        # inconsistencies that reading alone tolerates.
        if fmt in ("pptx", "docx"):
            import tempfile
            opener = None
            if fmt == "pptx":
                from pptx import Presentation as opener        # type: ignore
            else:
                from docx import Document as opener            # type: ignore
            doc = opener(path)
            with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as tf:
                tmp = tf.name
            try:
                doc.save(tmp)
                if os.path.getsize(tmp) < 1024:
                    return False, "re-saved package is implausibly small"
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        return True, ""
    except Exception as e:
        return False, f"package will not open cleanly: {type(e).__name__}: {str(e)[:80]}"


def validate(path: str, output_format: str, expect: Dict[str, Any],
             ref_stats: Optional[Dict[str, Any]] = None) -> Tuple[bool, str, Dict]:
    """
    The gate. Returns (accepted, reason, actual_stats).

    Checks content volume PER CONTAINER, not just in total. The earlier version
    summed rows across the workbook, which is why a Project Plan whose Master Plan
    sheet collapsed from 288 rows to 121 still passed: five extra sheets masked the
    loss and the total went UP. A shortfall in any single sheet is now a rejection.

    `ref_stats` enables fidelity checks that are only meaningful RELATIVE to the
    reference. A deliverable with no formulas is perfectly legitimate — unless the
    reference it is modelled on has 1,087 of them, in which case producing zero is
    a failure to reproduce the artefact's form. There is no absolute floor.
    """
    fmt = (output_format or "").lower()
    if not path or not os.path.exists(path):
        return False, "no output file was produced", {}
    if os.path.getsize(path) < 1024:
        return False, f"output is only {os.path.getsize(path)} bytes", {}

    inspector = _INSPECTORS.get(fmt)
    if inspector is None:
        return False, f"no inspector for .{fmt}", {}
    try:
        actual = inspector(path)
    except Exception as e:
        return False, f"output will not open: {str(e)[:120]}", {}

    want_c = int(expect.get("containers") or 0)
    want_r = int(expect.get("rows") or 0)

    if want_c and actual["containers"] < want_c:
        return False, (f"lost content: {actual['containers']} sheets/sections vs "
                       f"{want_c} in the approved plan"), actual

    # ── decks: slide count may exceed the plan, but only so far ───────────────
    #
    # The original ten-slide failure was a deck padded to a quota with thin pages, so
    # this cap exists to stop that recurring. It is deliberately loose: DETAIL is the
    # priority for this deliverable and one or two extra slides are acceptable when the
    # content genuinely needs them. A tighter cap (+1) measurably cost content — the
    # deck consolidated its RACI onto one page by dropping 19 rows of activities.
    #
    # +2 is the line. Beyond that the deck is being paginated rather than authored, and
    # the thin-page failure is back. Note a rejection costs the entire cloned design,
    # since the run falls back to the plain renderer — so this must not be strict.
    if fmt == "pptx" and want_c and actual["containers"] > want_c + 2:
        return False, (f"padded: {actual['containers']} slides vs {want_c} in the "
                       f"approved plan (up to {want_c + 2} allowed) — the content "
                       f"belongs on denser pages, not more of them"), actual
    # 5% tolerance: a genuine re-layout (merging two thin rows, dropping a spacer)
    # should not be treated as content loss, but a real drop still is.
    if want_r and actual["rows"] < want_r * 0.95:
        return False, (f"lost content: {actual['rows']} rows vs {want_r} in the "
                       f"approved plan"), actual

    # ── per-container floor: the check that would have caught the Master Plan ──
    want_per = expect.get("rows_per_container") or []
    want_names = expect.get("names") or []
    if want_per and want_names:
        got = {_norm(n): r for n, r in zip(actual.get("names") or [],
                                           actual.get("rows_per_container") or [])}
        for name, need in zip(want_names, want_per):
            if need <= 0:
                continue
            have = got.get(_norm(name))
            if have is None:                      # unmatched name — positional fallback
                continue
            if have < need * 0.9:
                return False, (f"'{name}' has {have} rows but the approved plan "
                               f"specifies {need}"), actual

    # ── fidelity, judged only against the reference ───────────────────────────
    if fmt == "xlsx" and ref_stats:
        ref_f = int(ref_stats.get("formulas") or 0)
        if ref_f >= 50 and actual.get("formulas", 0) == 0:
            return False, (f"reference has {ref_f} formulas, output has none — the "
                           f"dates and totals are inert values, not a working model"), actual
        # Threshold, not "any": an incidental date-shaped string (a version stamp in
        # a history sheet) must not fail a document. A workbook with dozens of them
        # and not one real date cell is the actual defect.
        if actual.get("text_dates", 0) >= 10 and actual.get("typed_dates", 0) == 0:
            return False, (f"{actual['text_dates']} dates written as text with no real "
                           f"date cells — cannot sort, filter or compute"), actual

    return True, "accepted", actual


def reference_stats(path: str) -> Dict[str, Any]:
    """Formula/format counts for the reference, for the relative checks above."""
    try:
        if path and path.lower().endswith((".xlsx", ".xlsm")):
            return _inspect_xlsx(path)
    except Exception:
        pass
    return {}


# ── F1: measure the reference in Python, don't make the model discover it ────
#
# The authoring agent previously had to open the reference itself and work out its
# conventions over several Bash/openpyxl turns before it could write a line of
# build.py. That is slow, costs tokens, and is guesswork the model can get wrong.
#
# openpyxl can answer all of it exactly, for free, in-process. Handing over measured
# facts converts the most expensive part of the run into a constant.

_CELL_RE  = re.compile(r"\$?[A-Z]{1,3}\$?\d+")
_RANGE_RE = re.compile(r"\$?[A-Z]{1,3}\$?\d+:\$?[A-Z]{1,3}\$?\d+")
_SHEET_RE = re.compile(r"'[^']+'!\$?[A-Z]{1,3}\$?\d+|[A-Za-z_]\w*!\$?[A-Z]{1,3}\$?\d+")


def _abstract_formula(f: str) -> str:
    """`=WORKDAY(G8,E8-1)` -> `=WORKDAY(<cell>,<cell>-<n>)` so shapes can be counted."""
    f = _RANGE_RE.sub("<range>", f)
    f = _SHEET_RE.sub("<sheet_ref>", f)      # before <cell>: a sheet ref contains one
    f = _CELL_RE.sub("<cell>", f)
    return re.sub(r"\b\d+\b", "<n>", f)


def _describe_fill(cell) -> str:
    try:
        rgb = cell.fill.fgColor.rgb if (cell.fill and cell.fill.fgColor) else None
        return str(rgb)[-6:] if rgb and str(rgb) != "00000000" else ""
    except Exception:
        return ""


def analyse_reference(path: str) -> str:
    """
    A measured description of the reference workbook, as a markdown block for the
    brief. Returns "" for anything not an inspectable xlsx — the brief then falls
    back to generic conventions.
    """
    if not path or not path.lower().endswith((".xlsx", ".xlsm")):
        return ""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path)                    # keep formulas
    except Exception:
        return ""

    try:
        shapes: collections.Counter = collections.Counter()
        numfmts: collections.Counter = collections.Counter()
        abs_refs: collections.Counter = collections.Counter()
        validations: List[str] = []
        sheet_lines: List[str] = []
        header_desc = ""

        for ws in wb.worksheets:
            rows = sum(1 for r in ws.iter_rows() if any(c.value not in (None, "") for c in r))

            # Header row = first row that is mostly non-empty strings.
            hdr_row = 0
            for r in range(1, min(6, ws.max_row) + 1):
                vals = [ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
                filled = [v for v in vals if isinstance(v, str) and v.strip()]
                if len(filled) >= max(2, int(0.6 * ws.max_column)):
                    hdr_row = r
                    break

            if hdr_row and not header_desc:
                h = ws.cell(hdr_row, 1)
                header_desc = (
                    f"header on row {hdr_row}: bold={bool(h.font and h.font.bold)}, "
                    f"font colour #{str(getattr(h.font.color,'rgb','') or '')[-6:] or 'default'}, "
                    f"fill #{_describe_fill(h) or 'none'}")

            widths = {k: round(v.width, 1) for k, v in ws.column_dimensions.items() if v.width}
            fills = collections.Counter()
            for row in ws.iter_rows():
                for c in row:
                    hexv = _describe_fill(c)
                    if hexv:
                        fills[hexv] += 1
                    if c.number_format and c.number_format != "General":
                        numfmts[c.number_format] += 1
                    if isinstance(c.value, str) and c.value.startswith("="):
                        shapes[_abstract_formula(c.value)] += 1
                        for m in re.finditer(r"\$[A-Z]{1,3}\$\d+", c.value):
                            abs_refs[f"{ws.title}!{m.group(0)}"] += 1

            for dv in ws.data_validations.dataValidation:
                if dv.formula1:
                    validations.append(f"{ws.title} {dv.sqref}: list = {str(dv.formula1)[:120]}")

            w = ", ".join(f"{k}={v}" for k, v in list(widths.items())[:8])
            sheet_lines.append(
                f"  - {ws.title}: {rows} rows x {ws.max_column} cols, "
                f"freeze={ws.freeze_panes or 'none'}, widths[{w}]"
                + (f", fills[{','.join('#'+f for f,_ in fills.most_common(3))}]" if fills else ""))

        out = ["REFERENCE ANALYSIS — measured with openpyxl. These are FACTS about the",
               "reference file. Trust them; do not spend turns re-deriving them.", ""]
        out.append(f"Workbook: {len(wb.worksheets)} sheets. {header_desc}")
        out.append("")
        out.append("SHEETS:")
        out += sheet_lines[:20]
        if numfmts:
            out += ["", "NUMBER FORMATS IN USE (apply the same ones):"]
            out += [f"  - {fmt!r} x{n}" for fmt, n in numfmts.most_common(6)]
        if shapes:
            total = sum(shapes.values())
            out += ["", f"FORMULA PATTERNS ({total} formulas total) — reproduce these shapes:"]
            out += [f"  - {pat:44} x{n}" for pat, n in shapes.most_common(10)]
        if abs_refs:
            top, n = abs_refs.most_common(1)[0]
            out += ["", f"DRIVER CELL: {top} is referenced absolutely {n}x — the workbook is",
                    "  built so changing that one cell reflows everything downstream."]
        if validations:
            out += ["", "DATA VALIDATION (reproduce):"]
            out += [f"  - {v}" for v in validations[:4]]
        return "\n".join(out)
    except Exception:
        return ""
    finally:
        try:
            wb.close()
        except Exception:
            pass


def reference_deck_blueprint(path: str, max_chars: int = 2600) -> str:
    """
    Describe how the reference deck is ORGANISED, for the planning prompt.

    Why this exists: the plan stage never knew the reference had slide archetypes. Given
    a governance reference of five dense slides it emitted fifteen thin ones — one per
    topic — because nothing told it the reference consolidates. The authoring agent then
    had fifteen slides to clone instead of five, blew its time budget, and the run fell
    back to the basic renderer. The measured reference puts a 4x3 tier grid plus a
    six-card ribbon on ONE slide and a 12x7 coded matrix on another; a planner that
    cannot see that will always spread the same content thinner.

    Returns "" when the deck cannot be read, so callers keep their previous behaviour.
    """
    if not path or not os.path.exists(path) or not path.lower().endswith(".pptx"):
        return ""
    try:
        from pptx import Presentation
        from pptx.util import Emu
        import collections

        prs = Presentation(path)
        W = Emu(prs.slide_width).inches or 10.0
        lines = []
        for i, slide in enumerate(prs.slides, 1):
            title, bands = "", collections.defaultdict(list)
            spans = []
            for sh in slide.shapes:
                if sh.has_text_frame:
                    txt = sh.text_frame.text.strip()
                    if txt and not title:
                        title = txt.split("\n")[0][:52]
                try:
                    top = Emu(sh.top).inches
                    left = Emu(sh.left).inches
                    wid = Emu(sh.width).inches
                    bands[round(top, 1)].append(left)
                    # Body region only: above it sits the header band and the caption,
                    # below it the footer, and neither says anything about the layout.
                    if 1.15 <= top <= 5.1 and wid > 0.02:
                        spans.append((left, left + wid))
                except Exception:
                    pass
            n = len(slide.shapes)
            band_sizes = sorted((len(v) for v in bands.values()), reverse=True)
            widest = band_sizes[0] if band_sizes else 0

            # SPLIT = a genuine empty vertical corridor through the body.
            #
            # Two earlier heuristics both failed on real slides. Looking above y=1.2
            # caught the "accenture" logo, which is on every slide, so all five read as
            # split. Looking for narrow sub-headings then caught the RACI slide's
            # R/A/C/I legend chips. A corridor is what a split layout physically IS, it
            # works wherever the division falls — the cadence slide divides at 76% of
            # the width, not the midpoint — and a dense grid has no such gap because its
            # columns are contiguous.
            corridor, corridor_mid = 0.0, 0.0
            if spans:
                merged = []
                for a, b in sorted(spans):
                    if merged and a <= merged[-1][1] + 0.02:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                    else:
                        merged.append((a, b))
                for (a1, b1), (a2, _) in zip(merged, merged[1:]):
                    mid = (b1 + a2) / 2.0
                    if 0.30 * W <= mid <= 0.85 * W and (a2 - b1) > corridor:
                        corridor, corridor_mid = a2 - b1, mid
            split = corridor >= 0.28
            # Order matters. Testing density first labelled four of the five slides
            # "very dense matrix", including the split cadence slide and the 48-shape
            # tier grid — a description that fits everything tells the planner nothing.
            # Distinctive LAYOUT is checked before raw volume, and the volume bands are
            # set from the measured deck: 48 shapes is a grid, 310 is a coded matrix.
            grid = f"about {len(bands)} rows, up to {widest} cells per row"
            if n <= 8:
                shape_of = "title / cover slide — a few large text blocks only"
            elif n >= 150:
                shape_of = (f"VERY DENSE COLOUR-CODED MATRIX — {grid}; cells are small "
                            f"coded markers, not sentences")
            elif n >= 40:
                shape_of = f"DENSE STRUCTURED GRID — {grid}"
            else:
                shape_of = f"single content block ({grid})"
            # Only stated when the geometry actually shows it. Deliberately additive
            # rather than a category of its own: the cadence slide reads as two panels
            # to the eye, yet its shapes are horizontally contiguous with no corridor at
            # all, so calling it split would be a guess dressed as a measurement. Where
            # a corridor IS present (the contacts slide divides at 51% of the width) it
            # is worth telling the planner.
            if split:
                shape_of += (f"; laid out as TWO PANELS SIDE BY SIDE, divided at about "
                             f"{round(100 * (corridor_mid / W))}% of the width")
            lines.append(f"  {i}. \"{title}\" — {n} shapes; {shape_of}")

        return (
            "REFERENCE DECK ORGANISATION\n"
            f"The reference presents its whole subject in {len(prs.slides)} slides:\n"
            + "\n".join(lines) + "\n\n"
            "CONSOLIDATE THE WAY IT DOES; DO NOT MULTIPLY SLIDES.\n"
            "Each of those slides carries a great deal on one page by using a grid, "
            "colour coding and short labels rather than sentences. Plan the same way: "
            "one slide per subject area, dense, with the detail expressed as table rows "
            "and coded cells. Producing a separate slide per topic is a DEFECT here — it "
            "is what turns a five-slide wall chart into fifteen pages of thin bullets, "
            "and it loses the at-a-glance quality that is the whole point of the format."
        )[:max_chars]
    except Exception:
        return ""


def reference_depth_hint(path: str) -> str:
    """
    A short, measured statement of how DEEP the reference actually is, for the
    generation and plan prompts.

    Why this exists: `_FORMAT_DETAIL` said "15-40 data rows" per sheet, and the
    generator duly produced 24 rows on every single module sheet — twelve sheets,
    identical depth. The reference it was modelled on varies 22-28 by module and
    puts 288 rows in its primary sheet. Uniform depth is the signature of a model
    satisfying a quota rather than sizing to scope, and a flat range invites exactly
    that. Real counts, plus an explicit ban on uniformity, replace the guess.

    Returns "" when the reference cannot be measured; callers then keep today's
    generic range.
    """
    if not path or not os.path.exists(path):
        return ""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    inspector = _INSPECTORS.get("xlsx" if ext in ("xlsx", "xlsm") else ext)
    if inspector is None:
        return ""
    try:
        st = inspector(path)
    except Exception:
        return ""

    per = st.get("rows_per_container") or []
    names = st.get("names") or []

    # Decks are measured differently. _inspect_pptx counts non-empty paragraphs, and a
    # deck that draws each table cell as its own shape reports 307 for 65 real rows —
    # so this block was telling the planner to build a deck 4.7x denser than the
    # reference, and naming the 5-row COVER as the "primary slide that must be
    # substantially the deepest". Band counting measures rows as rows.
    if ext == "pptx":
        band = deck_rows_per_slide(path)
        if band and len(band) == len(per):
            per = band
        # Slides carrying no rows at all (the cover) say nothing about depth.
        keep = [(n, r) for n, r in zip(names, per) if r > 0]
        names = [n for n, _ in keep]
        per = [r for _, r in keep]

    if not per:
        return ""

    unit = {"xlsx": "sheet", "docx": "section", "pptx": "slide"}.get(
        "xlsx" if ext in ("xlsx", "xlsm") else ext, "section")

    lines = [f"MEASURED REFERENCE DEPTH — counted from the reference file, not estimated:",
             f"  {len(per)} {unit}s, {sum(per)} rows in total."]
    if len(per) > 1:
        primary_n, primary_name = per[0], (names[0] if names else "the first")
        rest = sorted(per[1:])
        median = rest[len(rest) // 2]
        lines.append(f"  Primary {unit} '{primary_name}': {primary_n} rows.")
        lines.append(f"  Remaining {unit}s: {min(rest)}-{max(rest)} rows "
                     f"(median {median}) — they are NOT all the same size.")
    lines.append(
        f"  Match this shape. Size each {unit} to ITS OWN scope: a module with more "
        f"in-scope work gets more rows than one with less. Producing the same row "
        f"count for every {unit} is a defect — it means you filled a quota instead "
        f"of reflecting what the source actually contains.")
    if ext == "pptx":
        # These counts are what the reference's layout holds on ONE page — a page
        # budget, not a content budget. Stating them as a ceiling made the deck drop
        # 19 rows of activities to stay inside it; stating them as a per-page figure
        # keeps the layout honest while letting real scope spill onto another page.
        lines.append(
            f"  These are PER-PAGE figures — what the reference's layout holds on one "
            f"page — and they are a FLOOR for this project, not a ceiling. This "
            f"project's scope is larger than the worked example's, so expect to exceed "
            f"them. Two priorities, in order: FIRST carry every in-scope row — never "
            f"drop or summarise rows to stay inside a page count; THEN fit them into as "
            f"few pages as possible, by raising density per page (coded cells, short "
            f"labels, more columns) before adding a page.")
    else:
        lines.append(
            f"  The primary {unit} aggregates the others and must be substantially "
            f"the deepest.")
    return "\n".join(lines)


def reference_volume_floors(path: str, output_format: str) -> dict:
    """
    Volume floors for the structure validator, MEASURED from the reference deck.

    Replaces MIN_VOLUME["pptx"], which says `min_items: 10` and nothing about depth.
    That pairing is backwards for a deck modelled on a reference: it makes ten thin
    slides valid and four dense ones a MAJOR defect. Measured consequence — the
    generator produced the four slides that were asked for, this check demanded ten,
    and rework appended six thin ones. The count is pinned by the instructions and the
    plan; what the validator should be guarding is DENSITY.

    So:
      min_items       2   — a title slide plus at least one content slide. Only catches
                            a degenerate deck; it can no longer inflate the count.
      min_total_rows  120% of the reference's measured DATA ROWS.

    ROWS, NOT CELLS — the distinction cost a whole run. `_inspect_pptx` counts
    non-empty PARAGRAPHS, and this reference draws every table cell as its own shape,
    so it reports 307 for a deck whose real depth is 65 rows. A floor derived from
    that number (45% of 307 = 138) demanded 2.1x the reference's density against the
    plan's `rows` arrays, which are data rows. Measured consequence: the plan produced
    a 79-row RACI where the reference has 28, the authoring agent authored a faithful
    61 rows, and the gate rejected its work as "lost content: 61 vs 138" — so the run
    fell back to the plain renderer and emitted tables that overflow the slide.

    Rows are counted as horizontal bands holding 2+ shapes, which is what a table row
    physically is. Measured here: 65 rows (8/28/16/13 across the content slides),
    Claude Desktop 63.

    WHY ABOVE 100%: the reference is the floor of acceptable detail, not the ceiling.
    The deliverable is built from a real project's scope, which routinely has more to
    say than the worked example — and the stated priority for this deliverable is
    DETAIL, with one or two extra slides explicitly acceptable if the content needs
    them. Measured: at 60% (39 rows) the deck came back with 59 rows and a RACI
    consolidated onto one page by DROPPING 19 rows of activities; the 78-row version
    across an extra slide was judged materially better. 120% of 65 = 78.

    The model lands on whatever floor it is given (a floor of 138 produced a plan of
    exactly 138), so this number effectively sets the density. It stays reachable
    because validate() now allows up to two slides beyond the plan: 6 slides of this
    reference's layouts hold well over 100 rows.

    Deliberately NOT `min_rows`: a pptx title slide has no `rows` array at all, and the
    per-item branch reports a missing array as MAJOR — an unfixable finding on every
    single deck.

    Returns {} for anything but a readable pptx reference, so every other format and
    every unmeasurable file keeps the shared table.
    """
    if (output_format or "").lower() != "pptx":
        return {}
    if not path or not os.path.exists(path) or not path.lower().endswith(".pptx"):
        return {}
    total = sum(deck_rows_per_slide(path))
    if total <= 0:
        return {}
    # 60%, not 70%: a 3-slide cut of this deck (cover + RACI + cadence) has 44 rows
    # available and 70% of 65 is 45 — unreachable by one row.
    return {"min_items": 2, "min_total_rows": max(12, int(total * 1.20))}


def deck_rows_per_slide(path: str) -> list:
    """
    Data rows per slide, counted as horizontal bands holding 2+ shapes.

    A table row IS a set of shapes sharing a top coordinate, so this measures the same
    thing the plan's `rows` arrays describe. Bands of a single shape are titles,
    captions and logos, not rows.

    Exists because _inspect_pptx counts non-empty PARAGRAPHS, which on a deck that
    draws each cell as its own shape is a CELL count: 307 for a deck of 65 rows. Both
    the validator floor and the depth hint were built on that number and both were
    wrong by 4-5x. Returns [] when the deck cannot be read.
    """
    try:
        from pptx import Presentation
        from pptx.util import Emu
        import collections

        out = []
        for slide in Presentation(path).slides:
            bands = collections.Counter()
            for sh in slide.shapes:
                try:
                    bands[round(Emu(sh.top).inches, 1)] += 1
                except Exception:
                    pass
            out.append(sum(1 for n in bands.values() if n >= 2))
        return out
    except Exception:
        return []


# ── The authoring brief ──────────────────────────────────────────────────────

_DECK_CLONE_RULES = """
CLONE THE REFERENCE AND REPOPULATE IT. DO NOT BUILD A NEW FILE.
This is the single most important instruction here, and getting it wrong has already
produced a rejected artefact. A previous attempt built a deck from scratch with the
library's defaults: 42 KB, one slide master, eleven layouts, zero images, every slide
on a blank layout. The reference it was modelled on has two masters, 62 layouts, 31
embedded images and a specific brand palette. It scored well on content volume and was
visually worthless, because a deck's value IS its design.

So:

1. START BY COPYING THE REFERENCE.
   `shutil.copy2(reference, output)` as the FIRST thing build.py does. Everything
   after that edits the copy in place. Never call `Presentation()` with no argument,
   and never `Document()` with no argument — that creates a blank file with library
   defaults and throws the design away.

2. EDIT TEXT INSIDE EXISTING SHAPES.
   Walk the copied file's slides/paragraphs and REPLACE THE TEXT of the shapes that
   are already there. Keep the shape, its position, size, fill, font and colour;
   change only the characters. Assign to `run.text` where a run exists rather than
   rebuilding the text frame, because rebuilding drops the run's formatting.

3. REUSE THE REFERENCE'S OWN STRUCTURES.
   The reference's slides are archetypes — a title slide, a tier/structure slide, a
   matrix, a cadence-plus-escalation slide, a contacts slide. Map the content contract
   onto those archetypes. If the contract has more material than the archetypes hold,
   DUPLICATE an existing slide (copy its XML) and repopulate the copy; do not invent a
   new layout. If it has less, delete the surplus slide.

4. NEVER DELETE ALL THE SLIDES.
   Removing the reference's slides and adding fresh ones loses every grouped shape,
   image and colour on them. That is the from-scratch failure wearing a disguise.

5. KEEP WHAT CARRIES THE BRAND.
   Slide masters, layouts, theme parts, embedded images, logos, footers, page numbers
   and slide dimensions must be byte-identical to the reference on the way out. The
   only differences between reference and output should be the words.

6. SUBSTITUTE PROJECT SPECIFICS, NOT DESIGN.
   Client name, dates, module names, role names, counts — those come from the content
   contract. Colours, fonts, geometry and imagery come from the reference. If the
   reference marks a value as [TBC], leaving it [TBC] is correct.

7. VERIFY BEFORE YOU FINISH.
   Re-open the output and assert: same slide dimensions as the reference, same number
   of slide masters, image count not lower, and the reference's brand colours still
   present. Print those numbers. If any of them dropped, you built rather than cloned —
   start again from step 1.
"""

_XLSX_MODEL_RULES = """
BUILD A WORKING MODEL, NOT A PICTURE OF ONE
This is the part that previous attempts got wrong, so read it carefully. The output
was rejected before because it looked right and behaved like a dead table: every
date was a text string, there was not one formula in it, and the sheets had no
relationship to each other. A reader could not re-baseline it, sort it, or trust a
single total. Avoid all of that:

1. TYPED CELLS, NEVER TEXT.
   Write dates as real `datetime.date`/`datetime` objects and apply a date
   number_format. `ws.cell(r, c, "04-May-2026")` is a DEFECT — it cannot sort,
   filter, feed a chart, or be used in arithmetic. Durations, counts and
   percentages must be real numbers, not strings.

2. A DRIVER CELL.
   Put the project start date in ONE cell near the top of the primary sheet and
   derive every other date from it. Changing that single cell must reflow the whole
   plan. Reference it absolutely (e.g. `=$F$2`).

3. A DEPENDENCY CHAIN, NOT A LIST OF DATES.
   Task end   = `=WORKDAY(<start>, <duration>-1)`
   Next start = `=WORKDAY(<predecessor end>, 1)`
   Working-day arithmetic, so nothing lands on a weekend. Where the content
   contract names a predecessor, the formula must actually point at that row.

4. SUMMARY ROWS COMPUTED FROM THEIR CHILDREN.
   A phase/section row must never hardcode its dates or duration:
   start = `=MIN(<child start range>)`, end = `=MAX(<child end range>)`,
   duration = `=NETWORKDAYS(<start>,<end>)`, progress = `=AVERAGE(<child range>)`.

5. ONE LINKED WORKBOOK.
   Detail sheets must anchor to the primary sheet by formula
   (e.g. `='Master Plan'!F72`), not repeat a copied date. Moving the primary sheet
   must move the detail sheets with it.

6. DATA VALIDATION.
   Apply a dropdown to any status/decision column, using the option list from the
   reference analysis where one exists. Use this exact list for status columns:
   "Confirmed,TBC,Out of Scope,Not started,In progress,Done,Blocked"

7. CONDITIONAL FORMATTING — ROW-LEVEL COLOUR UPDATE.
   After adding the DataValidation dropdown, add ConditionalFormatting rules so that
   changing a Status cell live in Excel immediately recolours the whole row.
   This is mandatory for any sheet that has a Status column — it is the feature that
   makes the dropdown useful. Apply it with this exact pattern (adjust col_letter and
   last_row to match the sheet):

   from openpyxl.formatting.rule import FormulaRule
   from openpyxl.styles import PatternFill
   _STATUS_CF = [
       ("Confirmed",    "C6EFCE"),   # green
       ("Done",         "D4EDDA"),   # green
       ("In progress",  "D6E4F7"),   # blue
       ("Not started",  "F5F5F5"),   # light grey
       ("TBC",          "FFF3CD"),   # amber
       ("Out of Scope", "E0E0E0"),   # grey
       ("Blocked",      "F8D7DA"),   # red
   ]
   row_range = f"A2:{get_column_letter(n_cols)}{last_data_row}"
   for _sv, _hx in _STATUS_CF:
       ws.conditional_formatting.add(
           row_range,
           FormulaRule(
               formula=[f'${col_letter}2="{_sv}"'],
               fill=PatternFill("solid", fgColor=_hx),
           ),
       )

   The formula uses an absolute column ($K) but a relative row (2), so Excel evaluates
   it per row across the whole range. The colours match the static fills applied at
   generation time so the sheet looks the same at open time as when Status is changed.

8. FORMATTING.
   Column widths, frozen header row, number formats and fills as measured in the
   reference analysis. No default styling anywhere.
"""


def build_brief(plan: Dict[str, Any], output_format: str, node_label: str,
                reference_name: str, expect: Dict[str, Any],
                reference_analysis: str = "", workdir: str = "") -> str:
    fmt = (output_format or "").lower()
    lib = _LIB_FOR_FORMAT.get(fmt, "openpyxl")
    out_name = f"output.{fmt}"

    # Absolute paths, not "./plan.json".
    #
    # The Read/Write/Glob tools require ABSOLUTE paths. Bash inherits the sandbox as
    # its cwd so relative paths work there, but the file tools do not — so a brief
    # written in "./" terms forced the model to GUESS the absolute location. It
    # guessed the project root, failed, and then spent its entire budget globbing
    # the tree looking for the files. A 3-sheet fixture burned 420s without ever
    # writing build.py. Naming the directory removes that failure mode outright.
    wd = os.path.abspath(workdir) if workdir else ""
    def _p(name: str) -> str:
        return os.path.join(wd, name) if wd else f"./{name}"

    plan_path, out_path_s = _p("plan.json"), _p(out_name)
    build_path = _p("build.py")
    ref_path = _p(reference_name) if reference_name else ""
    where = (f"WORKING DIRECTORY: {wd}\n"
             f"All paths below are absolute and already correct — use them verbatim. "
             f"Do NOT search for these files; they are exactly where this says.\n\n") if wd else ""

    _is_deck = fmt in ("pptx", "docx")

    if _is_deck:
        # Decks are CLONED, so the wording below must not tell the agent to ignore the
        # reference's structure — that is precisely what it has to keep.
        _provenance = (
            "The reference is a real deliverable from a PREVIOUS engagement, and for "
            "this format it is the STARTING MATERIAL, not just a style guide.\n"
            "Take from it: the file itself — masters, layouts, images, colours, fonts, "
            "geometry, footers, and the shape and order of its slides/sections.\n"
            "Do NOT take from it: project content. Every name, date, figure and scope "
            "item must come from the content contract. Its project is not this "
            "project — but its DESIGN is this project's design.\n\n"
        )
    else:
        _provenance = (
            "The reference is a real deliverable from a PREVIOUS engagement. It is your "
            "guide to HOW this artefact looks and behaves, and nothing else.\n"
            "Take from it: visual style, column widths, header treatment, formula "
            "patterns, number/date formats, level of detail per row.\n"
            "Do NOT take from it: scope, sheet list, or any project content. Its "
            "project is not this project.\n\n"
        )

    if _is_deck and reference_name:
        # The binary is always shipped for decks, so this branch wins over the
        # analysis-only one below.
        ref_block = f"REFERENCE FILE (your starting point): {ref_path}\n" + _provenance
        if reference_analysis:
            ref_block += reference_analysis + "\n\n"
        inspect_step = (
            f"1. Copy {ref_path} to {out_path_s} FIRST, then inspect the copy with "
            f"{lib} via Bash (never with Read — it is a binary). List its slides/"
            f"sections, the shapes on each, and its colours, so you know what you are "
            f"repopulating.\n")
    elif reference_analysis:
        # The measured analysis IS the reference. No binary is shipped in this case,
        # so there is nothing to open and no way to stall on it.
        ref_block = _provenance + reference_analysis + "\n\n"
        inspect_step = ("1. The reference has ALREADY been measured — the analysis "
                        "above is complete and authoritative. There is no reference "
                        "file to open. Go straight to step 2.\n")
    elif reference_name:
        ref_block = f"REFERENCE FILE: {ref_path}\n" + _provenance
        inspect_step = (f"1. Inspect {ref_path} with {lib} via Bash (never with Read — "
                        f"it is a binary). Note its conventions, then move on.\n")
    else:
        ref_block = (
            "There is no reference file. Apply professional delivery-document "
            "conventions: a clear header band, frozen header row, sensible column "
            "widths, consistent number and date formats, and no default styling.\n\n"
        )
        inspect_step = "1. (No reference to inspect.)\n"

    if fmt == "xlsx":
        model_rules = _XLSX_MODEL_RULES
    elif _is_deck and reference_name:
        model_rules = _DECK_CLONE_RULES
    else:
        model_rules = ""

    per_sheet = ""
    names = expect.get("names") or []
    per_row = expect.get("rows_per_container") or []
    if names and per_row:
        per_sheet = "\n".join(
            f"    - {n}: at least {r} rows" for n, r in zip(names, per_row))
        per_sheet = ("- Per sheet/section minimums (a shortfall in ANY one is a "
                     "rejection, even if the total is fine):\n" + per_sheet + "\n")

    # DETAIL WINS over slide count. An earlier version of this rule made the count
    # near-exact and told the agent to consolidate first; it obeyed, and consolidated a
    # RACI onto one page by dropping 19 rows of activities. Losing content to protect a
    # slide count is the wrong trade for this deliverable.
    count_rule = ""
    if _is_deck:
        want_c = int(expect.get("containers") or 0)
        count_rule = (
            f"TWO PRIORITIES, IN THIS ORDER. Apply the second only after the first is "
            f"fully satisfied.\n"
            f"\n"
            f"  PRIORITY 1 — DETAIL. Every row in the content contract appears in the "
            f"output. This is not negotiable and it outranks everything below. Never "
            f"drop, merge away, summarise or truncate content to make a layout work. "
            f"If you find yourself deciding what to leave out, you have already failed "
            f"this brief.\n"
            f"\n"
            f"  PRIORITY 2 — FEWEST SLIDES THAT HOLD IT. With the full detail fixed by "
            f"priority 1, use as FEW slides as can carry it. Reach for density before "
            f"you reach for another page: shorter labels, coded single-letter cells, "
            f"colour instead of words, tighter rows, more columns. The reference fits a "
            f"{want_c}-slide story precisely because it does this. A new page is the "
            f"LAST resort, never the first.\n"
            f"\n"
            f"  SPLIT INTO SIDE-BY-SIDE PANELS BEFORE SPLITTING ACROSS PAGES.\n"
            f"  A long table does not need a second slide — it needs a second COLUMN. "
            f"Put rows 1..n/2 in a panel on the left and the rest in an identical panel "
            f"on the right, each with its own header row. This is the reference's own "
            f"device: its contacts slide is two panels divided at 51% of the width. "
            f"Measured on this deck's layout: one column holds about 28 rows at the "
            f"reference's 0.25in pitch, and two panels hold about 59 at 0.18in with "
            f"6pt text — which is the smallest size the reference itself already uses. "
            f"So a 59-row matrix belongs on ONE slide as two panels, not on three "
            f"pages. Only when two panels genuinely overflow should a continuation "
            f"page appear.\n"
            f"\n"
            f"So: {want_c} slides carrying everything is the ideal outcome. "
            f"{want_c + 1} or {want_c + 2} carrying everything is fine when the content "
            f"genuinely will not compress further — title such a page as a deliberate "
            f"continuation of its subject. {want_c} slides that dropped rows to get "
            f"there is a FAILURE, and so is spreading content thin across "
            f"{want_c + 2} pages that would have fit on {want_c}.\n"
            f"Beyond {want_c + 2} slides the file is rejected.\n\n")

    return (
        f"Author a professional '{node_label}' as a real .{fmt} file.\n\n"
        f"{where}"
        f"CONTENT CONTRACT: {plan_path}\n"
        f"Already researched, planned and reviewed. It is the WHAT, and it is "
        f"settled — every sheet/section in it must appear in your output, with at "
        f"least the rows it specifies.\n\n"
        f"{ref_block}"
        f"{model_rules}\n"
        f"{count_rule}"
        f"YOUR TASK — WORK INCREMENTALLY, DO NOT DESIGN IT ALL UP FRONT\n"
        f"Build this one sheet at a time, running the script after each. Do NOT plan "
        f"the entire workbook before writing code: a previous attempt spent over six "
        f"minutes reasoning before its first line and ran out of budget with nothing "
        f"on disk. Get a working file early, then extend it.\n"
        f"{inspect_step}"
        f"2. Read {plan_path}.\n"
        f"3. Write {build_path} covering the FIRST sheet only, with its real "
        f"formatting, typed dates and formulas.\n"
        f"4. Run it: `python \"{build_path}\"` (try `py` if `python` is not found), "
        f"and confirm {out_path_s} opens.\n"
        f"5. Extend {build_path} with the next sheet and re-run. Repeat until every "
        f"sheet in the contract is present.\n"
        f"6. Finally, re-open {out_path_s} and CHECK it against the list below — "
        f"count sheets, rows per sheet and formulas, and confirm the dates are date "
        f"objects rather than strings. Fix and re-run until it passes. Do not report "
        f"success without having verified it.\n\n"
        f"ACCEPTANCE CHECKLIST — rejected unless all hold:\n"
        + (f"- Between {expect.get('containers', 0)} and "
           f"{int(expect.get('containers') or 0) + 2} slides. Use the extra pages if "
           f"they let you carry more real detail; more than "
           f"{int(expect.get('containers') or 0) + 2} is a rejection.\n"
           f"- NOTHING from the content contract has been dropped or summarised away "
           f"to save space. This is checked row by row.\n"
           if _is_deck else
           f"- At least {expect.get('containers', 0)} sheets/sections, one per entry in "
           f"the content contract.\n")
        +
        f"- At least {expect.get('rows', 0)} populated rows in total.\n"
        f"{per_sheet}"
        f"- Every cell value comes from the content contract. Invent NOTHING. If the "
        f"contract is thin somewhere, render what is there — do not pad with "
        f"plausible-looking filler.\n"
        + ("- Derived cells are FORMULAS, not literals, per the model rules above.\n"
           "- Dates are date objects with a date number_format, not strings.\n"
           if fmt == "xlsx" else "")
        + f"- The file opens cleanly.\n\n"
        f"RULES\n"
        f"- Write ONLY {build_path} and {out_path_s}. Nothing outside the working "
        f"directory.\n"
        f"- The final file MUST be at {out_path_s} — that exact path.\n"
        f"- Do not ask questions; you have everything you need. Work to completion.\n"
        f"- When done, reply with one line: sheets, total rows, and formula count.\n"
    )


# ── Entry point ──────────────────────────────────────────────────────────────

async def author_document(
    *,
    plan: Dict[str, Any],
    output_format: str,
    out_path: str,
    node_label: str,
    grounding_path: str = "",
    model: Optional[str] = None,
    run_id: str = "",
    emit: Optional[Callable[..., Awaitable[None]]] = None,
    session_id: str = "",
    node_id: str = "",
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Try to author `out_path` directly. Returns (accepted, reason, stats).

    On False the caller MUST fall back to templates.generate() — this function
    guarantees nothing about out_path unless it returns True.
    """
    fmt = (output_format or "").lower()
    if not is_enabled(fmt):
        return False, f"authoring disabled for .{fmt} (mode={_MODE})", {}

    expect = _plan_expectations(plan, fmt)
    started = time.time()

    # Decide affordability BEFORE spending anything. A run that cannot finish in
    # the allowance is not worth starting: the fallback produces the same file
    # either way, so an abandoned attempt is pure loss.
    timeout_s, refusal = budget_for(int(expect.get("containers") or 0), fmt,
                                    rows=int(expect.get("rows") or 0))
    if refusal:
        return False, refusal, {"containers": expect.get("containers", 0)}

    sweep_stale_sandboxes()

    sandbox = _SANDBOX_ROOT / (run_id or f"run_{int(started * 1000)}")
    if sandbox.exists():
        _safe_rmtree(sandbox)
    sandbox.mkdir(parents=True, exist_ok=True)

    try:
        # Only the plan and the reference go into the sandbox. C2: the reference is
        # handed over as a FILE, not as data_only text — that text pass loses every
        # fill, width, format and formula, which is precisely what we came for.
        (sandbox / "plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

        reference_name = ""
        ref_analysis = ""
        ref_stats: Dict[str, Any] = {}
        if grounding_path and os.path.exists(grounding_path):
            ext = os.path.splitext(grounding_path)[1].lower().lstrip(".")
            if ext in ("xlsx", "xlsm", "docx", "pptx"):
                # Measured in-process: exact, free, and it removes the discovery
                # turns the agent used to spend rediscovering all of it.
                ref_analysis = analyse_reference(grounding_path)
                ref_stats = reference_stats(grounding_path)

                # DECKS AND DOCUMENTS ALWAYS GET THE BINARY. Workbooks still do not.
                #
                # For xlsx the measured analysis genuinely is enough — the agent builds
                # a workbook from scratch and only needs the conventions, and shipping
                # the file made it stall reading a binary instead of writing build.py.
                #
                # For pptx/docx that reasoning is exactly backwards. The value of a
                # reference deck is its masters, layouts, embedded images, header bands
                # and per-shape brand colours, and none of that can be reconstructed
                # from a description — it has to be COPIED. Withholding the file forced
                # a from-scratch build: measured against the real Governance Matrix
                # reference, authoring returned a 42 KB deck with one master, eleven
                # layouts and zero images, where the reference has two masters, 62
                # layouts and 31 images. The design was simply gone.
                if ext in ("pptx", "docx") or not ref_analysis:
                    reference_name = f"reference.{ext}"
                    shutil.copy2(grounding_path, sandbox / reference_name)

        brief = build_brief(plan, fmt, node_label, reference_name, expect,
                            ref_analysis, workdir=str(sandbox))

        from llm_client import author_file, usage_scope, usage_summary
        scope = f"authoring/{run_id or 'run'}"

        # Heartbeat out to the UI. This is the longest stage a user ever waits on, so
        # it reports each step it takes. Purely observational — it cannot influence
        # what the agent builds.
        started_steps = time.time()

        async def _on_step(info: Dict[str, Any]) -> None:
            if emit is None:
                return
            try:
                await emit(session_id, "qp_authoring_step", {
                    "node_id": node_id,
                    "step": info.get("step", 0),
                    "detail": info.get("detail", ""),
                    "elapsed": round(time.time() - started_steps, 1),
                    "budget_s": timeout_s,
                })
            except Exception:
                pass

        try:
            with usage_scope(scope):
                await asyncio.wait_for(
                    author_file(brief=brief, workdir=str(sandbox), model=model,
                                max_turns=AUTHOR_MAX_TURNS,
                                max_budget_usd=AUTHOR_MAX_USD or None,
                                on_step=_on_step if emit is not None else None),
                    timeout=timeout_s,
                )
        except asyncio.TimeoutError:
            # SALVAGE BEFORE DISCARDING.
            #
            # The brief has the agent copy the reference to output.pptx as its FIRST
            # action and repopulate in place, so by the time a timeout fires there is
            # usually a real file on disk. Throwing it away unread meant a 29.5 minute
            # clone was replaced by the basic renderer's output — strictly worse than
            # what was already sitting in the sandbox.
            #
            # It is only used if it passes the SAME two gates a completed run must pass:
            # content volume against the plan, and design fidelity against the
            # reference. A half-repopulated deck fails the first (it still holds the
            # reference's slide count, not the plan's), so a file carrying a previous
            # engagement's text can never be shipped by this path.
            partial = sandbox / f"output.{fmt}"
            if partial.exists() and partial.stat().st_size > 0:
                p_ok, p_why, p_stats = validate(str(partial), fmt, expect, ref_stats)

                # A half-built file is the ONE case where the package itself may be
                # inconsistent, so it gets a check a completed run does not need.
                # Measured: a deck salvaged from a 1480s timeout passed validate()
                # (6 containers, rows fine) and PowerPoint refused to open it.
                if p_ok:
                    i_ok, i_why = package_integrity(str(partial), fmt)
                    p_ok, p_why = (p_ok and i_ok), (p_why or i_why)

                # Design fidelity must be MEASURED, not merely non-failing. It returns
                # "pass" for an unmeasurable pair so that a broken check can never block
                # a good file — correct for the normal path, wrong here, where it left
                # the salvage gated on validate() alone. The run that shipped a corrupt
                # deck logged design=None: nothing was ever compared.
                if p_ok:
                    if grounding_path and os.path.exists(grounding_path):
                        d_ok, d_why, d_meas = design_fidelity(str(partial),
                                                              grounding_path, fmt)
                        p_stats["design"] = d_meas or None
                        if not d_ok:
                            p_ok, p_why = False, (p_why or d_why)
                        elif not d_meas:
                            p_ok, p_why = False, ("design fidelity could not be "
                                                  "measured on a partial file")
                    else:
                        p_ok, p_why = False, ("no reference available to verify a "
                                              "partial file against")
                if p_ok:
                    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(partial, out_path)
                    p_stats.update(_cost(scope))
                    p_stats["seconds"] = round(time.time() - started, 1)
                    p_stats["salvaged_after_timeout"] = True
                    return True, f"accepted (salvaged after {timeout_s}s timeout)", p_stats
                stats = _cost(scope)
                stats["timeout_partial_rejected"] = p_why
                return False, (f"authoring timed out after {timeout_s}s; the partial "
                               f"file was not usable ({p_why})"), stats
            return False, f"authoring timed out after {timeout_s}s", _cost(scope)

        produced = sandbox / f"output.{fmt}"
        accepted, reason, stats = validate(str(produced), fmt, expect, ref_stats)
        stats["seconds"] = round(time.time() - started, 1)
        stats.update(_cost(scope))

        # Content volume is not enough for a cloned artefact. A deck that kept every
        # row and threw away the masters, layouts and images passed the check above and
        # was visually worthless — so design fidelity is gated separately, and only
        # where there is a reference to compare against.
        if accepted and grounding_path and os.path.exists(grounding_path):
            d_ok, d_reason, d_stats = design_fidelity(str(produced), grounding_path, fmt)
            if d_stats:
                stats["design"] = d_stats
            if not d_ok:
                return False, d_reason, stats

        if not accepted:
            return False, reason, stats

        # Only now does the authored file replace what templates.py would have made.
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(produced, out_path)
        return True, "accepted", stats

    except Exception as e:
        return False, f"authoring error: {str(e)[:160]}", {}
    finally:
        # Keep the sandbox when authoring is being debugged; otherwise reclaim it.
        # If Windows still holds the directory, the next run's sweep collects it.
        if os.environ.get("PROJECTZEN_AUTHORING_KEEP") != "1":
            _safe_rmtree(sandbox)


def _cost(scope: str) -> Dict[str, Any]:
    """What this authoring attempt actually consumed. Never raises."""
    try:
        from llm_client import usage_summary
        u = usage_summary(scope)
        return {"tokens_in": u.get("billable_input", 0),
                "tokens_out": u.get("output", 0),
                "cost_usd": u.get("cost_usd", 0.0),
                "llm_calls": u.get("calls", 0)}
    except Exception:
        return {}
