# SESSION 47 — G7 Emergency / Casualty OS, Triage, MLC Register, Identity Merge

**Date**: 2026-10-28
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Migration**: `121_emergency_and_casualty.sql` (+ `rollback/121_down.sql`). Not applied to any Supabase project.
**After**: full suite 7123 passed, 4 skipped, 0 failed. UI checked at 375 px against stubbed data, not a live backend.

---

## Goal

Registration never blocks care: an unconscious patient is on the board in one call as "Unknown Male ~40y"
with an `EMG-YYYY-NNNNN` number and a triage colour. Medico-legal cases are recorded and, once the doctor
locks them, frozen. When the family arrives the temporary identity is merged into the real patient in one
transaction. Dispositions hand off to IPD and G9 without retyping.

## Design decisions

| Decision | Why |
|:-|:-|
| `opd_next_counter('emg:YYYY' / 'mlc:YYYY')` instead of the spec's `MAX(seq)+1` | MAX+1 gives two concurrent arrivals the same EMG number; 10 concurrent registrations get 10 consecutive numbers (tested). |
| Temporary patient: `patients.is_temporary`, name "Unknown Male ~40y", phone placeholder = the EMG number, MRN assigned | `patients.phone` is NOT NULL and unique; the placeholder can never be a real number, so nothing is ever sent on WhatsApp. |
| Every state change is one SQL function (`emergency_register`, `emergency_record_mlc`, `emergency_disposition`, `emergency_merge_patient`); guards refuse anything else | Atomic and race-free; a visit closes once (replay-safe); one open ER visit per patient (partial unique index). |
| MLC: correctable until locked; locked = only an identity merge may repoint `patient_id` | Statutory record. The MLC keeps the identity it was recorded under in `patient_snapshot` ("Unknown Male ~40y") after the merge. |
| Merge moves ER visits, MLC, appointments, consultations, prescriptions, bills, admissions (and their charts), orders, lab results, consents, certificates, voice calls, lab reports; logs provenance in `patient_merge_log`; temporary row keeps `merged_into_patient_id` | Spec's "transfer all … with provenance". |
| Seven guards that freeze signed / issued records (103, 111, 115, 117, 118, 119) are redefined verbatim + one line: inside the merge transaction they accept an UPDATE whose **only** change is `patient_id` from the temporary record to the target (`patient_merge_allows`, generated columns excluded) | Signed documents stay immutable in every other respect; a forged merge flag that also changes content is refused (tested). Copied by a generator; rollback restores originals verbatim. |
| Merge refused when the source is not temporary, the target is temporary / merged, or both hold an open admission / ER visit | Never merges two real patients by mistake. |
| Admit to IPD calls `ipd_admit` inside `emergency_disposition` (source `emergency`, priority from triage, reason "Emergency EMG-…: complaint", bed optional) | Visit closed and admission created together, or neither. |
| LAMA requires a signed G9 `refusal_lama` consent recorded after arrival; death requires a doctor login and links the G9 death intimation if issued | Legal prerequisites enforced in the database. |
| FKs to admissions / consents / certificates `ON DELETE SET NULL (col)` | Owner purge of IPD / documents never blocked by ER rows. |

## Permissions

`EMERGENCY_READ` (DOCTOR, NURSE, CASHIER), `EMERGENCY_WRITE` (DOCTOR, NURSE), `MLC_MANAGE` (DOCTOR); pre-121
logins inherit from OPD/IPD clinical, nursing, front desk / admissions (write) and billing (read). Front desk
can register arrivals — registration cannot wait for a clinician.

## UI

Casualty / ER page: triage cards (RED / YELLOW / GREEN / BLACK, tap to filter), live board (RED first,
time since arrival, Unidentified badge, MLC badge, refresh every 30 s), floating ⚡ Rapid Admit
(unknown toggle, sex, approx age, big triage buttons, one complaint line; vitals optional). Visit drawer:
re-triage, doctor, ICD-10 provisional diagnosis, vitals, notes; "Is Medico-Legal Case?" with police
station remembered per browser, Save draft / Lock & sign / Print Police Intimation; Identify & merge
search; disposition (free beds offered, ICU preselected for RED; LAMA opens the G9 refusal consent; death
opens the G9 death intimation). Checked at 375 px: no horizontal overflow.

## Deviations from the task spec

Counter-based numbering (above); `patients.full_name` / `notes` in the spec's merge RPC do not exist —
provenance lives in `patient_merge_log` and `patients.merged_into_patient_id`; added columns
(`original_patient_id`, `latest_vitals`, `clinical_notes`, `provisional_diagnosis`, `transfer_to`,
`time_of_death`, `lama_consent_id`, `death_certificate_id`, snapshots on MLC); added routes
`PATCH /visits/{id}`, `GET /visits/{id}`, `GET /visits/{id}/mlc`, `GET /patients/search`.

## Files

`migrations/121_emergency_and_casualty.sql`, `migrations/rollback/121_down.sql`, `app/services/emergency.py`,
`app/routers/emergency.py`, `app/services/opd_pdf.py` (`render_mlc_intimation_pdf`),
`app/services/permissions.py`, `app/tenancy.py`, `app/main.py`, `app/assistant/registry.py`,
`admin/index.html`, `tests/test_migration_121_emergency.py`, `tests/test_emergency_api_e2e.py`,
`tests/ipd_pg_bridge.py` (ilike inside `or=`), `tests/test_migration_103_opd.py`,
`tests/test_migration_119_documents.py`, `tests/test_ipd_no_llm.py`.
