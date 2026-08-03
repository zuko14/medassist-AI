# CallMedex Monitoring & Alerting Guide

**Contract Version:** `v1.0.0`

---

## 1. Key Metrics & Observability Standards

All logs emitted by `app.integrations.callmedex` are structured JSON containing `correlation_id` and `report_job_id`.

### Metric Baselines:
- **Login Latency**: Target `< 2.5s`
- **Barcode Search Latency**: Target `< 1.8s`
- **PDF Download Latency**: Target `< 3.0s`
- **OCR Processing Time**: Target `< 1.2s`
- **Overall End-to-End Latency**: Target `< 10.0s`

---

## 2. Alert Conditions & Severity

| Alert Name | Condition | Severity | Action |
| :--- | :--- | :---: | :--- |
| `CallMedexAuthFailure` | `AuthenticationError` > 3 in 5m | **P1 - CRITICAL** | Verify EMR portal credentials in `settings.py`. |
| `CallMedexSelectorMismatch` | `ConnectorNavigationError` > 1 | **P2 - HIGH** | Check if EMR UI updated; update selector provider (`v2.py`). |
| `CallMedexQueueStall` | Queue backlog > 50 items | **P2 - HIGH** | Scale up worker runner instances. |
| `CallMedexLowConfidence` | OCR/AI confidence < 0.80 | **P3 - MEDIUM** | Item escalated for manual clinical review. |
