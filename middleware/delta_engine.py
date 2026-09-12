"""
delta_engine.py — Agent 4: Delta Analysis Agent
Claude Opus tool-use loop that compares document versions, identifies what
changed, and determines downstream impact following the knowledge graph.

Tools available to the agent:
  1. read_stored_document  — fetch a previously generated doc version from DB
  2. get_downstream_impact — list all downstream nodes from the graph
  3. identify_delta        — structured diff of old vs new document text

Entry point:  analyse_delta(...)  → returns DeltaAnalysisResult
"""

import json
import difflib
from typing import Any, Dict, List, Optional

from llm_client import ToolSpec, run_tool_agent
from knowledge_graph import (
    get_downstream_nodes,
    get_node,
    get_change_impact,
    get_default_format,
)
from context_store import DeltaItem, AffectedNode

MODEL    = "claude-opus-4-8"
MAX_ITER = 6
MAX_TOKS = 4096

# ── Tool schemas ───────────────────────────────────────────────────────────────

TOOLS: List[Dict] = [
    {
        "name": "read_stored_document",
        "description": (
            "Fetch the stored content of a previously generated cascade document. "
            "Use this to retrieve the 'old' version so you can compare it against the "
            "updated content provided by the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The cascade session ID."},
                "node_id":    {"type": "string", "description": "Document node ID (e.g. 'rtm', 'config-workbook')."},
                "version":    {"type": "integer", "description": "Version number to fetch (1, 2, 3, ...)."},
            },
            "required": ["session_id", "node_id", "version"],
        },
    },
    {
        "name": "get_downstream_impact",
        "description": (
            "Get the list of all downstream documents impacted by a change in the specified node. "
            "Returns node metadata, current version, and impact classification for each. "
            "Call this AFTER identify_delta to map the delta to downstream impact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Cascade session ID for version lookups."},
                "node_id":    {"type": "string", "description": "The node that was changed."},
            },
            "required": ["session_id", "node_id"],
        },
    },
    {
        "name": "identify_delta",
        "description": (
            "Compare old document text against new document text and extract a structured "
            "list of changes (added, modified, removed sections/fields/requirements). "
            "For prompt-mode changes, pass the user's change description as new_text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "old_text":   {"type": "string", "description": "Previously generated document content."},
                "new_text":   {"type": "string", "description": "Updated document content or user change description."},
                "input_type": {
                    "type": "string",
                    "description": "How the new content was provided.",
                    "enum": ["full_doc", "delta_section", "prompt"],
                },
            },
            "required": ["old_text", "new_text", "input_type"],
        },
    },
]

SYSTEM_PROMPT = """You are a senior Salesforce project delivery expert specialising in document
change impact analysis for enterprise implementations.

Your mission: Analyse what changed between two versions of a project document, then
determine which downstream documents are affected and must be updated.

Mandatory process — follow exactly:
  Step 1  Call read_stored_document to retrieve the old version content.
  Step 2  Call identify_delta to produce a structured list of what changed.
  Step 3  Call get_downstream_impact to retrieve the downstream document list.
  Step 4  Return your final JSON analysis.

Quality standards:
- Be SPECIFIC about what changed. Vague items like "content updated" are rejected.
- Identify section names, field names, requirement IDs, interface names, data types.
- Each delta item must include the exact section/field and a precise description.
- For downstream nodes: explain specifically WHY each node is impacted by THIS delta.
- Do NOT list nodes that are not genuinely impacted by the specific delta items found.

Final answer format (return as pure JSON, no markdown):
{
  "delta_items": [
    {"type": "added|modified|removed", "section": "exact section or field", "description": "specific detail"}
  ],
  "affected_nodes": [
    {"node_id": "...", "reason": "specific reason this node is impacted by the delta above"}
  ],
  "delta_summary": "one paragraph summarising the overall change and its significance"
}"""


# ── Tool execution ─────────────────────────────────────────────────────────────

async def _execute_tool(
    tool_name: str,
    tool_input: Dict,
    session_id: str,
) -> str:

    if tool_name == "read_stored_document":
        node_id = tool_input.get("node_id", "")
        version = int(tool_input.get("version", 1))
        sid     = tool_input.get("session_id", session_id)

        from db import get_cascade_doc_by_node_version, get_document
        cascade_doc = await get_cascade_doc_by_node_version(sid, node_id, version)
        if not cascade_doc:
            return f"No stored document found for node='{node_id}' version={version} in session='{sid}'."
        base_doc = await get_document(cascade_doc["file_id"])
        if not base_doc:
            return f"File record missing for file_id='{cascade_doc['file_id']}'."
        return (
            f"Node: {node_id}  |  Version: v{version}\n"
            f"File: {base_doc['file_name']}  |  Format: {base_doc['output_format']}\n"
            f"Generated: {base_doc['created_at']}\n\n"
            f"Content:\n{(base_doc.get('content_summary') or '')[:6000]}"
        )

    if tool_name == "get_downstream_impact":
        node_id = tool_input.get("node_id", "")
        sid     = tool_input.get("session_id", session_id)

        from db import get_cascade_doc_current_version, is_cascade_doc_finalised

        downstream = get_downstream_nodes(node_id)
        result = []
        for nid in downstream:
            node   = get_node(nid)
            if not node:
                continue
            impact       = get_change_impact(nid)
            current_ver  = await get_cascade_doc_current_version(sid, nid)
            is_finalised = await is_cascade_doc_finalised(sid, nid)
            result.append({
                "node_id":         nid,
                "label":           node["label"],
                "phase":           node["phase"],
                "tier":            node["tier"],
                "owner":           node["owner"],
                "current_version": current_ver,
                "next_version":    current_ver + 1,
                "is_finalised":    is_finalised,
                "impact_type":     impact.get("impactType", "Review"),
                "governance":      impact.get("governance", ""),
                "notes":           impact.get("notes", ""),
            })
        return json.dumps(result, indent=2) if result else "No downstream documents found."

    if tool_name == "identify_delta":
        old_text   = tool_input.get("old_text", "")
        new_text   = tool_input.get("new_text", "")
        input_type = tool_input.get("input_type", "full_doc")

        if input_type == "prompt":
            return json.dumps([{
                "type": "modified",
                "section": "User-specified change",
                "description": new_text[:800],
                "raw_diff": None,
            }])

        # Produce a unified diff for Claude to interpret
        old_lines = old_text.splitlines()[:300]
        new_lines = new_text.splitlines()[:300]
        diff_lines = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile="old_version",
            tofile="new_version",
            lineterm="",
            n=3,
        ))
        diff_text = "\n".join(diff_lines[:150])

        if not diff_text.strip():
            return json.dumps([{
                "type": "modified",
                "section": "General",
                "description": "Documents appear identical or diff is minimal — review manually.",
            }])

        return (
            "Unified diff output (interpret this to produce structured delta items):\n\n"
            + diff_text
        )

    return f"Unknown tool: {tool_name}"


# ── Public entry point ─────────────────────────────────────────────────────────

class DeltaAnalysisResult:
    __slots__ = ("delta_items", "affected_nodes", "delta_summary", "raw_response")

    def __init__(
        self,
        delta_items: List[DeltaItem],
        affected_nodes: List[AffectedNode],
        delta_summary: str,
        raw_response: str = "",
    ) -> None:
        self.delta_items    = delta_items
        self.affected_nodes = affected_nodes
        self.delta_summary  = delta_summary
        self.raw_response   = raw_response


async def analyse_delta(
    session_id: str,
    node_id: str,
    from_version: int,
    new_content: str,
    input_type: str,            # "full_doc" | "delta_section" | "prompt"
) -> DeltaAnalysisResult:
    """
    Run Agent 4: Delta Analysis Agent.

    Args:
        session_id:   Active cascade session
        node_id:      The document that changed (e.g. 'config-workbook')
        from_version: Which stored version to compare against
        new_content:  New document text, delta section text, or change description
        input_type:   "full_doc" | "delta_section" | "prompt"

    Returns:
        DeltaAnalysisResult with delta_items, affected_nodes, and delta_summary
    """
    source_node = get_node(node_id)
    node_label  = source_node["label"] if source_node else node_id

    user_content = (
        f"Analyse the change in document '{node_label}' (node_id='{node_id}', "
        f"session='{session_id}').\n\n"
        f"Compare version {from_version} (stored in the system) against the "
        f"new content provided below.\n"
        f"Input type: {input_type}\n\n"
        f"New content / change description:\n{new_content[:5000]}\n\n"
        f"Follow the mandatory process: "
        f"1) read_stored_document v{from_version}, "
        f"2) identify_delta, "
        f"3) get_downstream_impact, "
        f"4) return final JSON."
    )

    print(f"\n🔍 Delta Agent | node={node_id} | v{from_version} | type={input_type}")

    async def _run_tool(name: str, args: Dict) -> str:
        return await _execute_tool(name, args, session_id)

    tool_specs = [
        ToolSpec(t["name"], t["description"], t["input_schema"],
                 (lambda args, _n=t["name"]: _run_tool(_n, args)))
        for t in TOOLS
    ]

    raw_final = await run_tool_agent(
        prompt=user_content,
        tools=tool_specs,
        system=SYSTEM_PROMPT,
        model=MODEL,
        max_turns=MAX_ITER,
    )

    print(f"   → Delta Agent done")

    # ── Parse final JSON response ──────────────────────────────────────────
    delta_items:    List[DeltaItem]    = []
    affected_nodes: List[AffectedNode] = []
    delta_summary   = ""

    try:
        raw = raw_final.strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(raw[start:end])
            delta_items    = parsed.get("delta_items", [])
            delta_summary  = parsed.get("delta_summary", "")

            # Build AffectedNode objects from agent's affected_nodes list
            from db import get_cascade_doc_current_version, is_cascade_doc_finalised
            for item in parsed.get("affected_nodes", []):
                nid      = item.get("node_id", "")
                node_meta = get_node(nid)
                if not node_meta:
                    continue
                cur_ver  = await get_cascade_doc_current_version(session_id, nid)
                is_final = await is_cascade_doc_finalised(session_id, nid)
                impact   = get_change_impact(nid)
                affected_nodes.append(AffectedNode(
                    node_id=nid,
                    label=node_meta["label"],
                    phase=node_meta["phase"],
                    tier=node_meta["tier"],
                    current_version=cur_ver,
                    next_version=cur_ver + 1,
                    is_finalised=is_final,
                    impact_type=impact.get("impactType", "Review"),
                    impact_reason=item.get("reason", ""),
                ))
    except Exception as parse_err:
        print(f"   ⚠ Delta parse error: {parse_err}")
        delta_summary = f"Delta analysis completed. Raw: {raw_final[:300]}"

    return DeltaAnalysisResult(
        delta_items=delta_items,
        affected_nodes=affected_nodes,
        delta_summary=delta_summary,
        raw_response=raw_final,
    )
