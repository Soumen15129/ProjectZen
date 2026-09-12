"""
structure.py — schema/volume validation as a PURE PYTHON FUNCTION. Zero LLM calls.

v1 spent a Sonnet call per document per rework round asking a model to check whether
required JSON keys were present and arrays were non-empty. ProjectZen's artifacts are
FIXED-SCHEMA JSON (_FORMAT_SCHEMAS), so this check is:
    deterministic  — no sampling variance, no "the reviewer missed it this time"
    instant        — microseconds instead of a round-trip
    free           — 0 tokens
    always-on      — it runs at every depth including "minimal" and the delta path,
                     where no LLM reviewer runs at all

Oracle needed a model for this because its artifacts are free-form markdown against a
prose template. ProjectZen does not. This is the clearest case in the whole comparison
where copying Oracle's shape costs money and accuracy for nothing.

Findings are emitted in the exact same dict shape as the LLM reviewers
({severity, section, issue, fix, reviewer}), so the merge/rework path is identical.
"""

import json
from typing import Any, Dict, List

# Minimum volumes, derived from cascade_agent._FORMAT_DETAIL. Kept in sync manually —
# if _FORMAT_DETAIL changes, change these.
# Raised in step with _FORMAT_DETAIL in cascade_agent.py — the prompt must ask for at
# least what this enforces. Floors sit slightly BELOW the prompt's ask to leave headroom.
#
# min_items / min_total_* are BLOCKING (a thin document is a real defect and rework can
# fix it by adding content). Per-item min_rows / min_paras stay MINOR on purpose: a
# Version History sheet legitimately has 2 rows, and making that blocking would flag it
# every round with no possible fix — the same unfixable-finding trap that the
# bullets-vs-paragraphs rule fell into. The aggregate catches "this whole document is
# thin" without punishing a legitimately short individual sheet.
MIN_VOLUME = {
    "xlsx": {"container": "sheets",   "min_items": 6,  "min_rows": 12, "min_total_rows": 80},
    "docx": {"container": "sections", "min_items": 8,  "min_paras": 3, "min_total_body": 30},
    "pdf":  {"container": "sections", "min_items": 8,  "min_paras": 3, "min_total_body": 30},
    "pptx": {"container": "slides",   "min_items": 10},
    "xml":  {"container": "sheets",   "min_items": 4,  "min_rows": 8,  "min_total_rows": 40},
}

# ── Why a caller may override the table above ────────────────────────────────
# These floors assume a document whose slide/sheet count is free to grow. That holds
# for cascade, where each node invents its own structure. It does NOT hold for an
# adhoc document modelled on a specific reference file, and the pptx row is where the
# assumption did real damage:
#
#   The user asked for a 4-slide governance deck. Plan committed to 4. Generate
#   produced 4 — correct. Then `min_items: 10` fired MAJOR "Only 4 slides produced;
#   the format requires at least 10", and rework dutifully appended six thin slides.
#   Five successive prompt-level fixes could not win, because this check runs AFTER
#   generation and rework obeys it.
#
# Note also that pptx is the only format here with no depth floor at all. So the table
# polices the WRONG AXIS for decks: ten thin slides pass, four dense ones fail — the
# exact inverse of what makes a good deck. A caller that has measured its reference can
# pass floors describing that reference instead (see authoring.reference_volume_floors).
# `volume_floors=None` reproduces this table exactly, so cascade is unaffected.

_PLACEHOLDER_TOKENS = (
    "lorem ipsum", "tbd", "to be determined", "todo", "placeholder",
    "xxx", "n/a - fill", "<insert", "[insert", "example text", "sample data",
)

# Schema keys that are ALTERNATIVES or decorations, not mandatory content.
# A docx section legitimately has paragraphs OR bullets OR a table — flagging an empty
# `bullets` array as a blocking defect would send almost every document into a rework
# loop it does not need. (Caught by the package's own smoke test; see tests/.)
OPTIONAL_KEYS = {
    "subtitle", "bullets", "table", "has_totals", "chart_sheet", "chart_data_col",
    "notes", "level", "type", "rows", "headers",
}

# Containers whose emptiness IS a real defect, checked at the top level only.
TOP_LEVEL_CONTAINERS = {"sections", "sheets", "slides"}

# Keys that are ALTERNATIVES TO ONE ANOTHER inside the same object.
#
# A docx/pdf section legitimately carries paragraphs OR bullets OR a table. Requiring
# each one individually flags every bullet-shaped section as MAJOR — and MAJOR means
# REWORK. Cutover Plan, Go-Live Checklist, KT Plan and Onboarding Deck are naturally
# bullet-shaped, so they would enter a rework loop that CANNOT succeed: the only "fix"
# is to invent prose that does not belong, so the finding survives every round until
# no_progress() halts the loop, having burned an Opus call per round for nothing.
#
# OPTIONAL_KEYS already excused `bullets` and `table`, but not `paragraphs` — so the
# reciprocal case (bullets present, paragraphs absent) still fired. This closes it:
# the group is checked ONCE, and is satisfied when ANY member carries content.
ALTERNATIVE_GROUPS = [
    {"paragraphs", "bullets", "table"},
]


def _has_content(value: Any) -> bool:
    """True when a field actually carries something (not just present-but-empty)."""
    if isinstance(value, (list, dict)):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _group_satisfied(draft_obj: Dict[str, Any], group: set) -> bool:
    return any(_has_content(draft_obj.get(k)) for k in group)


def _f(severity: str, section: str, issue: str, fix: str) -> Dict[str, Any]:
    return {
        "severity": severity,
        "section": section,
        "issue": issue,
        "fix": fix,
        "reviewer": "structure",
    }


def _walk_required(draft: Any, schema: Any, path: str, out: List[Dict[str, Any]]) -> None:
    """Recursively verify the draft carries every key the schema declares."""
    if isinstance(schema, dict):
        if not isinstance(draft, dict):
            out.append(_f("CRITICAL", path or "root",
                          f"Expected an object at '{path or 'root'}', got "
                          f"{type(draft).__name__}.",
                          "Emit an object with the schema's keys at this position."))
            return

        # Alternative groups are judged ONCE for the whole group, before the per-key
        # loop, so a section carrying bullets is not faulted for lacking paragraphs.
        grouped: set = set()
        for group in ALTERNATIVE_GROUPS:
            members = group & set(schema.keys())
            if not members:
                continue
            grouped |= members
            if not _group_satisfied(draft, members):
                out.append(_f("MAJOR", path or "root",
                              f"None of {sorted(members)} carries any content — this "
                              "section is empty.",
                              f"Populate at least one of {sorted(members)} with real "
                              "project content."))
            # Still validate the shape of whichever members are actually present.
            for key in sorted(members):
                if key in draft:
                    _walk_required(draft[key], schema[key],
                                   f"{path}.{key}" if path else key, out)

        for key, sub_schema in schema.items():
            if key in grouped:
                continue
            if key not in draft:
                if key in OPTIONAL_KEYS:
                    continue          # alternative/decorative field — absence is fine
                out.append(_f("MAJOR", path or "root",
                              f"Required field '{key}' is missing.",
                              f"Add '{key}' to the object at '{path or 'root'}'."))
                continue
            _walk_required(draft[key], sub_schema, f"{path}.{key}" if path else key, out)

    elif isinstance(schema, list):
        if not isinstance(draft, list):
            out.append(_f("CRITICAL", path,
                          f"Expected an array at '{path}', got {type(draft).__name__}.",
                          "Emit an array at this position."))
            return
        if not draft:
            leaf = path.split(".")[-1].split("[")[0]
            # Only a top-level content container being empty is a blocking defect.
            # An empty `bullets` next to a populated `paragraphs` is normal.
            if leaf in TOP_LEVEL_CONTAINERS:
                out.append(_f("MAJOR", path, f"Array '{path}' is empty.",
                              "Populate this array with real project content."))
            return
        # Schema arrays carry one-or-more exemplar shapes; validate items against the
        # first exemplar only when the item is an object (pptx slides are a union of
        # shapes, so a strict per-item match would produce false positives).
        if schema and isinstance(schema[0], dict) and len(schema) == 1:
            for i, item in enumerate(draft[:3]):    # sample the first 3, not all
                _walk_required(item, schema[0], f"{path}[{i}]", out)


def _check_volume(draft: Any, output_format: str, out: List[Dict[str, Any]],
                  floors: Dict[str, Any] = None) -> None:
    rules = MIN_VOLUME.get((output_format or "").lower())
    if not rules or not isinstance(draft, dict):
        return
    if floors:
        # Caller-measured floors win key by key; `container` is never overridable
        # because it is a property of the schema, not of the reference.
        rules = {**rules, **{k: v for k, v in floors.items() if k != "container"}}

    container = rules["container"]
    items = draft.get(container)
    if not isinstance(items, list):
        return

    if len(items) < rules["min_items"]:
        out.append(_f("MAJOR", container,
                      f"Only {len(items)} {container} produced; the format requires at "
                      f"least {rules['min_items']}.",
                      f"Expand to at least {rules['min_items']} {container} using real "
                      "project content from the plan."))

    total_rows = 0
    total_body = 0

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        # Counted whenever an aggregate floor exists, INDEPENDENTLY of the per-item
        # floor. These were fused, so a caller asking only for `min_total_rows` (the
        # deck case: a title slide legitimately has no `rows` array, which makes the
        # per-item check below a guaranteed false MAJOR) got total_rows = 0 and the
        # aggregate finding fired on every document regardless of its real depth.
        if "min_rows" in rules or "min_total_rows" in rules:
            _r = item.get("rows")
            total_rows += len(_r) if isinstance(_r, list) else 0

        if "min_rows" in rules:
            rows = item.get("rows")
            if not isinstance(rows, list):
                # `rows` is in OPTIONAL_KEYS, so _walk_required deliberately skips it —
                # which meant a sheet with headers and NO rows array at all scored zero
                # findings and shipped as a valid document. An empty spreadsheet is the
                # most likely real defect in an xlsx artifact, so it is caught here and
                # is blocking: unlike the false positive above, rework CAN fix this.
                out.append(_f("MAJOR", f"{container}[{i}]",
                              f"'{item.get('name', i)}' has no 'rows' array — the "
                              f"{container[:-1]} carries no data.",
                              f"Add a 'rows' array with at least {rules['min_rows']} "
                              "rows of real project data."))
            elif len(rows) < rules["min_rows"]:
                out.append(_f("MINOR", f"{container}[{i}]",
                              f"'{item.get('name', i)}' has {len(rows)} rows; minimum "
                              f"is {rules['min_rows']}.",
                              "Add substantive data rows."))
        if "min_paras" in rules:
            paras = item.get("paragraphs")
            bullets = item.get("bullets")
            body = (len(paras) if isinstance(paras, list) else 0) + \
                   (len(bullets) if isinstance(bullets, list) else 0)
            total_body += body
            if body < rules["min_paras"]:
                out.append(_f("MINOR", f"{container}[{i}]",
                              f"'{item.get('heading', i)}' has almost no body content.",
                              f"Add at least {rules['min_paras']} paragraphs or bullets."))

    # ── Aggregate depth: BLOCKING ────────────────────────────────────────────
    # This is the check that catches "the document is technically valid but far too
    # shallow to hand a client" — the failure that shipped silently before. It is
    # judged across the whole document, so an individually short sheet is fine as
    # long as the artefact as a whole carries real depth. Rework can act on it.
    if "min_total_rows" in rules and total_rows < rules["min_total_rows"]:
        out.append(_f("MAJOR", container,
                      f"The document has {total_rows} data rows in total across "
                      f"{len(items)} {container}; a client-ready artefact of this type "
                      f"needs at least {rules['min_total_rows']}.",
                      "Expand the thinnest areas with real project detail drawn from "
                      "the source document and the reference template — do not pad."))

    if "min_total_body" in rules and total_body < rules["min_total_body"]:
        out.append(_f("MAJOR", container,
                      f"The document has {total_body} paragraphs/bullets in total across "
                      f"{len(items)} {container}; a client-ready artefact of this type "
                      f"needs at least {rules['min_total_body']}.",
                      "Expand the thinnest sections with real project detail drawn from "
                      "the source document and the reference template — do not pad."))

    _check_uniform_depth(items, container, out)


def _check_uniform_depth(items: List[Any], container: str,
                         out: List[Dict[str, Any]]) -> None:
    """
    Flag every container having the SAME depth — the signature of a filled quota.

    Observed on a real Project Plan: twelve module sheets of exactly 24 rows each,
    against a reference whose equivalents vary 22-28 by module. Identical depth
    across many containers does not happen when depth follows scope; it happens when
    the model is satisfying "15-40 rows" uniformly. Aggregate row counts cannot see
    this — the totals looked healthy — so it needs its own check.

    MAJOR, not CRITICAL: rework can genuinely fix it by resizing to scope. Requires
    4+ containers so a small document with naturally equal sections is not punished.
    """
    depths: List[int] = []
    for item in items:
        if not isinstance(item, dict):
            return
        rows = item.get("rows")
        if isinstance(rows, list):
            depths.append(len(rows))
            continue
        paras, bullets = item.get("paragraphs"), item.get("bullets")
        if isinstance(paras, list) or isinstance(bullets, list):
            depths.append((len(paras) if isinstance(paras, list) else 0)
                          + (len(bullets) if isinstance(bullets, list) else 0))

    # Ignore trivially small containers (a 2-row Version History sheet is legitimate
    # and would otherwise drag several documents into a false positive).
    depths = [d for d in depths if d >= 5]
    if len(depths) < 4:
        return

    # A DOMINANT MAJORITY sharing one depth, not literally all of them.
    #
    # Requiring unanimity missed the actual defect. The shipped Project Plan was
    # [120, 23 x 13, 2]: thirteen detail sheets of identical depth, but the Master
    # Plan legitimately differs, so an "all equal" test never fired. The summary
    # sheet is SUPPOSED to differ — it is the detail sheets clustering on one number
    # that betrays the quota. Reference for comparison, which passes: 6 of 13 share
    # a depth (46%).
    from collections import Counter
    top_depth, top_n = Counter(depths).most_common(1)[0]
    if top_n < 4 or top_n / len(depths) < 0.7:
        return

    out.append(_f("MAJOR", container,
                  f"{top_n} of {len(depths)} {container} have exactly {top_depth} rows. "
                  f"Near-identical depth across most {container} means the volume "
                  f"target was filled uniformly rather than sized to what is actually "
                  f"in scope for each one.",
                  f"Resize each {container[:-1]} to its own scope: the ones the source "
                  f"covers in more detail must carry more rows than the ones it barely "
                  f"mentions. Do not pad — redistribute to match real content."))


def _check_placeholders(node_obj: Any, path: str, out: List[Dict[str, Any]]) -> None:
    """Catch the failure that silently ships: schema-valid documents full of filler."""
    if isinstance(node_obj, str):
        low = node_obj.lower().strip()
        for tok in _PLACEHOLDER_TOKENS:
            if tok in low:
                out.append(_f("MAJOR", path,
                              f"Placeholder text detected: '{node_obj[:60]}'.",
                              "Replace with real content derived from the project context."))
                return
        if len(low) < 3 and low not in ("0", "-", ""):
            return
    elif isinstance(node_obj, dict):
        for k, v in node_obj.items():
            _check_placeholders(v, f"{path}.{k}" if path else k, out)
    elif isinstance(node_obj, list):
        for i, v in enumerate(node_obj[:40]):
            _check_placeholders(v, f"{path}[{i}]", out)


def review_structure(draft_json: str, schema: str, output_format: str,
                     volume_floors: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """
    Validate a draft against its schema. Returns findings in reviewer dict shape.
    Never raises — a validator that crashes a cascade is worse than no validator.

    `volume_floors` overrides MIN_VOLUME for this document only. None (every cascade
    caller) uses the table unchanged. See the note above MIN_VOLUME for why an adhoc
    document modelled on a measured reference needs different floors.
    """
    out: List[Dict[str, Any]] = []

    try:
        draft = json.loads(draft_json)
    except Exception as exc:
        return [_f("CRITICAL", "root",
                   f"Draft is not parseable JSON: {str(exc)[:120]}",
                   "Re-emit a single well-formed JSON object with no markdown fences.")]

    try:
        schema_obj = json.loads(schema)
    except Exception:
        schema_obj = None

    try:
        if schema_obj is not None:
            _walk_required(draft, schema_obj, "", out)
        _check_volume(draft, output_format, out, volume_floors)
        _check_placeholders(draft, "", out)
    except Exception as exc:   # pragma: no cover
        out.append(_f("MINOR", "validator",
                      f"Structure validator raised internally: {str(exc)[:120]}",
                      "Ignore; substantive reviewers still applied."))

    # Cap the noise — 40 structure findings will drown the accuracy findings in rework.
    return out[:25]
