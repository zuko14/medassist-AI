# Session 24 — CallMedex Inbound Report Jobs: Mounted `/api/v1/report-jobs` & Notifications

**Date:** 2026-09-24  
**Scope:** Resolving the final architectural bridge between CallMedex and Kriya AI — mounting `/api/v1/report-jobs`, `/api/v1/report-jobs/{report_job_id}`, and `/api/v1/notifications` on Kriya AI.

---

## 1. Context & Identified Problem

From Session 23:
> "CallMedex can't send reports to Kriya. CallMedex posts reports to `POST /api/v1/report-jobs`, and Kriya has no such route (returning HTTP 404). So no real report job ever reaches Kriya with CallMedex's job ID, and the callbacks never fire."

Furthermore, CallMedex's client (`mediassist_client.py`) signs outgoing requests using:
- Header: `X-Signature: sha256=<hex>` (not bare `X-Signature-256`)
- Message: `f"{X-Timestamp}." + query + body`
- Authentication: `Authorization: Bearer <MEDIASSIST_BEARER_TOKEN>`
- Processing Center: CallMedex's center UUID (e.g. Accumax `e204185b-fd1c-4753-9243-58715d76b51c`)

---

## 2. Changes Made

### 1. New External v1 Router: [`app/integrations/callmedex/api/v1_router.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/api/v1_router.py)
Mounted in [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/main.py):
- `POST /api/v1/report-jobs`: Accepts `CallMedexReportJobRequest`, validates auth & replay protection, returns HTTP 202 Accepted with `{"report_job_id": "...", "status": "queued"}`, and enqueues worker.
- `GET /api/v1/report-jobs/{report_job_id}`: Returns `CallMedexReportJobStatus` from in-memory cache or `lab_reports` database table (or HTTP 404 if not found).
- `POST /api/v1/notifications`: Accepts `CallMedexNotificationRequest`, returns HTTP 202 Accepted with `{"notification_id": "...", "status": "queued"}`.

### 2. Pydantic Schemas: [`app/integrations/callmedex/api/schemas.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/api/schemas.py)
Added models directly matching CallMedex's `mediassist-ai.openapi.yaml`:
- `CallMedexPatient` (`patient_id`, `phone`, `preferred_language`, `name`, `gender`, `dob`, `abha_number`)
- `CallMedexDelivery` (`channels`, `deliver_summary_to_doctor_id`, `whatsapp_phone`)
- `CallMedexReportJobRequest` (`report_job_id`, `source_type`, `source_document_url`, `booking_id`, `sample_id`, `processing_center_id`, `barcode`, `connector_type`, `patient`, `delivery`, `callback_base_url`)
- `CallMedexReportJobAccepted` (`report_job_id`, `status`)
- `CallMedexReportJobStatus` (`report_job_id`, `status`, `failure_reason`, `updated_at`)
- `CallMedexNotificationRequest`, `CallMedexNotificationAccepted`

### 3. Center Resolution: [`app/integrations/callmedex/config/processing_centers.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/config/processing_centers.py)
- Added `resolve_callmedex_clinic_id(processing_center_id)`:
  - Maps CallMedex Accumax (`e204185b-fd1c-4753-9243-58715d76b51c`) to Kriya Accumx (`c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c`).
  - Queries `clinics` and `integration_connectors` for dynamic lookups.
  - Fallback defaults to Accumx (`c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c`).

### 4. Background Worker: [`app/integrations/callmedex/workers/runner.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/workers/runner.py)
- Implemented `execute_callmedex_v1_job`:
  - Direct PDF download from `source_document_url` when present (bypassing MocDoc browser automation).
  - Barcode LIS automation fallback when `source_document_url` is not provided.
  - OCR extraction (`ocr_pipeline.process_pdf`) & clinical reasoning (`ClinicalReasoningEngine`).
  - WhatsApp delivery: Strategy 1 (CallMedex number) with fallback to Strategy 2 (Clinic number).
  - DB persistence in `lab_reports`.
  - Dispatches signed CallMedex callbacks (`report-accepted`, `report-processing`, `report-delivered` / `report-failed`).
  - In-memory fast polling status store (`get_job_status_record`, `set_job_status_record`).

### 5. Signature Verification: [`app/integrations/callmedex/api/router.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/integrations/callmedex/api/router.py)
- Enhanced `verify_callmedex_auth_and_hmac`:
  - Accepts both `X-Signature: sha256=<hex>` and `X-Signature-256`.
  - Supports both `f"{ts}." + query + body` and bare raw body formats.
  - Preserves 300s replay window and sliding replay cache.

---

## 3. Test Verification

- `tests/test_lint_unscoped_queries.py`: PASSED (0 unscoped queries).
- `app/integrations/callmedex/tests/test_callmedex_report_jobs_route.py`: PASSED (11/11 tests).
- `tests/test_admin_super_admin_scope_matrix.py`: PASSED.
- `tests/test_phase2_route_adversarial_matrix.py`: PASSED.
- `tests/test_clinical_firewall.py`: PASSED.
- `tests/test_callmedex_webhook_isolation.py`: PASSED.
- `app/integrations/callmedex/tests/test_callmedex_bidirectional_sync.py`: PASSED.
Total: 398 passed across all matrices.
