"""
jsonutil.py — ONE JSON-fence stripper for the whole quality package.

v1 BUG: _strip() was redefined independently in stage_rework.py and reviewer_base.py,
and neither matched cascade_agent.py's own _clean_json() — which additionally strips
trailing commas before } or ].  Three slightly different parsers on the same model
output is the kind of drift that produces an intermittent parse failure nobody expects.

This module is a faithful copy of cascade_agent._clean_json plus an array-aware variant
for reviewer payloads.  If cascade_agent._clean_json ever changes, change it here too —
or better, import it (see clean_json's docstring).
"""

import json
import re
from typing import Any, Optional


def clean_json(raw: str) -> str:
    """
    Byte-for-byte the same behaviour as cascade_agent._clean_json.

    If you prefer a single definition, delete this body and do:
        from cascade_agent import _clean_json as clean_json
    It is duplicated here only so the quality package can be imported and unit-tested
    without pulling in cascade_agent's LangGraph import chain.
    """
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```\s*$", "", s)
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        s = s[start:end + 1]
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def loads_or_none(raw: str) -> Optional[Any]:
    """Parse model output as JSON, returning None instead of raising."""
    try:
        return json.loads(clean_json(raw))
    except Exception:
        return None


def truncate(text: Any, limit: int) -> str:
    """Safe slice for prompt embedding — never raises on None/non-str."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    return s[:limit]
