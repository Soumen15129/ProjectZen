# Smoke tests

Run from the `middleware/` directory (so `llm_client` is importable), or with the
stub in this folder:

    python3 tests/test_smoke.py

Covers:
1. Structure validator catches missing fields, empty containers, volume shortfalls
   and placeholder text — with zero LLM calls.
1b. Optional/alternative fields (`bullets`, `table`, `subtitle`) do NOT produce
   blocking findings. This test exists because the first version of the validator
   flagged an empty `bullets` array as MAJOR, which would have sent nearly every
   document into an unnecessary rework loop.
2. Checkpoint keys are session+node+version qualified and an unqualified key raises.
3. A T1 document runs the full pipeline; the plan lands BEFORE `HARD RULES:`.
4. A T4 document consumes zero reviewer LLM calls.
5. A delta update still gets one reviewer (v1 gave delta updates none, ever).
