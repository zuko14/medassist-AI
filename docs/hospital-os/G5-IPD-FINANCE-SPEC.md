# G5 — IPD Finance: SPEC

> Brief: [GAP_ANALYSIS_AND_ROADMAP.md §5 G5](GAP_ANALYSIS_AND_ROADMAP.md#g5--ipd-finance). Status: [status.md](status.md).
> Migration `116_ipd_finance.sql`. Production rules: gap analysis §6 (all apply).

## 1. Scope

| Capability | What the hospital gets |
|:-|:-|
| **Deposits / advances** | Take money against a stay (cash, UPI, card, bank transfer) into the cashier's shift, numbered `DEP-YYYY-NNNNN`. A refundable balance per stay. Applied to the final bill once, when it is issued. Leftover refunded through the cashier shift (`DRF-` numbers). |
| **Running bill** | Live statement: posted charges, package cover, approved discount, deposits, net payable, proposed charges still awaiting review. |
| **Interim bill** | A printable *statement* of the running bill at a point in time, used to ask for a top-up deposit or to send to a payer. It is not a tax invoice and does not touch the ledger: the stay keeps **one** final bill (`uq_opd_invoices_one_per_admission`). |
| **Packages** | Package master (price, included charge categories, max days). Applied to a stay before its bill: one package charge; charges in included categories within the package days are covered (shown as one ₹0 "covered by package" line); excluded categories and days beyond the package are billed normally. |
| **Discount approval** | Billing staff request a discount with a reason; it is approved automatically within the requester's limit, otherwise an approver decides. Requester ≠ approver for non-automatic approvals (database rule). The final bill carries exactly the approved amount. |
| **Credit / corporate** | An administrator records a credit authorisation (payer, reference, amount covered). Discharge then needs no settlement waiver for the covered balance; the bill stays open as a receivable and is settled later by receipts (bank transfer added as a mode). Receivables page with ageing. |

## 2. Owner decisions taken as settings (gap analysis §9 items 5–6)

| Setting (`clinics.ipd_settings`) | Default | Meaning |
|:-|:-|:-|
| `discount_auto_approve_paise` | `0` | Billing staff (IPD_BILLING) may grant up to this without approval. Default: every staff discount needs approval. |
| `discount_approver_limit_paise` | `null` | Maximum an IPD_ADMIN may approve (`null` = no cap). Clinic admins are never capped. |
| Package overrun | rule, not setting | Included-category charges dated after the package's last day are billed normally; there is no partial-day proration. |

## 3. Invariants (database-enforced)

1. **Deposit ledger is append-only.** Balance = deposits − refunds − applied + unapplied, never negative (checked under the admission row lock, so two concurrent refunds cannot overdraw).
2. **Counter money needs the receiver's open shift**; cash deposits and cash deposit refunds count in the shift's expected cash (`opd_close_shift`).
3. **Applying a deposit is one transaction**: an `opd_receipts` row (mode `deposit`, no shift) and an `applied` ledger row; only via `ipd_apply_deposits`. Voiding an IPD bill first returns applied deposits to the stay's balance (`ipd_unapply_deposits`), so a bill is never voided with money on it.
4. **Final bill = posted charges − package cover − approved discount.** Lines come only from `ipd_bill_lines()`, which the running bill uses too, so preview and bill cannot disagree.
5. **Discount**: one live (requested/approved) per stay; content immutable; an IPD bill cannot be issued while a discount is pending or with a discount different from the approved one.
6. **Package**: one active per stay; applied/removed only while the stay has no live bill.
7. **Credit**: one active authorisation per stay; discharge treats the bill as settled only if the outstanding balance ≤ the authorised amount.
8. **Discharge checklist** adds: no pending discount; deposit balance zero (applied or refunded).

## 4. Permissions

| Action | Grant |
|:-|:-|
| Running bill, deposits (take), apply deposits, package apply/remove, discount request, receivables | `IPD_BILLING` |
| Deposit refund, discount approve/reject, credit authorisation, package master | `IPD_ADMIN` (clinic admin always) |

Branch-pinned staff act only on their branch's stays (existing `_admission` guard).

## 5. Out of scope (later modules)

GST on bills and credit notes (G4); payer tariffs, pre-auth and claims (G6); gateway refunds (G15).
