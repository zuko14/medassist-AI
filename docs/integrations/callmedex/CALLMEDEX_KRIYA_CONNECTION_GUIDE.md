# CallMedex & Kriya AI: Complete End-to-End Integration & "CALL Number" Connection Guide

**Document Version:** 2.0.0  
**Target Audience:** DevOps, System Engineers, Platform Administrators, Support Engineers  
**Source Code Baseline:** `app/integrations/callmedex/`, `app/services/lab_reports.py`, `app/routers/platform.py`, `app/routers/integrations.py`

---

## 1. System Identity & The "Single Call Number" Concept

### 1.1 The Dual WhatsApp Architecture in Kriya AI
In Kriya AI, patient communication is intentionally partitioned into two operating models:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               KRIYA AI MESSAGING PLATFORM                              │
├───────────────────────────────────────────┬────────────────────────────────────────────┤
│ MODEL A: Multi-Tenant Clinic Numbers      │ MODEL B: Central "CALL" Number             │
│ (app/services/whatsapp.py)                │ (app/integrations/callmedex/whatsapp/)     │
├───────────────────────────────────────────┼────────────────────────────────────────────┤
│ • Used for: OPD Appointments, Doctor      │ • Used for: Automated Diagnostic Lab       │
│   Rosters, Leaves, Follow-ups.            │   Report Delivery across participating     │
│ • Routing: Dispatched strictly from each  │   hospitals, centers, and processing labs. │
│   individual clinic's verified WhatsApp   │ • Routing: Dispatched strictly from ONE    │
│   Number (phone_number_id).               │   PERMANENT, single WhatsApp number        │
│ • Rule: Strict NO-PLATFORM-FALLBACK to    │   platform-wide ("The CallMedex Number").  │
│   prevent cross-clinic reply pollution.   │ • Invariant: Patient receives reports from │
│                                           │   a trusted, unified diagnostic sender.    │
└───────────────────────────────────────────┴────────────────────────────────────────────┘
```

### 1.2 Where the Single "CALL Number" Lives & How it Resolves
The single CallMedex number is dynamically loaded on every dispatch with **zero restart/redeploy needed**.

Resolution Priority:
1. **Database Override (Primary):**
   * Table: `callmedex_whatsapp_settings`
   * Row: `id = 'default'`
   * Columns: `phone_number_id`, `api_token_encrypted` (Fernet-encrypted with `CONNECTOR_ENCRYPTION_KEY`).
   * Managed via Platform Super-Admin API (`GET /platform/callmedex/whatsapp-settings` and `PUT /platform/callmedex/whatsapp-settings`).
2. **Environment Variable Fallback:**
   * `CALLMEDEX_WHATSAPP_PHONE_NUMBER_ID`
   * `CALLMEDEX_WHATSAPP_API_TOKEN` (configured in `.env` / Render environment).
3. **Simulation Mode Sentinel:**
   * If token is unset, empty, or equals `"dev_whatsapp_token"` / `"change_in_prod"`, the system enters **Simulation Mode**:
   * Logs: `WhatsApp Cloud API credentials not configured/placeholder — test simulation mode active`
   * Generates synthetic Meta ID: `wmid.callmedex.sim.<uuid>` (no real Meta HTTP call is made).

---

## 2. End-to-End System Pipeline & Workflow Trace

The report pipeline runs across 9 distinct execution stages:

```
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │ STAGE 1: INGESTION TRIGGER (Lab / EMR / MocDoc Ready)                                    │
 │ • LIS / EMR signs off on patient sample / barcode.                                      │
 │ • Dispatches POST /internal/integrations/callmedex/process-report                        │
 └────────────────────────────┬─────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │ STAGE 2: INGESTION GATEWAY & SECURITY ENFORCEMENT                                        │
 │ • Auth: Bearer Token / X-Integration-Secret verified with constant-time compare.        │
 │ • Anti-Replay: 300s timestamp skew window (X-Timestamp) + Sliding Replay Cache.         │
 │ • Signature: HMAC-SHA256 (X-Signature-256) calculated over raw request body.             │
 │ • Database Idempotency: Checks lab_reports for (clinic_id, external_report_id).          │
 │ • Enqueue: Job placed into InMemoryQueue / Redis queue engine -> returns task_id.         │
 └────────────────────────────┬─────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │ STAGE 3: BACKGROUND WORKER INITIALIZATION (CallMedexWorkerRunner)                        │
 │ • Generates report_job_id (UUID4) and correlation_id.                                    │
 │ • Initializes Playwright browser context in headless mode.                              │
 │ • Resolves processing center config (base_url, clinic_slug, credentials from DB).        │
 └────────────────────────────┬─────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │ STAGE 4: EMR AUTOMATION & REPORT ACQUISITION (Playwright)                                │
 │ • Checks connector health (health_check).                                                │
 │ • Logs into MocDoc / LIS portal using Fernet-decrypted credentials.                      │
 │ • Searches by Barcode / External Report ID; polls until status is ready.                 │
 │ • Downloads raw PDF bytes into memory / temp storage.                                    │
 │ • Validates PDF header: Must begin with '%PDF' bytes (rejects HTML session errors).      │
 │ • Validates patient name/MRN match against request metadata.                             │
 └────────────────────────────┬─────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │ STAGE 5: CANONICAL OCR & CLINICAL AI REASONING                                           │
 │ • CanonicalOCRPipeline: pdfplumber extracts tabular test names, values, units, ranges.   │
 │ • Fallback OCR: pytesseract + pdf2image if text density is low (scanned document).       │
 │ • ClinicalReasoningEngine: Identifies abnormal flags (HIGH, LOW, CRITICAL).              │
 │ • MultiAudienceSummaryGenerator: Crafts a 3-bullet layman patient summary + disclaimer.  │
 └────────────────────────────┬─────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │ STAGE 6: ENCRYPTED CLOUD PERSISTENCE                                                     │
 │ • PDF bytes uploaded to Supabase Storage: bucket 'lab-reports'.                          │
 │ • Path: callmedex/{clinic_id}/{report_job_id}.pdf                                        │
 │ • Generates 24-Hour Signed URL (86400s) for Meta document download.                     │
 └────────────────────────────┬─────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │ STAGE 7: DISPATCH VIA THE SINGLE CALL NUMBER (WhatsAppDeliveryService)                   │
 │ • Fetches active credentials from callmedex_whatsapp_settings (id='default').            │
 │ • Formats WhatsApp Cloud API Template Payload (template: lab_report_summary).            │
 │ • Header: Document link pointing to Supabase Signed PDF URL (filename: 'LabReport.pdf'). │
 │ • Body Param 1: Layman AI Summary text (capped at 1024 chars, flattened whitespace).     │
 │ • Body Param 2: Standard Medical Disclaimer text.                                        │
 │ • POST https://graph.facebook.com/v22.0/{phone_number_id}/messages                      │
 └────────────────────────────┬─────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────┐
 │ STAGE 8: PERSISTENCE, AUDIT & CALLBACK                                                   │
 │ • Persists record in lab_reports: clinic_id, external_report_id, file_path, status='sent'│
 │ • Dispatches HMAC-SHA256 signed callback to callmedex_callback_url.                      │
 │ • Emits structured event logs: EVENT:WhatsAppDelivered [ReportJob | Trace].              │
 └──────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Dual Delivery Strategy: Primary vs Fallback

The worker runner in [`app/integrations/callmedex/workers/runner.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/workers/runner.py) implements a dual-strategy safety net so that a patient **never receives silence**:

```
                       Is summary_report != None
                        and pdf_url valid?
                               │
                ┌──────────────┴──────────────┐
                │ YES                         │ NO (OCR/AI failed)
                ▼                             ▼
       ┌──────────────────┐          ┌───────────────────────────────────┐
       │   STRATEGY 1     │          │            STRATEGY 2             │
       │   (Primary)      │          │       (Kriya Core Fallback)       │
       ├──────────────────┤          ├───────────────────────────────────┤
       │ Dispatches via   │          │ Calls LabReportService            │
       │ CallMedex        │          │ .upload_and_send()                │
       │ WhatsAppService  │          │ Uses Kriya's own ReportSummarizer │
       │ from the SINGLE  │          │ (Raw text extraction -> LLM).     │
       │ CALL NUMBER      │          │ Dispatches via clinic's WABA.     │
       │ using template   │          │                                   │
       │ lab_report_summary          │                                   │
       └──────────────────┘          └───────────────────────────────────┘
```

> [!WARNING]
> **Strategy 2 Critical Operational Caveat:**  
> If CallMedex falls back to Strategy 2, Kriya AI's core `LabReportService` will attempt to route via the **individual clinic's WhatsApp credentials**. If that clinic has no individual WhatsApp credentials configured, Strategy 2 will fail with `MISSING_CLINIC_WHATSAPP_CREDENTIALS`. Therefore, ensuring CallMedex's OCR & AI summarizer runs cleanly in Strategy 1 is vital for 100% single-number delivery.

---

## 4. Configuration & Credentials Specification

### 4.1 Database Configuration Table (`callmedex_whatsapp_settings`)

```sql
CREATE TABLE IF NOT EXISTS callmedex_whatsapp_settings (
    id TEXT PRIMARY KEY DEFAULT 'default',
    phone_number_id TEXT NOT NULL,
    api_token_encrypted TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    updated_by TEXT
);
```

### 4.2 Updating the Single Number via Platform API

Platform Super-Admins can update the CALL number live at runtime using HTTP Basic Auth (`OWNER_USERNAME` / `OWNER_PASSWORD`):

#### View Current Configuration:
```http
GET /platform/callmedex/whatsapp-settings
Authorization: Basic <base64(username:password)>
```
*Response:*
```json
{
  "success": true,
  "source": "database",
  "phone_number_id": "105938472910293",
  "token_set": true,
  "updated_at": "2026-09-24T10:00:00Z",
  "updated_by": "superadmin"
}
```

#### Update Number or Token (Live zero-downtime update):
```http
PUT /platform/callmedex/whatsapp-settings
Authorization: Basic <base64(username:password)>
Content-Type: application/json

{
  "phone_number_id": "105938472910293",
  "api_token": "EAAxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

### 4.3 Environment Variable Fallback (`.env`)
If no row is populated in `callmedex_whatsapp_settings`, the system reads from:

| Environment Variable | Description | Example Value |
| :--- | :--- | :--- |
| `CALLMEDEX_WHATSAPP_PHONE_NUMBER_ID` | Meta Phone Number ID for the single CALL number | `105938472910293` |
| `CALLMEDEX_WHATSAPP_API_TOKEN` | Meta System User Permanent Access Token | `EAA...` |
| `CALLMEDEX_INTEGRATION_SECRET` | Machine-to-machine shared secret for inbound calls | `sec_callmedex_m2m_...` |
| `CALLMEDEX_HMAC_SIGNATURE_SECRET` | Secret key for signing/validating request payloads | `hmac_secret_...` |
| `CALLMEDEX_BEARER_TOKEN` | Bearer token for CallMedex HTTP API | `bearer_token_...` |
| `CONNECTOR_ENCRYPTION_KEY` | Fernet 32-byte key used to encrypt/decrypt tokens in DB | `base64_32_byte_key` |

---

## 5. Meta WhatsApp Business Account (WABA) Template Requirements

The single CALL number's Meta WABA **must have this exact template pre-approved**:

* **Template Name:** `lab_report_summary`
* **Category:** `UTILITY`
* **Allowed Languages:** `en` (English), `te` (Telugu), `hi` (Hindi)
* **Header Structure:** Media $\rightarrow$ Document (accepts PDF URL)
* **Body Structure:**
  ```text
  Hello, your diagnostic test report is ready and attached above.

  Summary of your results:
  {{1}}

  {{2}}
  ```
  * `{{1}}` $\rightarrow$ AI Patient Summary (max 1024 characters).
  * `{{2}}` $\rightarrow$ Medical Disclaimer (e.g. *"This is an automated summary. Please consult your physician for medical advice."*).

> [!IMPORTANT]
> **Meta Parameter Formatting Rule:**  
> Meta API throws Error `132000` if parameters contain newlines (`\n`), tabs, or 4+ consecutive spaces. CallMedex automatically sanitizes and flattens all summary text using `re.sub(r"\s+", " ", text).strip()[:1024]` before sending.

---

## 6. Complete Troubleshooting & Diagnostic Runbook

When diagnosing delivery issues between CallMedex, Kriya AI, and Meta WhatsApp, verify these 7 failure points in order:

### Check 1: Is CallMedex stuck in "Simulation Mode"?
* **Symptom:** Reports show status "sent", but no patient ever receives a WhatsApp message.
* **Root Cause:** Token is set to placeholder (`dev_whatsapp_token`, `change_in_prod`) or unset.
* **Evidence:** Look for this log in application output:
  ```text
  WhatsApp Cloud API credentials not configured/placeholder — test simulation mode active
  wmid.callmedex.sim.XXXXX
  ```
* **Fix:** Update the database with valid Meta credentials via `PUT /platform/callmedex/whatsapp-settings`.

### Check 2: Has the Meta Access Token Expired?
* **Symptom:** Logs show `HTTP 401 Unauthorized` or `OAuthException: Error validating access token`.
* **Root Cause:** A temporary 24-hour User Access Token was saved instead of a permanent **System User Token** in Meta Business Manager.
* **Fix:** In Meta Business Suite, go to **Business Settings $\rightarrow$ System Users $\rightarrow$ Generate Token**, select permissions `whatsapp_business_messaging` and `whatsapp_business_management`, select **Never Expire**, and save the new token.

### Check 3: Is the Template Approved on the Single Number's WABA?
* **Symptom:** Meta returns `HTTP 400 Bad Request` with `(#132001) Template name does not exist`.
* **Root Cause:** The template `lab_report_summary` is approved on a clinic's account, but **not on the CallMedex single WABA account**.
* **Fix:** Create and submit `lab_report_summary` for approval inside the specific WABA account associated with `CALLMEDEX_WHATSAPP_PHONE_NUMBER_ID`.

### Check 4: Can Meta Fetch the Supabase Signed URL?
* **Symptom:** Meta returns `HTTP 400` with `Failed to download media from URL`.
* **Root Cause:** Supabase Storage `lab-reports` bucket permissions or signed URL expiration.
* **Verification:**
  1. Copy the `pdf_url` from the logs.
  2. Open the URL in an incognito browser window without any Supabase cookies.
  3. If it returns `403 Forbidden` or `Signature Expired`, check storage bucket RLS policies and ensure signed URL duration is $\ge 86400$ seconds.

### Check 5: Invalid PDF Byte Stream (%PDF Check)
* **Symptom:** Worker aborts with `INVALID_PDF: missing %PDF header`.
* **Root Cause:** The portal (MocDoc/LIS) session timed out. The downloaded file was actually an HTML login redirect page rather than a genuine binary PDF.
* **Fix:** Verify EMR credentials in `callmedex_processing_centers` or `CALLMEDEX_MOCDOC_USERNAME` / `CALLMEDEX_MOCDOC_PASSWORD`.

### Check 6: Patient Phone Number Formatting
* **Symptom:** Meta returns `(#131030) Recipient phone number not in allowed list` or invalid phone format.
* **Root Cause:** Phone number missing India country code (`91`) or containing leading zeroes/dashes.
* **Fix:** Ensure input to `/process-report` provides standard E.164 numbers (e.g. `+919876543210` or `919876543210`). CallMedex normalizes numbers, but garbage strings (e.g. `987654321` with 9 digits) will be rejected.

### Check 7: Idempotency Collisions & Duplicate Lockout
* **Symptom:** `/process-report` returns `already_processed: true` immediately without dispatching.
* **Root Cause:** A previous failed run created a row in `lab_reports` for that `external_report_id`.
* **Query to Inspect:**
  ```sql
  SELECT id, clinic_id, external_report_id, status, delivery_status, error_message, created_at 
  FROM lab_reports 
  WHERE external_report_id = '<YOUR_BARCODE_HERE>';
  ```
* **Fix:** If the report was stuck in `processing` or `needs_review` from a crash, delete or update the test row to allow re-ingestion.

---

## 7. Verification & Health Probes

### 1. Ingest Gateway Health Probe
```bash
curl -X GET "https://your-domain.com/internal/integrations/callmedex/health" \
     -H "Authorization: Bearer <CALLMEDEX_BEARER_TOKEN>"
```
*Expected Output:*
```json
{
  "status": "healthy",
  "integration_api": true,
  "queue_status": "healthy",
  "version": "1.0.0"
}
```

### 2. Task Tracking Probe
```bash
curl -X GET "https://your-domain.com/internal/integrations/callmedex/jobs/<TASK_ID>" \
     -H "Authorization: Bearer <CALLMEDEX_BEARER_TOKEN>"
```
*Expected Output:*
```json
{
  "task_id": "8f9b2a1c-...",
  "status": "completed"
}
```

### 3. Automated Test Verification
Run the regression and unit suites covering CallMedex and platform WhatsApp settings:
```bash
pytest tests/test_platform.py -k "callmedex"
pytest app/integrations/callmedex/tests/
```
All tests verify cryptographic validation, sliding window replays, template formatting, and database override priority.
