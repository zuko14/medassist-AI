# Session 20 — Production Audit: Payments, Refunds, Branch Scoping, Scheduler & Agent Knowledge Base

**Date:** 2026-09-23  
**Branch:** `session-20-production-audit` → merged to `main`  
**Commits:** `fd15297`, `532c41a`, `01a2edd` (3 commits, 37 files changed, +3,598 / -100)  
**Migration:** None (all changes are code-only)

---

## 1. Scope & Problem Statement

A full-stack production forensic audit of the payment pipeline, slot booking, scheduler reminders, branch-pinned staff isolation, and check-in flows. Session 20 discovered and fixed **13 distinct bugs** — some of which silently swallowed money, leaked cross-branch data, or broke time-based scheduling across midnight boundaries.

### Non-Negotiable Invariants Enforced:
1. **A failed refund is never recorded as refunded.** Previously, `initiate_refund` could fail at Razorpay but the booking would still be marked `refunded`, causing the clinic to believe the patient was reimbursed when they weren't.
2. **Admin/doctor-initiated cancellations bypass patient cancellation cutoff.** When a doctor takes leave and their bookings are cancelled, the patient's "cancel X hours before" policy must not prevent the refund — the clinic broke the contract, not the patient.
3. **Branch-pinned staff see only their branch's bookings.** Before this fix, a staff member assigned to Branch A could view and act on Branch B's appointments.
4. **A cancelled booking must never be checked in.** A stale admin panel page could POST a check-in on a booking the patient had already cancelled on WhatsApp, giving the cancelled booking a live token and messaging the patient.
5. **"Refund failed" messages quote the clinic's phone number, not the platform's.** Multi-tenancy means the patient must always reach *their* clinic.
6. **Concurrent confirm/reject on `pending_review` bookings is CAS-guarded.** Two admins acting simultaneously on the same booking can no longer double-confirm or confirm-after-reject.

---

## 2. Bugs Found & Fixed

### A. Payment Pipeline (`app/services/payment.py`)

| # | Bug | Impact | Fix |
|:---|:---|:---|:---|
| 1 | **Failed refund still marked as refunded** | Clinic thinks patient was refunded; money stays with clinic | Refund-failed path now: alerts admin via WhatsApp, does NOT update booking status to `refunded`, sends patient "contact clinic" message |
| 2 | **Patient cancellation cutoff applied to admin/doctor cancellations** | Doctor takes leave → patient can't be refunded because slot is < X hours away | Added `enforce_window: bool = True` parameter; admin/doctor paths pass `False` |
| 3 | **Late payment on cancelled booking: no alert** | Payment captured for a booking already cancelled → money silently held | New detection: if `payment_id != booking["payment_id"]` on terminal state → admin WhatsApp alert with payment ID and amount |
| 4 | **"Refund needs manual review" message used platform phone** | Multi-tenant violation: patient told to call wrong clinic | `_clinic_contact_phone(clinic)` always resolves to the tenant's own number |
| 5 | **`_parse_slot_datetime` broke on "HH:MM:SS" time format** | Postgres TIME column returns `"HH:MM:SS"` but parser expected `"HH:MM"` → always returned `None` → cancellation window silently disabled | Fixed: `str(time_str)[:5]` truncates before parsing |
| 6 | **Concurrent confirm + reject on `pending_review`** | Two admins click simultaneously: confirm wins, but reject already refunded → patient is booked with no payment | CAS guard: `UPDATE ... SET status='confirmed' WHERE status='pending_review'` returns 0 rows if reject won |

### B. Slot Booking (`app/database.py`)

| # | Bug | Impact | Fix |
|:---|:---|:---|:---|
| 7 | **`pending_review` bookings not counted as slot-holders** | Slot picker offered a slot held by `pending_review` → patient got `slot_taken` on confirmation → retry offered same slot → infinite loop | Added `pending_review` to the `IN` filter alongside `confirmed` and `pending_payment` |
| 8 | **Cancelled booking could be checked in** | Stale admin page POST check-in on cancelled booking → patient gets token number and WhatsApp message | `_NOT_CHECKABLE_IN` frozenset blocks check-in for cancelled/refunded/expired/pending_payment/pending_review/no_show |

### C. Scheduler (`app/services/scheduler.py`)

| # | Bug | Impact | Fix |
|:---|:---|:---|:---|
| 9 | **2-hour reminder broke across midnight** | Comparing `"HH:MM"` strings: `"23:30" <= "01:30"` is false → no reminders for slots between 22:00–02:00 | Full datetime comparison using `datetime.combine()` with proper date handling for cross-midnight windows |
| 10 | **Half-day leave cancelled ALL bookings** | `leave_type` was only checked for `"full"` → half-morning/half-evening leave used the same "cancel everything" path | New logic: `doctor_session_slots(doc, session)` returns exactly which slots belong to morning vs. evening; only those slots are cancelled |
| 11 | **Doctor leave cancelled paid bookings without refund** | Status flipped to `cancelled` with no refund call → clinic kept the money | Now calls `initiate_refund(enforce_window=False)` first; if refund fails, alerts admin via WhatsApp with the payment ID |

### D. Branch Staff Isolation (`app/routers/admin.py`)

| # | Bug | Impact | Fix |
|:---|:---|:---|:---|
| 12 | **Branch-pinned staff could see all branches' bookings** | Staff at Branch A saw Branch B's queue, appointments, and analytics | `restrict_to_branch(query, branch_id)` + `_staff_branch(user)` helper; applied to appointment lists, queue, analytics, and check-in |
| 13 | **Branch-pinned staff could act on other branches' bookings** | Staff at Branch A could confirm/reject/cancel Branch B's appointments | `_enforce_booking_branch()` raises HTTP 403 if pinned staff targets wrong branch |

---

## 3. New Helper Functions

| Function | File | Purpose |
|:---|:---|:---|
| `restrict_to_branch(query, branch_id)` | `app/database.py` | Limits appointment queries to one branch + branch-less rows (UUID-validated) |
| `doctor_session_slots(doc, session)` | `app/database.py` | Single source of truth for which "HH:MM" slots belong to morning vs. evening session |
| `_staff_branch(user)` | `app/routers/admin.py` | Returns branch_id for pinned staff, None for clinic_admin/super_admin |
| `_enforce_booking_branch(user, clinic_id, booking_id)` | `app/routers/admin.py` | 403 guard preventing cross-branch actions by pinned staff |
| `_clinic_contact_phone(clinic)` | `app/services/payment.py` | Resolves the tenant-correct phone number for patient-facing messages |
| `_notify_late_payment_refund_failed(booking, clinic)` | `app/services/payment.py` | Tells patient their refund failed and provides clinic contact |

---

## 4. Agent Knowledge Base Created

Session 20 created the complete `docs/agent-context/` knowledge base (16 documents):

| Document | Lines | Purpose |
|:---|:---|:---|
| `00-REPOSITORY-OVERVIEW.md` | 145 | System identity, directory layout, authoritative vs. obsolete files |
| `01-ARCHITECTURE.md` | 159 | Multi-process topology, `sb()` thread pool, HTTP/1.1 patch |
| `02-SYSTEM-FLOWS.md` | 409 | 15 end-to-end traced workflows |
| `03-DATABASE-MODEL.md` | 150 | Complete schema of all 50+ tables |
| `04-API-MAP.md` | 252 | All endpoints across `/webhook`, `/admin`, `/platform`, `/fhir` |
| `05-FRONTEND-MAP.md` | 95 | `admin/index.html` tabs, tokens, state |
| `06-AI-SYSTEM.md` | 119 | OpenRouter LLMs, clinical firewall, spend caps |
| `07-INTEGRATIONS.md` | 116 | Meta WhatsApp, Razorpay, Supabase Storage, MocDoc, ABDM |
| `08-AUTH-AND-MULTI-TENANCY.md` | 92 | Tenant resolution, session tokens, branch scoping, RBAC |
| `09-BACKGROUND-JOBS.md` | 78 | All APScheduler jobs, distributed CAS locking |
| `10-SECURITY-MODEL.md` | 94 | HMAC verification, CSP, prompt injection, DPDP Act |
| `11-DEPLOYMENT-AND-OPERATIONS.md` | 123 | Render topology, Dockerfile, lifespan pre-flights |
| `12-TESTING-AND-VERIFICATION.md` | 80 | Test suite, fixtures, AST linter |
| `13-KNOWN-ISSUES-AND-GAPS.md` | 85 | Capability status matrix, fragile boundaries |
| `14-CURRENT-STATE.md` | 29 | Documentation drift analysis |
| `15-AGENT-HANDOFF.md` | 163 | 16 architectural handoff questions |

Additionally, `AGENTS.md` was created as the root-level operational guide for AI assistants.

---

## 5. Test Infrastructure Improvements

### A. Hermetic Test Fixtures (`tests/conftest.py`)
- Added `fresh_payment_service` fixture — creates a fresh `PaymentService` per test with isolated mock state
- Added `stub_payment_tables` fixture — provides pre-configured mock tables for payment flow tests
- Prevents cross-test contamination from shared service state

### B. New Test Suites

| Suite | Tests | Coverage |
|:---|:---|:---|
| `test_session20_payment_audit_fixes.py` | 512 lines | Failed refund handling, cancelled booking payment, stray payment alerts, clinic phone in messages, pending_review slot holding |
| `test_session20_payment_e2e.py` | 202 lines | End-to-end payment capture → refund → notification flows |
| `test_session20_real_postgres.py` | 106 lines | Real PostgreSQL query pattern validation |

### C. Existing Test Fixes
- `test_forensic_audit_remediation.py` — Updated for new payment flow
- `test_lab_booking_production_fixes.py` — Adapted for `pending_review` in slot availability
- `test_openrouter.py` — Fixed mock patterns
- `test_patient_metrics_production.py` — Added branch scope tests
- `test_phase2_route_adversarial_matrix.py` — Added branch staff adversarial tests
- `test_phase4_observability_and_operations.py` — Updated scheduler test patterns

---

## 6. Other Fixes

| Area | Change |
|:---|:---|
| `app/services/analytics.py` | Branch-scoping for analytics queries; pinned staff see only their branch metrics |
| `app/services/conversation.py` | Minor cleanup for guide command handling |
| `app/services/data_retention.py` | DPDP Act compliance improvements |
| `app/services/lab_reports.py` | Branch-scoped lab report visibility |
| `app/integrations/callmedex/workers/runner.py` | Reliability fix for CallMedEx worker |

---

## 7. Deployment Checklist

1. ✅ **Merge completed:** `session-20-production-audit` → `main` (commit `01a2edd`)
2. ✅ **No migration required** — all changes are code-only
3. 📢 **Clinic notification required:** Inform clinics that patient cancellations inside their configured cutoff window are no longer refunded (patient-facing policy now enforced)
4. 📢 **Branch-pinned staff:** Admins should know their branch-scoped staff now see only their own branch's data
5. 🧪 **Razorpay test-mode check:** Verify refund API works in the deployed environment
6. 👁️ **Monitor admin WhatsApp alerts** for a few days: "REFUND FAILED" and "Payment captured for closed booking" alerts only fire when real money needs human intervention

---

**Status:** Session 20 complete. Merged and pushed to `main`. Render auto-deploying.
