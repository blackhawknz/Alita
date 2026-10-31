---
name: skill-authoring
description: How to write or improve a reusable skill (SKILL.md). Use when saving a procedure as a skill, or when the user asks to create, edit or review a skill.
tags: [skills, meta]
---

# Writing good skills

A skill is procedural memory: instructions future-you will follow when a similar task comes up.

## When a skill is worth creating
- The task took several steps or some trial and error, and is likely to recur.
- There is non-obvious knowledge: exact commands, gotchas, ordering, verification steps.
- Not worth it: one-off questions, trivial lookups, anything fully covered by an existing skill (patch that instead).

## Structure
1. **Front-matter** — `name` (kebab-case) and a `description` that says *what it does* and *when to use it*.
   The description is all you see before loading the skill, so make the trigger obvious.
2. **When to use** — concrete situations and inputs.
3. **Steps** — numbered, imperative, generalised (use placeholders like `<project-dir>`, not one-off values).
4. **Pitfalls** — errors you hit and how you fixed them.
5. **Verification** — how to confirm success.

Keep it under ~60 lines. Put long scripts or templates in supporting files
(`skill_manage` action `write_file`, e.g. `scripts/build.py`) and reference them from the steps.

## Maintenance
- When following a skill fails or you find a better way, `patch` it right away.
- Prefer improving an existing skill over creating a near-duplicate.
