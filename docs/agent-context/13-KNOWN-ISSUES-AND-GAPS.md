# 13 - KNOWN ISSUES, GAPS & IMPLEMENTATION STATUS

This document provides a forensic classification of every major capability in KriyaAI, distinguishing between implemented, partially implemented, stubbed, dead, and unverified components based on code and test evidence.

---

## 1. COMPREHENSIVE CAPABILITY STATUS MATRIX

| Capability / Subsystem | Implementation Status | Evidence & Source Files | Operational Reality & Known Constraints |
| :--- | :--- | :--- | :--- |
| **WhatsApp Inbound Webhook** | Verified from code/tests | [`app/routers/webhook.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/webhook.py), [`app/services/conversation.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/conversation.py) | Cryptographic HMAC verification, atomic deduplication via `processed_messages`, durable ingress to `inbound_messages`. Returns HTTP 200 within 15ms. |
| **WhatsApp Outbound Messaging** | Verified from code/tests | [`app/services/whatsapp.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/whatsapp.py) | Enforces strict per-clinic credentials in `_get_credentials`. Refuses platform credential fallback in multi-tenant mode to prevent cross-tenant reply pollution. |
| **Razorpay Payments & Webhooks** | Verified from code/tests | [`app/services/payment.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/payment.py), [`app/routers/razorpay_webhook.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/razorpay_webhook.py) | Order generation, webhook signature validation, state transition `pending` -> `confirmed`, automated instant refunds on appointment cancellation. |
| **Multi-Tenant Data Isolation** | Verified in App Code (RLS Bypassed in DB) | [`app/tenancy.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/tenancy.py), [`app/database.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py) | Supabase `service_role` connection carries `BYPASSRLS`. Database RLS policies are bypassed; tenancy is 100% enforced via `TENANT_OWNED_TABLES` and `scoped_query()` in application code. |
| **Off-Loop Database Execution** | Verified from code/tests | [`app/database.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py) | Blocking Supabase PostgREST queries run in dedicated `_DB_EXECUTOR` thread pool (16 threads). HTTP/1.1 forced to prevent HTTP/2 multiplexed socket resets. |
| **Clinical Safety Firewall** | Verified from code/tests | [`app/services/clinical_firewall.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/clinical_firewall.py) | Zero-LLM deterministic interceptor screening 250+ drug names and treatment requests across English, Hindi, and Telugu. Replaces LLM with static NMC disclaimer. |
| **OpenRouter AI Orchestration** | Verified from code/tests | [`app/services/ai_engine.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/ai_engine.py), [`app/services/ai_gateway.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/ai_gateway.py) | Native multi-model fallback `[deepseek/deepseek-chat, google/gemini-2.0-flash-001]`, token accounting in `ai_usage_ledger`, administrative spend cap. |
| **Vector / Semantic Search** | **Stubbed** | [`app/services/vector_search.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/vector_search.py) | Documents the pgvector equality pre-filtering pattern (`WHERE clinic_id = $1`). Active FAQ engine currently uses deterministic keyword matching. |
| **ABDM / ABHA Validation** | **Partially Implemented** | [`app/services/abdm.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/abdm.py) | Validates ABHA numbers (14-digit regex + Luhn checksum) and addresses. Falls back to format-only validation when ABDM Gateway credentials are unset. |
| **MocDoc Playwright Connector** | Wired but unverified at runtime | [`connectors/mocdoc/worker.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/connectors/mocdoc/worker.py), [`connectors/runner.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/connectors/runner.py) | Code and integration tests are complete, but live scraping requires active hospital credentials and external portal access. |
| **CallMedex Lab Queue Engine** | Verified from code/tests | [`app/integrations/callmedex/`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/) | HMAC verification, replay window check, in-memory/DB task queue, and idempotent PDF ingestion. |
| **Distributed Scheduler Lock** | Verified from code/tests | [`app/services/distributed_lock.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/distributed_lock.py) | Atomic CAS acquire via `acquire_scheduler_lock` RPC; background lease renewal heartbeat; raises `LockStolenError` on lease loss. |
| **Orphaned File: `admin/admin.js`**| **Dead / Unreferenced** | [`admin/admin.js`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/admin.js) | Explicitly excluded from `admin/index.html` (which inlines its own script). Modifications to this file have zero runtime effect. |
| **Root Shell Redirection Files** | **Removed 2026-09-23** | `main`, `tuple[bool`, `type`, `bool`, `Expected`, `str` | Accidental artifacts created by Windows/PowerShell redirection typos during past development sessions. |

---

## 2. PRODUCTION RELIABILITY RISKS & FRAGILE BOUNDARIES

1. **Supabase `service_role` BYPASSRLS Risk**:
   - Because application queries bypass database-level RLS, a developer adding a new endpoint who writes `supabase.table("patients").select("*")` without `.eq("clinic_id", clinic_id)` will cause a silent, catastrophic cross-tenant data leak.
   - Guard: Enforced exclusively by `tests/test_lint_unscoped_queries.py`. **Never disable or bypass this test in CI/CD.**
2. **Meta 20-Second Webhook Timeout**:
   - Meta requires an HTTP 200 within 20 seconds. If an endpoint attempts synchronous LLM calls, PDF generation, or OCR inside the webhook request thread, Meta times out and resends the webhook, causing cascading retry loops.
   - Guard: Webhook route immediately commits payload to `inbound_messages` and returns HTTP 200 within 15ms.
3. **Playwright Memory Footprint**:
   - Headless Chromium consumes 150MB–400MB RAM per worker. Running connectors inside the web service container risks OOM kill events that terminate the web process.
   - Guard: `render.yaml` sets `RUN_CONNECTORS_IN_WEB: "false"` on the web service and isolates scraping to `mediassist-connector-worker`.
4. **Tenant Cache Invalidation Window (KA-19)**:
   - In-memory tenant cache (`CACHE_TTL_SECONDS = 30`) is process-local. In a 4-worker production cluster, invalidating cache on one worker means the other 3 workers may continue serving a suspended clinic for up to 30 seconds.

---

## 3. SESSION 20 AUDIT (2026-09-23) — FIXED, AND WHAT STILL NEEDS A DECISION

"Verified from code/tests" in the matrix above did NOT hold for payments: the refund
cutoff had never run in production, because tests built slot times as `HH:MM` while
Postgres returns `HH:MM:SS`. Treat a mocked test's data shape as a claim to verify.

**Fixed (tests: `tests/test_session20_payment_audit_fixes.py`)**
1. Cancellation window never enforced (TIME parsed as `HH:MM` only) — now enforced for patient cancels only.
2. Late-payment auto-refund failure was recorded and announced as "refunded" while the money stayed captured.
3. Payment on a booking the patient cancelled while unpaid was kept silently (no refund, no alert).
4. Stray second payment on a settled booking: now alerts admin.
5. `check_doctor_leaves` cancelled PAID bookings with no refund — now refunds (window ignored), alerts on failure.
6. `get_available_slots` offered slots held by `pending_review` (DB index holds them) -> endless slot_taken loop.
7. `admin_confirm_booking` had no CAS: a concurrent reject+refund could be overwritten to `confirmed`.
8. 2h reminder had no lower time bound: a missed run reminded patients of visits already started.
9. Two patient messages quoted the PLATFORM phone (`settings.hospital_phone`) instead of the clinic's.
10. `/admin/bookings/{id}/refund` and `DELETE /admin/appointments/{id}` returned raw exception text.
11. DPDP erasure audit row NEVER written: `user_id="dpdp_erasure"` into a UUID FK and no `role` (NOT NULL); failure logged at debug. Tests mocked the DB.
12. Blocking Supabase Storage calls on the event loop (lab_reports upload/download/sign x6, CallMedex runner x2, erasure remove) — now `asyncio.to_thread`.
13. Check-in accepted cancelled/refunded/expired/pending bookings (stale panel) and messaged the patient a token — now 409.

**Audited and found sound (no change):** admin/platform frontend escaping (no stored XSS from patient or tenant fields), `resolve_tenant` fail-closed cascade, WhatsApp webhook ingest/retry/claim release, platform owner + FHIR + clinics + integration auth, FHIR tenant scoping, broadcast/notification scoping, IST daily counters and fail-open report gate, queue token uniqueness, deposit-percent validation, no blocking `.execute()` in async code.

**Resolved by owner decision (2026-09-23)**
- Branch-pinned staff (role=staff with branch_id) now see and act on ONLY their branch's bookings plus
  branch-less ones: `admin._staff_branch/_branch_kw/_enforce_booking_branch` + `database.restrict_to_branch`.
  clinic_admin / super_admin / tenant-wide staff are unchanged (queries byte-identical).
- Half-day leave now cancels (and refunds) only the bookings in the blocked session, using the same
  `database.doctor_session_slots` the picker uses; an unknown doctor leaves bookings untouched.
- 2h reminders compare full datetimes and query tomorrow when the window crosses midnight.

**Still open**
- Clinics without `config.integration_secret` accept lab reports signed with the shared
  platform secret (documented migration window in `routers/integrations.py`). Config, not code.

**Test harness (fixed 2026-09-23)**
`tests/conftest.py` now forces test credentials at import, BEFORE app.config loads `.env`, and replaces
the Postgres scheduler/phone lock with an in-memory one (except the 5 modules that test the real lock).
A plain `pytest` no longer touches production Supabase, WhatsApp or production locks. Set
`KRIYA_TEST_LIVE=1` to deliberately run against `.env`. `test_patient_metrics_production.py`'s three
live-data checks run only under that flag. Real-schema proofs: `tests/test_session20_real_postgres.py`;
payment lifecycle through the real HTTP route: `tests/test_session20_payment_e2e.py`.

## 4. SESSION 21 (2026-09-23) — FIXED

Details: `docs/sessions/SESSION_21_PATIENT_QUESTIONS.md`. Tests: `tests/test_session21_*.py`.
1. Weekly summary never saved: placeholder `source="generating"` violated migration 085's CHECK; failure swallowed, API returned `ready`; panel also expected `'available'`. Now saved or 503.
2. Patient stuck on an unanswered language picker (`selecting_language` never expires, rejected typed text). Consented patients with a language now escape it; "I need the menu"-style phrasing is `is_menu_request`.
3. Typed "do you provide <treatment>" answered with the department list. Now answered from the clinic's treatment catalogue (`specialty_flow.answer_named_treatment`, strict match, no LLM).
4. Treatment bookings could reach unmapped doctors via typed name/department, older list, Edit booking, or "select another doctor". All paths now go through `specialty_flow._treatment_doctors`.

Still open: `get_treatment_doctor_ids` returns an empty set on a DB error, which reads as "any doctor" (fails open to the unfiltered list, consistent with what the patient was shown).

## 5. SESSION 22 (2026-09-23) — FIXED

1. Lab collection dates always started tomorrow. `_next_collection_dates(window)` now includes today while now(IST) < window end (Sunday end when both Sunday times set); stale `labdate_` taps are refused. Unrecognised `days` text no longer loops forever. Tests: `tests/test_lab_same_day_collection.py`.
2. Doctor's evening shift invisible to patients: `doctor_branches.session='morning'` (Branches page) filtered it out in `get_available_slots`, while the Doctors form never showed the session. The form now shows/sends "Session at this Branch" and warns when a shift is hidden; `GET /admin/doctors` returns `branch_session`; every session write (create, update, assign, re-session) rejects a session naming a shift the doctor lacks (422). Tests: `tests/test_doctor_branch_session_visibility.py`.

3. **RESOLVED** — saving the Doctors form for a doctor at several branches deleted every `doctor_branches` row and re-inserted only the dropdown's branch. The form now locks the branch fields ("Assigned to multiple branches (manage in Branches tab)") and omits them from the payload; `update_doctor` reads the assignments before any write and returns 400 for a branch change on a multi-branch doctor (re-sending one existing assignment unchanged is a no-op). Tests: `tests/test_doctor_multi_branch_preserved.py`.
4. **RESOLVED** — `tests/test_multi_worker_smoke.py` could never pass: conftest's placeholder credentials made the lifespan refuse to boot (`APP_ENV` testing). The subprocess now runs with `APP_ENV=development`; boot gates stay covered by `test_production_launch_gates.py` / `test_security.py`.
5. **RESOLVED** — the empty root redirection artifacts (`main`, `tuple[bool`, `type`, `bool`, `Expected`, `str`) are deleted.

## 6. SESSION 23 (2026-09-24) — CALLMEDEX SYNC

Fixed:
1. Owner panel showed Accumx twice: it has a clinic-wide AND a MAHARANIPETA-branch MocDoc connector (both legit, migration 025). `/platform/callmedex/centers` now returns one row per clinic with `connectors[]` (scope + non-secret config); total reports were double-counted per connector — now per clinic. Each connector gets its own Edit (sends `branch_id`, prefilled, clinic/type locked). Placeholder CallMedex number `100000000000000` is shown as "NOT configured".
2. `resolve_processing_center` used `.single()` → errored for Accumx's 2 rows → CallMedex jobs fell back to env MocDoc creds. Now prefers the clinic-wide row. It also passed `password_encrypted` ciphertext to MocDoc as the password → now decrypted.
3. CallMedex WhatsApp "simulation mode" reported DELIVERED in production when the number/token was unset → lab_reports said "sent", patient got nothing. Production now treats it as FAILED and the runner falls back to the clinic number (also when a real CallMedex send fails).
4. Per-event signed callbacks + CallMedex-number booking flow + signed client — see 07-INTEGRATIONS §5. `PUT /platform/callmedex/whatsapp-settings` refuses a phone_number_id any clinic owns.
Tests: `app/integrations/callmedex/tests/test_callmedex_bidirectional_sync.py`, `tests/test_callmedex_webhook_isolation.py`, `tests/test_platform.py` (2 new).

Still open:
- **CallMedex → Kriya report submission is WIRED (Session 24):** Mounted `POST /api/v1/report-jobs`, `GET /api/v1/report-jobs/{report_job_id}`, and `POST /api/v1/notifications` on `v1_router` in `app/main.py`. Supports CallMedex's `X-Signature: sha256=...` over `ts.query.body`, direct PDF download from `source_document_url`, center mapping (Accumax `e204185b-fd1c-4753-9243-58715d76b51c` -> `c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c`, **unverified**), OCR, AI summary, WhatsApp delivery, DB persistence, and signed callbacks. Session 24b hardened it (see `docs/sessions/SESSION_24_CALLMEDEX_REPORT_JOBS_V1.md`): centre mapping fails closed (no default clinic; patient uploads use the CallMedex number only), cross-worker `scheduler_locks` claim per `report_job_id`, background in production, barcode-only jobs refused (no Chromium in web), `source_document_url` allowlisted to https `*.supabase.co`, `/notifications` → 422.
- Migration 086 checksum row is recorded in live Supabase `schema_migrations` (applied 2026-09-24).
- Accumx's two MocDoc connectors poll the same portal/slug; `lab_reports` unique index prevents double sends, but one of them is probably redundant — owner decision.
- `app/integrations/callmedex/tests` are outside `tests/`, so `tests/conftest.py`'s forced test credentials only apply when both dirs are collected in one run; running that dir alone uses `.env` (production).

Still open (decisions, not code defects):
- `get_treatment_doctor_ids` fails open on a DB error (Session 21 decision; the doctor list read right after it fails too).
- 2026-09-23 read-only check: all 3 live clinics (Aura, Accumx, Visakha) have NO `config.integration_secret`, so each accepts lab reports signed with the shared platform secret. Setting one without updating that clinic's sender breaks report delivery — coordinate per clinic.
- Tenant cache staleness up to 30 s across workers (KA-19, section 2).
