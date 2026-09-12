"""
refusal.py — one place that decides "is this a refusal, or just a bad answer?"

Every stage in this package fails open: on error it returns "" and the document
still generates. That is right for a model that produced unusable output, and
badly wrong for a REFUSAL. When an account hit its usage limit, fail-open turned
the limit notice into a 72-character research pack, the plan into another 72
characters, and the user got a bare "failed" with the real cause nowhere in sight.

A refusal must propagate so the run stops immediately with an accurate message,
rather than being retried three times and then hidden.

The converse matters just as much, and cost a run to learn: a TRANSIENT SERVER
ERROR is not a refusal. Propagating one skips the very retry-and-fall-back logic
that exists to absorb it, so a hiccup on any single call destroys the whole
document. "Refusal" here means the account or the request is the problem and
trying again cannot help — a usage limit, a failed sign-in. A stalled stream is
the opposite: trying again is exactly the right move.
"""

def _is_llm_unavailable(exc: BaseException) -> bool:
    """isinstance check without importing eagerly (the test stub transport does
    not define LlmUnavailable)."""
    try:
        from ..llm_client import LlmUnavailable
        return isinstance(exc, LlmUnavailable)
    except Exception:
        try:
            from llm_client import LlmUnavailable   # type: ignore
            return isinstance(exc, LlmUnavailable)
        except Exception:
            return type(exc).__name__ == "LlmUnavailable"


def is_refusal(exc: BaseException) -> bool:
    """
    True when `exc` is an LlmUnavailable the run CANNOT recover from.

    A TRANSIENT SERVER ERROR IS NOT A REFUSAL. Treating every LlmUnavailable as
    one cost a complete 50-minute run: a rework call stalled mid-stream, the
    stage's `except` saw "refusal" and re-raised, and that skipped both the
    retry loop AND the `return draft_json` line immediately below it — so a
    finished, usable draft was thrown away and the user got a 503 instead of a
    document. llm_client had already classified the error correctly:

        LlmUnavailable("server_error", ...).is_retryable  ->  True

    Nothing consulted it. This does.

    Retryable ('server_error')     -> False, so the caller retries and then
                                      falls back to its own safe default.
    Everything else ('rate_limit', -> True, so the run stops immediately with an
    'authentication_failed', ...)     accurate message, which is why this module
                                      exists.

    An object without `is_retryable` (a stub) is treated as a refusal, keeping
    the original conservative behaviour.
    """
    if not _is_llm_unavailable(exc):
        return False
    return not bool(getattr(exc, "is_retryable", False))
