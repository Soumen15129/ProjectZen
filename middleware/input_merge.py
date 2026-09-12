"""
input_merge.py — combine several uploaded files into one input for generation.

WHY THIS EXISTS
---------------
Adhoc generation accepted exactly one file. A real engagement's scope arrives spread
across a SOW, a workbook, a deck and a process map, and asking the user to pick one
means the rest of the scope never reaches the pipeline — which is the same class of
failure as a section silently missing from the deliverable.

DESIGN NOTES
------------
PROVENANCE IS KEPT, NOT FLATTENED. Files are joined under explicit headers rather
than concatenated blind. The research stage builds its in-scope inventory from this
text, and "which document said this" is load-bearing when two sources disagree — a
figure in the SOW and a contradicting one in a workbook are a fact to reconcile, not
a coin toss.

OVER-BUDGET IS AN ERROR, NOT A SILENT TRIM. Quietly truncating is how scope goes
missing without anyone noticing; the user gets told, in plain words, which files were
too big and what to do. Individual extractors keep their own per-file ceiling, so the
only thing checked here is the combined total.

EXTRACTION QUALITY IS REPORTED PER FILE. The formats are not equal: .docx and .xlsx
come through faithfully, .vsdx recovers swimlanes and the full connector graph
(measured on a real customer process map), while .mpp is mined for strings out of a
binary container and loses dates, dependencies and hierarchy entirely. A user handing
over a .mpp deserves to know that before they judge the output.
"""

from typing import Any, Dict, List

# Roughly 200K tokens of English at ~4 chars/token, leaving generous room for the
# grounding reference, research pack, plan and schema that share the window.
MAX_MERGED_CHARS = 500_000

# Formats whose extraction is lossy enough that the user should be told.
DEGRADED_FORMATS = {
    "mpp": ("Microsoft Project files can only be read as loose text — task dates, "
            "dependencies and the task hierarchy are not recoverable. Exporting the "
            "plan to Excel or XML from MS Project will give a much better result."),
}


class InputTooLarge(Exception):
    """Raised when the combined files cannot fit. Carries a user-facing message."""

    def __init__(self, message: str, detail: Dict[str, Any]):
        super().__init__(message)
        self.message = message
        self.detail = detail


def _fmt_kb(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n >= 1024 else f"{n} chars"


def merge(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    files: [{"name": str, "ext": str, "text": str}, ...] — already extracted.

    Returns {"text", "files", "warnings", "total_chars"}.
    Raises InputTooLarge when the combined text will not fit.
    """
    usable = [f for f in files if (f.get("text") or "").strip()]
    empty = [f for f in files if not (f.get("text") or "").strip()]

    total = sum(len(f["text"]) for f in usable)
    if total > MAX_MERGED_CHARS:
        biggest = sorted(usable, key=lambda f: -len(f["text"]))
        lines = [f"  • {f['name']} — {_fmt_kb(len(f['text']))}" for f in biggest[:6]]
        raise InputTooLarge(
            "The files you selected are too large to process together.\n\n"
            f"Combined, they contain about {total // 1000:,}K characters of text, and "
            f"the limit is about {MAX_MERGED_CHARS // 1000:,}K.\n\n"
            "Largest files:\n" + "\n".join(lines) + "\n\n"
            "Please remove the largest file or two and try again. Documents that "
            "mostly contain images or slides often carry far more text than they "
            "appear to.",
            {"total_chars": total, "limit": MAX_MERGED_CHARS,
             "files": [{"name": f["name"], "chars": len(f["text"])} for f in biggest]},
        )

    warnings: List[Dict[str, str]] = []
    for f in usable:
        note = DEGRADED_FORMATS.get((f.get("ext") or "").lower())
        if note:
            warnings.append({"file": f["name"], "ext": f["ext"], "message": note})
    for f in empty:
        warnings.append({
            "file": f.get("name", "?"), "ext": f.get("ext", ""),
            "message": "No text could be read from this file, so it contributed "
                       "nothing to the document. It may be image-only, empty, or "
                       "password protected.",
        })

    if len(usable) == 1:
        # One file behaves exactly as before — no header, no change in what the
        # model sees. Multi-file support must not alter single-file output.
        body = usable[0]["text"]
    else:
        parts = [
            f"The following {len(usable)} source documents were provided together. "
            "Treat them as one combined input describing a single engagement. Where "
            "they overlap, prefer the more specific document and note any conflict.\n"
        ]
        for i, f in enumerate(usable, 1):
            parts.append(
                f"\n{'=' * 70}\n"
                f"SOURCE {i} of {len(usable)}: {f['name']}  "
                f"({(f.get('ext') or '?').upper()}, {len(f['text']):,} characters)\n"
                f"{'=' * 70}\n{f['text']}"
            )
        body = "\n".join(parts)

    return {
        "text": body,
        "total_chars": len(body),
        "files": [{"name": f["name"], "ext": f.get("ext", ""),
                   "chars": len(f["text"])} for f in usable],
        "skipped": [{"name": f.get("name", "?"), "ext": f.get("ext", "")} for f in empty],
        "warnings": warnings,
    }
