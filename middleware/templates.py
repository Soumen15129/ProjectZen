"""
templates.py — All 5 document generators as direct Python functions.
No subprocess. Called directly from app.py.

All 4 XML fixes preserved:
  FIX A: XML reference read as raw text (handled in extractor.py)
  FIX B: sanitize_xml_ref() strips attr values, keeps tag/attr names
  FIX C: XML prompt labels reference as EXACT SCHEMA (in app.py / agent.py)
  FIX D: generate_xml() parses ref_schema to discover real tag/attr names
"""

import os
import re
import json
import uuid
import xml.etree.ElementTree as ET
from xml.dom import minidom
from typing import Any, Dict, Optional


# ── Helpers ───────────────────────────────────────────────────────────────

def _safe(v: Any, maxlen: int = 120) -> str:
    if v is None:
        return ""
    s = str(v)
    s = "".join(c if 32 <= ord(c) < 127 else " " for c in s)
    return s[:maxlen].strip()


def _safe_tag(name: str) -> str:
    """Make a string safe to use as an XML tag name."""
    import re
    s = re.sub(r"[^a-zA-Z0-9_\-.]", "_", str(name))
    if s and s[0].isdigit():
        s = "_" + s
    return s or "Field"


# ── FIX B: sanitize_xml_ref ───────────────────────────────────────────────

def sanitize_xml_ref(xml_text: str) -> str:
    """
    Strips attribute VALUES but keeps attribute NAMES intact.
    Claude sees the real schema shape without noise from actual data values.
    """
    if not xml_text:
        return ""
    import re
    s = "".join(c if 32 <= ord(c) < 128 or c in "\t\n\r" else " " for c in xml_text)
    s = re.sub(r'="[^"]*"', '="..."', s)
    s = re.sub(r"\s{3,}", " ", s)
    return s.strip()[:12000]


# ═══════════════════════════════════════════════════════════════════════════
# XLSX
# ═══════════════════════════════════════════════════════════════════════════

def _xlsx_extract_theme(grounding_path: str):
    """
    Extract header fill colour and alt-row fill colour from the first sheet
    of the grounding XLSX.  Returns (hdr_hex, alt_hex) or (None, None) on failure.
    Only reads cells from the first 6 rows; opens without read_only so styles
    are available.  Silently ignores any error.
    """
    try:
        from openpyxl import load_workbook
        twb = load_workbook(grounding_path, data_only=True)
        tws = twb.worksheets[0]
        hdr_hex = None
        alt_hex = None
        for row in tws.iter_rows(min_row=1, max_row=6):
            for cell in row:
                fill = cell.fill
                if fill and fill.fill_type == "solid":
                    fg = fill.fgColor
                    if fg and fg.type == "rgb":
                        rgb = fg.rgb
                        # Strip leading alpha byte if present (e.g. FF1F497D -> 1F497D)
                        if len(rgb) == 8:
                            rgb = rgb[2:]
                        if len(rgb) == 6 and rgb.upper() not in ("000000", "FFFFFF"):
                            if hdr_hex is None:
                                hdr_hex = rgb.upper()
                            elif rgb.upper() != hdr_hex:
                                alt_hex = rgb.upper()
                                break
            if hdr_hex and alt_hex:
                break
        twb.close()
        return hdr_hex, alt_hex
    except Exception:
        return None, None


def generate_xlsx(plan: Dict, output_path: str, grounding_path: str = "") -> str:
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Border, Side, Alignment
    from openpyxl.chart import BarChart, Reference
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)

    # ── Try to pull colours from the grounding template ──────────────────
    hdr_hex, alt_hex = None, None
    if grounding_path and grounding_path.lower().endswith((".xlsx", ".xls")):
        hdr_hex, alt_hex = _xlsx_extract_theme(grounding_path)
        if hdr_hex:
            print(f"   Using XLSX template colours: hdr=#{hdr_hex} alt=#{alt_hex or 'default'}")

    HDR_FILL = PatternFill("solid", fgColor=hdr_hex or "1F497D")
    ALT_FILL = PatternFill("solid", fgColor=alt_hex or "DCE6F1")
    HDR_FONT = Font(bold=True, color="FFFFFF", size=11)
    TTL_FONT = Font(bold=True, size=14, color=hdr_hex or "1F497D")
    THIN     = Side(style="thin", color="B8CCE4")
    BORDER   = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
    CENTER   = Alignment(horizontal="center", vertical="center")

    # ── Status-aware fills (Items 01, 07, 02) ─────────────────────────────
    # Additive: existing output is byte-identical when the plan has no
    # Status or Notes column.  Applied only inside generate_xlsx — no
    # cascade code is involved.
    TBC_FILL     = PatternFill("solid", fgColor="FFF3CD")  # amber  — TBC / pending
    OOS_FILL     = PatternFill("solid", fgColor="E0E0E0")  # grey   — out of scope
    OOS_FONT     = Font(color="888888", strike=True)        # grey strikethrough
    AI_CELL_FILL = PatternFill("solid", fgColor="FFFDE7")  # light amber — AI-draft note

    for sheet in plan.get("sheets", []):
        ws      = wb.create_sheet(title=_safe(sheet.get("name", "Sheet"))[:31])
        headers = [_safe(h) for h in sheet.get("headers", [])]
        rows    = sheet.get("rows", [])
        n_cols  = max(len(headers), 1)

        # Headers on ROW 1, with no merged title banner above them.
        #
        # The banner cost more than it gave. A merged A1:K1 cell breaks sort, filter
        # and "Format as Table" — the three things anyone actually does to a delivery
        # spreadsheet — and it pushed headers to row 2, which is not where any tool
        # (or reader) looks for them. Real reference plans in this estate put headers
        # on row 1. The title already lives in the filename and the document metadata.
        for c, h in enumerate(headers, 1):
            cell = ws.cell(1, c, h)
            cell.fill = HDR_FILL
            cell.font = HDR_FONT
            cell.border = BORDER
            cell.alignment = CENTER
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = (
            f"A1:{get_column_letter(n_cols)}{len(rows) + 1}" if rows else None)

        # ── Detect status / notes column indices once per sheet ───────────
        # Status column → TBC (amber) / OOS (grey-strikethrough) row fills.
        # Notes  column → AI-draft accent on the specific cell.
        # Both are purely optional: if absent, coloring falls back to the
        # existing alt-row behaviour — no change to existing documents.
        _h_lower      = [h.strip().lower() for h in headers]
        _STATUS_NAMES = {"status", "req status", "requirement status",
                         "item status", "config status"}
        _NOTES_NAMES  = {"notes", "comments", "review notes", "ai notes", "review"}
        status_col_idx = next((i for i, h in enumerate(_h_lower)
                                if h in _STATUS_NAMES), None)
        notes_col_idx  = next((i for i, h in enumerate(_h_lower)
                                if h in _NOTES_NAMES), None)

        for r_idx, row in enumerate(rows):
            # ── Determine per-row status fill ────────────────────────────
            row_status = ""
            if status_col_idx is not None and status_col_idx < len(row):
                row_status = str(row[status_col_idx]).strip().lower()
            is_tbc = row_status in (
                "tbc", "to be confirmed", "pending",
                "not confirmed", "tbc - pending",
            )
            is_oos = row_status in (
                "out of scope", "oos", "excluded",
                "n/a - oos", "out-of-scope", "not in scope",
            )
            # AI-draft flag — is the Notes cell flagged for this row?
            is_ai_draft = False
            if notes_col_idx is not None and notes_col_idx < len(row):
                nv = str(row[notes_col_idx]).lower()
                is_ai_draft = "(ai draft" in nv or "(ai)" in nv
            # Baseline alt-row fill — used only when no status override applies.
            base_fill = ALT_FILL if r_idx % 2 == 0 else None

            for c_idx, val in enumerate(row):
                cell = ws.cell(r_idx + 2, c_idx + 1)
                try:
                    sv = str(val)
                    cell.value = (float(sv) if sv.replace(".", "", 1)
                                  .replace("-", "", 1).isdigit() else sv)
                except Exception:
                    cell.value = _safe(val)
                cell.border = BORDER
                # Fill priority: OOS > TBC > AI-draft-notes-cell > alt-row
                if is_oos:
                    cell.fill = OOS_FILL
                    cell.font  = OOS_FONT
                elif is_tbc:
                    cell.fill = TBC_FILL
                elif (is_ai_draft and notes_col_idx is not None
                      and c_idx == notes_col_idx):
                    cell.fill = AI_CELL_FILL
                elif base_fill:
                    cell.fill = base_fill

        # ── Item 04: DataValidation + ConditionalFormatting on Status ────
        # Adds a dropdown so consultants can change status interactively, and
        # CF rules so the row color updates live in Excel when they do.
        # Applied only when a Status column was detected; skipped otherwise.
        # Covers both delivery-checklist values (Not started / Done / Blocked)
        # AND requirement/config values (Confirmed / TBC / Out of Scope).
        if status_col_idx is not None and rows:
            try:
                from openpyxl.worksheet.datavalidation import DataValidation
                from openpyxl.formatting.rule import FormulaRule

                s_col_letter = get_column_letter(status_col_idx + 1)
                last_data_row = len(rows) + 1          # +1 for header
                dv_range  = f"{s_col_letter}2:{s_col_letter}{last_data_row}"
                row_range = f"A2:{get_column_letter(n_cols)}{last_data_row}"

                # Dropdown — superset of all status values across document types
                dv = DataValidation(
                    type="list",
                    formula1=('"Confirmed,TBC,Out of Scope,'
                              'Not started,In progress,Done,Blocked"'),
                    allow_blank=True,
                    showErrorMessage=True,
                    errorTitle="Invalid status",
                    error=("Choose from: Confirmed / TBC / Out of Scope / "
                           "Not started / In progress / Done / Blocked"),
                )
                ws.add_data_validation(dv)
                dv.sqref = dv_range

                # Row-level CF rules — last rule in the list has lowest priority
                # (Excel evaluates first match wins, top to bottom in manager).
                # Colors deliberately match the Python-applied fills so the sheet
                # looks the same at open time as when Status is later changed.
                _CF_RULES = [
                    ("Done",         "D4EDDA"),  # green
                    ("Blocked",      "F8D7DA"),  # red
                    ("In progress",  "D6E4F7"),  # light blue
                    ("Not started",  "F5F5F5"),  # light grey
                    ("TBC",          "FFF3CD"),  # amber  (matches TBC_FILL)
                    ("Out of Scope", "E0E0E0"),  # grey   (matches OOS_FILL)
                ]
                for status_val, hex_color in _CF_RULES:
                    ws.conditional_formatting.add(
                        row_range,
                        FormulaRule(
                            formula=[f'${s_col_letter}2="{status_val}"'],
                            fill=PatternFill("solid", fgColor=hex_color),
                        ),
                    )
            except Exception as _cf_err:
                # Non-fatal: DataValidation or CF is a nice-to-have.
                print(f"   [xlsx] DataValidation/CF skipped: {_cf_err}")

        for c_idx, h in enumerate(headers, 1):
            col_vals = [h] + [_safe(r[c_idx - 1]) if c_idx - 1 < len(r) else "" for r in rows]
            width = min(max((len(v) for v in col_vals), default=8) + 4, 50)
            ws.column_dimensions[get_column_letter(c_idx)].width = width

    # Chart on first sheet
    try:
        sheets  = plan.get("sheets", [])
        c_idx   = int(plan.get("chart_sheet", 0))
        if c_idx < len(wb.sheetnames) and c_idx < len(sheets):
            ws_c   = wb[wb.sheetnames[c_idx]]
            rows_c = sheets[c_idx].get("rows", [])
            n      = len(rows_c)
            if n >= 2:
                col   = int(plan.get("chart_data_col", 1)) + 1
                chart = BarChart()
                chart.title = _safe(plan.get("title", "Chart"))[:50]
                chart.style = 10
                # Rows shifted up by one when the title banner was removed: headers
                # are now row 1 and data starts at row 2.
                data_ref = Reference(ws_c, min_col=col, min_row=1, max_row=n + 1)
                cats_ref = Reference(ws_c, min_col=1, min_row=2, max_row=n + 1)
                chart.add_data(data_ref, titles_from_data=True)
                chart.set_categories(cats_ref)
                chart.width = 18
                chart.height = 12
                ws_c.add_chart(chart, "A" + str(n + 5))
    except Exception as e:
        print(f"   Chart skipped: {e}")

    wb.save(output_path)
    return output_path


# ═══════════════════════════════════════════════════════════════════════════
# DOCX
# ═══════════════════════════════════════════════════════════════════════════

def generate_docx(plan: Dict, output_path: str, grounding_path: str = "") -> str:
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    def set_cell_bg(cell, hex_color: str):
        tc   = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd  = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        tcPr.append(shd)

    # ── Try to use grounding DOCX as style template ───────────────────────
    doc = None
    if grounding_path and grounding_path.lower().endswith((".docx", ".doc")):
        try:
            doc = Document(grounding_path)
            # Clear all body content while keeping styles, themes and page layout.
            # The body must keep its <w:sectPr> (section/margin properties).
            body = doc.element.body
            ns   = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            for child in list(body):
                tag = child.tag.split("}")[-1]
                if tag != "sectPr":          # preserve page layout
                    body.remove(child)
            # DOCX spec requires at least one paragraph before sectPr
            empty_p = OxmlElement("w:p")
            sect_pr = body.find(f"{{{ns}}}sectPr")
            if sect_pr is not None:
                body.insert(list(body).index(sect_pr), empty_p)
            else:
                body.append(empty_p)
            print(f"   Using DOCX template: {os.path.basename(grounding_path)}")
        except Exception as e:
            print(f"   DOCX template load failed ({e}), using default style")
            doc = None

    if doc is None:
        doc = Document()
        for sec in doc.sections:
            sec.top_margin    = Inches(1)
            sec.bottom_margin = Inches(1)
            sec.left_margin   = Inches(1.2)
            sec.right_margin  = Inches(1.2)

    p  = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r  = p.add_run(_safe(plan.get("title", "Document"))[:120])
    r.bold = True
    r.font.size = Pt(24)
    r.font.color.rgb = RGBColor(0x1F, 0x49, 0x7D)
    p.paragraph_format.space_after = Pt(8)

    if plan.get("subtitle"):
        s  = doc.add_paragraph()
        s.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sr = s.add_run(_safe(plan["subtitle"])[:120])
        sr.font.size = Pt(13)
        sr.font.color.rgb = RGBColor(0x44, 0x72, 0xC4)
        s.paragraph_format.space_after = Pt(14)

    for section in plan.get("sections", []):
        h = doc.add_heading(_safe(section.get("heading", ""))[:100], level=int(section.get("level", 1)))
        h.paragraph_format.space_before = Pt(10)
        h.paragraph_format.space_after  = Pt(4)

        for para in section.get("paragraphs", []):
            p = doc.add_paragraph(_safe(para, 800))
            p.paragraph_format.space_after = Pt(6)

        for b in section.get("bullets", []):
            doc.add_paragraph(_safe(b, 300), style="List Bullet")

        tbl = section.get("table")
        if tbl:
            hdrs = [_safe(h) for h in tbl.get("headers", [])]
            rows = tbl.get("rows", [])
            if hdrs:
                t  = doc.add_table(rows=1 + len(rows), cols=len(hdrs))
                t.style = "Table Grid"
                hc = t.rows[0].cells
                for i, h in enumerate(hdrs):
                    hc[i].text = h
                    set_cell_bg(hc[i], "1F497D")
                    if hc[i].paragraphs[0].runs:
                        hc[i].paragraphs[0].runs[0].bold = True
                        hc[i].paragraphs[0].runs[0].font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                for ri, row in enumerate(rows):
                    bg = "DCE6F1" if ri % 2 == 0 else "FFFFFF"
                    for ci, val in enumerate(row):
                        if ci < len(t.rows[ri + 1].cells):
                            c = t.rows[ri + 1].cells[ci]
                            c.text = _safe(val, 120)
                            set_cell_bg(c, bg)
                doc.add_paragraph()

    doc.save(output_path)
    return output_path


# ═══════════════════════════════════════════════════════════════════════════
# PDF
# ═══════════════════════════════════════════════════════════════════════════

def generate_pdf(plan: Dict, output_path: str) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.enums import TA_CENTER

    doc   = SimpleDocTemplate(output_path, pagesize=A4,
                               leftMargin=inch, rightMargin=inch,
                               topMargin=inch, bottomMargin=inch)
    story = []
    styles = getSampleStyleSheet()
    HDR    = colors.HexColor("#1F497D")
    ALT    = colors.HexColor("#DCE6F1")

    ts = ParagraphStyle("T2", parent=styles["Title"],   fontSize=22, textColor=HDR, spaceAfter=6, alignment=TA_CENTER)
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=15, textColor=HDR, spaceAfter=4)
    bd = ParagraphStyle("B2", parent=styles["Normal"],  fontSize=10, spaceAfter=6, leading=14)

    story.append(Paragraph(_safe(plan.get("title", "Report"), 120), ts))
    if plan.get("subtitle"):
        story.append(Paragraph(_safe(plan["subtitle"], 120),
            ParagraphStyle("S", parent=styles["Normal"], fontSize=12,
                           textColor=colors.HexColor("#44729C"), alignment=TA_CENTER, spaceAfter=10)))
    story.append(HRFlowable(width="100%", thickness=2, color=HDR))
    story.append(Spacer(1, 0.15 * inch))

    for sec in plan.get("sections", []):
        if sec.get("heading"):
            story.append(Paragraph(_safe(sec["heading"], 100), h1))
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#B8CCE4")))
            story.append(Spacer(1, 0.06 * inch))
        for para in sec.get("paragraphs", []):
            story.append(Paragraph(_safe(para, 800), bd))
        tbl = sec.get("table")
        if tbl:
            hdrs = [_safe(h) for h in tbl.get("headers", [])]
            rows = tbl.get("rows", [])
            if hdrs:
                td   = [[_safe(h) for h in hdrs]] + [[_safe(v, 80) for v in row] for row in rows]
                cw   = (A4[0] - 2 * inch) / max(len(hdrs), 1)
                t    = Table(td, colWidths=[cw] * len(hdrs), repeatRows=1)
                t.setStyle(TableStyle([
                    ("BACKGROUND",   (0, 0), (-1, 0),  HDR),
                    ("TEXTCOLOR",    (0, 0), (-1, 0),  colors.white),
                    ("FONTNAME",     (0, 0), (-1, 0),  "Helvetica-Bold"),
                    ("FONTSIZE",     (0, 0), (-1, 0),  10),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ALT]),
                    ("GRID",         (0, 0), (-1, -1), 0.5, colors.HexColor("#B8CCE4")),
                    ("TOPPADDING",   (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
                ]))
                story.append(t)
                story.append(Spacer(1, 0.12 * inch))
        story.append(Spacer(1, 0.08 * inch))

    page_num = [0]

    def footer(canvas, doc):
        page_num[0] += 1
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        canvas.drawRightString(A4[0] - inch, 0.5 * inch, f"Page {page_num[0]}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output_path


# ═══════════════════════════════════════════════════════════════════════════
# PPTX
# ═══════════════════════════════════════════════════════════════════════════

def _pptx_get_accent_color(prs):
    """
    Extract the first non-black/non-white accent colour from the slide master's
    theme colour scheme.  Returns RGBColor or None.
    """
    try:
        from pptx.dml.color import RGBColor
        from pptx.oxml.ns import qn
        ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
        clrScheme = prs.slide_master._element.find(f".//{{{ns_a}}}clrScheme")
        if clrScheme is None:
            return None
        for clr_el in clrScheme:
            for sub in clr_el:
                tag = sub.tag.split("}")[-1]
                if tag == "srgbClr":
                    val = sub.get("val", "")
                    if len(val) == 6 and val.upper() not in ("000000", "FFFFFF", "FFFFFE"):
                        r, g, b = int(val[0:2], 16), int(val[2:4], 16), int(val[4:6], 16)
                        return RGBColor(r, g, b)
    except Exception:
        pass
    return None


# Layout names that carry a specific meaning. Perfectly clean, but a deck whose every
# content slide sits on the "Thank You" design reads as a mistake.
_PPTX_LOADED_LAYOUT = re.compile(
    r"thank|closing|back\s*cover|divider|agenda|contents|q\s*&\s*a|questions", re.I)


def _pptx_sample_style(path: str) -> Dict[str, Any]:
    """
    Learn the reference's look from its OWN SLIDES, not its theme.

    Reading the theme was the obvious approach and it was wrong: the measured
    Governance Matrix reference is built on Accenture purple 5C2D91 in Arial, yet its
    theme is stock Office — dk2 44546A, minor font Calibri. The brand lives in explicit
    run and fill formatting on each slide, so that is what has to be sampled. Reading
    the theme produced output sharing not one colour with the document it was modelled
    on, which is precisely the complaint.

    Returns {font, dark, accent, light}; any key may be absent.
    """
    from collections import Counter
    from pptx import Presentation
    from pptx.dml.color import RGBColor

    fonts: Counter = Counter()
    cols:  Counter = Counter()
    try:
        ref = Presentation(path)
        for slide in ref.slides:
            for sh in slide.shapes:
                frames = []
                if sh.has_text_frame:
                    frames.append(sh.text_frame)
                if sh.has_table:
                    for row in sh.table.rows:
                        frames.extend(c.text_frame for c in row.cells)
                for tf in frames:
                    for para in tf.paragraphs:
                        for run in para.runs:
                            if run.font.name:
                                fonts[run.font.name] += 1
                            try:
                                if run.font.color and run.font.color.rgb:
                                    cols[str(run.font.color.rgb).upper()] += 1
                            except Exception:
                                pass
                # Shape fills carry the header-band and tier colours, which are the
                # most recognisable part of a deck's identity — weighted accordingly.
                try:
                    if sh.fill.type == 1:                        # MSO_FILL.SOLID
                        rgb = sh.fill.fore_color.rgb
                        if rgb:
                            cols[str(rgb).upper()] += 3
                except Exception:
                    pass
    except Exception:
        return {}

    out: Dict[str, Any] = {}
    if fonts:
        out["font"] = fonts.most_common(1)[0][0]

    def _rgb(h):
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    def _lum(t):
        return 0.299 * t[0] + 0.587 * t[1] + 0.114 * t[2]

    def _chroma(t):
        return max(t) - min(t)

    # Rules chosen from the measured distribution of the real reference:
    #
    #   5C2D91  count 149  chroma 100   the brand purple
    #   2E7D32  count 105  chroma  79   RACI "C" green
    #   D32F2F  count  45  chroma 164   RACI "R" red
    #   D97B00  count  18  chroma 217   a tier pill
    #   F0EBF5  count 201  chroma  10   the row-banding tint
    #   424242  count 138  chroma   0   body grey
    #
    # Ranking by saturation alone elected D97B00 — the loudest colour in the deck, used
    # eighteen times. Ranking by frequency alone elects the body grey. The brand is the
    # most FREQUENT colour that is actually coloured, which picks 5C2D91 cleanly.
    coloured = [(h, _rgb(h)) for h in cols
                if h not in ("FFFFFF", "000000") and _chroma(_rgb(h)) >= 30]
    coloured.sort(key=lambda hv: -cols[hv[0]])
    for h, t in coloured:
        if _lum(t) < 150:
            out["dark"] = RGBColor.from_string(h)
            break

    # Accent deliberately mirrors the header rather than taking the runner-up. The
    # runner-up here is the RACI green, and colours that carry MEANING in the reference
    # must not be reused as decoration — a green subtitle would read as a status.
    if "dark" in out:
        out["accent"] = out["dark"]

    # Row banding wants the near-white TINT, which has almost no chroma and so is
    # invisible to the rule above. A slight chroma floor matters: the most frequent
    # light colour in the reference is FAFAFA (273 uses), a neutral off-white that
    # would band rows in a shade indistinguishable from the page. F0EBF5 (201 uses)
    # carries a trace of the brand purple and actually reads as banding.
    tints = [(h, _rgb(h)) for h in cols
             if h != "FFFFFF" and _lum(_rgb(h)) > 225 and _chroma(_rgb(h)) >= 5]
    tints.sort(key=lambda hv: -cols[hv[0]])
    if not tints:                          # a strictly greyscale reference
        tints = [(h, _rgb(h)) for h in cols if h != "FFFFFF" and _lum(_rgb(h)) > 225]
        tints.sort(key=lambda hv: -cols[hv[0]])
    if tints:
        out["light"] = RGBColor.from_string(tints[0][0])
    return out


def _pptx_theme_palette(prs) -> Dict[str, Any]:
    """
    Read dk2/lt2/accent1 out of the template's own theme.

    Without this the renderer painted every header bar `1F497D` and every accent
    `4472C4` — hardcoded Office blues. Measured against the Governance Matrix
    reference, which is built on Accenture purple `5C2D91`, the output shared not one
    colour with the document it was supposed to be modelled on.
    """
    from pptx.dml.color import RGBColor
    out: Dict[str, Any] = {}
    try:
        theme = prs.slide_masters[0].part.part_related_by(
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme")
        xml = theme.blob.decode("utf-8", "replace")
        scheme = re.search(r"<a:clrScheme.*?</a:clrScheme>", xml, re.S)
        if not scheme:
            return out
        block = scheme.group(0)
        for key, tag in (("dark", "dk2"), ("light", "lt2"), ("accent", "accent1")):
            m = re.search(rf'<a:{tag}>.*?val="([0-9A-Fa-f]{{6}})".*?</a:{tag}>', block, re.S)
            if m:
                out[key] = RGBColor.from_string(m.group(1).upper())
    except Exception:
        pass
    return out


def _pptx_theme_font(prs) -> Optional[str]:
    """The template's minor (body) typeface, so generated text is not left on Calibri."""
    try:
        theme = prs.slide_masters[0].part.part_related_by(
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme")
        xml = theme.blob.decode("utf-8", "replace")
        m = re.search(r"<a:minorFont>\s*<a:latin[^>]*typeface=\"([^\"]+)\"", xml, re.S)
        if m and m.group(1) and not m.group(1).startswith("+"):
            return m.group(1)
    except Exception:
        pass
    return None


def _pptx_pick_blank_layout(prs):
    """
    Choose the emptiest layout to paint on.

    This used to be `slide_layouts[min(6, len-1)]` — a positional guess. It happens to
    land on "Blank" in an 11-layout Office deck, which is why it looked fine, but a
    real corporate template has dozens: in the measured Testing Strategy deck index 6
    is "07. Three-Image Columns with Title", with five placeholders and eleven shapes.
    Every generated slide therefore carried three image frames it never filled, and
    every slide looked identical because they all used it.

    The renderer draws its own absolutely-positioned text boxes and header bars, so
    what it needs is a layout with as little furniture as possible. DECORATIVE shapes
    (logos, image frames, rules) are weighted an order of magnitude above placeholders
    because an unfilled placeholder is invisible when presenting, whereas a logo is not.
    """
    best, best_score = None, None
    for master in prs.slide_masters:
        for layout in master.slide_layouts:
            name = (layout.name or "").strip()
            placeholders = len(layout.placeholders)
            decorative = max(0, len(layout.shapes) - placeholders)
            score = decorative * 10 + placeholders
            if _PPTX_LOADED_LAYOUT.search(name):
                score += 25
            if name.lower() == "blank":
                score = -1                      # the template's own answer; take it
            if best_score is None or score < best_score:
                best, best_score = layout, score
    if best is None:                            # no masters at all — should not happen
        return prs.slide_layouts[min(6, len(prs.slide_layouts) - 1)]
    return best


def generate_pptx(plan: Dict, output_path: str, grounding_path: str = "") -> str:
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.chart.data import ChartData
    from pptx.enum.chart import XL_CHART_TYPE

    # ── Try to load grounding PPTX as design template ─────────────────────
    prs = None
    used_template = False
    if grounding_path and grounding_path.lower().endswith(".pptx"):
        try:
            prs = Presentation(grounding_path)
            # Remove all existing content slides while preserving slide master + layouts
            sldIdLst = prs.slides._sldIdLst
            for sldId in list(sldIdLst):
                rId = sldId.get(qn("r:id"))
                prs.part.drop_rel(rId)
                sldIdLst.remove(sldId)
            used_template = True
            print(f"   Using PPTX template: {os.path.basename(grounding_path)}")
        except Exception as e:
            print(f"   PPTX template load failed ({e}), using default style")
            prs = None

    if prs is None:
        prs = Presentation()
        prs.slide_width  = Inches(13.33)
        prs.slide_height = Inches(7.5)

    # ── EVERY COORDINATE BELOW IS SCALED TO THE ACTUAL CANVAS ─────────────
    #
    # The layout numbers were written for a 13.33 x 7.5in slide, which is what this
    # function creates when there is no template. A real corporate deck is often
    # 10 x 5.625in, and the reference's size is KEPT when we clone it — so a title box
    # declared 11.3in wide ran 2.3in past the right edge and a header bar declared
    # 13.33in wide overhung by a third. Measured on the Governance Matrix reference:
    # 41 of 41 shapes fell outside the canvas. That is the "text is outside the slide"
    # report, and it is arithmetic, not styling.
    #
    # Font sizes scale with the width too: 40pt on a 13.33in slide is proportionally
    # 30pt on a 10in one, and leaving type at full size on a smaller canvas is the
    # other half of the overflow.
    _DESIGN_W, _DESIGN_H = 13.33, 7.5
    _sx = (prs.slide_width  / Inches(_DESIGN_W)) if prs.slide_width  else 1.0
    _sy = (prs.slide_height / Inches(_DESIGN_H)) if prs.slide_height else 1.0

    def X(v):                      # horizontal position / width
        return int(Inches(v) * _sx)

    def Y(v):                      # vertical position / height
        return int(Inches(v) * _sy)

    def S(pt):                     # font size, scaled by the narrower axis
        return Pt(max(8, round(pt * min(_sx, _sy))))

    if used_template and abs(_sx - 1.0) > 0.01:
        print(f"   Canvas {round(Emu(prs.slide_width).inches, 2)}x"
              f"{round(Emu(prs.slide_height).inches, 2)}in — scaling layout by "
              f"{_sx:.2f}x{_sy:.2f}")

    # ── Colour palette and typeface, learned from the reference ────────────
    # Sampled from the reference's slides first (where the brand actually lives), then
    # its theme, then the accent helper, then Office-blue defaults.
    _style = _pptx_sample_style(grounding_path) if used_template else {}
    _pal   = _pptx_theme_palette(prs) if used_template else {}
    _accent = _pptx_get_accent_color(prs) if used_template else None
    HDR = _style.get("dark")   or _pal.get("dark")   or _accent or RGBColor(0x1F, 0x49, 0x7D)
    ACC = _style.get("accent") or _pal.get("accent") or _accent or RGBColor(0x44, 0x72, 0xC4)
    WHT = RGBColor(0xFF, 0xFF, 0xFF)
    BG  = _style.get("light")  or _pal.get("light")  or RGBColor(0xF2, 0xF7, 0xFF)
    FONT = _style.get("font") or (_pptx_theme_font(prs) if used_template else None)
    if used_template:
        print(f"   Reference style: font={FONT or 'default'} "
              f"header=#{HDR} accent=#{ACC} band=#{BG}")

    def set_bg(slide, rgb):
        # When using a template, skip overriding background so the master design shows
        if used_template:
            return
        f = slide.background.fill
        f.solid()
        f.fore_color.rgb = rgb

    def add_text(tf, text, size=18, bold=False, color=None, align=PP_ALIGN.LEFT):
        tf.text = ""
        tf.word_wrap = True
        para = tf.paragraphs[0]
        para.alignment = align
        run = para.add_run()
        run.text = _safe(text, 140)
        run.font.size = S(size)
        run.font.bold = bold
        if FONT:
            run.font.name = FONT
        if color:
            run.font.color.rgb = color

    blank = _pptx_pick_blank_layout(prs)

    for sl in plan.get("slides", []):
        t = sl.get("type", "bullets")

        if t == "title":
            slide = prs.slides.add_slide(blank)
            set_bg(slide, BG)
            b = slide.shapes.add_textbox(X(1), Y(2.2), X(11.3), Y(1.5))
            add_text(b.text_frame, sl.get("title", ""), 40, True, HDR, PP_ALIGN.CENTER)
            if sl.get("subtitle"):
                b2 = slide.shapes.add_textbox(X(1), Y(3.9), X(11.3), Y(0.8))
                add_text(b2.text_frame, sl["subtitle"], 20, False, ACC, PP_ALIGN.CENTER)

        elif t == "bullets":
            slide = prs.slides.add_slide(blank)
            set_bg(slide, WHT)
            bar = slide.shapes.add_shape(1, X(0), Y(0), X(13.33), Y(1.2))
            bar.fill.solid()
            bar.fill.fore_color.rgb = HDR
            bar.line.fill.background()
            tb = slide.shapes.add_textbox(X(0.3), Y(0.15), X(12.5), Y(0.9))
            add_text(tb.text_frame, sl.get("title", ""), 22, True, WHT)
            bx = slide.shapes.add_textbox(X(0.5), Y(1.4), X(12), Y(5.5))
            tf = bx.text_frame
            tf.word_wrap = True
            tf.text = ""
            for i, b in enumerate(sl.get("bullets", [])):
                p = tf.add_paragraph() if i > 0 else tf.paragraphs[0]
                p.text = f"  \u2022  {_safe(b, 120)}"
                p.space_after = S(8)
                if p.runs:
                    p.runs[0].font.size = S(16)
                    if FONT:
                        p.runs[0].font.name = FONT

        elif t == "table":
            slide = prs.slides.add_slide(blank)
            set_bg(slide, WHT)
            bar = slide.shapes.add_shape(1, X(0), Y(0), X(13.33), Y(1.2))
            bar.fill.solid()
            bar.fill.fore_color.rgb = HDR
            bar.line.fill.background()
            tb = slide.shapes.add_textbox(X(0.3), Y(0.15), X(12.5), Y(0.9))
            add_text(tb.text_frame, sl.get("title", ""), 22, True, WHT)
            hdrs = sl.get("headers", [])
            rows = sl.get("rows", [])
            if hdrs and rows:
                nc  = len(hdrs)
                nr  = len(rows)
                tbl = slide.shapes.add_table(
                    nr + 1, nc,
                    X(0.5), Y(1.4),
                    X(12.3), Y(min(nr * 0.5 + 0.5, 5.5))
                ).table
                # Cell type also has to scale: 18pt default in a 0.35in row on a 10in
                # canvas overflows the row and pushes the table past the slide.
                _cell_pt = S(11)
                for c, h in enumerate(hdrs):
                    cell = tbl.cell(0, c)
                    cell.text = _safe(h, 50)
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = HDR
                    for _p in cell.text_frame.paragraphs:
                        for _r in _p.runs:
                            _r.font.size = _cell_pt
                            _r.font.color.rgb = WHT
                            _r.font.bold = True
                            if FONT:
                                _r.font.name = FONT
                for r, row in enumerate(rows):
                    bg = BG if r % 2 == 0 else WHT
                    for c, val in enumerate(row):
                        if c < nc:
                            cell = tbl.cell(r + 1, c)
                            cell.text = _safe(val, 80)
                            cell.fill.solid()
                            cell.fill.fore_color.rgb = bg
                            for _p in cell.text_frame.paragraphs:
                                for _r in _p.runs:
                                    _r.font.size = _cell_pt
                                    if FONT:
                                        _r.font.name = FONT

        elif t == "chart":
            slide = prs.slides.add_slide(blank)
            set_bg(slide, WHT)
            bar = slide.shapes.add_shape(1, X(0), Y(0), X(13.33), Y(1.2))
            bar.fill.solid()
            bar.fill.fore_color.rgb = HDR
            bar.line.fill.background()
            tb = slide.shapes.add_textbox(X(0.3), Y(0.15), X(12.5), Y(0.9))
            add_text(tb.text_frame, sl.get("title", ""), 22, True, WHT)
            cats = sl.get("categories", ["A", "B", "C"])
            vals = sl.get("values", [1, 2, 3])
            try:
                cd = ChartData()
                cd.categories = cats
                cd.add_series(_safe(sl.get("series_name", "Series"), 40), vals)
                chart = slide.shapes.add_chart(
                    XL_CHART_TYPE.COLUMN_CLUSTERED,
                    X(0.5), Y(1.4), X(12.3), Y(5.5), cd
                ).chart
                chart.has_title = True
                chart.chart_title.text_frame.text = _safe(sl.get("title", ""), 80)
                chart.series[0].format.fill.solid()
                chart.series[0].format.fill.fore_color.rgb = ACC
            except Exception as e:
                print(f"   Chart error: {e}")

    prs.save(output_path)
    return output_path


# ═══════════════════════════════════════════════════════════════════════════
# XML  — FIX D: reference-driven
# ═══════════════════════════════════════════════════════════════════════════

def generate_xml(plan: Dict, output_path: str, ref_schema: str = "") -> str:
    """
    FIX D: Parses ref_schema at runtime to discover real root tag,
    entity tag names, and field names. Falls back to generic structure
    when no ref_schema is provided.
    """

    def prettify(elem) -> str:
        rough    = ET.tostring(elem, encoding="unicode")
        reparsed = minidom.parseString(rough)
        return reparsed.toprettyxml(indent="  ", encoding=None)

    # ── Parse reference schema ─────────────────────────────────────────────
    root_tag   = "Document"
    ns_uri     = None
    entity_map: Dict[str, list] = {}   # entity_name -> [field_names]
    entity_tag = "Entity"

    if ref_schema and ref_schema.strip().startswith("<"):
        try:
            schema_text = ref_schema
            if schema_text.lstrip().startswith("<?xml"):
                schema_text = schema_text[schema_text.index("?>") + 2:].strip()

            ref_root = ET.fromstring(schema_text)

            raw_tag = ref_root.tag
            if "}" in raw_tag:
                ns_uri   = raw_tag.split("}")[0][1:]
                root_tag = raw_tag.split("}")[1]
            else:
                root_tag = raw_tag

            for child in ref_root:
                ctag_full = child.tag
                ctag      = ctag_full.split("}")[1] if "}" in ctag_full else ctag_full
                entity_tag = ctag

                ent_name = (
                    child.get("name") or child.get("id") or
                    child.get("code") or ctag
                )

                field_names = []
                for subchild in child.iter():
                    stag  = subchild.tag.split("}")[1] if "}" in subchild.tag else subchild.tag
                    fname = (
                        subchild.get("name") or subchild.get("fieldName") or
                        subchild.get("id")
                    )
                    if fname and stag.lower() in ("field", "column", "attribute", "property", "fielddef"):
                        field_names.append(fname)

                entity_map[ent_name] = field_names

            print(f"   Parsed ref schema: root={root_tag}, entities={list(entity_map.keys())}")

        except Exception as parse_err:
            print(f"   ⚠ Could not parse ref_schema ({parse_err}), using generic structure")

    # ── Build output XML ───────────────────────────────────────────────────
    root_el = ET.Element(root_tag)
    if ns_uri:
        root_el.set("xmlns", ns_uri)
    root_el.set("version", "1.0")

    meta = ET.SubElement(root_el, "Metadata")
    ET.SubElement(meta, "Title").text       = _safe(plan.get("title", "Document"))
    ET.SubElement(meta, "GeneratedBy").text = "Claude Agent - ProjectZen"
    ET.SubElement(meta, "Model").text       = "claude-opus-4-7"

    for sheet in plan.get("sheets", []):
        sname   = _safe(sheet.get("name", "Entity")).replace(" ", "")
        headers = sheet.get("headers", [])
        rows    = sheet.get("rows", [])

        # Match sheet name to a reference entity
        ref_fields: list = []
        matched_ent = sname
        for ent_name, fields in entity_map.items():
            if (ent_name.lower() == sname.lower() or
                    ent_name.lower() in sname.lower() or
                    sname.lower() in ent_name.lower()):
                ref_fields  = fields
                matched_ent = ent_name
                break

        ent_el = ET.SubElement(root_el, entity_tag)
        ent_el.set("name",  matched_ent)
        ent_el.set("label", _safe(sheet.get("name", matched_ent)))

        use_fields = ref_fields if ref_fields else headers
        if use_fields:
            fields_el = ET.SubElement(ent_el, "Fields")
            for fname in use_fields:
                f_el = ET.SubElement(fields_el, "Field")
                f_el.set("name",      _safe_tag(fname))
                f_el.set("label",     _safe(fname))
                f_el.set("type",      "String")
                f_el.set("maxLength", "255")

        if rows:
            records_el = ET.SubElement(ent_el, "Records")
            for row in rows:
                record = ET.SubElement(records_el, "Record")
                for i, val in enumerate(row):
                    if i < len(headers):
                        tag_name = _safe_tag(headers[i])
                        ET.SubElement(record, tag_name).text = _safe(val)

    for sec in plan.get("sections", []):
        cc = ET.SubElement(root_el, "Section")
        cc.set("heading", _safe(sec.get("heading", "General")))
        for para in sec.get("paragraphs", []):
            ET.SubElement(cc, "Paragraph").text = _safe(para, 500)

    # ── Write output ───────────────────────────────────────────────────────
    xml_str   = prettify(root_el)
    xml_lines = xml_str.split("\n")
    if xml_lines and xml_lines[0].startswith("<?xml"):
        xml_lines = xml_lines[1:]

    declaration = '<?xml version="1.0" encoding="UTF-8"?>'
    output      = declaration + "\n" + "\n".join(xml_lines)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(output)

    return output_path


# ═══════════════════════════════════════════════════════════════════════════
# CSF XML — SAP SuccessFactors Country-Specific Fields
# Dedicated generator: reads CSF sheets from Excel directly.
# Produces exact <country-specific-fields> structure matching SF DTD.
# ═══════════════════════════════════════════════════════════════════════════

# Sheet name → HRIS element id + column layout
_CSF_SHEET_META = {
    "CSF Personal Info (Global Info)": {
        "hris_element":  "globalInfo",
        "header_row":    3,      # 0-indexed row of the column headers
        "country_col":   2,
        "field_id_col":  3,
        "label_col":     4,
        "type_col":      5,
        "maxlen_col":    6,
        "visibility_col":7,
        "required_col":  9,
        "picklist_col":  10,
    },
    "CSF Addresses": {
        "hris_element":  "homeAddress",
        "header_row":    3,
        "country_col":   2,
        "field_id_col":  3,
        "label_col":     4,
        "type_col":      5,
        "maxlen_col":    6,
        "visibility_col":7,
        "required_col":  9,
        "picklist_col":  10,
    },
    "CSF Dependents": {
        # Has an extra 'Classification' column at index 3 — shifts field cols right by 1
        "hris_element":  "dependents",
        "header_row":    3,
        "country_col":   2,
        "field_id_col":  4,
        "label_col":     5,
        "type_col":      6,
        "maxlen_col":    7,
        "visibility_col":None,   # no visibility column in this sheet
        "required_col":  10,
        "picklist_col":  11,
    },
    "CSF Job Info": {
        "hris_element":  "jobInfo",
        "header_row":    3,
        "country_col":   2,
        "field_id_col":  3,
        "label_col":     4,
        "type_col":      5,
        "maxlen_col":    6,
        "visibility_col":7,
        "required_col":  9,
        "picklist_col":  10,
    },
}

# SAP SF DOCTYPE declaration
_CSF_DOCTYPE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE country-specific-fields PUBLIC\n'
    ' "-//SuccessFactors, Inc.//DTD Country Specific Field Configuration//EN"\n'
    ' "http://svn/viewvc/svn/V4/trunk/src/com/sf/dtd/country-specific-fields.dtd?view=co">'
)


def _csf_get(row: tuple, col) -> Optional[str]:
    """Safely get a cell value from a row tuple."""
    if col is None or col >= len(row):
        return None
    v = row[col]
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() != "none" else None


def _csf_country_code(val: str) -> str:
    """
    Extract 3-letter ISO-3166-1 alpha-3 code from a country cell value.

    Handles two formats found in the workbook:
      1. 'Angola (AGO)'     → 'AGO'   (preferred — has code in parentheses)
      2. 'Angola'           → 'AGO'   (fallback — name-to-code lookup)
      3. 'South Korea'      → 'KOR'   (common-name lookup)
    """
    import re

    # ── Primary: code in parentheses ──────────────────────────────────────
    m = re.search(r"\(([A-Z]{2,3})\)", str(val))
    if m:
        return m.group(1)

    # ── Fallback: country name lookup ─────────────────────────────────────
    # Maps common names (and aliases) that appear in SF workbooks
    # to their ISO-3166-1 alpha-3 codes.
    _NAME_TO_CODE = {
        "afghanistan": "AFG", "albania": "ALB", "algeria": "DZA",
        "angola": "AGO", "argentina": "ARG", "armenia": "ARM",
        "australia": "AUS", "austria": "AUT", "azerbaijan": "AZE",
        "bahrain": "BHR", "bangladesh": "BGD", "belarus": "BLR",
        "belgium": "BEL", "bolivia": "BOL", "bosnia": "BIH",
        "botswana": "BWA", "brazil": "BRA", "bulgaria": "BGR",
        "cambodia": "KHM", "cameroon": "CMR", "canada": "CAN",
        "chile": "CHL", "china": "CHN", "colombia": "COL",
        "costa rica": "CRI", "croatia": "HRV", "czech republic": "CZE",
        "czechia": "CZE", "denmark": "DNK", "dominican republic": "DOM",
        "ecuador": "ECU", "egypt": "EGY", "el salvador": "SLV",
        "estonia": "EST", "ethiopia": "ETH", "finland": "FIN",
        "france": "FRA", "georgia": "GEO", "germany": "DEU",
        "ghana": "GHA", "greece": "GRC", "guatemala": "GTM",
        "honduras": "HND", "hong kong": "HKG", "hungary": "HUN",
        "india": "IND", "indonesia": "IDN", "iran": "IRN",
        "iraq": "IRQ", "ireland": "IRL", "israel": "ISR",
        "italy": "ITA", "ivory coast": "CIV", "jamaica": "JAM",
        "japan": "JPN", "jordan": "JOR", "kazakhstan": "KAZ",
        "kenya": "KEN", "kosovo": "XKX", "kuwait": "KWT",
        "latvia": "LVA", "lebanon": "LBN", "lithuania": "LTU",
        "luxembourg": "LUX", "malaysia": "MYS", "mauritius": "MUS",
        "mexico": "MEX", "moldova": "MDA", "morocco": "MAR",
        "mozambique": "MOZ", "myanmar": "MMR", "namibia": "NAM",
        "netherlands": "NLD", "new zealand": "NZL", "nigeria": "NGA",
        "norway": "NOR", "oman": "OMN", "pakistan": "PAK",
        "panama": "PAN", "paraguay": "PRY", "peru": "PER",
        "philippines": "PHL", "poland": "POL", "portugal": "PRT",
        "puerto rico": "PRI", "qatar": "QAT", "romania": "ROU",
        "russia": "RUS", "russian federation": "RUS",
        "saudi arabia": "SAU", "senegal": "SEN", "serbia": "SRB",
        "singapore": "SGP", "slovakia": "SVK", "slovenia": "SVN",
        "south africa": "ZAF", "south korea": "KOR",
        "korea, republic of": "KOR", "republic of korea": "KOR",
        "spain": "ESP", "sri lanka": "LKA", "sweden": "SWE",
        "switzerland": "CHE", "taiwan": "TWN", "tanzania": "TZA",
        "thailand": "THA", "tunisia": "TUN", "turkey": "TUR",
        "turkiye": "TUR", "ukraine": "UKR",
        "united arab emirates": "ARE", "uae": "ARE",
        "united kingdom": "GBR", "uk": "GBR",
        "united states": "USA", "usa": "USA", "us": "USA",
        "uruguay": "URY", "venezuela": "VEN", "vietnam": "VNM",
        "viet nam": "VNM", "zambia": "ZMB", "zimbabwe": "ZWE",
    }

    key = str(val).strip().lower()
    if key in _NAME_TO_CODE:
        return _NAME_TO_CODE[key]

    # ── Last resort: strip non-alpha and take first 3 uppercase chars ──────
    # This is intentionally kept but only reached for truly unrecognised names.
    letters = re.sub(r"[^A-Za-z]", "", str(val))
    return letters.upper()[:3]


def _csf_map_visibility(val: Optional[str]) -> str:
    """Map workbook visibility to SF XML values: both | none | view."""
    if not val:
        return "both"
    v = val.strip().lower()
    if v in ("no", "none", "hide", "hidden"):
        return "none"
    if v in ("view", "view only", "read only", "read"):
        return "view"
    return "both"   # Yes / Edit / both / anything else → both


def _csf_map_required(val: Optional[str]) -> Optional[str]:
    """Return 'true' if required, None otherwise (omit attribute)."""
    if not val:
        return None
    return "true" if val.strip().lower() in ("yes", "true", "mandatory", "y") else None


def generate_csf_xml(excel_path: str, output_path: str) -> str:
    """
    Read all CSF-prefixed sheets from the SuccessFactors configuration
    workbook and produce a country-specific-fields XML file matching the
    SF DTD structure:

        <country-specific-fields>
          <country id="AGO">
            <hris-element id="homeAddress">
              <hris-field id="address1" max-length="256"
                          visibility="both" required="true">
                <label>Care Of</label>
              </hris-field>
              ...
            </hris-element>
            <hris-element id="globalInfo">...</hris-element>
            ...
          </country>
          ...
        </country-specific-fields>
    """
    from openpyxl import load_workbook
    from collections import defaultdict, OrderedDict

    wb = load_workbook(excel_path, read_only=True, data_only=True)

    # country_code → hris_element_id → [field dicts]
    # Use OrderedDict to keep country insertion order (alphabetical by code)
    country_data: Dict[str, Dict[str, list]] = defaultdict(
        lambda: defaultdict(list)
    )

    csf_sheets_found = []

    for sheet_name, meta in _CSF_SHEET_META.items():
        if sheet_name not in wb.sheetnames:
            print(f"   ⚠ Sheet not found: {sheet_name} — skipping")
            continue

        csf_sheets_found.append(sheet_name)
        ws       = wb[sheet_name]
        hris_el  = meta["hris_element"]
        hdr_row  = meta["header_row"]

        for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
            # Skip header rows
            if row_idx <= hdr_row:
                continue

            country_raw = _csf_get(row, meta["country_col"])
            field_id    = _csf_get(row, meta["field_id_col"])

            # Skip blank or header-like rows
            if not country_raw or not field_id:
                continue
            if country_raw.lower().strip(" ") in ("country", "country  "):
                continue

            code = _csf_country_code(country_raw)
            if len(code) < 2:
                continue

            label    = _csf_get(row, meta["label_col"]) or field_id
            ftype    = _csf_get(row, meta["type_col"])   or "STRING"
            maxlen   = _csf_get(row, meta["maxlen_col"]) or "256"
            vis_raw  = _csf_get(row, meta["visibility_col"])
            req_raw  = _csf_get(row, meta["required_col"])
            picklist = _csf_get(row, meta["picklist_col"])

            # Normalise max-length to integer string
            try:
                maxlen = str(int(float(str(maxlen))))
            except (ValueError, TypeError):
                maxlen = "256"

            country_data[code][hris_el].append({
                "id":         field_id,
                "label":      label,
                "type":       ftype,
                "max_length": maxlen,
                "visibility": _csf_map_visibility(vis_raw),
                "required":   _csf_map_required(req_raw),
                "picklist":   picklist,
            })

    wb.close()

    if not csf_sheets_found:
        raise ValueError(
            "No CSF sheets found in the workbook. "
            "Expected sheets named: " + ", ".join(_CSF_SHEET_META.keys())
        )

    print(f"   ✅ CSF extraction: {len(country_data)} countries "
          f"from {len(csf_sheets_found)} sheets")

    # ── Build XML tree ────────────────────────────────────────────────────
    root = ET.Element("country-specific-fields")

    for code in sorted(country_data.keys()):
        country_el = ET.SubElement(root, "country")
        country_el.set("id", code)

        for hris_el_id, fields in country_data[code].items():
            if not fields:
                continue
            hris_el = ET.SubElement(country_el, "hris-element")
            hris_el.set("id", hris_el_id)

            for f in fields:
                hf = ET.SubElement(hris_el, "hris-field")
                hf.set("id",         f["id"])
                hf.set("max-length", f["max_length"])
                hf.set("visibility", f["visibility"])
                if f["required"]:
                    hf.set("required", f["required"])
                if f["picklist"]:
                    hf.set("picklist", f["picklist"])

                label_el = ET.SubElement(hf, "label")
                label_el.text = f["label"]

    # ── Serialise with pretty-print ───────────────────────────────────────
    rough    = ET.tostring(root, encoding="unicode")
    reparsed = minidom.parseString(rough)
    pretty   = reparsed.toprettyxml(indent="  ", encoding=None)

    # Strip minidom's own <?xml?> declaration — we use _CSF_DOCTYPE instead
    xml_lines = pretty.split("\n")
    if xml_lines and xml_lines[0].startswith("<?xml"):
        xml_lines = xml_lines[1:]

    output = _CSF_DOCTYPE + "\n" + "\n".join(xml_lines)

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(output)

    print(f"   ✅ CSF XML saved → {output_path}")
    return output_path


# ═══════════════════════════════════════════════════════════════════════════
# Dispatcher
# ═══════════════════════════════════════════════════════════════════════════

def generate(
    output_format: str,
    plan: Dict,
    output_path: str,
    ref_schema: str = "",
    excel_path: str = "",        # used by csf_xml route
    grounding_path: str = "",    # path to admin-uploaded reference file for template cloning
) -> str:
    """
    Single entry point. Dispatches to the correct generator.
    Returns output_path on success, raises on failure.

    output_format values:
        xlsx | docx | pdf | pptx | xml | csf_xml
    """
    fmt = output_format.lower().strip()
    if fmt == "xlsx":
        return generate_xlsx(plan, output_path, grounding_path)
    if fmt == "docx":
        return generate_docx(plan, output_path, grounding_path)
    if fmt == "pdf":
        return generate_pdf(plan, output_path)
    if fmt == "pptx":
        return generate_pptx(plan, output_path, grounding_path)
    if fmt == "xml":
        return generate_xml(plan, output_path, ref_schema)
    if fmt == "csf_xml":
        if not excel_path:
            raise ValueError("excel_path is required for csf_xml format")
        return generate_csf_xml(excel_path, output_path)
    raise ValueError(f"Unsupported output format: {output_format}")
