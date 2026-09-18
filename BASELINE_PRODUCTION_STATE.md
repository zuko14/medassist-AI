# BASELINE PRODUCTION STATE — KRIYA AI

**Audit Date:** 2026-09-18  
**Auditor Role:** Principal Production Engineer, Software Architect, Security Engineer, QA Engineer  
**Repository:** `KriyaAI` (Healthcare Multi-Tenant AI Platform)  
**Starting Commit:** `be4f7a4ed8f7f1155cf382f3cc7adb034902fb7b` (`feat: women & child plan (082), lab test details (083), diagnostics catalogue experience`)  
**Branch:** `main`  
**Working Tree Status:** 
- Modified: `admin/index.html`, `app/routers/admin.py` (Session 13 Lab Test Bulk Delete in-progress verification)
- Untracked: `docs/sessions/SESSION_13_LAB_TEST_BULK_DELETE.md`, `tests/test_lab_tests_bulk_delete.py`

---

## 1. System Map & Architecture Inventory

### 1.1 Application Architecture
- **Backend Core:** FastAPI (Python 3.12/3.11), Uvicorn ASGI server with multi-process concurrency (`WEB_CONCURRENCY=2`, Render.com container setup).
- **Database & Persistence:** Supabase PostgreSQL 15+ with Row Level Security (RLS) policies (Migrations 001 through 083).
  - Off-loop PostgREST execution wrapper: `app.database.sb()` utilizing bounded thread pool `_DB_EXECUTOR` (`db_thread_pool_size=16`, timeout `15s`, HTTP/1.1 forced to avoid H2 multiplexing cascade failures).
  - Tenant query builder: `app.database.scoped_query(table, clinic_id)` enforcing pre-filters against `TENANT_OWNED_TABLES`.
- **AI Stack:**
  - OpenRouter (`https://openrouter.ai/api/v1/chat/completions`) as primary provider via `app.services.ai_engine.OpenRouterService` implementing `ILLMProvider`.
  - Default Model: `deepseek/deepseek-chat` (or configured via `OPENROUTER_MODEL`).
  - Fallback Provider: Groq (`llama-3.3-70b-versatile`).
  - Zero-LLM Clinical Safety Firewall: `app.services.clinical_firewall` (deterministic medication & diagnostic query interceptor).
  - Vector/Semantic Search: `app.services.vector_search.VectorSearchService` (with `TenantIsolationError` guard).
- **Messaging Stack:**
  - Meta WhatsApp Cloud API (`v22.0`).
  - Webhook: `app.routers.webhook` verifying `X-Hub-Signature-256` HMAC-SHA256 and routing incoming messages by destination phone number (`to` number in WABA payload) to resolve tenant `clinic_id`.
  - Durable Inbound Queue: `app.services.message_queue` and table `inbound_messages` (fail-closed idempotency).
  - Outbound Ledger: `app.services.message_accounting.log_outbound` and table `outbound_message_ledger`.
- **Frontends:**
  - Clinic Admin Panel: `admin/index.html` (~496 KB single-page web app with vanilla JS, responsive layout, dark/light theme, role-based tab gating).
  - Platform Super-Admin Dashboard: `admin/platform.html` (~220 KB, owner-only finance, tenant provisioning, system health).

---

### 1.2 Data Architecture & Core Entities

| Entity | Primary Key | Tenant Scoping Column | Key Relationships |
|---|---|---|---|
| `clinics` | `id` (UUID) | Master tenant table (`id`) | Root tenant container |
| `branches` | `id` (UUID) | `clinic_id` (UUID) | Belongs to clinic; represents physical branches |
| `clinic_admins` | `id` (UUID) | `clinic_id` (UUID) | Staff & admin accounts; role & permissions |
| `patients` | `id` (UUID) | `clinic_id` (UUID) | Phone unique per clinic |
| `conversations` | `id` (UUID) | `clinic_id` (UUID) | Active WhatsApp session state & context |
| `appointments` | `id` (UUID) | `clinic_id` (UUID) | Links patient, doctor/branch/lab_test |
| `lab_tests` | `id` (UUID) | `clinic_id` (UUID) | Catalogue items (name, price_paise, category, description) |
| `lab_reports` | `id` (UUID) | `clinic_id` (UUID) | Diagnostic results, storage file_path, status |
| `specialty_treatments`| `id` (UUID) | `clinic_id` (UUID) | Clinical treatment packages |
| `analytics_events` | `id` (UUID) | `clinic_id` (UUID) | Tracking patient funnel and search taps |
| `outbound_message_ledger` | `id` (UUID) | `clinic_id` (UUID) | WhatsApp send tracking, Meta billing categories |
| `admin_audit_logs` | `id` (UUID) | `clinic_id` (UUID) | Immutable audit log of administrative mutations |

---

### 1.3 Security & Multi-Tenancy Architecture
- **Tenant Resolution:**
  - Webhook path: `destination_phone` -> `clinics.whatsapp_number` -> `clinic_id`.
  - Admin API path: `Authorization: Bearer <token>` or session cookie -> `clinic_admins.clinic_id` -> verified against `clinic_id` header/query param via `enforce_clinic_access()`.
- **Isolation Enforcement:**
  - `TENANT_OWNED_TABLES` (in `app/tenancy.py`) defines all 28 tenant tables.
  - `is_valid_clinic_scope(clinic_id)` rejects non-scopes (`default`, `none`, `null`, `""`, `*`, `all`).
  - `tests/test_lint_unscoped_queries.py` statically enforces AST query linting with a zero-tolerance ratchet.

---

### 1.4 Conversation Architecture
`WhatsApp Webhook (Meta)`  
  → HMAC Signature Check (`verify_webhook_signature`)  
  → Durable Inbound Enqueue (`inbound_messages`)  
  → BackgroundTask Worker  
  → Duplicate Message Check (`last_processed_message_id`)  
  → Tenant Resolution (`get_clinic_by_phone`)  
  → Clinical Firewall Check (`clinical_firewall.screen_input`)  
  → Intent Detection (`ai_engine.detect_intent` with fallback `keyword_intent_fallback`)  
  → FSM Transition (`ConversationManager.handle_message`)  
  → State Handling (e.g. `browsing_lab_tests` -> `_match_lab_tests`)  
  → Human Escalation / Staff Handoff (`escalated_to_human`)  
  → Outbound Send (`whatsapp_service.send_text` / `send_interactive_list`)  
  → Message Ledger Logging (`outbound_message_ledger`)

---

## 2. Existing Workflow Inventory

1. **Patient Conversation Start & Greeting:** Language selection, consent, main menu dispatch.
2. **Patient Test Search:** Lab test catalogue search (`_show_lab_test_list`, `_match_lab_tests`), category grouping (`_show_lab_test_categories`).
3. **Doctor & Slot Booking:** Doctor directory, branch selection, date/slot selection, slot holding (`pending_payment` / `pending_review`).
4. **Lab Test Booking:** Test selection, sample collection date, home/centre collection, booking creation.
5. **Human Escalation:** Patient requests human agent -> state transitions to `escalated_to_human` -> patient advised to call/await staff.
6. **Staff Response:** Currently done via manual phone call or reception; no unified in-app AI-suggested staff reply inbox.
7. **Lab Report Delivery:** HMIS connector (MocDoc) / Admin upload -> PDF storage -> WhatsApp delivery (template or freeform within 24h).
8. **Catalogue Management:** Create, edit, CSV import (`/admin/lab-tests/import-csv`), bulk delete (`/admin/lab-tests/bulk-delete`).
9. **Analytics & Insights:** `analytics_service.get_insights` computing booking trends, revenue, diagnostics drop-off, report turnaround.
10. **Data Retention & Compliance:** Scheduled daily jobs purging expired conversations (30-day DPDP) while maintaining clinical records (7-year NMC).

---

## 3. Baseline Verification Runs

### Test Run 1: Query Scoping Linter & OpenRouter Provider & Bulk Delete
```
Command: pytest tests/test_lint_unscoped_queries.py tests/test_openrouter.py tests/test_lab_tests_bulk_delete.py
Collected: 26 items
Results: 26 passed in 47.57s
Status: PASS (0 failures, 0 warnings)
```

### Test Run 2: Core AI Engine, Clinical Firewall, Insights, Lab Search, Tenant Isolation
```
Command: pytest tests/test_ai_engine.py tests/test_clinical_firewall.py tests/test_insights.py tests/test_lab_test_search.py tests/test_phase2_tenant_isolation.py
Collected: 98 items
Results: 98 passed in 8.53s
Status: PASS (0 failures, 0 warnings)
```

### Frontend Validation: Admin Panel JS Syntax
```
Command: node inline script syntax validation on admin/index.html
Results: 1 large inline script block validated with Function constructor
Status: PASS (0 syntax errors)
```

### Database Migration Status:
- Verified 83 sequential SQL migrations in `migrations/` from `001_initial_schema.sql` through `083_lab_test_details.sql`.
- Next additive migration will be `084_ai_features_infrastructure.sql`.

---

## 4. Pre-Existing Risks & Gaps Discovered

1. **No Staff Inbox View in Admin Panel:** When a conversation enters `escalated_to_human`, staff had no web interface to read conversation history, view context, or reply.
2. **Missing Conversation State Hold on Human Escalation:** In `app/services/conversation.py:handle_message`, messages sent while in `escalated_to_human` fell through the `else:` branch, resetting conversation state back to `main_menu` instead of staying parked with staff.
3. **No Centralized AI Gateway:** Direct calls to `call_openrouter_with_backoff` were scattered in `ai_engine.py` without structured task routing or per-tenant cost caps.
4. **Catalogue Import Restricted to CSV:** Admin catalogue import only accepted flat CSV files; PDFs, Excel (.xlsx), and images required manual transcription.
5. **Lab Search Limited to Direct Substring Matching:** `_match_lab_tests` required all query tokens to exist in the test name, missing synonyms ("sugar" for "glucose"), Hindi/Telugu queries, and common typos.
6. **No Automated Catalogue Quality / Cleanup Detection:** Duplicates, missing fasting/prep requirements, and aberrant prices had to be manually spotted row-by-row.
7. **No Automated AI Insights Operational Summary:** Insights data was numerical and graphical only, requiring clinic managers to manually interpret drop-offs.
