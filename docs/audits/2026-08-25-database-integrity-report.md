# KRIYA AI — DATABASE INTEGRITY & TRANSACTIONAL INVARIANTS REPORT

**Audit Date:** 2026-08-25  
**Database Engine Tested:** Real Embedded PostgreSQL 16.2 (`pgserver`)  
**Migrations Executed:** 46 sequential migration files (001 through 046)  
**Total Invariants Verified:** **14 / 14 (100% Passing)**  

---

## 1. Migration History & Schema Integrity

All 46 database migrations have been executed in sequence against a clean PostgreSQL instance using `scripts/migrate.py`.

Key Migrations Added / Audited:
- `migrations/021_add_unique_queue_token.sql`: Enforces partial unique index `idx_unique_queue_token` on `(clinic_id, doctor_name, appointment_date, token_number) WHERE queue_status != 'cancelled'`.
- `migrations/044_fix_payment_audit_trigger.sql`: Corrected duplicate `log_payment_status_change()` plpgsql trigger function definition.
- `migrations/046_add_refund_columns.sql`: Added `refund_id`, `refund_reason`, and `refunded_at` with partial index `idx_appointments_refund_id`.

---

## 2. Invariant Verification Results (PostgreSQL 16.2 Engine)

| Invariant # | Invariant Description | PostgreSQL Mechanism | Verification Test | Status |
|---|---|---|---|---|
| **INV-01** | Appointment Unique Active Slot | Partial unique index `idx_unique_active_slot` | `test_appointment_unique_partial_index` | **PASS** |
| **INV-02** | Payment Status Constraint | `CHECK (status IN ('pending', 'confirmed', ...))` | `test_payment_status_check_constraint_rejects_arbitrary_string` | **PASS** |
| **INV-03** | Payment Events Immutability | Trigger `prevent_payment_event_mutation()` | `test_payment_events_immutability_trigger` | **PASS** |
| **INV-04** | Queue Token Uniqueness | Partial unique index `idx_unique_queue_token` | `test_queue_token_uniqueness_partial_index` | **PASS** |
| **INV-05** | Multi-Tenant Foreign Key Cascades | `REFERENCES clinics(id) ON DELETE CASCADE` | `test_multi_tenant_foreign_key_cascades` | **PASS** |
| **INV-06** | Appointment Cancellation Release | Status transition to `'cancelled'` frees slot | `test_appointment_cancellation_releases_slot_for_rebooking` | **PASS** |
| **INV-07** | Slot Hold Expiration Release | Expiration transition releases slot | `test_slot_hold_expiration_releases_slot` | **PASS** |
| **INV-08** | Payment Confirmation Locking | Transition to `'confirmed'` locks slot permanently | `test_payment_confirmation_state_transition` | **PASS** |
| **INV-09** | Refund State Transition | Canonical `'refunded'` + metadata columns | `test_refund_state_transition_valid_columns` | **PASS** |
| **INV-10** | Transaction Rollback on Failure | Atomic transaction rollback on constraint error | `test_transaction_rollback_preserves_initial_state` | **PASS** |
| **INV-11** | Concurrent Slot Racing Contention | Concurrency under 10 racing threads (1 win, 9 fail) | `test_concurrent_booking_racing_slots` | **PASS** |
| **INV-12** | Concurrent Payment Webhooks | Webhook deduplication under concurrent calls | `test_concurrent_payment_confirmation_dedup` | **PASS** |
| **INV-13** | Concurrent Expiry Worker Safety | Skip expiry on concurrent confirmation | `test_concurrent_expiration_vs_confirmation` | **PASS** |
| **INV-14** | Concurrent Cancellation / Reschedule | Atomic status transitions prevent ghost slots | `test_concurrent_cancellation_and_reschedule` | **PASS** |

---

## 3. Migration Runner Tooling

The migration runner [scripts/migrate.py](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/hospital-bot/scripts/migrate.py):
1. Verifies SHA256 checksums of applied migrations against `schema_migrations` table.
2. Prevents out-of-order execution or modified historical migrations.
3. Automatically rolls back failed migration files inside atomic transactions.
