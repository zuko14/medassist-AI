# KK-P0-15 hardening checklist — OPD front desk (KK-P4-07, PB-06)

Audited 2026-10-14 by reading `app/routers/opd.py` and `app/services/opd.py` (`register_patient`,
`duplicate_candidates`, `search_patients`, `create_walk_in`, `arrive`, `transition_stage`). All routes go
through `_opd_scope(..., live=True)` (OPD enabled and set up). Same columns as [leaves.md](leaves.md).

| Endpoint | Action | Perm | Tenant | Branch | Bounds | Idempotent / CAS | Audit row | 5xx | Off-loop | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| `POST /admin/opd/patients/duplicate-check` | preview of `patient.register` (read-only POST: `registry.read_only_post`) | OPD_FRONT_DESK (handler) | ✔ | — | ✘ **D-30**: `name` / `phone` have no length limit | read-only | — | ✔ | ✔ | Used |
| `POST /admin/opd/patients` | `patient.register` (R2) | OPD_FRONT_DESK (handler) | ✔ `scope` | — | ✔ `PatientRegisterIn` (phone 10-15 digits, name ≤ 100) | ✔ refuses unacknowledged duplicates (409); `data_consent` must be true (422) | ✔ `OPD_PATIENT_REGISTER` | ✔ | ✔ | Registered. Consent only from the form's yes/no answers (REG-03); `acknowledged_duplicates` only after "new person" (REG-02). Read-back: `GET /admin/opd/patients/{id}` |
| `GET /admin/opd/patients/search` | `opd.walk_in` preview | any OPD role (handler) | ✔ | — | ✔ q ≥ 2, limit ≤ 50 | — | — | ✔ | ✔ | Used |
| `POST /admin/opd/walk-ins` | `opd.walk_in` (R2) | OPD_FRONT_DESK (handler) | ✔ | ✔ `_write_branch` | ✔ `WalkInIn` | ✘ not idempotent (a new token each call): Kriya refuses when the patient already has an open token with that doctor today, and never retries | ✔ `OPD_WALK_IN_CREATE` | ✔ | ✔ | Registered. Read-back: the token in `GET /admin/opd/queue` (REG-04) |
| `POST /admin/opd/appointments/{id}/arrive` | `opd.arrive` (R1; the check-in variant on a live-OPD clinic, KK-FR-061) | OPD_FRONT_DESK (handler) | ✔ | ✔ `enforce_branch_scope` in `arrive()` | ✔ one id | ✔ already arrived → 409; not checkable → 409 | ✔ `OPD_PATIENT_ARRIVE` | ✔ | ✔ | Registered. Read-back: the appointment on today's queue |
| `POST /admin/opd/queue/{id}/recall` | `opd.recall` (R1) | OPD_FRONT_DESK or OPD_CLINICAL (handler) | ✔ | ✘ **D-30**: no branch check and no own-doctor rule for OPD_CLINICAL (the stage route has one) | ✔ one id | sends a WhatsApp each call | ✘ **D-30**: no audit row | ✔ | ✔ | Registered (it only re-sends a "please come to the room" message). Evidence = the response's `notified` (ADR-004); `false` is never shown as done |
| `POST /admin/opd/queue/{id}/stage` | `opd.stage` (R1) | OPD_FRONT_DESK, or OPD_CLINICAL out of `in_consultation` for their own doctor (handler) | ✔ | doctor rule as on the page | ✔ `StageChangeIn` (enum) | ✔ CAS on `expected_from` (409 when the row moved) | ✔ `OPD_STAGE_TRANSITION` | ✔ | ✔ | Registered. Kriya never offers a move to `cancelled`. Read-back: the row's stage on today's queue |

Observation (not a defect): the legacy `POST /admin/appointments/{id}/check-in` on a live-OPD clinic starts
the queue at `opd_settings.after_checkin_stage` with a default of `waiting`, while OPD `arrive()` defaults to
`vitals_pending`. Kriya uses `arrive` on live-OPD clinics, so its check-ins match the OPD desk.
