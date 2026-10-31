"""Step 3: relevance, matching and remote eligibility on top of the ingested jobs.

Layers, cheapest first (every layer is non-destructive and stores why):
    remote   - who a remote job is actually open to (this package: `remote`)
    gate     - deterministic role/skill/seniority/date rules       (phase B)
    embed    - embedding similarity against my two profiles       (phase C)
    score    - Gemini fit scoring for the shortlist, with a budget (phase D)
"""
