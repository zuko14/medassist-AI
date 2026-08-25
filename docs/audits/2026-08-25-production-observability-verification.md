# Kriya AI — Production Observability & Alerting Verification Report

**Audit Date:** 2026-08-25  
**Verification Suite:** `tests/test_alert_verification.py`  
**Verdict:** **6 / 6 Alert Conditions Passed (100% Proven)**  

---

## 1. Alert Verification Matrix

| # | Alert Scenario | Trigger Condition | Failure Context Verified | Verification Test |
|---|---|---|---|---|
| 1 | Dead-Letter Queue Alert | `failed_messages` count > 0 in 1 hour | Admin alert includes pending count and clinic details | `test_alert_01_failed_messages_triggers_with_context` |
| 2 | Message Queue Fail-Closed | Elevated fail-closed rate (>5 events) | Alert includes count of unacquired messages and exception context | `test_alert_02_message_queue_fail_closed_triggers_alert` |
| 3 | Low OCR Confidence Gate | OCR confidence score < 0.60 | Report flagged as `needs_review` with reason logged | `test_alert_03_ocr_low_confidence_flags_needs_review` |
| 4 | Database Latency / Outage | Database latency spike / connection timeout | `/health/ready` returns HTTP 500 `status: not_ready, database: disconnected` | `test_alert_04_database_latency_outage_fails_readiness` |
| 5 | Ingest-Acquire Deadlock Guard | P0 regression invariant: ingest writes `inbound_messages`, acquire claims `processed_messages` | Message is ingested and verified acquirable | `test_alert_05_ingest_acquire_deadlock_invariant_alert` |
| 6 | Meta Webhook Error Spike | Webhook HTTP error rate > 5.0% | Alert fires with status code, endpoint, and error percentage | `test_alert_06_meta_webhook_high_error_rate_alert` |
