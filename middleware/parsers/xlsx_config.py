"""
parsers/xlsx_config.py — Structured configuration workbook parser.

Converts the tab-CSV text that extractor.py produces from uploaded xlsx files
into a compact, field-level summary that Claude uses instead of inferring
field names, types, and picklist values from free-form text.

This is Item 06 from the Portugal TDLC quality adoption plan.
ADHOC ONLY — never imported from cascade code paths.

──────────────────────────────────────────────────────────────────────────
INPUT FORMAT  (what extractor.py produces for every xlsx):

  [Sheet Name]
  col1,col2,col3,...
  val1,val2,val3,...
  ...

  [Sheet Name 2]
  col1,col2,...
  ...

OUTPUT FORMAT  (compact, ~50 chars/field; replaces ~150-300 chars of raw CSV):

  CONFIGURATION WORKBOOK — STRUCTURED DATA:
  [Tab: Personal Information (18 fields)]
    hireDate | Hire Date | Date | Required
    employmentType | Employment Type | Picklist | Required | Values: Full Time; Part Time; ...
  ...

QUALITY BENEFIT:
  Claude reads exact SF field API names (hireDate, not "hire date") and exact
  picklist values ("Full Time", not "full-time") — no guessing, no paraphrasing.
  Test scripts, RTM rows, and config workbooks become immediately executable.
──────────────────────────────────────────────────────────────────────────
"""

import re
from typing import List, Optional, Tuple

# ── Tab filtering ──────────────────────────────────────────────────────────
#
# Metadata tabs (instructions, legends, version history) are skipped because
# they contain no field configuration data.

_SKIP_TAB_RE = re.compile(
    r"(?:^|\b)(?:instruction|version|cover|index|change.?log|"
    r"revision|history|legend|readme|notes|template.?list|"
    r"picklist.?master|ref(?:erence)?|summary|overview|toc|"
    r"contents|navigation|glossary|abbreviation)(?:\b|$)",
    re.IGNORECASE,
)

_CONFIG_TAB_RE = re.compile(
    r"(?:employee|personal|job|position|pay|benefit|compensation|"
    r"workflow|notification|role|permission|rbp|group|"
    r"field|config|form|section|foundation|org|entity|object|"
    r"onboard|recruit|perform|goal|learn|succession|talent|"
    r"time|absence|compliance|document|attachment|approval)",
    re.IGNORECASE,
)


def _is_metadata_tab(name: str) -> bool:
    """True for tabs that should be skipped (instructions, indexes, legends)."""
    return bool(_SKIP_TAB_RE.search(name))


def _looks_like_config_tab(name: str) -> bool:
    """True for tabs that are likely SF configuration field definitions."""
    return bool(_CONFIG_TAB_RE.search(name))


# ── Column header matching ──────────────────────────────────────────────────

def _find_col(headers: List[str], keywords: Tuple[str, ...]) -> Optional[int]:
    """
    Return the index of the first header whose lowercase value contains
    any of the supplied keywords.  Case-insensitive substring match.
    """
    h_lower = [h.lower().strip() for h in headers]
    for kw in keywords:
        for i, h in enumerate(h_lower):
            if kw in h:
                return i
    return None


# ── CSV row splitting ───────────────────────────────────────────────────────

def _split_row(line: str, n_cols: int) -> List[str]:
    """
    Split a comma-joined row (extractor.py format) into exactly n_cols cells.

    extractor.py joins xlsx cells with plain commas and no quoting, so a cell
    that contains a comma (common in picklist values) produces more parts than
    columns.  We fold the overflow back into the last column so that picklist
    cells remain intact.
    """
    parts = line.split(",")
    if len(parts) <= n_cols:
        return parts + [""] * (n_cols - len(parts))   # pad short rows
    # More parts than columns: last n_cols-1 cells are clean, the rest is
    # the last column with its embedded commas restored.
    result = parts[:n_cols - 1]
    result.append(",".join(parts[n_cols - 1:]))
    return result


# ── Parser constants ────────────────────────────────────────────────────────

_MAX_FIELDS_PER_TAB  = 60    # cap per tab — keeps block token-efficient
_MAX_TABS            = 12    # cap on total tabs parsed
_MAX_PICKLIST_CHARS  = 120   # max chars for the picklist value summary
_MIN_DATA_ROWS       = 1     # skip tabs with fewer data rows than this
_MIN_RECOGNISABLE_COLS = 2   # skip tabs with fewer columns than this


# ── Main entry point ────────────────────────────────────────────────────────

def parse_xlsx_extraction(input_text: str) -> Optional[str]:
    """
    Parse the tab-CSV text produced by extractor.py for xlsx files.

    Returns a compact, field-level prompt block when the input looks like a
    Salesforce configuration workbook, or None otherwise.

    Never raises — any exception should be caught by the caller so the
    adhoc pipeline fails open to plain-text extraction.
    """
    stripped = input_text.strip()

    # Fast gate: xlsx extraction always starts with "[Sheet Name]"
    if not stripped.startswith("["):
        return None

    # Split on blank lines that precede a new tab header line
    raw_tabs = re.split(r"\n\n+(?=\[)", stripped)
    if not raw_tabs:
        return None

    config_tabs_output: List[str] = []
    total_fields = 0

    for raw_tab in raw_tabs:
        if len(config_tabs_output) >= _MAX_TABS:
            break

        lines = [ln for ln in raw_tab.split("\n") if ln.strip()]
        if not lines:
            continue

        # ── Extract tab name ──────────────────────────────────────────────
        m = re.match(r"^\[(.+)\]$", lines[0].strip())
        if not m:
            continue
        tab_name = m.group(1).strip()

        if _is_metadata_tab(tab_name):
            continue

        data_lines = lines[1:]
        if len(data_lines) < _MIN_DATA_ROWS + 1:   # header + at least one row
            continue

        # ── Parse header row ──────────────────────────────────────────────
        headers_raw = data_lines[0].split(",")
        headers = [h.strip() for h in headers_raw]
        n_cols = len(headers)

        if n_cols < _MIN_RECOGNISABLE_COLS:
            continue

        # ── Identify key columns ──────────────────────────────────────────
        label_idx    = _find_col(headers, (
            "field label", "label", "field name", "name",
            "display name", "ui label", "description",
        ))
        api_idx      = _find_col(headers, (
            "api name", "api id", "field id", "field_name",
            "technical name", "api", "id", "code", "element",
        ))
        type_idx     = _find_col(headers, (
            "type", "field type", "data type", "element type", "format",
        ))
        required_idx = _find_col(headers, (
            "required", "mandatory", "required?", "mandatory?", "req",
        ))
        picklist_idx = _find_col(headers, (
            "values", "picklist", "options", "allowed values",
            "valid values", "list values", "value list", "picklist values",
            "available values", "dropdown values",
        ))

        # ── Fallback when no label/api column is identified ───────────────
        # Some workbooks use column 0 as an implicit field name.  Accept this
        # only when the first few values in that column look like identifiers
        # (short, no spaces — typical of SF technical names).
        if label_idx is None and api_idx is None:
            first_vals = []
            for raw in data_lines[1:6]:
                row = _split_row(raw, n_cols)
                first_vals.append(row[0].strip() if row else "")
            candidate_ids = [v for v in first_vals if v and len(v) < 50]
            no_spaces     = sum(1 for v in candidate_ids if " " not in v)
            if candidate_ids and no_spaces >= len(candidate_ids) * 0.6:
                api_idx = 0
            else:
                # Cannot identify field columns — skip this tab
                continue

        # ── Parse data rows ───────────────────────────────────────────────
        field_lines: List[str] = []
        for row_line in data_lines[1:]:
            if len(field_lines) >= _MAX_FIELDS_PER_TAB:
                break
            if not row_line.strip():
                continue

            row = _split_row(row_line, n_cols)

            def _get(idx: Optional[int]) -> str:
                if idx is None or idx >= len(row):
                    return ""
                return row[idx].strip()

            label    = _get(label_idx)
            api_name = _get(api_idx)
            ftype    = _get(type_idx)
            req_raw  = _get(required_idx)
            picklist = _get(picklist_idx)

            # Skip entirely empty rows
            if not label and not api_name:
                continue
            # Skip header-repeat rows (sometimes workbooks embed a repeated header)
            if label and label.lower() in ("field label", "label", "field name",
                                           "name", "display name"):
                continue

            # Required flag — normalise various spellings
            req_flag = ""
            if req_raw:
                req_flag = ("Required" if req_raw.lower() in
                            ("yes", "y", "true", "1", "x",
                             "required", "mandatory", "req")
                            else "Optional")

            # Build compact field line: api_name | label | type | required [| values]
            parts: List[str] = []
            if api_name:
                parts.append(api_name)
            if label and label.lower() != (api_name or "").lower():
                parts.append(label)
            if ftype:
                parts.append(ftype)
            if req_flag:
                parts.append(req_flag)

            if picklist:
                # Normalise separator to "; ", deduplicate, limit length
                raw_vals = re.split(r"[|;\n\r]", picklist)
                vals = []
                seen: set = set()
                for v in raw_vals:
                    v = v.strip().strip('"\'')
                    if v and v.lower() not in seen:
                        vals.append(v)
                        seen.add(v.lower())
                if vals:
                    summary = "; ".join(vals)
                    if len(summary) > _MAX_PICKLIST_CHARS:
                        # Truncate at last complete value
                        summary = summary[:_MAX_PICKLIST_CHARS]
                        last_semi = summary.rfind(";")
                        if last_semi > 0:
                            summary = summary[:last_semi] + "; …"
                    parts.append(f"Values: {summary}")

            if parts:
                field_lines.append("  " + " | ".join(parts))

        if field_lines:
            config_tabs_output.append(
                f"[Tab: {tab_name} ({len(field_lines)} fields)]\n"
                + "\n".join(field_lines)
            )
            total_fields += len(field_lines)

    if not config_tabs_output:
        return None

    # Build the final prompt block
    header = (
        "CONFIGURATION WORKBOOK — STRUCTURED DATA:\n"
        "The following fields were parsed directly from the uploaded "
        "configuration workbook.\n"
        "RULE: Use the exact identifiers, types, and picklist values listed "
        "below — do NOT guess, paraphrase, or invent alternatives.\n"
        "When writing test steps, RTM rows, or configuration tables, reference "
        "the exact API name shown here (e.g. hireDate, not 'Hire Date').\n\n"
    )
    footer = (
        f"\n[END STRUCTURED DATA — {total_fields} fields across "
        f"{len(config_tabs_output)} tab(s)]\n"
    )
    return header + "\n\n".join(config_tabs_output) + footer
