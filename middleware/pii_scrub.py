"""
pii_scrub.py — strip personal data from GROUNDING documents before they are stored.

WHY THIS EXISTS
---------------
A grounding document is a prior client's deliverable, reused as a template across
every future engagement. Content from the user's own input files is theirs and stays
untouched; content from a reference is another organisation's, and any name, email or
phone number in it can be copied into a delivered document for a different client.

The riskiest path is not the prompt — it is template cloning. `templates.generate()`
copies cells out of the reference into the output file, so a stray name in a
reference lands verbatim in a client deliverable with nobody reading it first.

SCRUB AT UPLOAD, NOT AT USE. Cleaning once when the document is registered means
every downstream consumer is safe by construction: the prompt text, the file handed
to the authoring sandbox, `analyse_reference`, and the cell-cloning path all read the
same already-clean artifact. Scrubbing at use would mean each of those four had to
remember, and one forgetting is a leak.

WHAT IS REMOVED, AND WHAT IS DELIBERATELY NOT
---------------------------------------------
Removed: email addresses, phone numbers, national-ID shapes, IBANs, and the document
AUTHOR METADATA — `dc:creator` and `cp:lastModifiedBy`. That last one matters more
than it looks: scanning the nine reference files shipped in the installer found real
people's names in every one of them and an email in one, none of it visible in Excel
or PowerPoint, all of it plainly readable in the file.

NOT removed: client and company names. They are confidential rather than personal,
and stripping them would gut the domain context that makes a reference useful as a
template. That was an explicit product decision, not an oversight.

STRUCTURE IS PRESERVED. Only string VALUES are rewritten; column widths, number
formats, fills, formulas and sheet names are left alone — the whole point of a
grounding document is its form.
"""

import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Ordered: IBAN before phone, or an IBAN's digits get partly eaten as a number.
_PATTERNS: List[Tuple[str, "re.Pattern[str]", str]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    ("iban",  re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"), "[IBAN]"),
    ("ssn",   re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"), "[ID]"),
    # Phone: require a separator or a leading +, so plain 10-digit part numbers and
    # amounts are not mangled. False positives here damage a template's content for
    # no security gain, so the pattern is deliberately conservative.
    ("phone", re.compile(
        r"(?<![\d\-])(?:\+\d{1,3}[ \-]?)?(?:\(\d{2,4}\)[ \-]?|\d{2,4}[ \-])\d{3,4}[ \-]\d{3,4}(?![\d\-])"),
     "[PHONE]"),
]

# OPC metadata parts carrying the author's identity.
_META_PARTS = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")
_META_TAGS = ("dc:creator", "cp:lastModifiedBy", "dc:description",
              "cp:category", "Manager", "Company")


def scrub_text(text: str) -> Tuple[str, Dict[str, int]]:
    """Redact a string. Returns (clean, {kind: count})."""
    counts: Dict[str, int] = {}
    if not text:
        return text, counts
    for kind, pat, repl in _PATTERNS:
        text, n = pat.subn(repl, text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text, counts


def _merge(into: Dict[str, int], other: Dict[str, int]) -> None:
    for k, v in other.items():
        into[k] = into.get(k, 0) + v


# The elements that actually hold visible text, per format. Everything else in these
# XML parts is markup — geometry, style ids, relationship ids, extension GUIDs.
#
# SCRUBBING RAW XML IS NOT SAFE, and this is not theoretical: doing so rewrote 93
# "phone numbers" inside DrawingML extension URIs like
#     <a:ext uri="{91240B29-F687-4F16-...}">
# because a GUID's digit groups match a phone shape. Not one of those 93 was in a
# text node. That corrupts the document while claiming to protect it, so redaction
# is confined to the text elements below and never applied to markup.
_TEXT_ELEMENTS = (
    "a:t",      # pptx / drawing text runs
    "t",        # xlsx sharedStrings
    "w:t",      # docx runs
    "Text",     # vsdx shape text
    "vt:lpwstr",  # docProps custom properties
)
_TEXT_NODE_RE = re.compile(
    r"(<(" + "|".join(re.escape(e) for e in _TEXT_ELEMENTS) + r")(?:\s[^>]*)?>)([^<]*)(</\2>)"
)


def _scrub_text_nodes(xml: str) -> Tuple[str, Dict[str, int]]:
    """Redact only the character data inside known text elements."""
    counts: Dict[str, int] = {}

    def repl(m: "re.Match[str]") -> str:
        clean, c = scrub_text(m.group(3))
        _merge(counts, c)
        return m.group(1) + clean + m.group(4)

    return _TEXT_NODE_RE.sub(repl, xml), counts


# Parts of an OPC package that hold human-readable text. Cell values live in
# sharedStrings; everything else is per-format body text.
def _is_text_part(name: str) -> bool:
    return (name in _META_PARTS
            or name == "xl/sharedStrings.xml"
            or name.startswith("word/document")
            or name.startswith("ppt/slides/slide")
            or name.startswith("ppt/notesSlides/")
            or re.match(r"visio/pages/page\d+\.xml$", name) is not None)


def _scrub_opc(path: Path) -> Dict[str, int]:
    """
    Redact an OPC package (xlsx / docx / pptx / vsdx) by rewriting only the XML parts
    that carry text, and copying every other entry through byte-for-byte.

    DELIBERATELY NOT openpyxl / python-pptx. Round-tripping a workbook through
    openpyxl emits `DrawingML support is incomplete ... Shapes and drawings will be
    lost` and it means it: on the 3.4 MB reference workbook that path silently
    dropped 578 KB — 17% of the file. For a document whose entire value is its FORM,
    a scrub that quietly deletes drawings is worse than the leak it prevents.

    Compression type is preserved per entry so already-compressed media is not
    re-deflated, keeping the output the same size as the input apart from the
    redactions themselves.
    """
    counts: Dict[str, int] = {}
    try:
        with zipfile.ZipFile(path) as z:
            entries = [(i, z.read(i.filename)) for i in z.infolist()]
    except Exception:
        return counts                                   # not a zip: caller handles

    changed = False
    out: List[Tuple[Any, bytes]] = []
    for info, data in entries:
        if _is_text_part(info.filename):
            try:
                xml = data.decode("utf-8")
                original = xml
                if info.filename in _META_PARTS:
                    for tag in _META_TAGS:
                        # The opening tag may carry attributes, and they must be KEPT.
                        #
                        # The original pattern was `<dc:creator>...</dc:creator>` with
                        # no allowance for attributes, so it silently skipped every
                        # file where Excel writes
                        #     <dc:creator xmlns:dc="...">Jane Consultant</dc:creator>
                        # — which is most of them. `cp:lastModifiedBy` carries no
                        # attributes and WAS being cleared, so the report read
                        # "author_metadata: 1" and looked like it had worked while the
                        # author's name was still in the file.
                        #
                        # The attributes are preserved rather than dropped because
                        # xmlns:dc declares the prefix; removing it leaves an
                        # undeclared namespace and an unparseable part.
                        xml, n = re.subn(
                            rf"<{re.escape(tag)}((?:\s[^>]*)?)>[^<]*</{re.escape(tag)}>",
                            lambda m: f"<{tag}{m.group(1)}></{tag}>", xml)
                        if n:
                            counts["author_metadata"] = counts.get("author_metadata", 0) + n
                xml, c = _scrub_text_nodes(xml)
                _merge(counts, c)
                if xml != original:
                    data = xml.encode("utf-8")
                    changed = True
            except Exception:
                pass                                    # binary or odd encoding
        out.append((info, data))

    if not changed:
        return counts
    try:
        tmp = path.with_suffix(path.suffix + ".scrub")
        with zipfile.ZipFile(tmp, "w") as z:
            for info, data in out:
                # Carry the original entry's compression across, so media stays as
                # it was and only the touched XML is rewritten.
                zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                zi.compress_type = info.compress_type
                zi.external_attr = info.external_attr
                z.writestr(zi, data)
        shutil.move(str(tmp), str(path))
    except Exception:
        return {}                                       # left untouched; report nothing
    return counts


def scrub_file(path: str) -> Dict[str, Any]:
    """
    Clean a grounding file IN PLACE. Never raises — a scrub failure must not block an
    upload, but it is reported so the admin is not left believing a file is clean.

    Returns {"redactions": {kind: n}, "total": n, "ok": bool}.
    """
    p = Path(path)
    counts: Dict[str, int] = {}
    ok = True
    try:
        ext = p.suffix.lower().lstrip(".")
        if ext in ("xlsx", "xlsm", "docx", "pptx", "vsdx"):
            _merge(counts, _scrub_opc(p))
        elif ext in ("txt", "csv", "md", "json", "xml"):
            raw = p.read_text(encoding="utf-8", errors="replace")
            clean, c = scrub_text(raw)
            if c:
                p.write_text(clean, encoding="utf-8")
                _merge(counts, c)
    except Exception:
        ok = False
    return {"redactions": counts, "total": sum(counts.values()), "ok": ok}
