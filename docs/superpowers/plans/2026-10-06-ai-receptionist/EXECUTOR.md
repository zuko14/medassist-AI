# EXECUTOR PROTOCOL — read this completely before touching anything

You are executing the Kriya AI Receptionist plan (`README.md` in this folder).
The design work is DONE and VERIFIED. Your job is to apply it exactly, prove each
step with the stated command, and record the proof in `STATUS.md`.

## The one rule

**Do not write or "improve" code from memory.** Every line of production code is
already in `payload.patch` (built and tested against commit `d263bb4`). You apply
it task by task with `git apply --include=...`. If something does not apply or a
test fails, you STOP, record it in STATUS.md as `BLOCKED`, and report. You never
"fix" by guessing.

## Why

This system takes real bookings, payments and medical-report requests for live
hospitals. Existing WhatsApp workflows serve paying clients and must not break.
Every file in the payload was tested against the real codebase, the real
migration chain on PostgreSQL, the tenant-isolation linters and the full
3,900-test suite. A plausible-looking hand edit is the most likely way to break it.

## Loop for EVERY task (no exceptions)

1. Read the task in README.md fully. Read STATUS.md. The task must be `TODO` and
   every task it depends on must be `DONE`.
2. Set it to `IN_PROGRESS` in STATUS.md (with date/time).
3. Run the task's **Apply** command exactly as written.
   - `git apply --check` first (the command includes it). Non-zero exit => `BLOCKED`.
4. Run the task's **Verify** commands exactly. Copy the final summary line of each
   (e.g. `14 passed in 16.0s`) into STATUS.md under that task.
5. Compare with the **Expected** values. Any difference (fewer passed, any new
   failed, an error) => `BLOCKED`. Do NOT edit tests to make them pass.
6. Run `git status --short` and confirm ONLY the task's listed files changed.
   Anything else changed => revert it (`git checkout -- <file>`) and note it.
7. Commit with the task's commit message. Record the commit hash in STATUS.md.
8. Set the task to `DONE`. Move to the next task.

## Stop conditions (set BLOCKED, write what you saw, stop and report)

- `git apply --check` fails.
- Any Verify count differs from Expected, or any test outside the known
  baseline failures (listed in STATUS.md) fails.
- A command asks for credentials, touches production, or needs network access
  you were not told to use.
- You are tempted to change a file the task does not list.

## Never

- Never run tests with `KRIYA_TEST_LIVE=1`.
- Never run migrations against production. Migration to production is a HUMAN gate.
- Never push to `main` or deploy. Never set `VOICE_ENABLED=true` anywhere but a
  local/staging `.env`.
- Never edit `payload.patch`, the tests it adds, or the linter ratchets.
- Never remove an existing `# unscoped:` annotation or tenant predicate.
- Never claim a gate is passed without the command output pasted in STATUS.md.

## Environment facts you need

- Windows; use the repo venv: `venv\Scripts\python.exe` (Python 3.12). Production
  runs Python **3.11** (Dockerfile) — Task 11 compiles everything under 3.11.
- `pytest` is hermetic (tests/conftest.py forces test credentials) EXCEPT that
  platform tests need `OWNER_USERNAME` / `OWNER_PASSWORD` set to any value.
  Always run with: `OWNER_USERNAME=owner OWNER_PASSWORD=pw` (PowerShell:
  `$env:OWNER_USERNAME='owner'; $env:OWNER_PASSWORD='pw'`).
- Migration tests use an embedded PostgreSQL (pgserver) — first run takes ~30 s.
- After any full-suite run, check for orphaned `python.exe` processes and kill them
  (see memory note "local app runs steal prod locks").
- The patch uses LF line endings; git's autocrlf handles it. If `git apply` reports
  whitespace errors only, retry with `--ignore-whitespace` and note it in STATUS.md.

## Reporting format (in STATUS.md, per task)

```
### T5 — DONE (2026-10-07 11:20)
Apply: OK
Verify: tests/voice ... -> "141 passed in 1.2s" (expected 141 passed)
git status: only listed files
Commit: abc1234
```
