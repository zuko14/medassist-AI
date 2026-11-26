# Kriya Data Import (MI) — Execution Status

> Spec: [SPEC.md](SPEC.md). Same states and rules as [docs/continuous-care/status.md](../continuous-care/status.md).

## 1. Current pointer
| Field | Value |
|:-|:-|
| Active phase | MI-00…MI-10 built (2026-11-25); VERIFIED in the test suite; presets validated on synthetic exports only (MI-O2 real samples still owed) |
| Next task | Owner: apply 144 on Supabase; enable `data_import` for the pilot hospital; run its real MocDoc / Insta exports on the sandbox (MI-O2) |
| Migration | **144** `144_data_import.sql` + `rollback/144_rollback_data_import.sql` (renumbered from the provisional 149) |
| Owner decisions | MI-O1 which source systems get presets first (default: MocDoc, Practo Insta, generic Excel); MI-O2 real export samples from a pilot hospital (required before presets are VERIFIED) |

## 2. Tasks
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| MI-00 | Baseline gates; re-verify SPEC §1 by symbol | — | green | CG1–CG7 | VERIFIED — SPEC §1 symbols re-checked (`client_data.parse_patient_csv`, `import_records`, `csv_cell`, `get_available_slots`, `book_appointment`) |
| MI-01 | Migration 149 + rollback + tenancy registration | MI-00 | idempotent | `test_migration_mi.py` | VERIFIED — ten `mi_*` tables, RLS forced, registered; `mi_refresh_matches`, atomic `mi_undo`; no trigger, no column on an existing table (`test_migration_mi.py`) |
| MI-02 | Shared import engine (dry run / apply / undo, caps, error rows, formula guard) reusing `client_data` parsers | MI-01 | SPEC §2 | `test_mi_engine.py` | VERIFIED — `app/services/data_import.py`: CSV/XLSX (zip-bomb guard, values only), 5 MB / 25,000 caps, row errors (≤100 stored), dry run → apply (claimed, idempotent per file sha) → undo; formula guard on templates / error CSV (`test_mi_engine.py`) |
| MI-03 | Masters: doctors (+ schedules), services, packages, payers | MI-02 | SPEC §3 | `test_mi_masters.py` | VERIFIED — doctors (+ weekly days / slots template read by `get_available_slots`), services (update keeps old values for undo), packages (never re-priced), payers (merge by name) (`test_mi_masters.py`) |
| MI-04 | Patient links + front-desk legacy search + WhatsApp match queue | MI-02 | never auto-merge on phone alone; consent never set | `test_mi_patients.py` | VERIFIED — patient list via the 088 importer; front-desk legacy search; staff link; phone match queue (shared phones → several candidates, staff decide); consent never set; DPDP erasure removes linked legacy rows (`test_mi_patients.py`) |
| MI-05 | Legacy history (encounters, lab values) + diagnosis map | MI-04 | read-only, labelled | `test_mi_history.py` | VERIFIED — legacy visits / lab values read-only and labelled, never an `opd_encounters` / `opd_prescriptions` row; deterministic diagnosis map with back-fill (`test_mi_history.py`) |
| MI-06 | Future appointments via `book_appointment`; dues | MI-04 | conflicts reported; no messages by default | `test_mi_appointments.py` | VERIFIED — future appointments through `get_available_slots` (holiday, leave, off day, taken, not a slot, past) then `book_appointment`; refusals per row; no message; undo refused once a visit exists; dues closed only through an issued bill (`test_mi_appointments.py`, `test_mi_history.py`) |
| MI-07 | Presets (MocDoc, Insta, generic) validated on real samples | MI-03…06, MI-O2 | sandbox run recorded | preset fixtures | BUILT — MocDoc / Practo Insta / generic presets with synthetic export fixtures (`tests/fixtures/mi/`); VERIFIED pending MI-O2 real samples |
| MI-08 | "Switch to Kriya" wizard UI + owner console progress | MI-03…06 | UI-G1…G12 (UI spec §8) | manual | VERIFIED (automated) — `miwizard` page (stepper, template, drop zone, dry-run tiles, errors, apply, undo, phone-match card) and owner-console read-only progress + toggle; UI-G2 screenshots pending a live pilot |
| MI-09 | CC hand-off: `chronic_list` → CC candidates (needs CC P6) | MI-05, CC-40 | never enrolls | `test_mi_cc_candidates.py` | VERIFIED — `chronic_list` → `mi_chronic_candidates` only with Continuous Care on; never enrols; `/admin/mi/chronic-candidates` is the CC radar source |
| MI-10 | Zero impact + release readiness | all | existing import/export unchanged | `test_mi_zero_impact.py` | VERIFIED — `test_mi_zero_impact.py` + `test_mi_route_matrix.py`: OFF by default, refusal before any mi_* read, existing 088 import / `/admin/me` send no mi_* query |

## 3. Log
| Date | Entry |
|:-|:-|
| 2026-11-24 | Spec written from code audit at `c8c37ff`; no code changed |
| 2026-11-25 | MI-00…MI-10 built. Owner opt-in `data_import` added (every new module is off by default). Test helper `tests/ipd_pg_bridge.py` learned PostgREST upserts (`on_conflict` + `resolution=…`); no existing test edited. Shortcut recorded in code: `send_confirmations` is audited but imported bookings never message (SPEC §3 default) |
