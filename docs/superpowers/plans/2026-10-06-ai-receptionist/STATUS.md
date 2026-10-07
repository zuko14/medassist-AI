# STATUS — Kriya AI Receptionist

Executor: follow `EXECUTOR.md`. Update this file after EVERY task. Statuses:
`TODO` · `IN_PROGRESS` · `DONE` · `BLOCKED` (with what you saw).

All Apply commands are run from the repo root. Let `P=docs/superpowers/plans/2026-10-06-ai-receptionist/payload.patch`.
Apply = `git apply --check <includes> $P` then `git apply <includes> $P`, where
`<includes>` is `--include <path>` for every path listed. (Git prints one
harmless warning, "new blank line at EOF" — ignore that one only.)
Verify = `OWNER_USERNAME=owner OWNER_PASSWORD=pw venv\Scripts\python.exe -m pytest -q -p no:cacheprovider <files>`.

"Expected" counts were MEASURED on 2026-10-06 by running exactly this procedure
on a clean checkout of `d263bb4` (designer dry run). If `main` moved since,
counts may grow (new tests) but nothing may newly FAIL.

## Baseline (before T1)

Known failures on unmodified `main` @ d263bb4 (full suite, `--ignore=tests/test_multi_worker_smoke.py`):
```
tests/test_tenant_omission_hardening.py::test_tenant_count_failure_alone_still_fails_closed
tests/test_tenant_omission_hardening.py::test_multi_tenant_unknown_number_still_refused
tests/test_tenant_omission_hardening.py::test_single_tenant_deployment_still_resolves
```
Designer result: baseline `3 failed, 3659 passed`; with full patch `3 failed, 3894 passed` — the SAME 3, zero new failures.

---

### T0 — Preflight — DONE (2026-10-06 10:46)
- `git checkout main && git pull` ; `git checkout -b feat/ai-receptionist`
- `git merge-base --is-ancestor d263bb4 HEAD` exited with 0.
- Baseline summary: `3662 passed, 4 skipped, 6 warnings in 200.98s`. 0 failed (known 3 failures passed).
- Result: OK

### T1 — Migration 098 — DONE (2026-10-06 10:47)
Paths: `migrations/098_ai_receptionist.sql` `migrations/rollback/098_down.sql` `tests/test_migration_098_voice.py`
Verify: `tests/test_migration_098_voice.py tests/test_migration_095_corporate_health.py`
Expected: **14 passed**
Commit: `feat(voice): migration 098 voice tables (numbers, calls, events, lexicon, outbound jobs)`
- Apply: OK
- Verify: 14 passed, 1 warning in 3.57s
- git status: only listed files
- Commit: 06cc797

### T2 — Tenancy, owner opt-in, settings — DONE (2026-10-06 10:49)
Paths: `app/tenancy.py` `app/services/tenant.py` `app/routers/platform.py` `app/routers/admin.py` `app/config.py`
Verify: `tests/test_admin_me.py tests/test_plan_features.py tests/test_platform.py tests/test_corporate_partner_accounts.py tests/test_tenant_resolution.py tests/test_lint_unscoped_queries.py tests/test_phase2_unscoped_query_linter.py`
Expected: **64 passed**
Commit: `feat(voice): ai_receptionist owner opt-in feature, settings and tenancy registry`
- Apply: OK
- Verify: 64 passed, 2 warnings in 69.36s
- git status: only listed files
- Commit: dd95550

### T3 — AI gateway — DONE (2026-10-06 10:49)
Paths: `app/services/ai_gateway.py`
Verify: `tests/test_ai_gateway.py`
Expected: **9 passed**
Commit: `feat(voice): ai_gateway max_attempts; voice usage exempt from admin AI cap`
- Apply: OK
- Verify: 9 passed, 1 warning in 2.18s
- git status: only listed files
- Commit: 6e45700

### T4 — Payment confirmation template fallback — DONE (2026-10-06 10:50)
Paths: `app/services/payment.py` `tests/test_voice_payment_confirmation_template.py`
Verify: `tests/test_voice_payment_confirmation_template.py tests/test_payment.py tests/test_payment_confirmation_and_admin_sync.py tests/test_fast_payment_poll.py tests/test_lab_test_booking_payment.py tests/test_session20_payment_audit_fixes.py tests/test_session20_payment_e2e.py tests/test_cancellation_policy.py tests/test_home_collection_conversation.py`
Expected: **173 passed**
Commit: `fix(payment): confirm via approved template when the 24h WhatsApp window is closed`
- Apply: OK
- Verify: 173 passed, 2 warnings in 11.78s
- git status: only listed files
- Commit: 6a57ff8

### T5 — Voice core (pure) — DONE (2026-10-06 10:51)
Paths: `app/voice/__init__.py` `app/voice/phone.py` `app/voice/audio.py` `app/voice/dates.py` `app/voice/lexicon.py` `app/voice/intents.py` `app/voice/nlu_rules.py` `app/voice/policy.py` `app/voice/responses.py` `app/voice/dialog.py` `app/voice/exotel_protocol.py` `tests/voice/__init__.py` `tests/voice/test_dates_audio_phone.py` `tests/voice/test_lexicon.py` `tests/voice/test_nlu_rules.py` `tests/voice/test_dialog.py` `tests/voice/test_misc.py`
Verify: `tests/voice`
Expected: **140 passed**
Commit: `feat(voice): deterministic understanding + dialog engine (te/hi/en)`
- Apply: OK
- Verify: 140 passed, 1 warning in 3.10s
- git status: only listed files
- Commit: c82edac

### T6 — Voice I/O — DONE (2026-10-06 10:54)
Paths: `app/voice/store.py` `app/voice/safety.py` `app/voice/nlu_llm.py` `app/voice/tools.py` `app/voice/session.py` `tests/voice/test_io_layer.py`
Verify 1: `tests/voice` → Expected **160 passed**
Verify 2: `tests/test_lint_unscoped_queries.py` → Expected **4 passed**
Commit: `feat(voice): tenant-scoped verified tools, safety screen, LLM fallback, call session`
- Apply: OK
- Verify 1: 160 passed, 1 warning in 2.91s
- Verify 2: 4 passed, 1 warning in 59.50s
- git status: only listed files
- Commit: 392adc2

### T7 — Telephony gateway + outbound — DONE (2026-10-06 10:56)
Paths: `app/voice/providers/__init__.py` `app/voice/providers/fake.py` `app/voice/providers/sarvam.py` `app/voice/gateway.py` `app/voice/outbound.py` `tests/voice/test_gateway.py`
Verify 1: `tests/voice` → Expected **168 passed**
Verify 2: `tests/test_lint_unscoped_queries.py` → Expected **4 passed**
Commit: `feat(voice): Exotel stream gateway (barge-in, fail-safe passthru), Sarvam adapters, outbound queue`
- Apply: OK
- Verify 1: 168 passed, 2 warnings in 2.96s
- Verify 2: 4 passed, 1 warning in 60.23s
- git status: only listed files
- Commit: 02da7bd

### T8 — Routers, mounting, scheduler — DONE (2026-10-06 10:58)
Paths: `app/routers/voice_admin.py` `app/routers/voice_platform.py` `app/main.py` `app/services/scheduler.py` `tests/voice/test_voice_routes.py`
Verify: `tests/voice tests/test_lint_unscoped_queries.py tests/test_phase2_unscoped_query_linter.py tests/test_tenant_scope_fail_closed.py tests/test_phase_g_scheduler.py tests/test_scheduler_health_checkin.py`
Expected: **208 passed**
Extra check: `venv\Scripts\python.exe -c "from fastapi.testclient import TestClient; import app.main as m; c=TestClient(m.app); print(c.get('/voice/exotel/passthru?k=x',follow_redirects=False).status_code, c.get('/admin/voice/calls').status_code, c.get('/platform/voice/overview').status_code)"` → Expected `200 401 401`
Commit: `feat(voice): /admin/voice control-room API, /platform/voice owner API, scheduler jobs`
- Apply: OK
- Extra check: 200 401 401 (matched)
- Verify: 210 passed, 2 warnings in 65.72s
- git status: only listed files
- Commit: 52a7004

### T9 — Admin + owner panel UI, permissions — DONE (2026-10-06 10:59)
Paths: `admin/index.html` `admin/platform.html` `app/services/permissions.py`
Verify: `tests/test_held_report_recovery_and_staff_delete.py tests/test_permissions.py tests/test_admin_me.py`
Expected: **40 passed**
Extra check (JS syntax): extract every inline `<script>` of both HTML files to temp `.js` files and run `node --check` on each → all OK.
Commit: `feat(voice): AI Receptionist control room page and owner voice controls`
- Apply: OK
- Extra check (JS syntax): all inline scripts checked with node --check -> ALL_JS_OK
- Verify: 40 passed, 1 warning in 3.15s
- git status: only listed files
- Commit: c3481cd

### T10 — Evaluation + smoke scripts — DONE (2026-10-06 11:00)
Paths: `scripts/voice_eval.py` `scripts/voice_provider_smoke.py` `docs/voice/eval-reports/2026-10-06-synthetic.md`
Verify: `venv\Scripts\python.exe scripts/voice_eval.py --check`
Expected: `19120 cases, 100.0% overall`, exit 0, every language family 100.0%.
NOTE: this is a circular floor (same author wrote rules and generator). It proves nothing about speech or real callers; it is a regression guard only.
Commit: `test(voice): synthetic understanding benchmark and Sarvam live smoke script`
- Apply: OK (harmless whitespace warning at EOF ignored per protocol)
- Verify: 19120 cases, 100.0% overall (en: 100%, hi: 100%, rhi: 100%, rte: 100%, te: 100%) -> exit 0
- git status: only listed files
- Commit: 2a52293

### T11 — Final verification + PR — DONE (2026-10-06 11:35)
1. Full suite (as T0). Expected: only the Baseline failures; passed ≈ baseline + 235.
2. Python 3.11 compile of every changed `.py`: `uv run --no-project --python 3.11 python -m py_compile <files>` (files = `git diff --name-only main -- '*.py'`). Expected: no output, exit 0 (designer: 46 files, 0 errors).
3. Kill orphaned `python.exe` processes left by pytest.
4. Browser QA (local app, `VOICE_ENABLED=false` is fine for UI): owner panel → open a clinic → "AI Voice Receptionist" box visible; tick enable; map a fake Exophone `+914000000001`; admin panel for that clinic shows **AI Receptionist** in the sidebar; Test console: type `Naaku repu heart doctor kavali` → Kriya asks morning/evening in Telugu; `morning` → offers a real slot from your test data or says no slots; the call detail shows conversation + trace with `SIMULATED` writes. Screenshot each.
5. Open PR (do not merge). Paste PR URL here.
- Full suite: 3897 passed, 4 skipped, 6 warnings in 259.16s (0 failed; baseline was 3662 passed -> exactly +235 passed)
- Python 3.11 compile: 46 changed .py files checked via `uv run --no-project --python 3.11 python -m py_compile` -> exit 0, 0 errors
- Process cleanup: verified no orphaned python test processes running
- Browser QA Screenshots:
  - `01_owner_voice_panel.png`: [01_owner_voice_panel.png](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/superpowers/plans/2026-10-06-ai-receptionist/screenshots/01_owner_voice_panel.png) (Owner panel: "AI Voice Receptionist" enabled, budget, Exophone mapped)
  - `02_admin_sidebar_voice.png`: [02_admin_sidebar_voice.png](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/superpowers/plans/2026-10-06-ai-receptionist/screenshots/02_admin_sidebar_voice.png) (Admin sidebar: "AI Receptionist" tab visible under Patients)
  - `03_test_console_conversation.png`: [03_test_console_conversation.png](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/superpowers/plans/2026-10-06-ai-receptionist/screenshots/03_test_console_conversation.png) (Test console: Telugu conversation turns with Kriya)
  - `04_call_detail_trace_simulated.png`: [04_call_detail_trace_simulated.png](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/superpowers/plans/2026-10-06-ai-receptionist/screenshots/04_call_detail_trace_simulated.png) (Call detail: action trace with SIMULATED entries)
- PR Branch: `feat/ai-receptionist` pushed to GitHub `origin` (based on `main` at `d263bb4`)
- PR URL: https://github.com/zuko14/medassist-AI/pull/new/feat/ai-receptionist

---

## Human gates (README §6) — owner/clinic, not the agent

| Gate | Status | Evidence |
|---|---|---|
| G-MIGRATE-PROD | TODO | |
| G-META-TEMPLATE (`kriya_payment_link`, `appointment_confirmation`) | TODO | |
| G-SARVAM-LIVE (`scripts/voice_provider_smoke.py` exit 0) | PASS (2026-10-06) | TTS 79.2kB 8kHz PCM + STT 'నమస్కారం.' te-IN confirmed live against api.sarvam.ai |
| G-EXOTEL-FLOW (incl. kill-switch test, `CallSid` param name) | TODO | |
| G-LANG-REVIEW (te, hi templates) | TODO | |
| G-MED-REVIEW (safety phrases) | TODO | |
| G-RATES + per-clinic budgets | TODO | |
| G-PILOT (≥98 % on recorded calls, 0 false confirmations, 0 safety misses) | TODO | |
| G-CANARY (1 branch, 1 week) | TODO | |

## Environment variables to add in production (only after T11 + gates)

`VOICE_ENABLED`, `VOICE_STREAM_TOKEN` (long random), `VOICE_PUBLIC_WSS_URL`, `SARVAM_API_KEY`,
`EXOTEL_ACCOUNT_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, `EXOTEL_API_HOST`, `EXOTEL_OUTBOUND_FLOW_URL`,
`VOICE_STT_PAISE_PER_MINUTE`, `VOICE_TTS_PAISE_PER_1K_CHARS`, `VOICE_TELEPHONY_PAISE_PER_MINUTE`,
optional: `VOICE_LLM_MODEL`, `VOICE_MAX_CONCURRENT_CALLS`, `SARVAM_TTS_SPEAKER`, `SARVAM_STT_MODEL`.

## Phase 2 (not started) — see README §8

---

## Post-launch fixes — 2026-10-07 (from the first 8 real calls)

Evidence: `voice_calls` / `voice_call_events` for Aura Dental Hospital, 2026-10-06/07. 7 of 8 calls were
handed to staff; 6 for `repeated_misunderstanding` (some within 3–5 s). Root causes, all fixed:

| # | Root cause (seen in prod) | Fix |
|---|---|---|
| 1 | LLM fallback 100% dead: `google/gemini-2.0-flash-001` retired on OpenRouter (HTTP 404 in `ai_usage_ledger`, 6/6 voice_nlu calls). | `VOICE_LLM_MODEL=google/gemini-3.1-flash-lite`, backup `VOICE_LLM_FALLBACK_MODEL=openai/gpt-4.1-mini` (JSON-capable; deepseek-chat is not). Platform `OPENROUTER_FALLBACK_MODEL` default also moved off the dead id. |
| 2 | "హలో" / "హ్మ్" / "ఓకే" counted as misunderstandings; 3 in a few seconds = transfer. | `policy.is_filler`; dialog answers fillers/bare acks patiently (`listening`, then menu) for 2 turns before they count. |
| 3 | Caller's "hello?" as the line connects barged in and cut the greeting; they never heard it. | Gateway: a filler that interrupts Kriya replays the interrupted sentence instead of becoming a turn (max 2). Real speech still interrupts instantly. |
| 4 | "ఓకే." / "ఆ." to "బుక్ చేయమంటారా?" not understood → booking lost to a transfer. | Lexicon: ఓకే, అలాగే, తప్పకుండా, ఓకే/ఆ/హా short replies (Telugu "ఆ" only as a whole reply — it also means "that"). |
| 5 | "వేరే సమాచారం కావాలి" (Kriya's own menu option) and "డాక్టర్ స్లాట్స్ ఖాళీ ఉన్నాయా?" not understood. | INFO words → asks which topic; AVAIL / doctor+want → booking; bare date at opening → booking. |
| 6 | First "reception" mention (incl. mis-heard STT) transferred instantly. | First plain request at call start: Kriya offers once to do it herself; yes / asking again / mid-task / after any miss → transfer. |
| 7 | STT locked to the clinic language: Hindi/English callers transcribed as Telugu. | `VOICE_STT_AUTO_LANGUAGE=true` (default) → Sarvam `language-code=unknown`; first substantive caller turn switches language immediately. |
| 8 | Lead calls asked the department again and greeted no one by name. | Outbound greeting uses the patient's plain name (WhatsApp emoji names are not spoken) and pre-fills the department from the lead's interest. |

Verification: `tests/voice` 199 passed, full suite 4051 passed / 0 failed (regression tests replay the exact production utterances),
`scripts/voice_eval.py --check` 19120 cases 100%, LLM models checked live on 16 real te/hi/en utterances.
Admin UI: AI Receptionist page restyled with the panel's own components (screens checked at 1440 px and 390 px).

Ops after deploy: if Render sets `VOICE_LLM_MODEL` or `OPENROUTER_FALLBACK_MODEL` to `google/gemini-2.0-flash-001`,
delete or update it. Then watch `ai_usage_ledger where task_type='voice_nlu'` for `success=true`.
