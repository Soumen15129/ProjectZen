"""
adhoc_pipeline.py — run the CASCADE quality pipeline for a single adhoc document.

WHY THIS EXISTS
---------------
Adhoc and cascade produced documents by completely different means. Cascade ran
research -> plan -> generate -> review -> bounded rework, then let Claude author the
file directly with the reference in hand. Adhoc made ONE call to get_content_plan()
and rendered the result. Same button, same templates, materially different output —
and the gap was invisible to the user, who simply got a weaker document.

This module closes that gap by REUSING the cascade machinery rather than
reimplementing it. Anything else would drift: two pipelines diverge the first time
one of them is tuned, and the whole point is that adhoc matches the quality bar
already validated on cascade.

WHAT ADHOC LACKS, AND WHY IT DOES NOT MATTER
--------------------------------------------
Cascade supplies three things adhoc has no equivalent for:

  * UPSTREAM DELIVERABLES — a cascade chain feeds each node the documents generated
    before it. Adhoc generates one document, so there are none.

    This is NOT the same as having no source material, and conflating the two was a
    real bug: the first version of this module accepted `input_text` and then never
    referenced it, so the user's SOW was extracted, passed in, and silently dropped.
    The model was left with nothing but the prompt and duly emitted a sheet titled
    "Scope Input Required" — it was reporting the defect, and the run still returned
    HTTP 200 with 22 formulas and inherited styling, so every surface-level signal
    said the pipeline was healthy.

    Cascade delivers the uploaded root document through the SAME parent mechanism
    (run_new_cascade seeds generated_docs[root_node_id] with the raw input text), so
    adhoc does likewise below. The input is the substance of the document; the
    grounding reference only supplies its form.
  * A CASCADE GLOSSARY — the terminology contract that keeps 31 documents agreeing
    with each other. One document cannot disagree with itself, so it starts clean.
  * AN SSE SESSION — cascade streams progress to a subscribed client. Adhoc's request
    is still a blocking POST, so emit_event is a no-op here. Streaming is a separate
    change; the quality work must not wait for it.

Everything else — tier-based model choice, grounding, the reviewer panel, patch-mode
rework, authoring — applies unchanged.

FAILS OPEN. Any error anywhere returns None and the caller falls straight back to the
original single-shot path. A worse document is bad; no document is worse.
"""

import json
import re
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

# Key under which the user's uploaded file is handed to the generator as a "parent".
# _build_gen_prompt resolves each parent key through get_node() and falls back to the
# key itself when it matches no graph node, so this string is what the model actually
# reads as the section heading — hence a sentence, not an identifier.
_INPUT_PARENT_LABEL = "Source Input Document (uploaded by the user for this request)"


def synth_node(template: str, node_id: str = "") -> Dict[str, Any]:
    """
    Build a knowledge-graph-shaped node for an adhoc template.

    The pipeline is written against graph nodes (label, tier, model). Adhoc templates
    are bare strings, so the frontend sends the graph id it maps to and we borrow that
    node's TIER — which is what selects Opus over Sonnet — while keeping the ADHOC
    LABEL. Keeping the label matters: the label reaches prompts and output filenames,
    so inheriting the graph's would make a user who picked "Cutover Strategy" receive
    a file called Cutover Plan.
    """
    # Every key a graph node carries, with safe defaults. _build_gen_prompt reads
    # type / phase / owner / trigger / notes directly with [] subscripting, so a
    # partial copy raises KeyError and the whole pipeline silently falls back to the
    # single-shot path — which is exactly what happened on the first real run: the
    # document still appeared, with zero formulas and template-shaped sheets, and the
    # only clue was one line reading "pipeline failed ('type')".
    node: Dict[str, Any] = {
        "id": node_id or "adhoc", "label": template, "tier": "T2",
        "type": "Document", "phase": "", "owner": "Delivery Team",
        "trigger": "Requested directly by the user (adhoc generation)",
        "notes": "",
    }
    if node_id:
        try:
            from knowledge_graph import get_node
            real = get_node(node_id)
            if real:
                node.update(real)                # inherit everything the graph knows
                node["id"] = node_id
                node["label"] = template          # ...but keep the ADHOC name
        except Exception:
            pass
    return node


def detail_depth_hint(detail: str) -> str:
    """
    Keep the per-slide DEPTH guidance from the base format detail, drop its slide-count
    quota.

    The base pptx line is "Generate 10-18 slides. Mix title, bullets, and table slide
    types." — the count is what has to go, the rest is still useful. Anything appended
    later (the measured reference depth hint) is preserved untouched.
    """
    kept = []
    for chunk in (detail or "").split("\n\n"):
        first = chunk.strip()
        if re.match(r"^Generate\s+\d+\s*[-–]\s*\d+\s+(slides|sections)\b", first, re.I):
            # Strip only the leading count sentence; keep any advice after it.
            rest = re.sub(r"^Generate\s+\d+\s*[-–]\s*\d+\s+(slides|sections)\.\s*", "",
                          first, flags=re.I)
            if rest:
                kept.append(rest)
            continue
        if first:
            kept.append(first)
    return "\n\n".join(kept)


def model_for(node: Dict[str, Any]) -> str:
    """Tier-based model choice, exactly as cascade does it."""
    try:
        from knowledge_graph import TIER_MODELS
        return TIER_MODELS.get(node.get("tier", "T2"), "claude-opus-4-8")
    except Exception:
        return "claude-opus-4-8"


# ── Node-ID sets / keyword sets for document-type detection ──────────────────
_RTM_NODE_IDS     = {"rtm"}
_RTM_KEYWORDS     = ("rtm", "requirement traceability", "traceability matrix")
_CONFIRM_KEYWORDS = ("config", "configuration", "workbook", "design", "test",
                     "deployment", "checklist", "fit", "gap", "solution")


def _adhoc_quality_blocks(
    template: str,
    node_id: str,
    output_format: str,
    project_ctx: Optional[Dict[str, Any]] = None,
    sheet_shape: str = "unknown",
) -> str:
    """
    Return extra_blocks prompt text that enables the Portugal TDLC quality
    patterns for adhoc generation.

    Items implemented here:
      01 — TBC / Confirmed / Out-of-Scope status tracking
      02 — AI-draft flagging in the Notes column
      03 — RTM-specific: unique IDs, user stories, Config Workbook reference
      05 — Scope checkpoint: explicit confirmed-in-scope module list
      08 — Open items tracking: dedicated Open Items sheet

    ADHOC ONLY — this function is called only from build_prompt() inside
    adhoc_pipeline.run(), never from cascade_agent.  Cascade has its own
    quality machinery and must not be affected.
    """
    t_lower = template.lower()
    n_lower = node_id.lower()
    blocks: list = []

    # ── Item 05 — Scope checkpoint ────────────────────────────────────────
    # Uses the already-extracted project_ctx (no extra Claude call).
    # Injects an explicit CONFIRMED IN-SCOPE list so Claude doesn't generate
    # for modules the client hasn't purchased / hasn't confirmed.
    # Skipped silently when project_ctx is absent or has no modules listed.
    if project_ctx:
        in_scope  = [str(m) for m in (project_ctx.get("in_scope_modules") or []) if m]
        out_scope = [str(m) for m in (project_ctx.get("out_of_scope")     or []) if m]
        geo_scope = (project_ctx.get("geographic_scope") or "").strip()

        if in_scope:
            # ENUMERATED, not prose. Measured: with the list rendered as a
            # comma-joined sentence and the only rule being "generate ONLY for
            # the in-scope items", a Project Plan run silently dropped two
            # contracted modules (CDP & SP, EC Benefits) — both named verbatim
            # in the source. The instruction read as a filter, so the model
            # applied it downward and never checked for completeness upward.
            # A numbered list with a stated count is something the model can
            # verify itself against; a sentence is not.
            numbered = "\n".join(
                f"    {i}. {m}" for i, m in enumerate(in_scope, 1))
            scope_lines = [
                "SCOPE CHECKPOINT — confirmed from source document analysis:\n",
                f"  IN SCOPE ({len(in_scope)} items) — every one MUST appear:\n",
                numbered + "\n",
            ]
            if out_scope:
                scope_lines.append(
                    f"  OUT OF SCOPE: {', '.join(out_scope)}\n"
                )
            if geo_scope:
                scope_lines.append(
                    f"  GEOGRAPHY : {geo_scope}\n"
                )
            # HOW the items must be carried depends on how the reference
            # organises itself, measured in scope_gate.reference_shape().
            # Demanding a sheet per module on a document whose reference is
            # organised by test phase would rewrite a working deliverable —
            # the Test Script reference has 'SIT Test Cases' / 'UAT Test
            # Cases' and no module sheet at all.
            if sheet_shape == "tabs":
                floor_shape = (
                    "For xlsx that means one dedicated sheet per module or "
                    "workstream; for docx one dedicated section. ")
            else:
                floor_shape = (
                    "Carry them using the organising structure THE REFERENCE "
                    "TEMPLATE uses — if it is organised by phase, scenario or "
                    "process rather than by module, follow that and make sure "
                    "every in-scope item appears within it. Do NOT impose a "
                    "sheet-per-module layout the reference does not use. ")
            scope_lines.append(
                "\nSCOPE IS A FLOOR AS WELL AS A CEILING — BOTH ARE ENFORCED:\n"
                f"  FLOOR: all {len(in_scope)} in-scope items above must be "
                "represented. " + floor_shape +
                "Silently omitting an in-scope item is a CRITICAL failure — "
                "it ships a plan for contracted work that will never be "
                "scheduled, staffed or costed, and it looks correct while "
                "doing so.\n"
                "  CEILING: generate detailed content only for those items. "
                "For any out-of-scope area, set Status = 'Out of Scope' "
                "and include a single placeholder row — do not omit it "
                "silently and do not generate detailed content for it.\n"
                "  If an area is present in the reference template but absent "
                "from the in-scope list, mark it Out of Scope.\n"
                "  BEFORE YOU RETURN: check your output against the numbered "
                f"list above. If any of the {len(in_scope)} is not covered, "
                "add it before returning the JSON.\n"
                "  SCOPE NEVER COSTS DEPTH. This list bounds WHICH areas you "
                "cover, never HOW DEEPLY you cover them. Do not shorten a "
                "sheet, drop activities, or thin the primary sheet in order to "
                "satisfy it — adding the missing areas must ADD rows to the "
                "document, never redistribute the rows you already had.\n"
            )
            blocks.append("".join(scope_lines))

    # ── Items 01 + 07 — Status tracking for xlsx / docx ──────────────────
    # Instructs Claude to include a Status column so the template layer can
    # apply TBC (amber) / OOS (grey) / Confirmed fills automatically.
    if output_format in ("xlsx", "docx"):
        blocks.append(
            "DELIVERY QUALITY — STATUS TRACKING (required):\n"
            "For every sheet or table section that contains requirements, "
            "configuration items, test cases, action items, or work items:\n"
            "  • Include a 'Status' column (xlsx) or Status field in each row "
            "(docx tables).\n"
            "  • Each row must have exactly one of:\n"
            "      'Confirmed'    — content is agreed / explicitly in the source document\n"
            "      'TBC'          — content depends on a pending client decision\n"
            "      'Out of Scope' — area is not in scope for this engagement\n"
            "  • Do NOT add a Status column to metadata-only sheets such as "
            "Version History or Guidance.\n"
            "  • Never leave the Status cell blank.\n"
        )

    # ── Item 02 — AI-draft flagging for xlsx ─────────────────────────────
    # Instructs Claude to mark inferred rows in the sheet's notes column so
    # the template layer can highlight them for consultant review.
    #
    # REUSE BEFORE CREATE. The first wording said only "include a 'Notes'
    # column as the last column". Delivery templates in this estate already
    # carry one — 'Comments / Notes' — so the model honoured the instruction
    # literally and produced BOTH: 'Comments / Notes' left completely empty
    # and a new 'Notes' column holding every flag. A consultant checking the
    # column the template has always used saw nothing at all.
    if output_format == "xlsx":
        blocks.append(
            "DELIVERY QUALITY — AI-DRAFT FLAGGING (required):\n"
            "For every sheet that contains requirements, test cases, or "
            "configuration items:\n"
            "  • Use the sheet's EXISTING notes column when it has one — any "
            "column named 'Notes', 'Comments', 'Comments / Notes', 'Remarks' "
            "or similar. Do NOT add a second notes column next to it: a sheet "
            "carrying both 'Comments / Notes' and 'Notes' hides the content in "
            "the column readers do not check. Only when the sheet has no such "
            "column at all should you add one named 'Notes' as the last "
            "column.\n"
            "  • In that column, when a row's content was inferred or "
            "generated rather than taken word-for-word from the source "
            "document, set the value to: '(AI draft — review before use)'\n"
            "  • When the row is sourced verbatim from the input, leave it "
            "blank.\n"
            "  • THE FLAG IS A SIGNAL, NOT A DISCLAIMER. Its whole purpose is "
            "to separate the rows a reviewer must scrutinise from the rows "
            "they can trust. Putting it on every row destroys that: a column "
            "where all 124 entries read '(AI draft)' tells a consultant "
            "nothing and is worse than an empty one. Expect a MINORITY of "
            "rows to carry it.\n"
            "  • NEVER APPEND IT TO A NOTE THAT CITES THE SOURCE. If the note "
            "states something the document actually says — a figure, a named "
            "constraint, a slide reference, a stated exclusion — that note is "
            "EVIDENCE, not a draft. Write the fact on its own and leave the "
            "flag off. 'Slide 2 says 9,133; assumptions say ~9,345' is "
            "sourced; appending '(AI draft)' to it is wrong.\n"
            "  • Do not add a notes column to metadata sheets.\n"
        )

    # ── Cell-level precision for filterable columns ──────────────────────
    #
    # Both rules below are measured failures from the 08 Aug Test Script, and
    # both share a cause: a cell that is technically populated but carries no
    # usable information, so nothing downstream flags it.
    #
    #   Country column — 11 rows read 'IN/NP/UAE/EG', plus 'UAE/Egypt',
    #   'EC-only 9' and 'N-A'. None survive a filter, so the rows they
    #   describe drop out of every per-country view. Over the same run the
    #   four countries carrying payroll and statutory scope lost roughly half
    #   their coverage to the catch-all 'Global'.
    #
    #   Traceability column — 143 of 252 rows read a bare '(no map)'. The
    #   previous generation wrote '(no map - validate vs Config Workbook)'
    #   and '(no map - Comparison Template)'. Same absence, but one tells a
    #   test lead where to go next and the other is a dead end.
    if output_format == "xlsx":
        blocks.append(
            "DELIVERY QUALITY — CELL-LEVEL PRECISION (required for xlsx):\n"
            "  • ONE VALUE PER CATEGORICAL CELL. Any column a reader will "
            "filter or group by — Country, Module, Phase, Owner, Priority, "
            "Status — holds exactly one value per row. If a row genuinely "
            "applies to four countries, write four rows, or write the one "
            "country the row is really about. Never 'IN/NP/UAE/EG', never "
            "'UAE/Egypt', never a count like 'EC-only 9'. A combined cell "
            "silently disappears from every filtered view, taking its row "
            "with it.\n"
            "  • PREFER THE SPECIFIC VALUE OVER THE CATCH-ALL. Reach for a "
            "blanket value such as 'Global' or 'All' only when the row truly "
            "is identical everywhere. Where a country, module or role has its "
            "own rules — statutory logic, local approvals, a distinct "
            "calendar — that specific value is what makes the row testable, "
            "and generalising it away is a loss of coverage even though the "
            "row count is unchanged.\n"
            "  • BUT SPECIFICITY MUST NEVER COST BREADTH. Where the source "
            "enumerates a SET — 13 countries, two payroll waves, five "
            "environments, a list of roles — EVERY member of that set needs "
            "at least one row naming it. Concentrating depth on the few "
            "members that carry the most rules, and letting the rest fall to "
            "zero, is not focus: it is a silent scope gap in the exact shape "
            "the reader cannot see, because the rows that remain all look "
            "correct. Deepen the high-risk members by ADDING rows, never by "
            "quietly dropping the others.\n"
            "  • CARRY THE SOURCE'S OWN GROUPINGS. When the input groups "
            "members of a set under named labels — waves, phases, releases, "
            "tranches — those labels are delivery facts that drive sequencing "
            "and scheduling, so keep them on the rows they belong to. Covering "
            "all the members while dropping the grouping that orders them "
            "loses information the source stated explicitly.\n"
            "  • A REFERENCE COLUMN THAT SAYS 'NONE' MUST SAY WHAT INSTEAD. "
            "For traceability columns — source map, requirement ref, document "
            "ref — never write a bare '(no map)', 'N/A' or '-'. State what "
            "the row should be validated against instead: "
            "'(no map - validate vs Config Workbook)', "
            "'(no map - SAP standard behaviour)', "
            "'(no map - confirm scope with HR Lead)'. The reader's next "
            "action is the entire value of that column.\n"
        )

    # ── Rollup sheets must reconcile ─────────────────────────────────────
    #
    # WHY: measured across every Test Script generated so far, including runs
    # predating any of the gate work, the summary tab has never agreed with the
    # workbook it summarises:
    #
    #     06 Aug   no numeric counts at all
    #     05:50    claims India 60   against an actual 114
    #     08:38    claims SIT 172 / UAT 16 / PPT 12
    #              against an actual 262 /  38 /  24,
    #              'Total TCs' blank on every row, and four countries
    #              (Nepal, UAE, Egypt, Turkey) each listed TWICE with
    #              different numbers
    #
    # This is the SECOND tab a reader opens. A rollup that contradicts the
    # detail behind it discredits the detail too — and unlike a thin module,
    # it is obvious to anyone who adds up a column.
    if output_format == "xlsx":
        blocks.append(
            "DELIVERY QUALITY — ROLLUP SHEETS MUST RECONCILE (required for xlsx):\n"
            "If you produce any summary, dashboard or coverage sheet that "
            "counts or totals what is in the other sheets:\n"
            "  • EVERY COUNT CELL MUST CONTAIN A NUMBER. A blank is not an "
            "acceptable answer, and neither is a dash: if you cannot count a "
            "row, do not include that row at all. A rollup with empty cells is "
            "worse than no rollup, because it looks like a section someone "
            "forgot to finish. Write 0 where the real answer is zero.\n"
            "  • COUNT FROM THE ROWS YOU ACTUALLY WROTE — the real number of "
            "matching rows elsewhere in this workbook, not an estimate and not "
            "a figure carried over from an earlier draft.\n"
            "  • ONE ROW PER LABEL. Never list the same category, module or "
            "country twice. Two rows for the same label with different numbers "
            "is worse than omitting it: the reader cannot tell which is real.\n"
            "  • FILL THE TOTAL COLUMN. A 'Total' column left blank while the "
            "breakdown columns are populated makes the sheet unusable for the "
            "one question it exists to answer. Totals must equal the sum of "
            "their parts.\n"
            "  • BEFORE YOU RETURN: add up each breakdown and check it against "
            "the detail sheets. If they disagree, the detail is right and the "
            "summary is wrong — fix the summary.\n"
        )

    # ── Item 08 — Open items tracking ─────────────────────────────────────
    # Instructs Claude to surface unresolved decisions as a dedicated sheet.
    if output_format == "xlsx":
        blocks.append(
            "DELIVERY QUALITY — OPEN ITEMS (required for xlsx):\n"
            "Include a sheet named 'Open Items' as the last sheet.\n"
            "Columns: ID | Category | Description | Owner | Due Date | Priority | Status\n"
            "  • Populate it with every unresolved decision, pending client "
            "confirmation, or open question found in the source document.\n"
            "  • ID format: OI-001, OI-002, …\n"
            "  • Category: e.g. 'Configuration', 'Data Migration', 'Integration', "
            "'Process', 'Sign-off'.\n"
            "  • Priority: 'High', 'Medium', or 'Low'.\n"
            "  • Status: 'Open' for all new items.\n"
            "  • If no open items are found, include one placeholder row: "
            "OI-001 | General | No open items identified | Delivery Lead | "
            "TBC | Low | Open\n"
            "  • GROUND EVERY ITEM. Describe only what the source actually "
            "says, and cite where (e.g. 'Slide 2 says 9,133, assumptions say "
            "~9,345'). Do not assert a structure the source does not contain "
            "— waves, sequences, phased splits, named owners — in order to "
            "have something to confirm. Where the source is simply silent, "
            "say it is unspecified. An invented detail in this sheet is worse "
            "than an absent one: it reads as a finding and gets actioned.\n"
            "  • Do not add a Notes column to this sheet.\n"
        )

    # ── Item 03 — RTM-specific: unique IDs, user stories, config ref ─────
    is_rtm = (n_lower in _RTM_NODE_IDS
              or any(k in t_lower for k in _RTM_KEYWORDS)
              or any(k in n_lower for k in _RTM_KEYWORDS))
    if is_rtm and output_format == "xlsx":
        blocks.append(
            "RTM-SPECIFIC REQUIREMENTS (this document is a Requirement "
            "Traceability Matrix — apply ALL rules below):\n"
            "1. REQ ID (first column): module-prefixed sequential IDs.\n"
            "   Format: {MODULE}-{NNNN}  e.g. EC-0001, PAY-0014, RCM-0003.\n"
            "   Use the correct 2-4 letter SF module prefix per requirement "
            "area (EC = Employee Central, PAY = Payroll, RCM = Recruiting, "
            "LMS = Learning, PMGM = Performance, ONB = Onboarding, etc.).\n"
            "2. USER STORY (second column after Req ID): structured sentence:\n"
            "   'As a [specific role], I need [specific need] so that "
            "[business outcome].'\n"
            "   Use real roles (HR Admin, Payroll Manager, Employee, "
            "Recruiter) not generic 'user'. Be specific to the requirement.\n"
            "3. STATUS column: Confirmed / TBC / Out of Scope per row "
            "(as defined in the Status Tracking rule above).\n"
            "4. CONFIG WORKBOOK REF column: name the Configuration Workbook "
            "section this requirement maps to (e.g. 'Personal Info tab', "
            "'Pay Component table'). Use 'N/A' only for non-config reqs.\n"
            "5. Every row must reflect a real SF configuration requirement "
            "from the input — no generic placeholders.\n"
        )

    return "\n".join(blocks) + "\n" if blocks else ""


async def _enforce_scope(
    *,
    plan: Dict[str, Any],
    project_ctx: Dict[str, Any],
    grounding_path: str,
    output_format: str,
    build_prompt,
    stream_generate,
    sheet_shape: str = "unknown",
    template: str = "",
) -> Dict[str, Any]:
    """
    Check the finished plan against the in-scope list and, if short, send it
    back ONCE with the specific gaps named.

    Why a check and not a stronger instruction: three runs of the same input
    with only prompt wording changing produced 15, 13 and 12 sheets, and the
    run with the strongest scope wording produced the worst coverage. See the
    measurements in scope_gate.py.

    Guarantees:
      * xlsx only — other formats return untouched.
      * STRUCTURE IS ONLY ENFORCED WHERE THE REFERENCE USES IT. A sheet-per-
        module demand is raised only when the reference itself organises by
        module (`sheet_shape == "tabs"`). A Test Script, whose reference is
        organised by test phase, is reported on but never repaired for
        structure — otherwise this would rewrite a deliverable that works.
      * Depth is checked everywhere: it is additive and structure-neutral.
      * At most ONE extra generation call.
      * NEVER regresses: the repaired plan is adopted only if it closes gaps
        without losing sheets or rows. Otherwise the original stands.
      * Fail-open: any exception returns the original plan.
    """
    if output_format != "xlsx":
        return plan

    try:
        import scope_gate
    except Exception as exc:
        print(f"   [scope-gate] unavailable ({type(exc).__name__}); skipped")
        return plan

    def _finish(p: Dict[str, Any]) -> Dict[str, Any]:
        """
        Last thing before the plan leaves the gate: make the rollup sheet agree
        with the document. Runs on EVERY exit path, and after any merge, because
        the counts are only final once nothing more will be added. Inert unless
        a sheet actually has a 'Total TCs' column.
        """
        try:
            p2, notes = scope_gate.recompute_rollup(p)
            if notes:
                print(f"   [scope-gate] rollup recomputed ({len(notes)} cell(s)): "
                      + "; ".join(notes[:4]))
            return p2
        except Exception:
            return p

    try:
        modules = [str(m) for m in (project_ctx.get("in_scope_modules") or []) if m]
        if not modules:
            print("   [scope-gate] no in-scope modules extracted; nothing to check")
            return _finish(plan)

        # Shape was measured once in run() and passed in, so the prompt and
        # this check cannot disagree. 'unknown' means no measurable reference,
        # which is a reason to enforce nothing structural — not a reason to
        # guess.
        # Collapse variant spellings of the same value before anything measures
        # the plan — R7 held 'N/A' on 165 rows and 'N-A' on 100 of the same
        # column, which breaks a filter and would also split the counts every
        # check below depends on.
        plan, _spell = scope_gate.unify_value_spellings(plan)
        if _spell:
            print(f"   [scope-gate] unified value spellings — {'; '.join(_spell[:4])}")

        shape = sheet_shape if sheet_shape in ("tabs", "rows") else "rows"
        report = scope_gate.analyse(plan, modules, shape)

        # ── Depth floor, anchored to BOTH the reference and our own history ──
        #
        # The reference alone has no memory. Measured on Test Script: the
        # reference carries 223 data rows, a 06 Aug run produced 319, and an
        # 08 Aug run produced 220 — a 38% regression that cleared the
        # reference-only threshold of 211 and passed silently.
        floor       = scope_gate.reference_row_floor(grounding_path)
        prior, pnm, ppath = scope_gate.prior_best_run(
            template, output_format, reference_floor=floor)
        actual      = scope_gate.plan_row_count(plan)
        threshold, why = scope_gate.combined_row_floor(floor, prior)
        shortfall   = max(0, threshold - actual) if threshold else 0

        # ── Where that depth SITS ────────────────────────────────────────────
        #
        # The aggregate floor above says how much, never where. The 08 Aug
        # 03:51 Test Script cleared it cleanly (314 rows vs 287) while five of
        # the seven in-scope module groups shared 25 cases between them and EC
        # took 143. Structure-neutral, so it applies whatever shape the
        # reference uses — this is about depth, not layout.
        balance = scope_gate.module_balance(plan, modules)
        if balance["starved"]:
            print(f"   [scope-gate] uneven depth — mean {balance['mean']} "
                  f"rows/module, floor {balance['floor']}: "
                  + ", ".join(f"{m} ({n})" for m, n in balance["starved"]))

        # ── Enforceable floors on named dimensions ───────────────────────────
        #
        # Measured over six runs on identical input, PPT swung 8/4/8/18/24/8 and
        # UAT 34/9/27/22/38/22 — while everything Python governs held steady.
        # Volume cannot be fixed by instruction: the generator reallocates a
        # fixed output budget every run, so each new emphasis just moves the
        # shortfall somewhere else. So these two are measured and enforced.
        #
        # Structurally inert elsewhere: both columns are absent from every
        # Project Plan, and dimension_balance() no-ops on a missing column.
        # The recent history, newest first. More than one run is what lets the
        # floor tell an established dimension from a single run's vocabulary
        # drift — without it, one stray value becomes a permanent obligation.
        _hist = scope_gate.recent_run_files(template, output_format)
        # The run history is keyed on TEMPLATE only — the documents table has no
        # project column — so "recent Test Scripts" spans every client. Without
        # this vocabulary, a new customer's document is measured against the
        # previous customer's countries and the repair demands them back. The
        # gate is this run's own extracted context: defend a value only if THIS
        # input mentions it.
        _vocab = scope_gate.project_vocabulary(project_ctx)
        if _hist and not _vocab:
            print("   [scope-gate] project context too thin to scope the "
                  "dimension floors; comparing on history alone")
        dims = []
        for _col in ("Test Level", "Country"):
            _d = (scope_gate.dimension_balance(plan, _col, _hist, _vocab)
                  if _hist else None)
            if _d and _d["shortfalls"]:
                dims.append(_d)
                print(f"   [scope-gate] '{_col}' lost coverage vs {pnm}: "
                      + ", ".join(f"{v} {p}->{c}" for v, p, c in _d["shortfalls"]))

        # The scope CEILING. The prompt has always asked for this and complied
        # inconsistently — R5 and R6 acknowledged all six exclusions, R7 named
        # none of them and carried no 'Out of Scope' status at all. Presence is
        # checkable, so it is checked.
        _oos = [str(x) for x in (project_ctx.get("out_of_scope") or []) if x]
        ceiling = scope_gate.out_of_scope_gaps(plan, _oos, modules)
        if ceiling["needs_repair"]:
            print(f"   [scope-gate] scope ceiling missing — {ceiling['expected']} "
                  f"exclusion(s) named by the source, 0 rows marked 'Out of Scope'")

        # Advisory only — never triggers a repair by itself. It rides along
        # when one is already being sent, so a signal that cannot tell a
        # regression from a legitimate re-cut never costs a generation call.
        col_notes = (scope_gate.column_regressions(plan, ppath, _vocab)
                     if ppath else [])
        if col_notes:
            print(f"   [scope-gate] coverage shrank vs {pnm}: "
                  + " | ".join(col_notes))

        print(f"   {scope_gate.summarise(report)}")
        if threshold:
            verdict = "OK" if not shortfall else f"SHORT by {shortfall}"
            print(f"   [scope-gate] depth {actual} rows vs floor {threshold} "
                  f"[{why}] — {verdict}")
            if prior:
                print(f"   [scope-gate] best prior run: {prior} rows ({pnm})")

        if not report["needs_repair"] and not shortfall \
                and not balance["needs_repair"] and not dims and not ceiling["needs_repair"]:
            return _finish(plan)

        instruction = scope_gate.repair_instruction(
            report, row_shortfall=shortfall, row_floor=threshold,
            row_actual=actual, balance=balance, column_notes=col_notes,
            dimensions=dims, excluded=(ceiling["items"] if ceiling["needs_repair"] else None))

        print("   [scope-gate] requesting one repair pass (row delta)")
        raw = await stream_generate(build_prompt(instruction))
        delta = json.loads(raw)

        # The repair returns ONLY the rows it is adding; Python appends them.
        # Asking for the whole plan back is what made this pass useless — see
        # the measurement in scope_gate.repair_instruction(). A merge cannot
        # thin or drop existing content, so the guards below are now checking
        # for an ineffective repair rather than a destructive one.
        repaired, added, notes = scope_gate.merge_row_additions(plan, delta)
        if not added:
            print("   [scope-gate] repair returned no usable rows; "
                  "keeping original")
            return _finish(plan)
        print(f"   [scope-gate] merged +{added} row(s): {', '.join(notes[:6])}")

        # ── Adopt only on strict improvement ──────────────────────────────
        r_report  = scope_gate.analyse(repaired, modules, shape)
        r_rows    = scope_gate.plan_row_count(repaired)
        r_sheets  = len(repaired.get("sheets") or [])
        o_sheets  = len(plan.get("sheets") or [])
        r_balance = scope_gate.module_balance(repaired, modules)

        gaps_before = len(report["absent"]) + len(report["in_rows_only"])
        gaps_after  = len(r_report["absent"]) + len(r_report["in_rows_only"])
        thin_before = len(balance["starved"])
        thin_after  = len(r_balance["starved"])
        # Dimension shortfalls counted the same way: a repair that fills PPT by
        # emptying UAT must not read as a success.
        dim_before  = sum(len(d["shortfalls"]) for d in dims)
        dim_after   = sum(len(scope_gate.dimension_balance(
                              repaired, d["column"], _hist, _vocab)["shortfalls"])
                          for d in dims)
        excl_after  = (1 if scope_gate.out_of_scope_gaps(
                            repaired, _oos, modules)["needs_repair"] else 0)
        excl_before = 1 if ceiling["needs_repair"] else 0

        # Starving a NEW module while filling the reported one is the specific
        # way a "make these even" instruction goes wrong — the model rebalances
        # by cutting instead of adding. Row count alone would not catch it,
        # because a pure reshuffle keeps the total identical.
        worse = (gaps_after > gaps_before or r_sheets < o_sheets
                 or r_rows < actual or thin_after > thin_before
                 or dim_after > dim_before or excl_after > excl_before)
        # And it must actually FIX something. "Not worse" was the old bar, and
        # it let a repair that returned the plan completely unchanged be logged
        # as accepted — the content was identical, so nothing broke, but the
        # log claimed a fix that never happened and a failed repair looked
        # like a successful one.
        better = (gaps_after < gaps_before or r_rows > actual
                  or thin_after < thin_before or dim_after < dim_before
                  or excl_after < excl_before)

        if worse or not better:
            why = "regressed" if worse else "changed nothing"
            print(f"   [scope-gate] repair rejected ({why}) — gaps "
                  f"{gaps_before}->{gaps_after}, sheets {o_sheets}->{r_sheets}, "
                  f"rows {actual}->{r_rows}, thin {thin_before}->{thin_after}; "
                  f"keeping original")
            return _finish(plan)

        print(f"   [scope-gate] repair accepted — gaps {gaps_before}->{gaps_after}, "
              f"sheets {o_sheets}->{r_sheets}, rows {actual}->{r_rows}, "
              f"thin {thin_before}->{thin_after}")
        return _finish(repaired)

    except Exception as exc:
        print(f"   [scope-gate] skipped ({type(exc).__name__}): {str(exc)[:110]}")
        return _finish(plan)


async def run(
    template: str,
    node_id: str,
    output_format: str,
    user_prompt: str,
    input_text: str,
    grounding_text: str = "",
    grounding_path: str = "",
    on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Optional[Tuple[Dict[str, Any], str]]:
    """
    Produce a reviewed document plan for one adhoc template.

    Returns (plan, model) on success, or None to tell the caller to use the old path.
    """
    try:
        from cascade_agent import (
            _FORMAT_SCHEMAS, _FORMAT_DETAIL, _sanitize_plan, _clean_json,
            MAX_SOURCE_CHARS,
        )
        from llm_client import complete, usage_scope
        from quality.pipeline import run_quality_pipeline
    except Exception as exc:
        print(f"   [adhoc] pipeline unavailable ({str(exc)[:100]}); single-shot")
        return None

    node = synth_node(template, node_id)
    model = model_for(node)
    schema = _FORMAT_SCHEMAS.get(output_format, _FORMAT_SCHEMAS["docx"])
    detail = _FORMAT_DETAIL.get(output_format, _FORMAT_DETAIL["docx"])

    # Measured per-sheet depth from the actual reference beats the generic
    # "15-40 rows" range — the same hint cascade feeds its generator.
    try:
        from authoring import reference_depth_hint
        hint = reference_depth_hint(grounding_path)
        if hint:
            detail = detail + "\n\n" + hint
    except Exception:
        pass

    # How the reference ORGANISES itself, for decks.
    #
    # Injected through detail_override rather than by editing _build_gen_prompt, because
    # that function is cascade's and cascade is out of scope.
    #
    # The base instruction for pptx is "Generate 10-18 slides", and it must be REPLACED
    # here, not appended to. Measured: with the blueprint and an admin prompt saying
    # "exactly four slides" both present but that sentence still leading, the planner
    # produced exactly 10 — the bottom of the range — with 3 rows on most of them. An
    # explicit numeric quota beats advisory prose every time, and the two were pulling
    # in opposite directions: one demanded breadth, the others demanded depth.
    deck_scope_rule = ""
    try:
        from authoring import reference_deck_blueprint
        blueprint = reference_deck_blueprint(grounding_path)
        if blueprint and output_format in ("pptx", "docx"):
            # The planner's default scope rule says "one section per in-scope inventory
            # item" and calls following the template's section list a CRITICAL failure.
            # For a scope-coverage document that is right. For a deck modelled on a
            # reference it is the single reason the output had ten thin slides: SP051
            # has about ten workstreams, so the planner produced one slide each and
            # ignored both the volume requirement and the admin instruction asking for
            # four. Same inventory, same coverage — expressed as ROWS inside fixed
            # slides instead of as more slides.
            deck_scope_rule = (
                "SCOPE RULE — read before planning anything:\n"
                "The TEMPLATE STRUCTURE above comes from a DIFFERENT project. Take its "
                "SCOPE from the research pack's IN-SCOPE INVENTORY and its SHAPE from "
                "the reference organisation below.\n"
                "Every inventory item must be covered, but coverage is expressed as "
                "ROWS AND CELLS WITHIN the fixed set of slides — never as an extra "
                "slide. Adding a slide per inventory item is a CRITICAL planning "
                "failure here: it produces a deck of thin pages instead of the dense "
                "wall chart this deliverable is. If an item has no obvious home, put "
                "it in the slide whose subject it belongs to and say which row "
                "carries it.\n"
                "Match the reference's slide count, or the count the admin "
                "instructions give if they differ, exactly.\n\n"
            )
            detail = (
                "SLIDE COUNT IS FIXED BY THE REFERENCE; DEPTH IS YOUR TARGET.\n"
                "Produce the same number of slides as the reference organisation below "
                "— or the number the ADMIN INSTRUCTIONS specify, if they give one — and "
                "no more. Adding a slide for an extra topic is a defect: fold that "
                "material into whichever listed slide it belongs to.\n\n"
                "Every slide must be DENSE. A grid slide carries a full table of rows; "
                "a matrix slide carries every activity against every role. Judge your "
                "output by how much each slide holds, never by how many slides there "
                "are. Three bullets on a slide is a failure of this brief.\n\n"
                "TWO PRIORITIES, IN THIS ORDER.\n"
                "  1. DETAIL. Every in-scope item gets a row. The row counts below are "
                "a FLOOR, not a ceiling — this project's scope is larger than the "
                "reference project's, so plan more rows where it has more to say. "
                "Dropping or summarising away an in-scope item to keep a slide tidy is "
                "a CRITICAL planning failure.\n"
                "  2. FEWEST SLIDES THAT HOLD IT. With the detail fixed, pack it into "
                "as few slides as possible — raise rows and columns per slide before "
                "planning another one. FOLDING MEANS RE-HOMING A ROW, NEVER DELETING "
                "ONE.\n\n"
                + blueprint + "\n\n" + detail_depth_hint(detail)
            )
        elif blueprint:
            detail = detail + "\n\n" + blueprint
    except Exception:
        pass

    # Volume floors measured from the reference, replacing MIN_VOLUME for this run.
    #
    # This is what actually decided the slide count, and it is not a prompt: the
    # structure validator is pure Python, always on, and runs AFTER generation, so
    # rework obeys it over every instruction the model was given. With
    # MIN_VOLUME["pptx"]["min_items"] = 10 in force, a correct 4-slide deck drew
    # "[MAJOR] Only 4 slides produced; the format requires at least 10" and rework
    # appended six thin slides to satisfy it.
    volume_floors = None
    try:
        from authoring import reference_volume_floors
        volume_floors = reference_volume_floors(grounding_path, output_format) or None
    except Exception:
        pass

    # The uploaded document is the SUBSTANCE of the deliverable, and it reaches the
    # model by the two routes cascade uses — never one or the other:
    #   1. Agent 2 turns it into structured facts (modules, timeline, stakeholders)
    #      that the research and planning stages reason over.
    #   2. The raw text goes to the generator as a parent, which is what stops the
    #      model falling back on the grounding reference's scope — a different
    #      project's — when it needs a concrete detail.
    prompt_ctx = {"project_name": template,
                  "scope_summary": user_prompt[:4000],
                  "additional_context": ""}

    if input_text.strip():
        try:
            from cascade_agent import extract_project_context
            with usage_scope(f"adhoc/{node['id']}"):
                project_ctx = await extract_project_context(
                    input_text, f"adhoc-{node['id']}")
        except Exception as exc:
            # Degrade to the raw text rather than to the prompt: losing the structure
            # costs quality, losing the scope produces a document about nothing.
            print(f"   [adhoc] context extraction failed "
                  f"({type(exc).__name__}); using raw input as scope")
            project_ctx = dict(prompt_ctx, scope_summary=input_text[:4000])
        # The user's instruction is not in the SOW and must survive alongside it.
        project_ctx["additional_context"] = (
            (project_ctx.get("additional_context") or "")
            + "\n\nUSER REQUEST FOR THIS DOCUMENT:\n" + user_prompt[:2000]
        ).strip()
        # Log what the run believes is in scope. Everything downstream — the
        # scope checkpoint block and the scope gate — is derived from this list,
        # so when a module goes missing this line is what distinguishes a
        # generation failure from an extraction failure. It was not visible
        # anywhere before, which made that exact question unanswerable.
        try:
            _mods = [str(m) for m in (project_ctx.get("in_scope_modules") or []) if m]
            print(f"   [adhoc] in-scope modules ({len(_mods)}): "
                  f"{', '.join(_mods) if _mods else '(none extracted)'}")
        except Exception:
            pass
    else:
        project_ctx = prompt_ctx      # no file supplied — the prompt is the brief

    # Raw source text, delivered exactly as run_new_cascade delivers its root document.
    parents: Dict[str, str] = {}
    if input_text.strip():
        parents[_INPUT_PARENT_LABEL] = input_text[:MAX_SOURCE_CHARS]

    # ── Item 06 — Structured workbook parsing ─────────────────────────────
    # Attempts to parse the tab-CSV structure that extractor.py produces from
    # xlsx files into a compact, field-level prompt block so Claude reads
    # exact SF field IDs and picklist values instead of inferring them from
    # free-form text.  Entirely fail-open: if parsing finds no config
    # structure, _structured_wb is "" and the generation prompt is unchanged.
    _structured_wb = ""
    if input_text.strip() and output_format in ("xlsx", "docx"):
        try:
            from parsers.xlsx_config import parse_xlsx_extraction
            _parsed = parse_xlsx_extraction(input_text)
            if _parsed:
                _structured_wb = _parsed
                _field_count = _parsed.count("\n  ")
                print(f"   [parser] structured config workbook: "
                      f"{len(_structured_wb):,} chars / ~{_field_count} fields")
        except Exception as _pe:
            print(f"   [parser] skipped ({type(_pe).__name__}): "
                  f"{str(_pe)[:80]}")

    # How the reference organises itself, measured ONCE and shared by the
    # prompt and the gate. They must agree: a prompt that asks for a sheet per
    # module while the gate accepts phase sheets (or the reverse) pulls the
    # model two ways and produces neither shape cleanly.
    _sheet_shape = "unknown"
    _org_hint = ""
    if output_format == "xlsx":
        try:
            import scope_gate as _sg
            _sheet_shape = _sg.reference_shape(
                grounding_path,
                [str(m) for m in (project_ctx.get("in_scope_modules") or []) if m],
            )
            print(f"   [scope-gate] reference organises by: {_sheet_shape}")

            # When the reference does NOT divide by module, the way it DOES
            # divide is the thing at risk of being lost. The Test Script
            # reference splits SIT (77%) from UAT (23%); told only to "follow
            # the reference", the generator kept module tabs and let UAT fall
            # to 9 cases against the reference's 43. Naming the split, with
            # measured proportions, is what that instruction was missing.
            # Not injected for module-organised references: there the scope
            # checkpoint already enumerates the same ground, and repeating it
            # would just crowd the prompt.
            if _sheet_shape != "tabs":
                _org_hint = _sg.reference_organisation_hint(grounding_path)
                if _org_hint:
                    print(f"   [scope-gate] reference splits into "
                          f"{_org_hint.count(chr(10) + '    - ')} parts; "
                          f"proportions passed to the generator")
        except Exception as _se:
            print(f"   [scope-gate] shape detection skipped "
                  f"({type(_se).__name__}); structure not enforced")

    def build_prompt(extra_blocks: str = "") -> str:
        from cascade_agent import _build_gen_prompt
        # Prepend adhoc-only quality blocks (Items 01, 02, 03, 05, 07, 08).
        # These are isolated here: cascade has its own build_prompt closure
        # and this function is never called from cascade code paths.
        # Wrapped in try/except so any failure degrades to the original prompt.
        try:
            q_blocks = _adhoc_quality_blocks(
                template, node_id, output_format, project_ctx,
                sheet_shape=_sheet_shape)
        except Exception:
            q_blocks = ""
        # Structured workbook data (Item 06) leads the extra blocks so Claude
        # reads concrete field facts before any quality instructions.
        all_extra = (
            (_structured_wb + "\n" if _structured_wb else "")
            + q_blocks
            + (_org_hint + "\n\n" if _org_hint else "")
            + extra_blocks
        )
        return _build_gen_prompt(
            node_id=node["id"], node=node, project_ctx=project_ctx,
            parent_contents=parents, output_format=output_format,
            grounding_text=grounding_text, delta_items=None,
            existing_content=None, version=1,
            extra_blocks=all_extra, detail_override=detail,
        )

    async def stream_generate(prompt: str, conv=None) -> str:
        for attempt in range(1, 4):
            print(f"   [adhoc] {template} | model={model} | attempt={attempt}")
            try:
                raw = (await conv.turn(prompt, model=model, on_chunk=on_chunk)
                       if conv is not None
                       else await complete(prompt, model=model, on_chunk=on_chunk))
                return json.dumps(_sanitize_plan(json.loads(_clean_json(raw))))
            except Exception as e:
                print(f"   [adhoc] gen attempt {attempt} failed: {str(e)[:110]}")
                if attempt == 3:
                    raise
        raise RuntimeError("generation exhausted retries")

    async def no_events(*_a, **_k):
        return None                     # adhoc has no SSE subscriber yet

    try:
        with usage_scope(f"adhoc/{node['id']}"):
            draft = await run_quality_pipeline(
                session_id=f"adhoc-{node['id']}",
                node=node, node_id=node["id"], version=1,
                project_ctx=project_ctx, parent_contents=parents,
                grounding_text=grounding_text, schema=schema,
                output_format=output_format, detail=detail,
                plan_scope_rule=deck_scope_rule,
                # The admin template prompt and the user's request. They reached the
                # generator but not the planner, so the plan committed to sections the
                # user had explicitly excluded and the generator fought its own plan.
                plan_instructions=user_prompt,
                volume_floors=volume_floors,
                # One document, not thirty-one. DEPTH_BY_TIER exists to keep a full
                # cascade finishing on a Pro seat, and inheriting T2 -> "light" cost
                # this document its research stage: the planner logged "RESEARCH PACK
                # IS EMPTY" and fell back to the reference — another project's — for
                # its scope. "standard" restores research and the accuracy reviewer.
                # Not "full": its extra reviewer is `consistency`, whose ground truth
                # is the cascade glossary, and adhoc has no glossary to check against.
                depth_override="standard",
                build_prompt_fn=build_prompt,
                stream_generate_fn=stream_generate,
                sanitize_fn=_sanitize_plan,
                emit_event_fn=no_events, on_chunk=on_chunk,
                is_delta=False, delta_items=None,
                existing_content="", checkpoint_fns=None,
            )
        plan = json.loads(draft)
        # Deterministic scope + depth gate. Runs AFTER the plan exists, so it
        # judges the actual output rather than trusting the instruction — see
        # scope_gate.py for the measured reason that distinction matters.
        # Fail-open: any error returns the plan untouched.
        plan = await _enforce_scope(
            plan=plan, project_ctx=project_ctx, grounding_path=grounding_path,
            output_format=output_format, build_prompt=build_prompt,
            stream_generate=stream_generate, sheet_shape=_sheet_shape,
            template=template,
        )
        return plan, model
    except Exception as exc:
        # A refusal must surface — it is an account problem, not a content problem,
        # and retrying on the single-shot path would hit the same wall and mislabel it.
        try:
            from quality.refusal import is_refusal
            if is_refusal(exc):
                raise
        except ImportError:
            pass
        # Print the TYPE and a traceback tail, not just str(exc).
        #
        # A bare message is useless for the failures that matter here: this fell back
        # for weeks-worth-of-quality reasons reporting only "('type')" — a KeyError
        # from a partial node dict, indistinguishable at a glance from a legitimate
        # runtime problem. Fail-open must stay quiet in behaviour, never in evidence.
        import traceback
        print(f"   [adhoc] pipeline failed: {type(exc).__name__}: {str(exc)[:160]}")
        print("   " + "\n   ".join(traceback.format_exc().strip().splitlines()[-4:]))
        print("   [adhoc] falling back to single-shot generation")
        try:
            import runlog
            runlog.record_error(exc, where="adhoc_pipeline")
        except Exception:
            pass
        return None
