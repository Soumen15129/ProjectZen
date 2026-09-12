"""
patch.py — a minimal RFC 6902 JSON Patch applier, with no third-party dependency.

Used by the rework stage so a correction round returns a handful of operations
instead of re-emitting the entire document. Only the three operations a document
correction actually needs are supported:

    {"op": "replace", "path": "/sections/3/paragraphs/0", "value": "..."}
    {"op": "add",     "path": "/sheets/2/rows/-",         "value": ["a", "b"]}
    {"op": "remove",  "path": "/sections/5"}

Deliberately NOT supported: move, copy, test. They add failure modes without
serving any correction the reviewers actually raise.

Design note — ops are applied INDIVIDUALLY and independently. A model that gets one
path wrong should not cost the whole round; that op is skipped and reported, and the
caller decides whether enough succeeded to accept the result.
"""

import copy
from typing import Any, Dict, List, Tuple


def _unescape(token: str) -> str:
    # RFC 6901: ~1 is "/", ~0 is "~", and the order matters.
    return token.replace("~1", "/").replace("~0", "~")


def _resolve(doc: Any, tokens: List[str]) -> Tuple[Any, Any]:
    """Walk to the CONTAINER of the final token. Returns (container, key)."""
    node = doc
    for tok in tokens[:-1]:
        tok = _unescape(tok)
        if isinstance(node, list):
            node = node[int(tok)]                 # IndexError/ValueError -> caller
        elif isinstance(node, dict):
            node = node[tok]                      # KeyError -> caller
        else:
            raise KeyError(f"cannot descend into {type(node).__name__} at '{tok}'")
    last = _unescape(tokens[-1])
    return node, last


def apply_op(doc: Any, op: Dict[str, Any]) -> None:
    """Apply one operation IN PLACE. Raises on any malformed path or index."""
    kind = op.get("op")
    path = op.get("path", "")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"bad path {path!r}")

    tokens = path.split("/")[1:]
    if not tokens:
        raise ValueError("cannot operate on the document root")

    container, key = _resolve(doc, tokens)

    if isinstance(container, list):
        if kind == "add":
            # "-" means append, per RFC 6901.
            container.append(op["value"]) if key == "-" else container.insert(int(key), op["value"])
            return
        idx = int(key)
        if kind == "replace":
            container[idx] = op["value"]          # IndexError if out of range
        elif kind == "remove":
            del container[idx]
        else:
            raise ValueError(f"unsupported op {kind!r}")
        return

    if isinstance(container, dict):
        if kind == "replace":
            if key not in container:
                raise KeyError(f"no such key {key!r} to replace")
            container[key] = op["value"]
        elif kind == "add":
            container[key] = op["value"]
        elif kind == "remove":
            del container[key]
        else:
            raise ValueError(f"unsupported op {kind!r}")
        return

    raise KeyError(f"path {path!r} does not address a container")


def apply_patch(doc: Any, ops: List[Dict[str, Any]]) -> Tuple[Any, int, List[str]]:
    """
    Apply `ops` to a DEEP COPY of doc. Returns (result, applied_count, failures).

    Removals are applied last, in descending index order. Without that, removing
    /sections/2 first would shift every later index and silently corrupt the
    remaining operations — the classic JSON Patch footgun.
    """
    result = copy.deepcopy(doc)
    failures: List[str] = []
    applied = 0

    removes = [o for o in ops if o.get("op") == "remove"]
    others = [o for o in ops if o.get("op") != "remove"]
    removes.sort(key=lambda o: _sort_key(o.get("path", "")), reverse=True)

    for op in others + removes:
        try:
            apply_op(result, op)
            applied += 1
        except Exception as e:
            failures.append(f"{op.get('op')} {op.get('path')}: {type(e).__name__} {e}")
    return result, applied, failures


def _sort_key(path: str) -> List[int]:
    """Numeric path components, so /sections/10 sorts after /sections/9."""
    return [int(t) if t.isdigit() else -1 for t in path.split("/")[1:]]


def outline(doc: Any, max_value: int = 90, max_rows: int = 400) -> str:
    """
    A compact, addressable map of the document: every JSON Pointer the model may
    target, with its current value abbreviated.

    This is what makes patching reliable. Asked to patch without it, the model
    guesses at paths and the ops miss; given the real pointers, it addresses them.
    """
    lines: List[str] = []

    def walk(node: Any, path: str) -> None:
        if len(lines) >= max_rows:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            # Long homogeneous arrays (table rows) get summarised, not enumerated.
            if len(node) > 12 and all(isinstance(x, (list, str, int, float)) for x in node):
                lines.append(f"{path}  [{len(node)} items, indices 0-{len(node)-1}]")
                for i in (0, len(node) - 1):
                    lines.append(f"{path}/{i} = {_abbrev(node[i], max_value)}")
                return
            for i, v in enumerate(node):
                walk(v, f"{path}/{i}")
        else:
            lines.append(f"{path} = {_abbrev(node, max_value)}")

    walk(doc, "")
    return "\n".join(lines[:max_rows])


def _abbrev(v: Any, n: int) -> str:
    s = repr(v)
    return s if len(s) <= n else s[: n - 3] + "..."
