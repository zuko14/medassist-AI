# Kriya Hospital OS — Execution Status (source of truth)

> Read with: [GAP_ANALYSIS_AND_ROADMAP.md](GAP_ANALYSIS_AND_ROADMAP.md) (what to build and the production bar, §6).
> States: `NOT_STARTED` · `IN_PROGRESS` · `IMPLEMENTED` (code written, evidence incomplete) ·
> `VERIFIED` (the listed command was run and passed — result pasted) · `DEFERRED` (reason given).
> Rule: never mark VERIFIED without pasting the command result. Update this file in the same change as the work.

## Module board

| ID | Module | State | Session |
|:-|:-|:-|:-|
| G1 | Investigation & procedure orders → bill → diagnostic worklist | **VERIFIED** (local real-Postgres; staging apply pending) | [41](../sessions/SESSION_41_G1_INVESTIGATION_AND_PROCEDURE_ORDERS.md) |
| G5 | IPD finance: deposits, running/interim bill, packages, discount approval, credit/corporate | **VERIFIED** (local real-Postgres; staging apply pending) | [42](../sessions/SESSION_42_G5_IPD_FINANCE.md) |
| G8 | Operation theatre: theatres, booking (no double booking), WHO checklist, anaesthesia record, post-op note, auto-posted charge | **VERIFIED** (local real-Postgres; staging apply pending) | [48](../sessions/SESSION_48_G8_OPERATION_THEATRE.md) |
| G13 | Radiology: imaging worklist, reports (draft → signed, amendments), PACS link, letterhead PDF to WhatsApp, statutory PCPNDT Form F, fetal-sex firewall | **VERIFIED** (local real-Postgres; staging apply pending) | [49](../sessions/SESSION_49_G13_RADIOLOGY_AND_PCPNDT.md) |
| G6 | Insurance / TPA: payers, policies, cashless pre-auth (query / enhancement), dual-debtor claim on the GST bill, NEFT / TDS / disallowance settlement, write-off via credit note, ageing | **VERIFIED** (local real-Postgres; staging apply pending) | [50](../sessions/SESSION_50_G6_INSURANCE_TPA.md) |
| G2–G4, G6, G7, G9–G18 | See gap analysis §4 | NOT_STARTED | — |

**Pre-production re-audit (2026-10-30, [Session 51](../sessions/SESSION_51_PRE_PRODUCTION_REAUDIT.md))**: 1 high
(role-implied grants bypassed the staff grant cap) and 1 GST defect (OT override dropped the service's tax) fixed;
Supabase grants / RLS verified on all 28 new tables. `pytest tests/` → **7672 passed, 4 skipped, 0 failed**.
Deploy order: apply 115–124 to Supabase **before** merging to `main`.

---

## G1 — Investigation & procedure orders

### What exists

| Layer | Item |
|:-|:-|
| Migration | `migrations/115_investigation_and_procedure_orders.sql` · rollback `migrations/rollback/115_investigation_and_procedure_orders.down.sql` |
| Table | `opd_orders`: one row per ordered line = worklist row = bill source. `source` `opd` (encounter + appointment) or `ipd` (mirrored `ipd_orders` row). Composite tenant FKs to encounter, appointment, admission, IPD order, patient, family member, doctor, branch. |
| Bill link | `opd_invoice_items.order_id` (composite FK). The line points at the order, because the draft save replaces every line. |
| DB invariants | Content and price immutable after insert (`opd_guard_order`); workflow transitions only; `billed` ↔ `ordered` only via bill issue / void (`opd_invoice_orders_billing`); one live bill line per order, same patient / visit (`opd_guard_invoice_item_order`, row lock); specimen test cannot complete before collection; cancel refused on issued / paid bill; append-only (delete only in the owner's clinic purge). |
| RPCs | `opd_place_orders` (one transaction, idempotent per click: line *n* carries `<key>:<n>`; catalogue price + sample snapshotted in SQL), `opd_bill_orders` (visit draft, or a supplementary draft once the visit bill is issued; appointment row lock), `opd_cancel_order` (removes a draft line in the same transaction), `opd_save_invoice_draft` (114, now carries `order_id`). |
| IPD | Trigger on `ipd_orders`: investigation / procedure → worklist row + **proposed** `ipd_charges` row (`source='order'`, `source_ref='order:<id>'`) when the lab test has a price. Discontinue → worklist row cancelled, charge voided unless the sample was taken or it is on the bill. Worklist completion → ward order completed. |
| Service | `app/services/orders.py` (zero-LLM; batched `in_()` by 100 via `fetch_all`; mapped `opd_*` error codes, no raw DB text). |
| Routes | `app/routers/orders.py`: `POST/GET /admin/opd/encounters/{id}/orders`, `GET /admin/opd/orders/catalog`, `GET /admin/opd/appointments/{id}/orders`, `POST /admin/opd/appointments/{id}/orders/bill`, `DELETE /admin/opd/orders/{id}`, `GET /admin/diagnostic/worklist`, `POST /admin/diagnostic/orders/{id}/collect`, `POST /admin/diagnostic/orders/{id}/complete`. |
| Permissions | `ORDERS_READ`, `ORDERS_WRITE`, `ORDERS_COLLECT`, `ORDERS_CANCEL` (`app/services/permissions.py`). Pre-115 module grants imply the matching order grant (no account needs re-saving). A `PHLEBOTOMIST` login gets READ + COLLECT from its role and the two worklist routes are on its allowlist. Placing an order needs the **treating doctor's own login**. |
| Billing integration | Visit bill opened after the consult picks up pending orders; non-admin cashiers cannot re-price an ordered line (order snapshot wins); ad-hoc invoice with a refused order line is rolled back. |
| UI (`admin/index.html`) | Consultation: "Investigation & Procedure Orders" card (catalogue typeahead, custom items, urgency, place / cancel; signing places pending orders). OPD Billing: "Pending Orders from Consult (N items)" banner + Add All / Create Bill for Orders; ordered lines locked. New page **Diagnostic Worklist** (STAT first, token / bed, barcode, collect, record result); stacks by card width (container queries). Staff permission pickers offer the four grants. |
| Kriya Ketu | `dxworklist` page registered for navigation only; the assistant never names `opd_orders` (`tests/test_ipd_no_llm.py`). |

### Evidence (2026-10-28, local pgserver Postgres, Python 3.12)

| Check | Command | Result |
|:-|:-|:-|
| Tenant-isolation linter | `pytest tests/test_lint_unscoped_queries.py` | **4 passed** |
| Migration + orders E2E | `pytest tests/test_migration_115_orders.py tests/test_orders_api_e2e.py` | **15 passed** |
| Scope matrices | `pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py` | **850 passed** |
| Zero-LLM + firewall | `pytest tests/test_clinical_firewall.py tests/test_ipd_no_llm.py` | **34 passed** |
| OPD billing + IPD regression | `pytest tests/test_opd_billing_real_pg.py tests/test_ipd_api_e2e.py` | **21 passed** |
| Full suite | `pytest tests/` | **6876 passed, 4 skipped, 0 failed** |
| Concurrency | 10 parallel `opd_place_orders` with one key (`test_ten_concurrent_placements_with_one_key_insert_once`) | exactly 1 order, 1 non-replayed response |
| UI | Static page + stubbed API, real render code; widths 1920 / 1440 / 375 | No page overflow on the new worklist / consult card / banner; collect and place request bodies match the server models |

### Open items

| # | Item | State |
|:-|:-|:-|
| G1-1 | Apply 115 on a Supabase **staging** branch, then run the E2E with `TEST_DATABASE_URL` | NOT_STARTED — do before production |
| G1-2 | Lab-operator / diagnostic-operator accounts created before 115 have no `ORDERS_*` grant (their module grants are report grants, not implied). Re-save those staff accounts, or grant explicitly | Owner action |
| G1-3 | Clinic policy "collect only after payment": the worklist shows bill status, but collection is not blocked when unpaid | DEFERRED — owner decision |
| G1-4 | IPD procedures without a lab-test link create no proposed charge (no service-catalogue code on `ipd_orders`); billing staff add the charge manually as today | DEFERRED → G12 service catalogue per payer |
| G1-5 | Result entry is a free-text summary; parameters, ranges and validation are G2 (LIS). `in_progress` / `resulted` statuses are reserved for it | DEFERRED → G2 |
| G1-6 | Pre-existing: the OPD Billing lines table (`#opdInvTable`) overflows the page by ~140 px at 375 px | Not part of G1; follow-up |
| G1-7 | Clinical reads of orders are not audit-logged (writes are), same as OPD encounter reads | Follow §6 rule 9 with the OPD read-audit work |

---

## G5 — IPD finance

Spec: [G5-IPD-FINANCE-SPEC.md](G5-IPD-FINANCE-SPEC.md). Migration `116_ipd_finance.sql` · rollback `rollback/116_down.sql`.

### What exists

| Layer | Item |
|:-|:-|
| Deposit ledger | `ipd_deposits` (append-only): deposit / refund / applied / unapplied; `DEP-` / `DRF-` gapless numbers; counter money tied to the receiver's open shift; balance checked under the admission row lock (never negative). Cash deposits and refunds count in `opd_close_shift` and the live shift view. |
| Bill integration | `ipd_apply_deposits` (on final-bill issue, automatic) writes an `opd_receipts` row in new mode `deposit` + an `applied` ledger row; `ipd_unapply_deposits` returns them before a void (only when deposits are the bill's only payments). Day collections report deposits separately and skip deposit-mode receipts (no double count). |
| Running / interim bill | `ipd_bill_lines()` is the single line rule (final bill and running bill); `ipd_running_bill()`; printable interim statement (not a tax invoice; one final bill per stay is unchanged). |
| Packages | `ipd_packages` master; `ipd_admission_packages` snapshot, one active per stay, applied/removed only before the bill; package charge posted; covered charges summed into one ₹0 line; days beyond `max_days` and excluded categories billed. |
| Discounts | `ipd_discounts`: one live per stay; auto-approved within the requester's limit (`discount_auto_approve_paise`, `discount_approver_limit_paise`; clinic admins uncapped); four-eyes CHECK; bill issue refused while pending or with a different amount (`ipd_guard_bill_issue`). |
| Credit / corporate | `ipd_credit_authorizations`: one active per stay; discharge checklist treats a balance ≤ the authorised amount as settled (no waiver); receipts in new mode `bank_transfer` (UTR required); receivables with ageing. |
| Discharge checklist | + `discount_decided`, + `deposits_cleared`; `bill_settled` honours credit. |
| API | `/admin/ipd/admissions/{id}/running-bill`, `…/deposits` (GET/POST), `…/deposits/refund`, `…/deposits/apply`, `/admin/ipd/packages` (GET/POST/PATCH), `…/package` + `…/package/remove`, `…/discounts`, `/admin/ipd/discounts/pending`, `/admin/ipd/discounts/{id}/decision`, `…/credit`, `/admin/ipd/credit/{id}/revoke`, `/admin/ipd/receivables`. |
| UI | Chart → Charges & Bill: running bill, interim statement, deposits (take / refund / apply), package, discount (request / approve / reject / cancel), credit. IPD Setup: discount authority, package master. New page **IPD Receivables** (ageing, outstanding bills, deposits held, discount approvals). |

### Evidence (2026-10-28, local pgserver Postgres, Python 3.12)

| Check | Command | Result |
|:-|:-|:-|
| Migration + finance E2E (3 consecutive runs) | `pytest tests/test_migration_116_ipd_finance.py tests/test_ipd_finance_api_e2e.py` | **12 passed** ×3 |
| IPD / OPD money regression | `pytest tests/test_ipd_api_e2e.py tests/test_migration_111_ipd.py tests/test_opd_billing_real_pg.py` | **56 passed** |
| Scope matrices | `pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py` | **898 passed** |
| Zero-LLM + firewall | `pytest tests/test_clinical_firewall.py tests/test_ipd_no_llm.py` | **34 passed** |
| Tenant linter | `pytest tests/test_lint_unscoped_queries.py` | **4 passed** |
| Full suite | `pytest tests/` | **6936 passed, 4 skipped, 0 failed** |
| Concurrency | two parallel refunds of the whole balance | exactly one paid out, the other `ipd_deposit_insufficient` |
| UI | static page + stubbed API, real render code; 1440 / 375 px | no page overflow (chart finance panel, receivables, setup); deposit / approval request bodies match the API models |

### Open items

| # | Item | State |
|:-|:-|:-|
| G5-1 | Apply 116 on a Supabase **staging** branch; run the finance E2E with `TEST_DATABASE_URL` | NOT_STARTED — before production |
| G5-2 | Owner confirmation of the discount defaults (staff 0, IPD admin uncapped) and the package overrun rule | Owner decision (gap analysis §9 items 5–6) |
| G5-3 | Package price or discount changes after the bill is issued need credit notes | DEFERRED → G4 |
| G5-4 | Payer tariffs, pre-auth, claims, settlement deductions | DEFERRED → G6 |
| G5-5 | Deposit refunds by gateway (Razorpay/PhonePe) | DEFERRED → G15 |

---

## G8 — Operation theatre

Migration `122_operation_theatre.sql` · rollback `rollback/122_down.sql`. Design, permissions, API and UI:
[SESSION 48](../sessions/SESSION_48_G8_OPERATION_THEATRE.md).

### Evidence (2026-10-29, local pgserver Postgres, Python 3.12)

| Check | Command | Result |
|:-|:-|:-|
| Migration + OT E2E | `pytest tests/test_migration_122_ot.py tests/test_ot_api_e2e.py` | **20 passed** |
| Linter, scope matrix, firewall, zero-LLM | `pytest tests/test_lint_unscoped_queries.py tests/test_admin_super_admin_scope_matrix.py tests/test_clinical_firewall.py tests/test_ipd_no_llm.py` | **731 passed** |
| Migration rollback chain | `pytest tests/test_migration_1*.py` | **153 passed** |
| Full suite | `pytest -q` | **7633 passed, 5 skipped, 0 failed** (after the discharge guard) |
| Concurrency | 10 parallel overlapping bookings; 10 parallel bookings; REPEATABLE READ race | exactly 1 booked; consecutive numbers; racer refused |
| UI | static page + stubbed API, then the real app on local Postgres, 390 px, light + dark | full flow works; 3 UI bugs found and fixed |

### Open items

| # | Item | State |
|:-|:-|:-|
| G8-1 | Apply 122 on a Supabase **staging** branch; rerun the OT E2E with `TEST_DATABASE_URL` | NOT_STARTED — before production |
| G8-2 | Overrunning surgery does not block the next booking (overlap uses scheduled times); confirmed by probe | DEFERRED |
| G8-3 | Discharge while a surgery is scheduled / in theatre / in recovery | **Fixed** — blocked in the database (`ipd_surgery_open`), owner decision 2026-10-29 |
| G8-4 | Implants / consumables from pharmacy stock | DEFERRED → G3 |
| G8-5 | Surgery consent is the G9 consent, not linked to the surgery row | DEFERRED |

---

## G13 — Radiology & PCPNDT Form F

Migration `123_radiology_and_pcpndt.sql` · rollback `rollback/123_down.sql`. Design, permissions, API and UI:
[SESSION 49](../sessions/SESSION_49_G13_RADIOLOGY_AND_PCPNDT.md).

### Evidence (2026-10-30, local pgserver Postgres, Python 3.12)

| Check | Command | Result |
|:-|:-|:-|
| Tenant linter | `pytest tests/test_lint_unscoped_queries.py` | **4 passed** |
| Migration + radiology E2E | `pytest tests/test_migration_123_radiology.py tests/test_radiology_api_e2e.py` | **59 passed** |
| Scope matrix + clinical firewall | `pytest tests/test_admin_super_admin_scope_matrix.py tests/test_clinical_firewall.py` | **741 passed** |
| Zero-LLM + migration rollback chain | `pytest tests/test_ipd_no_llm.py tests/test_migration_1*.py` | **196 passed** |
| Full suite | `pytest tests/` | **7600 passed, 4 skipped, 0 failed** |
| Concurrency | 11 parallel first saves (10 orders + 10 racers on one order) | consecutive numbers; one draft per order |
| PCPNDT | SQL rule and Python firewall on a shared 17-case corpus; API refuses before any write | identical; nothing saved |
| UI | real app on local Postgres: worklist, obstetric draft, firewall refusal, Form F, sign, PDF, nurse read-only, 390 px | full flow works; 4 UI issues found and fixed |

### Open items

| # | Item | State |
|:-|:-|:-|
| G13-1 | Apply 123 on a Supabase **staging** branch; rerun the radiology E2E with `TEST_DATABASE_URL` | NOT_STARTED — before production |
| G13-2 | PACS / modality worklist (DICOM MWL) integration; today a study UID + viewer link | DEFERRED |
| G13-3 | Form F Section C (invasive procedure details) and the monthly Form F return to the Appropriate Authority | DEFERRED |

---

## G6 — Insurance / TPA

Migration `124_insurance_tpa_and_cashless.sql` · rollback `rollback/124_down.sql`. Design, permissions, API and UI:
[SESSION 50](../sessions/SESSION_50_G6_INSURANCE_TPA.md).

### Evidence (2026-10-30, local pgserver Postgres, Python 3.12)

| Check | Command | Result |
|:-|:-|:-|
| Tenant linter | `pytest tests/test_lint_unscoped_queries.py` | **4 passed** |
| Migration + insurance E2E | `pytest tests/test_migration_124_insurance.py tests/test_insurance_api_e2e.py` | **16 passed** |
| Scope matrix + clinical firewall | `pytest tests/test_admin_super_admin_scope_matrix.py tests/test_clinical_firewall.py` | **775 passed** |
| Zero-LLM + migration rollback chain | `pytest tests/test_ipd_no_llm.py tests/test_migration_1*.py` | **208 passed** |
| Full suite | `pytest tests/` | **7667 passed, 4 skipped, 0 failed** |
| Money rules | ₹1,15,000 bill, ₹90,000 approved → payer ₹90,000 / patient ₹25,000; deposits and counter capped at the patient's share | exact; refused above |
| Concurrency | 10 parallel ₹10,000 settlements on a ₹90,000 claim | 9 recorded, 1 refused, outstanding 0 |
| UI | real app on local Postgres as TPA desk, cashier, admin: pipeline, stay view, cashless bill, settlement, write-off, ageing, chart card; 390 px | full flow works; 3 issues found and fixed |

### Open items

| # | Item | State |
|:-|:-|:-|
| G6-1 | Apply 124 on a Supabase **staging** branch; rerun the insurance E2E with `TEST_DATABASE_URL` | NOT_STARTED — before production |
| G6-2 | NHCX / TPA portal integration (decisions and settlements are keyed in) | DEFERRED |
| G6-3 | Approval-letter upload (path reference only today) | DEFERRED |
| G6-4 | Room-rent proportionate deduction computed from the policy's limit | DEFERRED |

