# Kriya Pharmacy OS — Traced Workflows

> Part of the Pharmacy OS set: start at [status.md](status.md). Each workflow: actors → preconditions → steps (UI →
> route → SQL function → rows written) → failure branches → acceptance test. Routes: [API.md](API.md). Tables:
> [DATA-MODEL.md](DATA-MODEL.md). All dates IST.

---

## W1 — Supplier PO → GRN inward → batch labelling
**Actors:** Store keeper (`RX_PURCHASE`), Pharmacy manager (approves PO). **Store:** central (or a counter with
`can_receive_grn`).
1. Reorder list `GET /admin/rx/reorder?store_id=` → items where `rx_v_item_stock.qty < reorder_level_units`, grouped by
   last supplier (from the latest posted GRN line of that item).
2. Manager creates PO `POST /admin/rx/purchase-orders` (draft lines in packs + free packs, expected rate) → `POST
   …/{id}/send` assigns `PO-2026-00042` (`opd_next_counter 'rx_po'`), status `sent`; PDF/WhatsApp-to-supplier is a
   **manual share** (download) — Kriya does not message suppliers in v1.
3. Goods arrive. Store keeper opens **GRN** → picks supplier + PO (optional), enters supplier invoice no./date →
   `POST /admin/rx/grns` (draft). `is_interstate` defaults from supplier `state_code` ≠ clinic GSTIN state.
4. Per line: scan barcode (resolves item) or search; enter batch, expiry (MM/YYYY → last day of month, or DD/MM/YYYY exact), packs, free
   packs, rate, scheme %, cash %, GST rate (prefilled from item), MRP. Trigger `rx_grn_line_calc` computes taxable,
   tax, landed unit cost. Warnings: expiry < `near_expiry_days`, rate > MRP, MRP differs from last batch (DPCO check),
   GST rate differs from item master (store keeper chooses which is right; item change goes to `rx_item_changes`).
5. Footer shows computed total vs supplier's printed total; mismatch > ₹0.99 blocks posting with the per-line diff.
6. `POST /admin/rx/grns/{id}/post` → `rx_post_grn`: `GRN-` number; batches upserted by (item, batch, expiry, MRP);
   `grn_in` movements; PO line `received_packs`; PO status `partially_received|received`; register `in` entries for
   H1/X/narcotic items with supplier snapshot.
7. **Labels.** `GET /admin/rx/grns/{id}/labels.pdf` — 38×25 mm shelf/strip labels (item, batch, expiry, MRP, internal
   barcode `RX<batch short id>` when the pack has no GS1 code). Labels only print; no state change.
**Failure branches:** same supplier invoice twice → 409 `rx_grn_duplicate_invoice`; expired batch → 422
`rx_grn_expired_batch`; void after any unit sold → 409 `rx_grn_stock_consumed` (use supplier return).
**Acceptance:** `test_rx_purchase.py::test_po_to_grn_to_stock`, `::test_free_packs_dilute_cost`,
`::test_interstate_igst`, `::test_total_mismatch_blocks_post`, `::test_duplicate_supplier_invoice`.

---

## W2 — OPD prescription auto-fulfilment at POS
**Actors:** Pharmacist (`RX_DISPENSE`). **Precondition:** doctor signed the Rx in OPD (103); counter shift open.
1. Patient arrives with token slip / phone. Pharmacist presses **F2**, types token no., MRN, phone, or scans the QR on
   the printed Rx (encodes `opd_prescriptions.id`). `GET /admin/rx/pos/lookup?q=` returns signed, non-superseded Rx of
   the last `rx_settings.rx_validity_days` (default 30) with fulfilment state per line.
2. Select Rx → `POST /admin/rx/sales` `{store_id, opd_prescription_id}` creates a draft (unique per Rx×store while
   draft — a second counter gets the existing draft). Server maps each Rx line (ADR-RX-07): alias → trigram; computes
   quantity where frequency ∈ closed map and `duration_days` set: `units = per_dose × freq × duration`, rounded **up**
   to whole packs only if `allow_loose = false`; FEFO picks batch(es).
3. Screen shows each line: Rx text ↔ mapped item, batch chip (expiry colour), qty, price. Unmapped / ambiguous lines
   are amber and must be confirmed (Enter on the line). Out of stock lines show same-generic alternatives (if any)
   and "Mark pending".
4. Schedule checks (client hint, server enforces): H/H1/X lines need the Rx's `signer_snapshot.registration_number`
   (present for every signed OPD Rx, AUDIT A2); X/narcotic lines need `RX_CONTROLLED`.
5. **F4** discount (≤ `max_discount_bps` unless `RX_ADMIN`), payment mode keys (**F6** cash, **F7** UPI, **F8** card,
   split allowed), **Enter** → `POST /admin/rx/sales/{id}/complete` `{payments, shift_id, idempotency_key}` →
   `rx_complete_sale` (DATA-MODEL §5). Receipt (80 mm or A5) prints; optional WhatsApp e-bill to the patient's
   registered number (consented channel, utility template).
6. `rx_rx_fulfilments` rows mark lines done; partial lines remain open with balance printed. A second fill of the same
   Rx (any counter, any store) is capped at the prescribed units under a per-Rx lock — hard for H1/X/narcotic,
   override-only for others (loophole X1).
7. If chronic items were sold, `rx_refill_plans` rows are created (W10) when `refill_enabled`.
**Failure branches:** Rx superseded between lookup and complete → 409 `rx_prescription_superseded` (reload);
stock taken by another counter → 409 with line; expired batch picked by stale screen → trigger 409
`rx_batch_expired`; shift closed → 409 `rx_shift_not_open`.
**Acceptance:** `test_rx_pos.py::test_signed_rx_loads_and_completes`, `::test_superseded_rx_refused`,
`::test_quantity_from_closed_frequency_map_only`, `::test_second_counter_gets_same_draft`,
`::test_complete_is_idempotent`; UI timing script `tests/ui/rx_pos_timing.md` (3-line Rx ≤ 3 s, recorded in status §9).

---

## W3 — Walk-in OTC sale
1. **F2** → item search / barcode scan with no patient. `POST /admin/rx/sales {store_id, sale_kind:'otc'}`.
2. Lines limited to `schedule IN ('none','G')` unless a prescription is captured: for H/H1 the pharmacist must enter
   an **external Rx** (prescriber name + registration no. + date, patient name) → stored in `prescriber_snapshot`;
   X/narcotic never sold OTC (server 422 `rx_controlled_requires_inhouse_rx`).
3. Patient optional: phone → existing patient match (`patient_match.py`) or walk-in name.
4. Complete as W2 step 5.
**Acceptance:** `test_rx_pos.py::test_otc_blocks_h1_without_external_rx`, `::test_otc_x_always_blocked`,
`::test_walkin_sale_without_patient`.

---

## W4 — IPD nursing indent → pharmacy dispense → IPD charge → MAR administration
**Actors:** Doctor (orders, existing), Ward nurse (`IPD_NURSING`), Pharmacist (`RX_DISPENSE`).
1. Doctor writes a medication order on the IPD chart (existing `POST /admin/ipd/admissions/{id}/orders`, 111).
2. Nurse opens **Ward Indent** for the admission: active medication orders listed (`GET
   /admin/rx/ipd/admissions/{id}/indentable`), each with last dispensed qty and ward sub-store stock. Nurse picks
   orders, enters units (default: 24 h need from the closed frequency map, editable), optional item pick → `POST
   /admin/rx/ipd/indents` (`IND-` number, priority). Trigger checks order active + medication + same admission.
3. Pharmacy **Indent Queue** (`GET /admin/rx/ipd/indents?status=raised`) sorted stat → urgent → routine, then age.
   STAT indents ring (UI sound + Ketu notification).
4. Pharmacist opens indent: maps lines (same mapping as W2), FEFO batch picks, may substitute same generic (reason),
   marks unavailable lines → `POST /admin/rx/ipd/indents/{id}/dispense` `{store_id, lines, idempotency_key}` →
   `rx_dispense_indent`: `ipd_dispense_out` movements, `rx_dispense_lines`, `ipd_charges` (pharmacy, posted per
   `ipd_autopost`, `source_ref rx:dl:<id>`), register rows, indent status. Issue slip prints (A5) with bed, patient,
   items, batches.
5. Runner delivers; nurse acknowledges **Received** (`POST /admin/rx/ipd/dispenses/{id}/receive`) → indent `closed`
   when all lines done.
6. Nurse gives the dose on the **existing MAR** (`POST /admin/ipd/admissions/{id}/administrations`). When RX is on,
   the form shows an optional "Batch" select of this admission's dispense lines for that order. The route pops
   `rx_dispense_line_id` before the unchanged MAR insert, then writes `rx_mar_links` (a failed link never fails the
   dose record).
7. Billing sees the pharmacy lines on the running bill (`ipd_running_bill`, 117:492) immediately. If the admission's
   package covers `pharmacy` (116:340), the lines show as covered — existing package behaviour; the issue slip says
   "Covered by package". If a final bill already exists, the slip says "Final bill generated — regenerate before
   discharge" (the discharge checklist already blocks unbilled posted charges, 117:572).
**Ward sub-store variant:** nurse issues from ward stock: `POST /admin/rx/ipd/ward-issue` (permission
`IPD_NURSING` + store scope = their ward) → `rx_ward_issue` (same charge path, `ward_issue_out`).
**Failure branches:** order discontinued after indent → open lines auto-cancelled (trigger); dispense > requested →
CHECK 409; admission discharged → 409 `rx_admission_not_active`; retry → same charge (unique `source_ref`).
**Acceptance:** `test_rx_ipd.py` (ADR-RX-04 list) + `::test_mar_batch_tag_optional_and_validated`.

---

## W5 — Patient medication return & credit note
**W5a OPD return.** Pharmacist opens sale (scan bill barcode `PHS-…`) → **Return** → selects lines and units ≤
`qty − returned`, reason, restock yes/no (opened strips, cold-chain items, or controlled drugs → restock forced **no**
unless `RX_ADMIN`) → `POST /admin/rx/sales/{id}/returns` → `rx_record_return`: `sale_return_in` movements for restock
lines (to the original batch and store — even if now near-expiry; if **expired**, the return is accepted and
immediately written off with `expiry_writeoff_out`), `damage_writeoff_out` for non-restock, `PRT-` number; refund in
the same tx as a negative `rx_sale_payments` row (in the actor's open shift for cash) or a `PCN-` credit note.
Refund value = **sold** net per unit (after the line's share of the header discount) × units, half-up — never today's
MRP (loophole X4). Refund tender follows the original tender: cash back only up to the cash paid; UPI/card refunds
are recorded against the original reference; any other split needs an `rx_overrides` approval (X3).
Tax reversed proportionally (same inclusive split on the returned value). Register `return` entries for scheduled
items. Return window `rx_settings.return_days` (default 7); older needs `RX_ADMIN`.
**W5b IPD return.** Nurse returns unused units → `POST /admin/rx/ipd/returns` → pharmacist verifies physically →
`…/{id}/accept` → `rx_ipd_return`: `ipd_return_in`; if the charge is unbilled: `ipd_void_charge` (reason "Returned
n units") then insert a new charge for the kept units (`source_ref rx:dl:<id>:r<k>`); if billed (final bill issued):
`opd_issue_credit_note` on the IPD invoice for the returned value (117:292). Never edits a charge.
**Acceptance:** `test_rx_returns.py::test_partial_return_restock_and_refund`, `::test_opened_strip_writeoff`,
`::test_return_after_window_needs_admin`, `::test_ipd_return_unbilled_void_reissue`,
`::test_ipd_return_billed_credit_note`, `::test_return_never_exceeds_sold`.

---

## W6 — Supplier return (expiry / damage)
1. **Near-expiry list** `GET /admin/rx/expiry?within_days=90&store_id=` → batches with stock, grouped by supplier of
   first GRN.
2. Store keeper selects batches/units → `POST /admin/rx/supplier-returns` (draft, debit note no. `SRN-`) → **Dispatch**
   → `rx_supplier_return`: `supplier_return_out` (allowed on expired/blocked batches), register `out` entries with
   supplier snapshot for controlled drugs. Debit note PDF with GST reversal at GRN rate (for the accountant).
3. Supplier credit is recorded later as a reference on the return (`credit_received_paise`, `credit_ref`) — no
   accounts-payable ledger in v1 (GAP G16).
**Acceptance:** `test_rx_purchase.py::test_supplier_return_of_expired_batch`, `::test_debit_note_tax`.

---

## W7 — Inter-store transfer (central → ward sub-store)
1. Ward sub-store reorder or nurse request creates a draft transfer (`POST /admin/rx/transfers` from=central,
   to=ward store); store keeper adds batches (FEFO) → **Dispatch** (`…/{id}/dispatch`) → `rx_dispatch_transfer`:
   `transfer_out` from central, `STR-` number, status `dispatched`, challan PDF.
2. Ward nurse (store scope = their ward) opens **Incoming** → counts → **Receive** with `received_units` per line →
   `rx_receive_transfer`: `transfer_in` to ward store; short lines → `damage_writeoff_out` against central with the
   discrepancy reason; status `received|partially_received`.
3. Controlled drugs: both dispatch and receipt need `RX_CONTROLLED` (two different users — `received_by ≠
   dispatched_by` CHECK for X/narcotic lines); register `out` (central) and `in` (ward) entries.
**Acceptance:** `test_rx_transfers.py` (ADR-RX-03) + `::test_controlled_transfer_two_person`.

---

## W8 — Physical stock audit & variance reconciliation
1. Manager starts audit `POST /admin/rx/audits {store_id, scope, freeze_sales}` → snapshot `system_units_at_start` per
   batch with stock (and zero-stock batches of in-scope items, to catch found stock).
2. Counters count on mobile (390 px): scan batch barcode or pick, enter units. Sales continue unless frozen; sale
   movements during the count accumulate in `movement_delta_since_start` (trigger on movements while an audit of that
   store is `counting`).
3. **Review**: variance list by value; each non-zero variance needs a reason (`breakage`, `theft_suspected`,
   `billing_error`, `found`, `expiry_missed`, `other`).
4. **Post** `…/{id}/post` → `rx_post_audit`: `adjust_in/out` movements; above-threshold variance requires a second
   `RX_ADMIN` approver; controlled-scope audits write register `adjust` entries.
**Acceptance:** `test_rx_audit.py::test_variance_with_concurrent_sales`, `::test_reason_required`,
`::test_second_approver_threshold`, `::test_found_stock_adjust_in`.

---

## W9 — Schedule H1 & narcotic inspection export
1. Drug Inspector visits. Pharmacist (`RX_READ` + `RX_REGISTER_EXPORT`, granted to PHARMACIST/PHARMACY_MANAGER)
   opens **Registers** → register (H, H1, X, narcotic) → date range → store.
2. `GET /admin/rx/registers/{register}?from&to&store_id` (paged) and `…/export.pdf` / `…/export.csv`.
   PDF: clinic name, address, drug licence no. (from store), register name, period, page "n of N", columns per
   register (H1: date, bill no., patient name & address, prescriber name & reg. no., drug, batch, qty, pharmacist);
   narcotic: running balance, receipts and issues per item. Footer: chain-verification result and generation time.
3. Export is audit-logged (`rx_register_export`, who, range). Exports never include phone numbers.
4. `GET /admin/rx/registers/{register}/verify` shows chain status; a break is shown in red with the first bad seq.
**Acceptance:** `test_rx_registers.py::test_export_pdf_columns_h1`, `::test_export_csv_matches_rows`,
`::test_export_audited`, `::test_narcotic_running_balance`.

---

## W10 — Automated WhatsApp chronic refill & delivery flow
**Preconditions:** `rx_settings.refill_enabled`; clinic has its own WhatsApp credentials (AGENTS.md §1.6); utility
template `rx_refill_due_v1` (two quick-reply buttons) approved by Meta; the pharmacist ticked **"Patient wants refill
reminders"** at the counter (recorded `opt_in_by/at`); at send time `ConsentService.accepts_engagement()` is true (the
patient has not sent the existing global STOP — VERIFICATION A56).
1. On sale completion, for each opted-in line with `item.is_chronic` and schedule not H1/X/narcotic/psychotropic and a
   derivable `units_per_day` (DATA-MODEL §9): `covered_until = sale_date + floor(units / units_per_day)`;
   `remind_on = covered_until − refill_lead_days`. Upsert `rx_refill_plans` (newer sale of the same item for the same
   patient supersedes the older plan → `cancelled`).
2. Job `rx_refill_reminders` daily 09:30 IST (CAS-locked): plans `scheduled` with `remind_on ≤ today`, clinic
   ACTIVE, `accepts_engagement` true, no send in the last 3 days → send template:
   *"{clinic}: Your {brand} supply from {date} is likely to run out around {covered_until}."* with buttons
   **[Request refill]** (payload `rx_refill:<plan_id>`) and **[Stop refill reminders]** (payload
   `rx_refill_off:<plan_id>`). No dose text, no clinical advice. Status `sent`, `last_message_id` stored (delivery
   tracking only); masked phone in logs. 3 consecutive send failures → `expired`.
3. A button tap arrives as `message_type="button"` with our payload (webhook.py:299–306). In `conversation.py`, next
   to the dental-review prefix hook (680–689), `rx_refills.handle_button(clinic, phone, payload)` claims **only** the
   two `rx_refill` prefixes: the plan must belong to this clinic **and** this sender's phone (else ignored and logged —
   loophole X17). *Request refill* → `rx_refill_requests` (`new`) + fixed reply "Request received — the pharmacy will
   confirm."; *Stop refill reminders* → that patient's plans `opted_out`. Typed text ("refill", "STOP") is **not**
   intercepted: it flows through the existing FSM, where STOP is the global opt-out that already silences refills.
   Runs in the existing async worker after the webhook has persisted the message (AGENTS.md §1.4).
4. Pharmacist sees **Refill Requests** queue: verifies the original Rx still valid (`rx_validity_days`, or doctor
   repeat instruction) → **Verify** → **Pack** creates a draft sale from the plan's item (FEFO) → patient notified
   "ready for pickup" → **Complete** at counter (W2 step 5). Invalid Rx → **Reject** with reason → patient told
   "Please consult your doctor for a new prescription" (fixed text) and offered the clinic's booking link.
5. Home delivery (`fulfilment='home_delivery'`) only when owner decision O-6 enables it; payment link reuses the
   clinic's gateway (`payment.py`), delivery status is manual.
**Never:** auto-dispense, change quantity, suggest dosage, or remind for controlled schedules.
**Acceptance:** `test_rx_refills.py::test_plan_from_closed_frequency_only`, `::test_no_plan_for_h1_x`,
`::test_job_respects_consent_and_optout`, `::test_refill_reply_creates_request`, `::test_stop_opts_out`,
`::test_job_skips_off_clinics`, `::test_no_llm_on_reply_path`.
