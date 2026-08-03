# CallMedex Integration Checklist & Production Verification

## Prerequisites & Environment Setup
- [x] Python 3.11/3.12 runtime environment configured.
- [x] Environment configuration model (`CallMedexSettings`) defined with `CALLMEDEX_` env prefix.
- [x] Dependencies pinned in `requirements.txt` (`fastapi`, `pydantic`, `pdfplumber`, `playwright`, `pytesseract`, `pdf2image`).
- [x] System packages added to `Dockerfile` (`tesseract-ocr`, `poppler-utils`, `playwright install --with-deps chromium`).
- [x] IST Timezone (`TZ=Asia/Kolkata`) configured in container environment.

## Connector & API Checklist
- [x] Abstract connector base interface (`BaseLaboratoryConnector`) frozen and implemented.
- [x] MocDoc 10-step EMR browser connector (`MocDocConnector`) implemented.
- [x] Versioned DOM selector provider (`MocDocSelectorProviderV1` v1.0.0) grounded in reference screenshots.
- [x] Resumable job recovery checkpoints (`JobCheckpoint`: `CREATED` -> `AUTHENTICATED` -> `BARCODE_LOCATED` -> `REPORT_LOCATED` -> `PDF_DOWNLOADED` -> `VALIDATED` -> `CALLBACK_SENT`).
- [x] Internal HTTP API Router mounted at `/internal/integrations/callmedex` in `app/main.py`.
- [x] Endpoint `POST /process-report` implemented with `ProcessReportRequest` and `ProcessReportResponse`.
- [x] Endpoint `GET /health` implemented with `HealthCheckResponse`.
- [x] Endpoint `GET /jobs/{task_id}` implemented for status tracking.

## Security & Authentication Checklist
- [x] Bearer token & `X-Integration-Secret` machine-to-machine authentication implemented.
- [x] HMAC-SHA256 request and callback signature verification (`X-Signature-256`) implemented using `hmac.compare_digest`.
- [x] 5-minute (300s) timestamp anti-replay window (`X-Timestamp`) enforced.
- [x] Zero hardcoded secrets in source code; EMR credentials loaded via `SecretStr` (`mocdoc_username`, `mocdoc_password`).
- [x] Patient phone & PHI masking (`***{phone[-4:]}`) enforced across all log statements.
- [x] Local storage temporary file path sanitization (`_sanitize_part`) implemented against directory traversal (`../../etc/passwd`).

## Queue & Background Processing Checklist
- [x] Abstract task queue base interface (`BaseQueue`) implemented.
- [x] In-memory task queue driver (`InMemoryQueue`) implemented with DLQ support.
- [x] Background worker runner (`CallMedexWorkerRunner`) executing 9-step report lifecycle.
- [x] Container lifespan startup (`queue_engine.start()`) and shutdown (`queue_engine.shutdown()`) hooks wired in `app/main.py`.
- [x] Unconditional browser session context cleanup (`close_context`) implemented to prevent orphan processes.
- [x] Automated failure screenshot capture (`capture_screenshot`) enabled on task failures.

## Canonical OCR & AI Summary Engine Checklist
- [x] Real `pdfplumber` native text and table cell extraction implemented in `CanonicalOCRPipeline`.
- [x] Secondary `pytesseract` + `pdf2image` OCR fallback for scanned/image PDFs implemented.
- [x] Dynamic confidence scoring implemented (`0.99`/`0.95` for PDF text, `0.94`/`0.88` for OCR engine).
- [x] Test name normalizer (`normalize_lab_test_name`) mapping variants to canonical LOINC/local codes.
- [x] Validation & deduplication engine (`validate_and_deduplicate_tests`) detecting impossible numerical values.
- [x] Layer 1 clinical reasoning engine (`ClinicalReasoningEngine`) computing flags and abnormal findings.
- [x] Layer 2 multi-audience summary generator (`MultiAudienceSummaryGenerator`) producing patient and clinician summaries.
- [x] Zero-medical-advice compliance guardrails enforced.

## End-to-End Testing & Verification Checklist
- [x] Core hospital bot test suite: **204/204 tests green**.
- [x] CallMeDex test suite: **52/52 tests green** + 1 credential-gated live sandbox test.
- [x] Regression test `test_phase_r1_bugs.py` verifying real HTTP dispatch, 500 error handling, and path sanitization.
- [x] Regression test `test_phase4_5_browser_validation.py` verifying DOM stability and PDF integrity.
- [x] Regression test `test_phase5_canonical_ocr.py` verifying PDF fixture extraction and dynamic confidence.
- [x] Regression test `test_api_router.py` verifying Bearer auth, HMAC signatures, replay windows, and API routes.
- [x] Credential-gated integration test `test_mocdoc_live_sandbox.py` configured for staging validation.

## Production Readiness Checklist
- [x] All R1-R4 code modifications confined strictly to `app/integrations/callmedex/` and sanctioned exceptions (`Dockerfile`, `requirements.txt`, `app/main.py`).
- [x] Frozen Phase 2 contracts preserved without regressions.
- [x] Complete operational runbook updated in `docs/operations/RUNBOOK.md`.
- [x] Full security model documented in `docs/security/SECURITY_MODEL.md`.
- [x] Full API reference documented in `docs/api/API_REFERENCE.md`.
- [x] Full testing strategy documented in `docs/testing/TESTING_STRATEGY.md`.
