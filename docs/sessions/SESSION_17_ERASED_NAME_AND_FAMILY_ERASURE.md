# Session 17 — Erased Patient Name ("[REDACTED]") and Family-Member Erasure

**Date:** 2026-09-19
**Branch:** main (uncommitted at time of writing)
**Migration:** none — no schema change.
**Trigger:** live test on Accumx Diagnostics after Session 16 shipped. Owner booked a lab test ("BMD - SPINE AND HIP", ref `MC-2026-9ETZ5PQY`) with **For Me**; WhatsApp said *"Who is this test for, [REDACTED]?"* and the admin Appointments page showed patient **[REDACTED]**.

---

## 1. Symptoms → root causes

| # | Symptom | Root cause | Where |
|---|---|---|---|
| 1 | "For Me" booked the returning patient as **[REDACTED]**, never asked their name; greeting also said "[REDACTED]" | Earlier in the day the owner used **Delete my data**. DPDP erasure keeps the `patients` row but sets `name = "[REDACTED]"`. Every "known name?" check treats any non-empty name as real, so For Me used the placeholder. Doctor booking had the same latent bug (`resolve_booking_name`, `_handle_selecting_family_member`, `for_self` button) | `data_retention.anonymize_clinical_records` step 5 → read by `database.get_patient_by_phone` callers |
| 2 | (found) Saved family members **survived** Delete my data | Step 4 updated `family_members.name` / `.primary_patient_phone` — columns that do not exist (real: `full_name`, `primary_phone`). PostgREST errored, caught and logged at **debug**, so it failed silently every time | `data_retention.anonymize_clinical_records` step 4 |

The other two bookings shown as [REDACTED] in admin (cancelled, booked before the erasure) are correct — they are the anonymized clinical records the erasure is meant to keep.

---

## 2. Changes

### A. `app/database.py` — `get_patient_by_phone`
If the stored name is exactly `"[REDACTED]"`, return the row with `name = None` (a copy; DB untouched). This is the single lookup used by conversation, specialty_flow, consent and payment, so every "For Me" path now falls into the existing "no name on file → ask → `update_patient(name)`" branch. No caller relied on the literal placeholder (consent reads `opted_in`/`data_consent`, payment reads `visit_count`/`language`).

### B. `app/services/conversation.py` — `resolve_booking_name`
Skips `"[REDACTED]"` as a candidate (like the `"there"` greeting placeholder), covering conversation contexts already in flight at deploy time.

### C. `app/services/data_retention.py` — erasure step 4
Now **deletes** `family_members` rows by `clinic_id` + `primary_phone`.
- Delete, not redact: saved family names are pure PII (quick-booking list), not clinical records — no NMC retention need. Redacting `full_name` would also violate `UNIQUE (clinic_id, primary_phone, full_name)` for 2+ members.
- No FK references `family_members` (checked migrations).
- Failure now logs at **error** and is appended to `results["errors"]` (audit log); `results["family_members_deleted"]` records the count. Errors do not change `delete_patient_data`'s return value, so the rest of the erasure is unaffected.

### D. Production data fix (done this session)
Appointment `MC-2026-9ETZ5PQY` (id `654b8655-…`, clinic `c2a14afe-…`): `patient_name` `[REDACTED]` → `Chaitanya Kumar`. Selected first (exactly one row, real phone), update guarded by id + clinic_id + `patient_name = '[REDACTED]'`; 1 row updated.
The owner's `patients.name` was left as `[REDACTED]` on purpose — with fix A the next For Me asks the name and saves it.

---

## 3. Tests

- `tests/test_lab_patient_name_and_guide_commands.py`
  - `test_erased_name_reads_as_no_name` (parametrized: `[REDACTED]` → None, real name kept, None stays None; source row not mutated)
  - `test_resolve_booking_name_skips_the_erasure_placeholder`
  - Existing `test_for_me_without_saved_name_asks_then_saves_it` covers the ask → save → book path.
- `tests/test_data_retention.py`
  - `test_erasure_deletes_saved_family_members_by_real_columns` (delete, not update; `clinic_id` + `primary_phone`; count; no errors)

Full suite (excluding `tests/test_multi_worker_smoke.py`, which boots real workers against the production DB — see memory note on stolen scheduler locks): **2806 passed, 2 skipped, 0 failed**.

---

## 4. Open items

- Changes are **uncommitted**; commit + push to `main` deploys to Render.
- Family members saved *before* this fix by patients who already erased their data still exist (the old step 4 never ran). A one-off cleanup would need the list of erased phones — `patients.name = '[REDACTED]'` only covers patients who have not rebooked since. Not done; decide if wanted.
