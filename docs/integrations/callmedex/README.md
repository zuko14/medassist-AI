# CallMedex Laboratory Automation & EMR Integration

## Overview
CallMedex is Kriya AI's high-throughput diagnostic laboratory automation and report ingestion subsystem. It automates:
1. EMR/LIS browser acquisition (Playwright automation across hospital portals like MocDoc).
2. Canonical OCR table parsing (`pdfplumber` + `pytesseract` fallback).
3. Clinical reasoning and multi-audience AI layman summaries (`ClinicalReasoningEngine`).
4. Automated WhatsApp PDF and summary delivery to patients from a **single, permanently fixed CallMedex WhatsApp Number** ("The CALL Number").

---

## Authoritative Documentation & Guides

* **[CALLMEDEX_KRIYA_CONNECTION_GUIDE.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/integrations/callmedex/CALLMEDEX_KRIYA_CONNECTION_GUIDE.md)**: **The complete technical connection guide.** Covers the single-number dispatch mechanism, 9-stage pipeline, database overrides (`callmedex_whatsapp_settings`), Meta Graph API v22.0 integration, dual-strategy routing (Primary vs Kriya fallback), and a 7-point troubleshooting runbook.
* **[INTEGRATION_CHECKLIST.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/integrations/callmedex/INTEGRATION_CHECKLIST.md)**: Production verification checklist covering dependencies, security, queueing, and OCR test suites.
* **[callmedex.openapi.yaml](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/integrations/callmedex/callmedex.openapi.yaml)**: Complete OpenAPI 3.0 specification for internal CallMedex endpoints.
* **[supabase_schema_callmedex.sql](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/integrations/callmedex/supabase_schema_callmedex.sql)**: Database schema migration for CallMedex queue, audit, and credential tables.

---

## Directory Layout in Source Code

* `app/integrations/callmedex/api/`: HTTP API Router (`/internal/integrations/callmedex`), authentication, HMAC-SHA256 verification, and sliding replay protection.
* `app/integrations/callmedex/workers/`: `CallMedexWorkerRunner` and `CallMedexContainer` implementing the 9-step report lifecycle.
* `app/integrations/callmedex/browser/`: Playwright browser session management and artifact capture.
* `app/integrations/callmedex/ocr/`: `CanonicalOCRPipeline` parsing PDF tables and test values.
* `app/integrations/callmedex/ai/`: Two-layer clinical reasoning engine and multi-audience summary generator.
* `app/integrations/callmedex/whatsapp/`: `WhatsAppDeliveryService` handling Meta Cloud API dispatch through the single platform number.
* `app/integrations/callmedex/config/`: Configuration settings and processing center directory.
