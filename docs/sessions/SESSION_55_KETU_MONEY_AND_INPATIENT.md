# SESSION 55: Kriya Ketu money and inpatient tasks

**Date**: 2026-11-01
**Branch**: `ccr-ea78ec15-vz8zco`
**Ask (owner)**: "do the money and inpatient tasks next" (the group left for a later round in session 54).

No migration, no new endpoint, no permission change. Each action calls the endpoint the Inpatient Chart, IPD
Receivables or OPD Billing page already uses, as the logged-in user, so the server's permission, branch and
state checks all still apply.

---

## 1. What Kriya now does

| Action | Say | Endpoint | Risk | Who may |
|:-|:-|:-|:-|:-|
| `ipd.charge_add` | "add 2 injections at 150 each for bed 5", "bed 5 x-ray charge 800", "charge bed 5 ₹1200 for physiotherapy" | `POST /admin/ipd/admissions/{id}/charges` | **R3** (type ₹ total) | IPD_BILLING |
| `ipd.charge_void` | "remove the dressing charge from bed 5 because it was entered twice" | `POST /admin/ipd/charges/{id}/void` | **R3** | IPD_BILLING |
| `ipd.charges_post` | "post the charges for bed 5" | `POST /admin/ipd/admissions/{id}/charges/post` | **R3** (type the total posted) | IPD_BILLING |
| `ipd.final_bill` | "make the final bill for bed 7" | `POST /admin/ipd/admissions/{id}/final-bill` | R2 | IPD_BILLING |
| `ipd_finance.deposit_refund` | "refund 2000 deposit to bed 5 in cash because …", "return the advance of 5000 to bed 7 by UPI UTR … as …" | `POST /admin/ipd/admissions/{id}/deposits/refund` | **R3** | IPD_ADMIN |
| `ipd.assign_bed` | "give bed 12 to Sita", "put Ravi Kumar in bed 12" | `POST /admin/ipd/admissions/{id}/assign-bed` | R2 | IPD_ADMISSIONS |
| `ipd.admission_cancel` | "cancel Sita's admission because she went home" | `POST /admin/ipd/admissions/{id}/cancel` | R2 | IPD_ADMISSIONS |
| `ipd.package_remove` | "take bed 5 off the package because the surgery was not done" | `POST /admin/ipd/admissions/{id}/package/remove` | R2 | IPD_BILLING |
| `opd.shift_open` | "open my cash counter with 2000 float", "open my shift" | `POST /admin/opd/shifts/open` | R2 | OPD_BILLING / OPD_ADMIN |
| `opd.shift_close` | "close my shift, cash counted 15400" | `POST /admin/opd/shifts/{id}/close` | **R3** (type the counted cash) | own shift only |

## 2. Money safety (all tested)

* **R3 for every money move**: the preview shows the amount, and Confirm needs the rupees typed; a wrong number is
  refused and nothing is sent.
* **Never twice**: each charge carries a `source_ref` and each refund an `idempotency_key` derived from the stay and
  what was read, so a retried confirm returns the first row (server unique constraints). Saying the same charge
  twice on purpose makes two charges, each with its own key.
* **Refused before anything is sent**, mirroring the database rules (`migrations/111`, `117`, `124`):
  a charge already on the final bill (→ credit note), a refund above the unused deposit balance, a UPI / bank
  refund without its UTR, a card refund, a final bill before the discharge starts or while charges wait to be
  posted, a second final bill, a bed that is not free, cancelling a patient already in a bed, a second open shift,
  closing with no open shift.
* **Charges are added as proposed** (as the page does) and count on the bill once billing posts them.
* **The close preview shows expected vs counted cash** and the difference (over / short) before you confirm.
* A refund that would leave the stay owing more is warned; cash refunds and deposits go through the open drawer.

## 3. Router changes found on the way

* A stated **reason is content, never the task**: "take bed 5 off the package because the surgery was not done"
  tied with *finish surgery*, "… as the surgery was cancelled" with *cancel surgery*. The reason clause
  (because / since / due to / as she / as the …) is set aside when choosing the action (`rules._REASON_CLAUSE`).
* "as she / he / they / it / we / patient …" is read as a reason (it was only "as the …").
* Neighbours give way: OPD "make the bill" never takes an IPD final bill; admit, package and bed-status actions
  never take a cancel, a package removal or a bed assignment.

## 4. A defect caught by the regression run before commit

`_SHIFTS` (the cash drawer's path) was a new top-level name that already existed in `playbooks.py` (a doctor's
morning / evening shift fields): the second definition replaced the first and "change Dr …'s hours" / slot length
previews failed (6 tests). Renamed to `_CASH_SHIFTS`; the same check found `_BILL` repeated in `registry.py`
(harmless there, renamed `_IPD_BILL`). `test_no_top_level_name_is_defined_twice` now fails on any such repeat in
the assistant's playbooks, registry, resolvers and rules.

## 5. Still with a person (unchanged)

Clinical inpatient work (orders, notes, medicine administration, observation corrections, discharge summaries and
every signature), insurance policy entry and disallowance resolution, payment-exception resolution, credit
revocation and discharge reversal: they need a clinician's or an admin's judgement on the page.

## 6. Verification

| Check | Result |
|:-|:-|
| `tests/test_assistant_money_ipd.py` (new, 40) | every action end to end (preview, nothing before Confirm, wrong typed amount refused, exact body, read-back) and every refusal above |
| Accuracy gate | DEV 100 % · DEV2 93.9 % · DEV3 100 % · DEV4 100 % · DEV5 (new) 100 % · HELDOUT 96.2 % · **0 wrong on every set** |
| All assistant tests + AGENTS.md gates + UI tests | first run: 6 failures, all one cause (§4), fixed; **final run on the committed code: 3,897 passed, 0 failed** |
| Fixture corpus (994) vs the previous commit | unchanged (17 misses before and after) |
| Every sentence in the assistant tests (2,258) vs the previous commit | 33 changed, all reviewed; 32 correct (several fix old wrong routes: "return the advance … as the surgery was cancelled" used to CANCEL THE SURGERY; "make the final bill for bed 5" made an OPD bill); 1 regression ("turn off auto open shift" opened a drawer) fixed and pinned by a test |
| flake8 (non-E501) on changed files vs the previous commit | no new issues |
