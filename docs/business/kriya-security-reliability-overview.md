# KRIYA AI — SECURITY, PRIVACY & SYSTEM RELIABILITY OVERVIEW

**Document Type:** Technical Due Diligence, Security Architecture & Reliability Engineering Specification  
**Target Audience:** Chief Information Security Officers (CISO), Chief Technology Officers (CTO), Hospital IT Directors, Healthcare Compliance Auditors  
**Compliance Standards Addressed:** India Digital Personal Data Protection (DPDP) Act 2023 · National Medical Commission (NMC) Regulations · HL7 FHIR R4 Standards  

---

## 1. Security Architecture & Threat Model

Kriya AI is designed with a defense-in-depth security model specifically engineered for healthcare operational environments. The platform establishes strict trust boundaries across all external and internal touchpoints:

```
[Untrusted Internet / Patient WhatsApp]
                  │
                  ▼ (TLS 1.3 / HTTPS)
┌─────────────────────────────────────────────────────────────┐
│  INGESTION & PERIMETER SECURITY                             │
│  - Meta X-Hub-Signature-256 HMAC Verification               │
│  - Persistent Token-Bucket Rate Limiter                     │
│  - Input Sanitization & Prompt Injection Scrubbing         │
└─────────────────────────┬───────────────────────────────────┘
                          │ (Validated Webhook Payload)
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  CORE APPLICATION TRUST BOUNDARY                            │
│  - Dynamic Tenant Scoping: resolve_tenant(clinic_id)        │
│  - Zero-LLM Deterministic Clinical Safety Firewall          │
│  - Per-Phone Async Lock (Message Serialization)             │
│  - RBAC Policy Gate (Role-Based Endpoint Protection)        │
└─────────────────────────┬───────────────────────────────────┘
                          │ (Scoped Query Execution)
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  PERSISTENCE & DATA SECURITY                                │
│  - Supabase PostgreSQL with Row-Level Security (RLS)        │
│  - Partial Unique Index Slot Anti-Collision Constraints     │
│  - Append-Only Financial (payment_events) & Admin Audit Logs│
│  - Tiered Data Retention Engine (30d Purge / 7yr Retention) │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Comprehensive Security Controls Verification

| Security Domain | Control Mechanism | Implementation Details | Status | Source Evidence |
| :--- | :--- | :--- | :--- | :--- |
| **Perimeter Verification** | Meta Webhook Signature Check | Validates `X-Hub-Signature-256` HMAC against `META_APP_SECRET` on every inbound message | **VERIFIED** | `app/routers/webhook.py:L40-L75` |
| **Payment Verification** | Razorpay Webhook HMAC Check | Computes SHA-256 HMAC of webhook payload against `razorpay_webhook_secret` | **VERIFIED** | `app/services/payment.py:L50-L110` |
| **Tenant Data Isolation** | PostgreSQL Row-Level Security | Tables enforce `clinic_id = auth.uid()` or tenant-scoped filters; prevents cross-clinic leaks | **VERIFIED** | `migrations/049_force_row_level_security.sql` |
| **Admin Authentication** | Secure Session & Password Hashing | Session tokens stored in `admin_sessions` table; passwords hashed with bcrypt | **VERIFIED** | `migrations/067_admin_sessions.sql`, `app/routers/admin.py` |
| **Role-Based Access (RBAC)**| Granular Permission Matrix | Distinguishes Super-Admin, Hospital Admin, Branch Manager, Doctor, and Receptionist roles | **VERIFIED** | `app/services/permissions.py`, `migrations/036_staff_permissions.sql` |
| **Clinical Liability Shield**| Zero-LLM Regex Firewall | Deterministic blocker intercepts medical advice, prescription, and dosage queries | **VERIFIED** | `app/services/clinical_firewall.py:L1-L358` |
| **Diagnostic Match Safety** | Fuzzy Patient Identity Scoring | Strips honorifics, token-sorts names, and verifies phone numbers before report dispatch | **VERIFIED** | `app/services/patient_match.py:L1-L340` |
| **Privacy Compliance (DPDP)**| Explicit Consent & Retention Engine| Logs consent timestamp; automatically purges 30-day chat logs while preserving 7-year medical data | **VERIFIED** | `migrations/007_data_retention.sql`, `app/services/consent.py` |
| **PII Redaction for AI** | External AI Sanitizer | Strips patient names, ages, and phone numbers before sending lab text to external LLMs | **VERIFIED** | `app/utils/pii_sanitizer.py`, `app/services/report_summarizer.py` |
| **Financial Auditability** | Append-Only Ledger | All payment transitions, captures, and refunds recorded in `payment_events` table | **VERIFIED** | `migrations/008_payments.sql`, `migrations/054_payment_events_and_slot_index.sql` |

---

## 3. Reliability & Anti-Collision Engineering

### 3.1 Slot Anti-Double-Booking Protection (ACID Concurrency)
To eliminate the critical failure mode of double-booking doctor slots during concurrent patient booking attempts, Kriya AI implements database-level ACID constraints:
* **Temporary Slot Hold:** When a patient initiates booking, a 10-minute temporary hold is placed on the requested slot.
* **PostgreSQL Partial Unique Index:** Enforced via `migrations/064_fix_slot_uniqueness_key.sql`:
  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS uq_appointments_slot_active
  ON appointments (clinic_id, doctor_id, appointment_date, appointment_time)
  WHERE status NOT IN ('cancelled', 'rejected');
  ```
* **Collision Resolution:** If two patients attempt to pay for or confirm the same slot simultaneously, the database strictly permits the first transaction to commit and cleanly raises a unique constraint error for the second, automatically triggering an alternate slot recommendation or automated refund.

### 3.2 Inbound Message Idempotency & Deduplication
* **Durable Message Ledger:** Inbound WhatsApp message IDs (`wamid`) are recorded in the `processed_messages` table within an atomic transaction.
* **Replay Attack & Network Retry Defense:** If Meta retries a webhook delivery due to transient latency, the duplicate `wamid` is detected immediately, returning HTTP 200 without executing duplicate business logic or double-sending replies.

### 3.3 Payment Reconciliation & Fail-Closed Gating
* **Integer Currency Precision:** All financial calculations and database columns operate strictly in integer paise (`₹1.00 = 100 paise`), eliminating IEEE 754 floating-point rounding errors.
* **Fail-Closed State Machine:** If a payment webhook arrives with an unverified signature, missing slot hold, or amount mismatch, the appointment state transitions to `pending_review` rather than auto-confirming, immediately alerting hospital administrative staff while preserving clinical safety.

### 3.4 Patient Report Delivery Safety Gate
* **Misrouting Defense:** Diagnostic lab reports scraped from EMRs (such as MocDoc) must pass the `PatientMatchService` before WhatsApp dispatch.
* **Fuzzy Similarity Threshold:** If the scraped name and database patient name have a similarity score below 0.70 (or if phone numbers conflict across shared family accounts), automated delivery is blocked and the report is flagged for manual supervisor sign-off.

---

## 4. Regulatory Compliance Matrix

```
┌────────────────────────┬──────────────────────────────────────────────────────────────────────────┐
│ Regulatory Requirement │ Kriya AI Technical Compliance Implementation                             │
├────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ India DPDP Act 2023    │ - Explicit opt-in consent recorded on initial WhatsApp interaction       │
│                        │ - Right to erasure endpoint (`/admin/patients/{id}/purge`)               │
│                        │ - Automated 30-day purge of conversational transcript PII                 │
│                        │ - PII redaction before external LLM processing                           │
├────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ National Medical       │ - Zero-LLM Clinical Safety Firewall blocks AI-generated medical advice  │
│ Commission (NMC) Rules │ - 7-year retention of patient medical appointment records and audit logs  │
│                        │ - Clear disclaimer that AI acts as an administrative assistant, not a MD  │
├────────────────────────┼──────────────────────────────────────────────────────────────────────────┤
│ Financial & Payment    │ - PCI-DSS scope minimized: Zero card/UPI storage on Kriya servers         │
│ Regulations (RBI)      │ - Direct integration with RBI-authorized payment aggregator (Razorpay)   │
│                        │ - Immutable audit logging of all transaction identifiers and timestamps   │
└────────────────────────┴──────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Security & SRE Verification Summary

Kriya AI has been subjected to comprehensive automated test suites (including 438 verified unit, integration, and security test cases) verifying tenant isolation, webhook signature validation, slot race-condition protection, and DPDP compliance automation.
