# CallMeDex API Reference

## Overview
This document specifies the internal HTTP API surface exposed by the CallMeDex integration subsystem mounted at `/internal/integrations/callmedex`. These endpoints are used by machine-to-machine connectors, EMR webhooks, and administrative automation runners.

---

## Authentication & Security Headers

All request endpoints under `/internal/integrations/callmedex` enforce header-based authentication, anti-replay window checks, and HMAC-SHA256 signature verification.

| Header Name | Type | Required | Description |
|---|---|---|---|
| `Authorization` | String | Conditional | `Bearer <token>` matching `CALLMEDEX_BEARER_TOKEN` |
| `X-Integration-Secret` | String | Conditional | Shared secret matching `CALLMEDEX_INTEGRATION_SECRET` |
| `X-Signature-256` | String | Optional | HMAC-SHA256 hex digest of raw request body |
| `X-Timestamp` | String | Optional | ISO-8601 UTC timestamp or Unix epoch (must be within 5-min window) |

---

## Endpoints

### 1. Process Laboratory Report

Enqueues and processes an incoming laboratory report request through the 9-step CallMedex workflow.

- **URL**: `/internal/integrations/callmedex/process-report`
- **Method**: `POST`
- **Content-Type**: `application/json`

#### Request Body
```json
{
  "clinic_id": "visakha-multispeciality-clinics",
  "connector_type": "mocdoc",
  "external_report_id": "260700009225",
  "patient": {
    "patient_phone": "+919966773300",
    "patient_name": "Mr.Ammaradi Apparao",
    "patient_mrn": "MRN-50380"
  },
  "report_name": "Complete Blood Count",
  "report_type": "Laboratory"
}
```

#### Response Body (`HTTP 200 OK`)
```json
{
  "success": true,
  "task_id": "3ecd37ca-2215-41f3-87f0-d4d7a837b0f7",
  "already_processed": false,
  "lab_report_id": null,
  "message": "Report 260700009225 processed successfully",
  "callback_delivered": true,
  "timestamp": "2026-08-03T10:14:00.000Z"
}
```

---

### 2. Subsystem Health Check

Performs diagnostic health check on MocDoc connector capabilities, queue driver status, and configuration.

- **URL**: `/internal/integrations/callmedex/health`
- **Method**: `GET`

#### Response Body (`HTTP 200 OK`)
```json
{
  "status": "healthy",
  "integration_api": true,
  "queue_status": "healthy",
  "version": "1.0.0"
}
```

---

### 3. Query Job Execution Status

Retrieves current status for an enqueued or executed report job by task tracking ID.

- **URL**: `/internal/integrations/callmedex/jobs/{task_id}`
- **Method**: `GET`

#### Response Body (`HTTP 200 OK`)
```json
{
  "task_id": "3ecd37ca-2215-41f3-87f0-d4d7a837b0f7",
  "status": "completed"
}
```

---

## HTTP Status Codes & Error Responses

| Status Code | Reason | Detail |
|---|---|---|
| `200 OK` | Success | Request accepted and processed |
| `400 Bad Request` | Bad Request | Invalid JSON body or malformed `X-Timestamp` header |
| `401 Unauthorized` | Auth Failed | Missing/invalid Bearer token, stale timestamp (>5 mins), or HMAC signature mismatch |
| `404 Not Found` | Job Not Found | `task_id` does not exist in task queue |
| `500 Server Error` | Pipeline Error | Exception during worker execution pipeline |
| `503 Service Unavailable` | Unconfigured | Integration secrets missing from environment |

#### Example Error Payload (`HTTP 401`)
```json
{
  "detail": "Request timestamp outside 5-minute replay window"
}
```

---

## cURL Example

```bash
curl -X POST https://api.callmedex.org/internal/integrations/callmedex/process-report \
  -H "Authorization: Bearer dev_bearer_token_change_in_prod" \
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
