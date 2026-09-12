"""
workbook_taxonomy.py — which configuration workbooks exist, per SF module.

WHY THIS IS A FILE AND NOT MARKUP
---------------------------------
The user page and the admin grounding page both need this list, and they must
never disagree: an admin who grounds a workbook under a name the user page does
not offer has grounded something nobody can reach. One module here, read by both,
makes that impossible. Adding a module is an edit to this file — no HTML change,
no endpoint change.

WHAT DECIDES THE COUNTRY STEP
-----------------------------
NOT this file. `country_filtered` below is only a DEFAULT for the admin
checkbox. The real answer comes from classify_sheets() reading the workbook that
was actually grounded, because it varies per FILE, not per module. Measured on
the ten reference workbooks supplied:

    EC  / Employee Data          7 country-scoped sheets   -> filterable
    EC  / Foundation Data       10 country-scoped sheets   -> filterable
    EC  / Security Matrix        0                         -> plain download
    EC  / Position Management    0                         -> plain download
    EC  / Transactions           0                         -> plain download
    PMGM/ all four workbooks     0                         -> plain download
    RCM / RCM Configuration      3 (offer letters, purging, email triggers)

Three of EC's five workbooks are NOT country-partitioned, which is why the flag
can never be inferred from the module. RCM is the reason the admin can override
the detection: an offer-letter template really is country-scoped, an email
trigger probably is not, and only a human knows whether filtering that workbook
is wanted.
"""

import re
from typing import Any, Dict, List, Optional

# ── The taxonomy ─────────────────────────────────────────────────────────────
#
# id            stable key; forms the grounding row id `workbook:<mod>:<id>`
#               and must not change once a workbook has been grounded
# label         what both pages display
# aliases       lowercase fragments matched against an uploaded FILE NAME, so
#               the admin gets a suggested workbook instead of hunting a list.
#               Purely a convenience — never authoritative.
# country_filtered
#               default state of the admin's "filter by country" checkbox.
#               Detection overrides it on upload; the admin overrides detection.

MODULES: List[Dict[str, Any]] = [
    {
        "id": "EC", "label": "Employee Central (EC)",
        "workbooks": [
            {"id": "employee-data", "label": "Employee Data Configuration Workbook",
             "aliases": ["employee data", "employee_data"], "country_filtered": True},
            {"id": "foundation-data", "label": "Foundation Data Configuration Workbook",
             "aliases": ["foundation"], "country_filtered": True},
            # NO DATA MODELS HERE. Corporate / Succession / CSF data models are
            # XML reference structures, not configuration workbooks, and the
            # admin page already grounds them under "Data Model Agent (reference
            # structures)" — whose four names are routing keys in
            # data_model_agent.TEMPLATE_KEYS. Listing them here as well would
            # give one artefact two grounding homes and two sources of truth.
            {"id": "position-management", "label": "Position Management Workbook",
             "aliases": ["position management"], "country_filtered": False},
            {"id": "transactions", "label": "Transactions Workbook (Events & Workflows)",
             "aliases": ["transaction"], "country_filtered": False},
            {"id": "security-matrix", "label": "EC Security Matrix / RBP",
             "aliases": ["security matrix", "security_matrix"], "country_filtered": False},
            {"id": "business-rules", "label": "Business Rules Workbook",
             "aliases": ["business rule"], "country_filtered": False},
        ],
    },
    {
        "id": "ECP", "label": "EC Payroll",
        "workbooks": [
            {"id": "payroll-config", "label": "Payroll Configuration Workbook",
             "aliases": ["payroll config"], "country_filtered": True},
            {"id": "wage-types", "label": "Wage Type Catalogue",
             "aliases": ["wage type"], "country_filtered": True},
            {"id": "pcr-schema", "label": "PCRs & Payroll Schemas",
             "aliases": ["pcr", "schema"], "country_filtered": True},
            {"id": "statutory", "label": "Statutory & Legal Change Configuration",
             "aliases": ["statutory", "legal change"], "country_filtered": True},
            {"id": "parallel-payroll", "label": "Parallel Payroll Comparison Template",
             "aliases": ["parallel"], "country_filtered": True},
            {"id": "payroll-rbp", "label": "Payroll Role-Based Permissions",
             "aliases": ["payroll rbp"], "country_filtered": False},
        ],
    },
    {
        "id": "TA", "label": "Time Off & Time Tracking (T&A)",
        "workbooks": [
            {"id": "time-off", "label": "Time Off Configuration Workbook",
             "aliases": ["time off", "time-off", "absence"], "country_filtered": True},
            {"id": "time-tracking", "label": "Time Tracking / Timesheet Workbook",
             "aliases": ["time tracking", "timesheet"], "country_filtered": True},
            {"id": "holiday-calendar", "label": "Holiday Calendars",
             "aliases": ["holiday", "calendar"], "country_filtered": True},
            {"id": "time-profile", "label": "Time Profiles & Accrual Rules",
             "aliases": ["time profile", "accrual"], "country_filtered": True},
        ],
    },
    {
        "id": "RCM", "label": "Recruiting (RCM)",
        "workbooks": [
            {"id": "rcm-config", "label": "Recruiting Configuration Workbook",
             "aliases": ["rcm", "recruiting"], "country_filtered": False},
            {"id": "requisition-template", "label": "Requisition Templates",
             "aliases": ["requisition"], "country_filtered": False},
            {"id": "application-template", "label": "Application & Candidate Templates",
             "aliases": ["application", "candidate"], "country_filtered": False},
            {"id": "offer-approval", "label": "Offer Approval & Offer Letters",
             "aliases": ["offer"], "country_filtered": True},
            {"id": "career-site", "label": "Career Site Builder Configuration",
             "aliases": ["career site", "csb"], "country_filtered": False},
            {"id": "rcm-rbp", "label": "Recruiting Role-Based Permissions",
             "aliases": ["rcm rbp"], "country_filtered": False},
        ],
    },
    {
        "id": "ONB", "label": "Onboarding 2.0 (ONB)",
        "workbooks": [
            {"id": "onb-config", "label": "Onboarding Configuration Workbook",
             "aliases": ["onboarding", "onb"], "country_filtered": False},
            {"id": "onb-process", "label": "Onboarding Process & Task Configuration",
             "aliases": ["onboarding process", "task"], "country_filtered": False},
            {"id": "compliance-forms", "label": "Compliance & Country Forms",
             "aliases": ["compliance", "form"], "country_filtered": True},
            {"id": "offboarding", "label": "Offboarding Configuration",
             "aliases": ["offboard"], "country_filtered": False},
        ],
    },
    {
        "id": "PMGM", "label": "Performance & Goals (PMGM)",
        "workbooks": [
            {"id": "pmgm-config", "label": "PMGM Configuration Workbook",
             "aliases": ["pmgm"], "country_filtered": False},
            {"id": "goal-plan", "label": "Goal Plan Template",
             "aliases": ["goal"], "country_filtered": False},
            {"id": "form-template", "label": "Performance Form Template & Route Map",
             "aliases": ["form template", "route map"], "country_filtered": False},
            {"id": "multirater-360", "label": "360 Multirater Workbook",
             "aliases": ["360", "multirater"], "country_filtered": False},
            {"id": "pip", "label": "Performance Improvement Plan (PIP) Workbook",
             "aliases": ["pip"], "country_filtered": False},
            {"id": "calibration", "label": "Calibration Configuration",
             "aliases": ["calibration"], "country_filtered": False},
            {"id": "pmgm-rbp", "label": "PMGM Role-Based Permissions",
             "aliases": ["role_based", "role based", "rbp"], "country_filtered": False},
        ],
    },
    {
        "id": "COMP", "label": "Compensation & Variable Pay",
        "workbooks": [
            {"id": "comp-plan", "label": "Compensation Plan Template",
             "aliases": ["compensation"], "country_filtered": True},
            {"id": "variable-pay", "label": "Variable Pay / Bonus Plan",
             "aliases": ["variable pay", "bonus"], "country_filtered": True},
            {"id": "comp-statement", "label": "Compensation Statement Template",
             "aliases": ["statement"], "country_filtered": False},
            {"id": "comp-rbp", "label": "Compensation Role-Based Permissions",
             "aliases": ["comp rbp"], "country_filtered": False},
        ],
    },
    {
        "id": "SPCDP", "label": "Succession & Career Development (SP/CDP)",
        "workbooks": [
            {"id": "succession-config", "label": "Succession Configuration Workbook",
             "aliases": ["succession"], "country_filtered": False},
            {"id": "talent-pool", "label": "Talent Pools & Nomination",
             "aliases": ["talent pool", "nomination"], "country_filtered": False},
            {"id": "career-path", "label": "Career Path & Career Worksheet",
             "aliases": ["career path", "career worksheet"], "country_filtered": False},
            {"id": "development-plan", "label": "Development Plan Template",
             "aliases": ["development"], "country_filtered": False},
            {"id": "mentoring", "label": "Mentoring Programme Configuration",
             "aliases": ["mentoring"], "country_filtered": False},
        ],
    },
    {
        "id": "LMS", "label": "Learning (LMS)",
        "workbooks": [
            {"id": "lms-config", "label": "LMS Configuration Workbook",
             "aliases": ["lms", "learning"], "country_filtered": False},
            {"id": "item-curriculum", "label": "Items, Curricula & Programmes",
             "aliases": ["curricul", "item"], "country_filtered": False},
            {"id": "learning-assignment", "label": "Assignment Profiles & Rules",
             "aliases": ["assignment"], "country_filtered": False},
            {"id": "lms-catalog", "label": "Catalogue & Domain Structure",
             "aliases": ["catalog", "domain"], "country_filtered": False},
        ],
    },
    {
        "id": "PLT", "label": "Platform & Cross-Module",
        "workbooks": [
            {"id": "rbp-matrix", "label": "Role-Based Permissions Matrix",
             "aliases": ["role_based", "role based", "rbp", "permission"],
             "country_filtered": False},
            {"id": "integration-catalog", "label": "Integration Catalogue",
             "aliases": ["integration", "interface"], "country_filtered": False},
            {"id": "data-migration", "label": "Data Migration Templates",
             "aliases": ["migration"], "country_filtered": False},
            {"id": "reporting", "label": "Reporting & Analytics Configuration",
             "aliases": ["report", "analytic"], "country_filtered": False},
            {"id": "notification", "label": "Email Notification Templates",
             "aliases": ["notification", "email"], "country_filtered": False},
        ],
    },
]

GROUNDING_PREFIX = "workbook"


# ── Lookups ──────────────────────────────────────────────────────────────────

def all_modules() -> List[Dict[str, str]]:
    """[{id, label, workbook_count}] for the first dropdown."""
    return [{"id": m["id"], "label": m["label"], "workbook_count": len(m["workbooks"])}
            for m in MODULES]


def get_module(module_id: str) -> Optional[Dict[str, Any]]:
    key = str(module_id or "").strip().upper()
    return next((m for m in MODULES if m["id"].upper() == key), None)


def workbooks_for(module_id: str) -> List[Dict[str, Any]]:
    """[{id, label, country_filtered}] for the second dropdown."""
    m = get_module(module_id)
    if not m:
        return []
    return [{"id": w["id"], "label": w["label"],
             "country_filtered": bool(w.get("country_filtered"))}
            for w in m["workbooks"]]


def get_workbook(module_id: str, workbook_id: str) -> Optional[Dict[str, Any]]:
    m = get_module(module_id)
    if not m:
        return None
    key = str(workbook_id or "").strip().lower()
    return next((w for w in m["workbooks"] if w["id"].lower() == key), None)


def grounding_id(module_id: str, workbook_id: str, index: int = 1) -> str:
    """
    The `grounding_docs.node_id` a workbook file is stored under.

    'workbook:EC:employee-data' for the first file, ':2', ':3' ... for extras.
    The prefix is what keeps these rows invisible to every existing consumer:
    cascade looks grounding up by exact graph node id, and /admin/grounding
    enumerates knowledge-graph nodes — neither will ever ask for one of these.
    """
    base = f"{GROUNDING_PREFIX}:{str(module_id).strip().upper()}:{str(workbook_id).strip().lower()}"
    return base if index <= 1 else f"{base}:{int(index)}"


def parse_grounding_id(node_id: str) -> Optional[Dict[str, Any]]:
    """Reverse of grounding_id(). None when this is not a workbook row."""
    s = str(node_id or "")
    if not s.startswith(GROUNDING_PREFIX + ":"):
        return None
    parts = s.split(":")
    if len(parts) < 3:
        return None
    idx = 1
    if len(parts) >= 4 and parts[3].isdigit():
        idx = int(parts[3])
    return {"module": parts[1], "workbook": parts[2], "index": idx}


def is_workbook_row(node_id: str) -> bool:
    return parse_grounding_id(node_id) is not None


def suggest_workbook(file_name: str) -> Optional[Dict[str, str]]:
    """
    Best guess at (module, workbook) from an uploaded file name, so the admin
    starts from a suggestion rather than a blank pair of dropdowns.

    Longest alias wins, so 'employee data' beats a bare 'data'. Returns None
    rather than a weak guess — a wrong pre-selection is worse than none.
    """
    name = str(file_name or "").lower()
    if not name:
        return None

    # Data models are not configuration workbooks. They are grounded under
    # "Data Model Agent (reference structures)", whose four names are routing
    # keys in data_model_agent.TEMPLATE_KEYS. Without this guard the SPCDP
    # alias 'succession' claims 'Succession data-model.xml' and quietly offers
    # to file a data model as a workbook.
    if re.search(r"\.xml$|data[\s_-]?model|datamodel", name):
        return None

    best = None
    for m in MODULES:
        for w in m["workbooks"]:
            for alias in w.get("aliases", []):
                a = str(alias).lower()
                if a and a in name and (best is None or len(a) > best[0]):
                    best = (len(a), m["id"], w["id"], w["label"], m["label"])
    if not best:
        return None
    return {"module": best[1], "workbook": best[2],
            "workbook_label": best[3], "module_label": best[4]}
