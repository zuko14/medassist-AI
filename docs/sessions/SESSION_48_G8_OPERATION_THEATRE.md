# SESSION 48 — G8 Operation Theatre: schedule, WHO checklist, anaesthesia, auto-posted surgery charge

**Date**: 2026-10-29
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Migration**: `122_operation_theatre.sql` (+ `rollback/122_down.sql`). Not applied to any Supabase project.
**After**: full suite 7633 passed, 5 skipped, 0 failed (with the discharge guard). UI checked at 390 px against stubbed data and end to end against the real app on local Postgres (not Supabase).

---

## Goal

An admitted patient is booked into a theatre; the database refuses a double booking. The theatre team runs
the WHO Surgical Safety Checklist strictly in order (Sign-In → Time-Out → Sign-Out); the anaesthetist keeps
the chart and signs it; the surgeon finishes the surgery with a post-op note, and exactly one procedure
charge lands on the stay's bill — never twice, never after the final bill.

## Design decisions

| Decision | Why |
|:-|:-|
| `opd_next_counter('ot:YYYY')` for `OT-YYYY-NNNNN`, not the spec's `ot_next_seq` (MAX+1) | MAX+1 hands two concurrent bookings the same number. The counter row lock is the repo's pattern (OPD, IPD, EMG); allocated in the booking's transaction, so a refused booking leaves no gap. 10 concurrent bookings get 10 consecutive numbers (tested). |
| No double booking enforced in the database by a trigger (`ot_check_overlap`) that first `UPDATE`s the theatre row, then checks overlap over `scheduled` / `in_theatre` | An `EXCLUDE … USING gist` needs `btree_gist`, which the embedded test Postgres does not ship. The theatre-row UPDATE serialises every booking of a theatre: under READ COMMITTED the second waits and sees the first; under REPEATABLE READ it fails with a serialisation error instead of missing it (both tested). The trigger covers every write path, not only the function. |
| Same trigger refuses one patient in two theatres at once (`ot_patient_busy`) | Cheap and clearly wrong otherwise. The booking function holds the admission row lock. |
| `recovery` does not hold the theatre | The patient has left the table; an emergency can use the theatre. |
| Every state change is one SQL function (`ot_schedule_surgery`, `ot_update_checklist`, `ot_record_anaesthesia`, `ot_set_status`, `ot_complete_surgery`); `ot_guard_record` refuses direct writes; a closed surgery and a signed anaesthesia record never change | Atomic, replay-safe, same pattern as G7. |
| Checklist completion is a CHECK too: a phase can be complete only with every item true and the previous phase complete | Holds even if a future code path bypasses the function (tested by forcing an update inside the function flag). |
| Completion posts one `ipd_charges` row: `category 'procedure'`, `source 'order'`, `source_ref 'ot_surgery:<id>'`, status **posted**, price = manual amount or the linked `billable_services` price at that moment (GST rate only when the clinic has a GSTIN) | The `(clinic, admission, source_ref)` unique index (111) makes a retry a no-op; a completed surgery replays with the same charge. |
| Completion refused when a non-void final bill exists (`ot_final_bill_exists`) or the stay is no longer active | G5: a charge after the final bill would sit outside it. Billing voids the bill first. |
| Admission FK carries `patient_id` (`ON UPDATE CASCADE ON DELETE CASCADE`) to `ipd_admissions (clinic_id, id, patient_id)` | A G7 identity merge moves the surgery with its admission (guard accepts only that change via `patient_merge_allows`); the owner purge (`ipd_purge_clinic`) removes a stay's surgeries. Both tested. |
| Signing the anaesthesia record uses the acting doctor's snapshot (registration number required); finishing a surgery needs a doctor-linked login | Clinical records are signed by an identified clinician (IPD rule). |
| Feature gate = IPD module (`ipd_access`); new bookings refused in WIND_DOWN | OT is part of IPD. |
| A stay cannot start its discharge (any type, LAMA and death included) while one of its surgeries is scheduled, in theatre or in recovery: trigger `trg_ipd_admissions_ot_open` raises `ipd_surgery_open` (409 on the IPD route: "finish, cancel or postpone it on the Operation Theatre page"). Postponed / cancelled / completed surgeries do not hold the stay | Owner decision 2026-10-29. Booking and discharge both take the admission row lock first, so they cannot race past each other. A death on the table is recorded by cancelling the surgery with the reason, then the death discharge. |

## Permissions

`OT_SCHEDULE` (book, postpone, cancel), `OT_CHECKLIST` (WHO checklist, wheel in, recovery), `OT_CLINICAL`
(anaesthesia record, post-op note). `holds_ot_permission` keeps old logins working: `IPD_ADMIN`,
`IPD_ADMISSIONS`, `IPD_CLINICAL` → schedule; `IPD_NURSING`, `IPD_CLINICAL`, `OT_CLINICAL` → checklist;
`IPD_CLINICAL` → clinical; staff role `DOCTOR` → all three, `NURSE` → checklist; admins always. Reading the
day list needs any OT grant. Theatres are managed by `IPD_ADMIN` / clinic admins. The DOCTOR preset gains all
three, NURSE gains `OT_CHECKLIST`. The staff create / edit forms offer the three grants (shown when IPD is on).

## API (`app/routers/ot.py`)

`GET/POST /admin/ot/theatres`, `PATCH /admin/ot/theatres/{id}`, `GET /admin/ot/options` (form pickers),
`POST /admin/ot/surgeries` (idempotency key; 409 `ot_theatre_busy` / `ot_patient_busy`),
`GET /admin/ot/schedule?date=` (branch-pinned staff see their branch plus branch-less rows),
`GET /admin/ot/surgeries/{id}`, `POST …/who-checklist/{sign_in|time_out|sign_out}`, `POST …/anaesthesia`,
`POST …/status` (`start` / `recovery` / `cancel` / `postpone`), `POST …/complete`. Every write is audited
(`log_admin_action`), every route checks tenant (`enforce_clinic_access`) and branch (`enforce_branch_scope`);
errors are mapped codes, never database text.

## UI (`admin/index.html`)

Sidebar **Operation Theatre** (IPD OS section). Day picker; status counts; one column per theatre with
surgery cards (time, status, patient, MRN · admission · OT number, procedure, surgeon, anaesthetist,
priority tag, Sign-In / Time-Out / Sign-Out progress). Schedule-surgery form (admitted patients, theatre,
catalogue procedure that sets the charge, date / start / duration, surgeon, anaesthetist). Surgery dialog
with the actions the login may take. WHO checklist dialog with three tabs opened in order. Anaesthesia
chart (ASA, Mallampati, technique, agents, fluids, Aldrete, intra-op vitals table) with Sign & lock.
Surgeon's post-op drawer: note fields, charge (blank = the service price) and **Finish surgery**, which
shows the posted charge. One dialog at a time (sub-dialogs return to the surgery). Theatres dialog for IPD
admins. Post-op textareas are Kriya Ketu dictation targets. Checked at 390 px, light and dark, against
stubbed API data, then end to end against the real app on the local Postgres (see Verification).

## Kriya Ketu

Page `ot` in `PAGES` (+ synonyms), so the page table still equals the sidebar. ADR-IPD-08: one R0 read
`ot.today` (`GET /admin/ot/schedule`, same holder as the route) and three hand-offs `ot.schedule`,
`ot.checklist`, `ot.finish` — nothing clinical is executable. 19 corpus phrases (en, te-Latin, hi-Latin,
te, hi, two negatives). "take consent for surgery" still routes to the G9 consent.

## Deviations from the task spec

Counter numbering and the trigger lock instead of `ot_next_seq` / btree_gist (above). Added columns:
`ot_theatres.schedule_version` (the booking lock), `ot_surgeries.charge_id`, `completed_by(_name)`,
`idempotency_key`, `created_by_name`. `cancellation_reason` also holds the postpone reason. A postponed
surgery is final; it is booked again as a new surgery (its checklist was empty anyway). Added
`GET /admin/ot/options`.

## Files

`migrations/122_operation_theatre.sql`, `migrations/rollback/122_down.sql`, `app/services/ot.py`,
`app/routers/ot.py`, `app/main.py`, `app/tenancy.py`, `app/services/permissions.py`,
`app/assistant/registry.py`, `admin/index.html`, `tests/test_migration_122_ot.py`, `tests/test_ot_api_e2e.py`,
`tests/test_migration_121_emergency.py` (`unwind_121` unwinds 122 first), `tests/test_migration_120_masters.py`
(same), `tests/test_ipd_no_llm.py`, `tests/test_assistant_hospital_modules.py`, `tests/test_assistant_reads.py`,
`tests/fixtures/assistant_corpus.json`, `docs/hospital-os/status.md`, `docs/hospital-os/GAP_ANALYSIS_AND_ROADMAP.md`.

## Verification

Local embedded Postgres (pgserver, PG 16), Python 3.12, 2026-10-29. Commands run in this session; last line of each output pasted.

| Check | Command | Result |
|:-|:-|:-|
| Migration 122 + OT E2E | `pytest tests/test_migration_122_ot.py tests/test_ot_api_e2e.py` | `20 passed, 2 warnings in 2.37s` |
| Tenant linter, scope matrix, clinical firewall, zero-LLM | `pytest tests/test_lint_unscoped_queries.py tests/test_admin_super_admin_scope_matrix.py tests/test_clinical_firewall.py tests/test_ipd_no_llm.py` | `731 passed, 2 warnings in 24.70s` |
| Every migration test (103–122, rollback chain) | `pytest tests/test_migration_1*.py` | `153 passed, 1 warning in 4.26s` |
| Kriya Ketu (registry = sidebar, reads, corpus, size budget) | `pytest tests/test_assistant_hospital_modules.py tests/test_assistant_reads.py tests/test_assistant_rules.py tests/test_assistant_registry.py tests/test_assistant_panel_static.py` | `882 passed, 2 warnings in 50.07s` |
| Full suite | `pytest -q` | `7628 passed, 5 skipped, 15 warnings in 190.74s (0:03:10)` (was 7531 passed, 5 skipped) |
| Concurrency | `test_ten_concurrent_overlapping_bookings_admit_exactly_one`, `test_ten_concurrent_bookings_take_consecutive_numbers`, `test_repeatable_read_race_fails_instead_of_double_booking` | 1 of 10 overlapping bookings succeeds, 9 get `ot_theatre_busy`; 10 numbers consecutive; the REPEATABLE READ racer fails |
| UI | static page + stubbed API, real render code, 390 px, light + dark | page and dialog `scrollWidth` 390 = viewport; no console errors besides the static server's 404s for `/admin/*` |
| UI against a live backend | The real app (uvicorn, lifespan off) on the local Postgres through `tests/ipd_pg_bridge.py`, auth fixed to one doctor-linked clinic admin, real `admin/index.html` boot via `/admin/me`, 390 px | Booked OT-2026-00001; an overlapping booking showed the server's 409 text and the retry got OT-2026-00002 (no number lost); Finish refused before the checklist; Time-Out tab refused before Sign-In; partial save; three phases completed; anaesthesia signed with the doctor's registration; Finish posted one charge (₹35,000, posted) that appears on `ipd_running_bill`; the stay's timeline has `surgery_scheduled`, `surgery_in_theatre`, `surgery_checklist_*`, `anaesthesia_signed`, `surgery_completed`. Page `scrollWidth` 390. |

The "ValueError: I/O operation on closed file" logging line printed after the E2E runs also appears on the existing `tests/test_emergency_api_e2e.py`; it does not affect results.

### Found and fixed in the live check

| Issue | Fix |
|:-|:-|
| "+ Add reading" re-rendered the anaesthesia chart and wiped fields typed but not yet saved | Unsaved fields are kept as a draft across the re-render (`otAnaesFields`); verified live: ASA, technique and induction agents survive, then save |
| Surgery header showed "OT 1 · · IPD-…" when the patient has no MRN | Empty parts are left out |
| Surgery cards had no accessible name | `aria-label` = time, patient, procedure, status |

After the fixes: `pytest tests/test_assistant_panel_static.py tests/test_assistant_registry.py tests/test_ot_api_e2e.py tests/test_migration_122_ot.py` → `207 passed`; inline script `node --check` OK.

### Discharge guard (added after the live check)

| Check | Command | Result |
|:-|:-|:-|
| OT migration + E2E | `pytest tests/test_migration_122_ot.py tests/test_ot_api_e2e.py` | `25 passed, 2 warnings in 1.76s` |
| Migrations, IPD / finance / ER E2E, linter, scope matrix, firewall, zero-LLM | `pytest tests/test_migration_1*.py tests/test_ipd_api_e2e.py tests/test_ipd_finance_api_e2e.py tests/test_emergency_api_e2e.py tests/test_lint_unscoped_queries.py tests/test_admin_super_admin_scope_matrix.py tests/test_clinical_firewall.py tests/test_ipd_no_llm.py` | `909 passed, 2 warnings in 29.17s` |
| Full suite | `pytest -q` | `7633 passed, 5 skipped, 15 warnings in 188.87s (0:03:08)` |

## Not verified / open

| # | Item |
|:-|:-|
| G8-1 | Migration 122 not applied to Supabase staging or production. This checkout has no `.env`, no database URL and no linked Supabase project, so it cannot be reached from here. Apply on a staging branch and rerun `pytest tests/test_migration_122_ot.py tests/test_ot_api_e2e.py` with `TEST_DATABASE_URL`. |
| G8-2 | Live check used the local Postgres through the test bridge, not PostgREST/Supabase, and a stubbed `/admin/login` (auth overridden); not tried on a real phone. |
| G8-3 | Confirmed by probe: a surgery still in theatre past its booked end does not block a booking that starts at that end (overlap uses scheduled times). |
| G8-4 | ~~Discharge while a surgery is open~~ — fixed: blocked in the database (owner decision); tested for scheduled / in theatre / recovery and regular / LAMA / death, released by cancel, postpone or finish, and the 409 on `POST /admin/ipd/admissions/{id}/discharge/initiate`. |
| G8-5 | Implants / consumables are free text, not issued from pharmacy stock (G3); consent is the G9 consent (not linked to the surgery row). |
| G8-6 | Clinical reads (surgery detail) are not audit-logged; writes are. Same as the other G modules. |
