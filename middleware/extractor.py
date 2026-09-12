"""
extractor.py — File text extraction for all supported formats.
Used both for input files (to feed Claude) and generated files (to build content_summary).
FIX A preserved: XML/txt/csv return raw text directly.
"""

import os
from typing import Optional

# Per-file extraction ceiling.
#
# This used to be the literal 80000 repeated in every branch, applied silently: a
# 27 MB deck arrived as its first 80,000 characters with no signal anywhere, which
# is how whole sections of scope go missing without anyone noticing.
#
# The DEFAULT IS DELIBERATELY UNCHANGED so cascade — which was measured and tuned
# against exactly this behaviour — is byte-for-byte identical. Only callers that
# ask for more get more, and today that is the adhoc input path alone.
DEFAULT_MAX_CHARS = 80000


def extract_text(file_path: str, ext: Optional[str] = None,
                 max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """
    Extract plain text from a file based on its extension.
    Returns empty string on failure — never raises.
    """
    if not ext:
        ext = os.path.splitext(file_path)[1].lstrip(".").lower()
    ext = ext.lower().strip(".")

    try:
        # ── DOCX ──────────────────────────────────────────────────────────
        if ext in ("docx", "doc"):
            import mammoth
            with open(file_path, "rb") as f:
                result = mammoth.extract_raw_text(f)
            return (result.value or "")[:max_chars]

        # ── XLSX / XLS ────────────────────────────────────────────────────
        if ext in ("xlsx", "xls"):
            import openpyxl
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            parts = []
            for name in wb.sheetnames:
                ws = wb[name]
                rows = []
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    if any(cells):
                        rows.append(",".join(cells))
                parts.append(f"[{name}]\n" + "\n".join(rows))
            return "\n\n".join(parts)[:max_chars]

        # ── PPTX ──────────────────────────────────────────────────────────
        # The previous version walked only slide.shapes at the top level and read
        # shape.text. That silently dropped three whole classes of content:
        #
        #   * GROUP shapes      — never recursed into
        #   * TABLES            — a GraphicFrame has no .text at all, so every
        #                         RACI grid, test-phase matrix and migration-wave
        #                         table extracted as nothing
        #   * SPEAKER NOTES     — never touched
        #
        # Measured across the reference decks in this estate, tables alone were
        # 1,278-21,350 chars per deck of pure loss; the Migration Strategy deck
        # extracted 62% short. Strategy decks keep their substance in tables, so
        # this was the bulk of what the grounding reference was meant to supply.
        #
        # Slide markers are also new: the old output was one flat run of shape text
        # with no slide boundaries, leaving no way to tell where a topic ended.
        if ext in ("pptx", "ppt"):
            from pptx import Presentation
            from pptx.enum.shapes import MSO_SHAPE_TYPE

            def _walk(shapes):
                """Yield every shape, descending into groups."""
                for shape in shapes:
                    yield shape
                    try:
                        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                            yield from _walk(shape.shapes)
                    except Exception:
                        pass

            prs = Presentation(file_path)
            lines = []
            for idx, slide in enumerate(prs.slides, 1):
                slide_parts = []
                for shape in _walk(slide.shapes):
                    try:
                        if getattr(shape, "has_table", False):
                            for row in shape.table.rows:
                                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                                if any(cells):
                                    slide_parts.append(" | ".join(cells))
                            continue
                        if shape.has_text_frame:
                            text = shape.text_frame.text.strip()
                            if text:
                                slide_parts.append(text)
                    except Exception:
                        continue
                try:
                    if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                        note = slide.notes_slide.notes_text_frame.text.strip()
                        if note:
                            slide_parts.append(f"[speaker notes] {note}")
                except Exception:
                    pass
                if slide_parts:
                    lines.append(f"--- Slide {idx} ---\n" + "\n".join(slide_parts))
            return "\n\n".join(lines)[:max_chars]

        # ── PDF ───────────────────────────────────────────────────────────
        if ext == "pdf":
            text = ""
            # Primary: pdfminer.six
            try:
                from pdfminer.high_level import extract_text as pdfminer_extract
                text = (pdfminer_extract(file_path) or "").strip()
            except Exception as e:
                print(f"   ⚠ pdfminer failed ({e}), trying pypdf")

            # Fallback: pypdf
            if not text:
                try:
                    import pypdf
                    reader = pypdf.PdfReader(file_path)
                    pages = []
                    for page in reader.pages:
                        t = page.extract_text()
                        if t:
                            pages.append(t)
                    text = "\n".join(pages).strip()
                except Exception as e:
                    print(f"   ⚠ pypdf failed ({e})")

            return text[:max_chars]

        # ── FIX A: XML / TXT / CSV — return raw text ──────────────────────
        if ext in ("xml", "txt", "csv", "md", "json"):
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[:max_chars]

        # ── MPP (Microsoft Project) ────────────────────────────────────────
        # ── VSDX (Visio) ──────────────────────────────────────────────────
        # A .vsdx is an OPC/ZIP package like .docx, so no new dependency is
        # needed. Shape text alone would be a bag of words; a process map only
        # means something if the FLOW survives. Measured against a real customer
        # map (Dabur To-Be), this recovers the swimlanes, the shape roles
        # (Decision vs Subprocess) and all 15 connector edges — i.e. the actual
        # process, not a word list.
        if ext == "vsdx":
            return _extract_vsdx(file_path)[:max_chars]

        if ext == "mpp":
            import re
            with open(file_path, "rb") as f:
                raw = f.read()
            # .mpp is an OLE compound document — task names, resource names,
            # and notes are stored as plain ASCII / UTF-16LE strings inside
            # it. Mining those directly is far simpler and more reliable
            # than parsing the binary structure, and is the same technique
            # workbook_processor.py already uses for this format.
            found = set()
            for m in re.finditer(rb"[ -~]{4,}", raw):
                s = m.group().decode("ascii", errors="ignore").strip()
                if s:
                    found.add(s)
            for m in re.finditer(rb"(?:[\x20-\x7e]\x00){4,}", raw):
                try:
                    s = m.group().decode("utf-16-le", errors="ignore").strip()
                    if s:
                        found.add(s)
                except Exception:
                    pass
            ordered = sorted(found, key=len, reverse=True)
            return "\n".join(ordered)[:max_chars]

    except Exception as e:
        print(f"   ⚠ extractor error ({ext}): {e}")

    return ""


# ── Visio ────────────────────────────────────────────────────────────────────
_VSDX_NS = {"v": "http://schemas.microsoft.com/office/visio/2012/main"}


def _vsdx_shape_text(shape) -> str:
    """Text of one shape, joining the <t> runs Visio splits on formatting."""
    import re
    t = shape.find("v:Text", _VSDX_NS)
    if t is None:
        return ""
    parts = [t.text or ""]
    for child in t:
        parts.append(child.tail or "")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _extract_vsdx(file_path: str) -> str:
    """
    Recover a Visio drawing as text: swimlanes, elements with their shape role,
    and the connector graph.

    The connector graph is the point. <Connect> rows carry FromSheet/ToSheet with a
    FromCell of BeginX/EndX, so a connector's two ends can be resolved back to the
    shapes it joins — turning a pile of boxes into "A -> B -> C". Master names give
    each shape its role, which is how a Decision is distinguished from a step.
    """
    import re
    import zipfile
    from collections import defaultdict
    from xml.etree import ElementTree as ET

    z = zipfile.ZipFile(file_path)

    masters = {}
    try:
        mroot = ET.fromstring(z.read("visio/masters/masters.xml"))
        for m in mroot.findall("v:Master", _VSDX_NS):
            masters[m.get("ID")] = m.get("NameU") or m.get("Name") or ""
    except Exception:
        pass

    page_names = {}
    try:
        proot = ET.fromstring(z.read("visio/pages/pages.xml"))
        for i, p in enumerate(proot.findall("v:Page", _VSDX_NS), 1):
            page_names[i] = p.get("Name") or p.get("NameU") or f"Page {i}"
    except Exception:
        pass

    out = []
    for idx, pf in enumerate(
            sorted(n for n in z.namelist()
                   if re.match(r"visio/pages/page\d+\.xml$", n)), 1):
        try:
            root = ET.fromstring(z.read(pf))
        except Exception:
            continue

        roles, texts = {}, {}

        def walk(node):
            for s in node.findall("v:Shapes/v:Shape", _VSDX_NS):
                sid = s.get("ID")
                roles[sid] = masters.get(s.get("Master") or s.get("MasterShape") or "", "")
                txt = _vsdx_shape_text(s)
                if txt and txt not in ("`", "'"):      # Visio leaves stray marks
                    texts[sid] = txt
                walk(s)
        walk(root)

        flow = defaultdict(lambda: {"from": [], "to": []})
        for c in root.findall(".//v:Connect", _VSDX_NS):
            cell = c.get("FromCell", "")
            if cell.startswith("Begin"):
                flow[c.get("FromSheet")]["from"].append(c.get("ToSheet"))
            elif cell.startswith("End"):
                flow[c.get("FromSheet")]["to"].append(c.get("ToSheet"))

        lanes = [t for sid, t in texts.items() if roles.get(sid) == "Separator"]
        out.append(f"## Visio page: {page_names.get(idx, f'Page {idx}')}")
        if lanes:
            out.append(f"Swimlanes / bands: {', '.join(lanes)}")
        out.append("")
        out.append("### Elements")
        for sid, txt in texts.items():
            role = roles.get(sid) or "shape"
            out.append(f"- [{role}] {txt}")

        edges = []
        for cid, ends in flow.items():
            src = (ends["from"] or [None])[0]
            dst = (ends["to"] or [None])[0]
            if not (src and dst):
                continue
            a = texts.get(src) or f"<{roles.get(src) or 'shape'}>"
            b = texts.get(dst) or f"<{roles.get(dst) or 'shape'}>"
            label = texts.get(cid, "")
            edges.append(f"- {a} --{label + '-->' if label else '-->'} {b}")
        if edges:
            out.append("")
            out.append("### Flow")
            out.extend(edges)
        out.append("")

    return "\n".join(out)


def extract_from_bytes(content: bytes, ext: str,
                       max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """
    Extract text from raw bytes (used for uploaded files sent as base64).
    Writes to a temp file, extracts, cleans up.
    """
    import tempfile

    ext = ext.lower().strip(".")
    suffix = f".{ext}"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        return extract_text(tmp_path, ext, max_chars=max_chars)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
