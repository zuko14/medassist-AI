# KK-P0-15 hardening checklist — treatments and dental plans (KK-P4-10)

Audited 2026-10-15 by reading `app/routers/admin.py` (`TreatmentUpdate`, `TreatmentBulkStatus`,
`TreatmentDoctorsUpdate`, `list_treatments_admin`, `update_treatment`, `set_treatments_status`,
`set_treatment_doctors`, `generate_treatment_description_admin`, `DentalPlanCreate`, `DentalFirstSitting`,
`create_dental_plan`, `dental_free_slots`, `book_dental_sitting`, `list_dental_plans`, `get_dental_plan`,
`_book_and_notify`) and `app/services/dental_plans.py` (`available_slots`, `schedule_sitting`,
`patient_messageable`, `send_patient_confirmation`). Same columns as [desk.md](desk.md).

| Endpoint | Action | Perm | Tenant | Branch | Bounds | Idempotent / CAS | Audit row | 5xx | Off-loop | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| `PUT /admin/treatments/{id}` `{price_from_rupees}` | `treatment.price` (R2), `treatment.price_bulk` (R3, ≤ 100, typed count) | TREATMENTS_MANAGE (dependency) + `specialty_enabled` | ✔ `clinic_id` filter, id must be a UUID | — the catalogue has no branch | ✔ 0..1 crore at the model; Kriya refuses ≤ 0 (KK-FR-071) and skips "price on consultation" (0) rows in bulk | ✔ absolute value; the plan hash holds each old price, so a change meanwhile gives a new preview | ✔ `update_treatment` | ✔ fixed text | ✔ | Registered |
| `POST /admin/treatments/status` | `treatments.status` (R2, one treatment) | TREATMENTS_MANAGE | ✔ | — | ✔ ≤ 500 UUIDs | ✔ repeat harmless; already shown/hidden → refused in the preview | ✔ `set_treatments_status` | ✔ | ✔ | Registered |
| `PUT /admin/treatments/{id}/doctors` | `treatment.doctors` (R2, "only Dr X") | TREATMENTS_MANAGE | ✔ doctors must belong to the clinic | — | ✔ ≤ 200 | ✔ replaces the set (repeat harmless); delete-then-insert is not atomic (documented in the code: a failure in between means "any doctor", no data lost) | ✔ `set_treatment_doctors` | ✔ | ✔ | Registered |
| `POST /admin/treatments/ai-description` | hand-off (help card "Generate all 3 languages") | TREATMENTS_MANAGE | ✔ | — | ✔ | draft only, uses AI credits | — | ✔ | ✔ | Not registered: the executor never stores response bodies, so a draft could not be shown (CAT-08 holds: nothing is saved) |
| `GET /admin/dental/slots` | sitting preview (CAT-10) | DENTAL_PLANS_MANAGE + `dental_plans_enabled` | ✔ | ✔ a pinned user's own branch session | ✔ ISO date pattern, doctor UUID | — | — | ✔ | ✔ | Used: holiday, leave, off-day and taken slots are removed; Kriya refuses a time not in the list |
| `POST /admin/dental/plans` (+ `first_sitting`) | `dental.plan_create` (R2) | DENTAL_PLANS_MANAGE | ✔ `clinic_id` in the insert | ✔ a pinned user's plans belong to their branch | ✔ name ≤ 100, phone validated, sittings 1..30, quote ≤ 1 crore | ✘ not idempotent (a repeat creates a second plan); Kriya never retries and warns when the number already has plans. If the first sitting fails the plan is still saved with a `warning` (the endpoint's design); Kriya's read-back then reports "plan saved, first sitting not booked" and the run is not VERIFIED | ✔ `DENTAL_PLAN_CREATE` | ✔ (`warning` holds `DentalError`/`HTTPException` texts, written for staff) | ✔ | Registered. WhatsApp consent only from the yes/no form |
| `POST /admin/dental/plans/{id}/sittings` | `dental.sitting_book` (R2) | DENTAL_PLANS_MANAGE | ✔ | ✔ `_plan_or_404` branch rule | ✔ | ✔ the slot-uniqueness index refuses a double booking | ✔ `DENTAL_SITTING_BOOK` | ✔ | ✔ | Registered |
| `GET /admin/dental/plans`, `GET /admin/dental/plans/{id}`, `GET /admin/treatments`, `GET /admin/doctors` | previews + read-back | as their pages | ✔ | ✔ (`restrict_to_branch` on plans) | ✔ `q` ≤ 100, `limit` ≤ 200 | — | — | ✔ | ✔ | Used |

Observations (not defects):
- `send_patient` on a sitting only reaches patients who agreed or have written to the clinic before
  (`patient_messageable`), so Kriya always sends `send_patient: true` and the consent answer decides.
- Sitting completion records cash collected → hand-off (H) by the Kriya rule "cash is hand-off only";
  sitting cancel and notify are not built here (an upcoming sitting can be cancelled as a booking,
  `appointment.cancel`, which uses the same refund-aware path).
