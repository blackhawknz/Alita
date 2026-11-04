# DONE — D3: `OpenCodeExecutor`

## What changed

- Added `backend/alita/executors/opencode.py` implementing the D3 executor:
  - Resolves `opencode` via `shutil.which` at construction and raises a clear
    `RuntimeError` when absent.
  - Handles Windows `.ps1` shims through `powershell -NoProfile
    -NonInteractive -File` and `.cmd`/`.bat` through `%COMSPEC% /c`.
  - Spawns `opencode run --dir <workdir> --format json [-m <model>] <task>`
    with an explicit argv list (no shell), reads NDJSON line by line, and maps
    `step_start`, `text`, `tool`/`tool_use`, and `step_finish` to
    `ExecutorEvent`s. Unknown event types are logged at DEBUG and ignored.
  - Uses the OpenCode `sessionID` as the handle and resumes it with
    `-s <sessionID>` on later `send()` calls.
  - Captures/logs stderr at WARNING and includes the last ~2000 chars in the
    error event on non-zero exit.
  - Enforces a per-call timeout; on expiry kills the process tree, emits
    `error` then `done`.
  - Supports `interrupt()` (tree kill, no hang) and `close()`.
  - Accumulates per-handle cost/tokens and exposes `handle_cost(handle)`.
- Exported `OpenCodeExecutor` lazily from `backend/alita/executors/__init__.py`.
- Added `backend/tests/test_open_code_executor.py` using a fake Python
  script/`.cmd` shim (canned NDJSON) for happy path, session-ID capture,
  `-s` resume, non-zero exit, timeout kill, interrupt, unknown handles,
  missing binary, and `.ps1` resolution. The real-CLI test is skipped unless
  `ALITA_OPENCODE_INTEGRATION=1` is set as well as `opencode` being present.
- Added `NOTES-D3.md` documenting the small judgement calls.

## Test counts

- Before: **532 passed**.
- After: **540 passed, 1 skipped** (8 new tests; the skip is the opt-in real
  OpenCode integration test, which would cost money).

## Deliberately not done

- No changes to `AlitaAgent` registration, config settings, tools, kanban, or
  `.env` — those belong to D4+.
- Did not run the real OpenCode CLI; only the fake-binary tests ran.
- Did not touch the untracked `opencode.json` user file.
