# Implementation Phases

## Overview

This document outlines the phased lifecycle for implementing the isolated CallMedex integration within MediAssist AI.

## Phase 0: Environment & Structure Preparation
- Lock down strict directory sandboxing under `app/integrations/callmedex/`.
- Establish documentation hierarchy and baseline specs.
- Configure `.gitignore` for browser runtime artifacts.

## Phase 1: Gap Analysis & Technical Assessment
- Perform read-only audit of existing codebase against frozen architecture.
- Identify technical debt, reusable modules, and gap classification.

## Phase 2: Interface & Schema Definition
- **Scope**: Define contract interfaces and abstractions ONLY. NO business logic, NO browser/Playwright code, NO queue implementations, NO workers, NO OCR, NO WhatsApp handlers.
- **Contract Deliverables**:
  - `BaseLaboratoryConnector` abstract interface
  - `BaseQueue` queue interface
  - Configuration models & feature flags
  - Pydantic request/response schemas
  - OpenAPI route definitions (`callmedex.openapi.yaml`, `mediassist.openapi.yaml`)
  - Integration exception hierarchy
  - Shared domain models & callback contracts
  - Browser & storage abstraction interfaces
  - Selector interface definition (versioned: `v1.py`, `v2.py`, `current.py`)
- **Interface Freeze Mandate**: Once approved, Phase 2 public interfaces are frozen for Phases 3–5 to prevent downstream rewrites.
- **Gate Criteria**:
  1. Every interface compiles without error.
  2. OpenAPI specs validate.
  3. Pydantic models validate.
  4. Zero circular imports or duplicate models.
  7. Zero modifications outside sandbox (`app/integrations/callmedex/`).
  8. Gate 8 (Contract Compatibility): 100% agreement between OpenAPI specs and Pydantic schemas.
  9. Gate 9 (Configuration Validation): All settings, tokens, secrets, timeouts & flags validated in `settings.py`.



## Phase 3: Core Connector & Service Implementation
- **Dependency Freeze**: Zero new third-party libraries without explicit justification and documentation.
- **Standardized Connector Lifecycle**: `Created` ➔ `Config Validated` ➔ `Health Check` ➔ `Login` ➔ `Search Barcode` ➔ `Download Report` ➔ `Validate Report` ➔ `Logout` ➔ `Dispose`
- **Allowed Scope**: Concrete `MocDocConnector`, `BaseQueue` drivers (APScheduler/Redis), Callback sender, Storage provider, Browser session abstraction, Dependency injection, Structured logging with `correlation_id` & `report_job_id`.
- **Not Yet Allowed**: OCR, AI summaries, WhatsApp delivery, Live credentials, Production deployment, Real patient testing.
- **Browser Selector Rule**: Never guess selectors. If screenshots are missing/incomplete, stop and request them.
- **Phase 3 Exit Criteria**:
  1. Connector passes health check.
  2. Queue startup/shutdown clean.
  3. Dependency injection resolves all services.
  4. Configuration validation fails fast on missing secrets.
  5. Zero browser session leaks.
  6. Temporary files cleaned up.
  7. Exceptions map to defined error types in `exceptions.py`.
  8. Structured logging contains `correlation_id` and `report_job_id`.


## Phase 3.5: Sandbox Validation
- **Offline Determinism**: Execute 100% offline without live internet access or production credentials.
- **Browser Environment Validation**: Verify Playwright browser launch, executable existence, data/download directory writability, storage cleanup, and screenshot artifact generation without logging into live EMR systems.
- **Failure Injection Scenarios**: Test clean handling for 7 failure injections (missing config, invalid HMAC, missing selector, corrupted download, queue failure, browser failure, storage permission error).
- **Exit Criteria**:
  1. Browser launches and exits cleanly in sandbox.
  2. Queue starts/stops cleanly.
  3. Worker initializes without processing real jobs.
  4. Configuration validation fails fast on missing secrets.
  5. Selector provider loads and versioned selectors resolve correctly.
  6. Screenshot artifact generation succeeds.
  7. Storage directories are created and cleaned up.
  8. Structured logging emits `correlation_id` and `report_job_id`.
  9. All failure injection scenarios map to typed exceptions.
  10. Existing chatbot test suite passes 100%.


## Phase 4.5: Browser Automation & DOM Stability Validation
- **Validation Goals**: Validate Login, Barcode Search, Patient Navigation, Report Discovery, PDF Download (`%PDF` header, readable, SHA256 checksum), Logout, Retry Recovery, and Checkpoint Persistence.
- **Verification Requirements**: DOM stability across multiple runs, forced failure retries (network drop, session timeout), operational timing baselines (Login, Search, Download duration), automated regression screenshot generation.
- **Prohibitions**: NO OCR, NO AI Summary, NO WhatsApp, NO Callbacks, NO Production Rollout.

## Phase 5: Canonical OCR Pipeline & Structured Lab Extraction
- **Architectural Safeguard**: Structured Data ONLY. The OCR pipeline extracts raw text, table structures, and numerical test results into canonical JSON. It MUST NEVER generate medical interpretations, advice, or attempt to evaluate clinical significance.
- **Canonical Flow**: `PDF` ➔ `File Integrity Check` ➔ `PDF Text Extraction` ➔ `OCR (Scanned PDFs)` ➔ `Section/Table Detection` ➔ `Test Normalization` ➔ `Result & Unit Extraction` ➔ `Reference Range & Flag Detection` ➔ `Confidence Scoring` ➔ `Validation` ➔ `Canonical JSON Output`
- **Per-Field Metadata**: `code`, `display_name`, `value`, `unit`, `reference_range`, `flag`, `confidence` (`0.0-1.0`), `source` (`pdf_text`/`ocr`), `page_number`.
- **Test Name Normalization**: Normalize variant test names (`Hb`, `HEMOGLOBIN`, `Haemoglobin`) to standardized canonical codes (`HB`, `WBC`, `RBC`, `PLATELETS`, `GLUCOSE`, `CREATININE`, `ALT`, `AST`, `TSH`, etc.).
- **Validation Rules**: Catch missing units, duplicate tests, impossible numeric values, missing reference ranges, corrupted PDFs, multi-page reports, rotated pages, scanned vs digital PDFs.
- **Prohibition**: NO AI-generated summaries in Phase 5.
- **Phase 5 Exit Criteria**:
  1. Native digital PDFs parsed and normalized correctly.
  2. Scanned image PDFs parsed correctly via OCR.
  3. Multi-page and mixed-content PDFs handled cleanly.
  4. Confidence score recorded for every extracted value.
  5. Test name normalizer maps variants to canonical codes (`HB`, `WBC`, etc.).
  6. Validation catches malformed data and impossible numeric values.
  7. Canonical JSON schema produced consistently.
  8. Zero AI-generated summaries.
  9. Existing browser automation tests pass 100%.
  10. Existing hospital chatbot test suite passes 100%.


## Phase 6: AI Medical Summary & Multi-Audience Insights Engine
- **Two-Layer Architecture**:
  - **Layer 1 (Clinical Reasoning Layer)**: Input `CanonicalLabReport` JSON ➔ Output `ClinicalReasoningResult` (`abnormal_tests`, `critical_tests`, `confidence_score`). Zero prose text.
  - **Layer 2 (Summary & Language Generation Layer)**: Input `ClinicalReasoningResult` + `CanonicalLabReport` ➔ Output `Patient Summary`, `Clinician Summary`, `Statement Provenance`, `Medical Disclaimer`.
- **LLM Input Rule**: LLMs consume ONLY structured `CanonicalLabReport` JSON, NEVER raw PDF or OCR text.
- **Statement Provenance**: Every generated statement links to supporting test codes (e.g. `{"statement": "Hemoglobin is normal.", "supported_by": ["HB"]}`).
- **Clinical Safety Firewall**: Never diagnose, never prescribe, never override physicians, never ignore symptoms.
- **Confidence Routing**:
  - `Confidence >= 0.95`: Generate summary cleanly.
  - `0.80 <= Confidence < 0.95`: Generate summary & flag for review.
  - `Confidence < 0.80`: Refuse summary generation; escalate.
- **Multi-Language Generator**: Agnostic reasoning layer supporting `en`, `hi`, `te`, `ta`.
- **Phase 6 Exit Criteria**:
  1. Two-layered summary pipeline operational.
  2. Patient summary & Clinician summary generated.
  3. Provenance (`supported_by`) attached to all sentences.
  4. Medical disclaimer attached.
  5. Zero diagnosis or prescription claims.
  6. Confidence threshold routing active.
  7. Multi-language support verified.
  8. All browser, OCR, and chatbot tests pass 100%.


## Phase 7: WhatsApp Delivery & Template Messaging
- **Flow**: `PDF + Summary` ➔ `Meta Template Assembly` ➔ `WhatsApp Delivery` ➔ `Delivery Status Tracking` ➔ `Signed Callback Dispatch`
- **Responsibilities**:
  1. Assemble Meta WhatsApp Cloud API media template with PDF document attachment.
  2. Dispatch HMAC-SHA256 signed callback notification via `CallMedexCallbackHandler`.
  3. Track delivery lifecycle (`PENDING` ➔ `SENT` ➔ `DELIVERED`).
- **Phase 7 Exit Criteria**:
  1. WhatsApp media template payload formatted cleanly.
  2. Signed webhook callback dispatched with valid HMAC-SHA256 signature.
  3. Delivery status state machine verified.
  4. All test suites pass 100%.


## Phase 9: Production Readiness & Architectural Governance
- **Operational Documentation**:
  - `docs/operations/RUNBOOK.md`
  - `docs/operations/DISASTER_RECOVERY.md`
  - `docs/operations/MONITORING.md`
  - `docs/operations/INCIDENT_RESPONSE.md`
- **Universal Connector Compliance Test Suite**:
  - Requires all laboratory connectors (MocDoc, Crelio, CloudLIMS) to pass standard compliance tests (`login`, `health_check`, `search_by_barcode`, `download_report`, `validate_report`, `logout`, `cleanup`, `retry`, `checkpoint_resume`).
- **Semantic Contract Versioning**: `v1.0.0` (Contract frozen).

