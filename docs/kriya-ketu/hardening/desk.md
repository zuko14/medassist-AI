# KK-P0-15 hardening checklist — front-desk write endpoints (KK-P4-05)

Audited 2026-10-14 by reading the routes in `app/routers/admin.py`, `app/routers/opd.py` and the
services they call (`app/services/payment.py`, `app/database.py`, `app/services/opd.py`,
`app/services/broadcast.py`). Same columns as [leaves.md](leaves.md).

| Endpoint | Action | Perm | Tenant | Branch | Bounds | Idempotent / CAS | Audit row | 5xx | Off-loop | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| `POST /admin/appointments/{id}/check-in` | `appointment.check_in` (R1) | none (`verify_credentials`) | ✔ `resolve_clinic_id_for_write` | ✔ `_enforce_booking_branch` | ✔ one id | ✔ a booking that already has a token returns it unchanged; cancelled/no-show etc. → 409 | ✔ `PATIENT_CHECK_IN` | ✔ fixed text | ✔ | Registered |
| `DELETE /admin/appointments/{id}` | `appointment.cancel` (R2) | none | ✔ `enforce_clinic_access` | ✔ `_enforce_booking_branch` | ✔ one id | ✔ only from `confirmed`; anything else returns **200** `{"success": false}`, so Kriya verifies by read-back, never by the response. Ends `cancelled`, or `refunded` when `initiate_refund` succeeded (status.md §5 item 4 resolved) | ✔ `cancel_appointment` (D-04) | ✔ "Internal error" | ✔ | Registered |
| `POST /admin/bookings/{id}/confirm` | `booking.confirm` (R2) | none | ✔ | ✔ `_enforce_booking_branch` | ✔ | ✔ CAS `pending_review → confirmed` (`booking_changed_concurrently`); patient notified | ✔ `BOOKING_MANUAL_CONFIRM` | ✔ (a 400 `detail` is a reason code; the executor never forwards `detail`) | ✔ | Registered |
| `POST /admin/bookings/{id}/reject` | `booking.reject` (R2) | none | ✔ | ✔ | ✔ | ✔ only from `pending_review`/`confirmed`; refund first, then `cancelled`; a failed refund leaves the status as it was (400) | ✔ `BOOKING_MANUAL_REJECT` | ✔ | ✔ | Registered |
| `POST /admin/opd/queue/call-next` | `opd.call_next` (R1) | handler: OPD_FRONT_DESK, or OPD_CLINICAL for the user's own doctor only | ✔ `_opd_scope(live=True)` | doctor-level, as on the Live Queue page | ✔ one doctor | ✘ **not idempotent** (finishes the open consultation, claims the lowest waiting token; CAS per row). Kriya puts both rows in the plan hash, so a moved queue means a new preview, and never retries | ✔ `OPD_QUEUE_CALL_NEXT` | ✔ | ✔ | Registered |
| `POST /admin/notifications/mark-all-read` | `notifications.mark_all_read` (R1) | none | ✔ `clinic_id` filter | — | ✔ | ✔ a repeat is harmless | — (none; low impact) | ✔ | ✔ | Registered. Read state is **clinic-wide** (the `admin_id` filter is a no-op); the preview says so |
| `POST /admin/holidays` | `holiday.create` (R3, typed count of that day's confirmed bookings) | HOLIDAYS_CREATE (dependency) | ✔ insert carries `clinic_id` | clinic-wide by nature (the preview says so to pinned users) | ✔ `name` 1..100 → 422 (D-27); past dates still accepted by the endpoint, refused by Kriya before any read | ✔ D-27 fixed in code: PK (clinic_id, holiday_date) (migration 109, **not yet applied to production**); a repeat for the same clinic and date → unique violation → 500, so Kriya refuses an existing date in the preview | ✔ `create_holiday` | ✔ fixed text | ✔ | Registered 2026-10-14. Existing bookings are **not** cancelled (nothing in the code does); the preview names their count. Read-back: `GET /admin/holidays` |
| `GET /admin/appointments`, `GET /admin/bookings/pending-review`, `GET /admin/opd/queue`, `GET /admin/notifications` | previews + read-back | as their pages | ✔ | ✔ branch-filtered lists | ✔ (see r0-reads) | — | — | ✔ | ✔ | Used |

Observations (not defects):
- `cancel` / `check-in` / `confirm` / `reject` need no permission (`verify_credentials` only), the same as
  the pages that call them; Kriya registers them as `perm_source="none"` (no false sense of a check).
- The legacy `POST /admin/doctors/{name}/queue/call-next` (non-OPD clinics) is not registered: the Live
  Queue page uses the OPD route, and the plan names that one.
