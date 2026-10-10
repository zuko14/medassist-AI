# Kriya OPD OS — Phase 1 Implementation Specification

> Status: **DRAFT FOR APPROVAL** — no code, no migration applied.
> Anchored to `main` @ `da41ccf`, latest migration `102_voice_knowledge_entries.sql`.
> Tracking: [`docs/opd-os/status.md`](status.md). This file is the design; `status.md` is the checklist.

Principle: **one patient, one operational record, one appointment engine, one OPD workflow, many channels.**
Concretely: the existing `appointments` row *is* the visit. Walk-ins are appointment rows. The existing
token queue (`token_number` / `queue_status`) *is* the OPD queue. New OPD tables hang off the appointment.
Nothing in the WhatsApp/voice booking path is replaced; it gains a `booking_channel` stamp.

---

## 0. Pre-flight findings — where the brief and the code disagree

Every row below was verified against the code at `da41ccf`. The plan follows the **code**, not the brief.

| # | Brief assumes | Code reality | Decision |
|---|---|---|---|
| F1 | `idx_appointments_slot_unique` guards scheduled slots | Does not exist. Guards are `uq_appointment_active_slot` `(clinic_id, doctor_id, date, time)` and `uq_appointment_active_slot_unassigned` `(clinic_id, doctor_name, …)`, both `WHERE status IN ('confirmed','pending_payment','pending_review') AND booking_type='consultation'` (migration 064). `app/utils/helpers.py:is_slot_conflict()` matches on the **name prefix** `uq_appointment_active_slot`. | Rebuild both with `AND is_walk_in = false`, **same names** (keeps `is_slot_conflict()` working). |
| F2 | Queue/token desk is new | Exists: `appointments.token_number`, `queue_status ∈ {waiting,in_consultation,done}` (019), `idx_unique_queue_token` (021), `idx_unique_lab_queue_token` (073), `database.check_in_appointment()` (unique index + 5 retries = the "optimistic locking"), `database.call_next_patient()` (CAS on `queue_status`), `POST /admin/appointments/{id}/check-in`, `POST /admin/doctors/{doctor_name}/queue/call-next`, WhatsApp token/turn notifications, `get_patient_queue_status()` used by WhatsApp **and** `app/voice/tools.py:queue_status`. | Extend, never fork. Widen the `queue_status` CHECK; add `initial_queue_status` param to `check_in_appointment()`; new `opd_call_next()` used by OPD clinics only. |
| F3 | Add `opd_enabled` to `OPT_IN_FEATURES`; build owner toggle | `OPT_IN_FEATURES` (`app/services/tenant.py:652`) and `PATCH /platform/clinics/{id}/features` (`platform.py:867`) already exist and already accept any `OPT_IN_FEATURES` key. Precedent: `ai_receptionist`, `corporate_health` with `*_enabled(clinic)` helpers that ignore the enterprise wildcard. | Add one dict entry + `opd_enabled(clinic)` helper + guards/provisioning inside the existing route. |
| F4 | Roles `doctor`, `receptionist`, `billing`, `admin` | Login roles are only `super_admin`, `clinic_admin`, `staff`. Staff have `staff_role` (`app/services/permissions.py:STAFF_ROLES`) and a `permissions[]` grant list checked by `require_permission()`. No login is linked to a `doctors` row. | 4 new permissions (`OPD_FRONT_DESK`, `OPD_CLINICAL`, `OPD_BILLING`, `OPD_ADMIN`), 2 new staff roles (`DOCTOR`, `CASHIER`), `clinic_admins.doctor_id`. **Only a login linked to the encounter's doctor can sign** — regardless of role. |
| F5 | New `prescriptions` concept | A `prescriptions` table exists (migration 002) — it is the **medication-reminder** table (`reminder_times`, `start/end_date`), fed by `/admin/prescriptions`. | New `opd_prescriptions` + `opd_prescription_items`. The reminder table is untouched; bridging signed Rx → reminders is out of scope (YAGNI). |
| F6 | Extend `patients` with demographics → one record per patient | `patients` is `UNIQUE (clinic_id, phone)` (035). A mother and child sharing a phone are **one** `patients` row + `family_members` rows (020). WhatsApp code assumes one patient per phone. | Keep the phone-account model. Demographics + MRN on **both** `patients` and `family_members`. Every OPD row carries `patient_id NOT NULL` + `family_member_id NULL`. MRN numbering is shared, so it is unique across both tables. |
| F7 | PDF generation exists | No PDF *writer* in `requirements.txt` (only `pdfplumber`, a reader). | Add `fpdf2` (pure Python) + `uharfbuzz` (Indic shaping) + bundled Noto TTFs. Thermal 80 mm receipt = browser print CSS, no PDF. |
| F8 | "Instant WhatsApp alert" to walk-ins | `whatsapp_service.send_text/send_document` only work inside Meta's 24 h window (`_can_send_freeform`). Walk-ins usually never messaged the clinic. Lab reports already solve this with a **template with a document header** (`app/services/lab_reports.py:template_name_for`). | Approved utility templates per clinic WABA: `opd_token_issued`, `opd_token_called`, `opd_prescription_ready` (doc header), `opd_receipt` (doc header). Names in `clinics.opd_settings.templates`. **External blocker: Meta approval.** Until approved: in-window freeform only, plus the TV display. |
| F9 | "30 tenant tables" | `TENANT_OWNED_TABLES` has 46 entries (agent-context docs are stale). | Add 7. Update `08-AUTH-AND-MULTI-TENANCY.md` count. |
| F10 | Migration may use `CREATE INDEX CONCURRENTLY` | `scripts/migrate.py` runs each file in **one transaction** (`commit`/`rollback`). CONCURRENTLY is impossible. | Plain index builds; `appointments` is small (3 live clinics). Apply off-peak (before 08:00 IST). |
| F11 | 6 new tables | Cash-drawer reconciliation needs a durable "declared vs expected" record per cashier session. No existing table fits. | **7th table `opd_cashier_shifts`** (deviation, flagged). |
| F12 | Service catalog | No catalog table. Lab tests are priced in `lab_tests`; doctor fee in `doctors.consultation_fee`. | Nursing/procedure items in `clinics.opd_settings.service_catalog` (JSON array). `ponytail:` upgrade to a table when a clinic exceeds ~300 items or needs per-branch prices. Invoice lines snapshot name + price anyway. |
| F13 | DPDP erasure | `data_retention.py` **deletes** `family_members` on erasure ("pure PII, not clinical"). Once a family member has OPD records, that is no longer true, and NMC requires 7-year retention. | Erasure deletes family members with **no** OPD references (as today) and **redacts in place** those with OPD references. **Owner/legal decision D3.** |
| F14 | RLS policies protect OPD data | `service_role` has `BYPASSRLS`. Policies protect only against a leaked `anon`/`authenticated` key (Supabase grants those roles on new tables by default). | Policies for parity with 049 **plus** `REVOKE ALL … FROM anon, authenticated`. Real guards: app scoping, **composite tenant FKs**, and immutability **triggers** (triggers fire for `service_role`; RLS does not). |
| F15 | Server clock | `Dockerfile` sets `TZ=Asia/Kolkata`; existing `datetime.now()` calls are IST in prod. | New code still uses `app.voice.dates.today_ist()` / `IST` explicitly (tests run on dev machines in any TZ). |
| F16 | Rollback | `app/main.py` lifespan refuses boot only if DB migration < disk. Pre-OPD code boots fine on a DB at 103. | Rollback = feature off → redeploy previous code. Schema rollback (`103_down.sql`) is optional and destructive. |
| F17 | Postgres version | Tests run on `pgserver` (PG 16). Production Supabase version is **unverified**. `ON DELETE SET NULL (col)` (used twice below) needs PG ≥ 15. | Phase 1.1 gate: `SELECT version()` on prod. If < 15: use single-column `REFERENCES doctors(id) ON DELETE SET NULL` / `REFERENCES family_members(id) ON DELETE SET NULL` + an app-side same-clinic check for those two FKs. |

### Owner decisions required (defaults chosen so work can proceed)

| ID | Decision | Default in this spec |
|---|---|---|
| D1 | Booked patients jump the walk-in token line? | No. Token order = arrival order (existing behaviour). |
| D2 | Invoice year = calendar or Indian FY (Apr–Mar)? | Calendar year IST, as briefed (`INV-2026-00042`). |
| D3 | DPDP erasure vs NMC retention for family members with OPD records | Redact name/contact in place; keep MRN, DOB, allergies and signed records. |
| D4 | Gateway refunds for OPD payment links | Out of Phase 1. Manual cash/UPI refunds only; gateway refund done in Razorpay/PhonePe dashboard + recorded. |
| D5 | Patient names on the hallway TV | Off. Tokens + doctor + room only. |

---

## Part 1 — Complete impact map

### 1.1 Database

| Path | Action |
|---|---|
| `migrations/103_opd_os_core.sql` | **NEW** — Part 2. |
| `migrations/rollback/103_down.sql` | **NEW** — Part 2 §2.12. |
| `migrations/verify_supabase_schema.sql` | Add the 7 tables, the 2 rebuilt slot indexes, `appointments_queue_status_check`, and the OPD functions to the PASS/FAIL checks. |

### 1.2 Backend — new files

| Path | Contents |
|---|---|
| `app/services/opd.py` | Registry + walk-in + queue + setup. Functions: `search_patients(clinic_id, q, limit)`, `duplicate_candidates(clinic_id, name, phone, dob, age)`, `register_patient(clinic_id, body, actor) -> dict`, `assign_mrn(clinic_id, patient_id, family_member_id)`, `create_walk_in(clinic, body, actor) -> dict`, `arrive(clinic, appointment_id, actor) -> dict`, `get_queue_board(clinic_id, date, doctor_id, department, branch_id) -> dict`, `transition_stage(clinic, appointment_id, to_stage, expected_from, actor) -> dict`, `opd_call_next(clinic, doctor_id, actor) -> dict \| None`, `queue_position(clinic_id, appointment) -> dict`, `setup_checklist(clinic) -> list[dict]`, `effective_state(clinic, checklist) -> str`, `dry_run(clinic) -> dict`, `go_live(clinic, actor)`, `provision_defaults(clinic_id)`, `deactivation_preview(clinic_id) -> dict`, `rotate_display_token(clinic_id) -> str`, `public_display_payload(clinic_id, branch_id) -> dict`. Constants `STAGES`, `ALLOWED_TRANSITIONS`, `ACTIVE_STAGES`, `DEFAULT_SETTINGS`. |
| `app/services/opd_clinical.py` | Encounters + prescriptions. `get_or_create_draft(clinic_id, appointment)`, `save_vitals(…)`, `save_notes(…, expected_updated_at)`, `sign_encounter(clinic, encounter_id, user)`, `amend_encounter(…)`, `patient_history(clinic_id, patient_id, family_member_id, limit=20)`, `save_prescription_draft(…)`, `allergy_warnings(allergies, allergies_status, items) -> list[dict]`, `sign_prescription(clinic, rx_id, user, acknowledgements)`, `amend_prescription(…)`, `send_prescription(clinic, rx_id, actor)`. **Imports nothing from `ai_engine`, `ai_gateway`, `faq_engine`, `app.voice`.** Static `ALLERGY_CLASSES: dict[str, frozenset[str]]` (penicillin→amoxicillin/ampicillin/…, sulfa→sulfamethoxazole/…, nsaid→ibuprofen/diclofenac/aspirin/naproxen/…, cephalosporin, macrolide, fluoroquinolone, opioid). `ponytail:` substring + class map; upgrade to a drug-master DB when one is licensed. |
| `app/services/opd_billing.py` | `ensure_visit_invoice(clinic, appointment_id, actor)`, `update_draft(…, expected_updated_at)`, `issue_invoice(…)`, `record_receipt(clinic, invoice_id, mode, amount_paise, reference, actor)`, `refund(…)`, `void_invoice(…)`, `create_payment_link(clinic, invoice_id, actor)`, `settle_payment_link(clinic_id, invoice_id, gateway, gateway_payment_id, amount_paise)` (webhook-facing, idempotent), `open_shift(…)`, `current_shift(…)`, `close_shift(…)`, `collections_summary(clinic_id, date, branch_id)`, `catalog(clinic)`. |
| `app/services/opd_pdf.py` | `render_prescription_pdf(rx, items) -> bytes`, `render_invoice_pdf(invoice, items, receipts) -> bytes`. fpdf2, A5 portrait for Rx, A4 for invoice; renders **only snapshots stored on the row** (deterministic reprints). Fonts from `app/assets/fonts/`. |
| `app/routers/opd.py` | `router = APIRouter(prefix="/admin/opd")` (all clinic routes, Part 3) and `public_router = APIRouter(prefix="/public")` (display). |
| `admin/queue-display.html` | Standalone hallway TV page (Part 4 §4.10). |
| `admin/vendor/qrcode.min.js` | Vendored MIT QR generator (UPI QR), served like Chart.js. Pin version + SHA-256 in a comment. |
| `app/assets/fonts/NotoSans-Regular.ttf`, `NotoSans-Bold.ttf`, `NotoSansDevanagari-Regular.ttf`, `NotoSansTelugu-Regular.ttf` + `OFL.txt` | PDF fonts (SIL OFL). |
| Tests | Listed in `status.md` per phase. |

### 1.3 Backend — modified files

| Path | Symbol | Change |
|---|---|---|
| `app/tenancy.py` | `TENANT_OWNED_TABLES` | `+` the 7 OPD tables (§2.11). |
| `app/services/tenant.py` | `OPT_IN_FEATURES` | `+ "opd_enabled": "OPD OS (Front Desk, Queue, Consultation, Billing)"`. |
| | **new** `opd_enabled(clinic)` | `account_type == 'tenant'` **and** `features.opd_enabled is True` **and** `has_feature(clinic, "booking")`. Never `has_feature(clinic, "opd_enabled")`. |
| | **new** `opd_eligible(clinic)` | Same minus the opt-in flag; used by the platform toggle to reject partner labs and plans without `booking`. |
| `app/services/permissions.py` | `PERMISSIONS` | `+ OPD_FRONT_DESK, OPD_CLINICAL, OPD_BILLING, OPD_ADMIN` (comment: routes still require `opd_enabled`). |
| | `STAFF_ROLES` | `+ "DOCTOR", "CASHIER"`. |
| | `ROLE_PRESETS` | `RECEPTIONIST`, `FRONT_DESK` → `["OPD_FRONT_DESK"]`; `DOCTOR` → `["OPD_CLINICAL"]`; `CASHIER` → `["OPD_BILLING"]`. Presets apply at account creation only (`resolve_permissions`), so existing accounts gain nothing retroactively. |
| `app/routers/admin.py` | `AdminUser` | `+ doctor_id: Optional[str]` (ctor kwarg, default None). |
| | `verify_credentials` | Select `clinic_admins.doctor_id` with the session's admin row; pass into `AdminUser`. Env/owner sentinel users get `None`. |
| | `GET /admin/me` (≈ lines 775–830) | `+ "opd_enabled": opd_enabled(clinic)`, `"opd_state": effective_state`, `"opd_doctor_id": user.doctor_id`. No-clinic branch returns `False/None`. |
| | staff create/update models + handlers | Accept `doctor_id` (must be a doctor of the **same** clinic → 422 otherwise; unique per clinic → 409), `staff_role` DOCTOR/CASHIER. `DOCTOR` requires `doctor_id`. |
| | doctors create/update models | `+ registration_number`, `registration_council` (optional; format-checked). |
| | `DELETE /admin/doctors/{id}` | Catch FK violation `23503` → **409** "Doctor has OPD clinical or billing records — deactivate instead." (today it would surface as 500). |
| | `POST /admin/doctors/{doctor_name}/queue/call-next` | If `opd_enabled(clinic)` and state READY → delegate to `opd.opd_call_next()` (resolve doctor by name **within scope**). Else unchanged. |
| | `POST /admin/appointments/{id}/check-in` | If OPD READY → pass `initial_queue_status` from settings. Else unchanged. |
| `app/database.py` | `check_in_appointment` | `+ initial_queue_status: str = "waiting"` (default keeps every current caller's queue value identical); also writes `checked_in_at = now()` and `queue_timeline = {<stage>: iso}` (new columns, harmless for non-OPD clinics). |
| | `get_available_slots` (line 950) | Active-appointment read adds `.eq("is_walk_in", False)`. **Without this, every walk-in silently consumes a WhatsApp/voice slot.** |
| | `book_appointment` (line 1149) | Accept `booking_channel` in `data` (validated); slot pre-check query adds `.eq("is_walk_in", False)`. |
| | `get_patient_queue_status` (line 1587) | If clinic is OPD-enabled → return `opd.queue_position()` (counts `registered/vitals_pending/waiting` tokens ahead + `in_consultation`); else unchanged. Output keys identical, so WhatsApp + voice callers need no change. |
| `app/services/payment.py` | `create_booking_with_payment` | Accept + persist `booking_channel`; slot pre-check adds `is_walk_in = false`. |
| | `process_payment_webhook` | **First branch**: `notes.kind == "opd_invoice"` → `opd_billing.settle_payment_link(...)`; clinic id taken from the **invoice row**, must equal the resolved event clinic, else log + 200 no-op. Existing booking path untouched below it. |
| | `process_phonepe_webhook` | Merchant order ids `OPDINV-<invoice_uuid>-<n>` → same settle path. |
| | **new** `create_opd_payment_link(clinic, invoice)` | Reuses `_create_payment_link` / `_start_phonepe_checkout` plumbing with OPD notes. |
| `app/services/scheduler.py` | `send_24h_reminders`, `send_2h_reminders` | `.eq("is_walk_in", False)` (a walk-in is already at the desk). |
| | `check_doctor_leaves` | Exclude `is_walk_in` (same-day, handled in person). |
| `app/services/conversation.py` | every booking call site | Pass `booking_channel="whatsapp"`. `_handle_queue_status` unchanged (inherits the `get_patient_queue_status` delegation). |
| `app/voice/tools.py` | `KriyaTools._create` | Pass `booking_channel="voice"`. `queue_status` unchanged. |
| `app/services/data_retention.py` | erasure step 4 | Split: delete family members with no OPD references; redact referenced ones (`full_name = '[REDACTED:<first 8 of id>]'` — unique, so `UNIQUE (clinic_id, primary_phone, full_name)` holds). Also NULL new patient contact columns (`address_line`, `city`, `pincode`, `emergency_contact_*`) — never `'[REDACTED]'` (it would violate the pincode/phone CHECKs). |
| `app/routers/platform.py` | `ClinicFeatureOverride` | `+ confirm_disable: bool = False`. |
| | `update_clinic_feature` | For `opd_enabled`: eligibility guard (400); on enable → `opd.provision_defaults()` + state (`DISABLED`→`READY` if `went_live_at` else `CONFIGURING`/`NOT_CONFIGURED`); on disable → `deactivation_preview()`; if blockers and not `confirm_disable` → **409** `{preview}`; else set `opd_state='DISABLED'`. Audit-logged as today. |
| | **new** `GET /platform/clinics/{clinic_id}/opd/deactivation-preview` | Owner auth. Returns preview (§3.9). |
| | `GET /platform/clinics/{clinic_id}/deletion-preview` | Add OPD counts + `opd_purge_required: bool`. |
| | `DELETE /platform/clinics/{clinic_id}` | If OPD rows exist: require `confirm_opd_purge=true`, call RPC `opd_purge_clinic(p_clinic_id)` **before** the clinic delete (immutability triggers otherwise block the cascade, by design). |
| `app/main.py` | routers | `include_router(opd.router)`, `include_router(opd.public_router)`; `GET /public/queue-display` → `FileResponse("admin/queue-display.html")`; `GET /panel-assets/qrcode.min.js` like Chart.js. |
| `app/utils/security.py` | CSP | No change (`'self' 'unsafe-inline'`); confirm `/public/queue-display` gets the same headers + `X-Robots-Tag: noindex`. |
| `requirements.txt` | | `+ fpdf2>=2.7,<3`, `uharfbuzz>=0.39,<1`. |
| `Dockerfile` | | Nothing, unless the image lacks a `uharfbuzz` wheel (verify in Phase 1.3). |
| `docs/agent-context/03,04,05,08,13` | | Document new tables, routes, nav, permissions. |

### 1.4 Frontend

| File | Section | Change |
|---|---|---|
| `admin/platform.html` | Clinic modal, after "AI Voice Receptionist" card (≈ line 1961) | New "OPD OS" card (§4.1); `toggleOpd()`, `renderOpdDeactivationDialog()`; load `opd_state` with clinic detail. Directory: `OPD` badge column. |
| `admin/index.html` | `:root` + light theme | OPD tokens (§4.2). |
| | Sidebar (after `voice` nav ≈ line 1767) | OPD group with 6 links, `data-opd` gating (§4.3). |
| | Pages | `#page-opdsetup`, `#page-opddesk`, `#page-opdqueue`, `#page-opdworkspace` (+ Rx studio panel), `#page-opdbilling`, `#page-opdanalytics`. |
| | Staff forms (≈ lines 3753, 4186) | `data-opd="perm"` checkboxes for 4 permissions; Doctor link `<select>` for `DOCTOR`. |
| | Doctor form | Registration number + council inputs. |
| | `me` loaders (≈ lines 4963, 5122) | `myOpd = !!me.opd_enabled; myOpdState = me.opd_state; myDoctorId = me.opd_doctor_id;` → `applyOpdGates()`. |
| `admin/admin.js` | — | **Do not touch** (dead file). |
| `admin/queue-display.html` | new | §4.10. |

---

## Part 2 — Migration `103_opd_os_core.sql`

Properties: idempotent (`IF NOT EXISTS`, `DROP … IF EXISTS` before `ADD CONSTRAINT`), one transaction (runner), **additive and backward-compatible with pre-OPD code** (new columns have defaults; the index predicates only *narrow*), never inserts into `schema_migrations` (`test_rt16`). Every new table has `clinic_id NOT NULL` and `UNIQUE (clinic_id, id)` so children reference parents through **composite tenant FKs**: the database itself rejects an encounter whose patient belongs to another clinic, even under `BYPASSRLS`.

```sql
-- ============================================================================
-- Migration 103: Kriya OPD OS core (Phase 1)
-- Walk-ins, OPD queue stages, demographics/MRN, clinical encounters,
-- clinician-signed e-prescriptions, invoices, receipts, cashier shifts.
-- Additive + backward compatible: pre-103 application code runs unchanged.
-- Apply BEFORE deploying code that ships this file (lifespan parity check).
-- Apply off-peak: index rebuilds on appointments take a SHARE lock.
-- ============================================================================

-- ── 1. Composite tenant keys on existing parents (FK targets) ───────────────
-- id is already unique, so these can never fail; they let child tables use
-- FOREIGN KEY (clinic_id, x_id) and make cross-tenant references impossible.
CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_clinic_id_id       ON patients       (clinic_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_family_members_clinic_id_id ON family_members (clinic_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_appointments_clinic_id_id   ON appointments   (clinic_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_doctors_clinic_id_id        ON doctors        (clinic_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_branches_clinic_id_id       ON branches       (clinic_id, id);

-- ── 2. clinics: module state, settings, counters, TV display token ─────────
ALTER TABLE clinics
    ADD COLUMN IF NOT EXISTS opd_state              TEXT  NOT NULL DEFAULT 'NOT_CONFIGURED',
    ADD COLUMN IF NOT EXISTS opd_settings           JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS opd_counters           JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS opd_display_token_hash TEXT  NULL;
-- DEGRADED is computed at read time from the checklist, never stored (it would go stale).
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_opd_state_check;
ALTER TABLE clinics ADD  CONSTRAINT clinics_opd_state_check
    CHECK (opd_state IN ('NOT_CONFIGURED', 'CONFIGURING', 'READY', 'DISABLED'));
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_opd_json_check;
ALTER TABLE clinics ADD  CONSTRAINT clinics_opd_json_check
    CHECK (jsonb_typeof(opd_settings) = 'object' AND jsonb_typeof(opd_counters) = 'object');
CREATE UNIQUE INDEX IF NOT EXISTS uq_clinics_opd_display_token_hash
    ON clinics (opd_display_token_hash) WHERE opd_display_token_hash IS NOT NULL;

-- ── 3. doctors: NMC registration stamped on every signed record ────────────
ALTER TABLE doctors
    ADD COLUMN IF NOT EXISTS registration_number  TEXT NULL,
    ADD COLUMN IF NOT EXISTS registration_council TEXT NULL;
ALTER TABLE doctors DROP CONSTRAINT IF EXISTS doctors_registration_check;
ALTER TABLE doctors ADD  CONSTRAINT doctors_registration_check CHECK (
    (registration_number  IS NULL OR registration_number ~ '^[A-Za-z0-9/.\- ]{3,40}$')
AND (registration_council IS NULL OR char_length(registration_council) BETWEEN 2 AND 120));

-- ── 4. clinic_admins: a login may BE a doctor (required to sign) ───────────
ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS doctor_id UUID NULL;
ALTER TABLE clinic_admins DROP CONSTRAINT IF EXISTS clinic_admins_doctor_fk;
ALTER TABLE clinic_admins ADD  CONSTRAINT clinic_admins_doctor_fk
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors (clinic_id, id)
    ON DELETE SET NULL (doctor_id);                       -- PG >= 15, see F17
ALTER TABLE clinic_admins DROP CONSTRAINT IF EXISTS clinic_admins_doctor_needs_clinic;
ALTER TABLE clinic_admins ADD  CONSTRAINT clinic_admins_doctor_needs_clinic
    CHECK (doctor_id IS NULL OR clinic_id IS NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS uq_clinic_admins_doctor
    ON clinic_admins (clinic_id, doctor_id) WHERE doctor_id IS NOT NULL;

-- ── 5. patients + family_members: demographics, MRN, allergies ─────────────
ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS mrn                        TEXT     NULL,
    ADD COLUMN IF NOT EXISTS date_of_birth              DATE     NULL,
    ADD COLUMN IF NOT EXISTS age_years                  SMALLINT NULL,
    ADD COLUMN IF NOT EXISTS age_recorded_on            DATE     NULL,
    ADD COLUMN IF NOT EXISTS gender                     TEXT     NULL,
    ADD COLUMN IF NOT EXISTS address_line               TEXT     NULL,
    ADD COLUMN IF NOT EXISTS city                       TEXT     NULL,
    ADD COLUMN IF NOT EXISTS pincode                    TEXT     NULL,
    ADD COLUMN IF NOT EXISTS emergency_contact_name     TEXT     NULL,
    ADD COLUMN IF NOT EXISTS emergency_contact_phone    TEXT     NULL,
    ADD COLUMN IF NOT EXISTS emergency_contact_relation TEXT     NULL,
    ADD COLUMN IF NOT EXISTS allergies                  TEXT[]   NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS allergies_status           TEXT     NOT NULL DEFAULT 'unknown';
ALTER TABLE patients DROP CONSTRAINT IF EXISTS patients_opd_demographics_check;
ALTER TABLE patients ADD  CONSTRAINT patients_opd_demographics_check CHECK (
    (gender IS NULL OR gender IN ('male', 'female', 'other', 'undisclosed'))
AND (age_years IS NULL OR (age_years BETWEEN 0 AND 130 AND age_recorded_on IS NOT NULL))
AND (date_of_birth IS NULL OR date_of_birth >= DATE '1900-01-01')
AND (pincode IS NULL OR pincode ~ '^[1-9][0-9]{5}$')
AND (address_line IS NULL OR char_length(address_line) <= 300)
AND (city IS NULL OR char_length(city) <= 80)
AND (emergency_contact_name IS NULL OR char_length(emergency_contact_name) <= 100)
AND (emergency_contact_phone IS NULL OR emergency_contact_phone ~ '^\+?[0-9]{10,15}$')
AND (emergency_contact_relation IS NULL OR char_length(emergency_contact_relation) <= 40)
-- "unknown" (never asked) is clinically different from "none known" (asked, NKA).
AND allergies_status IN ('unknown', 'none_known', 'recorded')
AND ((allergies_status = 'recorded') = (cardinality(allergies) > 0))
AND cardinality(allergies) <= 50);
CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_clinic_mrn
    ON patients (clinic_id, mrn) WHERE mrn IS NOT NULL;

ALTER TABLE family_members
    ADD COLUMN IF NOT EXISTS mrn              TEXT     NULL,
    ADD COLUMN IF NOT EXISTS date_of_birth    DATE     NULL,
    ADD COLUMN IF NOT EXISTS age_years        SMALLINT NULL,
    ADD COLUMN IF NOT EXISTS age_recorded_on  DATE     NULL,
    ADD COLUMN IF NOT EXISTS gender           TEXT     NULL,
    ADD COLUMN IF NOT EXISTS allergies        TEXT[]   NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS allergies_status TEXT     NOT NULL DEFAULT 'unknown';
ALTER TABLE family_members DROP CONSTRAINT IF EXISTS family_members_opd_demographics_check;
ALTER TABLE family_members ADD  CONSTRAINT family_members_opd_demographics_check CHECK (
    (gender IS NULL OR gender IN ('male', 'female', 'other', 'undisclosed'))
AND (age_years IS NULL OR (age_years BETWEEN 0 AND 130 AND age_recorded_on IS NOT NULL))
AND (date_of_birth IS NULL OR date_of_birth >= DATE '1900-01-01')
AND allergies_status IN ('unknown', 'none_known', 'recorded')
AND ((allergies_status = 'recorded') = (cardinality(allergies) > 0))
AND cardinality(allergies) <= 50);
CREATE UNIQUE INDEX IF NOT EXISTS uq_family_members_clinic_mrn
    ON family_members (clinic_id, mrn) WHERE mrn IS NOT NULL;

-- ── 6. appointments: walk-ins, channel, visit type, OPD stages ─────────────
ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS is_walk_in       BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS booking_channel  TEXT        NULL,   -- NULL = pre-103 row, unknown
    ADD COLUMN IF NOT EXISTS visit_type       TEXT        NULL,
    ADD COLUMN IF NOT EXISTS family_member_id UUID        NULL,
    ADD COLUMN IF NOT EXISTS checked_in_at    TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS queue_timeline   JSONB       NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_opd_meta_check;
ALTER TABLE appointments ADD  CONSTRAINT appointments_opd_meta_check CHECK (
    (booking_channel IS NULL OR booking_channel IN ('whatsapp', 'voice', 'front_desk', 'web'))
AND (visit_type IS NULL OR visit_type IN ('new', 'followup', 'review'))
AND (NOT is_walk_in OR (booking_type = 'consultation'
                        AND doctor_id IS NOT NULL
                        AND booking_channel = 'front_desk'))
AND jsonb_typeof(queue_timeline) = 'object');

ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_family_member_fk;
ALTER TABLE appointments ADD  CONSTRAINT appointments_family_member_fk
    FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id)
    ON DELETE SET NULL (family_member_id);                -- PG >= 15, see F17

-- 6b. Widen queue_status. Migration 019 declared the CHECK inline, so its
-- name is generated; drop every CHECK that mentions queue_status by definition.
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'public.appointments'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%queue_status%'
    LOOP
        EXECUTE format('ALTER TABLE appointments DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
ALTER TABLE appointments ADD CONSTRAINT appointments_queue_status_check CHECK (
    queue_status IS NULL OR queue_status IN (
        'registered', 'vitals_pending', 'waiting', 'in_consultation',
        'billing', 'completed', 'cancelled',
        'done'   -- legacy terminal state, still written by non-OPD clinics
    ));

-- 6c. Scheduled-slot guards ignore walk-ins. SAME NAMES: is_slot_conflict()
-- matches the 'uq_appointment_active_slot' prefix. Inside this transaction
-- there is no moment without a guard; the predicate only narrows, and no
-- walk-in row exists yet, so the rebuild cannot fail on existing data.
DROP INDEX IF EXISTS uq_appointment_active_slot;
CREATE UNIQUE INDEX uq_appointment_active_slot
    ON appointments (clinic_id, doctor_id, appointment_date, appointment_time)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review')
      AND booking_type = 'consultation'
      AND doctor_id IS NOT NULL
      AND is_walk_in = false;
DROP INDEX IF EXISTS uq_appointment_active_slot_unassigned;
CREATE UNIQUE INDEX uq_appointment_active_slot_unassigned
    ON appointments (clinic_id, doctor_name, appointment_date, appointment_time)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review')
      AND booking_type = 'consultation'
      AND doctor_id IS NULL
      AND is_walk_in = false;
-- Token uniqueness (idx_unique_queue_token / idx_unique_lab_queue_token) is
-- deliberately untouched: walk-ins and booked arrivals share one token line.

CREATE INDEX IF NOT EXISTS idx_appointments_opd_queue
    ON appointments (clinic_id, appointment_date, doctor_id, queue_status)
    WHERE token_number IS NOT NULL;

-- ── 7. Counters: gapless per-clinic numbering under a row lock ─────────────
CREATE OR REPLACE FUNCTION opd_next_counter(p_clinic_id UUID, p_key TEXT)
RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE v INTEGER;
BEGIN
    UPDATE clinics
       SET opd_counters = jsonb_set(opd_counters, ARRAY[p_key],
                                    to_jsonb(COALESCE((opd_counters ->> p_key)::INTEGER, 0) + 1), true)
     WHERE id = p_clinic_id
    RETURNING (opd_counters ->> p_key)::INTEGER INTO v;
    IF v IS NULL THEN
        RAISE EXCEPTION 'opd_unknown_clinic:%', p_clinic_id USING ERRCODE = 'P0002';
    END IF;
    RETURN v;
END $$;

-- p_width digits until 10^p_width - 1, then the full number: lpad() would
-- TRUNCATE 123456 to 12345 and mint a duplicate-looking invoice number.
CREATE OR REPLACE FUNCTION opd_format_number(p_prefix TEXT, p_year INTEGER, p_seq INTEGER, p_width INTEGER)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT p_prefix
        || CASE WHEN p_year IS NULL THEN '' ELSE p_year::TEXT || '-' END
        || CASE WHEN p_seq < (10 ^ p_width)::INTEGER THEN lpad(p_seq::TEXT, p_width, '0') ELSE p_seq::TEXT END
$$;

-- MRN is shared by patients and family_members (one counter), so it is unique per clinic across both.
CREATE OR REPLACE FUNCTION opd_assign_mrn(p_clinic_id UUID, p_patient_id UUID, p_family_member_id UUID DEFAULT NULL)
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE v_mrn TEXT;
BEGIN
    IF p_family_member_id IS NULL THEN
        SELECT mrn INTO v_mrn FROM patients
         WHERE id = p_patient_id AND clinic_id = p_clinic_id FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'opd_patient_not_found' USING ERRCODE = 'P0002'; END IF;
        IF v_mrn IS NULL THEN
            v_mrn := opd_format_number('MRN-', NULL, opd_next_counter(p_clinic_id, 'mrn'), 6);
            UPDATE patients SET mrn = v_mrn WHERE id = p_patient_id AND clinic_id = p_clinic_id;
        END IF;
    ELSE
        -- the dependant must belong to THIS account holder's phone in THIS clinic
        SELECT fm.mrn INTO v_mrn
          FROM family_members fm
          JOIN patients p ON p.clinic_id = fm.clinic_id AND p.phone = fm.primary_phone
         WHERE fm.id = p_family_member_id AND fm.clinic_id = p_clinic_id AND p.id = p_patient_id
           FOR UPDATE OF fm;
        IF NOT FOUND THEN RAISE EXCEPTION 'opd_family_member_not_found' USING ERRCODE = 'P0002'; END IF;
        IF v_mrn IS NULL THEN
            v_mrn := opd_format_number('MRN-', NULL, opd_next_counter(p_clinic_id, 'mrn'), 6);
            UPDATE family_members SET mrn = v_mrn WHERE id = p_family_member_id AND clinic_id = p_clinic_id;
        END IF;
    END IF;
    RETURN v_mrn;
END $$;

-- ── 8. Shared trigger helpers ──────────────────────────────────────────────
CREATE OR REPLACE FUNCTION opd_touch_updated_at() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- Only opd_purge_clinic() sets this, for exactly one clinic, transaction-local.
CREATE OR REPLACE FUNCTION opd_purging(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT current_setting('kriya.opd_purge_clinic', true) = p_clinic_id::TEXT
$$;

-- ── 9. Tables ──────────────────────────────────────────────────────────────

-- 9.1 Clinical encounter: vitals + structured notes. Signed rows are immutable;
-- an amendment is a NEW row (version+1, supersedes_id) — that row chain IS the
-- revision history. Drafts are working copies, not legal records.
CREATE TABLE IF NOT EXISTS opd_encounters (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NULL,
    appointment_id      UUID NOT NULL,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    doctor_id           UUID NOT NULL,
    version             SMALLINT NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 99),
    supersedes_id       UUID NULL,
    status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'signed', 'superseded')),
    -- vitals (ranges reject typos such as 1200 for 120; the UI shows the same limits)
    bp_systolic         SMALLINT NULL CHECK (bp_systolic  BETWEEN 50 AND 300),
    bp_diastolic        SMALLINT NULL CHECK (bp_diastolic BETWEEN 20 AND 200),
    pulse_bpm           SMALLINT NULL CHECK (pulse_bpm    BETWEEN 20 AND 250),
    temperature_c       NUMERIC(4,1) NULL CHECK (temperature_c BETWEEN 30 AND 45),
    spo2_pct            SMALLINT NULL CHECK (spo2_pct     BETWEEN 50 AND 100),
    weight_kg           NUMERIC(5,2) NULL CHECK (weight_kg > 0 AND weight_kg <= 400),
    height_cm           NUMERIC(5,1) NULL CHECK (height_cm BETWEEN 30 AND 250),
    -- NUMERIC(6,1): 400 kg at 30 cm is 4444.4 and must not overflow the insert
    bmi                 NUMERIC(6,1) GENERATED ALWAYS AS (
                            CASE WHEN weight_kg IS NOT NULL AND height_cm IS NOT NULL
                                 THEN round(weight_kg / ((height_cm / 100.0) ^ 2), 1) END) STORED,
    vitals_recorded_at  TIMESTAMPTZ NULL,
    vitals_recorded_by  UUID NULL,   -- clinic_admins.id; no FK: staff rows can be deleted, the record stays
    vitals_recorded_by_name TEXT NULL,
    -- clinical documentation
    chief_complaints    TEXT NULL CHECK (char_length(chief_complaints)   <= 4000),
    clinical_findings   TEXT NULL CHECK (char_length(clinical_findings)  <= 8000),
    examination_notes   TEXT NULL CHECK (char_length(examination_notes)  <= 8000),
    diagnoses           JSONB NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(diagnoses) = 'array' AND jsonb_array_length(diagnoses) <= 20),
                        -- [{"system":"ICD-10"|null, "code":"J06.9"|null, "text":"Acute URTI"}]
    advice              TEXT NULL CHECK (char_length(advice) <= 4000),
    follow_up_date      DATE NULL,
    -- lifecycle
    started_at          TIMESTAMPTZ NULL,
    signed_at           TIMESTAMPTZ NULL,
    signed_by_admin_id  UUID NULL,
    signer_snapshot     JSONB NULL,  -- {doctor_id,name,qualifications,registration_number,registration_council}
    superseded_at       TIMESTAMPTZ NULL,
    created_by          UUID NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_encounters_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_encounters_version_key UNIQUE (clinic_id, appointment_id, version),
    CONSTRAINT opd_encounters_signed_complete CHECK (
        status = 'draft' OR (signed_at IS NOT NULL AND signed_by_admin_id IS NOT NULL
                             AND signer_snapshot ? 'registration_number')),
    CONSTRAINT opd_encounters_supersede_shape CHECK ((version = 1) = (supersedes_id IS NULL)),
    CONSTRAINT opd_encounters_appointment_fk FOREIGN KEY (clinic_id, appointment_id)   REFERENCES appointments (clinic_id, id),
    CONSTRAINT opd_encounters_patient_fk     FOREIGN KEY (clinic_id, patient_id)       REFERENCES patients (clinic_id, id),
    CONSTRAINT opd_encounters_family_fk      FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id),
    CONSTRAINT opd_encounters_doctor_fk      FOREIGN KEY (clinic_id, doctor_id)        REFERENCES doctors (clinic_id, id),
    CONSTRAINT opd_encounters_branch_fk      FOREIGN KEY (clinic_id, branch_id)        REFERENCES branches (clinic_id, id),
    CONSTRAINT opd_encounters_supersedes_fk  FOREIGN KEY (clinic_id, supersedes_id)    REFERENCES opd_encounters (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_encounters_one_draft  ON opd_encounters (clinic_id, appointment_id) WHERE status = 'draft';
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_encounters_one_signed ON opd_encounters (clinic_id, appointment_id) WHERE status = 'signed';
CREATE INDEX IF NOT EXISTS idx_opd_encounters_patient ON opd_encounters (clinic_id, patient_id, family_member_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opd_encounters_doctor  ON opd_encounters (clinic_id, doctor_id, created_at DESC);

-- 9.2 Prescription header. Same immutability + amendment model as encounters.
CREATE TABLE IF NOT EXISTS opd_prescriptions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    encounter_id        UUID NOT NULL,
    appointment_id      UUID NOT NULL,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    doctor_id           UUID NOT NULL,
    version             SMALLINT NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 99),
    supersedes_id       UUID NULL,
    status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'signed', 'superseded')),
    general_instructions TEXT NULL CHECK (char_length(general_instructions) <= 2000),
    allergy_review      JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(allergy_review) = 'array'),
                        -- [{"line_no":2,"allergen":"penicillin","drug":"Amoxicillin","override_reason":"…"}]
    signed_at           TIMESTAMPTZ NULL,
    signed_by_admin_id  UUID NULL,
    signer_snapshot     JSONB NULL,
    letterhead_snapshot JSONB NULL,  -- clinic name/address/phone/branch at signing → deterministic reprint
    patient_snapshot    JSONB NULL,  -- name, mrn, age, gender, allergies at signing
    superseded_at       TIMESTAMPTZ NULL,
    -- delivery (the only columns that may change after signing)
    delivery_status     TEXT NOT NULL DEFAULT 'not_sent' CHECK (delivery_status IN ('not_sent', 'sent', 'failed')),
    whatsapp_message_id TEXT NULL,
    last_sent_at        TIMESTAMPTZ NULL,
    send_count          SMALLINT NOT NULL DEFAULT 0 CHECK (send_count BETWEEN 0 AND 50),
    created_by          UUID NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_prescriptions_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_prescriptions_version_key UNIQUE (clinic_id, appointment_id, version),
    CONSTRAINT opd_prescriptions_signed_complete CHECK (
        status = 'draft' OR (signed_at IS NOT NULL AND signed_by_admin_id IS NOT NULL
                             AND signer_snapshot ? 'registration_number'
                             AND letterhead_snapshot IS NOT NULL AND patient_snapshot IS NOT NULL)),
    CONSTRAINT opd_prescriptions_supersede_shape CHECK ((version = 1) = (supersedes_id IS NULL)),
    CONSTRAINT opd_prescriptions_encounter_fk   FOREIGN KEY (clinic_id, encounter_id)     REFERENCES opd_encounters (clinic_id, id),
    CONSTRAINT opd_prescriptions_appointment_fk FOREIGN KEY (clinic_id, appointment_id)   REFERENCES appointments (clinic_id, id),
    CONSTRAINT opd_prescriptions_patient_fk     FOREIGN KEY (clinic_id, patient_id)       REFERENCES patients (clinic_id, id),
    CONSTRAINT opd_prescriptions_family_fk      FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id),
    CONSTRAINT opd_prescriptions_doctor_fk      FOREIGN KEY (clinic_id, doctor_id)        REFERENCES doctors (clinic_id, id),
    CONSTRAINT opd_prescriptions_supersedes_fk  FOREIGN KEY (clinic_id, supersedes_id)    REFERENCES opd_prescriptions (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_prescriptions_one_draft  ON opd_prescriptions (clinic_id, appointment_id) WHERE status = 'draft';
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_prescriptions_one_signed ON opd_prescriptions (clinic_id, appointment_id) WHERE status = 'signed';
CREATE INDEX IF NOT EXISTS idx_opd_prescriptions_patient ON opd_prescriptions (clinic_id, patient_id, family_member_id, created_at DESC);

-- 9.3 Prescription lines. Writable only while the parent is a draft (trigger).
CREATE TABLE IF NOT EXISTS opd_prescription_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    prescription_id     UUID NOT NULL,
    line_no             SMALLINT NOT NULL CHECK (line_no BETWEEN 1 AND 40),
    drug_name           TEXT NOT NULL CHECK (char_length(btrim(drug_name)) BETWEEN 2 AND 200),
    formulation         TEXT NOT NULL CHECK (formulation IN ('tablet', 'capsule', 'syrup', 'suspension', 'injection',
                            'drops', 'ointment', 'cream', 'gel', 'inhaler', 'powder', 'lotion', 'spray', 'patch', 'other')),
    strength            TEXT NULL CHECK (char_length(strength) <= 60),
    dosage              TEXT NOT NULL CHECK (char_length(btrim(dosage)) BETWEEN 1 AND 60),
    route               TEXT NOT NULL DEFAULT 'oral' CHECK (route IN ('oral', 'topical', 'iv', 'im', 'sc', 'inhalation',
                            'nasal', 'ophthalmic', 'otic', 'rectal', 'vaginal', 'sublingual', 'other')),
    frequency           TEXT NOT NULL CHECK (char_length(btrim(frequency)) BETWEEN 1 AND 40),  -- OD/BD/TDS/QID/HS/SOS/STAT or custom
    timing              TEXT NOT NULL DEFAULT 'any' CHECK (timing IN ('before_food', 'after_food', 'with_food',
                            'empty_stomach', 'bedtime', 'any')),
    duration_days       SMALLINT NULL CHECK (duration_days BETWEEN 1 AND 365),               -- NULL = SOS / until review
    instructions        TEXT NULL CHECK (char_length(instructions) <= 500),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_prescription_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_prescription_items_line_key UNIQUE (prescription_id, line_no),
    CONSTRAINT opd_prescription_items_rx_fk FOREIGN KEY (clinic_id, prescription_id)
        REFERENCES opd_prescriptions (clinic_id, id) ON DELETE CASCADE
);

-- 9.4 Invoice header. Number assigned atomically at issue (gapless). Subtotal
-- is maintained by the item trigger; paid_paise ONLY by the receipt trigger.
CREATE TABLE IF NOT EXISTS opd_invoices (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NULL,
    appointment_id      UUID NULL,
    encounter_id        UUID NULL,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'issued', 'partially_paid', 'paid', 'void')),
    invoice_year        SMALLINT NULL,
    invoice_seq         INTEGER  NULL CHECK (invoice_seq > 0),
    invoice_number      TEXT GENERATED ALWAYS AS (
                            CASE WHEN invoice_seq IS NULL THEN NULL
                                 ELSE opd_format_number('INV-', invoice_year, invoice_seq, 5) END) STORED,
    subtotal_paise      INTEGER NOT NULL DEFAULT 0 CHECK (subtotal_paise >= 0),
    discount_paise      INTEGER NOT NULL DEFAULT 0 CHECK (discount_paise >= 0),
    total_paise         INTEGER GENERATED ALWAYS AS (subtotal_paise - discount_paise) STORED,
    paid_paise          INTEGER NOT NULL DEFAULT 0 CHECK (paid_paise >= 0),
    discount_reason     TEXT NULL CHECK (char_length(discount_reason) <= 200),
    patient_snapshot    JSONB NULL,
    notes               TEXT NULL CHECK (char_length(notes) <= 1000),
    payment_link_gateway TEXT NULL CHECK (payment_link_gateway IN ('razorpay', 'phonepe')),
    payment_link_id     TEXT NULL,
    payment_link_url    TEXT NULL,
    payment_link_amount_paise INTEGER NULL CHECK (payment_link_amount_paise > 0),
    payment_link_created_at TIMESTAMPTZ NULL,
    issued_at           TIMESTAMPTZ NULL,
    issued_by           UUID NULL,
    voided_at           TIMESTAMPTZ NULL,
    voided_by           UUID NULL,
    void_reason         TEXT NULL CHECK (char_length(void_reason) BETWEEN 5 AND 300),
    created_by          UUID NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_invoices_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_invoices_number_key UNIQUE (clinic_id, invoice_year, invoice_seq),
    CONSTRAINT opd_invoices_discount_le_subtotal CHECK (discount_paise <= subtotal_paise),
    CONSTRAINT opd_invoices_numbered_when_issued CHECK (status IN ('draft', 'void') OR invoice_seq IS NOT NULL),
    CONSTRAINT opd_invoices_void_complete CHECK (status <> 'void' OR (voided_at IS NOT NULL AND void_reason IS NOT NULL)),
    -- status is a pure function of paid vs total; over-payment and over-refund fail here
    CONSTRAINT opd_invoices_paid_consistent CHECK (
        (status IN ('draft', 'issued', 'void') AND paid_paise = 0)
     OR (status = 'partially_paid' AND paid_paise > 0 AND paid_paise < subtotal_paise - discount_paise)
     OR (status = 'paid' AND paid_paise = subtotal_paise - discount_paise)),
    CONSTRAINT opd_invoices_appointment_fk FOREIGN KEY (clinic_id, appointment_id)   REFERENCES appointments (clinic_id, id),
    CONSTRAINT opd_invoices_encounter_fk   FOREIGN KEY (clinic_id, encounter_id)     REFERENCES opd_encounters (clinic_id, id),
    CONSTRAINT opd_invoices_patient_fk     FOREIGN KEY (clinic_id, patient_id)       REFERENCES patients (clinic_id, id),
    CONSTRAINT opd_invoices_family_fk      FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id),
    CONSTRAINT opd_invoices_branch_fk      FOREIGN KEY (clinic_id, branch_id)        REFERENCES branches (clinic_id, id)
);
-- one live invoice per visit: no double billing
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_invoices_one_per_visit
    ON opd_invoices (clinic_id, appointment_id) WHERE appointment_id IS NOT NULL AND status <> 'void';
CREATE INDEX IF NOT EXISTS idx_opd_invoices_day     ON opd_invoices (clinic_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opd_invoices_open    ON opd_invoices (clinic_id, status) WHERE status IN ('issued', 'partially_paid');
CREATE INDEX IF NOT EXISTS idx_opd_invoices_patient ON opd_invoices (clinic_id, patient_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_invoices_payment_link
    ON opd_invoices (payment_link_gateway, payment_link_id) WHERE payment_link_id IS NOT NULL;

-- 9.5 Invoice lines (snapshotted description + price).
CREATE TABLE IF NOT EXISTS opd_invoice_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    invoice_id          UUID NOT NULL,
    line_no             SMALLINT NOT NULL CHECK (line_no BETWEEN 1 AND 100),
    item_type           TEXT NOT NULL CHECK (item_type IN ('consultation', 'nursing', 'diagnostic', 'procedure', 'other')),
    catalog_code        TEXT NULL CHECK (char_length(catalog_code) <= 40),
    doctor_id           UUID NULL,
    lab_test_id         UUID NULL,   -- reference only; price is snapshotted below
    description         TEXT NOT NULL CHECK (char_length(btrim(description)) BETWEEN 1 AND 200),
    quantity            SMALLINT NOT NULL DEFAULT 1 CHECK (quantity BETWEEN 1 AND 999),
    unit_price_paise    INTEGER NOT NULL CHECK (unit_price_paise BETWEEN 0 AND 100000000),
    discount_paise      INTEGER NOT NULL DEFAULT 0 CHECK (discount_paise >= 0),
    line_total_paise    INTEGER GENERATED ALWAYS AS (quantity * unit_price_paise - discount_paise) STORED,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_invoice_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_invoice_items_line_key UNIQUE (invoice_id, line_no),
    CONSTRAINT opd_invoice_items_discount_ok CHECK (discount_paise <= quantity * unit_price_paise),
    CONSTRAINT opd_invoice_items_invoice_fk FOREIGN KEY (clinic_id, invoice_id)
        REFERENCES opd_invoices (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT opd_invoice_items_doctor_fk FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors (clinic_id, id)
);

-- 9.6 Cashier shift (7th table — see F11). One open shift per cashier.
CREATE TABLE IF NOT EXISTS opd_cashier_shifts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NULL,
    cashier_admin_id    UUID NOT NULL,
    cashier_name        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    opening_float_paise INTEGER NOT NULL DEFAULT 0 CHECK (opening_float_paise BETWEEN 0 AND 100000000),
    closed_at           TIMESTAMPTZ NULL,
    expected_cash_paise INTEGER NULL,
    declared_cash_paise INTEGER NULL CHECK (declared_cash_paise >= 0),
    variance_paise      INTEGER GENERATED ALWAYS AS (declared_cash_paise - expected_cash_paise) STORED,
    close_notes         TEXT NULL CHECK (char_length(close_notes) <= 500),
    CONSTRAINT opd_cashier_shifts_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_cashier_shifts_closed_complete CHECK (
        status = 'open' OR (closed_at IS NOT NULL AND expected_cash_paise IS NOT NULL AND declared_cash_paise IS NOT NULL)),
    CONSTRAINT opd_cashier_shifts_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_cashier_shifts_one_open
    ON opd_cashier_shifts (clinic_id, cashier_admin_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_opd_cashier_shifts_day ON opd_cashier_shifts (clinic_id, opened_at DESC);

-- 9.7 Receipts: append-only money ledger. A refund is a new row (kind=refund).
CREATE TABLE IF NOT EXISTS opd_receipts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    invoice_id          UUID NOT NULL,
    shift_id            UUID NULL,           -- NULL only for online money (payment_link / prepaid_online)
    kind                TEXT NOT NULL DEFAULT 'payment' CHECK (kind IN ('payment', 'refund')),
    mode                TEXT NOT NULL CHECK (mode IN ('cash', 'upi', 'card', 'payment_link', 'prepaid_online')),
    amount_paise        INTEGER NOT NULL CHECK (amount_paise BETWEEN 1 AND 100000000),
    reference           TEXT NULL CHECK (char_length(reference) <= 100),  -- UPI UTR / card RRN
    gateway             TEXT NULL CHECK (gateway IN ('razorpay', 'phonepe')),
    gateway_payment_id  TEXT NULL,
    receipt_year        SMALLINT NULL,
    receipt_seq         INTEGER  NULL,
    receipt_number      TEXT GENERATED ALWAYS AS (
                            CASE WHEN receipt_seq IS NULL THEN NULL
                                 ELSE opd_format_number('RCT-', receipt_year, receipt_seq, 5) END) STORED,
    received_by_admin_id UUID NULL,          -- NULL = gateway webhook
    received_by_name    TEXT NULL,
    reason              TEXT NULL CHECK (char_length(reason) <= 300),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_receipts_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_receipts_number_key UNIQUE (clinic_id, receipt_year, receipt_seq),
    CONSTRAINT opd_receipts_reference_required CHECK (mode NOT IN ('upi', 'card') OR reference IS NOT NULL),
    CONSTRAINT opd_receipts_online_shape CHECK (
        (mode IN ('payment_link', 'prepaid_online')) = (gateway_payment_id IS NOT NULL AND shift_id IS NULL)),
    CONSTRAINT opd_receipts_counter_needs_shift CHECK (mode IN ('payment_link', 'prepaid_online') OR shift_id IS NOT NULL),
    CONSTRAINT opd_receipts_refund_manual CHECK (kind = 'payment' OR (mode IN ('cash', 'upi') AND reason IS NOT NULL)),
    CONSTRAINT opd_receipts_invoice_fk FOREIGN KEY (clinic_id, invoice_id) REFERENCES opd_invoices (clinic_id, id),
    CONSTRAINT opd_receipts_shift_fk   FOREIGN KEY (clinic_id, shift_id)   REFERENCES opd_cashier_shifts (clinic_id, id)
);
-- webhook idempotency: a gateway payment settles at most once
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_receipts_gateway_payment
    ON opd_receipts (clinic_id, gateway, gateway_payment_id) WHERE gateway_payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opd_receipts_invoice ON opd_receipts (clinic_id, invoice_id);
CREATE INDEX IF NOT EXISTS idx_opd_receipts_shift   ON opd_receipts (clinic_id, shift_id) WHERE shift_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opd_receipts_day     ON opd_receipts (clinic_id, created_at DESC);

-- ── 10. Integrity triggers (they fire for service_role; RLS does not) ──────

-- 10.1 Signed encounters/prescriptions are immutable. In BEFORE triggers
-- generated columns (bmi) are not yet computed in NEW, so they are excluded
-- from the comparison along with the columns each transition may change.
CREATE OR REPLACE FUNCTION opd_guard_signed_record() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_mutable TEXT[] := ARRAY['updated_at', 'bmi'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' AND NOT opd_purging(OLD.clinic_id) THEN
            RAISE EXCEPTION 'opd_record_locked:%:%', TG_TABLE_NAME, OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.patient_id <> OLD.patient_id
       OR NEW.doctor_id <> OLD.doctor_id OR NEW.appointment_id <> OLD.appointment_id
       OR NEW.version <> OLD.version OR NEW.supersedes_id IS DISTINCT FROM OLD.supersedes_id THEN
        RAISE EXCEPTION 'opd_identity_immutable:%:%', TG_TABLE_NAME, OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'superseded' THEN
            RAISE EXCEPTION 'opd_bad_transition:draft->superseded' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'signed' THEN
        IF TG_TABLE_NAME = 'opd_prescriptions' THEN
            v_mutable := v_mutable || ARRAY['delivery_status', 'whatsapp_message_id', 'last_sent_at', 'send_count'];
        END IF;
        IF NEW.status = 'superseded' THEN
            v_mutable := v_mutable || ARRAY['status', 'superseded_at'];
        ELSIF NEW.status <> 'signed' THEN
            RAISE EXCEPTION 'opd_bad_transition:signed->%', NEW.status USING ERRCODE = 'P0001';
        END IF;
        IF (to_jsonb(NEW) - v_mutable) = (to_jsonb(OLD) - v_mutable) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'opd_record_locked:%:%', TG_TABLE_NAME, OLD.id USING ERRCODE = 'P0001';
END $$;

-- 10.2 Prescription lines follow their parent's lock.
CREATE OR REPLACE FUNCTION opd_guard_rx_items() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_status TEXT; v_clinic UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
BEGIN
    IF opd_purging(v_clinic) THEN RETURN COALESCE(NEW, OLD); END IF;
    SELECT status INTO v_status FROM opd_prescriptions
     WHERE id = COALESCE(NEW.prescription_id, OLD.prescription_id) AND clinic_id = v_clinic;
    -- NULL status = parent already gone (draft delete cascading) → allow
    IF v_status IS NOT NULL AND v_status <> 'draft' THEN
        RAISE EXCEPTION 'opd_record_locked:opd_prescription_items' USING ERRCODE = 'P0001';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

-- 10.3 Invoice lines: draft-only; keep the parent subtotal in step.
CREATE OR REPLACE FUNCTION opd_guard_invoice_items() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_status TEXT; v_clinic UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
BEGIN
    IF opd_purging(v_clinic) THEN RETURN COALESCE(NEW, OLD); END IF;
    SELECT status INTO v_status FROM opd_invoices
     WHERE id = COALESCE(NEW.invoice_id, OLD.invoice_id) AND clinic_id = v_clinic;
    IF v_status IS NOT NULL AND v_status <> 'draft' THEN
        RAISE EXCEPTION 'opd_record_locked:opd_invoice_items' USING ERRCODE = 'P0001';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

CREATE OR REPLACE FUNCTION opd_recalc_invoice_subtotal() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_invoice UUID := COALESCE(NEW.invoice_id, OLD.invoice_id);
        v_clinic  UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
BEGIN
    UPDATE opd_invoices i
       SET subtotal_paise = (SELECT COALESCE(SUM(line_total_paise), 0) FROM opd_invoice_items
                              WHERE invoice_id = v_invoice AND clinic_id = v_clinic)
     WHERE i.id = v_invoice AND i.clinic_id = v_clinic AND i.status = 'draft';
    RETURN NULL;
END $$;

-- 10.4 Invoice header: numbering at issue, lock after issue, paid only via receipts.
-- total_paise / invoice_number are generated → not yet computed in NEW → excluded.
CREATE OR REPLACE FUNCTION opd_guard_invoice() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_items   INTEGER;
    v_total   INTEGER;
    v_mutable TEXT[] := ARRAY['updated_at', 'total_paise', 'invoice_number',
                              'payment_link_gateway', 'payment_link_id', 'payment_link_url',
                              'payment_link_amount_paise', 'payment_link_created_at'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' AND NOT opd_purging(OLD.clinic_id) THEN
            RAISE EXCEPTION 'opd_record_locked:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.patient_id <> OLD.patient_id
       OR NEW.appointment_id IS DISTINCT FROM OLD.appointment_id THEN
        RAISE EXCEPTION 'opd_identity_immutable:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    -- paid_paise moves only inside the receipt trigger, for this invoice
    IF NEW.paid_paise <> OLD.paid_paise
       AND current_setting('kriya.opd_receipt_invoice', true) IS DISTINCT FROM OLD.id::TEXT THEN
        RAISE EXCEPTION 'opd_paid_is_derived' USING ERRCODE = 'P0001';
    END IF;

    IF OLD.status = 'draft' THEN
        IF NEW.status IN ('issued', 'paid') THEN
            SELECT count(*), COALESCE(SUM(line_total_paise), 0) INTO v_items, v_total
              FROM opd_invoice_items WHERE invoice_id = OLD.id AND clinic_id = OLD.clinic_id;
            IF v_items = 0 THEN RAISE EXCEPTION 'opd_invoice_empty' USING ERRCODE = 'P0001'; END IF;
            IF v_total <> NEW.subtotal_paise THEN RAISE EXCEPTION 'opd_invoice_subtotal_mismatch' USING ERRCODE = 'P0001'; END IF;
            NEW.invoice_year := EXTRACT(YEAR FROM (now() AT TIME ZONE 'Asia/Kolkata'))::SMALLINT;
            NEW.invoice_seq  := opd_next_counter(OLD.clinic_id, 'inv:' || NEW.invoice_year);
            NEW.issued_at    := now();
            NEW.status       := CASE WHEN NEW.subtotal_paise - NEW.discount_paise = 0 THEN 'paid' ELSE 'issued' END;
        ELSIF NEW.status NOT IN ('draft', 'void') THEN
            RAISE EXCEPTION 'opd_bad_transition:draft->%', NEW.status USING ERRCODE = 'P0001';
        ELSIF NEW.invoice_seq IS NOT NULL OR NEW.invoice_year IS NOT NULL THEN
            RAISE EXCEPTION 'opd_number_is_assigned_at_issue' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'void' THEN
        RAISE EXCEPTION 'opd_record_locked:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
    END IF;

    -- issued / partially_paid / paid
    IF NEW.status = 'void' THEN
        IF OLD.paid_paise <> 0 THEN RAISE EXCEPTION 'opd_void_requires_refund_first' USING ERRCODE = 'P0001'; END IF;
        v_mutable := v_mutable || ARRAY['status', 'voided_at', 'voided_by', 'void_reason'];
    ELSIF NEW.status = 'draft' THEN
        RAISE EXCEPTION 'opd_bad_transition:%->draft', OLD.status USING ERRCODE = 'P0001';
    ELSE
        v_mutable := v_mutable || ARRAY['status', 'paid_paise'];
    END IF;
    IF (to_jsonb(NEW) - v_mutable) <> (to_jsonb(OLD) - v_mutable) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

-- 10.5 Receipts: number + shift check before insert; apply to invoice after;
-- never updated or deleted.
CREATE OR REPLACE FUNCTION opd_receipt_before_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_shift_status TEXT; v_cashier UUID;
BEGIN
    IF NEW.shift_id IS NOT NULL THEN
        -- FOR SHARE waits for a concurrent opd_close_shift and re-reads its result
        SELECT status, cashier_admin_id INTO v_shift_status, v_cashier
          FROM opd_cashier_shifts WHERE id = NEW.shift_id AND clinic_id = NEW.clinic_id FOR SHARE;
        IF v_shift_status IS DISTINCT FROM 'open' THEN
            RAISE EXCEPTION 'opd_shift_not_open' USING ERRCODE = 'P0001';
        END IF;
        IF NEW.received_by_admin_id IS DISTINCT FROM v_cashier THEN
            RAISE EXCEPTION 'opd_shift_not_yours' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    NEW.receipt_year := EXTRACT(YEAR FROM (now() AT TIME ZONE 'Asia/Kolkata'))::SMALLINT;
    NEW.receipt_seq  := opd_next_counter(NEW.clinic_id, 'rct:' || NEW.receipt_year);
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION opd_receipt_after_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_new_paid INTEGER; v_total INTEGER; v_status TEXT;
BEGIN
    SELECT status, subtotal_paise - discount_paise,
           paid_paise + CASE WHEN NEW.kind = 'payment' THEN NEW.amount_paise ELSE -NEW.amount_paise END
      INTO v_status, v_total, v_new_paid
      FROM opd_invoices WHERE id = NEW.invoice_id AND clinic_id = NEW.clinic_id FOR UPDATE;
    IF v_status IS NULL OR v_status NOT IN ('issued', 'partially_paid', 'paid') THEN
        RAISE EXCEPTION 'opd_invoice_not_payable:%', COALESCE(v_status, 'missing') USING ERRCODE = 'P0001';
    END IF;
    PERFORM set_config('kriya.opd_receipt_invoice', NEW.invoice_id::TEXT, true);
    -- over-payment / over-refund violate opd_invoices_paid_consistent and abort the receipt
    UPDATE opd_invoices
       SET paid_paise = v_new_paid,
           status = CASE WHEN v_new_paid = 0 THEN 'issued'
                         WHEN v_new_paid < v_total THEN 'partially_paid'
                         ELSE 'paid' END
     WHERE id = NEW.invoice_id AND clinic_id = NEW.clinic_id;
    PERFORM set_config('kriya.opd_receipt_invoice', '', true);
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION opd_guard_append_only() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND opd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'opd_append_only:%', TG_TABLE_NAME USING ERRCODE = 'P0001';
END $$;

-- 10.6 Closed shifts are immutable.
CREATE OR REPLACE FUNCTION opd_guard_shift() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF NOT opd_purging(OLD.clinic_id) THEN
            RAISE EXCEPTION 'opd_append_only:opd_cashier_shifts' USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'closed' THEN
        RAISE EXCEPTION 'opd_record_locked:opd_cashier_shifts:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.cashier_admin_id <> OLD.cashier_admin_id
       OR NEW.opening_float_paise <> OLD.opening_float_paise OR NEW.opened_at <> OLD.opened_at THEN
        RAISE EXCEPTION 'opd_identity_immutable:opd_cashier_shifts' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

-- Trigger wiring (DROP first: idempotent re-run). BEFORE triggers fire in name
-- order, so *_guard runs before *_touch on the same table.
DROP TRIGGER IF EXISTS trg_opd_encounters_guard    ON opd_encounters;
CREATE TRIGGER trg_opd_encounters_guard    BEFORE UPDATE OR DELETE ON opd_encounters
    FOR EACH ROW EXECUTE FUNCTION opd_guard_signed_record();
DROP TRIGGER IF EXISTS trg_opd_encounters_touch    ON opd_encounters;
CREATE TRIGGER trg_opd_encounters_touch    BEFORE UPDATE ON opd_encounters
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_opd_prescriptions_guard ON opd_prescriptions;
CREATE TRIGGER trg_opd_prescriptions_guard BEFORE UPDATE OR DELETE ON opd_prescriptions
    FOR EACH ROW EXECUTE FUNCTION opd_guard_signed_record();
DROP TRIGGER IF EXISTS trg_opd_prescriptions_touch ON opd_prescriptions;
CREATE TRIGGER trg_opd_prescriptions_touch BEFORE UPDATE ON opd_prescriptions
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_opd_rx_items_guard      ON opd_prescription_items;
CREATE TRIGGER trg_opd_rx_items_guard      BEFORE INSERT OR UPDATE OR DELETE ON opd_prescription_items
    FOR EACH ROW EXECUTE FUNCTION opd_guard_rx_items();
DROP TRIGGER IF EXISTS trg_opd_invoices_guard      ON opd_invoices;
CREATE TRIGGER trg_opd_invoices_guard      BEFORE UPDATE OR DELETE ON opd_invoices
    FOR EACH ROW EXECUTE FUNCTION opd_guard_invoice();
DROP TRIGGER IF EXISTS trg_opd_invoices_touch      ON opd_invoices;
CREATE TRIGGER trg_opd_invoices_touch      BEFORE UPDATE ON opd_invoices
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_opd_inv_items_guard     ON opd_invoice_items;
CREATE TRIGGER trg_opd_inv_items_guard     BEFORE INSERT OR UPDATE OR DELETE ON opd_invoice_items
    FOR EACH ROW EXECUTE FUNCTION opd_guard_invoice_items();
DROP TRIGGER IF EXISTS trg_opd_inv_items_recalc    ON opd_invoice_items;
CREATE TRIGGER trg_opd_inv_items_recalc    AFTER INSERT OR UPDATE OR DELETE ON opd_invoice_items
    FOR EACH ROW EXECUTE FUNCTION opd_recalc_invoice_subtotal();
DROP TRIGGER IF EXISTS trg_opd_receipts_before     ON opd_receipts;
CREATE TRIGGER trg_opd_receipts_before     BEFORE INSERT ON opd_receipts
    FOR EACH ROW EXECUTE FUNCTION opd_receipt_before_insert();
DROP TRIGGER IF EXISTS trg_opd_receipts_after      ON opd_receipts;
CREATE TRIGGER trg_opd_receipts_after      AFTER INSERT ON opd_receipts
    FOR EACH ROW EXECUTE FUNCTION opd_receipt_after_insert();
DROP TRIGGER IF EXISTS trg_opd_receipts_immutable  ON opd_receipts;
CREATE TRIGGER trg_opd_receipts_immutable  BEFORE UPDATE OR DELETE ON opd_receipts
    FOR EACH ROW EXECUTE FUNCTION opd_guard_append_only();
DROP TRIGGER IF EXISTS trg_opd_shifts_guard        ON opd_cashier_shifts;
CREATE TRIGGER trg_opd_shifts_guard        BEFORE UPDATE OR DELETE ON opd_cashier_shifts
    FOR EACH ROW EXECUTE FUNCTION opd_guard_shift();

-- ── 11. Atomic RPCs (every one takes p_clinic_id and filters on it) ────────

-- Sign (and, for an amendment, supersede the prior signed version) atomically.
-- Supersede runs first so uq_opd_encounters_one_signed never sees two rows.
CREATE OR REPLACE FUNCTION opd_sign_encounter(
    p_clinic_id UUID, p_encounter_id UUID, p_signer_admin_id UUID,
    p_signer_doctor_id UUID, p_signer_snapshot JSONB)
RETURNS SETOF opd_encounters LANGUAGE plpgsql AS $$
DECLARE e opd_encounters;
BEGIN
    SELECT * INTO e FROM opd_encounters WHERE id = p_encounter_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;
    IF e.status <> 'draft' THEN RAISE EXCEPTION 'opd_not_draft' USING ERRCODE = 'P0001'; END IF;
    IF e.doctor_id <> p_signer_doctor_id THEN RAISE EXCEPTION 'opd_not_treating_doctor' USING ERRCODE = 'P0001'; END IF;
    IF e.supersedes_id IS NOT NULL THEN
        UPDATE opd_encounters SET status = 'superseded', superseded_at = now()
         WHERE id = e.supersedes_id AND clinic_id = p_clinic_id AND status = 'signed';
        IF NOT FOUND THEN RAISE EXCEPTION 'opd_supersede_target_not_signed' USING ERRCODE = 'P0001'; END IF;
    END IF;
    RETURN QUERY
    UPDATE opd_encounters
       SET status = 'signed', signed_at = now(), signed_by_admin_id = p_signer_admin_id,
           signer_snapshot = p_signer_snapshot
     WHERE id = p_encounter_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;

CREATE OR REPLACE FUNCTION opd_sign_prescription(
    p_clinic_id UUID, p_rx_id UUID, p_signer_admin_id UUID, p_signer_doctor_id UUID,
    p_signer_snapshot JSONB, p_letterhead_snapshot JSONB, p_patient_snapshot JSONB, p_allergy_review JSONB)
RETURNS SETOF opd_prescriptions LANGUAGE plpgsql AS $$
DECLARE r opd_prescriptions; v_enc_status TEXT;
BEGIN
    SELECT * INTO r FROM opd_prescriptions WHERE id = p_rx_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;
    IF r.status <> 'draft' THEN RAISE EXCEPTION 'opd_not_draft' USING ERRCODE = 'P0001'; END IF;
    IF r.doctor_id <> p_signer_doctor_id THEN RAISE EXCEPTION 'opd_not_treating_doctor' USING ERRCODE = 'P0001'; END IF;
    SELECT status INTO v_enc_status FROM opd_encounters WHERE id = r.encounter_id AND clinic_id = p_clinic_id;
    IF v_enc_status IS NULL OR v_enc_status NOT IN ('signed', 'superseded') THEN
        RAISE EXCEPTION 'opd_encounter_not_signed' USING ERRCODE = 'P0001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM opd_prescription_items WHERE prescription_id = p_rx_id AND clinic_id = p_clinic_id) THEN
        RAISE EXCEPTION 'opd_prescription_empty' USING ERRCODE = 'P0001';
    END IF;
    IF r.supersedes_id IS NOT NULL THEN
        UPDATE opd_prescriptions SET status = 'superseded', superseded_at = now()
         WHERE id = r.supersedes_id AND clinic_id = p_clinic_id AND status = 'signed';
        IF NOT FOUND THEN RAISE EXCEPTION 'opd_supersede_target_not_signed' USING ERRCODE = 'P0001'; END IF;
    END IF;
    RETURN QUERY
    UPDATE opd_prescriptions
       SET status = 'signed', signed_at = now(), signed_by_admin_id = p_signer_admin_id,
           signer_snapshot = p_signer_snapshot, letterhead_snapshot = p_letterhead_snapshot,
           patient_snapshot = p_patient_snapshot, allergy_review = p_allergy_review
     WHERE id = p_rx_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;

-- Close a shift: expected = float + cash in − cash refunded, computed under the shift lock.
CREATE OR REPLACE FUNCTION opd_close_shift(
    p_clinic_id UUID, p_shift_id UUID, p_admin_id UUID, p_declared_paise INTEGER, p_notes TEXT)
RETURNS SETOF opd_cashier_shifts LANGUAGE plpgsql AS $$
DECLARE s opd_cashier_shifts; v_expected INTEGER;
BEGIN
    SELECT * INTO s FROM opd_cashier_shifts WHERE id = p_shift_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;
    IF s.status <> 'open' THEN RAISE EXCEPTION 'opd_shift_not_open' USING ERRCODE = 'P0001'; END IF;
    IF s.cashier_admin_id <> p_admin_id THEN RAISE EXCEPTION 'opd_shift_not_yours' USING ERRCODE = 'P0001'; END IF;
    SELECT s.opening_float_paise
         + COALESCE(SUM(CASE WHEN kind = 'payment' THEN amount_paise ELSE -amount_paise END), 0)
      INTO v_expected
      FROM opd_receipts WHERE clinic_id = p_clinic_id AND shift_id = p_shift_id AND mode = 'cash';
    RETURN QUERY
    UPDATE opd_cashier_shifts
       SET status = 'closed', closed_at = now(), expected_cash_paise = v_expected,
           declared_cash_paise = p_declared_paise, close_notes = p_notes
     WHERE id = p_shift_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;

-- Owner-only clinic deletion path: the ONLY way signed clinical/financial rows
-- can be removed. Self-referencing supersedes FKs are NO ACTION (checked at
-- statement end), so one DELETE per table removes a whole version chain.
CREATE OR REPLACE FUNCTION opd_purge_clinic(p_clinic_id UUID)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM opd_receipts           WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('receipts', n);
    DELETE FROM opd_invoice_items      WHERE clinic_id = p_clinic_id;
    DELETE FROM opd_invoices           WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('invoices', n);
    DELETE FROM opd_cashier_shifts     WHERE clinic_id = p_clinic_id;
    DELETE FROM opd_prescription_items WHERE clinic_id = p_clinic_id;
    DELETE FROM opd_prescriptions      WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('prescriptions', n);
    DELETE FROM opd_encounters         WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('encounters', n);
    PERFORM set_config('kriya.opd_purge_clinic', '', true);
    RETURN v;
END $$;

-- ── 12. RLS parity (049 pattern) + API-role lockout ────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['opd_encounters', 'opd_prescriptions', 'opd_prescription_items',
                             'opd_invoices', 'opd_invoice_items', 'opd_receipts', 'opd_cashier_shifts']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_all_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                           'service_role_all_' || t, t);
        END IF;
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'tenant_isolation_' || t, t);
        EXECUTE format($p$CREATE POLICY %I ON %I FOR ALL
                         USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
                         WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)$p$,
                       'tenant_isolation_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
        END IF;
    END LOOP;
END $$;

-- Supabase exposes public functions at /rest/v1/rpc to API roles by default.
DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY[
        'opd_next_counter(uuid,text)', 'opd_assign_mrn(uuid,uuid,uuid)',
        'opd_sign_encounter(uuid,uuid,uuid,uuid,jsonb)',
        'opd_sign_prescription(uuid,uuid,uuid,uuid,jsonb,jsonb,jsonb,jsonb)',
        'opd_close_shift(uuid,uuid,uuid,integer,text)', 'opd_purge_clinic(uuid)']
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', f);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', f);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', f);
        END IF;
    END LOOP;
END $$;

-- ── 13. Verify (fails the transaction if anything is missing) ──────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename IN (
          'opd_encounters', 'opd_prescriptions', 'opd_prescription_items', 'opd_invoices',
          'opd_invoice_items', 'opd_receipts', 'opd_cashier_shifts')) <> 7 THEN
        RAISE EXCEPTION '103 verify: OPD tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_indexes WHERE tablename = 'appointments'
          AND indexname IN ('uq_appointment_active_slot', 'uq_appointment_active_slot_unassigned')
          AND indexdef ILIKE '%is_walk_in = false%') <> 2 THEN
        RAISE EXCEPTION '103 verify: slot guards not rebuilt';
    END IF;
END $$;
```

Phase 1.1 must also confirm two things the SQL takes for granted, by real-Postgres test rather than inspection:
`opd_next_counter` touching `clinics` fires any existing `clinics` UPDATE triggers (e.g. `update_clinics_updated_at`
from 003) harmlessly, and `pg_indexes.indexdef` renders the predicate as `is_walk_in = false` (adjust the verify
pattern to the rendered text if not).

### 2.11 `app/tenancy.py`

```python
    # Migration 103: OPD OS. Every table carries clinic_id NOT NULL and
    # UNIQUE (clinic_id, id); children reference parents through composite
    # (clinic_id, x_id) FKs, so a cross-tenant reference fails in Postgres too.
    "opd_encounters", "opd_prescriptions", "opd_prescription_items",
    "opd_invoices", "opd_invoice_items", "opd_receipts", "opd_cashier_shifts",
```

### 2.12 `migrations/rollback/103_down.sql` (destructive — last resort)

Order: pre-flight → remap queue values → restore 064 slot guards → drop tables/functions → drop columns. The header
must state: **destroys OPD clinical and financial records; take `pg_dump -t 'opd_*'` first; the normal rollback is the
owner toggle + code revert (status.md §R).**

```sql
-- Rollback 103. DESTROYS OPD clinical + financial records. pg_dump -t 'opd_*' FIRST.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM appointments WHERE is_walk_in AND status IN ('confirmed','pending_payment','pending_review')
               AND appointment_date >= (now() AT TIME ZONE 'Asia/Kolkata')::date) THEN
        RAISE EXCEPTION '103_down: active walk-ins today/future — close the OPD day first';
    END IF;
END $$;
UPDATE appointments SET queue_status = CASE queue_status
        WHEN 'registered' THEN 'waiting' WHEN 'vitals_pending' THEN 'waiting'
        WHEN 'billing' THEN 'done' WHEN 'completed' THEN 'done' WHEN 'cancelled' THEN 'done'
        ELSE queue_status END
 WHERE queue_status IN ('registered','vitals_pending','billing','completed','cancelled');
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_queue_status_check;
ALTER TABLE appointments ADD CONSTRAINT appointments_queue_status_check
    CHECK (queue_status IN ('waiting', 'in_consultation', 'done'));
-- past walk-ins are retired so the 064 guards cannot collide on them
UPDATE appointments SET status = 'completed' WHERE is_walk_in AND status IN ('confirmed','pending_payment','pending_review');
DROP INDEX IF EXISTS uq_appointment_active_slot;
CREATE UNIQUE INDEX uq_appointment_active_slot ON appointments (clinic_id, doctor_id, appointment_date, appointment_time)
    WHERE status IN ('confirmed','pending_payment','pending_review') AND booking_type = 'consultation' AND doctor_id IS NOT NULL;
DROP INDEX IF EXISTS uq_appointment_active_slot_unassigned;
CREATE UNIQUE INDEX uq_appointment_active_slot_unassigned ON appointments (clinic_id, doctor_name, appointment_date, appointment_time)
    WHERE status IN ('confirmed','pending_payment','pending_review') AND booking_type = 'consultation' AND doctor_id IS NULL;
DROP INDEX IF EXISTS idx_appointments_opd_queue;
DROP TABLE IF EXISTS opd_receipts, opd_cashier_shifts, opd_invoice_items, opd_invoices,
                     opd_prescription_items, opd_prescriptions, opd_encounters CASCADE;
DROP FUNCTION IF EXISTS opd_purge_clinic(uuid), opd_close_shift(uuid,uuid,uuid,integer,text),
    opd_sign_prescription(uuid,uuid,uuid,uuid,jsonb,jsonb,jsonb,jsonb), opd_sign_encounter(uuid,uuid,uuid,uuid,jsonb),
    opd_guard_shift(), opd_guard_append_only(), opd_receipt_after_insert(), opd_receipt_before_insert(),
    opd_guard_invoice(), opd_recalc_invoice_subtotal(), opd_guard_invoice_items(), opd_guard_rx_items(),
    opd_guard_signed_record(), opd_purging(uuid), opd_touch_updated_at(), opd_assign_mrn(uuid,uuid,uuid),
    opd_format_number(text,integer,integer,integer), opd_next_counter(uuid,text);
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_family_member_fk,
                         DROP CONSTRAINT IF EXISTS appointments_opd_meta_check,
                         DROP COLUMN IF EXISTS queue_timeline, DROP COLUMN IF EXISTS checked_in_at,
                         DROP COLUMN IF EXISTS family_member_id, DROP COLUMN IF EXISTS visit_type,
                         DROP COLUMN IF EXISTS booking_channel, DROP COLUMN IF EXISTS is_walk_in;
ALTER TABLE family_members DROP CONSTRAINT IF EXISTS family_members_opd_demographics_check,
    DROP COLUMN IF EXISTS mrn, DROP COLUMN IF EXISTS date_of_birth, DROP COLUMN IF EXISTS age_years,
    DROP COLUMN IF EXISTS age_recorded_on, DROP COLUMN IF EXISTS gender, DROP COLUMN IF EXISTS allergies,
    DROP COLUMN IF EXISTS allergies_status;
ALTER TABLE patients DROP CONSTRAINT IF EXISTS patients_opd_demographics_check,
    DROP COLUMN IF EXISTS mrn, DROP COLUMN IF EXISTS date_of_birth, DROP COLUMN IF EXISTS age_years,
    DROP COLUMN IF EXISTS age_recorded_on, DROP COLUMN IF EXISTS gender, DROP COLUMN IF EXISTS address_line,
    DROP COLUMN IF EXISTS city, DROP COLUMN IF EXISTS pincode, DROP COLUMN IF EXISTS emergency_contact_name,
    DROP COLUMN IF EXISTS emergency_contact_phone, DROP COLUMN IF EXISTS emergency_contact_relation,
    DROP COLUMN IF EXISTS allergies, DROP COLUMN IF EXISTS allergies_status;
ALTER TABLE clinic_admins DROP CONSTRAINT IF EXISTS clinic_admins_doctor_fk,
    DROP CONSTRAINT IF EXISTS clinic_admins_doctor_needs_clinic, DROP COLUMN IF EXISTS doctor_id;
ALTER TABLE doctors DROP CONSTRAINT IF EXISTS doctors_registration_check,
    DROP COLUMN IF EXISTS registration_number, DROP COLUMN IF EXISTS registration_council;
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_opd_state_check, DROP CONSTRAINT IF EXISTS clinics_opd_json_check,
    DROP COLUMN IF EXISTS opd_display_token_hash, DROP COLUMN IF EXISTS opd_counters,
    DROP COLUMN IF EXISTS opd_settings, DROP COLUMN IF EXISTS opd_state;
DROP INDEX IF EXISTS uq_patients_clinic_id_id, uq_family_members_clinic_id_id, uq_appointments_clinic_id_id,
                     uq_doctors_clinic_id_id, uq_branches_clinic_id_id;
-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '103_opd_os_core.sql';
```

---

## Part 3 — Backend API

### 3.1 Common rules (every route in `app/routers/opd.py`)

```python
def _holds(user: AdminUser, *perms: str) -> bool:
    return user.role in ("clinic_admin", "super_admin") or any(p in (user.permissions or []) for p in perms)

async def _opd_scope(user: AdminUser, clinic_id: str, *perms: str, live: bool = True) -> tuple[str, dict]:
    if perms and not _holds(user, *perms):
        raise HTTPException(403, f"Missing permission: {' or '.join(perms)}")
    scope = enforce_clinic_access(user, clinic_id)      # never "default"; super_admin must pass ?clinic_id
    clinic = await get_clinic_by_id(scope)
    if not opd_enabled(clinic):
        raise HTTPException(403, "The OPD module is not enabled for this clinic.")
    if live and clinic.get("opd_state") != "READY":
        raise HTTPException(409, "OPD setup is not complete. Finish the setup wizard first.")
    return scope, clinic
```

- Route shape: `clinic_id: str = "default"`, `user: AdminUser = Depends(verify_credentials)`, then `scope, clinic = await _opd_scope(...)` — the `voice_admin.py` pattern. Corporate-viewer and phlebotomist confinement in `verify_credentials` already 403s those logins before any OPD route runs.
- **Every** table call: `await sb(supabase.table(T)….eq("clinic_id", scope)…)` or `scoped_query(T, scope)`; every RPC gets `p_clinic_id=scope`. Ids from the URL are always **paired** with `.eq("clinic_id", scope)`. A miss is **404** (never 403: don't confirm another tenant's id exists).
- Branch-pinned staff: list reads wrap with `restrict_to_branch(query, user.branch_id)`; writes call `enforce_branch_scope(user, row["branch_id"])`.
- Optimistic concurrency on editable drafts: body carries `expected_updated_at`; update is `.eq("updated_at", expected)`; empty result → **409** `{"error":"stale","current": row}`.
- DB errors mapped once in `_db_error(exc)`: `opd_record_locked*`/`opd_bad_transition*` → 409; `opd_not_treating_doctor` → 403; `opd_shift_not_open|opd_shift_not_yours` → 409; `opd_invoice_not_payable*`, CHECK `23514` on `opd_invoices_paid_consistent` → 422 "amount exceeds balance"; `23505` → 409; `P0002` → 404; anything else → 500 "Internal error" with a log line. **Never** echo `str(exc)` (Session 20 #10).
- Every mutation → `log_admin_action(user, action="OPD_<VERB>", resource_type, resource_id, details)`. Reads of clinical content → `OPD_CLINICAL_VIEW` (DPDP access trail). Phones in logs masked `+91XXXXXX7890`.
- Times: `datetime.now(IST)`; dates: `today_ist()`.

### 3.2 Setup wizard

| Method & path | Perm | `live` | Request | Response |
|---|---|---|---|---|
| `GET /admin/opd/setup` | any OPD | no | — | `OpdSetupOut` |
| `PUT /admin/opd/setup/settings` | `OPD_ADMIN` | no | `OpdSettingsPatch` | `OpdSetupOut`; moves `NOT_CONFIGURED→CONFIGURING` |
| `POST /admin/opd/setup/dry-run` | `OPD_ADMIN` | no | — | `DryRunOut` (no DB writes; stamps `opd_settings.dry_run_passed_at` if ok) |
| `POST /admin/opd/setup/go-live` | `OPD_ADMIN` | no | — | `OpdSetupOut`; 409 + failing items unless every blocking step done and dry run < 24 h old |
| `POST /admin/opd/display-token` | `OPD_ADMIN` | no | — | `{"url": "/public/queue-display#t=<token>"}` — plaintext **once**; stores `sha256` |

```python
class ChecklistItem(BaseModel):
    key: str; label: str; done: bool; blocking: bool
    detail: Optional[str] = None; fix_page: Optional[str] = None

class OpdSetupOut(BaseModel):
    state: Literal["NOT_CONFIGURED","CONFIGURING","READY","DEGRADED","DISABLED"]  # DEGRADED = READY + a blocking item now failing
    checklist: list[ChecklistItem]        # always 14, fixed order
    settings: dict
    went_live_at: Optional[datetime] = None

class DryRunOut(BaseModel):
    ok: bool
    checks: list[dict]                    # [{name, ok, detail}]

class TemplateNames(BaseModel):
    token_issued: Optional[str] = Field(None, max_length=64, pattern=r"^[a-z0-9_]+$")
    token_called: Optional[str] = Field(None, max_length=64, pattern=r"^[a-z0-9_]+$")
    prescription_ready: Optional[str] = Field(None, max_length=64, pattern=r"^[a-z0-9_]+$")
    receipt: Optional[str] = Field(None, max_length=64, pattern=r"^[a-z0-9_]+$")

class CatalogItem(BaseModel):
    code: str = Field(..., pattern=r"^[A-Z0-9_\-]{2,40}$")
    name: str = Field(..., min_length=1, max_length=200)
    item_type: Literal["nursing","diagnostic","procedure","other"]
    price_paise: int = Field(..., ge=0, le=100_000_000)
    active: bool = True

class OpdSettingsPatch(BaseModel):            # all optional; merged into opd_settings
    vitals_required: Optional[bool] = None
    after_checkin_stage: Optional[Literal["registered","vitals_pending","waiting"]] = None
    billing_after_consult: Optional[bool] = None   # True → call-next moves the finished patient to "billing"
    token_rule: Optional[Literal["per_doctor_daily"]] = None   # the only rule the existing index supports
    payment_modes: Optional[list[Literal["cash","upi","card","payment_link"]]] = Field(None, min_length=1)
    upi_vpa: Optional[str] = Field(None, pattern=r"^[\w.\-]{2,256}@[a-zA-Z]{2,64}$")
    auto_open_shift: Optional[bool] = None
    rooms: Optional[dict[str, str]] = None    # doctor_id -> "Room 3"; keys validated as doctors in scope
    templates: Optional[TemplateNames] = None
    service_catalog: Optional[list[CatalogItem]] = Field(None, max_length=300)   # codes unique (validator)
    confirmed_steps: Optional[list[str]] = None   # acknowledgements for review-only steps
```

Settings writes are read-merge-write guarded by `.eq("opd_settings", <value read>)` (CAS, 3 retries) so two admins can't clobber each other.

**14-step checklist** (`opd.setup_checklist`, computed from live data on every call — never cached):

| # | key | Blocking | Done when |
|---|---|---|---|
| 1 | `clinic_profile` | ✔ | name, address, clinic phone present |
| 2 | `operating_hours` | ✔ | every active doctor has a morning or evening session |
| 3 | `branches` | ✔ if `multi_branch` | ≥1 active branch, or single-site confirmed |
| 4 | `departments` | ✔ | ≥1 department among active doctors + confirmed |
| 5 | `doctor_roster` | ✔ | ≥1 active doctor |
| 6 | `doctor_registration` | ✔ | every active doctor has `registration_number` + `registration_council` |
| 7 | `consult_durations` | ✔ | `slot_duration_minutes` set (default 30 counts) |
| 8 | `consult_fees` | ✔ | `consultation_fee` set for each active doctor (0 allowed = free) |
| 9 | `front_desk_permissions` | ✔ | ≥1 active login holding `OPD_FRONT_DESK` (or a clinic_admin) |
| 10 | `doctor_logins` | ✔ | every active doctor has a linked login (`clinic_admins.doctor_id`) — else nobody can sign |
| 11 | `token_rules` | ✔ | confirmed (`per_doctor_daily`) + after-check-in stage chosen |
| 12 | `billing_setup` | ✔ | ≥1 payment mode; `upi_vpa` if `upi`; gateway configured (`payment.gateway_configured`) if `payment_link`; catalog reviewed |
| 13 | `whatsapp_templates` | ✘ | template names set (non-blocking: Meta approval is external) |
| 14 | `test_run_go_live` | ✔ | dry run passed < 24 h ago |

**Dry run** (`opd.dry_run`): per active doctor → today's sessions/holiday/leave resolution; next token (read-only `max+1`); fee resolution; render a sample Rx PDF and invoice PDF in memory (asserts `%PDF`, registration printed); payment config valid; clinic WhatsApp credentials present. Zero writes besides the timestamp, so no test rows pollute patient data.

### 3.3 Patient registry

| Method & path | Perm | Request | Response |
|---|---|---|---|
| `GET /admin/opd/patients/search?q=&limit=20` | FRONT_DESK \| CLINICAL \| BILLING | `q` ≥ 2 chars | `list[PatientHit]` |
| `POST /admin/opd/patients/duplicate-check` | FRONT_DESK | `DuplicateCheckIn {name, phone, date_of_birth?, age_years?}` | `list[PatientHit]` with `match_reason` |
| `POST /admin/opd/patients` | FRONT_DESK | `PatientRegisterIn` | `PatientOut` (201); 409 `{candidates}` on unacknowledged duplicates |
| `GET /admin/opd/patients/{patient_id}?family_member_id=` | FRONT_DESK \| CLINICAL \| BILLING | — | `PatientOut` + `visits[]`; `clinical_history[]` **only** for CLINICAL/admin |
| `PATCH /admin/opd/patients/{patient_id}?family_member_id=` | FRONT_DESK \| CLINICAL | `PatientPatchIn` (same fields as register, all optional) | `PatientOut`. Field-level last-write-wins (no `updated_at` on `patients`); every change audit-logged with before/after of the changed fields. |

```python
class PatientHit(BaseModel):
    patient_id: str; family_member_id: Optional[str]; mrn: Optional[str]
    name: str; phone: str; relationship: Optional[str]
    age_years: Optional[int]; gender: Optional[str]; last_visit_date: Optional[date]
    match_reason: Optional[Literal["same_phone","same_name_dob","same_name_age","legacy_record"]] = None

class PatientRegisterIn(BaseModel):
    phone: str = Field(..., pattern=r"^\+?[0-9]{10,15}$")      # normalised to E.164 server-side
    name: str = Field(..., min_length=1, max_length=100)
    is_account_holder: bool = True     # False → family_members row under `phone`
    relationship: Optional[str] = Field(None, max_length=40)
    date_of_birth: Optional[date] = None
    age_years: Optional[int] = Field(None, ge=0, le=130)
    gender: Optional[Literal["male","female","other","undisclosed"]] = None
    address_line: Optional[str] = Field(None, max_length=300)
    city: Optional[str] = Field(None, max_length=80)
    pincode: Optional[str] = Field(None, pattern=r"^[1-9][0-9]{5}$")
    emergency_contact_name: Optional[str] = Field(None, max_length=100)
    emergency_contact_phone: Optional[str] = Field(None, pattern=r"^\+?[0-9]{10,15}$")
    emergency_contact_relation: Optional[str] = Field(None, max_length=40)
    allergies_status: Literal["unknown","none_known","recorded"] = "unknown"
    allergies: list[constr(min_length=2, max_length=80)] = Field(default_factory=list, max_length=50)
    data_consent: Literal[True]          # DPDP: registration without consent is refused (422)
    whatsapp_opt_in: bool = False        # Meta policy: no WhatsApp to a walk-in without it
    acknowledged_duplicates: list[str] = Field(default_factory=list)
    # validators: DOB wins over age; DOB not in the future (IST); allergies non-empty iff status=recorded;
    # relationship required iff not is_account_holder
```

Rules: search is clinic-scoped on `patients` (`phone` exact or suffix ≥ 4 digits, `mrn` exact, name tokens via the existing `"*" + "*".join(tokens) + "*"` `ilike` idiom) **plus** `family_members` (name/MRN/primary_phone) **plus** `patient_records` (legacy import; phone match → prefill only, never auto-merged). Register: account holder → upsert `patients` on `(clinic_id, phone)` (existing row: fill **null** demographic fields only — never silently overwrite a WhatsApp-provided name or consent); dependant → insert `family_members`; then `rpc("opd_assign_mrn", …)`. Duplicates are recomputed server-side; any candidate not in `acknowledged_duplicates` → 409. Age stored with `age_recorded_on = today_ist()`; displayed age = DOB-derived, else `age_years` + full years since `age_recorded_on`.

### 3.4 Walk-ins, arrivals, live queue

| Method & path | Perm | Request | Response |
|---|---|---|---|
| `POST /admin/opd/walk-ins` | FRONT_DESK | `WalkInIn` | `QueueRow` (201) + async WhatsApp token message |
| `POST /admin/opd/appointments/{appointment_id}/arrive` | FRONT_DESK | `{visit_type?}` | `QueueRow` (409 if not today / not checkable) |
| `GET /admin/opd/queue?date=&doctor_id=&department=&branch_id=` | FRONT_DESK \| CLINICAL \| BILLING | `If-None-Match` | `QueueBoardOut` + `ETag`; **304** if unchanged |
| `POST /admin/opd/queue/{appointment_id}/stage` | FRONT_DESK (CLINICAL for own doctor's `in_consultation→*`) | `StageChangeIn` | `QueueRow`; 409 on stale `expected_from` |
| `POST /admin/opd/queue/call-next` | FRONT_DESK \| CLINICAL (own doctor) | `{doctor_id}` | `{called: QueueRow\|null, finished: QueueRow\|null}` |
| `POST /admin/opd/queue/{appointment_id}/recall` | FRONT_DESK \| CLINICAL | — | `{notified: bool}` (re-announce; no state change) |

```python
class WalkInIn(BaseModel):
    patient_id: str
    family_member_id: Optional[str] = None
    doctor_id: str
    branch_id: Optional[str] = None       # defaults to user.branch_id; must be a branch where the doctor sits
    visit_type: Literal["new","followup","review"] = "new"
    symptoms: Optional[str] = Field(None, max_length=500)   # reception note; never sent to any LLM
    skip_vitals: bool = False

class StageChangeIn(BaseModel):
    to_stage: Literal["registered","vitals_pending","waiting","in_consultation","billing","completed","cancelled"]
    expected_from: str
    reason: Optional[str] = Field(None, max_length=200)    # validator: required for cancelled

class QueueRow(BaseModel):
    appointment_id: str; token_number: int; stage: str; doctor_id: Optional[str]; doctor_name: str
    department: Optional[str]; branch_id: Optional[str]; patient_name: str; mrn: Optional[str]
    visit_type: Optional[str]; booking_channel: Optional[str]; is_walk_in: bool
    checked_in_at: Optional[datetime]; waiting_minutes: Optional[int]
    has_vitals: bool; invoice_status: Optional[str]; payment_status: Optional[str]

class QueueBoardOut(BaseModel):
    date: date
    doctors: list[dict]   # [{doctor_id, doctor_name, department, room, now_serving: QueueRow|None, rows: list[QueueRow], counts:{stage:n}}]
    generated_at: datetime
```

**Walk-in algorithm** (`opd.create_walk_in`) — order fixed by CLAUDE.md (holiday + leave **before** availability):
1. Doctor via `.eq("clinic_id", scope).eq("id", doctor_id)` → 404; inactive → 422.
2. `hospital_holidays` for today → 409 `clinic_closed`.
3. `doctor_leaves` for today (full day, or the current session for a half-day, via `database.doctor_session_slots`) → 409 `doctor_on_leave`.
4. Doctor sits at `branch_id` (`doctor_branches`) with a session covering now or later today → else 409 `doctor_not_in_opd_today`.
5. Patient/family member in scope (404) → `opd_assign_mrn`.
6. Insert appointment: `booking_type='consultation', is_walk_in=true, booking_channel='front_desk', status='confirmed', appointment_date=today_ist(), appointment_time=now IST (HH:MM), doctor_id, doctor_name, department, branch_id, patient_id, family_member_id, patient_phone, patient_name, visit_type, symptoms, booking_ref` (same generator `book_appointment` uses) and the existing pay-at-clinic `payment_status` value.
7. `check_in_appointment(scope, id, initial_queue_status=('waiting' if skip_vitals else after_checkin_stage))` → token via the existing unique index + retry.
8. Fire-and-forget WhatsApp `opd_token_issued` (only if `patients.opted_in`), `asyncio.create_task` inside the existing try/log wrapper; never blocks the response.

**Allowed transitions** (`opd.ALLOWED_TRANSITIONS`; enforced in Python **and** by the CAS `.eq("queue_status", expected_from)`):

```
registered      → vitals_pending | waiting | cancelled
vitals_pending  → waiting | cancelled
waiting         → in_consultation | vitals_pending | cancelled
in_consultation → billing | completed | waiting        (waiting = "stepped out for a test")
billing         → completed
completed, cancelled, done → (terminal)
```
Side effects: `→ in_consultation` creates the draft encounter (idempotent via `uq_opd_encounters_one_draft`) and stamps `started_at`; `→ billing` calls `ensure_visit_invoice`; `→ completed` sets `appointments.status='completed'`; `→ cancelled` on a **paid online** booking routes through `PaymentService.admin_cancel_confirmed_booking` (refund rules unchanged), else `status='cancelled'` with the reason. Every transition merges `{stage: iso_ts}` into `queue_timeline` in the **same** CAS update.

**Call next** (`opd.opd_call_next`, mirrors `database.call_next_patient`'s CAS loop, keyed on `doctor_name` to match `idx_unique_queue_token`):
1. Current `in_consultation` row for doctor/today → `billing` if `billing_after_consult` else `completed` (CAS on `queue_status='in_consultation'`).
2. Lowest-token `waiting` row → CAS claim to `in_consultation` (5 retries on a lost race).
3. Notify the called patient (`opd_token_called` template, or in-window text with the existing `queue_your_turn` copy).
4. Audit `QUEUE_CALL_NEXT`. A doctor-initiated call requires `user.doctor_id == doctor_id` unless the caller holds FRONT_DESK/admin.

**ETag**: `sha1` over `(appointment_id, queue_status, token_number, updated_at)` of the board rows; clients poll every 5 s with `If-None-Match` → mostly 304, no body. `ponytail:` polling; move to SSE when a clinic runs >10 boards.

### 3.5 Encounters & vitals

| Method & path | Perm | Request | Response |
|---|---|---|---|
| `GET /admin/opd/workspace?date=&doctor_id=` | CLINICAL | doctor logins: `doctor_id` forced to `user.doctor_id`; admins may pick | `{doctor, queue: list[QueueRow]}` |
| `GET /admin/opd/encounters/by-appointment/{appointment_id}` | CLINICAL \| FRONT_DESK (vitals only) | — | `EncounterOut` (+`history`, `allergies`); FRONT_DESK receives vitals fields only |
| `PUT /admin/opd/encounters/by-appointment/{appointment_id}/vitals` | FRONT_DESK \| CLINICAL | `VitalsIn` | `EncounterOut`; advances `vitals_pending→waiting` |
| `PUT /admin/opd/encounters/{encounter_id}` | CLINICAL (treating doctor only) | `EncounterNotesIn` | `EncounterOut`; 409 stale |
| `POST /admin/opd/encounters/{encounter_id}/sign` | CLINICAL (treating doctor only) | — | `EncounterOut` |
| `POST /admin/opd/encounters/{encounter_id}/amend` | CLINICAL (treating doctor only) | `{reason (5–300)}` | new draft `EncounterOut` (copy of signed content, `version+1`, `supersedes_id`) |

```python
class VitalsIn(BaseModel):
    bp_systolic: Optional[int] = Field(None, ge=50, le=300)
    bp_diastolic: Optional[int] = Field(None, ge=20, le=200)
    pulse_bpm: Optional[int] = Field(None, ge=20, le=250)
    temperature_c: Optional[Decimal] = Field(None, ge=30, le=45, decimal_places=1)  # UI converts °F
    spo2_pct: Optional[int] = Field(None, ge=50, le=100)
    weight_kg: Optional[Decimal] = Field(None, gt=0, le=400, decimal_places=2)
    height_cm: Optional[Decimal] = Field(None, ge=30, le=250, decimal_places=1)
    # validators: systolic > diastolic when both present; at least one field

class Diagnosis(BaseModel):
    system: Optional[Literal["ICD-10"]] = None
    code: Optional[str] = Field(None, pattern=r"^[A-TV-Z][0-9][0-9AB](\.[0-9A-TV-Z]{1,4})?$")
    text: str = Field(..., min_length=2, max_length=200)
    # validator: code requires system

class EncounterNotesIn(BaseModel):
    expected_updated_at: datetime
    chief_complaints: Optional[str] = Field(None, max_length=4000)
    clinical_findings: Optional[str] = Field(None, max_length=8000)
    examination_notes: Optional[str] = Field(None, max_length=8000)
    diagnoses: list[Diagnosis] = Field(default_factory=list, max_length=20)
    advice: Optional[str] = Field(None, max_length=4000)
    follow_up_date: Optional[date] = None      # validator: >= today_ist()
```

Sign rules (`opd_clinical.sign_encounter`): `user.doctor_id` set and equal to `encounter.doctor_id` (403 otherwise — **clinic_admin included**: NMC requires the treating clinician); doctor row has registration fields (422 `registration_missing`); `chief_complaints` or ≥1 diagnosis present (422). The signer snapshot is built server-side, then `rpc("opd_sign_encounter", …)`. History = signed/superseded encounters of the same patient/family member (last 20, newest first), each with its signed Rx summary. No ICD-10 lookup list in Phase 1 (`ponytail:` free text + optional code; bundle a code list when doctors ask for autocomplete).

### 3.6 e-Prescriptions

| Method & path | Perm | Request | Response |
|---|---|---|---|
| `GET /admin/opd/encounters/{encounter_id}/prescription` | CLINICAL \| FRONT_DESK (signed only) | — | `PrescriptionOut` (draft or signed) |
| `PUT /admin/opd/encounters/{encounter_id}/prescription` | CLINICAL (treating) | `PrescriptionDraftIn` | `PrescriptionOut` incl. recomputed `warnings` |
| `POST /admin/opd/prescriptions/{rx_id}/sign` | CLINICAL (treating) | `RxSignIn` | `PrescriptionOut` |
| `POST /admin/opd/prescriptions/{rx_id}/amend` | CLINICAL (treating) | `{reason (5–300)}` | new draft |
| `GET /admin/opd/prescriptions/{rx_id}/pdf` | CLINICAL \| FRONT_DESK | — | `application/pdf` (signed/superseded only; 409 for drafts) |
| `POST /admin/opd/prescriptions/{rx_id}/send-whatsapp` | CLINICAL \| FRONT_DESK | — | `{sent: bool, channel: "session"\|"template", reason?}` |

```python
class RxItemIn(BaseModel):
    drug_name: str = Field(..., min_length=2, max_length=200)
    formulation: Literal["tablet","capsule","syrup","suspension","injection","drops","ointment","cream",
                         "gel","inhaler","powder","lotion","spray","patch","other"]
    strength: Optional[str] = Field(None, max_length=60)
    dosage: str = Field(..., min_length=1, max_length=60)
    route: Literal["oral","topical","iv","im","sc","inhalation","nasal","ophthalmic","otic","rectal",
                   "vaginal","sublingual","other"] = "oral"
    frequency: str = Field(..., min_length=1, max_length=40)
    timing: Literal["before_food","after_food","with_food","empty_stomach","bedtime","any"] = "any"
    duration_days: Optional[int] = Field(None, ge=1, le=365)
    instructions: Optional[str] = Field(None, max_length=500)

class PrescriptionDraftIn(BaseModel):
    expected_updated_at: Optional[datetime] = None   # None only when creating
    general_instructions: Optional[str] = Field(None, max_length=2000)
    items: list[RxItemIn] = Field(..., max_length=40)

class AllergyAck(BaseModel):
    line_no: int; allergen: str
    override_reason: str = Field(..., min_length=5, max_length=300)

class RxSignIn(BaseModel):
    acknowledgements: list[AllergyAck] = Field(default_factory=list)
```

Draft save = delete-all + insert items (draft only; the trigger enforces it), then a header `updated_at` CAS bump. **Allergy review** (`allergy_warnings`), deterministic and zero-LLM: normalise drug names and allergies (lowercase, strip strength/punctuation); warn when an allergy string is a substring of a drug name, or the allergy names a class in `ALLERGY_CLASSES` that contains the drug; `allergies_status='unknown'` → one general warning ("allergy history not recorded") that also needs acknowledgement. Sign is refused (422 + warning list) unless **every** current warning is acknowledged — recomputed server-side at sign time, never trusted from the client. Snapshots at sign: signer, letterhead (clinic name/address/phone/branch; no logo in Phase 1), patient (name, MRN, age, gender, allergies).

**PDF** (`render_prescription_pdf`, A5): letterhead · patient block (name, MRN, age/sex, date, token) · vitals line · diagnoses · ℞ table (drug + formulation + strength, dosage, frequency, timing, duration, instructions) · general instructions · follow-up date · signature block: doctor name, qualifications, **Reg. No. & council** (from the snapshot), "Digitally signed on <IST>" · footer "Kriya Rx <short id> v<version>". Drafts are never rendered.

**WhatsApp dispatch** (`send_prescription`): 409 `no_whatsapp_consent` unless the account holder `opted_in`; PDF bytes → `whatsapp_service.upload_media` → in the 24 h window `send_document(…, _source="opd")`, else the `opd_settings.templates.prescription_ready` template with a document header (same shape as the `lab_reports.py` header build); no template + closed window → `{sent:false, reason:"outside_24h_no_template"}` (200; the UI offers Print). Updates `delivery_status/whatsapp_message_id/last_sent_at/send_count` (the only post-sign columns the trigger allows). Limit 5 sends per Rx per day.

### 3.7 Invoicing & cashier

| Method & path | Perm | Request | Response |
|---|---|---|---|
| `GET /admin/opd/catalog` | BILLING \| ADMIN | — | consultation fees by doctor + `service_catalog` + active `lab_tests` |
| `POST /admin/opd/appointments/{appointment_id}/invoice` | BILLING \| FRONT_DESK | — | `InvoiceOut` (create-or-get draft; adds the consultation line) |
| `POST /admin/opd/invoices` | BILLING | `InvoiceCreateIn {patient_id, family_member_id?, items}` (ad-hoc, no appointment) | `InvoiceOut` |
| `PUT /admin/opd/invoices/{invoice_id}` | BILLING | `InvoiceDraftIn` | `InvoiceOut`; 409 stale |
| `POST /admin/opd/invoices/{invoice_id}/issue` | BILLING | — | `InvoiceOut` (number assigned by the trigger) |
| `POST /admin/opd/invoices/{invoice_id}/receipts` | BILLING | `ReceiptIn` | `ReceiptOut` (201) |
| `POST /admin/opd/invoices/{invoice_id}/payment-link` | BILLING | — | `{url, gateway, amount_paise}`; sent over WhatsApp if opted in |
| `POST /admin/opd/invoices/{invoice_id}/refunds` | ADMIN | `RefundIn` | `ReceiptOut` |
| `POST /admin/opd/invoices/{invoice_id}/void` | ADMIN | `{reason (5–300)}` | `InvoiceOut`; 409 if paid > 0 |
| `GET /admin/opd/invoices?date=&status=&q=` | BILLING \| ADMIN | — | list |
| `GET /admin/opd/invoices/{invoice_id}` | BILLING \| FRONT_DESK | — | `InvoiceOut` + receipts |
| `GET /admin/opd/invoices/{invoice_id}/pdf` | BILLING \| FRONT_DESK | — | `application/pdf` (issued+ only) |
| `POST /admin/opd/shifts/open` | BILLING | `{opening_float_paise, branch_id?}` | `ShiftOut`; 409 if one is open |
| `GET /admin/opd/shifts/current` | BILLING | — | `ShiftOut \| null` + live totals by mode |
| `POST /admin/opd/shifts/{shift_id}/close` | BILLING (own shift) | `{declared_cash_paise, notes?}` | `ShiftOut` via `rpc("opd_close_shift")` |
| `GET /admin/opd/shifts?date=` | ADMIN | — | every cashier's shifts + variance (drawer reconciliation) |
| `GET /admin/opd/collections/summary?date=&branch_id=` | BILLING \| ADMIN | — | totals by mode × cashier, refunds, outstanding dues, online vs counter |

```python
class InvoiceItemIn(BaseModel):
    item_type: Literal["consultation","nursing","diagnostic","procedure","other"]
    catalog_code: Optional[str] = Field(None, max_length=40)
    doctor_id: Optional[str] = None
    lab_test_id: Optional[str] = None
    description: str = Field(..., min_length=1, max_length=200)
    quantity: int = Field(1, ge=1, le=999)
    unit_price_paise: int = Field(..., ge=0, le=100_000_000)
    discount_paise: int = Field(0, ge=0)

class InvoiceDraftIn(BaseModel):
    expected_updated_at: datetime
    items: list[InvoiceItemIn] = Field(..., min_length=1, max_length=100)
    discount_paise: int = Field(0, ge=0)
    discount_reason: Optional[str] = Field(None, max_length=200)   # validator: required when discount_paise > 0
    notes: Optional[str] = Field(None, max_length=1000)

class ReceiptIn(BaseModel):
    mode: Literal["cash","upi","card"]          # payment_link / prepaid_online are system-created only
    amount_paise: int = Field(..., ge=1, le=100_000_000)
    reference: Optional[str] = Field(None, max_length=100)    # required for upi/card (DB CHECK too)
    idempotency_key: str = Field(..., min_length=8, max_length=64)

class RefundIn(BaseModel):
    mode: Literal["cash","upi"]
    amount_paise: int = Field(..., ge=1)
    reason: str = Field(..., min_length=5, max_length=300)
    reference: Optional[str] = Field(None, max_length=100)
```

Server-side pricing: a `consultation` line is priced from `doctors.consultation_fee` × 100 (client price ignored unless the caller holds `OPD_ADMIN`); catalog items from `service_catalog`; `diagnostic` from scoped `lab_tests`; free-text `other` needs BILLING and is audit-logged. **Prepaid online bookings**: on issue, if the appointment has `payment_status='paid'` (WhatsApp/voice prepayment), insert a `prepaid_online` receipt for the captured amount with `gateway_payment_id` from the booking (unique index → idempotent), so the patient is never charged twice and the drawer never counts it. Counter receipts need the caller's open shift (auto-opened with ₹0 float when `auto_open_shift`), `received_by_admin_id = user.user_id`. `idempotency_key`: a repeated key within 10 minutes returns the first receipt instead of inserting (in-process TTL map + audit-log lookup on miss). Payment-link settlement (`settle_payment_link`) is webhook-only: `shift_id NULL`, `received_by_name='online'`, amount = gateway-captured amount, idempotent on `(clinic_id, gateway, gateway_payment_id)`; amount mismatch, void invoice or clinic mismatch → admin notification, never an exception into the webhook (webhooks must return 200).

**Thermal receipt**: no server endpoint. The page builds 80 mm HTML from `GET /invoices/{id}` JSON into a hidden `<iframe srcdoc>` and calls `print()` (`@page { size: 80mm auto; margin: 3mm }`, monospace 12px, no images). **PDF receipt** = `/invoices/{id}/pdf` (A4, every receipt listed).

### 3.8 Operational analytics

`GET /admin/opd/analytics?from=&to=&branch_id=` — `OPD_ADMIN`; range ≤ 92 days (422 otherwise). Computed in Python over scoped rows (`appointments` with `token_number` or `is_walk_in`, `opd_invoices`, `opd_receipts`, `opd_prescriptions`), paged at 1 000 rows, hard cap 50 000 → 422 "narrow the range". `ponytail:` Python aggregation; move to a SQL RPC when a clinic passes ~1 500 visits/day.

```python
class OpdAnalyticsOut(BaseModel):
    footfall: dict          # total, walk_in, booked, by_channel{whatsapp,voice,front_desk,web,unknown}, by_visit_type, by_doctor, by_department, by_day
    waits: dict             # p50/p90 minutes checked_in_at→in_consultation; consult p50/p90 in_consultation→billing|completed
    no_shows: int           # pre-booked, confirmed, past date, never checked in
    cancellations_in_queue: int
    collections: dict       # gross by mode, refunds, net, online_vs_counter, outstanding_paise, invoices_by_status
    prescriptions: dict     # signed, sent_whatsapp, send_failed, amended
    peak_hours: list[dict]  # check-ins per hour (IST)
```

### 3.9 Platform owner

| Method & path | Auth | Notes |
|---|---|---|
| `PATCH /platform/clinics/{clinic_id}/features` | `verify_owner_credentials` | existing route; OPD behaviour in §1.3 |
| `GET /platform/clinics/{clinic_id}/opd/deactivation-preview` | owner | `{active_queue, todays_open_visits, unpaid_invoices:{count, amount_paise}, draft_encounters, draft_prescriptions, open_shifts, retained:{encounters, prescriptions, invoices, receipts}, blocking: bool}` — read-only; every query `.eq("clinic_id", clinic_id)` |

`provision_defaults(clinic_id)`: `opd_settings = DEFAULT_SETTINGS ∪ existing` (existing keys win — never overwrite), with the same CAS-guarded read-merge-write as §3.2. `DEFAULT_SETTINGS = {"vitals_required": true, "after_checkin_stage": "vitals_pending", "billing_after_consult": true, "token_rule": "per_doctor_daily", "payment_modes": ["cash","upi"], "auto_open_shift": true, "rooms": {}, "templates": {}, "service_catalog": [], "confirmed_steps": []}`. Disabling **never** deletes or mutates patient, clinical or financial rows; it flips `features.opd_enabled=false` + `opd_state='DISABLED'`. A disabled clinic's bots fall back to the legacy queue answer automatically (the `get_patient_queue_status` delegation checks `opd_enabled`). Workers may serve a stale flag for up to 30 s (tenant cache, KA-19); every OPD route re-checks per request, so that is the full exposure.

### 3.10 Public hallway display

| Method & path | Auth | Notes |
|---|---|---|
| `GET /public/queue-display` | none | `FileResponse("admin/queue-display.html")`, `Cache-Control: no-store`, `X-Robots-Tag: noindex` |
| `GET /public/queue-display/data?branch_id=` | header `X-Display-Token` | `sha256(token)` → `clinics.opd_display_token_hash` (`clinics` is not tenant-owned; annotate `# unscoped: global_auth_lookup`); then `opd_enabled` + READY; `branch_id` must belong to that clinic (else 404). Rate limit via the existing limiter in `app/utils/security.py` (`is_rate_limited`), key `opd_display:<ip>:<hash[:12]>`, 30 req/min. Response `{clinic_name, generated_at, doctors:[{doctor_name, department, room, now_serving, next:[token,…≤5]}]}` — **no patient names, phones or MRNs** (D5). The token travels in the URL **fragment**, so it never reaches server or proxy logs. |

### 3.11 Channel integration (Phase 1.5)

- **One booking engine**: WhatsApp (`conversation.py`) and voice (`voice/tools.py:_create`) already end in `database.book_appointment` / `PaymentService.create_booking_with_payment`; they only gain `booking_channel`. Walk-ins use the OPD service, which writes the **same** `appointments` row shape.
- **One queue answer**: `get_patient_queue_status` delegates to `opd.queue_position` for OPD clinics — WhatsApp "queue status" and the voice `queue_status` tool return exactly what the board shows (a test asserts equality).
- **Firewall**: no LLM path reads or writes `opd_*` tables; no OPD module imports an AI module (AST test). `screen_message()` stays the first step of every inbound message; OPD adds no new inbound intent in Phase 1. Bots never quote clinical notes, diagnoses or medicines.
- **Templates**: `opd_token_issued` (token, doctor, clinic), `opd_token_called` (token, doctor, room), `opd_prescription_ready` (document header, doctor, date), `opd_receipt` (document header, invoice no., amount) — utility category, per-clinic WABA, names in `opd_settings.templates`. Sends go through `whatsapp_service`, so `outbound_message_ledger` accounting and consent checks are unchanged.

---

## Part 4 — Frontend

### 4.1 `admin/platform.html` — OPD card in the clinic modal

Insert after the "AI Voice Receptionist" card (≈ line 1961), same inline-style idiom as its neighbours.

```html
<!-- Migration 103: OPD OS. Off until the owner enables it here. -->
<div id="modalOpdCard" style="background:var(--surface2); padding:20px; border-radius:var(--radius-sm); border:1px solid var(--border); margin-top:16px;">
  <h4 style="color:#fff; font-size:0.95rem; margin-bottom:4px;">OPD OS</h4>
  <p style="color:var(--text3); font-size:0.78rem; margin-bottom:12px;">Front desk, live token queue, consultation notes, e-prescriptions and OPD billing. Turning it off keeps every record.</p>
  <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; color:var(--text2); cursor:pointer;">
    <input type="checkbox" id="modalOpdToggle" onchange="toggleOpd(this)"> OPD OS enabled
  </label>
  <div id="modalOpdState" style="font-size:0.8rem; color:var(--text3); margin-top:10px;"></div>
</div>

<!-- Deactivation dialog (reuses the deletion wizard's overlay classes) -->
<div id="opdDeactivateDialog" class="modal-overlay" style="display:none" role="dialog" aria-modal="true" aria-labelledby="opdDeactTitle">
  <div class="modal" style="max-width:520px;">
    <h3 id="opdDeactTitle">Turn off OPD OS for <span id="opdDeactClinic"></span>?</h3>
    <ul id="opdDeactBlockers"></ul>     <!-- active queue, today's open visits, unpaid invoices, drafts, open shifts -->
    <p id="opdDeactRetained" style="color:var(--text3)"></p>   <!-- "Kept: N encounters, N prescriptions, N invoices…" -->
    <label><input type="checkbox" id="opdDeactConfirm"> I understand staff lose the OPD screens now; no record is deleted.</label>
    <div style="display:flex; gap:8px; justify-content:flex-end;">
      <button class="btn-secondary" onclick="closeOpdDeactivate(false)">Keep enabled</button>
      <button class="btn-danger" id="opdDeactGo" disabled onclick="closeOpdDeactivate(true)">Turn off</button>
    </div>
  </div>
</div>
```

```js
let opdDeact = { clinicId: null, checkbox: null };
async function toggleOpd(cb) {
  if (cb.checked) return toggleClinicFeature('opd_enabled', true);          // existing helper; refreshes modal
  const p = await (await ownerFetch(`/platform/clinics/${currentClinicId}/opd/deactivation-preview`)).json();
  if (!p.blocking && !p.retained.encounters && !p.retained.invoices) return toggleClinicFeature('opd_enabled', false);
  opdDeact = { clinicId: currentClinicId, checkbox: cb };
  renderOpdDeactivationDialog(p);       // textContent only; #opdDeactConfirm enables #opdDeactGo
}
function closeOpdDeactivate(go) {
  const d = opdDeact;
  document.getElementById('opdDeactivateDialog').style.display = 'none';
  if (!go) { d.checkbox.checked = true; return; }
  patchClinicFeature(d.clinicId, { feature: 'opd_enabled', enabled: false, confirm_disable: true });
}
// On modal load: modalOpdToggle.checked = overrides.opd_enabled === true;
// modalOpdState.textContent = `State: ${clinic.opd_state}` (+ went-live date); card hidden when account_type !== 'tenant'.
// 400 from the eligibility guard → toast with the server detail, checkbox reverted.
```

(`ownerFetch` / `patchClinicFeature` stand for the panel's existing Basic-auth fetch helpers — use their real names; `toggleClinicFeature` is already at ≈ line 3060.) Directory table: an `OPD` pill (`READY` green, `CONFIGURING` amber, `DISABLED` grey) from `features.opd_enabled` + `opd_state`.

### 4.2 `admin/index.html` — design tokens

Inside the existing `:root` (dark) and `:root[data-theme="light"]` blocks; reuse existing tokens wherever one fits.

```css
:root {
  --opd-registered: #94A3B8;  --opd-vitals: #F59E0B;  --opd-waiting: #3D8BFD;
  --opd-consult: #10B981;     --opd-billing: #A78BFA; --opd-done: #64748B;  --opd-cancel: #EF4444;
  --opd-warn-bg: rgba(245,158,11,0.14); --opd-danger-bg: rgba(239,68,68,0.14);
  --opd-token-font: 700 1.6rem/1 ui-monospace, "SFMono-Regular", Consolas, monospace;
}
:root[data-theme="light"] { --opd-waiting: #2563EB; --opd-consult: #059669; --opd-done: #475569; }
.opd-pill { display:inline-flex; align-items:center; gap:6px; padding:2px 10px; border-radius:999px; font-size:.75rem; font-weight:600; }
.opd-pill[data-stage="registered"]      { background:color-mix(in srgb, var(--opd-registered) 18%, transparent); color:var(--opd-registered); }
.opd-pill[data-stage="vitals_pending"]  { background:color-mix(in srgb, var(--opd-vitals) 18%, transparent);     color:var(--opd-vitals); }
.opd-pill[data-stage="waiting"]         { background:color-mix(in srgb, var(--opd-waiting) 18%, transparent);    color:var(--opd-waiting); }
.opd-pill[data-stage="in_consultation"] { background:color-mix(in srgb, var(--opd-consult) 18%, transparent);    color:var(--opd-consult); }
.opd-pill[data-stage="billing"]         { background:color-mix(in srgb, var(--opd-billing) 18%, transparent);    color:var(--opd-billing); }
.opd-pill[data-stage="completed"]       { background:color-mix(in srgb, var(--opd-done) 18%, transparent);       color:var(--opd-done); }
.opd-pill[data-stage="cancelled"]       { background:color-mix(in srgb, var(--opd-cancel) 18%, transparent);     color:var(--opd-cancel); }
.opd-token { font: var(--opd-token-font); color: var(--text-strong); min-width: 3ch; text-align: right; }
.opd-grid  { display:grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap:16px; }
.opd-split { display:grid; grid-template-columns: 320px 1fr; gap:16px; }
.opd-warn  { background: var(--opd-warn-bg); border-radius: var(--radius-sm); padding: 10px 12px; }
.opd-locked input, .opd-locked textarea, .opd-locked select { pointer-events:none; opacity:.75; }
@media (max-width: 900px) { .opd-split { grid-template-columns: 1fr; } }
```

### 4.3 Navigation & gating

```html
<div class="nav-section-label" data-opd="nav" style="display:none">OPD</div>
<div class="nav-link" tabindex="0" data-page="opdsetup"     data-opd="admin"                  style="display:none" onclick="go('opdsetup',this)">Setup</div>
<div class="nav-link" tabindex="0" data-page="opddesk"      data-opd="front"                  style="display:none" onclick="go('opddesk',this)">Front Desk</div>
<div class="nav-link" tabindex="0" data-page="opdqueue"     data-opd="front clinical billing" style="display:none" onclick="go('opdqueue',this)">Live Queue</div>
<div class="nav-link" tabindex="0" data-page="opdworkspace" data-opd="clinical"               style="display:none" onclick="go('opdworkspace',this)">Consultation</div>
<div class="nav-link" tabindex="0" data-page="opdbilling"   data-opd="billing"                style="display:none" onclick="go('opdbilling',this)">Billing</div>
<div class="nav-link" tabindex="0" data-page="opdanalytics" data-opd="admin"                  style="display:none" onclick="go('opdanalytics',this)">OPD Analytics</div>
```

```js
let myOpd = false, myOpdState = null, myDoctorId = null;
const OPD_CAP = { admin: ['OPD_ADMIN'], front: ['OPD_FRONT_DESK'], clinical: ['OPD_CLINICAL'], billing: ['OPD_BILLING'] };
function opdCan(cap) {
  if (!myOpd) return false;
  if (['clinic_admin', 'super_admin'].includes(currentUser.role)) return true;
  return (OPD_CAP[cap] || []).some(p => (currentUser.permissions || []).includes(p));
}
function applyOpdGates() {
  const live = myOpdState === 'READY' || myOpdState === 'DEGRADED';
  document.querySelectorAll('[data-opd]').forEach(el => {
    const caps = el.dataset.opd.split(' ');
    const show = (caps.includes('nav') || caps.includes('perm')) ? myOpd
               : caps.some(opdCan) && (live || el.dataset.page === 'opdsetup');
    el.style.display = show ? '' : 'none';
  });
  renderOpdBanner();   // DEGRADED / not-yet-live banner at the top of every OPD page
}
// Called from the existing /admin/me loaders (≈ lines 4963, 5122) after the voice/home-collection flags.
```

The server is the authority — hiding a tab protects nothing (corporate-viewer precedent). Every OPD page also handles 403/409 from its first API call by rendering an inline "not available" panel.

### 4.4 Shared OPD client state & helpers

```js
const opd = {
  setup: { data: null },
  desk:  { q: '', seq: 0, hits: [], selected: null, dupes: [] },
  queue: { etag: null, board: null, filters: { doctor_id: '', department: '', branch_id: '' }, timer: null },
  ws:    { doctorId: null, appt: null, enc: null, rx: null, warnings: [], dirty: false, saveTimer: null },
  bill:  { invoice: null, shift: null, idem: null },
};
const opdApi = (path, opts = {}) => api(`/admin/opd${path}`, opts);   // existing api(): auth, 401, clinic_id for super_admin
function opdStartPolling(fn, ms = 5000) {
  opdStopPolling();
  const tick = () => { if (!document.hidden) fn(); };
  tick(); opd.queue.timer = setInterval(tick, ms);
}
function opdStopPolling() { clearInterval(opd.queue.timer); opd.queue.timer = null; }
// go(page) hook: leaving opdqueue/opdworkspace → opdStopPolling(); unsaved notes → confirm().
// Every dynamic string goes through the panel's existing escape helper or textContent — never raw innerHTML.
```

### 4.5 Setup wizard (`#page-opdsetup`)

```html
<section id="page-opdsetup" class="page" hidden>
  <header class="page-head"><h1>OPD Setup</h1><span id="opdSetupState" class="opd-pill"></span></header>
  <div class="opd-split">
    <ol id="opdSetupSteps" aria-label="Setup steps"></ol>   <!-- 14 items: ✓ / ! / ○ + label -->
    <div class="form-card">
      <div id="opdSetupDetail"></div>          <!-- selected step: status, what's missing, "Fix" → go(fix_page) -->
      <div id="opdSetupSettingsForm"></div>    <!-- steps 11–13: after-check-in stage, vitals, billing-after-consult, payment modes, UPI VPA, templates, catalog editor -->
      <footer>
        <button id="opdDisplayLinkBtn" class="btn-secondary" onclick="opdDisplayLink()">TV display link</button>
        <button id="opdDryRunBtn" class="btn-secondary" onclick="opdDryRun()">Run test visit</button>
        <button id="opdGoLiveBtn" class="btn-primary" disabled onclick="opdGoLive()">Go live</button>
      </footer>
    </div>
  </div>
</section>
```
Render: `GET /setup` → steps (blocking-and-not-done first, amber). `Go live` enabled only when every blocking item is done (the server re-checks). Catalog editor = table with add/remove rows validated with the `CatalogItem` patterns. "TV display link" → `POST /display-token` → copy to clipboard, with a warning that it replaces the previous screen URL.

### 4.6 Front desk walk-in desk (`#page-opddesk`)

```html
<section id="page-opddesk" class="page" hidden>
  <div class="opd-split">
    <div class="form-card">
      <label for="opdSearch">Find patient (phone, name or MRN)</label>
      <input id="opdSearch" type="search" autocomplete="off" inputmode="search">
      <ul id="opdSearchHits" role="listbox"></ul>      <!-- account holder + family rows: MRN, age/sex, last visit -->
      <button class="btn-secondary" onclick="opdNewPatient()">New patient</button>
    </div>
    <div class="form-card">
      <form id="opdRegisterForm" hidden><!-- demographics, consent (required), WhatsApp opt-in, allergy chips --></form>
      <div id="opdDupeWarning" class="opd-warn" hidden></div>   <!-- candidates: "Use this record" / "Different person" -->
      <form id="opdWalkInForm" hidden>
        <select id="opdWalkDoctor" required></select>            <!-- doctors in OPD today at this branch -->
        <select id="opdWalkVisitType"><option value="new">New</option><option value="followup">Follow-up</option><option value="review">Review</option></select>
        <textarea id="opdWalkSymptoms" maxlength="500"></textarea>
        <label><input type="checkbox" id="opdWalkSkipVitals"> Skip vitals</label>
        <button class="btn-primary" type="submit">Issue token</button>
      </form>
      <div id="opdTokenSlip" hidden></div>                      <!-- big token + Print slip (80 mm) -->
      <h3>Today's bookings not yet arrived</h3>
      <table id="opdArrivals"></table>                          <!-- WhatsApp/voice bookings → "Mark arrived" -->
    </div>
  </div>
</section>
```
Logic: search debounced 250 ms; `opd.desk.seq` drops out-of-order responses. Register → `POST /patients`; a 409 shows the candidates, the clerk picks one or confirms "different person" (ids go into `acknowledged_duplicates`, resubmit). Walk-in → `POST /walk-ins` → token slip; the submit button stays disabled while in flight (no double tokens). Keyboard: `/` focuses search, ↑/↓/Enter pick.

### 4.7 Live queue board (`#page-opdqueue`)

```html
<section id="page-opdqueue" class="page" hidden>
  <header class="page-head">
    <h1>Live Queue</h1>
    <select id="opdQDoctor"></select><select id="opdQDept"></select><select id="opdQBranch"></select>
    <span id="opdQUpdated" aria-live="polite"></span>
  </header>
  <div id="opdQBoard" class="opd-grid"></div>
</section>
<template id="opdQCard">
  <article class="form-card opd-qcard">
    <header><h2 class="doc"></h2><span class="room"></span><button class="btn-primary call-next">Call next</button></header>
    <div class="now"><span class="opd-token"></span><span class="who"></span></div>
    <ol class="rows"></ol>   <!-- token · name · stage pill · wait min · channel icon · actions (Vitals / Skip / Cancel) -->
  </article>
</template>
```
Logic: `opdStartPolling(loadQueue)` with `If-None-Match`; on 304 only "updated Xs ago" changes. Render by keyed diff on `appointment_id` (no full re-render, no focus loss). Stage actions post `{to_stage, expected_from}`; a 409 triggers an immediate reload and the toast "Someone else just moved this patient". `Call next` is disabled for 1.5 s after a click. Branch-pinned staff get a locked branch selector (existing behaviour).

### 4.8 Doctor workspace + e-Prescription studio (`#page-opdworkspace`)

```html
<section id="page-opdworkspace" class="page" hidden>
  <div class="opd-split">
    <aside class="form-card"><h2>My queue</h2><ol id="opdWsQueue"></ol><button id="opdWsCallNext" class="btn-primary">Call next</button></aside>
    <main>
      <div id="opdWsHeader" class="form-card"></div>   <!-- name, MRN, age/sex, ALLERGIES (red if recorded, amber if unknown), visit type -->
      <div id="opdWsVitals" class="form-card"></div>   <!-- BP, pulse, temp (°C/°F toggle), SpO2, wt, ht, BMI -->
      <form id="opdWsNotes" class="form-card">
        <textarea id="opdCC"></textarea><textarea id="opdFindings"></textarea><textarea id="opdExam"></textarea>
        <div id="opdDx"></div>                         <!-- rows: optional ICD-10 code + text -->
        <textarea id="opdAdvice"></textarea><input type="date" id="opdFollowUp">
        <span id="opdSaveState" aria-live="polite"></span>
      </form>
      <div id="opdRx" class="form-card">               <!-- e-Prescription studio -->
        <table id="opdRxGrid"></table>                 <!-- drug, formulation, strength, dose, route, freq, timing, days, instructions; add/remove/reorder -->
        <datalist id="opdFreqList"><option>OD</option><option>BD</option><option>TDS</option><option>QID</option><option>HS</option><option>SOS</option><option>STAT</option></datalist>
        <div id="opdRxWarnings" class="opd-warn" hidden></div>   <!-- each warning needs an override reason -->
        <textarea id="opdRxGeneral"></textarea>
      </div>
      <footer class="form-card">
        <button id="opdSignBtn" class="btn-primary">Sign &amp; finalise</button>
        <button id="opdPdfBtn" class="btn-secondary" disabled>Print / PDF</button>
        <button id="opdSendBtn" class="btn-secondary" disabled>Send on WhatsApp</button>
        <button id="opdAmendBtn" class="btn-secondary" hidden>Amend</button>
      </footer>
      <details class="form-card"><summary>Past visits</summary><ol id="opdWsHistory"></ol></details>
    </main>
  </div>
</section>
```
Logic: notes autosave 1.5 s after the last keystroke via `PUT /encounters/{id}` with `expected_updated_at`; a 409 shows "Edited elsewhere — reload" (no silent overwrite). The Rx grid saves on blur. Sign = flush pending saves → `POST /encounters/{id}/sign` → if the Rx has items, `POST /prescriptions/{id}/sign` with acknowledgements; a 422 highlights the warning rows. Once signed the page gets `.opd-locked` and Amend appears. PDF = authenticated `fetch` → `Blob` → `URL.createObjectURL` → new tab. Logins without a doctor link see the page read-only (Sign hidden); the server enforces it regardless.

### 4.9 Billing & cashier desk (`#page-opdbilling`)

```html
<section id="page-opdbilling" class="page" hidden>
  <header class="page-head"><h1>Billing</h1><div id="opdShiftBar"></div></header>   <!-- open/close shift, live cash total -->
  <div class="opd-split">
    <aside class="form-card"><input id="opdBillSearch" type="search"><ol id="opdBillQueue"></ol></aside>   <!-- 'billing' stage + unpaid invoices -->
    <main class="form-card">
      <div id="opdInvHeader"></div>        <!-- INV number or "Draft", patient, status pill -->
      <table id="opdInvLines"></table>      <!-- catalog picker + qty + discount -->
      <div id="opdInvTotals"></div>
      <div id="opdPayPanel">                <!-- mode tabs: Cash | UPI (QR + UTR) | Card (RRN) | Payment link -->
        <input id="opdPayAmount" inputmode="decimal"><input id="opdPayRef">
        <canvas id="opdUpiQr" width="220" height="220" hidden></canvas>
        <button id="opdPayBtn" class="btn-primary">Record payment</button>
      </div>
      <ol id="opdReceipts"></ol>            <!-- RCT numbers, mode, amount, Print 80 mm -->
      <footer>
        <button id="opdIssueBtn">Issue</button><button id="opdInvPdf">PDF</button>
        <button id="opdVoidBtn" data-opd="admin">Void</button><button id="opdRefundBtn" data-opd="admin">Refund</button>
      </footer>
    </main>
  </div>
  <iframe id="opdPrintFrame" title="Receipt print" hidden></iframe>
  <dialog id="opdCloseShift"><!-- expected (from server), declared input, variance preview, notes --></dialog>
</section>
```
Logic: amounts typed in rupees, converted once to integer paise (`Math.round(x * 100)`), shown with `Intl.NumberFormat('en-IN', {style:'currency', currency:'INR'})`. Each "Record payment" click mints `crypto.randomUUID()` as `idempotency_key`, reused on retry of the same click. Split payment = several receipts until the balance is 0. UPI QR payload `upi://pay?pa=<vpa>&pn=<clinic>&am=<rupees>&tn=<INV no>&cu=INR`, drawn by the vendored QR lib. Thermal print: receipt HTML → `#opdPrintFrame.srcdoc` → `contentWindow.print()`.

### 4.10 Public hallway display (`admin/queue-display.html`)

Standalone page; own minimal CSS (high-contrast dark, `clamp()` font sizes for 32"–65" TVs); no admin JS, no cookies.

```html
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Now Serving</title><style>/* inline */</style></head>
<body>
  <header><h1 id="clinic"></h1><time id="clock"></time><button id="fs" aria-label="Full screen">⛶</button></header>
  <main id="board"></main>       <!-- per doctor: name, room, NOW SERVING (huge), next 5 tokens -->
  <footer id="status" aria-live="polite"></footer>
<script>
  const p = new URLSearchParams(location.hash.slice(1));   // #t=<token>&b=<branch_id>
  const token = p.get('t'), branch = p.get('b') || '';
  let last = '';
  async function poll() {
    try {
      const r = await fetch('/public/queue-display/data' + (branch ? '?branch_id=' + encodeURIComponent(branch) : ''),
                            { headers: { 'X-Display-Token': token }, cache: 'no-store' });
      if ([401, 403, 404].includes(r.status)) return render({ error: 'This display link is no longer valid.' });
      const d = await r.json(); const sig = JSON.stringify(d.doctors);
      if (sig !== last) { chimeIfNowServingChanged(last, d); last = sig; render(d); }
      setStatus('');
    } catch { setStatus('Reconnecting…'); }
  }
  poll(); setInterval(poll, 5000);
  // render() uses textContent only; chime = WebAudio two-tone beep (no audio asset)
</script></body></html>
```

---

## Part 5 — Phased implementation (5 milestones, each independently shippable)

Each phase ships behind `opd_enabled` (off for every clinic by default) and must leave the full suite green. No phase changes behaviour for a clinic without the flag **except** the walk-in exclusions in slot/reminder queries, which are no-ops while no walk-in row exists.

### Phase 1.1 — Migration, tenancy, entitlement
Scope: `103_opd_os_core.sql` + `103_down.sql`; `TENANT_OWNED_TABLES`; `OPT_IN_FEATURES` + `opd_enabled()`/`opd_eligible()`; permissions/roles; `AdminUser.doctor_id` + `verify_credentials`; `/admin/me` flags; platform toggle guards + provisioning + deactivation preview + deletion-preview/purge; platform OPD card; doctor registration fields + staff doctor link (API + forms); `DELETE /admin/doctors` 409 mapping; `verify_supabase_schema.sql`; prod PG version check (F17).
Exit: migration applies on embedded PG **and** a Supabase branch; rollback applies and re-apply succeeds; trigger/FK/RLS tests pass; an enterprise clinic still reports `opd_enabled=false`; toggle round-trip loses no data.

### Phase 1.2 — Registry, walk-ins, live queue
Scope: `app/services/opd.py` (registry, MRN, walk-in, stages, call-next, board, setup wizard + dry run + go-live, display token, public payload); router groups §3.2–3.4 and §3.10; `check_in_appointment(initial_queue_status)`; legacy check-in/call-next delegation; walk-in exclusion in `get_available_slots` / `book_appointment` / `create_booking_with_payment`; scheduler reminder/leave exclusion; `data_retention` family-member split; frontend Setup, Front Desk, Live Queue, `queue-display.html`.
Exit: two concurrent walk-ins never share a token (threaded real-PG test); a 10:00 walk-in does not hide the 10:00 WhatsApp slot; non-OPD check-in/call-next unchanged (`test_admin_queue.py`, `test_queue_database.py` green without edits).

### Phase 1.3 — Consultation workspace & signed e-prescriptions
Scope: `opd_clinical.py`, `opd_pdf.py`, fonts, `fpdf2`/`uharfbuzz`; routes §3.5–3.6; WhatsApp document send (session + template); frontend workspace + Rx studio.
Exit: only the linked treating doctor can sign (clinic_admin without a link → 403); signed rows immutable at the DB level; amendment chain correct; PDF carries the registration number; allergy warnings block signing until acknowledged; AST no-LLM test green; firewall suite green.

### Phase 1.4 — Invoicing, cashier, payments, receipts
Scope: `opd_billing.py`; routes §3.7; payment-link creation + webhook branches (Razorpay + PhonePe); prepaid detection; shifts + reconciliation; invoice PDF; thermal print; vendored QR; frontend Billing page.
Exit: gapless INV/RCT numbers under 20 concurrent issues; over-payment rejected by the DB; a duplicate webhook delivery creates one receipt; booking payment e2e (`test_session20_payment_e2e.py`) unchanged; shift variance correct with interleaved cash/UPI/refund.

### Phase 1.5 — Channels, analytics, hardening
Scope: `booking_channel` stamping in conversation + voice + admin create; `get_patient_queue_status` OPD delegation; template wiring; analytics endpoint + page; adversarial-matrix extension; queue-polling load test (50 boards × 5 s); `docs/agent-context` updates; production enablement runbook for the first clinic.
Exit: WhatsApp "queue status", the voice `queue_status` tool and the board agree for the same patient; analytics totals reconcile with invoices/receipts; the super-admin scope matrix and route adversarial matrix cover every `/admin/opd/*` route.

### Deploy order (every phase)
1. Phase 1.1 only: `python scripts/migrate.py` against prod **off-peak** (later phases add no migration).
2. Deploy code. 3. Smoke: `/health`, a WhatsApp booking on the sandbox clinic, owner toggle on the sandbox clinic only.
4. Enable for one pilot clinic in the owner console; run the wizard with the clinic; go live.
