# SESSION 49 — G13 Radiology Information System, PACS link, statutory PCPNDT Form F

**Date**: 2026-10-30
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Migration**: `123_radiology_and_pcpndt.sql` (+ `rollback/123_down.sql`). Not applied to any Supabase project.
**After**: full suite 7600 passed, 4 skipped, 0 failed. UI checked end to end against the real app on local Postgres (not Supabase), at desktop width and 390 px.

---

## Goal

A G1 imaging order (X-ray, USG, CT, MRI …) is reported by a radiologist: draft → signed report, which
completes the order in the same transaction, freezes the text and the signer's registration, and sends the
letterhead PDF to the patient's WhatsApp. A correction is a versioned amendment. An obstetric ultrasound
cannot be signed without a recorded, locked PCPNDT Form F, and nothing that determines or discloses the sex of
the foetus is ever saved.

## Design decisions

| Decision | Why |
|:-|:-|
| `opd_next_counter('rad:YYYY' / 'pndt:YYYY')`, not the spec's `radiology_next_seq` / `pcpndt_next_seq` (MAX+1) | MAX+1 hands concurrent saves the same number (same reason as 121 / 122). 11 concurrent first saves get 11 consecutive numbers (tested). An amendment keeps its report's number (`RAD-2026-00012 v2`). |
| Tenant-composite FKs everywhere (`(clinic_id, id)`), not the spec's plain FKs | A row can never point at another clinic's order, patient or doctor. |
| Report → order FK carries `patient_id` (`ON UPDATE CASCADE ON DELETE CASCADE`), Form F → report the same | A G7 emergency identity merge moves `opd_orders.patient_id`; report and Form F follow, and the guards admit exactly that change (`patient_merge_allows`). Owner purge cascades. Both tested. |
| Every write is one SQL function (`radiology_save_report`, `radiology_record_form_f`, `radiology_finalize_report`); guards refuse direct writes; the only later write is the delivered PDF's path, once | Same pattern as LIS / OT. Finalize locks order then report (same order everywhere: no deadlock), checks `expected_updated_at` so a signature never covers text nobody reviewed, and is replay-safe (double click). |
| Radiologist is NULL on a draft; signing and Form F need a login linked to a doctor with a registration number | A draft can be prepared by staff; the legal signer is identified at signing. `RADIOLOGIST` joins `_DOCTOR_LINKED_ROLES`. |
| Obstetric detection: an ultrasound whose procedure or history reads obstetric (`radiology_is_obstetric_text`) IS obstetric — a CHECK, not just a UI hint | A radiologist cannot untick the box to skip Form F. Once Form F exists the study stays obstetric (`radiology_form_f_fixes_obstetric`). Obstetric on a male patient is refused (422). |
| PCPNDT firewall in two places: `clinical_firewall.fetal_sex_disclosure` (400 before any write) and `radiology_fetal_sex_text` (trigger + CHECK). Every report: the explicit phrases ("male fetus", "sex of the baby", Hindi / Telugu). Obstetric records: any sex / gender / genital word at all | Zero tolerance on obstetric records; a man's own anatomy on a non-obstetric study ("normal male pelvis", "both testes normal") still goes through. A parity test keeps SQL and Python identical on a shared corpus. The blocked text is never logged. |
| Form F keyed by order (`UNIQUE (clinic_id, order_id)`), recorded and locked in one step (`is_locked` CHECK true, both declarations CHECK true, sons + daughters = living children, LMP within 45 weeks) | Form F is per procedure, so an amended report reuses it. A repeat returns the recorded form. |
| Form F PDF readable only with `RADIOLOGY_WRITE` | It is the facility's statutory register, not part of the patient's report. It is never sent on WhatsApp. |
| Obstetric report delivery skips the AI summary (`upload_and_send(ai_summary=False)`) | Antenatal text never reaches a model. Other reports keep the existing pipeline. |
| PACS: `pacs_viewer_url` must be `http(s)://` (CHECK + pydantic), study UID is a DICOM UID | The panel opens it in a new tab (`noopener`); no `javascript:` links. |
| Imaging orders identified by the tariff master (`billable_services.category = 'radiology'`, or a radiology department) or by modality keywords in the item name | `opd_orders` has no modality column; legacy JSON catalogue and custom lines still work. |
| `orders.complete_order` (G1 "complete with a text note") refuses once a radiology report exists | Only the radiologist's signature releases the study, as LIS does. |

## Permissions

`RADIOLOGY_READ` (worklist, reports, report PDF), `RADIOLOGY_WRITE` (draft, Form F, sign). Staff role
`RADIOLOGIST` (preset `ORDERS_READ`, both radiology grants, doctor link required). `DOCTOR` preset gains both;
`NURSE` gains READ. Logins saved before 123: `OPD_CLINICAL` / `IPD_CLINICAL` / `IPD_NURSING` imply READ; roles
`RADIOLOGIST` / `DOCTOR` imply WRITE (`holds_radiology_permission`). Entitlement: OPD or IPD module.

## API (`app/routers/radiology.py`)

| Route | |
|:-|:-|
| `GET /admin/radiology/worklist?status=pending\|completed&modality=x_ray\|ultrasound\|ct_mri\|…` | open imaging orders STAT first with their draft; completed = signed in the last 7 days |
| `GET /admin/radiology/orders/{id}` | console for an order: latest report, or suggested modality / obstetric flag; letterhead for the preview |
| `POST /admin/radiology/reports` | save the draft for an order (create / edit / amendment with reason) |
| `GET /admin/radiology/reports/{id}` | report, Form F, versions, letterhead |
| `POST /admin/radiology/reports/{id}/finalize` | sign; order completed; PDF to WhatsApp in the background |
| `GET /admin/radiology/reports/{id}/pdf` | signed report PDF (`render_radiology_report_pdf`) |
| `POST /admin/radiology/reports/{id}/pcpndt-form-f` | record + lock Form F (`PNDT-YYYY-NNNNN`) |
| `GET /admin/radiology/reports/{id}/pcpndt-form-f/pdf` | Form F PDF (`render_pcpndt_form_f_pdf`) |

Every write is audited (`RADIOLOGY_REPORT_CREATE` / `_AMEND` / `_SIGN`, `PCPNDT_FORM_F_LOCK`, the latter
without maternal history).

## UI (`admin/index.html` only)

Nav "Radiology"; tabs All | X-Ray | Ultrasound | CT / MRI | Completed; rows show token / IP number, patient,
procedure, modality and urgency chips, status (with "Form F pending"), Write Report / View. The reporting
console has quick templates, the PACS viewer button, the "⚠️ Mandatory PCPNDT Form F Required" banner with
the Form F drawer (LMP → gestational age, sons / daughters, indication, both declarations), a live letterhead
preview, Save draft, Finalize & Sign (disabled until Form F; signs only what is saved), amendment, PDFs.
Staff create / edit offer the Radiologist role and both grants. A request-sequence guard stops a slower,
older worklist load from overwriting the selected tab.

## Not done / follow-ups

- Not applied to Supabase; not deployed.
- No DICOM / PACS server integration beyond storing a study UID and viewer link (no worklist push / MWL).
- Form F Section C (invasive procedures: amniocentesis / CVS details) is a flag only; the form says it is kept separately.
- The monthly Form F submission to the Appropriate Authority (by the 5th) is not generated; the register is queryable (`idx_pcpndt_form_f_register`).

## Verification

| Command | Result |
|:-|:-|
| `pytest tests/test_lint_unscoped_queries.py` | 4 passed |
| `pytest tests/test_migration_123_radiology.py tests/test_radiology_api_e2e.py` | 59 passed |
| `pytest tests/test_admin_super_admin_scope_matrix.py tests/test_clinical_firewall.py` | 741 passed |
| `pytest tests/test_ipd_no_llm.py tests/test_migration_1*.py` | 196 passed |
| `pytest tests/` | 7600 passed, 4 skipped, 0 failed |

Live UI pass (real app, local Postgres, PgBridge) found and fixed: a worklist load race overwriting the
selected tab, Form F PDF button shown to read-only roles (server refuses it), card-layout wrapping of multi-line
cells, an empty-MRN separator in the preview.
