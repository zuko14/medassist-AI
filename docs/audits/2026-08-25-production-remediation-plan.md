# Kriya AI / MediAssist AI — Production Remediation Master Plan

**Date:** 2026-08-25  
**Baseline Audit Score:** 42 / 100  
**Baseline Launch Gates Passed:** 0 / 15  
**Baseline Status:** BLOCKED (5 P0 Blockers, 8 P1 Findings)  
**Target Score:** >90 / 100  
**Target Launch Gates:** 15 / 15 Passed  
**Program Model:** Controlled, Phase-by-Phase Remediation with Continuous Evidence-Based Verification  

---

## 1. Execution Protocol & Phase Governance

Every phase strictly follows the 9-step remediation cycle:
1. **STEP A — Baseline:** Read audit findings, examine source code, trace downstream callers, record test baseline.
2. **STEP B — Threat / Failure Analysis:** Document exact failure mechanisms, race conditions, tenant leakage risks, and regression vectors.
3. **STEP C — Implementation:** Minimal, surgical, non-destructive implementation of the phase scope only.
4. **STEP D — Targeted Verification:** Unit, integration, adversarial, concurrency, and real-PostgreSQL tests.
5. **STEP E — Regression Verification:** Full test suite execution ensuring 0 unexpected regressions.
6. **STEP F — Cross-Verification:** Independent adversarial review ("How could this still fail?").
7. **STEP G — Re-Verification:** Execution of original motivating failure scenario plus adjacent edge cases.
8. **STEP H — Score Update:** Phase report, finding transition tracking, and risk ledger update.
9. **STEP I — Gate:** Pass/Block criteria evaluation before advancing to the next phase.

---

## 2. Phase Execution Roadmap & Gate Ledger

| Phase # | Phase Title | Primary Focus & Deliverables | Findings In Scope | Gate Status |
|---|---|---|---|---|
| **Phase 0** | **Real PostgreSQL Verification Foundation** | Real Postgres test harness, migration runner `scripts/migrate.py`, `schema_migrations` tracking, real-DB invariant tests. | Foundation for all invariants | **PENDING** |
| **Phase 1** | **P0 Payment State / Refund Integrity** | Add `refund_reason` column, write valid `refunded` status, return 500 on unhandled webhook exceptions. | P0-1, P1-8 | **PENDING** |
| **Phase 2** | **P0 Tenant Isolation & Query Scoping** | Repository-level `scoped()` query builder, tenant boundaries, defense-in-depth isolation. | P0-5, P2-3 | **PENDING** |
| **Phase 3** | **P0 Wrong-Patient Report Delivery** | Fail-closed patient matching (`needs_review`, `is_safe_to_send=False`) on DB errors. | P0-4 | **PENDING** |
| **Phase 4** | **P0 Admin Refund Tenant Security** | Tenant-scoped admin refund, clinic-specific Razorpay credentials, deterministic idempotency key. | P0-3 | **PENDING** |
| **Phase 5** | **P1 Webhook / DLQ / Reliability** | Durable message staging, post-processing `last_processed_message_id`, DLQ replayer. | P1-4, P1-5, P1-6 | **PENDING** |
| **Phase 6** | **Frontend ↔ Backend Wiring Audit** | Complete 18-point trace of all admin/platform routes against `admin/index.html` & `platform.html`. | Domain 14 Audit | **PENDING** |
| **Phase 7** | **Connector Safety & Report Validation** | Real PDF content validation, server-side tenant/match validation on intake routes. | P1-1, P1-2 | **PENDING** |
| **Phase 8** | **Distributed Job / Lock / Cache Safety** | Advisory locks on APScheduler jobs, cross-process cache invalidation, proxy header support. | P2-2, P2-5, P2-6 | **PENDING** |
| **Phase 9** | **Complete Data Deletion / Retention** | End-to-end "DELETE MY DATA" verification, retention policies on unbounded tables. | Domain 13 Audit | **PENDING** |
| **Phase 10** | **Security Hardening** | Timing-safe secret comparison, plaintext password fallback elimination, input schemas, junk cleanup. | P3/P4 Security Findings | **PENDING** |
| **Phase 11** | **Payment & Booking Formal Invariants** | Multi-worker concurrent load testing (100 concurrent ops), slot/queue/payment CAS tests. | P1-7, Invariant Verification | **PENDING** |
| **Phase 12** | **AI Safety & Report Summarization** | LLM state-mutation boundary audit, hallucination defenses on patient-facing summaries. | Domain 12 Audit | **PENDING** |
| **Phase 13** | **Observability & Silent Failure Elimination** | Correlation IDs, structured metrics, threshold-based alerting, fail-closed telemetry. | Domain 10 & 11 Audit | **PENDING** |
| **Phase 14** | **Scale & Capacity Engineering** | Real benchmark harnesses (10, 100, 1000 tenant models), memory & bottleneck analysis. | Domain 15 Audit | **PENDING** |
| **Phase 15** | **Failure Injection & Recovery** | Provider outage simulations (Meta, Razorpay, Groq, Postgres, process crash). | Domain 18 Audit | **PENDING** |
| **Phase 16** | **Full End-to-End Production Simulation** | Multi-tenant comprehensive lifecycle simulation under concurrency and restarts. | All Domains | **PENDING** |
| **Phase 17** | **Independent Second Forensic Audit** | Clean-slate adversarial audit comparing baseline vs post-remediation system. | Full Rescore | **PENDING** |
| **Phase 18** | **Final Score Rebuild & Deliverables** | Final Audit Report, Runbooks, Launch Gate Report, Capacity Report, Integrity Reports. | All Deliverables | **PENDING** |

---

## 3. Baseline Tracking Ledger

- **Baseline Score:** 42 / 100
- **Baseline Open P0s:**
  - `P0-1`: Late-payment refund writes invalid status `refunded_late_payment` blocked by check constraint.
  - `P0-2`: IDOR on `/admin/lab-reports/{id}/resend` allowing cross-tenant report re-delivery.
  - `P0-3`: IDOR on `/admin/bookings/{id}/refund` allowing cross-tenant refunds with global credentials.
  - `P0-4`: Patient match gate fails open on DB error, permitting wrong-patient delivery.
  - `P0-5`: Supabase `service_role` key bypasses RLS for all app traffic with zero DB backstop.
- **Baseline Open P1s:**
  - `P1-1`: `validate_report()` is a stub logging verification without checking.
  - `P1-2`: Lab report intake trusts client-asserted match confidence and clinic_id.
  - `P1-3`: Transient Razorpay error during expiry sweep can expire a paid booking.
  - `P1-4`: DLQ `failed_messages` has no replayer and replay is blocked by Guard-1 ordering.
  - `P1-5`: Silent message drop on Supabase blip during `message_queue.acquire()`.
  - `P1-6`: Message loss on deploy/crash due to returning 200 before processing.
  - `P1-7`: Double-booking constraint is untested against real database engine.
  - `P1-8`: Razorpay webhook swallows processing errors as HTTP 200.
