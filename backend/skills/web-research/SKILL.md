---
name: web-research
description: Systematic web research with source verification and citations. Use for questions needing current information, comparisons, fact-checking, or "find out about X" requests.
tags: [research, web]
---

# Web research

## Steps
1. Restate the question and what a complete answer needs (facts, numbers, dates, comparisons).
2. Run 2-4 targeted `browser` searches (action `search`) with different phrasings; include the year for recent topics.
3. Open the most authoritative results with `browser` action `extract_text` (official docs, primary sources,
   reputable outlets). For broad topics, run independent sub-questions in parallel with `delegate_task`.
4. Cross-check key claims across at least two sources; note disagreements and publication dates.
5. Answer with the conclusion first, then supporting detail. Cite sources inline as markdown links.

## Pitfalls
- Search snippets can be stale or wrong. Read the page before quoting numbers.
- Prefer primary sources over aggregators. Say when information may be outdated.
- If a page fails to load, try another source rather than retrying the same URL.

## Verification
- Every non-obvious claim has a source link.
- Dates and versions are explicit.
