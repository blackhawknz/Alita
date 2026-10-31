---
name: code-change
description: Safe workflow for implementing features or fixing bugs in a codebase (explore, plan, edit, verify). Use for any non-trivial coding task in the workspace.
tags: [coding, workflow]
---

# Making code changes

## Steps
1. **Explore**: list the project (`file_ops` list/search), read the README, the entry points and the files involved.
   Find existing conventions (style, test framework, how similar things are done).
2. **Plan**: for more than a couple of edits, write a `todo` list. Note the exact files and functions to change.
3. **Edit**: make focused changes with `file_ops` `patch` (exact, unique `old_string`). Read the file back if unsure.
   Match surrounding style. Don't leave TODOs or dead code.
4. **Verify**: run the project's tests, linter or type-checker with `terminal`. Reproduce the bug and confirm it's fixed.
   If something fails, read the error carefully and fix the root cause.
5. **Report**: summarise what changed (files and why), how it was verified, and any follow-ups.

## Pitfalls
- A patch that matches 0 or multiple places fails. Include more surrounding context.
- Don't run destructive commands (rm -rf, git reset --hard, force push) without explicit user approval.
- On Windows the terminal is PowerShell: use `;` instead of `&&` and `Get-ChildItem` instead of `ls -la`.
- Checkpoints let you roll back `file_ops` edits (`rollback` with the returned checkpoint_id).
