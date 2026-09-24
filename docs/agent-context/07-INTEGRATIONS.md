# 07 - EXTERNAL INTEGRATIONS & CONNECTORS

This document inventories all external service integrations, APIs, protocols, and connectors implemented in KriyaAI.

---

## 1. INTEGRATION INVENTORY & MATRIX

| Provider | Purpose | Primary Interface | Authentication | Fallback & Failure Behavior |
| :--- | :--- | :--- | :--- | :--- |
| **Meta WhatsApp Cloud API** | Inbound/Outbound patient messaging | Graph API v18.0+ REST | Bearer Token + Phone Number ID per clinic | Retries on 429/5xx with jitter; terminal fail on auth errors. Strict no-platform-fallback rule. |
| **Razorpay** | OPD booking & lab test prepayment | Razorpay Python SDK / REST | Key ID + Key Secret (per-clinic or global) | Webhook-driven idempotency; payment links expire after 30 mins; automated refund on cancellation. |
| **Supabase (PostgreSQL & Storage)** | Primary persistence & PDF report storage | PostgREST + Supabase Storage API | `SUPABASE_SERVICE_ROLE_KEY` | Off-loop thread pool execution via `sb()`; HTTP/1.1 forced session; 503 circuit-breaker on outage. |
| **OpenRouter AI** | Medical triage, FAQ, and admin intelligence | OpenAI-compatible chat REST | `OPENROUTER_API_KEY` | Multi-model fallback payload: `[deepseek/deepseek-chat, google/gemini-2.0-flash-001]`; backoff retry. |
| **MocDoc LIS / HMIS** | Automated diagnostic report scraping | Playwright headless browser | Clinic credentials encrypted via Fernet | Dedicated worker process; distributed CAS lock; dead-letter queue for failed reports. |
| **CallMedex Lab Automation** | High-throughput diagnostic ingestion | Internal REST API (`/internal/integrations/callmedex`) | Bearer Token + HMAC-SHA256 + Replay Window | In-memory/DB queue engine; DB idempotency check on `(clinic_id, external_report_id)`. |
| **ABDM / FHIR R4** | Indian national health records exchange | HL7 FHIR R4 JSON REST (`/fhir/*`) | Basic Auth (`admin_users`) | Validates ABHA numbers and addresses; returns FHIR `OperationOutcome` on error. |

---

## 2. META WHATSAPP CLOUD API

- **Service File**: [`app/services/whatsapp.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/whatsapp.py)
- **Base Endpoint**: `https://graph.facebook.com/{whatsapp_api_version}/{phone_number_id}/messages`

### Inbound Handshake & Delivery
1. Webhook Challenge: `GET /webhook` responds to Meta's hub subscription handshake.
2. Inbound Messages: `POST /webhook` validates `X-Hub-Signature-256` using `settings.meta_app_secret`.
3. Meta 20-Second SLA: Inbound webhooks are immediately recorded in `inbound_messages` and acknowledged with HTTP 200 within 15ms. Execution is dispatched asynchronously to FastAPI `BackgroundTasks`.

### Outbound Messaging & Strict Tenancy Isolation
- Function: `_get_credentials(self, clinic: dict) -> tuple[str, str]`
- **CRITICAL TENANT RULE**: Outbound messages **must only use the clinic's own WhatsApp credentials** (`meta_access_token` and `phone_number_id`).
- There is **no platform-wide fallback** in multi-tenant mode. A platform fallback would send messages from the shared platform number, causing patient replies to land in the wrong clinic's inbox. If a clinic's credentials are unconfigured, outbound sending raises `ValueError("Missing WhatsApp credentials")` and logs `MISSING_CLINIC_WHATSAPP_CREDENTIALS`.

### Message Types Supported
- Plain Text (`send_text`)
- Interactive Quick-Reply Buttons (`send_buttons`): Maximum 3 buttons per message (Meta API limit).
- Interactive Radio Lists (`send_list`): Maximum 10 rows and 10 sections; character length capped at 1024 chars (`MAX_LIST_BODY_CHARS`).
- PDF Documents (`send_document`): Used for delivering diagnostic lab reports.
- Message Templates (`send_template`): Required for business-initiated messages outside the 24-hour customer service window.

### Outbound Accounting (`outbound_message_ledger`)
- Every outbound message attempt is logged asynchronously via `message_accounting.log_outbound()`, recording message type, template name, Meta message ID (`wamid`), and delivery status.

---

## 3. RAZORPAY PAYMENT GATEWAY

- **Service File**: [`app/services/payment.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/payment.py)
- **Webhook Router**: [`app/routers/razorpay_webhook.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/razorpay_webhook.py)

### Payment Workflow
1. **Order Creation**: Calls Razorpay Orders API (`amount` in paise, `currency: INR`, `receipt: booking_ref`, `notes: {clinic_id, booking_id}`).
2. **Payment Link**: Generates short URL sent to patient on WhatsApp.
3. **Webhook Processing**:
   - Validates HMAC signature via `razorpay.utility.verify_payment_signature()`.
   - Idempotently transitions `bookings` state: `pending` -> `confirmed`.
   - Generates and schedules confirmed appointment.
4. **Refund Processing**: Initiates instant refunds for cancelled appointments or rejected booking requests via Razorpay Refunds API, updating `appointments.refund_id`.

---

## 4. MOCDOC HMIS & LABORATORY CONNECTOR

- **Worker File**: [`connectors/mocdoc/worker.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/connectors/mocdoc/worker.py)
- **Runner Entry Point**: [`connectors/runner.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/connectors/runner.py)

### Process Architecture
- Runs as an independent daemon or scheduled worker (`python -m connectors.runner --all`).
- Uses Playwright headless browser automation to log into hospital MocDoc portals.
- **Event Loop Policy**: Forces CPython's `DefaultEventLoopPolicy` (`_UnixSelectorEventLoop`) on Linux to prevent child-watcher incompatibility with Playwright when `uvloop` is installed.

### Security & Credential Encryption
- Connector passwords stored in database `connectors.config` are encrypted using Fernet symmetric encryption (`app/utils/connector_crypto.py`).
- Requires `CONNECTOR_ENCRYPTION_KEY` in environment. Startup pre-flight check validates key format.

### Distributed Concurrency Lock
- Employs table `scheduler_locks` with CAS timestamp leases (`distributed_job_lock`) ensuring multiple container instances do not scrape the same clinic portal simultaneously.

---

## 5. CALLMEDEX LAB AUTOMATION INTEGRATION

- **Directory**: [`app/integrations/callmedex/`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/)
- **Router**: [`app/integrations/callmedex/api/router.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/api/router.py)

### API Security (inbound, CallMedex → Kriya)
- Requires `Authorization: Bearer <token>` (or `X-Integration-Secret`).
- Header `X-Signature: sha256=<hex>` or `X-Signature-256`: HMAC-SHA256 over `f"{ts}." + raw_query + raw_body` (or bare raw body) using `CALLMEDEX_HMAC_SIGNATURE_SECRET`.
- Replay Attack Mitigation: Header `X-Timestamp` verified against a 300-second (5-minute) clock skew window; duplicate signatures are rejected.
- Mounted routers:
  - Internal: `POST /internal/integrations/callmedex/process-report`
  - External v1: `POST /api/v1/report-jobs`, `GET /api/v1/report-jobs/{report_job_id}`, `POST /api/v1/notifications` ([`app/integrations/callmedex/api/v1_router.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/api/v1_router.py)).

### Ingestion Flow
- Endpoint: `POST /api/v1/report-jobs` (external) or `POST /internal/integrations/callmedex/process-report` (internal).
- Direct PDF downloads supported via `source_document_url` (bypasses browser scraping when PDF is pre-signed).
- Center resolution: `resolve_callmedex_clinic_id` maps CallMedex centers (e.g. Accumax `e204185b-fd1c-4753-9243-58715d76b51c`) to Kriya clinics (`c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c`).
- Enqueues jobs to `global_container.queue_engine`.
- Background worker executes PDF extraction, patient fuzzy matching (`app/services/patient_match.py`), AI summarization (`app/services/report_summarizer.py`), and WhatsApp dispatch.
- Delivery: CallMedex number (template `lab_report_summary`) first; if no summary OR that send fails, falls back to `LabReportService.upload_and_send` on the clinic's own number. In production, unconfigured CallMedex WhatsApp credentials are a FAILED send (no simulated "delivered").
- Processing center creds: `config/processing_centers.py` prefers the clinic-wide `integration_connectors` row over branch rows and decrypts `password_encrypted`.

### Outbound (Kriya → CallMedex) — `app/integrations/callmedex/api/client.py`
- Base: `CALLMEDEX_BASE_URL` + `/api/v1/integrations/mediassist`. Blank base URL = no outbound calls.
- Signing matches CallMedex's verifier (`backend/app/middleware/mediassist_auth.py`): `Authorization: Bearer` (`CALLMEDEX_OUTBOUND_BEARER_TOKEN`, else `CALLMEDEX_BEARER_TOKEN`), `X-Timestamp` = epoch seconds, `X-Signature: sha256=hex(HMAC(ts + "." + raw_query + raw_body))`, `X-Correlation-Id`, `X-Idempotency-Key` (uuid5 of job+event, stable across retries).
- Report lifecycle callbacks (`callbacks/handler.py`): `report-accepted`, `report-processing` (background, drained before the terminal one), `report-delivered` (with `analysis` from `build_analysis_payload`), `report-failed` (mapped `failure_reason`). Sent only when the job carries CallMedex's `report_job_id`.
- WhatsApp booking (`whatsapp/booking.py`): inbound messages whose `phone_number_id` is the CallMedex number (DB `callmedex_whatsapp_settings` or `CALLMEDEX_WHATSAPP_PHONE_NUMBER_ID`, minus any id a clinic owns) skip tenant resolution and run a menu → date → window → address → confirm flow; `GET /patients/lookup` prefills address/language, `POST /whatsapp-bookings` creates the booking. State: `callmedex_booking_sessions` (migration 086), 30-min timeout, purged after 1 day.

---

## 6. AYUSHMAN BHARAT DIGITAL MISSION (ABDM) & FHIR R4

- **Validation Service**: [`app/services/abdm.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/abdm.py)
- **FHIR Router**: [`app/routers/fhir.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/fhir.py)

### ABDM Compliance
- **ABHA Number Validation**: Validates 14-digit Indian National Health ID format (`^\d{2}-\d{4}-\d{4}-\d{4}$`) and Luhn checksum.
- **ABHA Address Validation**: Validates PHR identifier format (`username@abdm`).
- **Health Facility Registry (HFR)**: Validates hospital facility IDs.
- **Degradation Mode**: When ABDM Gateway credentials (`ABDM_CLIENT_ID`, `ABDM_CLIENT_SECRET`) are unset, the system falls back to strict offline format validation without halting clinic operations.

### FHIR R4 Interoperability
- Transforms KriyaAI relational schema into standard HL7 FHIR R4 JSON resources:
  - `Patient`: Demographics, telecom, identifier, managing organization.
  - `Appointment`: Status, participants, start/end timestamps, service type.
  - `DiagnosticReport`: Status, conclusion (AI summary), presented form (PDF attachment).
  - `$everything`: Bundles all historical patient interactions for external HMIS export.
