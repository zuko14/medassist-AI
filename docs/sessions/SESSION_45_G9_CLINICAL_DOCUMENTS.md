# SESSION 45 — G9 Clinical Documents Engine

**Date**: 2026-10-28
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Migration**: `119_clinical_documents.sql` (+ `rollback/119_down.sql`). Not applied to any Supabase project.
**After**: full suite 7053 passed, 4 skipped, 0 failed. UI not exercised in a browser; PDFs checked by rendering samples.

---

## Goal

Signed, printable clinical documents on the clinic's letterhead that reprint identically forever: the
IPD discharge summary PDF (with vitals, signed LIS results and procedure notes), recorded informed
consents (English / Hindi / Telugu wording), and gapless `CERT-YYYY-NNNNN` medical / fitness certificates.

## Design decisions

| Decision | Why |
|:-|:-|
| Discharge summary keeps 111's versioning (`version`, `supersedes_id`, `amend_reason`, `doctor_snapshot`) | Already immutable and versioned; 119 adds only what printing needs. The spec's `amendment_reason` column would duplicate `amend_reason`. |
| `docs_sign_discharge_summary` = 111's sign function + snapshot write, one transaction | The PDF shows exactly the record at signing (letterhead, patient, stay, first/last vitals, validated lab results, completed orders, procedure notes). Later chart activity never changes it (tested). |
| Guard (redefined in 119) lets a signed row receive snapshots once, the storage path once, and delivery bookkeeping | Everything else stays frozen; snapshots can only be written inside a `docs_*` function. |
| Pre-119 signed summaries are frozen on first print (`docs_freeze_discharge_summary`) | No backfill job; first print wins, later prints are identical. |
| PDF rendered from the row on every download; storage holds a copy | Deterministic; a storage outage never blocks a print. |
| WhatsApp via the OPD prescription path (opt-in check, document in 24 h window, `discharge_summary_ready` / `certificate_ready` template outside it) — not the lab-report pipeline | The lab pipeline runs an AI summariser over the PDF; clinical documents stay LLM-free. Delivery outcome is stored on the row (`delivery_status`, `send_count`, max 5). |
| Signing auto-queues the send (`spawn_background_task`); "Send via WhatsApp" sends now and reports why if not | Matches the spec; the UI tells staff "not opted in" / "no template" instead of failing silently. |
| Consents are recorded as signed (paper / tablet) and immutable; a representative's signature needs a witness (DB CHECK) | The digital row records a real signature, it is not one. |
| Consent FK to the admission `ON DELETE CASCADE` | The owner's IPD purge removes them; outside a purge the guard refuses deletes. |
| Certificate number: `opd_next_counter('cert:YYYY')` (IST year) inside `docs_issue_certificate` | Same gapless row lock as INV-/RCT-/CRN-; 10 concurrent issues get 10 consecutive numbers (tested). Idempotency key replays. |
| Certificates signed only by a doctor login with a registration number; voidable, never editable | Legal signature; a void stays on record and prints with a VOID banner. |
| Composite `(clinic_id, id)` FKs everywhere; certificate's encounter / admission links `ON DELETE SET NULL (col)` | Tenant-safe; a purge unlinks rather than destroying an issued certificate. |

## Fixed on the way

`OPDDocument` never registered its Indic fallback fonts: fpdf2 keys fonts in lower case and the check
used `"NotoTelugu"`. Every Hindi / Telugu character (prescriptions included) was silently dropped. Fixed
(case-insensitive check, `exact_match=False` so bold falls back, HarfBuzz shaping on) with a regression
test in `tests/test_opd_pdf.py`.

## Permissions

`DOCUMENTS_READ` (DOCTOR, NURSE presets; implied by OPD_CLINICAL, IPD_CLINICAL, IPD_NURSING) and
`DOCUMENTS_WRITE` (DOCTOR; implied by OPD_CLINICAL, IPD_CLINICAL). Clinic / super admins hold both but
cannot issue a certificate without a doctor link.

## Deviations from the task spec

- No `amendment_reason` column (111's `amend_reason` exists); `branch_id` etc. are composite FKs.
- Added: `clinical_snapshot`, delivery bookkeeping on summaries; `family_member_id`, `language`,
  `procedure_name`, `doctor_id/snapshot`, snapshots, idempotency on consents; `family_member_id`,
  `encounter_id`, `admission_id`, snapshots, void + delivery columns on certificates.
- `POST …/discharge-summary/pdf` stores and sends the already-signed summary; signing stays on
  `POST /admin/ipd/discharge-summaries/{id}/sign` (attending-doctor rule from 111), which now freezes the
  snapshots and queues the send.
- Added routes: `GET /admin/clinical/consents/{id}/pdf`, `GET /admin/clinical/certificates`,
  `POST …/certificates/{id}/void`, `POST …/certificates/{id}/send`.
- PDFs are stored in the existing private `lab-reports` bucket under `<clinic>/documents/`.
- Hindi / Telugu consent wording is a starting template: have each hospital's own forms reviewed.

## Files

`migrations/119_clinical_documents.sql`, `migrations/rollback/119_down.sql`,
`app/services/clinical_documents.py`, `app/routers/documents.py`, `app/services/opd_pdf.py` (shared A4
blocks, `render_discharge_summary_pdf`, `render_consent_pdf`, `render_certificate_pdf`, Indic font fix),
`app/routers/ipd.py` (sign route), `app/services/ipd.py` (old sign wrapper removed),
`app/services/lis.py` (shared letterhead / subject helpers made public), `app/services/permissions.py`,
`app/tenancy.py`, `app/main.py`, `admin/index.html` (discharge tab official PDF + preview + WhatsApp,
Consents tab + modal, certificate modal from OPD consultation, patient list and inpatient chart, staff
grants), `tests/test_migration_119_documents.py`, `tests/test_clinical_documents_e2e.py`,
`tests/test_opd_pdf.py`, `tests/test_ipd_no_llm.py`.
