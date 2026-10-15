# KK-P0-15 hardening checklist — OPD billing, booking refund, home collection assignment (KK-P4-15)

Audited 2026-10-15 by reading `app/routers/opd.py` (`create_appointment_invoice`, `issue_opd_invoice`,
`generate_payment_link`, `list_opd_invoices`, `get_opd_invoice`), `app/services/opd_billing.py`
(`get_or_create_appointment_invoice`, `issue_invoice`, `list_invoices`, `get_invoice`), `app/routers/admin.py`
(`admin_refund_booking`, `get_appointments`, `_annotate_refund_state`), `app/services/payment.py`
(`initiate_refund`) and `app/routers/home_collection.py` (`list_visits`, `list_phlebotomists`, `assign_visit`,
`auto_assign_pending`). Same columns as [desk.md](desk.md). Gaps → **D-37**, **D-38**.

| Endpoint | Action | Perm | Tenant | Branch | Bounds | Idempotent / CAS | Audit row | 5xx / error text | Off-loop | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| `POST /admin/opd/appointments/{id}/invoice` | `opd.invoice_create` (R2) | OPD_BILLING / FRONT_DESK / ADMIN (handler, live OPD) | ✔ `scoped_query` | ✔ since **D-38** (appointment's branch, before the write) | ✔ | ✔ get-or-create (one live invoice per visit); Kriya refuses a visit that has one | ✔ since D-38 `OPD_INVOICE_CREATE` | ✔ | ✔ | Registered |
| `POST /admin/opd/invoices/{id}/issue` | `opd.invoice_issue` (R2) | OPD_BILLING / ADMIN | ✔ | ✔ since D-38 | ✔ refuses no items / void | ✔ already issued → returns it | ✔ since D-38 `OPD_INVOICE_ISSUE` | ✔ | ✔ | Registered; items + total previewed, hash holds `updated_at` |
| `POST /admin/opd/invoices/{id}/payment-link` | `opd.payment_link` (R2) | OPD_BILLING / ADMIN | ✔ | ✔ since D-38 | ✔ issued / partially paid with a balance | ✘ **not idempotent** (a new gateway link + WhatsApp per call, D-38 note); Kriya never retries and reads the result from the response | ✔ since D-38 `OPD_PAYMENT_LINK` | ✔ | ✔ | Registered |
| `POST /admin/bookings/{id}/refund` | `booking.refund` (R3, typed amount) | admin (`require_admin`) | ✔ clinic_admin confined — ✘ **D-37 (b)** super_admin: booking loaded by id, clinic from the row (open; Kriya finds the booking through the clinic-scoped list) | n/a (admins) | ✔ status confirmed / pending_review, a payment | ✔ deterministic gateway idempotency key; Kriya refuses a refund already refunded / in flight | ✔ `payment_events` (`refund_initiated` / `_completed` / `_failed`) | ✔ since **D-37 (a)** (gateway exception text no longer returned) | ✔ | Registered; full amount only (no partial); read back via the Appointments list `refund_state` |
| `POST /admin/home-collection/visits/{id}/assign` | `homecollect.assign` (R2) | HOME_COLLECTION_MANAGE (handler) | ✔ | ✔ `enforce_branch_scope` on the visit; phlebotomist branch eligibility | ✔ confirmed only; on-leave refused (422) | ✔ CAS in `hc.assign` (409 when changed) | ✔ `assign_home_collection` | ✔ | ✔ | Registered |
| `POST /admin/home-collection/auto-assign` | `homecollect.auto_assign` (R2) | HOME_COLLECTION_MANAGE | ✔ | ✔ pinned staff: own branch only | ✔ ≤ 200 visits | ✔ per-visit CAS; re-running assigns only what is still unassigned | ✘ the route writes no audit row — noted, not fixed | ✔ | ✔ | Registered; no dry run, so the preview counts the next 7 days and says later dates are included |

Observations:
- OPD cash receipts, OPD refunds and voids also write no audit row (D-38 note); they stay hand-offs (KK-P4-12).
- PB-14 asks for a per-phlebotomist preview of auto-assign; the endpoint has no dry run, so that preview is
  not possible without copying the assignment logic (not done).
