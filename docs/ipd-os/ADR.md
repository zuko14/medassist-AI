# Kriya IPD OS — Architecture Decision Records

Each ADR: context → alternatives → decision → consequences → evidence. Status: **Accepted** unless marked.
Owner decisions still open are listed at the end (they do not block the code; defaults are chosen and documented).

## ADR-IPD-01 — Shared patient and staff model, separate encounter
- **Context.** OPD made `appointments` the visit and hung clinical tables off it. An inpatient stay lasts days,
  spans beds, doctors and shifts, and has its own lifecycle (bed, discharge, settlement).
- **Alternatives.** (a) Model the stay as a long appointment. (b) A separate patient master for IPD. (c) A new
  encounter table on the shared patient master.
- **Decision.** (c): `ipd_admissions` references `patients`/`family_members`/`doctors`/`branches` through
  composite tenant FKs; it may reference the OPD visit (`source_appointment_id`, `source_encounter_id`) but never edits it.
- **Consequences.** No duplicate patients; MRN and allergies come from the registry; OPD history stays untouched.
- **Evidence.** `migrations/111_ipd_os_core.sql` §5; `test_cross_tenant_ids_are_not_found`.

## ADR-IPD-02 — Entitlement through the existing owner opt-in, gated on OPD
- **Context.** OPD OS is an owner opt-in (`OPT_IN_FEATURES`), never granted by a plan or the enterprise wildcard.
  IPD settles bills through OPD billing and admits patients from the OPD registry.
- **Decision.** `ipd_enabled` joins `OPT_IN_FEATURES`; `ipd_access()` requires `opd_enabled()`; the platform toggle
  rejects enabling IPD without OPD (400) and disabling OPD while IPD is not OFF (409). Every route checks
  `ipd_access()` on the server — the sidebar is cosmetic.
- **Evidence.** `test_ipd_requires_opd`, `test_module_is_off_until_the_owner_enables_it`,
  `test_owner_disable_with_patients_winds_down_and_reenable_loses_nothing`; the adversarial and super-admin matrices
  run on all 38 IPD routes.

## ADR-IPD-03 — Bed allocation and every state change in row-locking SQL functions
- **Context.** PostgREST has no multi-statement transactions; the app is `service_role` (no RLS). A bed
  assignment touches 3–5 rows.
- **Alternatives.** (a) Sequential PostgREST writes with compensation. (b) Optimistic version columns only.
  (c) One plpgsql function per transition, `SELECT … FOR UPDATE` on the admission then beds by id, plus partial
  unique indexes as a second line of defence.
- **Decision.** (c). State columns of beds/admissions/assignments/charges are writable only inside those
  functions (`kriya.ipd_tx` checked by triggers).
- **Consequences.** Invariants hold even for a buggy future caller; logic in SQL is harder to read, so each
  function is short, commented and covered by a real-Postgres test.
- **Evidence.** `test_twenty_concurrent_admissions_to_one_bed_exactly_one_wins`,
  `test_concurrent_transfers_into_one_bed`, `test_concurrent_swap_does_not_deadlock`,
  `test_failed_transfer_changes_nothing`, `test_bed_state_cannot_be_written_directly`,
  `test_concurrent_api_admissions_to_one_bed`.

## ADR-IPD-04 — Clinical records are append-only with provenance
- **Decision.** Observations and MAR entries are never edited: an error is marked `entered_in_error` with a reason
  (and a replacement row pointing at it via `corrects_id`). Notes are final when saved (authenticated authorship is
  the signature); the author amends, which supersedes the old row (`amended`). Orders are immutable except the
  one-way transition to `completed`/`discontinued`. Discharge summaries are drafts until the attending doctor signs;
  signed versions change only through a new version that supersedes the old one on signing.
- **Evidence.** `test_observation_immutable_and_corrected_with_provenance`, `test_notes_and_amendment_by_author_only`,
  `test_mar_rules`, `test_summary_signing_rules`.

## ADR-IPD-05 — Orders: an integration boundary, not a fake pharmacy or LIS
- **Context.** The repo has a lab catalogue (`lab_tests`) and lab report delivery, but **no** pharmacy stock,
  dispensing or LIS order queue; `prescriptions` is a medication-reminder table.
- **Decision.** `ipd_orders` records the doctor's order (medication / investigation / procedure / diet / nursing),
  MAR records administration separately. Investigation orders carry `lab_test_id` as a reference only. No stock,
  dispensing or result tables are created. Charges for orders are entered by billing (manual, idempotent
  `source_ref`), not auto-posted.
- **Consequences.** No disconnected copies of pharmacy or lab data. Pharmacy dispensing, LIS order transmission and
  result mapping are **DEFERRED** (status.md) until a system of record exists.

## ADR-IPD-06 — Billing: proposed → posted charges, settled as an OPD invoice
- **Decision.** `ipd_charges` (integer paise) start `proposed`; billing staff post them (immutable afterwards, void
  only before billing). Bed-day charges are computed by `ipd_accrue_bed_charges` from `ipd_settings.bed_tariffs`
  (one per IST date per stay, idempotent via `source_ref`). Missing tariffs are reported, never guessed.
  `ipd_generate_final_bill` turns posted charges into ONE draft `opd_invoices` row (grouped lines, idempotent,
  `uq_opd_invoices_one_per_admission`); issue, receipts, refunds, shifts and payment links are the existing OPD
  billing paths. A trigger stops the OPD page from editing IPD bill lines. A voided bill releases its charges.
- **Consequences.** One ledger, one numbering series, one cashier workflow. **Deposits/advances, packages with
  inclusion rules, TPA/insurance claim workflow and GST are DEFERRED** (status.md) — they need hospital policy
  decisions and a ledger design for advances that the invoice-bound receipt model does not have.
- **Evidence.** `test_bed_charges_are_idempotent_and_priced_from_tariff`, `test_missing_tariff_is_reported_not_guessed`,
  `test_posted_charge_is_immutable`, `test_voided_bill_releases_charges_for_a_new_bill`; E2E reconciles
  `invoice.total_paise == Σ posted charges` through the real OPD issue route.

## ADR-IPD-07 — Deactivation: wind-down, never deletion
- **Context.** Disabling a module must not interrupt care or lose records.
- **Decision.**
  | Situation at disable | Result |
  |---|---|
  | No pending/admitted/discharge-pending stays | `ipd_state = OFF`; routes 403; data kept |
  | Active stays, no `confirm_disable` | `409` with preview (in bed, pending, reserved beds, proposed charges) |
  | Active stays + `confirm_disable` | `ipd_state = WIND_DOWN`, `features.ipd_enabled = false`: new admissions and new wards/beds refused (409); everything for existing stays (observations, notes, orders, MAR, transfer, assign/reserve for already-created pending stays, billing, discharge) keeps working |
  | Re-enable | `ACTIVE` with all data intact |
  | Disable again with no active stays | `OFF` |
  OPD cannot be disabled while IPD is not OFF. Clinic deletion (owner, confirmed) purges IPD then OPD rows.
- **Evidence.** `test_owner_disable_with_patients_winds_down_and_reenable_loses_nothing`,
  `test_disable_without_patients_turns_off_and_blocks_routes`, `test_purge_removes_everything_and_clinic_can_be_deleted`.

## ADR-IPD-08 — AI boundaries: Kriya Ketu reads and hands off; it never writes IPD
- **Decision.** IPD service/router import nothing from AI/voice/assistant; AI, voice, chat and assistant code never
  name IPD clinical tables (AST/text guard). Ketu gets IPD navigation, four R0 reads (occupancy, inpatients,
  waiting list, discharges) that dispatch the same routes with the user's own permissions, and three H hand-offs
  (admit, discharge, vitals) that open the page where a person presses the button. No R1–R3 IPD action in v1.
- **Why.** Bed allocation, clinical documentation and billing are high-consequence; Ketu's write path requires the
  KK-P0-15 endpoint hardening review per endpoint, which has not been run for `/admin/ipd/*`. AI-drafted discharge
  summaries are explicitly out of scope until a clinician-approved draft/review workflow is designed.
- **Evidence.** `tests/test_ipd_no_llm.py`, `tests/test_assistant_registry.py` (validate + drift on IPD actions).

## ADR-IPD-09 — Role design inside the existing RBAC
- **Decision.** Permissions `IPD_ADMIN`, `IPD_ADMISSIONS`, `IPD_NURSING`, `IPD_CLINICAL`, `IPD_BILLING`; new staff
  role `NURSE` (preset `IPD_NURSING`, confined like DOCTOR/CASHIER from clinic-wide dashboards). Presets apply at
  account creation only, so no existing login gains IPD access. Clinical decisions (orders, progress/plan/procedure
  notes, regular/death/transfer discharge, summary signing) additionally require a doctor-linked login;
  LAMA/DAMA/absconded may be recorded by the admission desk. Settlement waivers need `IPD_ADMIN`.
- **Known gap (owner decision O-3).** House convention gives `clinic_admin` every permission, so a hospital
  administrator can *read* clinical charts (every read is audit-logged). They cannot author clinical decisions
  without a doctor link. Restricting admin clinical reads would change OPD-wide semantics and is left to the owner.
- **Data minimisation.** Billing-only and admission-only users get no observations/notes/orders/diagnosis in API
  responses (`test_full_inpatient_journey` asserts this).

## ADR-IPD-10 — No background jobs in v1
- **Decision.** Reservation expiry is evaluated at assignment time; bed-day charges are computed on demand by
  billing; the worklist is derived from source tables. No scheduler job, so no job can run with the wrong tenant
  context (a listed risk) and nothing changes state while nobody is looking.
- **Consequence.** Expected-discharge reminders and automatic nightly charge accrual are DEFERRED.

## ADR-IPD-11 — Time
- **Decision.** All dates are IST. SQL uses `AT TIME ZONE INTERVAL '+05:30'` (IST has no DST; this also works on
  servers without a tz database — the embedded test Postgres has none). Python uses `today_ist()`; the UI formats
  with `timeZone: 'Asia/Kolkata'`.

## Open owner decisions (defaults in force)
| ID | Question | Default |
|---|---|---|
| O-1 | Bed-day rule (calendar day vs 24-hour cycle; cut-off hour) | Calendar IST day; last day free if discharge started and before 12:00 (`discharge_cutoff_hour`) |
| O-2 | Unpaid discharge | Allowed only with an IPD-admin waiver reason (e.g. cashless insurance) — recorded on the admission |
| O-3 | Should clinic administrators read clinical charts? | Yes (house convention), audit-logged |
| O-4 | Mandatory admission consent | Recorded as a flag; not enforced (policy differs by hospital) |
| O-5 | Clinical alert thresholds (early-warning scores) | **None shipped.** Values are shown, not judged, until clinician-approved rules exist (same as the OPD owner decision of 2026-10-10) |
