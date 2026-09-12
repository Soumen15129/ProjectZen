"""
scope_gate.py — deterministic scope and depth enforcement for adhoc generation.

WHY THIS EXISTS
---------------
Scope coverage was asked for in the prompt and the model complied inconsistently.
Measured on the SAME input (SP051.pptx) and the same reference, with only prompt
wording changing between runs:

    10:38  ->  15 sheets, 0 in-scope modules without a sheet, 429 rows
    12:38  ->  13 sheets, 2 without a sheet,                  407 rows
    17:14  ->  12 sheets, 3 without a sheet,                  357 rows

Three runs, a spread of three sheets and 72 rows, no code change between them.
Strengthening the instruction did not close the gap — the 17:14 run had the
strongest scope wording of the three and produced the worst coverage AND the
only row count below the reference (376 data rows). An instruction is a request
the model weighs against every other instruction; it is not a guarantee.

So coverage is checked in Python, after the plan exists, and a shortfall is sent
back once with the specific modules named. That converts a hope into a check.

NOT HARDCODED
-------------
Nothing here knows what a Salesforce module is. The expected list is whatever
`extract_project_context()` pulled out of THIS run's input document, and the
required SHAPE (a sheet per module vs. rows inside one sheet) is measured from
the reference template. A SOW that never mentions Benefits produces no Benefits
requirement; an RTM reference that is a single grid is never asked for tabs.

MATCHING IS DELIBERATELY GENEROUS
---------------------------------
A false positive here is worse than a false negative: telling the model it
omitted a module it actually included invites a duplicate sheet, while missing
one leaves today's behaviour. So a module counts as covered when ANY signal
fires, and only a module with no signal at all is reported.

ADHOC ONLY — imported from adhoc_pipeline.py. Cascade is unaffected.
"""

import copy
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# Sheets that are not module deliverables.
#
# Matched WHOLE WORD, not by prefix. The prefix version read 'Test Summary' as a
# module sheet — 'testsummary' does not start with 'summary' — which inflated the
# module-sheet count on every test-script workbook and made a phase-organised
# reference look module-organised.
_META_WORDS = {
    "master", "summary", "overview", "version", "history", "cover", "index",
    "contents", "toc", "guidance", "legend", "readme", "assumptions",
    "milestones", "raci", "glossary", "changelog", "dashboard",
    "instructions", "instruction", "open", "log",
}

# A word that carries no identity on its own — "EC Payroll" and "Payroll
# Reporting" must not match on "payroll" alone in the token test.
# 'planning' and 'management' are deliberately NOT here. Both carry a real
# letter in the acronyms this estate actually uses, and removing them broke
# the two hardest renames:
#   'Succession & Career Development Planning' -> {s,c,d,p} == 'CDP & SP'
#   'Performance and Goals Management'         -> {p,g,m}   == 'PMGM'
# Treating them as noise words dropped the 'p' and the 'm' and both modules
# then read as missing from workbooks that plainly contained them.
_STOPWORDS = {
    "and", "the", "of", "for", "in", "on", "to", "a", "an", "sf", "sap",
    "module", "modules", "system", "solution",
    "scope", "phase", "core", "standard", "process", "processes",
}

# How many of the reference's own sheets must name an in-scope module before we
# accept that it organises BY module. Two is enough to be a pattern rather than a
# coincidence, and it leaves a wide margin either way on the real references
# measured here (Project Plan 8, Test Script 0).
_MIN_MODULE_MATCHES_FOR_TABS = 2


# ── Normalisation helpers ────────────────────────────────────────────────────

def _norm(s: Any) -> str:
    """Lowercase, alphanumerics only. 'EC Payroll' -> 'ecpayroll'."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _words(s: Any) -> List[str]:
    return [w for w in re.split(r"[^a-zA-Z0-9]+", str(s or "").lower()) if w]


def _tokens(s: Any) -> Set[str]:
    """Significant words only — 3+ chars and not a stopword."""
    return {w for w in _words(s) if len(w) >= 3 and w not in _STOPWORDS}


def _initials(s: Any) -> Set[str]:
    """
    First letters of the significant words, as a SET.

    This is what catches the pairing prose renaming cannot:
    'Succession & Career Development Planning' -> {s, c, d}
    sheet 'CDP & SP'                            -> {c, d, p, s} ... plus the
    letters of its own words. Set overlap is compared rather than order,
    because a workbook writes the same module as 'CDP & SP' where the SOW
    writes 'Succession & Career Development Planning'.
    """
    out: Set[str] = set()
    for w in _words(s):
        if w in _STOPWORDS:
            continue
        if len(w) <= 4 and w.isalpha() and w == w.lower():
            # Short token such as 'ec', 'cdp', 'sp' — every letter is an initial
            out.update(w)
        else:
            out.add(w[0])
    return out


def _is_meta_sheet(name: str) -> bool:
    """True for cover pages, summaries, version logs — anything not a module."""
    return bool(set(_words(name)) & _META_WORDS)


# ── Matching ─────────────────────────────────────────────────────────────────

def score(module: str, sheet_name: str) -> int:
    """
    How strongly does `sheet_name` represent `module`? 0 = not at all.

    Scored rather than boolean because the obvious boolean version was wrong in
    a way that silently disabled the whole gate: with "one name inside the
    other" as a rule, 'EC Benefits' matched the 'EC' sheet and 'Reporting'
    matched 'RCM', so a workbook missing three modules reported full coverage.
    Scoring lets assign() give each sheet to the module that fits it BEST and
    leave the losers correctly unmatched.
    """
    m_norm, s_norm = _norm(module), _norm(sheet_name)
    if not m_norm or not s_norm:
        return 0

    # Same name
    if m_norm == s_norm:
        return 100

    mi, si = _initials(module), _initials(sheet_name)

    # Acronym equivalence, compared as SETS so word order does not matter:
    #   'Performance and Goals Management'         -> {p,g,m}   vs 'PMGM'    {p,m,g}
    #   'Succession & Career Development Planning' -> {s,c,d,p} vs 'CDP & SP'{c,d,p,s}
    #   'Employee Central'                         -> {e,c}     vs 'EC'      {e,c}
    if mi and si and mi == si:
        return 80

    # Containment, but only on a substantial string. The 4-char floor is what
    # stops 'ec' claiming 'EC Benefits' for the 'EC' sheet.
    if len(s_norm) >= 4 and s_norm in m_norm:
        return 60
    if len(m_norm) >= 4 and m_norm in s_norm:
        return 60

    # A shared significant word — 'Time Off & Time Tracking' / 'Time & Attendance'
    if _tokens(module) & _tokens(sheet_name):
        return 50

    return 0


_MATCH_THRESHOLD = 50


def matches(module: str, sheet_name: str) -> bool:
    """Pairwise convenience wrapper. assign() is what the gate actually uses."""
    return score(module, sheet_name) >= _MATCH_THRESHOLD


def assign(modules: Sequence[str],
           sheet_names: Sequence[str]) -> Tuple[Dict[str, str], List[str]]:
    """
    Give each module at most one sheet, best fit first, no sheet used twice.

    Returns (module -> sheet, unmatched modules).

    One-to-one is the point. 'EC' scores for both 'Employee Central' and
    'EC Benefits'; whichever fits better takes it and the other is reported,
    which is exactly the gap a per-module test cannot see.
    """
    pairs = []
    for m in modules:
        for s in sheet_names:
            sc = score(m, s)
            if sc >= _MATCH_THRESHOLD:
                pairs.append((sc, m, s))
    # Highest score first; ties resolved deterministically by name.
    pairs.sort(key=lambda p: (-p[0], str(p[1]), str(p[2])))

    taken_sheets: Set[str] = set()
    matched: Dict[str, str] = {}
    for sc, m, s in pairs:
        if m in matched or s in taken_sheets:
            continue
        matched[m] = s
        taken_sheets.add(s)

    unmatched = [str(m) for m in modules if str(m) not in matched]
    return matched, unmatched


# ── Reference shape ──────────────────────────────────────────────────────────

def reference_shape(grounding_path: str, modules: Sequence[str] = ()) -> str:
    """
    How does the reference organise itself?

      'tabs'    — a sheet per MODULE, so the output owes one too
      'rows'    — organised by something else (phase, scenario, a single grid);
                  structure is the reference's business, not the gate's
      'unknown' — nothing measurable; never enforce structure

    Decided on EVIDENCE — do the reference's own sheets name the modules this
    project is scoped for? — not on a sheet count. Counting was wrong in a way
    that mattered: the Test Script reference is organised by test PHASE
    ('SIT Test Cases', 'UAT Test Cases'), which cleared a >=3 threshold and made
    the gate demand a tab per module on a document whose reference deliberately
    has none. Measured on the two references in this estate:

        SF_Project_Plan_V3.xlsx        8 of 9 sheets name a module  -> tabs
        ClientX_SF_EC_Test_Script.xlsx 0 of 2 sheets name a module  -> rows

    Without `modules` there is no evidence either way, so the answer is
    'unknown' and the caller enforces nothing.
    """
    if not grounding_path or not grounding_path.lower().endswith((".xlsx", ".xlsm")):
        return "unknown"
    try:
        from openpyxl import load_workbook
        wb = load_workbook(grounding_path, read_only=True)
        try:
            names = list(wb.sheetnames)
        finally:
            wb.close()
    except Exception:
        return "unknown"

    if not names:
        return "unknown"

    mods = [str(m).strip() for m in (modules or []) if str(m or "").strip()]
    if not mods:
        return "unknown"

    non_meta = [n for n in names if not _is_meta_sheet(n)]
    if not non_meta:
        return "rows"

    matched, _ = assign(mods, non_meta)
    return "tabs" if len(matched) >= _MIN_MODULE_MATCHES_FOR_TABS else "rows"


def _measure_data_rows(path: str) -> int:
    """
    Data rows in a workbook, excluding one header per populated sheet.

    Shared by the reference floor and the prior-run floor so the two numbers
    are directly comparable — measuring them differently would make the
    max() below meaningless.
    """
    if not path or not path.lower().endswith((".xlsx", ".xlsm")):
        return 0
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True)
        try:
            total = sheets = 0
            for ws in wb.worksheets:
                used = sum(1 for r in ws.iter_rows()
                           if any(c.value not in (None, "") for c in r))
                if used:
                    total += used
                    sheets += 1
            return max(0, total - sheets)
        finally:
            wb.close()
    except Exception:
        return 0


def reference_row_floor(grounding_path: str) -> int:
    """
    Data rows the reference actually carries. 0 when unmeasurable, which
    disables the depth check rather than guessing.

    The 17:14 Project Plan produced 345 plan rows against this reference's 376
    — the only run to fall below it — which is what the user noticed as a
    smaller file. Detail is the stated priority, so the reference is a floor.
    """
    return _measure_data_rows(grounding_path)


# ── Prior-run floor ─────────────────────────────────────────────────────────
#
# WHY: the reference is not the only evidence of what this pipeline can produce.
# Measured on Test Script, same input, same reference:
#
#     reference             189 test cases / 223 data rows
#     06 Aug generation     244 test cases / 319 data rows
#     08 Aug generation     151 test cases / 220 data rows   <- 38% regression
#
# The 08 Aug run PASSED the depth check, because 220 clears 95% of the
# reference's 223. Anchoring only to the reference means the gate has no memory:
# a run two-thirds the size of what the same pipeline produced two days earlier
# looks perfectly fine. The best recent generation is the stronger floor.

_PRIOR_LOOKBACK      = 4      # most recent generations to measure
_PRIOR_TOLERANCE     = 0.90   # a prior run is evidence, not a contract — allow 10%
_PRIOR_CAP_MULTIPLE  = 1.5    # never demand more than 1.5x the reference


def prior_best_rows(template: str,
                    output_format: str,
                    reference_floor: int = 0,
                    db_path: Optional[str] = None) -> Tuple[int, str]:
    """(rows, filename) — see prior_best_run(), which also returns the path."""
    rows, name, _ = prior_best_run(template, output_format,
                                   reference_floor, db_path)
    return rows, name


def prior_best_run(template: str,
                   output_format: str,
                   reference_floor: int = 0,
                   db_path: Optional[str] = None) -> Tuple[int, str, str]:
    """
    Best data-row count among recent prior generations of the SAME template.

    Returns (rows, filename, full_path), or (0, "", "") when there is no usable
    history — a first run must never be blocked by a floor that does not exist
    yet. The path is returned so the caller can compare against that run's
    actual content, not just its size.

    The result is CAPPED at `reference_floor * _PRIOR_CAP_MULTIPLE` so one
    exceptional run cannot ratchet the requirement upward forever and turn
    every later run into a repair. Without that cap this becomes a slow
    escalation that costs a retry on every generation.

    Never raises.
    """
    if not template or not output_format:
        return 0, "", ""
    try:
        import sqlite3
        if db_path is None:
            from paths import DB_PATH
            db_path = str(DB_PATH)
        if not os.path.exists(db_path):
            return 0, "", ""

        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = con.execute(
                "SELECT file_path, file_name FROM documents "
                "WHERE template = ? AND output_format = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (template, output_format, _PRIOR_LOOKBACK),
            )
            rows = cur.fetchall()
        finally:
            con.close()
    except Exception:
        return 0, "", ""

    best, best_name, best_path = 0, "", ""
    for file_path, file_name in rows:
        try:
            if not file_path or not os.path.exists(file_path):
                continue
            n = _measure_data_rows(file_path)
            if n > best:
                best = n
                best_name = file_name or os.path.basename(file_path)
                best_path = file_path
        except Exception:
            continue

    if best <= 0:
        return 0, "", ""

    if reference_floor > 0:
        ceiling = int(reference_floor * _PRIOR_CAP_MULTIPLE)
        if best > ceiling:
            # The ROW FLOOR is capped so one exceptional run cannot ratchet the
            # requirement upward forever. The PATH is not — it still points at
            # the real file, which is what the drift comparison needs.
            return ceiling, f"{best_name} (capped from {best})", best_path
    return best, best_name, best_path


def recent_run_files(template: str,
                     output_format: str,
                     db_path: Optional[str] = None,
                     limit: int = _PRIOR_LOOKBACK) -> List[str]:
    """
    Paths of the most recent generations of this template, newest first.

    Used by dimension_balance() to tell an established dimension from one run's
    vocabulary drift. Read-only, never raises, [] when there is no history.
    """
    if not template or not output_format:
        return []
    try:
        import sqlite3
        if db_path is None:
            from paths import DB_PATH
            db_path = str(DB_PATH)
        if not os.path.exists(db_path):
            return []
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT file_path FROM documents "
                "WHERE template = ? AND output_format = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (template, output_format, int(limit)),
            ).fetchall()
        finally:
            con.close()
        return [r[0] for r in rows if r[0] and os.path.exists(r[0])]
    except Exception:
        return []


def combined_row_floor(reference_floor: int,
                       prior_rows: int) -> Tuple[int, str]:
    """
    The threshold a plan must clear, and a short reason for the log.

    The reference is held at 95% (it is the contractual shape of the artefact);
    a prior generation at 90% (it is evidence of capability, and some run-to-run
    variance is legitimate). The stricter of the two wins.

    ONE KNOWN APPROXIMATION, stated rather than hidden: both floors are measured
    from FILES, and they are compared against a PLAN. Authoring adds a title row
    per sheet, so a file runs about 4-5% above the plan it came from (measured:
    plan 423 -> file 439; plan 220 -> file 232). The floors are therefore ~5%
    stricter than they read. That is inside the 10% tolerance, and it errs
    toward demanding more detail, which is the stated priority for these
    deliverables — so it is left as is rather than corrected with a fudge factor
    that would need re-deriving every time the renderer changes.
    """
    ref_thr   = int(reference_floor * 0.95) if reference_floor else 0
    prior_thr = int(prior_rows * _PRIOR_TOLERANCE) if prior_rows else 0
    if prior_thr > ref_thr:
        return prior_thr, f"prior run ({prior_rows} rows, 90%)"
    return ref_thr, (f"reference ({reference_floor} rows, 95%)"
                     if ref_thr else "no floor measurable")


# ── Reference organisation, for non-module references ───────────────────────

def reference_organisation_hint(grounding_path: str) -> str:
    """
    Describe how a reference that is NOT module-organised divides its content,
    with measured volumes, so the generator preserves that dimension.

    WHY: the Test Script reference splits SIT (148 rows) from UAT (45 rows).
    Told only "follow the reference's structure", the generator kept its own
    module tabs and let UAT collapse from 34 cases to 9 — against a reference
    carrying 43. The split is the part that mattered and nothing named it.

    Returns "" when unmeasurable or when the reference has no such split.
    """
    if not grounding_path or not grounding_path.lower().endswith((".xlsx", ".xlsm")):
        return ""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(grounding_path, read_only=True)
        try:
            parts = []
            for ws in wb.worksheets:
                if _is_meta_sheet(ws.title):
                    continue
                used = sum(1 for r in ws.iter_rows()
                           if any(c.value not in (None, "") for c in r))
                if used > 1:
                    parts.append((ws.title, max(0, used - 1)))
        finally:
            wb.close()
    except Exception:
        return ""

    if len(parts) < 2:
        return ""

    total = sum(n for _, n in parts)
    if total <= 0:
        return ""

    lines = [
        "REFERENCE ORGANISATION — measured from the reference file, not estimated:",
        f"  It divides its content into {len(parts)} parts:",
    ]
    for name, n in parts:
        pct = round(100 * n / total)
        lines.append(f"    - {name}: {n} rows ({pct}% of its content)")
    lines.append(
        "  THIS SPLIT IS A REAL DIMENSION OF THE DELIVERABLE — preserve it.\n"
        "  You may lay the workbook out by module if that suits this project "
        "better, but every one of the parts above must still be represented, "
        "in roughly the proportions shown. Producing one of them richly and "
        "another as a token handful of rows is a DEFECT: it silently drops a "
        "whole class of the work the reference exists to cover.")
    return "\n".join(lines)


# ── Plan inspection ──────────────────────────────────────────────────────────

def _plan_sheets(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [s for s in (plan.get("sheets") or []) if isinstance(s, dict)]


def plan_row_count(plan: Dict[str, Any]) -> int:
    return sum(len(s.get("rows") or []) for s in _plan_sheets(plan))


# ── Per-module balance ──────────────────────────────────────────────────────
#
# WHY: the aggregate depth floor above says HOW MUCH, never WHERE. Measured on
# the 08 Aug 03:51 Test Script, which cleared the aggregate floor cleanly (314
# rows against 287) and was still wrong:
#
#     EC + EC Payroll                        ~143 cases   (57% of the file)
#     T&A, RCM, ONB, PMGM, Compensation        ~25 cases   (10% of the file)
#
# Those five are five of the SEVEN module groups the input names as in scope,
# and the input weights them as equal scope bullets. The total was met by
# pouring depth into EC Core while the rest were reduced to a token handful —
# the exact failure the aggregate floor cannot see, because from its point of
# view nothing is missing and the row count is fine.
#
# So: a module that is PRESENT but far below its peers is reported. A module
# with no rows at all is left to analyse() — that is absence, not imbalance,
# and double-reporting it would ask for the same thing twice.

_BALANCE_MIN_ROWS     = 60     # below this the document is too small to judge
_BALANCE_MIN_MODULES  = 3      # fewer than three peers is not a distribution
_BALANCE_FLOOR        = 0.40   # a module under 40% of the mean is starved
_BALANCE_ABS_FLOOR    = 3      # never demand more than a token few on tiny plans
_LABEL_MAX_CHARS      = 40     # longer than this is prose, not a category label


def module_row_counts(plan: Dict[str, Any],
                      modules: Sequence[str]) -> Dict[str, int]:
    """
    Rows attributed to each module, one row to at most ONE module.

    ATTRIBUTION IS BY LABEL, NOT BY ROW TEXT. The first version of this scored
    each module against the whole concatenated row, and it was wrong in both
    directions at once — verified against runs whose real coverage is known:

        Project Plan R4   'Employee Central (EC)'  ->  0 rows
                          (its tab is called 'EC'; the words 'employee
                           central' appear nowhere on it)
        Test Script OLD   'EC Payroll'             -> 64 rows
                          (its only significant token is 'payroll', so it
                           claimed every row of every sheet mentioning pay)

    R4 is the run measured at 11 of 11 modules covered, so a report of 0 was
    pure false positive. The fix is to score against SHORT LABELS — the sheet
    name and the row's own category cells — using the same score() that
    assign() already relies on, which resolves 'EC' -> 'Employee Central (EC)'
    through initials and refuses 'EC' -> 'EC Payroll'. Long prose cells are
    skipped entirely: they are where the spurious token matches came from.
    """
    mods = [str(m).strip() for m in modules if str(m or "").strip()]
    counts: Dict[str, int] = {m: 0 for m in mods}
    if not mods:
        return counts

    memo: Dict[Tuple[str, str], int] = {}

    def best_module(label: str) -> Tuple[Optional[str], int]:
        best, best_sc = None, 0
        for m in mods:
            key = (m, label)
            sc = memo.get(key)
            if sc is None:
                sc = score(m, label)
                memo[key] = sc
            if sc > best_sc:
                best, best_sc = m, sc
        return best, best_sc

    for sheet in _plan_sheets(plan):
        sheet_name = str(sheet.get("name") or "")
        if _is_meta_sheet(sheet_name):
            continue
        sheet_best, sheet_sc = best_module(sheet_name)
        for row in (sheet.get("rows") or []):
            if not isinstance(row, (list, tuple)):
                continue
            # A cell naming the module beats the tab it sits on: on a workbook
            # organised by phase, the tab says 'UAT Test Cases' and only the
            # Module cell says which module the row is really about.
            best, best_sc = None, 0
            for cell in row:
                if not isinstance(cell, str):
                    continue
                label = cell.strip()
                if not label or len(label) > _LABEL_MAX_CHARS:
                    continue
                m, sc = best_module(label)
                if sc > best_sc:
                    best, best_sc = m, sc
            if best_sc < _MATCH_THRESHOLD and sheet_sc >= _MATCH_THRESHOLD:
                best, best_sc = sheet_best, sheet_sc
            if best is not None and best_sc >= _MATCH_THRESHOLD:
                counts[best] += 1
    return counts


def module_balance(plan: Dict[str, Any],
                   modules: Sequence[str]) -> Dict[str, Any]:
    """
    Find in-scope modules that are present but starved of depth.

    `needs_repair` is True only when at least one module sits below 40% of the
    mean per-module row count, on a plan large enough for that mean to mean
    anything. Modules with zero rows are excluded — analyse() reports those as
    absent, and asking for them here as well would send the same instruction
    twice in one prompt.

    Never raises; on any doubt it returns a clean report.
    """
    empty = {"counts": {}, "starved": [], "mean": 0, "floor": 0,
             "needs_repair": False}
    try:
        mods = [str(m).strip() for m in modules if str(m or "").strip()]
        if len(mods) < _BALANCE_MIN_MODULES:
            return empty

        counts = module_row_counts(plan, mods)
        attributed = sum(counts.values())
        if attributed < _BALANCE_MIN_ROWS:
            return empty

        present = [m for m in mods if counts[m] > 0]
        if len(present) < _BALANCE_MIN_MODULES:
            return empty

        mean  = attributed / float(len(present))
        floor = max(_BALANCE_ABS_FLOOR, int(mean * _BALANCE_FLOOR))
        starved = sorted(
            ((m, counts[m]) for m in present if counts[m] < floor),
            key=lambda p: p[1])

        return {
            "counts":       counts,
            "starved":      starved,
            "mean":         int(mean),
            "floor":        floor,
            "needs_repair": bool(starved),
        }
    except Exception:
        return empty


# ── Categorical drift against the best prior run ────────────────────────────
#
# WHY: measured on the same 08 Aug Test Script, the Country column moved like
# this against the 06 Aug run it should have matched or beaten:
#
#     India 103 -> 45      UAE 37 -> 15      Nepal 30 -> 15      Egypt 23 -> 10
#     Global 50 -> 106
#
# Those four are the only countries with payroll, statutory rules and a
# parallel payroll run in scope — the highest-risk countries in the engagement
# — and half their coverage was reclassified as 'Global'. Row count, module
# coverage and the aggregate floor all stayed clean through it.
#
# ADVISORY ONLY, deliberately. This compares against one prior file and cannot
# know whether a shift is a regression or a legitimate re-cut of the document.
# It never triggers a repair on its own; it only adds detail to a repair that
# some other check already decided to send. That keeps a noisy signal from
# costing a generation call while still putting the numbers in front of the
# model when it is being asked to revise anyway.

_DRIFT_MIN_PRIOR   = 5     # ignore values too rare to reason about
_DRIFT_TOLERANCE   = 0.60  # a value under 60% of its prior count has collapsed
_DRIFT_MAX_DISTINCT = 25   # more distinct values than this is not categorical
_DRIFT_MAX_REPORT  = 6     # keep the instruction readable


def _is_categorical(values: List[str]) -> bool:
    """A column worth comparing: few distinct values, repeated often."""
    if len(values) < _DRIFT_MIN_PRIOR:
        return False
    distinct = len(set(values))
    return distinct <= _DRIFT_MAX_DISTINCT and distinct * 2 <= len(values)


def _plan_column_counts(plan: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """{header -> {value -> count}} across every sheet of a plan."""
    out: Dict[str, Dict[str, int]] = {}
    for sheet in _plan_sheets(plan):
        headers = [str(h).strip() for h in (sheet.get("headers") or [])]
        for row in (sheet.get("rows") or []):
            if not isinstance(row, (list, tuple)):
                continue
            for i, h in enumerate(headers):
                if not h or i >= len(row):
                    continue
                v = str(row[i]).strip() if row[i] is not None else ""
                if v:
                    out.setdefault(h, {})
                    out[h][v] = out[h].get(v, 0) + 1
    return out


def _file_column_counts(path: str) -> Dict[str, Dict[str, int]]:
    """Same shape as _plan_column_counts, read from a generated workbook."""
    out: Dict[str, Dict[str, int]] = {}
    if not path or not path.lower().endswith((".xlsx", ".xlsm")):
        return out
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                rows = [[("" if c.value is None else str(c.value).strip())
                         for c in r] for r in ws.iter_rows(max_row=400)]
                # Header row: the first row carrying at least three short
                # labels. Generated workbooks put a title above it, so row 1
                # is usually not it.
                hdr_i = None
                for i, r in enumerate(rows[:6]):
                    filled = [c for c in r if c]
                    if len(filled) >= 3 and all(len(c) <= 60 for c in filled):
                        hdr_i = i
                        break
                if hdr_i is None:
                    continue
                headers = rows[hdr_i]
                for r in rows[hdr_i + 1:]:
                    for i, h in enumerate(headers):
                        if not h or i >= len(r) or not r[i]:
                            continue
                        out.setdefault(h, {})
                        out[h][r[i]] = out[h].get(r[i], 0) + 1
        finally:
            wb.close()
    except Exception:
        return {}
    return out


# ── Rollup counts, computed instead of requested ────────────────────────────
#
# The summary sheet has been wrong in ALL EIGHT runs, and two attempts to fix it
# by instruction each failed in a different direction: the first produced wrong
# numbers, the rewrite produced blank cells, the next run produced wrong numbers
# again. Measured on the last two:
#
#     R7   'By Test Level - SIT' claimed 190 against an actual 274;
#          UAT 12 vs 34; PPT 6 vs 22; every 'By Country' row wrong
#     R8   SIT / UAT / PPT / TOTAL entirely blank, and the nine module rows
#          summed to 214 against an actual 311
#
# Counting is not something a language model does reliably over 300 rows it
# wrote across fourteen sheets, and no wording changes that. Python counts.
#
# WHY THIS IS SAFE WHERE AN EARLIER ATTEMPT WAS NOT
# --------------------------------------------------
# The first design looked for "the summary sheet" BY NAME and would have matched
# a Project Plan's 'Master Plan' — its primary content sheet — and written test
# counts into it. This one keys on a COLUMN literally named 'Total TCs', which
# is present in 8 of 8 Test Scripts and 0 of 5 Project Plans. No such column,
# no action at all. And it only ever writes that one column, only on rows whose
# label resolves to exactly one countable thing; anything ambiguous is left
# exactly as the model wrote it.

_TOTAL_TCS = "total tcs"

# 'By Test Level - SIT', 'Module: Data Migration', 'By Country - India' — the
# label carries its dimension as a prefix. Four runs used four different
# conventions, so the prefix is stripped rather than parsed.
_ROLLUP_PREFIX = re.compile(
    r"^\s*(?:by\s+)?(test\s*level|module|country|dimension|category|"
    r"scope\s*area|workstream)\s*[-–:/]\s*", re.I)

_ROLLUP_TOTAL = {"total", "grandtotal", "totaltcs", "alltests", "all"}

# Words that carry no identity in a rollup label — 'SIT Test' and 'UAT Test
# Cases' are the SIT and UAT levels wearing filler.
_ROLLUP_FILLER = {"test", "tests", "testing", "case", "cases", "scenario",
                  "scenarios", "count", "counts", "breakdown", "summary"}


def recompute_rollup(plan: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Fill the 'Total TCs' column of a rollup sheet with counts taken from the
    plan itself.

    Returns (plan, notes). The plan comes back untouched — the same object —
    when there is no such column, nothing resolves, or anything goes wrong.
    """
    try:
        sheets = _plan_sheets(plan)
        rollup = tcol = None
        for sheet in sheets:
            headers = [str(h).strip().lower() for h in (sheet.get("headers") or [])]
            if _TOTAL_TCS in headers:
                rollup, tcol = sheet, headers.index(_TOTAL_TCS)
                break
        if rollup is None:
            return plan, []                    # not a test-count rollup -> inert

        # ── Count everything countable, from the data sheets only ────────────
        per_sheet: Dict[str, int] = {}
        levels: Dict[str, int] = {}
        countries: Dict[str, int] = {}
        grand = with_country = 0
        for sheet in sheets:
            name = str(sheet.get("name") or "")
            if sheet is rollup or _is_meta_sheet(name):
                continue
            headers = [str(h).strip().lower() for h in (sheet.get("headers") or [])]
            if not any("test case id" in h for h in headers):
                continue
            li = next((i for i, h in enumerate(headers)
                       if h in ("test level", "level")), None)
            ci = next((i for i, h in enumerate(headers) if "country" in h), None)
            n = 0
            for row in (sheet.get("rows") or []):
                if not isinstance(row, (list, tuple)) or not row:
                    continue
                if not _split_id(row[0]):
                    continue                   # not a test case row
                n += 1
                grand += 1
                if li is not None and li < len(row) and str(row[li] or "").strip():
                    v = str(row[li]).strip()
                    levels[v] = levels.get(v, 0) + 1
                if ci is not None and ci < len(row) and str(row[ci] or "").strip():
                    v = str(row[ci]).strip()
                    countries[v] = countries.get(v, 0) + 1
                    with_country += 1
            if n:
                per_sheet[name] = n
        if not grand:
            return plan, []

        def resolve(label: str) -> Optional[int]:
            """Exactly one countable meaning, or None."""
            pre = _ROLLUP_PREFIX.match(label)
            body = _ROLLUP_PREFIX.sub("", label).strip() if pre else label.strip()
            dim = (pre.group(1).lower() if pre else "")
            if _norm(body) in _ROLLUP_TOTAL:
                # A 'By Country' subtotal counts rows that HAVE a country, which
                # is not the same as the grand total when some rows leave it blank.
                return with_country if "country" in dim else grand
            for k, v in levels.items():
                if _norm(body) == _norm(k):
                    return v
            for k, v in countries.items():
                if _norm(body) == _norm(k):
                    return v

            bt = _tokens(body)

            # 'SIT Test', 'UAT Test Cases' name a LEVEL, dressed in filler. Try
            # again with the filler removed, before falling through to sheets —
            # otherwise 'SIT Test' loses the level and gets resolved to whatever
            # sheet happens to look closest.
            bare = bt - _ROLLUP_FILLER
            if bare and bare != bt:
                for k, v in levels.items():
                    if _tokens(k) == bare:
                        return v
                for k, v in countries.items():
                    if _tokens(k) == bare:
                        return v

            if bt:
                exact = [s for s in per_sheet if _tokens(s) == bt]
                if len(exact) == 1:
                    return per_sheet[exact[0]]
                # ONE DIRECTION ONLY: the label may be less specific than the
                # sheet ('PPT Parallel Payroll' -> 'PPT - Parallel Payroll
                # Test'), never the reverse. Allowing the reverse matched the
                # label 'SIT Test' to the sheet 'SIT - EC Core HR Processes',
                # because that sheet's only significant token is 'sit' — and
                # wrote 55 into a row whose true answer was 176.
                near = [s for s in per_sheet if bt <= _tokens(s)]
                if len(near) == 1:
                    return per_sheet[near[0]]
            return None

        out = copy.deepcopy(plan)
        out_rollup = next(s for s in _plan_sheets(out)
                          if [str(h).strip().lower() for h in (s.get("headers") or [])]
                          == [str(h).strip().lower() for h in (rollup.get("headers") or [])])
        notes: List[str] = []
        for row in (out_rollup.get("rows") or []):
            if not isinstance(row, list) or not row:
                continue
            label = str(row[0] or "").strip()
            if not label:
                continue
            n = resolve(label)
            if n is None:
                continue                       # ambiguous -> leave it alone
            while len(row) <= tcol:
                row.append("")
            before = str(row[tcol] or "").strip()
            if before == str(n):
                continue
            row[tcol] = str(n)
            notes.append(f"{label[:34]}: {before or 'blank'} -> {n}")
        return (out, notes) if notes else (plan, [])
    except Exception:
        return plan, []


# ── One spelling per value ──────────────────────────────────────────────────
#
# R7 wrote the SAME value two ways in the SAME column: Approach held 'N/A' on
# 165 rows and 'N-A' on 100. Filter on either and a third of the workbook
# silently disappears. R5 used only 'N-A', R6 only 'N/A' — so this is not a
# preference the model holds, it is one it forgets mid-document, which is why
# asking it again is not the fix.
#
# Two values are the same when they normalise identically (alphanumerics only,
# lowercased): 'N/A' and 'N-A' both become 'na'. 'India' and 'Indian' do not
# collide, so genuinely different values are never merged. Applied ONLY to
# categorical columns — collapsing on a free-text column would rewrite prose
# that merely differs in punctuation.

def unify_value_spellings(plan: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Collapse variant spellings of one value to the majority form.

    Returns (plan, notes). The plan is returned unchanged — the same object —
    when there is nothing to do or anything goes wrong.
    """
    try:
        counts = _plan_column_counts(plan)
        # header -> {normalised: winning spelling}
        canon: Dict[str, Dict[str, str]] = {}
        notes: List[str] = []
        for header, values in counts.items():
            flat = [v for v, n in values.items() for _ in range(n)]
            if not _is_categorical(flat):
                continue
            groups: Dict[str, List[Tuple[str, int]]] = {}
            for v, n in values.items():
                groups.setdefault(_norm(v), []).append((v, n))
            mapping = {}
            for key, variants in groups.items():
                if len(variants) < 2 or not key:
                    continue
                variants.sort(key=lambda p: (-p[1], p[0]))
                winner = variants[0][0]
                mapping[key] = winner
                notes.append(
                    f"'{header}': " +
                    ", ".join(f"{v}({n})" for v, n in variants) + f" -> {winner}")
            if mapping:
                canon[header] = mapping
        if not canon:
            return plan, []

        out = copy.deepcopy(plan)
        for sheet in _plan_sheets(out):
            headers = [str(h).strip() for h in (sheet.get("headers") or [])]
            cols = [(i, canon[h]) for i, h in enumerate(headers) if h in canon]
            if not cols:
                continue
            for row in (sheet.get("rows") or []):
                if not isinstance(row, list):
                    continue
                for i, mapping in cols:
                    if i >= len(row) or not isinstance(row[i], str):
                        continue
                    win = mapping.get(_norm(row[i]))
                    if win and row[i] != win:
                        row[i] = win
        return out, notes
    except Exception:
        return plan, []


# ── The scope CEILING: named exclusions must be acknowledged ────────────────
#
# The prompt has always carried the rule — "for any out-of-scope area, set
# Status = 'Out of Scope' and include a single placeholder row; do not omit it
# silently" — and like every other instruction it obeys some runs and not others:
#
#     R5   6 of 6 exclusions present, all marked   (Out of Scope x9)
#     R6   6 of 6 exclusions present, all marked   (Out of Scope x8)
#     R7   0 of 6 — no exclusion mentioned anywhere, no such Status at all
#
# The input names eight exclusions explicitly. A test script that never mentions
# them lets a reader assume LMS testing is included, which is a scope dispute
# waiting to happen in a contractual document. Presence is exactly the kind of
# thing Python can settle, so it is settled here rather than requested again.

_OOS_STATUS = "out of scope"


def out_of_scope_gaps(plan: Dict[str, Any],
                      out_of_scope: Sequence[str],
                      in_scope: Sequence[str] = ()) -> Dict[str, Any]:
    """
    Is the scope ceiling represented at all? Counts rows whose Status cell reads
    'Out of Scope'.

    Returns {"expected", "found", "items", "needs_repair"}; inert (never a
    repair) when the source names no exclusions, when no sheet has a Status
    column, or on any error.

    DELIBERATELY NOT A TEXT SEARCH. The first version of this looked for each
    exclusion's distinctive words in the plan, and the replay killed it: against
    an in-scope list containing 'Performance & Goals', the distinctive tokens of
    'Performance / load testing' are {load, testing, ...} — and 'testing' occurs
    on nearly every row of a TEST SCRIPT. Four of six exclusions were judged
    present when the workbook mentioned none of them. Every fuzzy variant of
    this check has the same shape of failure.
    'Out of Scope' is a controlled Status value this pipeline already mandates,
    so it can be counted exactly instead of inferred.

    `in_scope` is unused and kept for call-site compatibility.
    """
    empty = {"expected": 0, "found": 0, "items": [], "needs_repair": False}
    try:
        items = [str(x).strip() for x in (out_of_scope or []) if str(x or "").strip()]
        if not items:
            return empty

        has_status = False
        found = 0
        for sheet in _plan_sheets(plan):
            headers = [str(h).strip().lower() for h in (sheet.get("headers") or [])]
            if "status" not in headers:
                continue
            has_status = True
            idx = headers.index("status")
            for row in (sheet.get("rows") or []):
                if not isinstance(row, (list, tuple)) or idx >= len(row):
                    continue
                if str(row[idx] or "").strip().lower() == _OOS_STATUS:
                    found += 1
        if not has_status:
            return empty                       # nothing to judge against

        return {"expected": len(items), "found": found, "items": items,
                "needs_repair": found == 0}
    except Exception:
        return empty


# ── Enforceable floor on a NAMED dimension ──────────────────────────────────
#
# WHY THIS EXISTS AND WHY IT IS NOT A PROMPT RULE
# ------------------------------------------------
# Measured across six Test Script runs on identical input, every metric that a
# PROMPT instruction governs oscillates, and every metric that PYTHON governs
# holds:
#
#     PPT cases        8 -> 4 -> 8 -> 18 -> 24 -> 8      (prompt)
#     UAT cases       34 -> 9 -> 27 -> 22 -> 38 -> 22    (prompt)
#     wave mentions   17 -> 7 -> 4 ->  0 -> 47 -> 7      (prompt)
#     duplicate ids                        26 -> 0       (python, held)
#     bare '(no map)'      128 -> 0 -> 0 -> 0            (prompt, but FORMAT)
#
# The split is exact: FORMAT rules ("never write a bare (no map)", "one value
# per cell") hold in every later run. VOLUME rules do not, because the generator
# has a finite output budget and every emphasis added reallocates depth away
# from something else. Five attempts to fix allocation by wording produced five
# reallocations, not one convergence. Allocation has to be measured and enforced.
#
# WHY IT IS SAFE FOR OTHER DOCUMENT TYPES
# ---------------------------------------
# The floor keys on an EXACT COLUMN HEADER, and a missing column disables it
# entirely. Verified against the real estate:
#
#     'Test Level'   present in 6/6 Test Scripts, absent in 4/4 Project Plans
#     'Country'      present in 6/6 Test Scripts, absent in 4/4 Project Plans
#
# So a floor on either is INERT for a Project Plan by construction — not by a
# heuristic that might mismatch. That distinction matters: an earlier attempt to
# recompute summary sheets had to GUESS which sheet was a summary, and it
# matched the Project Plan's 'Master Plan' — its primary content sheet. Anything
# that has to interpret can mis-interpret; presence of a named column cannot.

_DIM_MIN_PRIOR   = 5      # a value too rare in the prior run tells us nothing
_DIM_TOLERANCE   = 0.65   # below 65% of its prior count, a value has collapsed
_DIM_MAX_REPORT  = 8

# Values this floor must NEVER defend, because other rules exist to remove them.
# Found by replaying the floor over the real run history: without this it fired
# on every cleanup, and would have instructed the model to put the rubbish back.
#
#     'N-A' 8 -> 0            a non-value the format rule deletes
#     'IN/NP/UAE/EG' 11 -> 0  a composite the one-value-per-cell rule splits
#     'Global' 106 -> 46      a catch-all the prefer-the-specific rule reduces
#
# Each of those is an IMPROVEMENT. A floor that treats them as coverage loss
# puts two rules in opposition, which is the oscillation this whole mechanism
# exists to end.
_DIM_NON_VALUES = {
    "n/a", "n-a", "na", "none", "-", "--", "tbd", "tbc", "n.a.", "unknown", "",
}
_DIM_CATCH_ALLS = {"global", "all", "various", "multiple", "any", "cross"}


def _dim_defensible(value: str) -> bool:
    """False for values another rule is actively trying to eliminate."""
    v = str(value or "").strip().lower()
    if v in _DIM_NON_VALUES or v in _DIM_CATCH_ALLS:
        return False
    # Composite cells ('IN/NP/UAE/EG', 'UAE/Egypt') violate one-value-per-cell.
    if re.search(r"[A-Za-z]\s*[/&+]\s*[A-Za-z]", str(value or "")):
        return False
    return True


_DIM_MIN_CORROBORATION = 2   # runs a value must appear in to count as established
_DIM_DAMP_MIN          = 15  # below this a peak is too small to be worth damping
_VOCAB_MIN_CHARS       = 80  # below this the extracted context is too thin to trust


# Fields that describe what this project IS. `out_of_scope` is deliberately
# excluded — a value listed there must not become something the floor demands.
_VOCAB_FIELDS = (
    "project_name", "client_name", "project_type", "scope_summary",
    "geographic_scope", "data_migration_scope", "delivery_methodology",
    "additional_context", "in_scope_modules", "integration_points",
    "key_requirements",
)


def project_vocabulary(project_ctx: Optional[Dict[str, Any]]) -> str:
    """
    Flatten what THIS run's input actually says, for use as a scope gate.

    WHY: the prior-run history is keyed on TEMPLATE only — the documents table
    has no project or client column — so "the last four Test Scripts" means the
    last four for ANY client. Without this gate a Test Script for a new customer
    is measured against the previous customer's numbers, and the repair
    instruction reads 'India: was 116, now 0 — restore it' on a project that has
    no Indian scope at all. The floor would inject one client's facts into
    another client's deliverable, silently.

    So a value is only defensible when this project's own extracted context
    mentions it. Returns "" when there is not enough context to judge, which
    disables the gate rather than guessing.
    """
    if not isinstance(project_ctx, dict):
        return ""
    parts: List[str] = []
    for key in _VOCAB_FIELDS:
        val = project_ctx.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, (list, tuple)):
            parts.extend(str(v) for v in val if v)
        elif isinstance(val, dict):
            parts.extend(str(v) for v in val.values() if v)
    text = " ".join(p for p in parts if p).strip()
    return text if len(text) >= _VOCAB_MIN_CHARS else ""


def _mentioned_in(value: str, vocabulary: str) -> bool:
    """
    Whole-word match, so 'SA' does not match 'SAP' and 'US' does not match
    'customer' — the short country codes this estate uses are exactly the ones a
    substring test gets wrong.
    """
    v = str(value or "").strip()
    if not v or not vocabulary:
        return False
    try:
        return re.search(r"\b" + re.escape(v) + r"\b", vocabulary, re.I) is not None
    except Exception:
        return False


def dimension_balance(plan: Dict[str, Any],
                      column: str,
                      prior_paths: Any,
                      project_terms: str = "") -> Dict[str, Any]:
    """
    Values of `column` that collapsed against the most recent prior run.

    `prior_paths` is the recent history, newest first (a bare string is accepted
    for one). The newest is the comparison baseline; the rest CORROBORATE —
    a value is defended only if it carried real weight in at least
    `_DIM_MIN_CORROBORATION` of them.

    That corroboration requirement is not decoration. Without it the floor fired
    on 'Regression' — a Test Level that appeared in exactly one run, is not among
    the levels the source document defines (DEV/SIT/UAT/PPT/PRD), and was
    correctly dropped by the next run. Defending a one-off would make every
    future run carry it forever: one run's vocabulary drift becomes a permanent
    obligation, and the floor slowly ratchets in whatever noise it has seen.

    Returns {"column", "shortfalls": [(value, prior_n, now_n)], "needs_repair"}.
    A clean report — never a repair — when the column is absent, when there is
    no history, or on any error. A first run is never blocked.
    """
    empty = {"column": column, "shortfalls": [], "needs_repair": False}
    try:
        if isinstance(prior_paths, str):
            prior_paths = [prior_paths] if prior_paths else []
        paths = [p for p in (prior_paths or []) if p]
        if not column or not paths:
            return empty
        cur = _plan_column_counts(plan).get(column)
        if not cur:
            return empty                      # column absent -> inert

        per_run = [_file_column_counts(p).get(column) or {} for p in paths]
        if not any(per_run):
            return empty

        # BASELINE IS THE BEST RECENT RUN PER VALUE, NOT THE LAST ONE.
        #
        # Anchoring to the most recent run ratifies whatever it did. Measured on
        # the real history: the newest run carries PPT 8 and UAT 22, while the
        # run before it carried 24 and 38. A last-run baseline would set the PPT
        # floor at 5 — so the next run could ship five parallel-payroll cases
        # and pass a check that exists precisely to catch that collapse. It also
        # contradicted prior_best_rows(), which already floors ROW DEPTH against
        # the best recent run; two floors disagreeing about what "prior" means
        # is a bug waiting to be argued about.
        #
        # The peak is damped to 1.5x the runner-up so ONE freak run cannot set a
        # permanent target — the same reasoning as _PRIOR_CAP_MULTIPLE on the row
        # floor. On the real data this bites exactly once (a country that spiked
        # to 62 against a 29 runner-up) and leaves every other value untouched.
        prior: Dict[str, int] = {}
        for value in {k for r in per_run for k in r}:
            counts = sorted((r.get(value, 0) for r in per_run), reverse=True)
            peak = counts[0]
            # Damp only where a spike is big enough to matter. Applied to small
            # counts it destroys them: 'Sri Lanka' ran 1 / 0 / 5, and damping a
            # peak of 5 against a runner-up of 1 gave a baseline of 1 — which
            # silently excused the country dropping to zero, the very
            # regression this floor was added to catch.
            if peak >= _DIM_DAMP_MIN and len(counts) > 1 and counts[1] > 0:
                peak = min(peak, int(counts[1] * _PRIOR_CAP_MULTIPLE))
            if peak > 0:
                prior[value] = peak

        # How many recent runs each value APPEARS in at all — presence, not
        # weight. Weight was the first attempt and it was wrong in a way the
        # replay caught: 'Sri Lanka' carried 5 rows in one run and 1 in another,
        # so a weight-based test read it as a one-off and let a real country
        # regression through. Presence separates the two cases cleanly —
        # 'Sri Lanka' appears in several runs, 'Regression' in exactly one.
        # The baseline count still has to clear _DIM_MIN_PRIOR to trigger, so
        # this only decides what is ESTABLISHED, never what is significant.
        support: Dict[str, int] = {}
        for p in paths:
            counts = _file_column_counts(p).get(column) or {}
            for v, n in counts.items():
                if n >= 1:
                    support[v] = support.get(v, 0) + 1
        # With only one run of history there is nothing to corroborate against,
        # so fall back to trusting it rather than enforcing nothing at all.
        need = _DIM_MIN_CORROBORATION if len(paths) > 1 else 1

        shortfalls = []
        for value, p_n in prior.items():
            if p_n < _DIM_MIN_PRIOR:
                continue
            if _norm(value).isdigit():
                continue                      # numbers are not categories
            if not _dim_defensible(value):
                continue                      # a cleanup, not a regression
            if support.get(value, 0) < need:
                continue                      # one-off drift, not an established dimension
            # A value from an earlier project that this one never mentions is
            # not a regression — it was never in scope. Skipped rather than
            # demanded. With no usable context the gate is off, so behaviour is
            # unchanged rather than silently stricter.
            if project_terms and not _mentioned_in(value, project_terms):
                continue
            c_n = cur.get(value, 0)
            if c_n < p_n * _DIM_TOLERANCE:
                shortfalls.append((value, p_n, c_n))
        shortfalls.sort(key=lambda s: -(s[1] - s[2]))
        return {"column": column,
                "shortfalls": shortfalls[:_DIM_MAX_REPORT],
                "needs_repair": bool(shortfalls)}
    except Exception:
        return empty


def column_regressions(plan: Dict[str, Any],
                       prior_path: str,
                       project_terms: str = "") -> List[str]:
    """
    Human-readable notes on categorical values that collapsed against the best
    prior run. Empty list when there is nothing to say, no prior run, or
    anything at all goes wrong.
    """
    try:
        if not prior_path:
            return []
        prior = _file_column_counts(prior_path)
        if not prior:
            return []
        current = _plan_column_counts(plan)
        if not current:
            return []

        notes: List[str] = []
        for header, p_counts in prior.items():
            c_counts = current.get(header)
            if not c_counts:
                continue
            flat = [v for v, n in p_counts.items() for _ in range(n)]
            if not _is_categorical(flat):
                continue
            drops = []
            for value, p_n in p_counts.items():
                if p_n < _DRIFT_MIN_PRIOR:
                    continue
                c_n = c_counts.get(value, 0)
                # A value that vanished COMPLETELY is almost always a rename,
                # not a loss. Measured across the 06->08 Aug Test Script pair,
                # every single zero was vocabulary: Status 'Not Executed'
                # 252->0 (replaced by Confirmed/TBC), Approach 'N/A' 236->0,
                # every '(no map - ...)' phrasing 98->0. Reporting those as
                # lost coverage buried the one real finding — Country
                # India 103->45 — in five lines of noise. A value that SHRANK
                # BUT SURVIVED is the shape of an actual regression, because
                # the vocabulary evidently still exists.
                if c_n <= 0:
                    continue
                # Numbers are not categories. 'Duration (Days)' repeats a
                # handful of integers often enough to look categorical, and
                # its churn says nothing about coverage.
                if _norm(value).isdigit():
                    continue
                # Same project gate as dimension_balance: these notes reach the
                # repair instruction, so a previous client's values must not be
                # presented to this one as lost coverage.
                if project_terms and not _mentioned_in(value, project_terms):
                    continue
                if c_n < p_n * _DRIFT_TOLERANCE:
                    drops.append((p_n - c_n, value, p_n, c_n))
            if drops:
                drops.sort(reverse=True)
                shown = ", ".join(
                    f"{v} {pn}->{cn}" for _, v, pn, cn in drops[:_DRIFT_MAX_REPORT])
                notes.append(f"'{header}': {shown}")
        return notes
    except Exception:
        return []


def _appears_in_rows(module: str, plan: Dict[str, Any]) -> Optional[str]:
    """Name of the first sheet whose cells mention `module`, else None."""
    m_tokens = _tokens(module)
    m_norm = _norm(module)
    for sheet in _plan_sheets(plan):
        for row in (sheet.get("rows") or []):
            if not isinstance(row, (list, tuple)):
                continue
            for cell in row:
                if not isinstance(cell, str):
                    continue
                c_norm = _norm(cell)
                if m_norm and (m_norm in c_norm):
                    return str(sheet.get("name") or "?")
                if m_tokens and (m_tokens & _tokens(cell)):
                    return str(sheet.get("name") or "?")
    return None


def analyse(plan: Dict[str, Any],
            modules: Sequence[str],
            shape: str) -> Dict[str, Any]:
    """
    Compare a generated plan against the in-scope module list.

    Returns a report; `needs_repair` is the only field the caller must read.
    """
    sheets = _plan_sheets(plan)
    sheet_names = [str(s.get("name") or "") for s in sheets]
    module_sheets = [n for n in sheet_names if not _is_meta_sheet(n)]

    mods = [str(m).strip() for m in modules if str(m or "").strip()]
    matched, unmatched = assign(mods, sheet_names)

    in_rows_only: List[Tuple[str, str]] = []
    absent: List[str] = []
    for mod in unmatched:
        where = _appears_in_rows(mod, plan)
        if where is None:
            absent.append(mod)
        elif shape == "tabs":
            # Rows are not enough when the reference organises by tab.
            in_rows_only.append((mod, where))
        else:
            pass          # 'rows' shape — rows ARE the correct home

    # ── Structural guard against false positives ──────────────────────────
    #
    # Name matching cannot resolve every abbreviation this estate uses —
    # 'Recruiting' vs a sheet called 'RCM' has no textual signal at all. So a
    # workbook that already has AT LEAST as many module sheets as there are
    # in-scope modules is treated as complete-but-differently-named, and only
    # warned about. Only a workbook that is structurally SHORT is repaired,
    # which is the case that actually loses content: the 17:14 run had 9 module
    # sheets for 11 modules and could not have covered them all.
    structurally_short = len(module_sheets) < len(mods)
    gaps = bool(in_rows_only or absent)

    # A gap is only ENFORCEABLE where the reference itself organises by module.
    # Everywhere else the document's structure belongs to the reference, not to
    # this check: the Test Script reference is organised by test phase, and
    # demanding a tab per module there would rewrite a deliverable that works.
    # Those gaps are still reported — as advice, never as a repair.
    enforceable = (shape == "tabs")

    return {
        "shape":         shape,
        "sheet_names":   sheet_names,
        "module_sheets": module_sheets,
        "module_count":  len(mods),
        "matched":       matched,
        "covered":       [m for m in mods if m in matched],
        "in_rows_only":  in_rows_only,
        "absent":        absent,
        "naming_only":   gaps and not structurally_short,
        "advisory":      gaps and not enforceable,
        "needs_repair":  gaps and structurally_short and enforceable,
    }


# ── Repair instruction ───────────────────────────────────────────────────────

def repair_instruction(report: Dict[str, Any],
                       row_shortfall: int = 0,
                       row_floor: int = 0,
                       row_actual: int = 0,
                       balance: Optional[Dict[str, Any]] = None,
                       column_notes: Optional[Sequence[str]] = None,
                       dimensions: Optional[Sequence[Dict[str, Any]]] = None,
                       excluded: Optional[Sequence[str]] = None) -> str:
    """
    The correction sent back to the model. Names the specific gaps — a generic
    "be more complete" is what already failed three times.

    `balance` and `column_notes` are optional so existing callers keep working
    unchanged.
    """
    parts: List[str] = [
        "SCOPE CORRECTION — YOUR PREVIOUS PLAN WAS INCOMPLETE.\n"
        "This is a mechanical check of your own output against the confirmed "
        "in-scope list, not an opinion. Fix exactly what is listed and change "
        "nothing else.\n\n"
    ]

    absent = report.get("absent") or []
    rows_only = report.get("in_rows_only") or []

    if absent:
        parts.append(
            "MISSING ENTIRELY — these in-scope items appear nowhere in your "
            "plan. Add a dedicated sheet for each, populated to the same depth "
            "as the comparable module sheets you already produced:\n"
            + "".join(f"    - {m}\n" for m in absent) + "\n"
        )

    if rows_only:
        parts.append(
            "PRESENT BUT WITH NO SHEET OF THEIR OWN — you folded these into "
            "another sheet. This deliverable is ONE workbook with EVERY module "
            "on its OWN tab, so promote each to a dedicated sheet, moving its "
            "rows across and expanding them to full module depth:\n"
            + "".join(f"    - {m}  (currently inside '{w}')\n" for m, w in rows_only)
            + "\n"
        )

    if row_shortfall > 0:
        parts.append(
            f"DEPTH SHORTFALL — your plan carries {row_actual} data rows against "
            f"a reference of {row_floor}. The reference is a FLOOR, not a target: "
            f"this project's scope is at least as large. Add at least "
            f"{row_shortfall} more rows of real, source-derived detail while "
            f"fixing the above.\n\n"
        )

    starved = (balance or {}).get("starved") or []
    if starved:
        mean  = (balance or {}).get("mean") or 0
        floor = (balance or {}).get("floor") or 0
        parts.append(
            "UNEVEN DEPTH — these in-scope items are present but far thinner "
            f"than their peers. Your plan averages {mean} rows per module; "
            f"each of these carries fewer than {floor}:\n"
            + "".join(f"    - {m}  ({n} row(s))\n" for m, n in starved)
            + "\n"
            "  Every one of these was named as in scope by the source document, "
            "with the same weight as the modules you covered deeply. A handful "
            "of rows for a whole module is not coverage — it is a placeholder "
            "that will read as tested and never be tested. Bring each up to a "
            "depth comparable to the modules you did cover, drawing the detail "
            "from the source document.\n"
            "  DO THIS BY ADDING, NEVER BY CUTTING. Do not thin a well-covered "
            "module to even out the average — that trades one defect for "
            "another and loses work you already did correctly.\n\n"
        )

    if excluded:
        parts.append(
            "SCOPE CEILING — these areas are named OUT OF SCOPE by the source "
            "document and appear NOWHERE in your plan:\n"
            + "".join(f"    - {x}\n" for x in excluded)
            + "\n"
            "  Silence is not the same as exclusion. A reader who cannot find "
            "an area in this document cannot tell whether it was excluded or "
            "forgotten, and on a contractual deliverable that ambiguity is "
            "resolved against the delivery team. Add ONE row for each, with "
            "Status set to 'Out of Scope' and the source's own reason in the "
            "notes. One row each — do NOT generate detailed content for them.\n\n"
        )

    for dim in (dimensions or []):
        sf = (dim or {}).get("shortfalls") or []
        if not sf:
            continue
        col = dim.get("column") or "dimension"
        parts.append(
            f"COVERAGE LOST ON '{col}' — measured against the last good version "
            f"of this document, not estimated. Each line reads "
            f"'value: was N rows, now M':\n"
            + "".join(f"    - {v}: was {p}, now {c}\n" for v, p, c in sf)
            + "\n"
            "  These are not optional dimensions — a previous generation from "
            "this same source covered them at that depth, so the source "
            "supports it. Restore each one by ADDING rows for it. If an item "
            "is genuinely out of scope per the source document, add a single "
            "row for it with Status 'Out of Scope' rather than leaving it "
            "absent.\n\n"
        )

    if column_notes:
        parts.append(
            "COVERAGE THAT SHRANK SINCE THE LAST VERSION OF THIS DOCUMENT — "
            "measured, not estimated. Each entry reads 'value old->new':\n"
            + "".join(f"    - {n}\n" for n in column_notes)
            + "\n"
            "  A previous generation from this same source covered these more "
            "deeply than you have. Where the drop is because rows moved to a "
            "broader catch-all value, that is a LOSS OF SPECIFICITY, not a "
            "simplification: the narrower value is what makes a row testable. "
            "Restore that depth unless the source document genuinely puts the "
            "item out of scope.\n\n"
        )

    # RETURN ONLY THE DELTA.
    #
    # The first version of this asked for the COMPLETE plan back. Measured on the
    # 08 Aug 05:50 run, that is why the repair does not work: the first pass
    # emitted 77,734 output tokens, and the repair — asked to reproduce all of it
    # plus additions — came back at 48,008, SMALLER than what it was correcting.
    # The never-regress guard then discarded it, so the pass consumed roughly a
    # quarter of the run's model work and changed nothing.
    #
    # Re-emitting hundreds of rows the model already got right is both the
    # expensive part and the fragile part. Asking only for what is missing costs
    # a fraction of the output, and Python appends it — so existing content
    # cannot be thinned, reordered or dropped by a model under output pressure.
    # It is structurally additive rather than additive-by-instruction.
    parts.append(
        "RETURN ONLY THE ADDITIONS — NOT THE WHOLE PLAN.\n\n"
        "Reply with EXACTLY this JSON shape and nothing else:\n\n"
        '{"additions": [\n'
        '  {"sheet": "<exact name of an EXISTING sheet>", '
        '"rows": [["cell", "cell", ...], ...]},\n'
        '  {"sheet": "<name of a NEW sheet>", "headers": ["col", ...], '
        '"rows": [["cell", ...], ...]}\n'
        "]}\n\n"
        "  - Every row must have exactly as many cells as that sheet's header "
        "row, in the same column order. Fill every column, including Status and "
        "the notes column.\n"
        "  - Use the EXACT existing sheet name when adding to a sheet that "
        "already exists. Supply \"headers\" ONLY for a sheet that does not exist "
        "yet.\n"
        "  - Do NOT resend rows the plan already has. Everything you return is "
        "APPENDED to what is there, so a resent row becomes a duplicate.\n"
        "  - Do NOT return existing sheets, the title, or any other field. Your "
        "existing content is preserved automatically — you cannot lose it, so "
        "spend the whole response on the missing detail.\n"
        "  - Populate from the source document, not from placeholders.\n"
        "  - ALREADY COVERED UNDER ANOTHER NAME? This check matches on names and "
        "cannot know every abbreviation. If an item above is genuinely already "
        "carried under a different name (for example 'Recruiting' by a sheet "
        "called 'RCM'), return no additions for it rather than creating a "
        "duplicate. An empty list is a valid answer.\n"
    )
    return "".join(parts)


# ── Merging the repair delta ─────────────────────────────────────────────────

# An identifier: a LETTER-led prefix, a separator, and a trailing number.
# 'SIT-DM-001' -> ('SIT-DM', '-', '001'). 'OI-007' -> ('OI', '-', '007').
#
# The leading-letter requirement is the whole safety gate for the dedup below,
# and it is not incidental. A Project Plan's first column is 'S.No.' holding
# 1, 2, 3 ... restarting on EVERY sheet, so a dedup keyed on "column 0 repeats"
# would treat the second sheet's row 1 as a clash with the first sheet's row 1
# and renumber an entire workbook that was correct. Verified against the real
# files: Project Plan matches 0 of 281 first-column values on its task sheets,
# and only its Open Items 'OI-00n' rows parse at all — where deduplicating IS
# the right behaviour. Test Script matches 335 of 416.
_ID_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z][A-Za-z0-9]*)*)([-_])(\d+)$")


def _split_id(value: Any) -> Optional[Tuple[str, str, int, int]]:
    """('SIT-DM', '-', 1, 3) for 'SIT-DM-001'; None when it is not an id."""
    m = _ID_RE.match(str(value or "").strip())
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3)), len(m.group(3))


def merge_row_additions(plan: Dict[str, Any],
                        delta: Any) -> Tuple[Dict[str, Any], int, List[str]]:
    """
    Fold the repair pass's `{"additions": [...]}` into the plan. ADDITIVE ONLY.

    Returns (new_plan, rows_added, notes). The input plan is never mutated —
    the caller still needs it intact to compare against and to fall back to.

    Nothing here can remove a sheet, remove a row, or reorder anything: the only
    write operations are `list.append` on a copied plan. That is the point. The
    previous design asked the model to hand back everything it already had, and
    trusted it not to drop content in the process; this one makes that class of
    failure impossible rather than checking for it afterwards.

    Never raises — on anything unexpected it returns the plan unchanged, which
    leaves today's behaviour.
    """
    try:
        items = (delta or {}).get("additions")
        if not isinstance(items, list) or not items:
            return plan, 0, []

        out = copy.deepcopy(plan)
        sheets = [s for s in (out.get("sheets") or []) if isinstance(s, dict)]
        by_exact = {str(s.get("name") or ""): s for s in sheets}
        by_norm = {_norm(s.get("name")): s for s in sheets}

        # ── Identifier registry, for the dedup below ─────────────────────────
        #
        # WHY: the 08 Aug 08:38 run merged 26 rows whose ids ALREADY existed and
        # carried different content — 'SIT-DM-001' was 'Employee master data
        # migration validation' on one sheet and 'Record count reconciliation'
        # on another, and 'UAT-EC-006' meant two different things INSIDE ONE
        # SHEET. None were harmless repeats. A test lead cannot trace an id that
        # denotes two scenarios, so the workbook fails review on it.
        #
        # The instruction did say "do not resend rows the plan already has", and
        # the model obeyed it — it did not resend anything. It reused id NUMBERS
        # for genuinely new rows, which that sentence never covered. Renumbering
        # in Python is the fix, because the content is worth keeping and only
        # the label collides.
        #
        # Safe by construction: a row being ADDED cannot yet be referenced by
        # anything, since it was not in the plan a moment ago. (Verified
        # separately that no column in these workbooks cites another row's id at
        # all, so no existing reference can break either.)
        seen_ids: Set[str] = set()
        max_by_prefix: Dict[str, int] = {}
        for s in sheets:
            for row in (s.get("rows") or []):
                if not isinstance(row, (list, tuple)) or not row:
                    continue
                parsed = _split_id(row[0])
                if not parsed:
                    continue                      # 'S.No.' 1/2/3 lands here
                prefix, _sep, num, _w = parsed
                seen_ids.add(f"{prefix}{_sep}{num}".upper())
                key = prefix.upper()
                max_by_prefix[key] = max(max_by_prefix.get(key, 0), num)

        added, renamed, notes = 0, 0, []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("sheet") or "").strip()
            rows = item.get("rows")
            if not name or not isinstance(rows, list) or not rows:
                continue

            target = by_exact.get(name) or by_norm.get(_norm(name))
            if target is None:
                # A genuinely new sheet. Borrow the headers the model supplied;
                # falling back to an existing sheet's columns keeps the workbook
                # coherent when it supplies none.
                headers = item.get("headers")
                if not isinstance(headers, list) or not headers:
                    donor = next((s for s in sheets
                                  if not _is_meta_sheet(str(s.get("name") or ""))
                                  and s.get("headers")), None)
                    headers = list(donor.get("headers")) if donor else []
                if not headers:
                    continue                      # no columns to write into
                target = {"name": name, "headers": [str(h) for h in headers],
                          "rows": []}
                # Insert BEFORE the TRAILING metadata sheets rather than at the
                # end. Appending put a 102-row data tab after 'Version History'
                # and 'Open Items' in the 08:38 workbook — content intact, but a
                # reader opening the last tab expects the change log, not test
                # cases.
                #
                # Scanned from the END, not the front. These workbooks open with
                # metadata too ('Cover & Change Log', 'Test Summary'), so
                # "before the first meta sheet" put the new tab at index 0,
                # ahead of the cover page — a different wrong answer.
                dest_sheets = out.setdefault("sheets", [])
                at = len(dest_sheets)
                while at > 0 and _is_meta_sheet(str(dest_sheets[at - 1].get("name") or "")):
                    at -= 1
                dest_sheets.insert(at, target)
                sheets.append(target)
                by_exact[name] = target
                by_norm[_norm(name)] = target
                notes.append(f"+sheet '{name}'")

            width = len(target.get("headers") or []) or None
            dest = target.setdefault("rows", [])
            n_before = len(dest)
            for row in rows:
                if not isinstance(row, (list, tuple)):
                    continue
                cells = ["" if c is None else str(c) for c in row]
                if width:
                    # Pad or trim to the sheet's own width. A short row would
                    # otherwise shift every later column on render, and a long
                    # one would write past the header.
                    cells = (cells + [""] * width)[:width]

                # Give a colliding identifier the next free number in its own
                # series. Rows whose first cell is not an identifier — a plain
                # '1', a task name — are never touched.
                parsed = _split_id(cells[0]) if cells else None
                if parsed:
                    prefix, sep, num, w = parsed
                    key = prefix.upper()
                    if f"{prefix}{sep}{num}".upper() in seen_ids:
                        num = max_by_prefix.get(key, 0) + 1
                        cells[0] = f"{prefix}{sep}{num:0{w}d}"
                        renamed += 1
                    seen_ids.add(f"{prefix}{sep}{num}".upper())
                    max_by_prefix[key] = max(max_by_prefix.get(key, 0), num)

                dest.append(cells)
            gained = len(dest) - n_before
            added += gained
            if gained:
                notes.append(f"{name} +{gained}")

        if renamed:
            notes.append(f"{renamed} duplicate id(s) renumbered")
        return (out, added, notes) if added else (plan, 0, [])
    except Exception:
        return plan, 0, []


# ── Reporting ────────────────────────────────────────────────────────────────

def summarise(report: Dict[str, Any]) -> str:
    """One log line describing the gate's verdict."""
    n_sheets = len(report.get("module_sheets") or [])
    n_mods   = int(report.get("module_count") or 0)
    head     = f"{n_sheets} module sheet(s) for {n_mods} in-scope module(s)"

    bits: List[str] = []
    if report.get("absent"):
        bits.append("absent: " + ", ".join(report["absent"]))
    if report.get("in_rows_only"):
        bits.append("no tab: " + ", ".join(m for m, _ in report["in_rows_only"]))
    detail = " | ".join(bits)

    if report.get("needs_repair"):
        return f"[scope-gate] SHORT — {head} — {detail}"
    if report.get("advisory"):
        return (f"[scope-gate] FYI — {head}; reference organises by "
                f"'{report.get('shape')}', not by module, so structure is not "
                f"enforced here ({detail})")
    if report.get("naming_only"):
        return (f"[scope-gate] OK — {head}; unmatched by name only, assuming "
                f"an abbreviation ({detail})")
    return f"[scope-gate] OK — {head}, all matched ({report.get('shape')})"
