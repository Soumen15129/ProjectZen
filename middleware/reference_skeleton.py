"""
reference_skeleton.py — describe a reference workbook's FORM for the prompt.

Extracted reference text is cut at a fixed length, so for a large workbook the model
saw its first few sheets and never the column layout of the rest. This reads every
sheet and returns its exact headers plus a few example rows, small enough for every
pipeline stage to see in full. Adhoc only; used for templates in
adhoc_pipeline.LONG_CELL_NODES.
"""

import re
from typing import Any, List

# The reference's record of its own revisions: not part of the deliverable's form.
HISTORY_SHEETS = {"review findings", "v1 disposition"}

MAX_CHARS = 30_000          # research, plan and review read the reference up to 40,000
_CELL_MAX = 400
_RAW_ROWS = 8


def _txt(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return re.sub(r"\s*\n\s*", " / ", str(v)).strip()


def is_history_sheet(name: str) -> bool:
    n = name.strip().lower()
    return n in HISTORY_SHEETS or bool(re.search(r"\bdisposition\b", n))


def _cut(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit].rstrip() + " ..."


def _header_index(rows: List[List[str]]) -> int:
    """Row with the most filled cells among the first six, or -1 if none has three."""
    best, best_n = -1, 2
    for i, r in enumerate(rows[:6]):
        n = sum(1 for c in r if c)
        if n > best_n:
            best, best_n = i, n
    return best


def _examples(header: List[str], body: List[List[str]], n: int) -> List[List[str]]:
    if any(h.lower() in ("step no", "step no.", "step id") for h in header):
        runs, start = [], 0
        for i in range(1, len(body) + 1):
            if i == len(body) or body[i][0] != body[start][0]:
                runs.append(body[start:i])
                start = i
        run = next((r for r in runs if len(r) >= 4), max(runs, key=len) if runs else [])
        return run[:6]
    ranked = sorted(range(len(body)),
                    key=lambda i: (-sum(1 for c in body[i] if c), -sum(map(len, body[i])), i))
    return [body[i] for i in sorted(ranked[:n])]


def _render(sheets, n_examples: int, cell_max: int) -> str:
    out = [
        "REFERENCE WORKBOOK SKELETON - read from the reference file, every sheet.",
        "The reference belongs to ANOTHER project. Copy its FORM: sheet order, the exact "
        "column headers of each sheet, the ID patterns, and the sentence style and level "
        "of detail of the example rows. Never copy its content, modules, countries or "
        "roles. Sheets recording the reference's own revision history are omitted on "
        "purpose; do not recreate them.",
        "",
    ]
    for name, rows in sheets:
        h = _header_index(rows)
        if h < 0 or len(rows) - h - 1 < 1:
            out.append(f'SHEET "{name}" - {len(rows)} rows in the reference. First rows:')
            for r in rows[:_RAW_ROWS]:
                out.append("    " + _cut(" | ".join(c for c in r if c), cell_max))
            out.append("")
            continue
        width = max(i for i, c in enumerate(rows[h]) if c) + 1
        header = [c or f"(column {i + 1})" for i, c in enumerate(rows[h][:width])]
        body = [(r + [""] * width)[:width] for r in rows[h + 1:]]
        out.append(f'SHEET "{name}" - {len(body)} data rows in the reference')
        if h:
            out.append("  Title rows: " + " / ".join(_cut(" ".join(c for c in r if c), 160)
                                                   for r in rows[:h]))
        out.append(f"  COLUMNS ({len(header)}, use exactly): " + " | ".join(header))
        out.append("  EXAMPLE ROWS (form and level of detail only):")
        for k, r in enumerate(_examples(header, body, n_examples), 1):
            pairs = [f"{header[i]}: {_cut(c, cell_max)}" for i, c in enumerate(r) if c]
            out.append(f"    [{k}] " + " | ".join(pairs))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def build(path: str, max_chars: int = MAX_CHARS) -> str:
    """The skeleton text, or "" when the file is not a readable workbook."""
    if not path or not path.lower().endswith((".xlsx", ".xlsm")):
        return ""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheets = []
        for ws in wb.worksheets:
            if is_history_sheet(ws.title):
                continue
            rows = [[_txt(c) for c in r] for r in ws.iter_rows(values_only=True)]
            rows = [r for r in rows if any(r)]
            if rows:
                sheets.append((ws.title, rows))
        wb.close()
    except Exception:
        return ""
    if not sheets:
        return ""
    for n_examples, cell_max in ((3, _CELL_MAX), (2, _CELL_MAX), (2, 250), (1, 200)):
        text = _render(sheets, n_examples, cell_max)
        if len(text) <= max_chars:
            return text
    return text[:max_chars]


def drop_history_parts(hint: str) -> str:
    """Remove history sheets from scope_gate.reference_organisation_hint() output."""
    if not hint:
        return hint
    lines = hint.split("\n")
    kept = [ln for ln in lines
            if not (re.match(r"^\s+- ", ln) and is_history_sheet(ln.strip()[2:].split(":")[0]))]
    removed = len(lines) - len(kept)
    text = "\n".join(kept)
    if removed:
        text = re.sub(r"into (\d+) parts",
                      lambda m: f"into {int(m.group(1)) - removed} parts", text, count=1)
    return text
