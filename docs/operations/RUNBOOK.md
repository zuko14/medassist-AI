# CallMedex Integration Production Operations Runbook

**Contract Version:** `v1.0.0`  
**Subsystem:** CallMedex Integration Subsystem (`app/integrations/callmedex/`)

---

## 1. Subsystem Architecture Overview

The CallMedex integration subsystem handles automated retrieval, OCR extraction, AI multi-audience summary generation, and WhatsApp delivery of laboratory report PDFs from EMR portals (e.g. MocDoc).

### High-Level Workflow:
`CallMedex API Request` ➔ `Queue Enqueue` ➔ `Worker Runner` ➔ `MocDoc Browser Connector` ➔ `PDF Download` ➔ `Canonical OCR Pipeline` ➔ `Layer 1 Reasoning & Layer 2 AI Summary Engine` ➔ `Meta WhatsApp Delivery` ➔ `Signed Callback Dispatch`

---

## 2. Environment & Configuration Setup

Verify all environment variables declared in `app/integrations/callmedex/config/settings.py`:

```env
CALLMEDEX_APP_ENV=production
CALLMEDEX_MEDIASSIST_BASE_URL=https://api.callmedex.org
CALLMEDEX_CALLMEDEX_CALLBACK_URL=https://api.callmedex.org/webhooks/report-status
CALLMEDEX_INTEGRATION_SECRET=<prod_integration_secret>
CALLMEDEX_HMAC_SIGNATURE_SECRET=<prod_hmac_signature_secret>
CALLMEDEX_BEARER_TOKEN=<prod_bearer_token>
CALLMEDEX_MOCDOC_USERNAME=<prod_mocdoc_username>
CALLMEDEX_MOCDOC_PASSWORD=<prod_mocdoc_password>
CALLMEDEX_QUEUE_BACKEND=memory
CALLMEDEX_BROWSER_HEADLESS=true
CALLMEDEX_BROWSER_TIMEOUT_MS=30000
CALLMEDEX_BROWSER_NAVIGATION_TIMEOUT_MS=60000
```

---

## 3. Subsystem Health & Monitoring Metrics

### Health Check Endpoint:
```bash
curl -X GET https://api.callmedex.org/internal/integrations/callmedex/health
```

### Operational Monitoring Metrics:
Log events emit structured key-value telemetry for log aggregators (e.g., CloudWatch, Datadog):

1. **Login Failure Rate**:
   - Indicator: `logger.error("MocDoc portal authentication failed...")`
   - Threshold: > 2% of attempts over 15 mins indicates credential issue or CAPTCHA trigger.
2. **Selector-Not-Found Rate (DOM Drift)**:
   - Indicator: `ConnectorNavigationError` logged at Checkpoint `AUTHENTICATED` or `BARCODE_LOCATED`.
   - Threshold: > 1 failure indicates MocDoc portal UI markup drift. Requires updating `v1.py` selectors.
3. **OCR Average Confidence Per Report**:
   - Indicator: `CanonicalOCRResult` `overall_confidence` telemetry.
   - Threshold: Average < 0.85 indicates low-quality PDF uploads or scanner degradation.
4. **Callback Delivery Failure Rate**:
   - Indicator: `logger.warning("Callback HTTP {status}...")` or `callback_delivered=False`.
   - Threshold: > 5% indicates webhook receiver downtime or network instability.

---

## 4. Connector Compliance Verification

Before accepting a new connector software type (e.g., MocDoc, Crelio, CloudLIMS), execute the Universal Connector Compliance Suite:
```bash
python -m pytest app/integrations/callmedex/tests/test_connector_compliance_suite.py -v
```

---

## 5. Manual Replay & Recovery Procedures

If a report job fails at Checkpoint 5 (PDF Downloaded) or requires manual re-triggering:
1. Locate report barcode (e.g., `260700009225`).
2. Re-trigger processing via HTTP API:
```bash
curl -X POST https://api.callmedex.org/internal/integrations/callmedex/process-report \
  -H "Authorization: Bearer <prod_bearer_token>" \
  -H "X-Timestamp: 2026-08-03T10:14:00Z" \
  -H "Content-Type: application/json" \
  -d '{
    "clinic_id": "visakha-multispeciality-clinics",
    "connector_type": "mocdoc",
    "external_report_id": "260700009225",
    "patient": {
      "patient_phone": "+919966773300",
      "patient_name": "Mr.Ammaradi Apparao"
    },
    "report_name": "Mantoux Test",
    "report_type": "Laboratory"
  }'
```

3. Query job execution status:
```bash
curl -X GET https://api.callmedex.org/internal/integrations/callmedex/jobs/<task_id>
```
