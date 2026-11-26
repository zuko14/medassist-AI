# Kriya Data Import — Switching from Old Systems (lane **MI**)

> Gap #2 of the product review: "removes the biggest reason hospitals refuse to switch". Tracker: [status.md](status.md).
> This spec extends what exists; it never weakens the consent isolation that `patient_records` was built for.

## 1. What exists today (evidence)
| Import | Where | Notes |
|:-|:-|:-|
| Legacy patient list → `patient_records` (isolated; not `patients`) | `app/services/client_data.py` (docstring explains why), `POST /admin/data/patient-records/import` (`app/routers/admin.py:8921`), `migrations/088_client_data_and_support.sql:65` | 5 MB / 25,000 rows per file; dedupe key; quota per clinic; read only by the Data & Support page and its export |
| Lab tests catalogue | `/admin/lab-tests/import-csv`, `import-preview` → `import-apply/{preview_id}` | preview + apply pattern |
| DX catalogue, B2B rate cards | `/admin/dx/import/dry-run|apply`, `/admin/dx/rate-cards/{id}/import` | dry run pattern |
| Pharmacy items, opening stock | `/admin/rx/items/import/dry-run|apply`, `/admin/rx/stock/opening-import` | CSV/XLSX (`openpyxl` in requirements) |
| Voice outbound list | `/admin/voice/outbound/bulk` | consent attestation |
| HMIS bridge (push appointments / pull roster) | `app/services/hmis_bridge.py` | per-clinic webhook config; not an import |

**Missing for a hospital switch:** doctors + schedules (department is a text column on `doctors`, `001_initial_schema.sql:68` — there is no departments table), services/tariffs (`billable_services`, 120),
packages, insurance payers, patient master **linked to live records with legacy MRN**, visit history (diagnoses,
prescriptions, lab values) as read-only legacy documents, future appointments, outstanding patient dues, current
inpatients (cutover list), chronic-patient lists for Continuous Care.

## 2. Principles
1. **Dry run → review → apply**, always. Apply is idempotent (re-running the same file changes nothing).
2. **Never fabricate consent.** Imported people never get `opted_in = true`; WhatsApp/voice outreach to them follows
   the existing rules (they become contacts only when they message the clinic, or staff attest consent per person).
3. **Legacy is labelled legacy.** Imported clinical history is a read-only "Imported record (from <system>, <date>)"
   — never a signed Kriya encounter/prescription, never used by the clinical firewall or protocol engine as a
   doctor's decision; Continuous Care uses it only to propose **candidates**.
4. **One batch = one undo.** Every row carries `import_batch_id`; a batch can be deleted until anything references it.
5. Files are untrusted: size/row caps, header allow-list, formula-injection guard on export (`=`, `+`, `-`, `@`
   prefixed cells quoted), no macros (CSV/XLSX values only), phone/date normalisation with row-level errors.

## 3. New import kinds
| Kind | Target | Key rule |
|:-|:-|:-|
| `doctors` | `doctors` (department text, registration number, `consultation_fee`) | duplicates by registration number / name+phone; schedules imported as weekly session templates through the existing roster functions |
| `services` | `billable_services` (120) with HSN/SAC, tax rate, price | price changes create a new tariff row only if the masters model versions; else update with audit |
| `packages` | `ipd_packages` (116) | inclusions as text lines; never auto-priced |
| `payers` | `insurance_payers` (124) | merge by name + code |
| `patients_link` | `patient_records` (existing) + new `mi_patient_links (patient_record_id → patients.id, legacy_mrn)` | link at first contact (front-desk search shows legacy hits; "Use this record" creates/links `patients` and keeps the legacy MRN); WhatsApp first message matches by phone → staff confirm in a queue — never auto-merge on phone alone (shared family phones) |
| `history` | `mi_legacy_encounters` (date, doctor name text, diagnosis text, optional ICD-10, medications text, vitals text, source system) and `mi_legacy_lab_values` (test name, value, unit, date, flag) | read-only cards on the patient profile; ICD-10 optional (a staff mapping table turns legacy diagnosis text → ICD codes deterministically for CC candidate search) |
| `appointments_future` | `appointments` via `book_appointment`, each slot first validated through `get_available_slots` (holidays, leave, sessions — `book_appointment` checks only conflicts; VERIFICATION D16) | slot conflicts and unavailable slots reported, never forced; imported bookings send **no** confirmation message unless staff choose to |
| `dues` | `mi_opening_balances` (patient, amount, as-of date, reference) | shown at the counter; collected through a normal OPD receipt against an "Opening balance" service line |
| `inpatients_cutover` | report only | lists who must be admitted in Kriya on day 1 (admission is a clinical act — done by staff) |
| `chronic_list` | CC candidates (`cc_radar_rows` reason `candidate`, source `import`) | requires CC on; never enrolls |

Presets: header mappings for common exports (MocDoc, Practo Insta, KareXpert, Marg, generic Excel). A preset is a
column-name map only and is validated on a real export from the system on the sandbox before a client go-live
(same rule as RX ROLLOUT §5). No vendor API scraping, no screen scraping.

## 4. Data model — migration 149 (provisional) `149_data_import.sql`
`mi_batches` (clinic, kind, file name, sha256, rows total/ok/error, status `dry_run/applied/undone`, created_by),
`mi_row_errors` (batch, row no, column, problem — ≤ 100 per batch shown), `mi_patient_links`, `mi_legacy_encounters`,
`mi_legacy_lab_values`, `mi_opening_balances`, `mi_diagnosis_map` (legacy text → ICD-10, per clinic). All in
`TENANT_OWNED_TABLES`; DPDP erasure deletes a person's legacy rows; platform purge deletes all.

## 5. API and UI
`POST /admin/mi/{kind}/dry-run` (multipart, returns preview id + counts + first errors) · `POST /admin/mi/{kind}/apply/{preview_id}`
· `DELETE /admin/mi/batches/{id}` (undo while unreferenced) · `GET /admin/mi/batches`. Permission `DATA_IMPORT`
(clinic admin only by default). UI (blueprint: [UI spec §7.8](../design/NEXT-LANES-UI-SPEC.md); icon Lucide `import`; gates UI-G1…G12): **Switch to Kriya** wizard on the Data & Support page — steps in dependency order
(doctors → services → packages → payers → patients → history → dues → future appointments), each with
template download, dry run, error table, apply, undo; a progress checklist the onboarding team and the owner console
can see.

## 6. Cutover runbook (per hospital)
T−14 export from old system → sandbox dry runs → fix mappings. T−7 full dry run on production clinic (no apply).
T−1 apply masters + patients + history. Day 0 freeze old system for registration; import future appointments and
dues; admit current inpatients manually from the cutover list; pharmacy opening stock (RX ROLLOUT §4). Day +7 compare
counts (patients, appointments, dues) with the old system's reports; sign-off.

## 7. Tests
Per kind: parser vectors (headers, dates dd/mm/yyyy and ISO, Indian phone formats, Devanagari names), idempotent
re-apply, undo, row caps, formula injection, consent never set, `book_appointment` conflicts reported, legacy history
never appears as a signed record; route matrix; zero impact (existing patient import and exports unchanged).
