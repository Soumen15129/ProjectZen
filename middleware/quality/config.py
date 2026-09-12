"""
config.py — all quality-pipeline tunables in one place.

DIFFERENCES FROM v1 (and why):
  * No MAX_TOKS_* constants.  llm_client.complete() drives the `claude` CLI via the
    Claude Agent SDK; ClaudeAgentOptions exposes no token ceiling at that layer.
    The v1 MAX_TOKS_RESEARCH / MAX_TOKS_PLAN / MAX_TOKS_REVIEW / MAX_TOKS_GEN family
    modelled a parameter that no longer exists anywhere in ProjectZen.
  * Depth is scoped BY TIER instead of "full pipeline for everything".  The graph is
    31 nodes in 16 sequential waves; full depth on all of them takes total LLM calls
    from ~32 to ~218-683 and the sequential critical path from ~17 to ~64-160.
    That is a 4-10x wall-clock increase on a Claude seat.  Tier scoping keeps maximum
    depth exactly where document quality actually matters (T1/T2 = 10 of 31 nodes)
    and keeps a cascade finishing in a usable amount of time.
    Set FORCE_DEPTH = "full" to override and run everything at maximum depth.
"""

# ── Models (mirror cascade_agent.py) ──────────────────────────────────────────
OPUS_MODEL   = "claude-opus-4-8"
SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL  = "claude-haiku-4-5-20251001"


# ── Context budgets (chars) ───────────────────────────────────────────────────
# Every stage previously truncated its inputs to 2,000-8,000 chars. Measured on a
# real run, that meant the research and plan stages saw 9.6% of the reference
# template and ~3% of the source document, then confidently planned a structure
# from what little they had — and the generator faithfully executed that plan.
#
# CTX_DRAFT is the one to watch: the reviewers and the rework stage were seeing
# only 8,000 / 12,000 chars of the draft. Rework is instructed to "preserve every
# other byte exactly" — impossible for content it was never shown, so a large
# document could silently lose everything past the cut.
CTX_GROUNDING = 40000   # reference template shown to research / plan
CTX_SOURCE    = 40000   # source + parent documents
CTX_PROJECT   = 20000   # extracted project-context JSON
CTX_RESEARCH  = 20000   # research pack passed downstream
CTX_PLAN      = 20000   # the plan passed downstream
CTX_DRAFT     = 120000  # the draft shown to reviewers and to rework
CTX_GLOSSARY  = 8000    # terminology contract injected into prompts
CTX_SCHEMA    = 4000    # the JSON schema block


# ── Depth profiles ────────────────────────────────────────────────────────────
# Each profile decides which stages run for a document.
#   research   — build the per-document research pack
#   plan       — build the section plan + traceability map
#   reviewers  — which LLM reviewers run (structure is ALWAYS on and is free)
#   rework     — max rework rounds
#
# "structure" is deliberately absent from every reviewers list: ProjectZen's artifacts
# are fixed-schema JSON, so structure is validated by a pure-Python function against
# _FORMAT_SCHEMAS — deterministic, instant, zero LLM calls.  v1 spent a Sonnet call
# on the one check that provably does not need a model.
DEPTH_PROFILES = {
    "full": {
        "research":  True,
        "plan":      True,
        "reviewers": ["accuracy", "alignment", "consistency"],
        "rework":    3,
    },
    "standard": {
        "research":  True,
        "plan":      True,
        "reviewers": ["accuracy", "alignment"],
        "rework":    2,
    },
    "light": {
        "research":  False,     # cascade glossary still applies — see glossary.py
        "plan":      True,
        "reviewers": ["alignment"],
        "rework":    1,
    },
    "minimal": {
        "research":  False,
        "plan":      False,
        "reviewers": [],        # structure validation only (free)
        "rework":    1,         # rework can still fire on structure findings
    },
}

# Tier -> depth profile.  T1 = client-critical hub documents (Project Plan, RTM,
# Solution Design, SOW) — 4 nodes.  T2 = 6 nodes.  T3 = 13.  T4 = 8.
# Tuned for a Claude **Pro** seat, not Enterprise (the profile this package was
# originally written for). Two measurements drove this:
#   * a T1 document at "full" depth took 17.0 minutes end to end;
#   * ~28 seconds of EVERY call is fixed overhead — the Agent SDK spawns a fresh
#     `claude` CLI subprocess per call, so 7-18 calls per document costs 3-8 minutes
#     before any reasoning happens.
# Pro also has a 5-hour rolling limit that ~100-220 Opus calls will exhaust.
#
# Raise these once Session B removes the per-call spawn overhead.
DEPTH_BY_TIER = {
    "T1": "standard",   # was "full"
    "T2": "light",      # was "standard"
    "T3": "minimal",    # was "light"
    "T4": "minimal",
}

# Set to a profile name ("full" / "standard" / "light" / "minimal") to override tier
# routing entirely.  None = use DEPTH_BY_TIER.
FORCE_DEPTH = None


# ── Delta path ────────────────────────────────────────────────────────────────
# v1 skipped the pipeline entirely for delta updates.  Because node_apply_delta_wave
# passes the same delta_items to EVERY node in the wave, that meant no delta document
# ever saw any review — on precisely the path that propagates a real, business-driven
# change to the highest-blast-radius documents.
#
# v2 gives delta a genuine but cheap safety net: no re-research, no re-plan (correct —
# do not re-research a whole document to change three fields), but structure validation
# (free) plus one alignment reviewer scoped to "did the requested delta actually land,
# and was anything outside the delta disturbed?".
DELTA_SKIP_RESEARCH = True
DELTA_SKIP_PLAN     = True
DELTA_REVIEWERS     = ["delta_alignment"]
DELTA_REWORK_ROUNDS = 1


# ── Model per stage ───────────────────────────────────────────────────────────
STAGE_MODELS = {
    "glossary": OPUS_MODEL,     # once per cascade — cheap at cascade scale, high leverage
    "research": OPUS_MODEL,
    "plan":     OPUS_MODEL,
    "rework":   OPUS_MODEL,
    # "generate" is NOT here — it keeps get_model_for_node() tier routing, unchanged.
}

# All reviewers on Sonnet for Pro. Reviewing is structured judgement against material
# it is handed, which Sonnet does well; Opus is better spent on the stages that
# actually author content (research, plan, generate, rework). This roughly halves the
# cost of every review round, and review rounds are the most repeated call in the
# pipeline. Move accuracy back to OPUS_MODEL if bug-finding depth proves lacking.
REVIEWER_MODELS = {
    "accuracy":        SONNET_MODEL,
    "alignment":       SONNET_MODEL,
    "consistency":     SONNET_MODEL,
    "delta_alignment": SONNET_MODEL,
}


# ── Rework loop ───────────────────────────────────────────────────────────────
REWORK_ON_SEVERITIES = {"CRITICAL", "MAJOR"}   # MINOR / NIT are reported, not reworked
MAX_ATTEMPTS         = 3                       # mirrors cascade_agent's retry count


# ── Checkpointing ─────────────────────────────────────────────────────────────
# v1 BUG: pipeline.py called _cached("research") with a bare stage name while
# CHECKPOINT_KEYS (the only place the namespacing existed) was imported nowhere.
# Every document in a session shared the key "research" — document 3's research pack
# would be served to document 17.  v2 builds the key in one place (checkpoint.py)
# and there is no way to call it without session + node + version.
CHECKPOINT_STAGES = True
CHECKPOINT_KEY_FMT = "qp:{session}:{node}:v{ver}:{stage}"
CASCADE_KEY_FMT    = "qp:{session}:_cascade:{stage}"


# ── Cascade-level shared glossary ─────────────────────────────────────────────
# The single highest-value ProjectZen-specific addition, and the one thing the
# Oracle/Sequential-Specialists design cannot have (it only ever produces ONE artifact).
# ProjectZen's hardest quality problem is 31 independently generated documents drifting
# from each other's terminology.  A per-document glossary cannot fix that — it makes
# each document internally consistent and mutually inconsistent.  This glossary is
# produced ONCE from the extracted project context and injected into every document's
# research, generation, and consistency review.
CASCADE_GLOSSARY = True


# ── SSE events ────────────────────────────────────────────────────────────────
# All additive.  If the frontend ignores them nothing breaks — content_chunk still
# drives the typewriter, and (unlike v1) it now also fires during rework.
SSE = {
    "glossary_start": "qp_glossary_start",
    "glossary_done":  "qp_glossary_done",
    "research_start": "qp_research_start",
    "research_done":  "qp_research_done",
    "plan_start":     "qp_plan_start",
    "plan_done":      "qp_plan_done",
    "review_start":   "qp_review_start",
    "review_done":    "qp_review_done",
    "rework_start":   "qp_rework_start",
    "rework_done":    "qp_rework_done",
    "quality_pass":   "qp_quality_pass",
    "depth":          "qp_depth",
}


def profile_for(node: dict, is_delta: bool = False) -> dict:
    """Resolve the depth profile for a node.  Never raises."""
    if is_delta:
        return {
            "research":  not DELTA_SKIP_RESEARCH,
            "plan":      not DELTA_SKIP_PLAN,
            "reviewers": list(DELTA_REVIEWERS),
            "rework":    DELTA_REWORK_ROUNDS,
            "_name":     "delta",
        }
    name = FORCE_DEPTH or DEPTH_BY_TIER.get((node or {}).get("tier", "T3"), "light")
    prof = dict(DEPTH_PROFILES.get(name, DEPTH_PROFILES["light"]))
    prof["_name"] = name
    return prof
