"""
input_fit.py — fit adhoc input files into the reading window, losing as little as
possible and saying exactly what was left out. ADHOC ONLY; cascade never calls it.

WHY THIS EXISTS
---------------
Every stage reads a fixed number of leading characters of the combined input. When
files were merged and then cut positionally, upload order decided what existed: with
ten Compensation files, the RTM — the document that says what to test — arrived after
one 180K workbook and was never read at all.

Fitting replaces that with two steps, applied ONLY when the input exceeds the window,
so anything that already fits is passed through untouched:

  1. Drop sheets that carry no deliverable content — translation tables, cover pages,
     tables of contents — from spreadsheets. A single "Translations" sheet holding
     field labels in a dozen languages was 18% of one measured workbook.

  2. If it still does not fit, give every file a fair share. Files smaller than the
     share are kept whole; the remaining room is divided equally among the larger
     ones. No file is left out entirely, and each cut lands on a line boundary with a
     marker so the model knows the file continues.
"""

from typing import Dict, List, Tuple

# Matched against the WHOLE sheet name, case-insensitively — never as a substring, so
# a sheet called "Translation Rules" or "Contents Approval" is kept.
LOW_VALUE_SHEETS = {
    "translations", "translation",
    "cover", "cover sheet", "cover page",
    "toc", "table of contents", "contents",
}
SPREADSHEET_EXTS = {"xlsx", "xlsm", "xls"}

# Room kept back per file for the "[... not shown ...]" marker and for the merge
# header's character count, whose digit count changes when a file is cut.
_MARKER_RESERVE = 120


def trim_low_value_sheets(text: str) -> Tuple[str, List[Tuple[str, int]]]:
    """
    Remove low-value sheets from extractor.py's spreadsheet text.

    That text is "[Sheet]" + rows, sheets joined by a blank line, and rows never
    contain blank lines. Splitting on blank-line-then-"[" and re-joining reproduces
    the input exactly, so when nothing matches the text is returned unchanged. A
    chunk is only ever dropped when its header is an exact low-value name, so a cell
    that happens to contain a blank line and a bracket cannot cause real data loss.

    Never empties a file: if every sheet were low-value, the text is kept as is.
    """
    if not text.startswith("["):
        return text, []
    raw = text.split("\n\n[")
    sheets = [raw[0]] + ["[" + chunk for chunk in raw[1:]]
    kept, dropped = [], []
    for sheet in sheets:
        head = sheet.split("\n", 1)[0].strip()
        name = head[1:-1] if head.startswith("[") and head.endswith("]") else head
        if name.strip().lower() in LOW_VALUE_SHEETS:
            dropped.append((name.strip(), len(sheet)))
        else:
            kept.append(sheet)
    if not dropped or not kept:
        return text, []
    return "\n\n".join(kept), dropped


def fair_shares(sizes: List[int], budget: int) -> List[int]:
    """
    Max-min fair allocation of `budget` characters across files of `sizes`.

    Smallest first: each file may take up to an equal split of what is left. A file
    smaller than that keeps everything and its unused room passes to the larger
    ones. Returns sizes unchanged when everything fits.
    """
    if sum(sizes) <= budget:
        return list(sizes)
    shares = [0] * len(sizes)
    remaining, left = max(0, budget), len(sizes)
    for i in sorted(range(len(sizes)), key=lambda k: sizes[k]):
        take = min(sizes[i], remaining // left)
        shares[i] = take
        remaining -= take
        left -= 1
    return shares


def _cut_point(text: str, limit: int) -> int:
    """Where to cut `text` so that it plus its marker fits in `limit`."""
    room = max(0, limit - _MARKER_RESERVE)
    cut = text.rfind("\n", 0, room)
    if cut < room // 2:            # no line break near the end: cut mid-line instead
        cut = room
    return cut


def cut_to(text: str, limit: int) -> str:
    """
    Shorten `text` to at most `limit` characters, at a line boundary, ending with a
    marker that tells the model the file continues. Unchanged if it already fits.
    """
    if len(text) <= limit:
        return text
    cut = _cut_point(text, limit)
    omitted = len(text) - cut
    return (text[:cut]
            + f"\n[... {omitted:,} more characters of this file not shown ...]")


def fit(parts: List[Dict[str, str]], overhead: int, window: int
        ) -> Tuple[List[Dict[str, str]], List[Dict[str, int]]]:
    """
    Cut `parts` so that their text plus `overhead` (headers and framing around them)
    fits in `window`. Returns (cut parts, one row per part: name, chars, kept).
    """
    sizes = [len(p.get("text") or "") for p in parts]
    budget = window - overhead - _MARKER_RESERVE * len(parts)
    shares = fair_shares(sizes, budget)
    out, rows = [], []
    for part, size, share in zip(parts, sizes, shares):
        text = part.get("text") or ""
        if share < size:
            kept = _cut_point(text, share)          # what actually survives the cut
            text = cut_to(text, share)
        else:
            kept = size
        out.append(dict(part, text=text))
        rows.append({"name": part.get("name") or "input", "chars": size, "kept": kept})
    return out, rows
