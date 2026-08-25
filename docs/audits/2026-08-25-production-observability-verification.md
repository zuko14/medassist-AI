# Kriya AI — Observability & Alerting Verification Report

**Audit Date:** 2026-08-25  
**Evaluated Capabilities:** Request Tracing, Prometheus Telemetry, Failure Mode Alerting, Structured Logging

---

## 1. Request Correlation ID Propagation (W5.1)
- **Middleware:** `CorrelationIdMiddleware` implemented in [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/hospital-bot/app/main.py).
- **Context Holder:** `contextvars` context manager in [`app/utils/correlation.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/hospital-bot/app/utils/correlation.py).
- **Header Threading:**
  - Incoming `X-Correlation-ID` or `X-Request-ID` is preserved.
  - New unique `cid_<uuid>` generated if missing.
  - Response headers include `X-Correlation-ID`.
  - Inbound WhatsApp webhook message IDs (`wamid`) are bound as correlation IDs across background conversation processing, booking, and payment confirmation.
- **Verification:** Verified via [`tests/test_phase4_observability_and_operations.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/hospital-bot/tests/test_phase4_observability_and_operations.py).

---

## 2. Prometheus Telemetry Endpoint (W5.2, W5.3)
- **Endpoint:** `GET /metrics` returning standard text format (`version=0.0.4`).
- **Collector:** Thread-safe in-memory `MetricsRegistry` in [`app/services/metrics.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/hospital-bot/app/services/metrics.py).
- **Exposed Operational Metrics:**
  - `kriya_inbound_messages_total{status="received|duplicate"}`: Inbound webhook message volume.
  - `kriya_dlq_depth`: Real-time gauge of failed retryable and dead-letter messages.
  - `kriya_slot_taken_total`: Counter for booking race slot conflicts.
  - `kriya_refund_failures_total`: Counter for automated refund exceptions.
  - `kriya_needs_review_total`: Counter for diagnostic reports held by the clinical match safety gate.
  - `kriya_scheduler_lock_contention_total`: Counter for skipped jobs due to active distributed lease.
  - `kriya_fail_closed_total`: Counter for database fail-closed events.

---

## 3. Proactive Alerting Triggers (W5.4, W5.5)
- **Failure Mode Probes in Scheduler ([app/services/scheduler.py](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/hospital-bot/app/services/scheduler.py)):**
  1. `alert_failed_messages`: Scans `failed_messages` dead-letter queue every 5 minutes and sends high-priority WhatsApp alerts to hospital operations.
  2. `alert_message_queue_fail_closed`: Alerts admin when database fail-closed rate is elevated (>5 events).
  3. `alert_needs_review_reports`: Monitors lab reports held in `status="needs_review"` requiring manual clinical staff sign-off.
  4. Payment signature error throttle with per-IP rate-limiting alerts to platform staff.
