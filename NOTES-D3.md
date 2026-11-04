# D3 implementation notes

Small judgement calls made while implementing `OpenCodeExecutor`.

## `start()` vs. the session-ID handle

The brief says to capture `sessionID` from the first event and "store as the
handle", but `start()` must return before the run is finished. I made `start()`
spawn the process and wait briefly (up to `min(timeout, 10s)`) for the first
event that carries a `sessionID`, then return that ID. Events are read by a
background reader into a queue, so nothing is lost: the first `send()` drains
that queue and streams the rest of the initial run. If no session ID ever
appears (e.g. the process dies first), `start()` returns a provisional
`oc-<uuid>` handle; the reader adds the real session ID as an alias if it
shows up later.

## Integration test is double-gated

The brief asks for an "opt-in" integration test but also only requires
`@pytest.mark.skipif(not shutil.which("opencode"))`. Because `opencode` *is*
installed on this machine, that condition alone would spend real money on
every `pytest` run. The test is therefore skipped unless **both**
`shutil.which("opencode")` is truthy **and** `ALITA_OPENCODE_INTEGRATION=1` is
set. This is the conservative choice; no other test launches the real CLI.

## Windows wrappers

The resolver wraps `.ps1` with `powershell -NoProfile -NonInteractive -File`
and `.cmd`/`.bat` with `%COMSPEC% /c`. It also supports `.py` by invoking the
current interpreter, which keeps the fake-binary tests simple and portable.
The fake in `tests/test_open_code_executor.py` is a Python script behind a
`.cmd` shim on Windows (or a shell shim elsewhere).

## Timeout scope

`timeout` is interpreted as the wall clock for one `send()` call. On expiry
the process tree is killed (`taskkill /F /T /PID` on Windows), an `error`
event says `timed out after Ns`, and a final `done` event follows, per the
brief.

## Cost after `close()`

`close()` removes the handle, so `handle_cost(handle)` returns `0.0`
afterwards. Callers that need the cost should read it before closing, which
is what D4's tool surface will do.
