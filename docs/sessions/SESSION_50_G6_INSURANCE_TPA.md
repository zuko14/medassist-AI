# SESSION 50 — G6 Insurance / TPA: cashless pre-authorisation, dual-debtor claims, settlement, ageing

**Date**: 2026-10-30
**Branch**: `hardening/production-readiness-audit` (uncommitted; not deployed)
**Migration**: `124_insurance_tpa_and_cashless.sql` (+ `rollback/124_down.sql`). Not applied to any Supabase project.
**After**: full suite 7667 passed, 4 skipped, 0 failed. UI checked end to end against the real app on local Postgres (not Supabase), desktop and 390 px.

---

## Goal

An insured inpatient: the TPA desk attaches the card, requests cashless pre-authorisation, records the
insurer's query / approval / enhancement, and at discharge issues the bill split between two debtors —
the insurer's approved share (a receivable) and the patient's co-pay / non-payables (paid at the counter,
deposits first). Later the payer's NEFT, TDS and reason-coded disallowances settle the claim; disallowances
are written off (with billing approval) or recovered from the patient. Payer receivables are aged.

## Design decisions

| Decision | Why |
|:-|:-|
| The final bill stays the one GST tax invoice for the whole stay; the split lives on the claim (`total_bill = patient_payable + claimed`). The spec put only the patient's share on the bill | A bill of the patient's share alone understates revenue and GST and leaves the insurer's money with no invoice to settle against. With one invoice, G4 (GST, credit notes), G5 (receivables, deposits) and the day book keep agreeing. |
| Payer money is an `opd_receipts` row: mode `insurance` (NEFT, UTR required) and `tds` (tax withheld), no cashier shift, written only by `ins_record_settlement` | Settling the invoice with receipts makes "paid" true for the invoice; insurer money never sits in a cash drawer. `tds` is not counted as collected in the day book. |
| A written-off disallowance is a G4 credit note (reason `insurance_disallowance`), in the same transaction; it needs the credit-note permission (billing approval) | A write-off reduces revenue and GST: that is exactly what a credit note is for, numbered and audited. |
| `ins_payer_hold` = payer outstanding + unresolved disallowances (or, before the claim, the approved amount). G5's `ipd_apply_deposits`, `ipd_running_bill`, `ipd_discharge_checklist` subtract it; a counter receipt above the patient's share is refused (`ins_receipt_exceeds_patient_share`, checked in the API and by trigger) | Deposits and the counter must never pay the insurer's share, also in the window between bill issue (which applies deposits) and claim filing. A stay can leave once the patient's share is paid. |
| Pre-auth state machine in one function (`ins_pre_auth_transition`): draft → submitted ⇄ query_raised → approved → enhancement_requested → approved (or denied), rejected, cancelled; one live per stay; frozen once claimed | The approved amount is the latest letter's TOTAL, never above what was requested (CHECK), and an enhancement never lowers it. Every step is an `ipd_events` row (the history the card shows). |
| `opd_next_counter` for PRE- / CLM- numbers, not the spec's MAX+1 functions | MAX+1 hands concurrent filings one number. |
| A live claim pins its bill: voiding it is refused (trigger + an up-front API check, before deposits are un-applied); withdraw the claim first (only before any settlement) | A payer cannot be asked to pay against a cancelled invoice. The void route used to un-apply deposits before trying the void, so the check runs first. |
| Settlement idempotent (key), serialised on the stay row; received + TDS + disallowed never exceeds outstanding | 10 concurrent settlements of ₹10,000 on a ₹90,000 claim: 9 succeed, outstanding never negative (tested). |
| Ledger (`ipd_claim_transactions`) append-only; a disallowance is resolved exactly once | Spec: deductions cannot be deleted. |
| The pre-auth carries the stay's patient (`ON UPDATE CASCADE` to `ipd_admissions (clinic_id, id, patient_id)`) | A G7 identity merge moves it (tested). Policies stay on the patient they were recorded for. |
| Payers seeded per clinic (12: Medi Assist, Vidal, Paramount, MDIndia, Heritage, Star Health, Care, HDFC ERGO, ICICI Lombard, Niva Bupa, PM-JAY, CGHS): existing clinics by the migration, new clinics on their first read | Payers are tenant data (each hospital edits its own empanelment list). |
| `TPA_DESK` gets `INSURANCE_READ`, `INSURANCE_MANAGE`, `ORDERS_READ` — the spec's `IPD_READ` does not exist, and IPD billing would grant charge / deposit edits | The desk works each stay from the Insurance page's own stay view (card, split, running-bill figures served by `/admin/ipd/admissions/{id}/insurance`). |

## Permissions

`INSURANCE_READ` (pipeline, claims, payers, ageing, the stay card), `INSURANCE_MANAGE` (payers, policies,
pre-auth, cashless bill, settlement). Implied for logins saved before 124: READ by `IPD_BILLING`,
`IPD_ADMISSIONS`, `IPD_ADMIN`, `OPD_BILLING`, roles `TPA_DESK` / `CASHIER`; MANAGE by `IPD_ADMIN`, role
`TPA_DESK`. Write-off additionally needs the credit-note grant. Entitlement: the IPD module.

## API

| Route | |
|:-|:-|
| `GET/POST /admin/insurance/payers`, `PUT /admin/insurance/payers/{id}` | payer master |
| `POST /admin/insurance/policies`, `GET /admin/insurance/policies/{patient_id}` | insurance cards |
| `GET /admin/insurance/pre-auths` | live pipeline |
| `GET /admin/insurance/claims[?view=all]`, `GET /admin/insurance/claims/{id}` | claims with split and ledger |
| `POST /admin/insurance/claims/{id}/settlement` | NEFT + UTR, TDS, disallowances (reason codes) |
| `POST /admin/insurance/claims/{id}/action` | payer query / in process / withdraw |
| `POST /admin/insurance/disallowances/{id}/resolve` | written_off (credit note) / recovered_from_patient |
| `GET /admin/insurance/ageing` | <30 / 30-60 / 60-90 / >90 days by payer |
| `GET /admin/ipd/admissions/{id}/insurance` | the cashless card |
| `POST /admin/ipd/admissions/{id}/pre-auth` | request pre-authorisation |
| `PATCH /admin/ipd/pre-auth/{id}/decision` | approved / query_raised / rejected / enhancement_denied |
| `POST /admin/ipd/pre-auth/{id}/action` | submit / request_enhancement / cancel |
| `POST /admin/ipd/admissions/{id}/cashless-bill` | generate + issue the final bill, file the claim |

## UI (`admin/index.html` only)

Nav "Insurance / TPA" with Pre-Auth Pipeline | Active Claims | Payers Master | Ageing Report; each row opens
the stay view (split + cashless card) or the claim (ledger, settlement form, disallowance resolution).
Inpatient Chart → Charges & Bill: the running bill shows Total incurred / Insurer share / **Patient payable**,
plus the cashless card (payer, card no., pre-auth badge, Request Enhancement, Cashless bill & file claim).
Staff create / edit offer the TPA desk role and both grants.

## Not done / follow-ups

- Not applied to Supabase; not deployed.
- No insurer / NHCX / TPA portal integration: decisions and settlements are keyed in from the insurer's letter.
- Approval letters are referenced by path only (no upload).
- Room-rent proportionate deduction is entered as an amount, not computed from the policy's room-rent limit.
- Policies do not move in a G7 identity merge (the pre-auth does).

## Verification

| Command | Result |
|:-|:-|
| `pytest tests/test_lint_unscoped_queries.py` | 4 passed |
| `pytest tests/test_migration_124_insurance.py tests/test_insurance_api_e2e.py` | 16 passed |
| `pytest tests/test_admin_super_admin_scope_matrix.py tests/test_clinical_firewall.py` | 775 passed |
| `pytest tests/test_ipd_no_llm.py tests/test_migration_1*.py` | 208 passed |
| `pytest tests/` | 7667 passed, 4 skipped, 0 failed |

Found and fixed while building: deposits applied at bill issue could pay the insurer's share before the claim
existed (now the approved amount holds it); counter receipts could do the same (now capped, API + trigger);
the void route un-applied deposits before a refused void (the claim check now runs first); the TPA desk could
not open the IPD chart (it now works each stay from its own view); `ipd_events` timestamps are `occurred_at`.
