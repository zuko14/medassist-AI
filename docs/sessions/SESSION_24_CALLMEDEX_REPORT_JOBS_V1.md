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

---

## Session 24b — Production audit of commit 8f68f0d (2026-09-24, same day)

Audited against the live DB (read-only), CallMedex's real submission code (`callmedex/backend/app/services/report_submission.py`, `routers/ai_reports.py`, `routers/pc_operations.py`, `workers/tasks/report_retry.py`) and prod `/health` (serving `8f68f0d`). **No CallMedex job had reached production yet** (0 `source='callmedex'` rows today), so none of the defects below had fired. Accumx's MocDoc connectors were healthy throughout (runs every 5–10 min, no errors).

| # | Defect in 8f68f0d | Real-world trigger | Fix |
|---|---|---|---|
| 1 | Unknown/absent `processing_center_id` defaulted to **Accumx** | Every patient self-upload (`ai_reports.py` sends no centre) → patient's report sent from Accumx's number and stored in Accumx's `lab_reports`. Cross-tenant PHI. | `resolve_callmedex_clinic_id` fails closed (returns `None`); accepts only the static map or a clinic with an enabled `integration_connectors` row. Patient uploads go via the CallMedex number only, never a clinic. Unknown centre → `report-failed`. |
| 2 | Idempotency only in a per-worker dict | CallMedex retries after its 20 s timeout; 2+ workers/instances → duplicate WhatsApp to the patient | Cross-worker claim in `scheduler_locks` (`callmedex_report_job:<id>`): 30-min lease, renewed to 7 days on delivery, released on failure (CallMedex's retry worker resubmits the same id). Claim-store error → 503 so CallMedex retries. |
| 3 | Sync vs background decided by `CALLMEDEX_APP_ENV` (default `development`) | If unset on Render, the whole download/OCR/WhatsApp job ran inside CallMedex's request | Background whenever `APP_ENV=production`. |
| 4 | Barcode-only jobs launched Playwright/Chromium in the web container | `pc_operations.py` submits at sample verification (report not ready) with only a barcode | Refused with `download_automation_failed`; the centre's own MocDoc connector delivers those. (Chromium in web = 2026-09-02 outage.) |
| 5 | `source_document_url` fetched unrestricted | SSRF from an external system's input | https + `*.supabase.co` (or CallMedex's own host) only, no redirects, 25 MB cap, `%PDF` check. |
| 6 | Retry after a failure could never succeed | A prior `failed`/`processing` `lab_reports` row made `upload_and_send` return "already processed" and our insert collide | Stale non-`sent` row for `(clinic, job id)` is cleared first (safe: the lease guarantees no concurrent attempt). `external_report_id` = CallMedex `report_job_id`. |
| 7 | `/notifications` returned 202 and did nothing | CallMedex would believe patients were notified | 422 `template_not_supported` (contract's code). |
| 8 | HMAC optional if `CALLMEDEX_APP_ENV` unset; bare-body signature accepted on v1 | Bare-body scheme does not bind `X-Timestamp` → replayable with a fresh timestamp | Signatures mandatory when `APP_ENV=production`; v1 routes require the timestamp-bound `X-Signature`. |
| 9 | GET status selected non-existent `lab_reports.created_at` | Always 404 after the in-memory record is gone | Uses `uploaded_at`, scoped to `source='callmedex'`. |
| 10 | Patient phone used raw | CallMedex sends `users.mobile` unnormalised | `normalize_phone` + `validate_phone`; invalid → `report-failed delivery_failed`. |

Tests: `test_callmedex_report_jobs_route.py` rewritten where it asserted the unsafe behaviour (+11 scenario tests: patient upload never touches a clinic, unknown centre fails closed, duplicate → 202 without reprocessing, claim error → 503, bare signature refused, prod runs in background, barcode-only never launches a browser, invalid phone, bad document, stale retry row cleared + clinic fallback, URL allowlist).

Still open:
- The Accumax mapping `e204185b-… → c2a14afe-…` is **unverified**: the CallMedex DB reachable from the local `.env` has 0 processing centres. Confirm the id in CallMedex's production `processing_centers`.
- `CALLMEDEX_BEARER_TOKEN` on Kriya must equal CallMedex's `MEDIASSIST_BEARER_TOKEN`; CallMedex's `MEDIASSIST_BASE_URL` (local `.env`: `http://localhost:8000`) must be Kriya's production URL on CallMedex's Render.
- If Accumax verifies samples in CallMedex AND Accumx's MocDoc connector runs, the same report can reach the patient twice (the two paths use different report ids). Decide which path owns Accumx.
- `/notifications` templates are not implemented.
