"""
data_model_agent.py — SAP SuccessFactors Employee Central data-model export.

WHAT THIS DOES
--------------
Takes ONE configuration workbook (.xlsx) and produces the SuccessFactors data-model
XML it contains data for — any of:

    Succession Data Model            CSF Succession Data Model
    Corporate Data Model             CSF Corporate Data Model

The workbook decides. The user does not pick a model; the agent reads the sheets and
determines which models are actually populated, because a consultant handed a
"configuration workbook" generally does not know (or care) which of SAP's four model
files their tabs map onto.

TWO ENGINES, DELIBERATELY
-------------------------
XML that goes into a SuccessFactors instance either imports or is rejected — there is
no "mostly right". So generation prefers determinism and falls back to the agent:

  1. DETERMINISTIC. If the workbook carries the exact CSF sheet layout that
     templates.generate_csf_xml() already parses, that code runs. It is proven, it
     costs nothing, and it cannot hallucinate an element name.
  2. AGENT. Everything else — the other three models, and CSF workbooks whose sheets
     do not match the known layout — goes to Claude with the grounded reference in
     hand.

STRUCTURE COMES FROM THE GROUNDING, NEVER FROM MEMORY
-----------------------------------------------------
Each model's root element and DOCTYPE are read out of the admin-supplied reference
XML at runtime. They are NOT hardcoded here.

That is a correctness decision, not a style one. SAP's DTD public identifiers and
root element names differ per model and per release, and a plausible-looking
DOCTYPE that is subtly wrong produces a file that fails on import with an unhelpful
error. The admin has the real files; this module copies their structure verbatim
rather than reproducing something half-remembered. A model with no reference
configured is REPORTED, not guessed at.

VALIDATION BEFORE DELIVERY
--------------------------
Generated XML is parsed, its root element compared against the reference, and every
element name checked against the reference's vocabulary. Invented tags are the
characteristic LLM failure here and they are caught before the user downloads.

PACKAGING
---------
One model detected -> a single .xml. Two or more -> a .zip of one .xml per model,
because Succession and Corporate models are separate SAP artefacts with different
DOCTYPEs and cannot validly be merged into one document.
"""

import io
import os
import re
import json
import zipfile
import contextlib
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

# Tracing is optional and must never be able to fail an export — same contract the
# rest of the codebase holds telemetry to.
try:
    import runlog
except Exception:                                        # pragma: no cover
    import types as _ty

    def _noop(*_a, **_k): return None

    @contextlib.contextmanager
    def _noop_scope(*_a, **_k): yield

    runlog = _ty.SimpleNamespace(emit=_noop, stage_scope=_noop_scope)  # type: ignore

# Admin template key per model. These are rows in adhoc_template_config, so all four
# references are managed through the existing Admin -> Template Config screen with no
# schema change.
TEMPLATE_KEYS: Dict[str, str] = {
    "succession":     "Data Model / Succession",
    "csf_succession": "Data Model / CSF Succession",
    "corporate":      "Data Model / Corporate",
    "csf_corporate":  "Data Model / CSF Corporate",
}

MODEL_LABELS: Dict[str, str] = {
    "succession":     "Succession Data Model",
    "csf_succession": "CSF Succession Data Model",
    "corporate":      "Corporate Data Model",
    "csf_corporate":  "CSF Corporate Data Model",
}

MODEL = "claude-opus-4-8"          # correctness-critical output; not a Sonnet job
MAX_REF_CHARS = 60000              # cap on the reference SKELETON (see below)

# Two different budgets, because detection and generation need different things.
#
# Detection only needs the SHAPE of the workbook — sheet names and a few header rows —
# so it gets a compact digest. Generation needs every field definition, and the
# measured real workbook (33 sheets, 1.4 MB) extracts to 337,785 characters in ~2s.
# The previous shared 60,000 cap silently dropped two whole sheets before the model
# ever saw them, which is the quiet scope loss this codebase has been bitten by
# repeatedly. 400,000 clears the measured file with margin and still bounds the
# pathological case.
MAX_WB_DIGEST_CHARS = 60000
MAX_WB_CHARS        = 400000

# A DOCTYPE may carry an internal subset in [...]; match that too so stripping is
# complete. Parsing is done on the stripped body: external DTDs are never fetched
# and entities are never expanded, which also removes any XXE exposure.
_DOCTYPE_RE = re.compile(r"<!DOCTYPE\s+[^>\[]*(?:\[[^\]]*\])?\s*>", re.S | re.I)
_DECL_RE    = re.compile(r"<\?xml[^>]*\?>", re.I)


def _split_document(body: str, root_tag: str) -> Tuple[str, str]:
    """
    Separate the XML document from anything the model said around it.

    Returns (xml, commentary).

    The prompt says "return ONLY the XML"; on the first real run against a live
    workbook the model returned a complete, correct 1,081-line document and then
    appended "Notes on scope decisions (not part of the XML)" with five bullet points.
    The document parsed as far as its closing tag and then failed with "junk after
    document element" — a perfect file thrown away over trailing prose.

    Robustness cannot depend on the model obeying an instruction, so the document is
    cut out by its own root element. The commentary is kept rather than discarded: it
    explains what was deliberately omitted and why, which is exactly what the person
    checking the export needs to read.
    """
    m = re.search(rf"<{re.escape(root_tag)}(?=[\s>])", body)
    close = f"</{root_tag}>"
    end = body.rfind(close)
    if not m or end < m.start():
        return body.strip(), ""
    cut = end + len(close)
    return body[m.start():cut].strip(), (body[:m.start()] + "\n" + body[cut:]).strip()


def _strip_fences(s: str) -> str:
    """
    Remove markdown code fences from a model response.

    Deliberately NOT cascade_agent._clean_json, which is the obvious thing to reach
    for and is wrong here: it slices everything between the first '{' and the last
    '}'. A data model containing a brace anywhere — a field label, a picklist value,
    a DTD internal subset — would be silently shredded into fragments that still
    look like XML at a glance. Fences are all that needs removing.
    """
    s = s.strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Reference (grounding) handling
# ─────────────────────────────────────────────────────────────────────────────

def _short(v: str, n: int = 44) -> str:
    v = re.sub(r"\s+", " ", (v or "").strip())
    return v if len(v) <= n else v[:n - 1] + "…"


def _skeleton_lines(el, depth: int, lines: List[str],
                    per_tag: int, max_depth: int, budget: int) -> None:
    """Recursive structural outline: every distinct shape, no repetition."""
    if depth > max_depth or len(lines) >= budget:
        return
    attrs = " ".join(f'{k.rsplit("}", 1)[-1]}="{_short(str(v), 24)}"'
                     for k, v in list(el.attrib.items())[:6])
    lead = "  " * depth
    open_tag = f"{lead}<{el.tag}{(' ' + attrs) if attrs else ''}>"
    kids = list(el)
    if not kids:
        lines.append(f"{open_tag}{_short(el.text or '', 40)}</{el.tag}>")
        return
    lines.append(open_tag)
    seen: Dict[str, int] = {}
    for k in kids:
        seen[k.tag] = seen.get(k.tag, 0) + 1
        if seen[k.tag] <= per_tag:
            _skeleton_lines(k, depth + 1, lines, per_tag, max_depth, budget)
        elif seen[k.tag] == per_tag + 1:
            lines.append(f"{'  ' * (depth + 1)}<!-- … further <{k.tag}> elements, "
                         f"same shape … -->")
    lines.append(f"{lead}</{el.tag}>")


def _structural_skeleton(root, budget_lines: int = 400) -> str:
    """
    A deduplicated outline of the reference, in place of its raw text.

    THIS IS THE POINT OF THE MODULE, so it is worth being explicit about.

    Real tenant exports are enormous and almost entirely repetition: the CSF
    succession model measured here is 10.3 MB of which 169,960 elements are <label>
    translations, across just 13 distinct tags. Feeding the first 60,000 characters
    of that to the model delivered 0.6% of the file, chopped mid-element, so the
    "reference structure" it saw was a malformed fragment of one arbitrary country.

    The skeleton carries every distinct SHAPE — element nesting, attribute names, a
    representative value — and drops the repetition, which is what a structure
    contract actually is. It costs a few thousand characters instead of ten million.
    """
    lines: List[str] = []
    _skeleton_lines(root, 0, lines, per_tag=2, max_depth=7, budget=budget_lines)
    return "\n".join(lines)


def _parse_reference(path: str) -> Dict[str, Any]:
    """Read a reference data-model XML and extract the contract it defines."""
    import xml.etree.ElementTree as ET

    text = Path(path).read_text(encoding="utf-8", errors="replace")
    m = _DOCTYPE_RE.search(text)
    doctype = m.group(0).strip() if m else ""
    body = _DECL_RE.sub("", _DOCTYPE_RE.sub("", text)).strip()

    root = ET.fromstring(body)
    tags = sorted({el.tag for el in root.iter()})
    attrs = sorted({a.rsplit("}", 1)[-1] for el in root.iter() for a in el.attrib})
    # hris-element ids are SAP-defined objects (personalInfo, jobInfo, homeAddress,
    # location, legalEntity …), not free-form names the workbook may invent. They are
    # also the ONLY thing distinguishing the two country-specific-fields models from
    # each other: CSF-for-succession and CSF-for-corporate share a root element and a
    # DOCTYPE, so without this a corporate model full of personalInfo would validate.
    hris_ids = sorted({el.get("id") for el in root.iter("hris-element")
                       if el.get("id")})
    return {
        "path":     path,
        "doctype":  doctype,
        "root_tag": root.tag,
        "tags":     tags,
        "attrs":    attrs,
        "hris_ids": hris_ids,
        "skeleton": _structural_skeleton(root),
        "elements": sum(1 for _ in root.iter()),
        "text":     text,
    }


async def resolve_groundings() -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """
    Load every configured reference. Returns (by_model, problems).

    A model whose reference is missing or unparseable is simply absent from the
    result — the caller reports it rather than generating something unvalidated.
    """
    from db import get_adhoc_config
    from paths import REFS_DIR

    out: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []

    for key, template in TEMPLATE_KEYS.items():
        try:
            cfg = await get_adhoc_config(template)
        except Exception as exc:
            problems.append(f"{MODEL_LABELS[key]}: config lookup failed ({exc})")
            continue
        if not cfg or not cfg.get("ref_id"):
            problems.append(
                f"{MODEL_LABELS[key]}: no reference structure configured "
                f"(Admin → Template Config → \"{template}\")")
            continue
        hits = list(Path(REFS_DIR).glob(f"{cfg['ref_id']}*"))
        if not hits:
            problems.append(f"{MODEL_LABELS[key]}: reference file missing from disk")
            continue
        try:
            ref = _parse_reference(str(hits[0]))
            ref["system_prompt"] = cfg.get("system_prompt") or ""
            out[key] = ref
        except Exception as exc:
            problems.append(
                f"{MODEL_LABELS[key]}: reference is not parseable XML ({exc})")
    return out, problems


# ─────────────────────────────────────────────────────────────────────────────
# Workbook inspection
# ─────────────────────────────────────────────────────────────────────────────

def inspect_workbook(path: str, sample_rows: int = 6) -> Dict[str, Any]:
    """
    Summarise a workbook cheaply: sheet names, header-ish rows, row counts.

    Deliberately not a full extraction — detection only needs the SHAPE, and feeding
    a 27 MB workbook's every cell into the detection prompt wastes the budget the
    generation step actually needs.
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = []
    try:
        for ws in wb.worksheets:
            rows = []
            for i, row in enumerate(ws.iter_rows(max_row=sample_rows, values_only=True)):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    rows.append(cells[:14])
                if i + 1 >= sample_rows:
                    break
            sheets.append({
                "name":     ws.title,
                "rows":     ws.max_row or 0,
                "cols":     ws.max_column or 0,
                "sample":   rows,
            })
    finally:
        wb.close()
    return {"sheets": sheets, "sheet_names": [s["name"] for s in sheets]}


def csf_sheets_present(inspection: Dict[str, Any]) -> bool:
    """True when the workbook carries the exact sheet layout the proven parser reads."""
    try:
        from templates import _CSF_SHEET_META
    except Exception:
        return False
    names = set(inspection.get("sheet_names") or [])
    return bool(set(_CSF_SHEET_META.keys()) & names)


def _workbook_digest(inspection: Dict[str, Any]) -> str:
    lines = []
    for s in inspection["sheets"]:
        lines.append(f"\n### Sheet: {s['name']}  ({s['rows']} rows x {s['cols']} cols)")
        for r in s["sample"]:
            lines.append("  | " + " | ".join(r))
    return "\n".join(lines)[:MAX_WB_DIGEST_CHARS]


# ─────────────────────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────────────────────

_DETECT_SYSTEM = (
    "You are an SAP SuccessFactors Employee Central consultant with deep, practical "
    "experience of the EC data models: Succession Data Model, Country-Specific (CSF) "
    "Succession Data Model, Corporate Data Model, and CSF Corporate Data Model. You "
    "know which configuration artefacts belong in each file: standard and custom "
    "HRIS elements and field overrides in the Succession model; foundation objects "
    "(legal entity, business unit, department, division, job classification, pay "
    "component, cost centre) in the Corporate model; and per-country field overrides "
    "in the CSF variants of each."
)


async def detect_models(inspection: Dict[str, Any],
                        available: List[str],
                        on_chunk=None) -> Dict[str, Any]:
    """Ask the agent which data models this workbook actually carries data for."""
    from llm_client import complete, usage_scope
    from cascade_agent import _clean_json

    opts = "\n".join(f"  {k} = {MODEL_LABELS[k]}" for k in available)
    prompt = (
        "Below is a summary of every sheet in a SuccessFactors configuration "
        "workbook: the sheet name, its size, and its first few rows.\n\n"
        "Decide which SAP data-model file(s) this workbook contains data for. "
        "Judge by what the sheets actually hold, not by their names alone — "
        "consultants name tabs inconsistently.\n\n"
        f"Models you may choose from:\n{opts}\n\n"
        "Rules:\n"
        "- Include a model ONLY if the workbook holds data that genuinely belongs in "
        "it. An empty or placeholder sheet is not data.\n"
        "- A workbook may legitimately map to several models, or to one.\n"
        "- If a sheet holds per-country overrides, that indicates a CSF model.\n\n"
        f"WORKBOOK:\n{_workbook_digest(inspection)}\n\n"
        'Return ONLY JSON: {"models":[{"key":"...","confidence":"high|medium|low",'
        '"evidence":"which sheets and why","sheets":["..."]}],'
        '"unmapped_sheets":["sheets holding data that fits none of the models"]}'
    )
    with usage_scope("datamodel/detect"):
        raw = await complete(prompt, system=_DETECT_SYSTEM, model=MODEL, on_chunk=on_chunk)
    data = json.loads(_clean_json(raw))
    picked = [m for m in (data.get("models") or []) if m.get("key") in available]
    return {"models": picked, "unmapped_sheets": data.get("unmapped_sheets") or []}


# ─────────────────────────────────────────────────────────────────────────────
# Generation
# ─────────────────────────────────────────────────────────────────────────────

async def generate_model_xml(key: str, ref: Dict[str, Any],
                             inspection: Dict[str, Any], workbook_text: str,
                             user_prompt: str, on_chunk=None) -> Tuple[str, str]:
    """Generate one model's XML, grounded on the admin reference structure."""
    from llm_client import complete, usage_scope

    extra = f"\n\nADMIN INSTRUCTIONS:\n{ref['system_prompt']}" if ref.get("system_prompt") else ""
    prompt = (
        f"Produce the {MODEL_LABELS[key]} XML for the SuccessFactors configuration "
        f"workbook below.\n\n"
        "REFERENCE STRUCTURE — this is a real data model from another tenant. Copy its "
        "STRUCTURE exactly: the root element, element nesting, attribute names and "
        "ordering conventions.\n"
        "- Use ONLY element and attribute names that appear in the reference. Never "
        "invent one; an element SuccessFactors does not recognise fails the import.\n"
        "- Do NOT copy the reference's DATA. Its fields, labels, countries and "
        "picklists belong to a different customer.\n"
        "- Every value must come from the workbook.\n"
        "- Omit sections the workbook has no data for rather than inventing filler.\n\n"
        "TRIGGER RULES — apply these rules precisely before including any trigger-rule "
        "element:\n"
        "1. NEVER include trigger rules whose rule= attribute starts with a "
        "tenant-specific prefix: ACN_, myConcerto_, migratedRule_, or any prefix that "
        "names a specific company's internal tooling. These belong to the reference "
        "tenant only and will fail import on any other system.\n"
        "2. For SAP_PERSON_ID_<COUNTRY> rules (appear on workPermitInfo and inside the "
        "document-number hris-field): ONLY include rules for countries that are "
        "explicitly in scope in this workbook. Determine in-scope countries from the "
        "CSF sheets. Do NOT include SAP_PERSON_ID rules for any country the workbook "
        "has no data for.\n"
        "3. All other generic trigger rules from the reference (Personal_Info_Change, "
        "National_ID_Change, natIDonInit, VOL_INVOL_TERMINATION, WorkflowDerivation, "
        "HireRule, EventDerivation, Probation_Alert, Global_Assignment, "
        "HRMGR_Check_onSave, Work_Permit_Alert, etc.) may be retained as they appear "
        "in the reference.\n\n"
        f"REFERENCE STRUCTURE — {MODEL_LABELS[key]}, outlined from a real "
        f"{ref['elements']:,}-element tenant export. Repeated siblings are collapsed; "
        f"the shapes shown are the complete vocabulary.\n"
        f"Root element: <{ref['root_tag']}>\n"
        f"Permitted element names: {', '.join(ref['tags'])}\n"
        f"Permitted attribute names: {', '.join(ref['attrs'])}\n\n"
        f"{ref['skeleton'][:MAX_REF_CHARS]}\n\n"
        f"WORKBOOK SHEET MAP:\n{_workbook_digest(inspection)}\n\n"
        f"WORKBOOK CONTENT:\n{workbook_text[:MAX_WB_CHARS]}\n"
        f"{extra}\n\n"
        f"USER REQUEST:\n{user_prompt[:2000]}\n\n"
        "Return ONLY the XML document body, starting at the root element "
        f"<{ref['root_tag']}>. No markdown fences, no commentary, and do NOT include "
        "the <?xml?> declaration or the DOCTYPE — those are added verbatim from the "
        "reference."
    )
    with usage_scope(f"datamodel/{key}"):
        raw = await complete(prompt, system=_DETECT_SYSTEM, model=MODEL, on_chunk=on_chunk)

    body = _strip_fences(raw)
    # The declaration and DOCTYPE are re-added from the reference, so drop whatever
    # the model emitted rather than trusting it to have reproduced them exactly.
    body = _DECL_RE.sub("", _DOCTYPE_RE.sub("", body)).strip()
    body, commentary = _split_document(body, ref["root_tag"])

    decl = '<?xml version="1.0" encoding="UTF-8"?>'
    parts = [decl] + ([ref["doctype"]] if ref["doctype"] else []) + [body]
    return "\n".join(parts) + "\n", commentary


def validate_against_reference(xml_text: str, ref: Dict[str, Any],
                               other_model_ids: Optional[List[str]] = None
                               ) -> Tuple[bool, List[str]]:
    """
    Parse the generated XML and check it against the reference's contract.

    Catches the failure that matters: an element name the model invented, which looks
    fine to a reader and is rejected by SuccessFactors.
    """
    import xml.etree.ElementTree as ET

    problems: List[str] = []
    body = _DECL_RE.sub("", _DOCTYPE_RE.sub("", xml_text)).strip()
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        return False, [f"not well-formed XML: {exc}"]

    if root.tag != ref["root_tag"]:
        problems.append(f"root element is <{root.tag}>, reference uses <{ref['root_tag']}>")

    known = set(ref["tags"])
    unknown = sorted({el.tag for el in root.iter()} - known)
    if unknown:
        problems.append(
            f"{len(unknown)} element name(s) absent from the reference: "
            + ", ".join(f"<{t}>" for t in unknown[:8])
            + (" …" if len(unknown) > 8 else ""))

    if len(list(root.iter())) <= 1:
        problems.append("document has no content below the root element")

    # Object-level ids: what tells the two country-specific-fields models apart, since
    # they share a root element AND a DOCTYPE.
    #
    # Only ids that belong to ANOTHER model are flagged. An id missing from this
    # reference but present in no other one usually means the reference tenant simply
    # never configured that object — the real workbook here has a CSF Dependents sheet
    # while the reference CSF model carries only globalInfo, homeAddress and jobInfo.
    # Flagging that would cry wolf on a correct export; flagging jobInfo inside a
    # Corporate model would not.
    ref_ids = set(ref.get("hris_ids") or [])
    other_ids = set(other_model_ids or ()) - ref_ids
    if ref_ids and other_ids:
        got = {el.get("id") for el in root.iter("hris-element") if el.get("id")}
        misplaced = sorted((got - ref_ids) & other_ids)
        if misplaced:
            problems.append(
                f"{len(misplaced)} object(s) belong to a different SAP data model: "
                + ", ".join(misplaced[:8]) + (" …" if len(misplaced) > 8 else "")
                + " — this content looks misfiled")

    return (not problems), problems


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def run(workbook_path: str, user_prompt: str, out_dir: str,
              base_name: str = "DataModel", on_chunk=None) -> Dict[str, Any]:
    """
    Full export. Returns a result dict describing what was produced and why.

    Never raises for content reasons — a caller needs the diagnostics to show the
    user. It raises only if the workbook itself cannot be opened.
    """
    from extractor import extract_text

    result: Dict[str, Any] = {
        "files": [], "detected": [], "problems": [], "notes": [], "method": {},
    }

    inspection = inspect_workbook(workbook_path)
    groundings, ground_problems = await resolve_groundings()
    result["problems"].extend(ground_problems)

    if not groundings:
        result["notes"].append(
            "No data-model reference structures are configured, so nothing can be "
            "generated safely. An administrator must upload the four reference XML "
            "files under Admin → Template Config.")
        return result

    detected = await detect_models(inspection, list(groundings.keys()), on_chunk)
    result["detected"] = detected["models"]
    if detected["unmapped_sheets"]:
        result["notes"].append(
            "These sheets hold data that fits none of the four data models and were "
            "not exported: " + ", ".join(detected["unmapped_sheets"][:12]))

    if not detected["models"]:
        result["notes"].append(
            "The workbook does not appear to contain data for any of the four "
            "SuccessFactors data models.")
        return result

    workbook_text = extract_text(workbook_path, max_chars=MAX_WB_CHARS)
    if len(workbook_text) >= MAX_WB_CHARS:
        # Say so rather than let it pass. A field that never reached the model is
        # indistinguishable, in the output, from a field the model chose to omit.
        result["notes"].append(
            f"This workbook is very large and only its first {MAX_WB_CHARS:,} "
            "characters were read. Fields beyond that point were not exported — "
            "split the workbook or remove sheets that are not needed for the data "
            "model.")
    deterministic_ok = csf_sheets_present(inspection)

    for entry in detected["models"]:
        key = entry["key"]
        ref = groundings[key]
        label = MODEL_LABELS[key]
        out_name = f"{base_name}_{key}.xml"
        out_path = os.path.join(out_dir, out_name)

        # Deterministic first — proven code beats a model for the layout it knows.
        #
        # The stage NAME carries the engine ("corporate/exact" vs "corporate/agent").
        # The run-log renderer shows stage names and durations but not arbitrary event
        # fields, so encoding it here makes the choice visible in the Logs tab with no
        # renderer change — and a deterministic run, which makes no LLM calls at all,
        # still produces a timed stage instead of an empty trace that reads as broken.
        if key == "csf_succession" and deterministic_ok:
            try:
                with runlog.stage_scope(f"{key}/exact"):
                    from templates import generate_csf_xml
                    generate_csf_xml(workbook_path, out_path)
                result["method"][key] = "deterministic"
                result["files"].append({"key": key, "label": label,
                                        "name": out_name, "path": out_path,
                                        "method": "deterministic", "valid": True,
                                        "problems": []})
                continue
            except Exception as exc:
                # Not fatal: fall through to the agent, but say so. Silently
                # switching engines would hide a real parsing regression.
                result["notes"].append(
                    f"{label}: the exact-match converter could not read this "
                    f"workbook ({str(exc)[:120]}); the agent generated it instead.")

        others = [i for k2, r2 in groundings.items() if k2 != key
                  for i in (r2.get("hris_ids") or [])]

        # Retry ONLY a document that will not parse. A vocabulary warning is a
        # judgement the user should see, not a dice roll worth re-rolling; a
        # malformed document is worth nothing at all, so it earns one more attempt.
        xml_text, ok, problems, commentary = "", False, [], ""
        for attempt in (1, 2):
            try:
                stage = f"{key}/agent" + ("" if attempt == 1 else "/retry")
                with runlog.stage_scope(stage):
                    xml_text, commentary = await generate_model_xml(
                        key, ref, inspection, workbook_text, user_prompt, on_chunk)
            except Exception as exc:
                result["problems"].append(
                    f"{label}: generation failed ({type(exc).__name__}: {exc})")
                xml_text = ""
                break

            ok, problems = validate_against_reference(
                xml_text, ref, other_model_ids=others)
            if ok or not any("well-formed" in p for p in problems):
                break
            if attempt == 1:
                print(f"   [datamodel] {label}: not well-formed; regenerating once")
                result["notes"].append(
                    f"{label}: the first attempt did not produce parseable XML and "
                    f"was regenerated.")

        if not xml_text:
            continue

        if commentary:
            # The model's own account of what it left out and why. On the first real
            # run this was five bullets explaining that Payment Info and Transaction
            # History are MDF objects rather than data-model content — worth reading.
            result["notes"].append(f"{label} — agent's notes: {commentary[:600]}")
        if not ok:
            # A structure mismatch belongs in the trace's FAILURES section, not only
            # in the on-screen preview: this is the one defect that reaches SAP and
            # fails there, and it is the whole reason validation exists.
            runlog.emit("error", where=f"datamodel/{key}/validation",
                        error={"type": "Data model structure validation",
                               "message": "; ".join(problems)[:400]})
        Path(out_path).write_text(xml_text, encoding="utf-8")
        result["method"][key] = "agent"
        result["files"].append({"key": key, "label": label, "name": out_name,
                                "path": out_path, "method": "agent",
                                "valid": ok, "problems": problems})
        if not ok:
            result["problems"].append(f"{label}: " + "; ".join(problems))

    return result


def package(files: List[Dict[str, Any]], out_dir: str, base_name: str) -> Tuple[str, str]:
    """
    One model -> the .xml itself. Several -> a .zip.

    Succession and Corporate models are separate SAP artefacts with different
    DOCTYPEs; concatenating them would produce a file that is valid as neither.
    """
    if len(files) == 1:
        src = files[0]["path"]
        final = os.path.join(out_dir, f"{base_name}.xml")
        if os.path.abspath(src) != os.path.abspath(final):
            os.replace(src, final)
        return final, "xml"

    zip_path = os.path.join(out_dir, f"{base_name}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f["path"], arcname=f"{f['key']}_data_model.xml")
            try:
                os.unlink(f["path"])
            except OSError:
                pass
    return zip_path, "zip"
