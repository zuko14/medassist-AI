# 04 - API MAP & ENDPOINT INTELLIGENCE

This document inventories every HTTP endpoint registered in the KriyaAI FastAPI application, detailing the path, method, authentication requirement, tenant resolution mechanism, database operations, and operational characteristics.

---

## 1. ROUTER ARCHITECTURE & MOUNT POINTS

All routes are registered in [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/main.py) through FastAPI routers:

| Router Module | Prefix | Tag | Auth Mechanism | Primary Purpose |
| :--- | :--- | :--- | :--- | :--- |
| [`app.routers.webhook`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/webhook.py) | `/webhook` | `webhook` | HMAC SHA256 (`X-Hub-Signature-256`) / Meta Token | WhatsApp Cloud API inbound webhooks & hub verification |
| [`app.routers.razorpay_webhook`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/razorpay_webhook.py) | `/webhooks/razorpay` | `payments` | HMAC SHA256 (`X-Razorpay-Signature`) | Razorpay payment confirmation and failure webhooks |
| [`app.routers.health`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/health.py) | `/health` (and `/ready`, `/live`) | `health` | None (Public) | Container orchestration liveness & readiness probes |
| [`app.routers.admin`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/admin.py) | `/admin` | `admin` | Session Token (`admin_sessions`) / Basic Auth fallback | Clinic administrative dashboard operations |
| [`app.routers.platform`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/platform.py) | `/platform` | `platform` | HTTP Basic Auth (`OWNER_USERNAME` / `OWNER_PASSWORD`) | Platform multi-tenant owner & billing management |
| [`app.routers.clinics`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/clinics.py) | `/api/clinics` | `clinics` | Admin Secret (`X-Admin-Secret`) | Clinic provisioning, updates, and testing API |
| [`app.routers.fhir`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/fhir.py) | `/fhir` | `fhir` | HTTP Basic Auth (`admin_users`) | HL7 FHIR R4 interoperability API for HMIS / ABDM |
| [`app.routers.integrations`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/integrations.py) | `/internal/integrations` | `integrations` | Integration Secret (`X-Integration-Secret`) | Connector report ingestion (MocDoc, Lab connectors) |
| [`app.integrations.callmedex.api.router`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/api/router.py) | `/internal/integrations/callmedex` | `callmedex` | Bearer Token + Replay Protection + HMAC | CallMedex automated laboratory pipeline integration |
| [`app.main`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/main.py) (Root) | `/`, `/metrics`, `/privacy`, `/admin-panel`, `/platform-panel` | — | Various (Metrics requires token; panels serve static HTML) | Static HTML, Prometheus metrics export, root status |

---

## 2. WEBHOOK & MESSAGING ENDPOINTS

### `GET /webhook`
- **File**: [`app/routers/webhook.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/webhook.py) (L27)
- **Auth**: Query parameter verification: `hub.mode == "subscribe"`, `hub.verify_token == settings.meta_verify_token`.
- **Purpose**: Meta WhatsApp webhook challenge-response handshake.
- **Tenant Context**: Global (Meta registers one global webhook URL for the WABA).
- **Response**: Returns `hub.challenge` integer as plain text (HTTP 200) or HTTP 403 on invalid token.

### `POST /webhook`
- **File**: [`app/routers/webhook.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/webhook.py) (L45)
- **Auth**: Cryptographic HMAC-SHA256 signature verification via header `X-Hub-Signature-256` matching `settings.meta_app_secret` (or per-clinic secret).
- **Tenant Context**: Resolved from `metadata.phone_number_id` or `metadata.display_phone_number` in the webhook payload via `tenant_service.get_clinic_by_phone()`.
- **Database Operations**:
  1. Writes raw payload to `inbound_messages(clinic_id, raw_payload, status='received')`.
  2. Checks deduplication via `processed_messages(message_id, clinic_id)`.
- **Side Effects**: Dispatches async execution via `BackgroundTasks` to `conversation_service.process_inbound_message()`.
- **Response**: Immediate `{"status": "ok"}` (HTTP 200) within 15ms to prevent Meta retries.

---

## 3. PAYMENT WEBHOOKS

### `POST /webhooks/razorpay` and `POST /webhooks/razorpay/{clinic_id}`
- **File**: [`app/routers/razorpay_webhook.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/razorpay_webhook.py) (L22-23)
- **Auth**: HMAC-SHA256 signature verification via header `X-Razorpay-Signature`. Secret is resolved per-clinic if `clinic_id` in path, else uses global `settings.razorpay_webhook_secret`.
- **Tenant Context**: Explicit via `{clinic_id}` URL path parameter or extracted from payment notes (`booking_id`, `clinic_id`).
- **Database Operations**:
  1. Logs event to `payment_events(clinic_id, event_type, payload)`.
  2. Updates `bookings(status='confirmed' | 'failed', payment_status='paid')`.
  3. Updates associated `appointments(status='confirmed', payment_id=...)`.
- **Side Effects**: Triggers `whatsapp_service.send_text()` sending booking confirmation receipt with appointment details to patient.

---

## 4. SYSTEM HEALTH & METRICS ENDPOINTS

| Method | Path | Auth | Purpose | Response |
| :--- | :--- | :--- | :--- | :--- |
| `GET`, `HEAD` | `/health` | None | Basic liveness probe | `{"status": "ok", "service": "Kriya AI", "pid": 1234, "commit": "a1b2c3d4", "timestamp": "..."}` |
| `GET` | `/health/ready` (alias `/ready`) | None | Database readiness probe | Executes `supabase.table("patients").select("count").limit(1)`. Returns `{"status": "ready", "database": "connected"}` or HTTP 200 with `status: "not_ready"`. |
| `GET` | `/health/live` (alias `/live`) | None | Process liveness | Returns `{"status": "alive"}`. |
| `GET` | `/metrics` | Bearer Token / `X-Metrics-Token` | Prometheus scrapable metrics | Plain text format (`text/plain; version=0.0.4`) exposing counters, gauges, and histograms from `app.services.metrics`. |
| `GET` | `/health/privacy` (alias `/privacy`) | None | DPDP Privacy Policy | Static HTML document or 307 redirect. |

---

## 5. CLINIC ADMINISTRATION ENDPOINTS (`/admin/*`)

All `/admin` endpoints require authentication via either a session token in header `Authorization: Bearer <token>` (validated against table `admin_sessions`) or fallback HTTP Basic Authentication. Permissions are enforced via the `require_permission(code)` dependency.

### A. Authentication & Session Management
- `POST /admin/login`: Public. Validates `username` + `password` against `admin_users` table via `bcrypt.checkpw()`. Enforces rate-limiting (`_check_login_rate_limit()`). Generates a cryptographically random 32-byte session token with a 24-hour expiration, writing to `admin_sessions`. Returns `{token, user: {id, username, role, clinic_id, branch_id}}`.
- `POST /admin/logout`: Invalidates session by deleting token record from `admin_sessions`.
- `GET /admin/me`: Returns authenticated user profile, clinic details, subscription tier, and effective permissions.
- `PUT /admin/change-password`: Modifies user password, re-hashes with bcrypt, and invalidates all existing sessions for that user.
- `PUT /admin/change-username`: Modifies user username with uniqueness check.

### B. Staff & Role-Based Access Control
- `GET /admin/staff`: Lists staff members in `admin_users` scoped to `user.clinic_id` and optionally `branch_id`.
- `POST /admin/staff`: Creates a new staff user (`admin`, `staff`, `doctor`, `receptionist`) with bcrypt-hashed initial password. Requires `STAFF_MANAGE`.
- `PUT /admin/staff/{staff_id}`: Updates name, role, assigned branch, and active status.
- `PUT /admin/staff/{staff_id}/toggle`: Activates/deactivates staff member without deleting.
- `DELETE /admin/staff/{staff_id}`: Deletes staff record. Prevents self-deletion.

### C. Appointments & Queue Management
- `GET /admin/appointments`: Scoped (+ branch-restricted) query on `appointments`. Filters: `date_basis` (visit|booked), `date_from`/`date_to` (≤366 days) or `period_days`, `status`, `limit`≤100/`offset`, and `q` (search, 2026-09-25). `q` matches patient_name, booking_ref, doctor_name, lab_test_name (ilike, words in order) and, for phone-like input, the last 10 digits of patient_phone; it is tokenized by `appointment_search_filter()` so no PostgREST/LIKE metacharacter reaches the filter. `q` with no dates searches all dates, newest first. Returns `{appointments, total, window_total, summary, limit, offset, truncated}`.
- `GET /admin/appointments/recent`: Fetches latest 50 appointments.
- `GET /admin/appointments/upcoming`: Fetches appointments scheduled for today and tomorrow.
- `POST /admin/appointments/{appointment_id}/check-in`: Updates status to `checked_in`, assigns queue token number, logs action in `audit_logs`.
- `DELETE /admin/appointments/{appointment_id}`: Cancels appointment with cancellation reason, updates status to `cancelled`, and sends WhatsApp cancellation alert to patient.
- `POST /admin/doctors/{doctor_name}/queue/call-next`: Advances clinic live OPD queue for that doctor, marks previous patient `completed`, sets current patient `in_consultation`, and notifies patient via WhatsApp.

### D. Doctors, Specialties & Catalogues
- `GET /admin/doctors`: Lists active and inactive doctors with department, consultation fees, available time slots, and branch associations.
- `POST /admin/doctors`: Registers new doctor, adds slots to `doctor_slots` and branch links to `doctor_branches`.
- `PUT /admin/doctors/{doctor_id}`: Updates doctor attributes, fees, schedule, and branches.
- `DELETE /admin/doctors/{doctor_id}`: Soft-deletes or removes doctor record and associated schedules.
- `GET /admin/departments/popular`: Aggregates booking volume by specialty/department over the last 30 days.
- `GET /admin/treatments`: Lists clinic treatments catalogue (derma/dental/eye procedures) with prices and duration.
- `POST /admin/treatments`: Creates procedure catalogue entry.
- `PUT /admin/treatments/{treatment_id}`: Modifies procedure details.
- `DELETE /admin/treatments/{treatment_id}`: Removes treatment entry.
- `PUT /admin/treatments/{treatment_id}/doctors`: Maps doctors capable of performing specific treatments.
- `POST /admin/treatments/ai-description`: Uses LLM (`OpenRouter`/`Groq`) to draft patient-friendly treatment descriptions.

### E. Diagnostics & Laboratory Management
- `GET /admin/lab-tests`: Lists diagnostic catalogue items with prices, sample requirements, fasting flags, and turnaround times.
- `POST /admin/lab-tests`: Adds individual lab test.
- `PUT /admin/lab-tests/{test_id}`: Updates lab test specifications.
- `DELETE /admin/lab-tests/{test_id}`: Deletes test from catalogue.
- `POST /admin/lab-tests/import-csv`: Parses and imports lab test catalog from CSV.
- `POST /admin/lab-tests/import-preview`: Staging preview for CSV uploads, returns diff and validation warnings.
- `POST /admin/lab-tests/import-apply/{preview_id}`: Commits staged CSV import.
- `POST /admin/lab-tests/auto-classify`: AI-assisted grouping of tests into medical profiles/packages.
- `POST /admin/lab-tests/bulk-delete`: Bulk deletion of lab tests.
- `GET /admin/lab-collection-window`: Fetches daily sample collection operating hours.
- `PUT /admin/lab-collection-window`: Configures home collection and walk-in sample operating windows.
- `DELETE /admin/lab-collection-window`: Clears branch override, inheriting clinic defaults.

### F. Reports & Deliveries
- `GET /admin/lab-reports`: Lists processed patient diagnostic reports.
- `POST /admin/lab-reports/upload`: Manual PDF upload by clinic staff; generates AI clinical summary, stores in Supabase Storage (`lab-reports` bucket), writes to `lab_reports`, and delivers via WhatsApp.
- `POST /admin/lab-reports/{report_id}/resend`: Retries WhatsApp PDF delivery for failed report.
- `GET /admin/reports/queue`: Lists pending report reconciliation queue (reports matched with low confidence or missing phone numbers).
- `POST /admin/reports/{report_id}/resolve-match`: Staff confirms or overrides patient matching for an unverified report.
- `POST /admin/reports/{report_id}/dismiss`: Dismisses unmatched report from queue.
- `POST /admin/reports/release-held-walkins`: Bulk releases reports held due to missing patient registration.
- `GET /admin/reports/deliveries` (alias `/admin/lab-reports/deliveries`): Audits message delivery status (`sent`, `delivered`, `read`, `failed`) for lab reports.

### G. Clinic Settings, Integrations & Connectors
- `GET /admin/profile`: Fetches clinic name, address, working hours, UPI ID, logo URL.
- `PUT /admin/profile`: Updates clinic operational profile.
- `GET /admin/settings/payment`: Fetches Razorpay credentials status and payment modes (`full`, `partial`, `pay_at_clinic`).
- `PUT /admin/settings/payment`: Updates Razorpay key ID, key secret, and deposit percentages.
- `GET /admin/connectors`: Lists configured external connectors (MocDoc, Lab LIS).
- `GET /admin/connectors/types`: Available connector integrations.
- `PUT /admin/connectors`: Configures or updates connector credentials (stored encrypted via Fernet).
- `POST /admin/connectors/{connector_id}/toggle`: Enables/disables automated syncing.
- `POST /admin/connectors/{connector_id}/test`: Runs immediate test poll against connector endpoint.
- `GET /admin/connectors/{connector_id}/test-status`: Polls status of running test.
- `POST /admin/connectors/{connector_id}/run-now`: Triggers out-of-band immediate sync.
- `GET /admin/connectors/{connector_id}/audit-log`: Sync history and execution logs.
- `GET /admin/connectors/failed-reports`: Lists reports that failed extraction or ingestion.
- `POST /admin/connectors/failed-reports/{failed_report_id}/resolve`: Marks connector failure as resolved.

### H. Multi-Branch Operations
- `GET /admin/branches`: Lists branches for the clinic tenant.
- `POST /admin/branches`: Creates new branch location (address, phone, coordinates, config).
- `PUT /admin/branches/{branch_id}`: Updates branch details.
- `DELETE /admin/branches/{branch_id}`: Removes branch (verifies no active appointments or unassigned doctors).
- `GET /admin/branches/{branch_id}/doctors`: Lists doctors assigned to a branch.
- `POST /admin/branches/{branch_id}/doctors`: Assigns doctor to branch.
- `DELETE /admin/branches/{branch_id}/doctors/{doctor_id}`: Removes doctor from branch.

### I. Analytics, Insights & AI Usage
- `GET /admin/stats`: Dashboard summary (today's appointments, completed, pending, revenue, new patients).
- `GET /admin/insights`: Detailed operational trends (cancellations, peak booking hours, no-show rates).
- `GET /admin/insights/summary`: Cached AI executive operational summary.
- `POST /admin/insights/summary/generate`: On-demand LLM generation of management insight bullet points.
- `GET /admin/ai/usage`: Token consumption, request count, and expenditure breakdown from `ai_usage_ledger`.
- `GET /admin/ai/budget`: Current monthly AI spend against configured monthly soft/hard cap.
- `GET /admin/messaging-usage`: Meta WhatsApp conversation volume and estimated utility charges.

---

## 6. PLATFORM OWNER / SUPER-ADMIN ENDPOINTS (`/platform/*`)

Protected by `verify_owner_credentials` (checks `OWNER_USERNAME` / `OWNER_PASSWORD`).

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/platform/overview` | Cross-tenant metrics: total clinics, active subscriptions, platform revenue, gross appointments. |
| `GET` | `/platform/clinics` | Lists all registered clinic tenants with plan, billing status, and message volume. |
| `POST` | `/platform/clinics` | Provisions a new clinic tenant, initializes default branches, and provisions initial admin user. |
| `GET` | `/platform/clinics/{clinic_id}` | Detailed tenant profile, configuration, and feature flags. |
| `PATCH` | `/platform/clinics/{clinic_id}/features` | Enables/disables individual modular features (`lab_reports`, `payments`, `ai_booking`, `marketing`). |
| `POST`, `PATCH` | `/platform/clinics/{clinic_id}/ai-budget` | Updates monthly AI dollar spending cap for a tenant. |
| `DELETE` | `/platform/clinics/{clinic_id}` | Soft/hard delete with cascading cleanup. |
| `GET` | `/platform/clinics/{clinic_id}/deletion-preview` | Pre-flight impact assessment (counts appointments, patients, and financial records affected). |
| `GET` | `/platform/clinic-admins` | Lists admin credentials across all clinic tenants. |
| `POST` | `/platform/clinic-admins` | Direct creation of tenant administrator. |
| `PUT` | `/platform/reset-admin-password` | Emergency password reset for any clinic administrator. |
| `GET` | `/platform/revenue` | Aggregated subscription revenue, platform fees, and transaction volume. |
| `GET` | `/platform/subscriptions` | Subscription status, renewal dates, and delinquent accounts. |
| `POST` | `/platform/clinics/{clinic_id}/renew` | Manually extends clinic subscription validity. |
| `GET` | `/platform/finance` | Financial dashboard: margin calculation, API provider expenses (Meta, OpenRouter, Supabase). |
| `GET` | `/platform/finance/expenses` | Manual entry of infrastructure costs. |
| `POST` | `/platform/finance/expenses` | Record platform expenditure. |
| `POST` | `/platform/finance/invoices/generate` | Generates monthly billing invoice for a clinic tenant. |
| `POST` | `/platform/broadcasts` | Schedules cross-tenant or multi-clinic WhatsApp system broadcast. |

---

## 7. FHIR R4 INTEROPERABILITY ENDPOINTS (`/fhir/*`)

Provides standard HL7 FHIR R4 JSON payloads (`application/fhir+json`):

| Method | Path | Auth | Scoping | FHIR Resource |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/fhir/Patient/{phone}` | Basic Auth | `clinic_id` | `Patient` resource with Telecom, Name, Identifier, Organization reference. |
| `GET` | `/fhir/Appointment/{booking_ref}` | Basic Auth | `clinic_id` | `Appointment` resource with status, participant (Patient, Practitioner), period. |
| `GET` | `/fhir/DiagnosticReport/{report_id}` | Basic Auth | `clinic_id` | `DiagnosticReport` with conclusion (AI summary), presentedForm (base64 PDF link). |
| `GET` | `/fhir/Patient/{phone}/everything` | Basic Auth | `clinic_id` | `Bundle` (type `searchset`) containing Patient, Appointments, and DiagnosticReports. |

---

## 8. CONNECTOR & INTEGRATION INGESTION ENDPOINTS

### `POST /internal/integrations/lab-report`
- **File**: [`app/routers/integrations.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/integrations.py)
- **Auth**: `X-Integration-Secret` matching `settings.integration_secret` or per-clinic pinned connector key.
- **Payload**: Multipart form (`file: UploadFile`, `clinic_id`, `patient_phone`, `patient_name`, `report_name`, `external_report_id`).
- **Processing**:
  1. Checks idempotency against `external_report_id` to prevent duplicate ingestion.
  2. Runs through `LabReportService.upload_and_send()`.
  3. Returns `{"success": true, "lab_report_id": "...", "already_processed": false}`.

### `POST /internal/integrations/callmedex/process-report`
- **File**: [`app/integrations/callmedex/api/router.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/api/router.py)
- **Auth**: Bearer token + timestamp replay protection (5-minute window) + SHA256 HMAC signature.
- **Payload**: JSON `ProcessReportRequest` (`clinic_id`, `external_report_id`, `patient_phone`, `patient_name`, `file_data_base64`).
- **Processing**:
  1. Verifies DB-level idempotency on `(clinic_id, external_report_id)`.
  2. Enqueues job in CallMedex internal queue engine (`global_container.queue_engine`).
  3. Returns HTTP 200 with tracking `task_id` for asynchronous polling.

---

## 9. PUBLIC CLINIC DISCOVERY & DIRECT BOOKING (`/api/clinics`)

- **File**: [`app/routers/clinics.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/clinics.py)
- `GET /api/clinics`: Lists active clinics (requires `X-Admin-Secret`).
- `POST /api/clinics`: Provisions new clinic via JSON (requires `X-Admin-Secret`).
- `PATCH /api/clinics/{clinic_id}`: Updates clinic configuration/tier (requires `X-Admin-Secret`).
- `DELETE /api/clinics/{clinic_id}`: Soft-deactivates clinic (requires `X-Admin-Secret`).

---

## 10. ROUTE SECURITY & VULNERABILITY OBSERVATIONS

1. **Unscoped Super-Admin Matrix**: Platform owner endpoints (`/platform/*`) and `/admin` routes have strict matrix tests ensuring that a platform user cannot manipulate clinic-scoped data without explicit `clinic_id` validation.
2. **Session Hijacking Mitigation**: `admin_sessions` tokens are 32-byte hex strings. Session expiration is checked on every query.
3. **Double Webhook Defense**:
   - Meta WhatsApp inbound webhooks write to `processed_messages(message_id, clinic_id)` before queueing.
   - Razorpay webhooks verify signature and idempotently transition booking state from `pending` -> `confirmed`. Repeated webhooks do not generate duplicate appointments.
4. **CORS Isolation**: In production, `allowed_origins` is empty (same-origin only).
