"""
checkpoint.py — stage-level durability with keys that cannot collide.

v1 BUG (data corruption, listed in its own docs as a strength):
    pipeline.py called  _cached("research", ...)  and  _cached("plan", ...)
    with bare stage names.  CHECKPOINT_KEYS — the only place session/node/version
    namespacing existed — was imported nowhere and referenced nowhere.  With
    checkpoint_fns wired, every document in a cascade shared the key "research",
    so document 3's research pack would be handed to document 17.

v2: the key is built in exactly one function, and that function REQUIRES session,
node and version.  There is no code path that can produce an unqualified key.

Backend is pluggable.  Pass nothing and you get an in-process dict (survives the
document, resets with the server — same lifetime as cascade_agent's MemorySaver).
Pass a Store with async get/set and it persists (e.g. backed by db.py).
"""

import os
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, Optional

from .config import CHECKPOINT_KEY_FMT, CASCADE_KEY_FMT, CHECKPOINT_STAGES

# In-process fallback store. Keyed by the fully-qualified key, so it is safe.
#
# BOUNDED, because this is a long-running server. A 31-document cascade writes a
# research pack, a plan and a findings blob per document — around 90 entries of up to
# ~20K chars each, so roughly 2 MB per cascade — and nothing ever removed them. Every
# cascade a user ran added another couple of megabytes for the life of the process.
#
# LRU by insertion order: the entries that matter are the ones from the cascade
# currently running, and those are by definition the most recently written. Eviction
# is not a correctness risk — a missing checkpoint simply means the stage recomputes.
_MEM_MAX_ENTRIES = int(os.environ.get("PROJECTZEN_CHECKPOINT_MAX_ENTRIES") or 400)
_MEM_MAX_BYTES = int(os.environ.get("PROJECTZEN_CHECKPOINT_MAX_BYTES") or 32_000_000)

_MEM: "OrderedDict[str, str]" = OrderedDict()
_MEM_BYTES = 0


def _mem_get(key: str) -> Optional[str]:
    if key not in _MEM:
        return None
    _MEM.move_to_end(key)              # mark as recently used
    return _MEM[key]


def _mem_set(key: str, value: str) -> None:
    global _MEM_BYTES
    value = value or ""
    if key in _MEM:
        _MEM_BYTES -= len(_MEM.pop(key))
    _MEM[key] = value
    _MEM_BYTES += len(value)
    while _MEM and (len(_MEM) > _MEM_MAX_ENTRIES or _MEM_BYTES > _MEM_MAX_BYTES):
        _, evicted = _MEM.popitem(last=False)     # oldest first
        _MEM_BYTES -= len(evicted)


def mem_stats() -> Dict[str, int]:
    """For diagnostics: how much the in-process checkpoint store is holding."""
    return {"entries": len(_MEM), "bytes": _MEM_BYTES,
            "max_entries": _MEM_MAX_ENTRIES, "max_bytes": _MEM_MAX_BYTES}


def doc_key(session_id: str, node_id: str, version: int, stage: str) -> str:
    """Fully-qualified per-document stage key. All four parts are mandatory."""
    if not session_id or not node_id or stage is None:
        raise ValueError("checkpoint key needs session_id, node_id and stage")
    return CHECKPOINT_KEY_FMT.format(
        session=session_id, node=node_id, ver=int(version), stage=stage
    )


def cascade_key(session_id: str, stage: str) -> str:
    """Cascade-scoped key (shared across all documents in the session, e.g. glossary)."""
    if not session_id or stage is None:
        raise ValueError("cascade key needs session_id and stage")
    return CASCADE_KEY_FMT.format(session=session_id, stage=stage)


class Store:
    """
    Thin async wrapper.  `fns` is an optional {"get": async fn(key)->str|None,
    "set": async fn(key, value)->None}.  Without it, the in-process dict is used.
    """

    __slots__ = ("_get", "_set", "enabled")

    def __init__(self, fns: Optional[Dict[str, Callable[..., Awaitable[Any]]]] = None,
                 enabled: bool = CHECKPOINT_STAGES) -> None:
        self.enabled = bool(enabled)
        self._get = (fns or {}).get("get")
        self._set = (fns or {}).get("set")

    async def get(self, key: str) -> Optional[str]:
        if not self.enabled:
            return None
        try:
            if self._get is not None:
                return await self._get(key)
            return _mem_get(key)
        except Exception:
            return None    # a broken checkpoint store must never fail a generation

    async def set(self, key: str, value: str) -> None:
        if not self.enabled or not value:
            return
        try:
            if self._set is not None:
                await self._set(key, value)
            else:
                _mem_set(key, value)
        except Exception:
            pass

    async def cached(self, key: str, producer: Callable[[], Awaitable[str]]) -> str:
        """Return the checkpointed value if present, else run producer and store it."""
        hit = await self.get(key)
        if hit:
            return hit
        val = await producer()
        if val:
            await self.set(key, val)
        return val
