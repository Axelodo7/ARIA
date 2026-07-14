# Business memory — ZentraIQ

Timestamped facts about the business that change over time (milestones, launched
features, policy changes). ARIA appends here when you say "remember ..." — Python
writes the date and formatting; the model supplies only the fact text.

Loaded newest-first (capped at 400 tokens) into every agent's `<business_context>`
as "Recent developments you should know about." Prune stale facts by hand — run
`python scripts/prune_memory.py` to see what's loaded, then delete lines here.

<!-- Facts below, one per line, exactly: "- YYYY-MM-DD: <fact>." -->
