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
| **Root Shell Redirection Files** | **Dead / Garbage** | `main`, `tuple[bool`, `type`, `bool`, `Expected`, `str` | Accidental artifacts created by Windows/PowerShell redirection typos during past development sessions. |

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

**Open — needs a product decision, deliberately NOT changed**
- Branch-pinned staff see and can cancel/confirm/reject appointments of every branch in
  their clinic (list and actions are clinic-scoped). Branch pinning applies to staff and
  doctor management only.
- Only FULL-day doctor leave cancels bookings; a half-day leave added after bookings exist
  leaves those bookings in place.
- Clinics without `config.integration_secret` accept lab reports signed with the shared
  platform secret (documented migration window in `routers/integrations.py`).
- The 2h reminder window cannot wrap past midnight: slots between 22:00 and 24:00 get no 2h reminder.

**Known pre-existing test failures (fail identically on the untouched code)**
- `handle_message` tests need the live DB phone lock (see memory note): test_booking_confirmation_followup,
  test_branch_context_integrity (symptom dispatch), test_diagnostics_catalogue_experience (greeting/help/resubscribe).
- `test_phase2_route_adversarial_matrix` staff PUT/toggle/DELETE: unmocked DB call; the routes themselves
  call `enforce_clinic_access` on the loaded row.
- `test_phase_g_scheduler::test_send_24h_reminders_idempotency_and_update`: does not mock the distributed lock.
