# MediAssist AI Master Implementation Prompt & Reference Specification

## Overview

This document serves as the master reference specification for the MediAssist AI platform and its isolated integrations (including CallMedex).

## Non-Negotiable Guardrail Rule

> [!IMPORTANT]
> **Strict Integration Sandboxing Rule:**
> Any change proposed or executed outside `app/integrations/callmedex/` MUST include an explicit technical justification explaining:
> 1. Why the change is strictly necessary.
> 2. How the change preserves 100% of existing Hospital Chatbot functionality without side effects.

## Architecture Scope & Isolation Standards

- The existing Hospital Chatbot and core services remain completely untouched and independent.
- All CallMedex integration code, API endpoints, workers, browser automation, connectors, storage, security policies, and AI functions must reside exclusively within `app/integrations/callmedex/`.

## Phase Definitions & Execution Strategy

- Phase 0: Environment & Structure Preparation (Complete)
- Phase 1: Gap Analysis & Technical Assessment (Complete)
- Phase 2: Interface & Schema Definition (Complete)
- Phase 3: Core Connector & Service Implementation (Complete)
- Phase 3.5: Sandbox Validation (Complete)
- Phase 4.5: Browser Automation & DOM Stability Validation (Current)
- Phase 5: OCR Pipeline & Structured Lab Value Extraction
- Phase 6: AI Medical Summary & Multi-Audience Insights
- Phase 7: WhatsApp Delivery & Template Messaging
- Phase 8: End-to-End Production Acceptance Testing


## Phase 2 Interface Freeze & Gate Criteria

> [!IMPORTANT]
> **API & Interface Freeze Mandate:**
> Public interfaces defined in Phase 2 (`BaseLaboratoryConnector`, `BaseQueue`, Configuration models, Pydantic schemas, Callback/Browser/Storage abstractions) are strictly **FROZEN** upon approval and cannot be modified during Phases 3–5 unless a genuine structural defect is found.

### Phase 2 Scope Rules:
- **Contract-Only**: Define interfaces, abstractions, domain models, exception hierarchies, and OpenAPI specs ONLY.
- **Strict Prohibitions**: NO browser logic, Playwright code, worker logic, queue execution, OCR, WhatsApp handlers, or business logic.

### Phase 2 Completion & Gate Criteria (Required before starting Phase 3):
1. Every interface compiles cleanly without syntax or import errors.
2. OpenAPI specifications (`callmedex.openapi.yaml`, `mediassist.openapi.yaml`) validate against OpenAPI 3.0.3 standards.
3. Pydantic request/response models instantiate and validate correctly.
4. Zero circular imports and zero duplicate models across packages.
5. Zero implementation code hidden inside interface methods (pure abstract contracts).
6. Existing hospital chatbot automated test suite passes 100% (`pytest tests/`).
7. Zero modifications outside `app/integrations/callmedex/` unless explicitly justified.
8. **Gate 8 (Contract Compatibility)**: 100% agreement between OpenAPI operations and Pydantic schemas for required/optional fields, enums, error responses, status codes, Bearer + HMAC auth, idempotency headers, and correlation ID handling.
9. **Gate 9 (Configuration Validation)**: All configuration settings (base URLs, tokens, secrets, queue driver, browser timeouts, storage paths, feature flags) fully declared and validated in `settings.py`.

## Phase 3 Execution Rules & Scope Boundaries

> [!IMPORTANT]
> **Dependency Freeze Rule:**
> From Phase 3 onward, NO new third-party libraries may be added unless explicitly justified (explaining why stdlib/existing dependencies are insufficient, licensing, maintenance, and security implications).

### Standardized Connector Lifecycle Sequence:
`Connector Created` ➔ `Configuration Validated` ➔ `Health Check` ➔ `Login` ➔ `Search by Barcode` ➔ `Download Report` ➔ `Validate Report` ➔ `Logout` ➔ `Dispose Resources`

### Phase 3 Scope Boundaries:
- **ALLOWED**: `MocDocConnector`, `BaseQueue` drivers (APScheduler / Redis), Callback sender, Storage provider, Browser session abstraction, Dependency injection, Structured logging (with `correlation_id` & `report_job_id`), Config loading & fail-fast validation.
- **NOT ALLOWED**: OCR, AI summaries, WhatsApp delivery, Live production credentials, Production deployment, Real patient testing.
- **Browser Automation Rule**: NEVER guess DOM selectors. If reference screenshots are incomplete, stop and request missing screenshots; do NOT infer DOM selectors.

### Production Safeguards for Phase 3:
- **Resumable Recovery Checkpoints**: `JobCheckpoint` tracking (`CREATED` ➔ `AUTHENTICATED` ➔ `BARCODE_LOCATED` ➔ `REPORT_LOCATED` ➔ `PDF_DOWNLOADED` ➔ `VALIDATED` ➔ `CALLBACK_SENT`). Tasks resume from the last completed checkpoint on retry/recovery.
- **Connector Capabilities**: `ConnectorCapabilities` model declaring capabilities (`browser_required`, `supports_barcode_search`, `supports_incremental_downloads`, `supports_multi_report`, `supports_pdf`, `supports_images`, `supports_retry`).
- **Structured Event Emission**: Emit typed events (`ReportJobCreated`, `ConnectorInitialized`, `LoginSucceeded`, `BarcodeFound`, `ReportDownloaded`, `ValidationSucceeded`, `CallbackDelivered`, `Completed`).

### Phase 3 Exit Criteria (Required before starting Phase 3.5):
1. Every connector passes its own health check.
2. Recovery checkpoints persist and resume correctly.
3. Queue startup and shutdown are 100% clean without thread or loop leaks.
4. Dependency injection container resolves all services cleanly.
5. Configuration validation fails fast on missing or invalid secrets.
6. Zero browser sessions leaked (resource disposal guaranteed via async context managers).
7. Temporary download files are automatically cleaned up post-processing.
8. Every exception maps to a defined error type in `app/integrations/callmedex/api/exceptions.py`.
9. Structured logging includes `correlation_id` and `report_job_id` on all log lines.
## Phase 3.5 Sandbox Validation Goals & Exit Criteria

### Core Validation Goals:
1. **Offline Determinism**: Execute 100% offline without internet access, live EMR portals, or real patient data.
2. **Browser Environment Validation**: Verify Playwright browser launch, executable existence, data/download directory writability, storage cleanup, and screenshot artifact generation without logging into live EMR systems.
3. **Failure Injection Scenarios**: Simulate missing configuration, invalid HMAC secrets, missing selectors, corrupted downloads, queue startup failure, browser launch failure, and storage permission errors to verify clean error handling.

### Phase 3.5 Exit Criteria (Required before starting Phase 4):
1. Browser launches and exits cleanly in sandbox environment.
2. Queue starts and stops cleanly without thread leaks.
3. Worker initializes without processing real jobs.
4. Configuration validation fails fast on missing or invalid secrets.
5. Selector provider loads successfully and versioned selectors resolve correctly.
6. Failure screenshot artifact generation succeeds.
7. Temporary storage directories are created and cleaned up.
8. Structured logging emits `correlation_id` and `report_job_id`.
9. All 7 failure injection scenarios behave as expected and map to typed exceptions.
## Phase 4.5 Browser Automation & DOM Stability Validation

### Validation Objectives:
1. **Full Workflow Validation**: Validate Login, Barcode Search, Patient Navigation, Report Discovery, PDF Download, Logout, Retry Behavior, Recovery Checkpoints, and Download Integrity (`file exists`, `%PDF` signature valid, non-zero bytes, readable, SHA256 checksum generated).
2. **DOM Stability Verification**: Re-run automation multiple times to guarantee selector stability across versioned providers.
3. **Retry & Recovery Checkpoints**: Inject simulated failures (network drop, page refresh, session expiry, unexpected popup) and verify checkpoint resumption.
4. **Timing & Performance Baselines**: Measure durations for Login, Barcode Search, Report Lookup, and PDF Download.
5. **Automated Screenshot Artifact Generation**: Capture regression screenshots for Login, Search, Patient Page, Reports Page, and Download Confirmation.
6. **Prohibitions**: NO OCR, NO AI Summary, NO WhatsApp, NO Callbacks, NO Production Rollout.

## Phase 5: Canonical OCR Pipeline & Structured Lab Extraction

### Core Architectural Safeguards & Rules:
1. **Structured Data ONLY**: The OCR pipeline extracts raw text, table structures, and numerical test results into canonical JSON. It MUST NEVER generate medical interpretations, advice, or attempt to evaluate clinical significance. Downstream AI summaries are strictly isolated to Phase 6.
2. **Canonical OCR Flow**:
   `Downloaded PDF` ➔ `File Integrity Check` ➔ `PDF Text Extraction` ➔ `OCR (Scanned PDFs)` ➔ `Section & Table Detection` ➔ `Test Name Normalization` ➔ `Result & Unit Extraction` ➔ `Reference Range & Flag Detection` ➔ `Confidence Scoring` ➔ `Validation` ➔ `Canonical JSON Output`
3. **Per-Field Extraction Metadata**: Every extracted test item includes confidence scoring (`0.0 - 1.0`), extraction source (`pdf_text` / `ocr`), page number, bounding region metadata, and canonical test code.
4. **Lab Test Name Normalization**: Normalize variant naming conventions (`Hb`, `HEMOGLOBIN`, `Hemoglobin`, `Haemoglobin`) to standardized canonical codes (`HB`, `WBC`, `RBC`, `PLATELETS`, `GLUCOSE`, `CREATININE`, `ALT`, `AST`, `TSH`, etc.).
5. **Robust PDF Validation**: Handle native digital PDFs, scanned PDFs, mixed multi-page PDFs, rotated pages, missing units, duplicate tests, impossible numeric values, and missing reference ranges without crashing.

### Canonical JSON Output Schema:
```json
{
  "report_metadata": {
    "report_id": "260700009225",
    "patient_id": "VAM-50380",
    "barcode": "260700009225",
    "processing_center_id": "visakha-multispeciality-clinics",
    "generated_at": "2026-08-02T13:03:30Z"
  },
  "tests": [
    {
      "code": "HB",
      "display_name": "Hemoglobin",
      "value": 13.6,
      "unit": "g/dL",
      "reference_range": "13.0-17.0",
      "flag": "normal",
      "confidence": 0.998,
      "source": "pdf_text",
      "page_number": 1
    }
  ]
}
```

### Phase 5 Exit Criteria:
1. Native digital PDFs parsed and normalized correctly.
2. Scanned image PDFs parsed correctly via OCR fallback.
3. Multi-page and mixed-content PDFs handled cleanly.
4. Confidence score recorded for every extracted value.
5. Normalizer maps variant test names to canonical test codes (`HB`, `WBC`, etc.).
6. Validation catches malformed data, impossible numeric values, and corrupted inputs.
7. Canonical JSON schema produced consistently.
8. Zero AI-generated summaries in Phase 5.
9. Existing browser automation tests continue to pass 100%.
10. Existing hospital chatbot test suite passes 100%.


## Phase 6: AI Medical Summary & Multi-Audience Insights Engine

### Core Architectural Refinement: Two-Layer Summary Pipeline
The summary engine MUST be split into two decoupled layers. LLMs NEVER parse raw PDF or OCR text directly—they consume ONLY `CanonicalLabReport` structured JSON:

```
Canonical JSON (CanonicalLabReport)
       ↓
Layer 1: Clinical Reasoning Layer (Abnormal/Critical categorization & confidence scoring)
       ↓
Layer 2: Summary & Language Generation Layer (Patient, Clinician & Multi-language outputs)
```

#### Layer 1: Clinical Reasoning Layer
- **Input**: `CanonicalLabReport` JSON
- **Output**: `ClinicalReasoningResult` (`abnormal_tests`, `critical_tests`, `missing_reference_ranges`, `overall_confidence_score`)
- **Rule**: Pure structured clinical reasoning ONLY. ZERO patient or doctor prose/language generation.

#### Layer 2: Summary & Language Generation Layer
- **Input**: `ClinicalReasoningResult` + `CanonicalLabReport`
- **Output**:
  - `Patient Summary`: Patient-accessible explanations with clear distinction between measured lab values and narrative text.
  - `Clinician Summary`: Concise technical summary for attending doctors.
  - `Statement Provenance`: Every generated statement tracks supporting test codes (e.g. `{"statement": "Hemoglobin level is normal.", "supported_by": ["HB"]}`).
  - `Medical Disclaimer`: Standard non-diagnostic informational disclaimer attached to every summary.
  - `Multi-Language Generator`: Supports language parameters (`en`, `hi`, `te`, `ta`).

### Clinical Safety Firewall Rules:
1. **Never Diagnose**: Summary MUST NEVER produce a diagnostic claim or medical diagnosis.
2. **Never Prescribe**: Summary MUST NEVER recommend medications or changes to existing treatments.
3. **Never Override Physician**: Summary MUST NEVER contradict or override physician guidance.
4. **Never Ignore Symptoms**: Summary MUST NEVER advise patients to ignore symptoms or delay care.
5. **Clear Measured Value Distinction**: Explicitly distinguish measured numerical lab values from generated narrative text.

### Confidence Threshold Routing:
- **`Confidence >= 0.95`**: Generate summary cleanly.
- **`0.80 <= Confidence < 0.95`**: Generate summary and flag for clinical review (`review_flagged = True`).
- **`Confidence < 0.80`**: Refuse summary generation; escalate to manual review (`status = ESCALATED`).

### Phase 6 Exit Criteria:
1. Two-layered pipeline (Clinical Reasoning ➔ Summary Generation) implemented and tested.
2. Patient-facing summary generated in plain, accessible language.
3. Clinician-facing summary generated in concise technical format.
4. Statement provenance (`supported_by`) attached to all narrative sentences.
5. Mandatory Medical Disclaimer attached to all generated summaries.
6. Clinical Safety Firewall guarantees ZERO diagnoses or prescriptions.
7. Confidence threshold routing routes high, medium, and low confidence reports accurately.
8. Multi-language support (English, Hindi, Telugu, Tamil) validated.
9. All browser, OCR, and core chatbot automated tests pass 100%.


## Phase 7: WhatsApp Delivery & Template Messaging

### Core Architectural Responsibilities:
1. **Consumer Scope**: Consumes ONLY output PDF report bytes + `MultiAudienceSummaryReport` JSON from Phase 6. ZERO raw OCR or unvalidated PDF processing.
2. **Meta WhatsApp Cloud API Template Assembly**: Formats interactive media message payload containing document header (PDF), body text (Patient summary & disclaimer), and action buttons.
3. **Signed Webhook Callback Dispatch**: Dispatches HMAC-SHA256 signed callback payload to CallMedex callback endpoint (`CallMedexCallbackHandler`).
4. **Delivery Status Tracking**: Tracks delivery states (`PENDING`, `SENT`, `DELIVERED`, `FAILED`) with exponential backoff retries.

### Phase 7 Exit Criteria:
1. Meta WhatsApp Cloud API template message assembled with PDF document header.
2. Signed webhook callback dispatched with HMAC-SHA256 signature.
3. Delivery status tracking updates state cleanly.
4. Retry policies handle simulated network drops gracefully.
5. All browser automation, OCR, AI summary, and chatbot tests pass 100%.


## Phase 8: End-to-End Production Acceptance Testing

### Complete Production Execution Chain:
`CallMedex Booking` ➔ `Barcode Issuance (260700009225)` ➔ `MocDoc EMR Login` ➔ `10-Step Browser Automation` ➔ `PDF Download & Integrity Check` ➔ `Canonical OCR Extraction` ➔ `Layer 1 & Layer 2 AI Summary Engine` ➔ `Meta WhatsApp Delivery` ➔ `HMAC Signed Webhook Callback` ➔ `CallMedex System Update`

### Phase 8 Exit Criteria & Acceptance Gates:
1. End-to-end integration flow executes from booking request to signed callback dispatch without manual intervention.
2. All 7 recovery checkpoints (`JobCheckpoint`) persist state and resume correctly under failure conditions.
3. Zero browser context or temporary storage leaks across repeated job executions.
4. HMAC-SHA256 signature verification succeeds on CallMedex webhook dispatch.
5. All CallMedex test suites (Phase 2 to Phase 8) pass 100%.
## Phase 9: Production Readiness & Architectural Governance

### Core Deliverables & Production Checklist:
1. **Operational Runbooks & Documentation**:
   - `docs/operations/RUNBOOK.md` (Connector deployment & execution procedure)
   - `docs/operations/DISASTER_RECOVERY.md` (Backup, failover & data restoration procedure)
   - `docs/operations/MONITORING.md` (Metrics, tracing, health alerts & SLAs)
   - `docs/operations/INCIDENT_RESPONSE.md` (Severity classification & escalation procedures)
2. **Universal Connector Compliance Test Suite (`test_connector_compliance_suite.py`)**:
   - Every laboratory connector (MocDoc, Crelio, CloudLIMS, etc.) MUST pass the standardized compliance contract before acceptance:
     - `login()`
     - `health_check()`
     - `search_by_barcode()`
     - `download_report()`
     - `validate_report()`
     - `logout()`
     - `cleanup()`
     - `retry()`
     - `checkpoint_resume()`
3. **Semantic Contract Versioning**:
   - CallMedex Integration Contract: `v1.0.0`
   - Future breaking interface changes: `v2.0.0`
   - Independent DOM Selector Provider versioning: `v1.0.0`






