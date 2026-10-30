# SESSION 43 — G4 GST Billing, Credit Notes, Room-Rent GST

**Date**: 2026-10-28
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Migration**: `117_gst_billing_and_credit_notes.sql` (+ `rollback/117_down.sql`). Not applied to any Supabase project.
**Before**: full suite 6936 passed (after Session 42). **After**: 6962 passed, 4 skipped, 0 failed (before the last UI-only edits; affected suites re-run green after them).

---

## Goal

Bills that are legally right under GST without disturbing the exempt majority: health care stays a bill
of supply at 0%; tax only for a GSTIN-registered clinic on taxable catalog items and on room rent above
₹5,000/day outside ICU (Notification 04/2022-CT(R)); reductions after issue only by numbered credit notes
(CGST Act s.34); a month-end GSTR-1 working summary.

## Design decisions

| Decision | Why |
|:-|:-|
| GSTIN in `clinics.opd_settings.gstin` (API-validated incl. check digit), tax rate on catalog entries | No new clinic column; every bill path (`create_invoice`, draft-save RPC, order billing, IPD final bill) already reads those settings. One BEFORE trigger on `opd_invoice_items` sets rate, SAC and CGST/SGST, so no Python path can get tax wrong or skip it. |
| No GSTIN → rate forced to 0 in the database | An unregistered clinic cannot charge GST, whatever the catalog says. Exempt-clinic invariance is a DB rule, not a UI convention. |
| Tax exclusive; `subtotal = Σ(line + line tax)` | `total_paise` (generated) is unchanged, so receipts, payment links, deposits and the paid/status CHECK collect the right amount with no change. |
| Header discount pro-rates tax | Lowers taxable value proportionally (header `total_tax_paise`); GSTR-1 splits by rate with the same factor and pushes rounding to the largest bucket so rows add up exactly. |
| Credit notes keep the invoice total; `credited_paise` lowers what is due | An issued invoice is never edited. `due = total − credited − paid`; the paid/status CHECK, receipt trigger, deposit application, running bill and discharge checklist use it. |
| Credit-note number from `opd_next_counter('crn:YYYY')` | The spec's `MAX(seq)+1` races (two concurrent notes → duplicate key). The counter row lock is the same gapless mechanism as INV-/RCT-. |
| Excess paid is refunded in the same transaction | Otherwise paid > due violates the CHECK. Refund is cash/UPI into the issuer's open shift (shift close balances; tested). |
| Notes are tax-proportional; the last note takes the exact remainder | Tax reversed never exceeds tax charged (tested with 4 notes on an odd amount). |
| Void refused once a credit note exists | Re-crediting is the only path; voiding without credit notes is unchanged (IPD re-bill still works). |
| Room-rent SAC `999311`, not `996311` | 04/2022 taxes the room under heading 9993 (health); 9963 is hotel accommodation. ICU-type = ward_type `icu/nicu/picu` **or** bed category `icu/nicu/picu/ccu/iccu`; HDU is taxed (not in the notification's list). The day's bed is the one the accrual prices, not the current bed. |
| Permission `BILLING_CREDIT_NOTE` (CASHIER preset); CASHIER role implies it | Cashier logins saved before 117 have no stored grant. OPD_ADMIN and clinic/super admins also pass. |

## Deviations from the task spec

`opd_next_credit_note_seq` (race) replaced by the counter; SAC 999311; tax-rate CHECK is the slab set
`0/5/12/18/28/40%` instead of `BETWEEN 0 AND 2800` (catches `18` typed for `1800`; allows 40%);
`issued_by` nullable (super-admin env login has no UUID); `amount_paise ≥ 0` with `amount + tax > 0`;
room-rent trigger reads the assignment for the service date instead of the admission's current bed.

## Files

| File | Change |
|:-|:-|
| `migrations/117_gst_billing_and_credit_notes.sql`, `rollback/117_down.sql` | Schema, triggers, `opd_issue_credit_note`, tax-aware redefinitions of 8 functions; down refuses credit notes / taxed rows and restores the 103/116 definitions verbatim. |
| `app/services/gst_billing.py` (new) | GSTIN check digit, `calculate_line_gst`, `issue_credit_note`, `export_gstr1_report`. |
| `app/routers/opd.py`, `app/main.py` | `POST/GET /admin/opd/invoices/{id}/credit-notes`, `GET /admin/billing/gstr1`, `gstin` setting, catalog `hsn_sac`/`tax_rate_bps`, buyer `patient_gstin`. |
| `app/services/opd_billing.py`, `opd_pdf.py` | Balance net of credits; credit notes on the invoice; PDF title Tax Invoice / Bill of Supply, HSN + GST columns, CGST/SGST, credit notes. |
| `app/tenancy.py`, `app/services/permissions.py` | `opd_credit_notes` tenant table; `BILLING_CREDIT_NOTE`. |
| `admin/index.html` | GSTIN setting, GSTR-1 card, tax/credit totals, buyer GSTIN, credit-note form + list + 80mm print, permission checkboxes. Checked at 390px. |
| `tests/test_migration_117_gst.py`, `tests/test_gst_billing_e2e.py` (new); 103/116 rollback tests run 117_down first. |

## Not done / next

- No UI editor for the service catalog exists (pre-existing); tax rates per item are set through `PUT /admin/opd/setup/settings`.
- Credit notes on a deposit-paid IPD bill refund cash/UPI, not back into the stay's deposit balance.
- GSTR-1 is a working summary (JSON), not the GSTN offline-tool upload format; numbering is per calendar year like INV-, not per April–March financial year.
- `opd_cancel_order`'s pre-check ignores line tax (the subtotal CHECK still protects the data).
- Deploy: apply 117 on a Supabase staging branch, run `tests/test_migration_117_gst.py tests/test_gst_billing_e2e.py` with `TEST_DATABASE_URL`.
