# 15 - AGENT HANDOFF & OPERATIONAL BRIEFING

This document serves as the master onboarding manual for any future senior engineer or AI agent (Claude Code / Antigravity) tasked with investigating bugs, modifying features, or evaluating production issues in KriyaAI.

---

## QUESTION 1: WHAT IS KRIYAAI?
KriyaAI (formerly MediAssist AI, version 2.0.0) is a production-hardened, multi-tenant healthcare operating system for Indian hospitals, specialty clinics, and diagnostic networks. It automates patient scheduling, doctor appointment booking, diagnostic lab report delivery, queue token management, and prepayment collection through the **Meta WhatsApp Cloud API**, backed by a web-based administrative portal and an owner multi-tenant management platform.

---

## QUESTION 2: WHAT ARE THE MAJOR SUBSYSTEMS?
1. **WhatsApp Conversational Engine**: Inbound message webhook ingress, state machine session tracking, slot booking, and template dispatch ([`app/services/conversation.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/conversation.py), [`app/services/whatsapp.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/whatsapp.py)).
2. **Clinical Safety Firewall & AI Engine**: Deterministic zero-LLM NMC medical safety screener ([`app/services/clinical_firewall.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/clinical_firewall.py)) backed by OpenRouter multi-model LLM completion and token spend tracking ([`app/services/ai_gateway.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/ai_gateway.py)).
3. **Database & Tenancy Layer**: Supabase PostgreSQL with off-loop PostgREST execution pool (`sb()`) and application-level scoping across 30 tenant-owned tables ([`app/tenancy.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/tenancy.py), [`app/database.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py)).
4. **Clinic Administration & Platform Panels**: Single-page frontends for clinic staff (`admin/index.html`) and platform owners (`admin/platform.html`).
5. **Payment Gateway**: Prepayment order generation, Razorpay webhook signature verification, and automated refunds ([`app/services/payment.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/payment.py)).
6. **Diagnostic LIS Connectors**: Dedicated background scraping daemon ([`connectors/runner.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/connectors/runner.py)) and CallMedex high-throughput lab ingestion pipeline ([`app/integrations/callmedex/`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/)).
7. **Background Scheduling & Recovery**: 24 registered APScheduler jobs with distributed PostgreSQL CAS locking and durable queue reapers ([`app/services/scheduler.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/scheduler.py)).

---

## QUESTION 3: WHERE DOES EXECUTION BEGIN?
- **Web Service Entrypoint**: [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/main.py), launched via `uvicorn app.main:app` (configured in `Dockerfile` and `render.yaml`).
- **Lifespan Startup Hook**: `lifespan(app)` in `app/main.py` executes startup pre-flights:
  1. Sizes `_DB_EXECUTOR` thread pool (16 workers).
  2. Verifies production credentials (refuses boot if placeholder secrets exist).
  3. Validates `CONNECTOR_ENCRYPTION_KEY` format.
  4. Checks database schema migration parity against disk (`schema_migrations`).
  5. Starts CallMedex queue engine and APScheduler (`scheduler_service.start()`).
- **Connector Worker Entrypoint**: [`connectors/runner.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/connectors/runner.py), executed as `python -m connectors.runner --all`.

---

## QUESTION 4: HOW DOES A USER REQUEST TRAVEL THROUGH THE SYSTEM?
### Inbound WhatsApp Message:
1. Meta sends HTTP POST to `/webhook`.
2. Router verifies HMAC-SHA256 signature (`X-Hub-Signature-256`).
3. Payload is immediately persisted to table `inbound_messages` with `status = 'received'`.
4. Endpoint responds with HTTP 200 within 15ms.
5. FastAPI `BackgroundTasks` dispatches execution to `conversation_service.process_inbound_message()`.
6. `resolve_tenant()` resolves clinic identity.
7. Input passes through `strip_injection_markers()` and `ClinicalFirewall`.
8. `ConversationService` queries patient state, checks intent, and calls `OpenRouterService` if necessary.
9. Outbound message sent via `whatsapp_service.send_*()` and logged to `outbound_message_ledger`.
10. `inbound_messages` row updated to `status = 'completed'`.

### Clinic Admin HTTP Request:
1. Browser issues request to `/admin/*` with `Authorization: Bearer <token>` or session cookie.
2. `verify_credentials` checks session token against `admin_sessions` and resolves `AdminUser`.
3. `require_permission(code)` asserts user role permissions.
4. `enforce_clinic_access()` validates requested clinic matches user's tenant context.
5. Endpoint queries database using `scoped_query(table, clinic_id)` wrapped in `await sb(...)`.
6. Returns JSON response with HTTP security headers.

---

## QUESTION 5: HOW IS TENANT IDENTITY ESTABLISHED?
- **Inbound WhatsApp**: Resolved in `resolve_tenant()` by `phone_number_id` (Meta immutable ID) or normalized E.164 phone number. Fails closed (raises `TenantNotFound`) if unresolvable.
- **Admin Panel**: Extracted from authenticated user session (`admin_sessions.clinic_id`). Verified via `enforce_clinic_access()`.
- **Super-Admin**: Required to explicitly supply `?clinic_id=<uuid>` parameter. Cannot default to a wildcard.
- **Razorpay Webhooks**: Extracted from URL parameter (`/webhooks/razorpay/{clinic_id}`) or payload metadata notes (`booking_id`, `clinic_id`).

---

## QUESTION 6: HOW IS AUTHENTICATION PERFORMED?
- **Meta Webhook**: Secret HMAC-SHA256 (`META_APP_SECRET`).
- **Clinic Admin**: Bcrypt password hashing + 32-byte session tokens in `admin_sessions`.
- **Platform Owner**: HTTP Basic Auth checking `OWNER_USERNAME` / `OWNER_PASSWORD`.
- **Connectors**: Shared secret (`INTEGRATION_SECRET`) or per-clinic pinned connector keys.
- **CallMedex**: Bearer token + HMAC digest + 5-minute replay window.
- **Prometheus Metrics (`/metrics`)**: Bearer token checking `METRICS_TOKEN`.

---

## QUESTION 7: HOW DOES DATA MOVE THROUGH THE DATABASE?
- All database communication passes through the Supabase PostgREST client in [`app/database.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py).
- Because the Supabase client executes synchronous blocking HTTP requests, all queries are wrapped in `await sb(...)` which dispatches execution to `_DB_EXECUTOR` (16-thread pool).
- Tenancy is stamped on every write (`clinic_id`) and filtered on every read (`scoped_query()`).
- High-concurrency slot booking uses a database-level unique constraint (`idx_appointments_slot_unique`).

---

## QUESTION 8: WHICH EXTERNAL SERVICES ARE INVOLVED?
- **Supabase**: PostgreSQL persistence and Storage (`lab-reports` bucket).
- **Meta WhatsApp Cloud API**: Inbound/outbound patient messaging.
- **Razorpay**: Prepayment order capture and instant refunds.
- **OpenRouter**: LLM inference (`deepseek/deepseek-chat` and `google/gemini-2.0-flash-001`).
- **MocDoc / LIS**: External hospital portal scraped via Playwright.

---

## QUESTION 9: WHICH BACKGROUND PROCESSES EXIST?
- **APScheduler**: 24 cron/interval jobs running inside the web service (Asia/Kolkata timezone).
- **Connector Daemon**: Standalone container running `connectors.runner` every 1 minute.
- **FastAPI BackgroundTasks**: Inbound webhook processing and usage ledger recording.

---

## QUESTION 10: WHERE ARE THE HIGHEST-RISK BOUNDARIES?
1. **Application-Level Tenancy (RLS Bypass)**: The database role `service_role` carries `BYPASSRLS`. If an engineer forgets `.eq("clinic_id", clinic_id)` on a tenant table, data leaks across tenants.
2. **WhatsApp Webhook Ingress (20-Second SLA)**: Meta drops the connection if HTTP 200 is not returned within 20s. Any synchronous blocking operation in the webhook handler triggers retry storms.
3. **Outbound Credential Sharing**: `_get_credentials` in `whatsapp.py` must never fall back to platform credentials in multi-tenant mode.

---

## QUESTION 11: WHERE SHOULD AN AGENT START WHEN DEBUGGING A PROBLEM?
- **Patient WhatsApp Not Answering**:
  1. Inspect `inbound_messages` table to verify webhook ingress.
  2. Inspect `failed_messages` for dead-letter queue entries.
  3. Check process logs for `MISSING_CLINIC_WHATSAPP_CREDENTIALS` or `TenantNotFound`.
- **Database Query Timeouts / Hanging**:
  1. Check if the query is wrapped in `await sb(...)`.
  2. Verify thread pool saturation in `_DB_EXECUTOR`.
  3. Inspect HTTP/1.1 session patch in `app/database.py`.
- **Cross-Tenant Data Leak Investigation**:
  1. Run `pytest tests/test_lint_unscoped_queries.py`.
  2. Inspect the queried table against `TENANT_OWNED_TABLES` in `app/tenancy.py`.
  3. Check if `enforce_clinic_access()` was called in the route.
- **Connector / Lab Report Sync Failures**:
  1. Check `connector_failed_reports` table.
  2. Inspect `mediassist-connector-worker` container logs.
  3. Verify `CONNECTOR_ENCRYPTION_KEY` validity.

---

## QUESTION 12: WHAT FILES ARE AUTHORITATIVE?
- **Tenancy Tables**: [`app/tenancy.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/tenancy.py) (`TENANT_OWNED_TABLES`).
- **Database Access**: [`app/database.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py) (`sb()`, `scoped_query()`).
- **Inbound Conversation Flow**: [`app/services/conversation.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/conversation.py).
- **Clinical Safety Rules**: [`app/services/clinical_firewall.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/clinical_firewall.py).
- **Outbound WhatsApp**: [`app/services/whatsapp.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/whatsapp.py).
- **Clinic Admin Frontend**: [`admin/index.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/index.html).
- **Platform Owner Frontend**: [`admin/platform.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/platform.html).
- **Deployment Topology**: [`render.yaml`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/render.yaml), [`Dockerfile`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/Dockerfile).

---

## QUESTION 13: WHAT AREAS ARE FRAGILE?
- **Supabase Async Queries**: Calling `.execute()` directly on Supabase SDK v2 without `sb()` blocks the asyncio event loop thread and degrades the entire API.
- **Admin Frontend Drift**: Never edit `admin/admin.js`; all active clinic panel code is inlined in `admin/index.html`.
- **Playwright in Web Process**: `settings.run_connectors_in_web` must stay `false` in production to prevent Chromium OOM crashes from killing the web server.

---

## QUESTION 14: WHAT ASSUMPTIONS MUST NEVER BE MADE?
1. **Never assume database RLS protects data**: Application code is the only tenant boundary.
2. **Never assume an LLM can safely give clinical advice**: `ClinicalFirewall` must remain zero-LLM and deterministic.
3. **Never assume `"default"` is a valid clinic ID**: It is a historical sentinel and must be rejected.
4. **Never assume a clinic inherits platform WhatsApp credentials**: It must have its own credentials configured.

---

## QUESTION 15: WHAT REMAINS UNKNOWN?
- Live MocDoc scraping behavior against active hospital portals (requires live hospital credentials).
- Live ABDM Milestone M1-M3 integration against live production National Health Authority sandbox.

---

## QUESTION 16: WHAT EVIDENCE SUPPORTS THIS ARCHITECTURE?
- 85 applied PostgreSQL migration scripts in `migrations/`.
- 2,978 passing automated tests in `tests/` and `app/integrations/callmedex/tests/`.
- Concrete source code and AST linters across `app/`, `connectors/`, and `admin/`.
