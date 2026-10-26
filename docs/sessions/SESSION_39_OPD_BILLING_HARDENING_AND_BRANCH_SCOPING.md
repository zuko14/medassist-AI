# SESSION 39 — OPD Billing Hardening, Atomic Draft Save & Branch Scoping

**Date**: 2026-10-26  
**Commit**: `4a9e789` on `main`  
**Migrations**: 113, 114  
**Files Changed**: 35 (1,798 insertions, 221 deletions)

---

## Context

This session addressed three categories of production-grade hardening discovered through the prior forensic analysis (Session 36–38):

1. **OPD Billing** — double-payment and lost-update concurrency bugs.
2. **IPD & OPD Branch Scoping** — incomplete multi-branch isolation for desk/billing staff.
3. **Kriya Ketu Assistant** — missing everyday queries (collections, unpaid bills, no-show rate, lab test price lookup, small talk handling).

Plus resilience fixes for the voice gateway (TTS/STT crash → dead air).

---

## 1. Migration 113 — OPD Receipt Idempotency

**File**: [`migrations/113_opd_receipt_idempotency.sql`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/migrations/113_opd_receipt_idempotency.sql)  
**Rollback**: [`migrations/rollback/113_down.sql`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/migrations/rollback/113_down.sql)

### Problem
Payment idempotency was stored in a per-process in-memory dict (`_IDEMPOTENCY_CACHE`). Multi-worker Render deployment meant a retry landing on a different worker recorded money twice.

### Solution
- Added nullable `idempotency_key TEXT` column to `opd_receipts`.
- Added CHECK constraint: key length 8–64 chars.
- Created a **partial unique index** `uq_opd_receipts_idempotency ON (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL`.
- Removed the in-memory `_IDEMPOTENCY_CACHE` dict and `_clean_idempotency_cache()` from [`app/services/opd_billing.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/app/services/opd_billing.py).

### Frontend Change
- [`admin/index.html`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/admin/index.html): the payment button now holds a single `payAttempt` object (`{ invoiceId, key }`) that survives retries. The button is disabled while an HTTP request is in-flight, preventing double-click.

---

## 2. Migration 114 — Atomic Draft-Bill Save & Ward Branch Guard

**File**: [`migrations/114_draft_bill_save_and_ward_branch.sql`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/migrations/114_draft_bill_save_and_ward_branch.sql)  
**Rollback**: [`migrations/rollback/114_down.sql`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/migrations/rollback/114_down.sql)

### 2a. `opd_save_invoice_draft()` — Atomic Draft Save

#### Problem
Draft save was five separate writes: version check in Python (0.5s tolerance), delete lines, insert each line, update header. Two cashiers passing the check → later one silently overwrites. A failing line insert left lines missing. Deleting lines of a discounted draft broke `opd_invoices_discount_le_subtotal`.

#### Solution
Created PL/pgSQL function `opd_save_invoice_draft(p_clinic_id, p_invoice_id, p_expected_updated_at, p_items, p_discount_paise, p_discount_reason, p_notes)` that:
1. Locks the invoice row (`FOR UPDATE`).
2. Checks status = `draft` and exact `updated_at` match (no 0.5s window).
3. Clears discount → deletes old lines → inserts new lines → sets discount and notes, all in one transaction.
4. Returns the updated invoice row.

#### Application-side
- [`app/services/opd_billing.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/app/services/opd_billing.py): `save_invoice_draft()` now calls `supabase.rpc("opd_save_invoice_draft", ...)` instead of five separate queries. Maps Postgres error codes (`P0001`, `P0002`) to HTTP 409/404.

### 2b. `ipd_guard_ward()` — Ward Branch Change Guard

#### Problem
Nothing prevented moving an IPD ward to a different branch while its beds were occupied/reserved. Those stays would silently belong to the wrong branch.

#### Solution
Created trigger function `ipd_guard_ward()` on `ipd_wards BEFORE UPDATE`:
1. If `branch_id` changed, locks all beds of the ward (`FOR UPDATE`).
2. If any bed is `occupied` or `reserved`, raises `ipd_ward_in_use` (mapped to HTTP 409).

---

## 3. OPD Branch Scoping Hardening

**File**: [`app/routers/opd.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/app/routers/opd.py)

### Problem
Branch-pinned desk/billing staff could read and act on encounters, prescriptions, and invoices from other branches via direct API calls.

### Changes
- Added `_visit_in_branch()` helper: resolves the visit's branch from the appointment/encounter/prescription, calls `enforce_branch_scope(user, branch_id)`.
- Applied to 7 endpoints: `get_encounter_by_appointment`, `update_encounter_vitals`, `get_prescription`, `get_prescription_pdf`, `send_prescription_whatsapp`, `update_invoice_draft`, `create_ad_hoc_invoice`.
- `update_queue_stage`: front-desk users now checked for branch scope on the appointment they're moving.
- `call_next_queue_patient`: passes the desk user's branch so only their branch's waiting patients are called.

---

## 4. IPD Branch Scoping Hardening

**Files**: [`app/routers/ipd.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/app/routers/ipd.py), [`app/services/ipd.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/app/services/ipd.py)

### Changes
- **Ward create**: branch-pinned admin's ward defaults to their branch.
- **Ward patch**: supports `branch_id` in payload; validates destination branch ownership; only hospital-wide admin can make a ward "shared".
- **Bed assignment / reservation / transfer**: `_ward_in_branch()` now checks the destination bed's ward branch, not just the admission.
- **Observation correction**: wraps `ValidationError` properly instead of letting 500 propagate.
- **`_branch_wards()`**: new service function returns a branch's own wards + shared (branch-less) wards, used by `bed_board()` and `worklist()`.

### Admin UI
- Ward setup section now shows a branch selector dropdown per ward with a "Move" button.
- `ipd.loadBranches()`, `ipd.branchOptions()`, `ipd.moveWard()` added to the admin frontend.
- Ward creation form includes branch dropdown for multi-branch hospitals.

---

## 5. OPD Call-Next Distributed Lock

**File**: [`app/services/opd.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/app/services/opd.py)

### Problem
Two concurrent "call next" requests (double-click, two desks) both found the same `in_consultation` patient, both finished them and both called the next — skipping a patient.

### Solution
- `opd_call_next()` now acquires `distributed_job_lock(f"opd_call_next:{clinic_id}:{doctor_id}", lease_seconds=30)`.
- Second concurrent call returns HTTP 409: "Another 'call next' for this doctor is in progress."
- Query filtering by `branch_id` added (desk sees only their branch's queue).

---

## 6. Voice Gateway Resilience

**File**: [`app/voice/gateway.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/app/voice/gateway.py)

### Problem
TTS or STT crash → unhandled exception → WebSocket drops → dead air for the caller (no hang-up, no transfer).

### Changes
- **TTS failure**: wrapped `_speak()` in try/except; on failure logs `VOICE_TTS_FAILED`, records error event, sets `control = "transfer"` and signals `finished` → passthru connects reception.
- **STT failure**: `pump_stt()` now wraps `_pump_stt()`; if recognition dies mid-call, triggers `_handoff("stt_lost")` to announce to the caller and transfer. If announcement also fails, transfers silently.

---

## 7. Kriya Ketu Assistant Enrichment

**Files**: [`app/assistant/registry.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/app/assistant/registry.py), [`app/assistant/rules.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/app/assistant/rules.py), [`app/assistant/resolve.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/app/assistant/resolve.py)

### New Reads (registry.py)
| Read Key | Description |
|:---|:---|
| `opd.collections` | Today's / any date's collection summary (cash, UPI, card, outstanding) |
| `opd.unpaid` | Unpaid bills (latest 50, with balance and patient name) |
| `opd.holidays` | Upcoming clinic holidays |

### Enhanced Reads
| Read Key | Enhancement |
|:---|:---|
| `stats.summary` | Added no-show rate (computed), and "no show" trigger phrases |
| `doctors.list` | Added fee column, "what is fee" trigger phrases |
| `lab_tests.list` | Price-term slot: "price of cbc test" filters to matching tests |
| `reports.queue` | Added "pending reports" / "pending lab reports" triggers |
| `patients.search` | Added Telugu and Hindi patient-search triggers |

### Small Talk Router (rules.py)
- New `SMALL_TALK` dictionary: greetings, thanks, capabilities.
- `small_talk(text)` classifier: messages made only of filler + greetings/thanks/help → answered directly, never routed to a read/write.
- `starts_new_request(text)`: a sentence with a write verb is a new request, not a typed answer to a clarify form.
- Greeting prefix stripping: "Hi Kriya, show today's appointments" → strips "Hi Kriya" and routes the rest.

### Price-Term Resolver (resolve.py)
- `resolve_price_term(text)`: extracts the test name from price questions ("price of cbc test" → `q=cbc`), sent as a filter to the lab tests endpoint.

---

## 8. Test Coverage Added

| Test File | What It Covers |
|:---|:---|
| [`test_migration_113_opd_receipts.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/tests/test_migration_113_opd_receipts.py) | Idempotency key constraint validation |
| [`test_migration_114_draft_save_ward_branch.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/tests/test_migration_114_draft_save_ward_branch.py) | Atomic draft save + ward guard trigger |
| [`test_opd_billing_real_pg.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/tests/test_opd_billing_real_pg.py) | End-to-end OPD billing against real Postgres |
| [`test_branch_isolation_real_pg.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/tests/test_branch_isolation_real_pg.py) | Multi-branch isolation adversarial matrix |
| [`test_assistant_chat.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/tests/test_assistant_chat.py) | Small talk, greeting prefix stripping |
| [`test_assistant_reads.py`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/tests/test_assistant_reads.py) | New reads (collections, unpaid, holidays, price term) |
| Updated: `test_opd_billing.py`, `test_opd_queue.py`, `test_ipd_api_e2e.py`, `test_assistant_doctors.py`, `test_assistant_feature_gate.py`, `test_assistant_handoff.py`, `voice/test_gateway.py` | Branch scoping, voice resilience, assistant enrichment |

---

## 9. Deployment Steps

### Pre-deployment (already done in Supabase SQL editor)
Migrations 113 and 114 were run directly in the Supabase SQL editor. The following checksum registrations must be run to prevent the Render migration runner from re-applying them:

```sql
INSERT INTO schema_migrations (name, checksum) VALUES
    ('113_opd_receipt_idempotency.sql', 'cb09adee5a059d0b13d9e62f79600f95b1901e0e8bb9bb327378a2342f62fe68')
ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum;

INSERT INTO schema_migrations (name, checksum) VALUES
    ('114_draft_bill_save_and_ward_branch.sql', '21334373894b7a719a826158bbf0497279441826f9d11798cc743ad0a974db9c')
ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum;
```

### Git
```
Commit: 4a9e789
Branch: main
Push: origin/main (88e5327..4a9e789)
```

Render auto-deploys from `main`.

---

## 10. Status Doc Updates

- [`docs/opd-os/status.md`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/docs/opd-os/status.md) — marked billing idempotency and atomic draft save as done.
- [`docs/ipd-os/status.md`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/docs/ipd-os/status.md) — marked ward branch guard as done.
- [`docs/kriya-ketu/status.md`](file:///Users/chaitanyakumarmanda/Developer/medassist-AI/docs/kriya-ketu/status.md) — marked new reads and small talk as done.
