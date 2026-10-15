# KK-P0-15 hardening checklist — doctor schedule, doctor create, branch assignment (KK-P4-11)

Audited 2026-10-15 by reading `app/routers/admin.py` (`DoctorCreate`, `DoctorUpdate`, `_apply_slot_config`,
`_reject_empty_branch_session`, `create_doctor`, `update_doctor`, `get_doctors`, `DoctorBranchAssign`,
`get_branch_doctors`, `assign_doctor_to_branch`) and `app/utils/helpers.generate_slots`. Same columns as
[desk.md](desk.md). Gaps → **D-34** (open; not reachable through Kriya, see below).

| Endpoint | Action | Perm | Tenant | Branch | Bounds | Idempotent / CAS | Audit row | 5xx | Off-loop | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| `PUT /admin/doctors/{id}` | `doctor.schedule` (R2) | DOCTORS_UPDATE (dependency) | ✔ ownership gate on `clinic_id` first (KA-P1-05) | ✔ a pinned user may edit only a doctor assigned to their branch | ✔ shift times validated (end after start, at least one shift) — ✘ **D-34**: missing shift fields blank that shift; duration defaults to 30 when not sent; fee unbounded | ✔ absolute values; the plan hash holds the current shifts and duration, so an edit meanwhile gives a new preview | ✔ `update_doctor` | ✔ `_friendly_db_error` | ✔ | Registered. Kriya sends the full current shifts + the doctor's own duration, never a branch field (SCH-02) |
| `POST /admin/doctors` | `doctor.create` (R2) | DOCTORS_CREATE | ✔ `resolve_clinic_id_for_write` | ✔ a pinned user's doctor goes to their branch; a single-branch clinic is auto-assigned | ✘ **D-34**: fee and text lengths unbounded; Kriya bounds the fee (₹1..1,00,000) and the form answers (≤ 60) | ✘ not idempotent (no unique name); Kriya refuses a name already on the list and never retries | ✔ `create_doctor` | ✔ | ✔ | Registered. Slots previewed with the server's `generate_slots` (SCH-03) |
| `POST /admin/branches/{id}/doctors` | `branch.doctor_assign` (R2) | DOCTOR_BRANCH_ASSIGN | ✔ doctor and branch must belong to the clinic | ✔ `resolve_owned_branch` (a pinned user only their branch) | ✔ session literal — ✘ **D-34**: `doctor_id` not UUID-checked (generic 500) | ✔ a repeat → 409 (unique); Kriya refuses an existing assignment in the preview | ✔ `assign_doctor_to_branch` | ✔ | ✔ | Registered |
| `GET /admin/doctors`, `GET /admin/branches`, `GET /admin/branches/{id}/doctors`, `GET /admin/appointments` | previews + read-back | as their pages | ✔ | ✔ | ✔ | — | — | ✔ | ✔ | Used |

Observations:
- Bookings outside the new hours (SCH-01) are listed for the next 60 days and never moved; the doctor's
  leave and holidays are unchanged.
- Changing working days, the slot length, the fee or a doctor's name is not built here (the Doctors page).
