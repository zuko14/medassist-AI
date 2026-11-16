# Kriya Pharmacy OS — Advanced Staff Features

> Part of the Pharmacy OS set: start at [status.md](status.md). These features make daily work faster for pharmacists,
> store keepers, nurses and managers. Every one is: **deterministic** (no LLM, ADR-RX-06), **inside the RX gate**
> (nothing for clinics without pharmacy), **read-only on existing modules** unless stated, and has a task ID, tests and
> a phase. Schema additions are listed in DATA-MODEL §14.

## Summary
| ID | Feature | Who it helps | Phase / task |
|:-|:-|:-|:-|
| A1 | **Rx Inbox** — signed prescriptions arrive at the pharmacy live, pre-pick before the patient walks over | pharmacist | P4 / RX-69 |
| A2 | Park / recall bills (F3) — several open drafts per counter | pharmacist | P4 / RX-70 |
| A3 | GS1 DataMatrix / pharma QR scan — GTIN, batch, expiry, serial fill GRN and POS | store keeper, pharmacist | P3 / RX-71 |
| A4 | Dispensing label with the prescriber's instructions + fixed-vocabulary local language | patient, pharmacist | P4 / RX-72 |
| A5 | Repeat last bill (F11) for chronic patients | pharmacist | P4 / RX-73 |
| A6 | Batch recall trace — who got this batch (sales, wards, MAR) | manager | P7 / RX-74 |
| A7 | Consumption-based reorder (ADU, lead time, min/max) + ABC / VED / FSN + dead stock | manager, store keeper | P10 / RX-75 |
| A8 | Expiry-return planner per supplier return window | store keeper | P8 / RX-76 |
| A9 | Manager override at the counter (re-auth modal) | pharmacist, manager | P4 / RX-77 |
| A10 | Ward par levels → daily auto-draft replenishment + rack-sorted pick lists | nurse, store keeper | P6 / RX-78 |
| A11 | Narcotic empty-ampoule return control | custodian, nurse | P7 / RX-79 |
| A12 | Lost-sale & pending-balance call list | manager | P9 / RX-80 |
| A13 | Pharmacy shift Z-report | cashier, manager | P4 / RX-81 |
| A14 | Patient medicine history + recorded allergies at the counter (display only) | pharmacist | P4 / RX-82 |

---

## A1 — Rx Inbox (pre-pick)
- **What.** A panel on POS listing prescriptions signed today (IST) at this store's branch, newest first, with fulfilment state: *New*, *Picked*, *Billed*, *Partial*. A pharmacist clicks
  **Pick** → a draft sale is created (same as W2 step 2) and the FEFO pick list prints (rack-sorted). When the patient
  arrives, F2 → token → the draft is already there → Enter.
- **Data.** Read-only on `opd_prescriptions` (status `signed`, `signed_at` today) + `rx_rx_fulfilments`; draft
  `rx_sales.picked_at`, `picked_by`. Poll every 15 s (`GET /admin/rx/pos/inbox?store_id&since=`), no websocket.
- **Safety.** Picking does not reserve stock (ADR in ARCHITECTURE §5); completion re-checks everything.
- **Tests.** `test_rx_pos.py::test_inbox_lists_signed_today_only`, `::test_inbox_excludes_superseded`, `::test_pick_creates_single_draft`.

## A2 — Park / recall bills
- **What.** F3 parks the current draft with a short label ("Lakshmi — waiting for son"); F3 again shows parked drafts
  of this counter; Enter recalls. Drafts older than 24 h are listed in *Stale drafts* for discard (no auto-delete).
- **Data.** `rx_sales.parked_label`, `parked_at`. Unique-draft-per-Rx×store rule unchanged.
- **Tests.** `test_rx_pos.py::test_park_and_recall`, `::test_stale_drafts_list`.

## A3 — GS1 DataMatrix / pharmaceutical QR scan
- **Why.** Many Indian packs (including brands that must carry a QR code under the Drugs Rules' Schedule H2 rule)
  carry GS1 element strings. One scan can fill item, batch and expiry.
- **What.** Scanner input that starts with the GS1 FNC1/`]d2`/`]Q3` prefix or parentheses form is parsed
  deterministically: AI `(01)` GTIN-14 → item via `rx_item_barcodes`; `(10)` batch; `(17)` expiry `YYMMDD` (day `00` →
  month end); `(21)` serial (stored on GRN line, not required). At GRN: fills line; mismatched batch/expiry vs typed
  values → red. At POS: picks that exact batch (FEFO override reason auto-filled "scanned pack") — and if that batch is
  expired/blocked the line is refused before the server even sees it (server still enforces).
- **Code.** `app/services/rx_gs1.py` (pure parser, < 120 lines) + same parser in the UI block.
- **Tests.** `test_rx_gs1.py` vectors: GS-separated, parenthesised, variable-length batch with/without GS, `17` with day 00, unknown AIs ignored, malformed → plain barcode lookup.

## A4 — Dispensing label
- **What.** 50×25 mm label per line (or printed on A5): patient name, MRN, item, batch, expiry, quantity, prescriber,
  date, and the **prescriber's own words** (`dosage`, `frequency`, `timing`, `duration_days`, `instructions` copied
  verbatim from `opd_prescription_items`, AUDIT A4). A second line renders the closed vocabulary in the patient's
  language (`patients.language` en/hi/te, the column the WhatsApp bot already reads — `app/services/conversation.py:58–66`): `OD/BD/TDS/QID/HS/SOS` and the `timing` enum via a fixed table
  (e.g. `BD` → "दिन में 2 बार" / "రోజుకు 2 సార్లు"; `after_food` → "खाना खाने के बाद" / "భోజనం తర్వాత").
  Free-text instructions are **not** translated.
- **Safety.** No dose is computed or suggested; a line without an Rx (OTC) prints no dosage text.
- **Tests.** `test_rx_labels.py::test_verbatim_prescriber_text`, `::test_closed_vocab_translation_only`, `::test_otc_label_has_no_dose`.

## A5 — Repeat last bill
- **What.** F11 on a patient shows their last 5 sales; **Repeat** creates a draft with the same items and quantities,
  fresh FEFO batches and today's prices. Scheduled items (H/H1/X/narcotic) are added **only** if a valid Rx is linked
  (the original Rx within `rx_validity_days` with remaining units, or a new Rx) — otherwise shown greyed "needs Rx".
- **Tests.** `test_rx_pos.py::test_repeat_bill_fresh_fefo`, `::test_repeat_excludes_scheduled_without_rx`.

## A6 — Batch recall trace
- **What.** On **Block batch** (or from Stock → batch → *Trace*), a report of every unit of that batch: POS sales
  (bill, date, patient, phone masked, qty), IPD dispenses (admission, ward, bed), MAR administrations linked through
  `rx_mar_links`, transfers, on-hand per store. Export CSV/PDF. **Notify patients** (optional) sends the approved
  utility template `rx_recall_v1` only after a manager confirms the recipient list (manual approval; no auto-send).
- **Data.** Pure reads over `rx_stock_movements` by `batch_id` (indexed `idx_rx_sm_batch`).
- **Tests.** `test_rx_recall.py::test_trace_covers_sale_ipd_mar_transfer`, `::test_notify_requires_confirmation`.

## A7 — Consumption-based reorder and stock classification
- **ADU** (average daily usage) per item×store = out-movements (sale, dispense, ward issue) over the last 90 days ÷ 90.
- **Suggested reorder level** = ADU × (supplier `lead_time_days` + `safety_days` setting, default 7); **max** = level +
  ADU × `review_days` (default 15). Shown next to the manual level; **Apply** writes it (RX_ADMIN). Never auto-changes.
- **ABC** by 90-day consumption value (A = top 70%, B next 20%, C rest); **VED** (vital/essential/desirable) manual tag
  on the item; **FSN** (fast/slow/non-moving: >1 unit/day, >0, none in 90 days). **Dead stock**: on hand, no out
  movement in 180 days, with value.
- **Data.** `rx_items.ved_class`, `rx_suppliers.lead_time_days`; computed by a nightly materialisation into
  `rx_item_metrics(store_id, item_id, adu_x1000, abc, fsn, computed_on)` (replaced nightly, not append-only).
- **Tests.** `test_rx_metrics.py` with a fixed ledger fixture → exact ADU, classes, suggestions.

## A8 — Expiry-return planner
- Supplier field `return_window_days` (e.g. returns accepted up to 90 days before expiry). Monthly job (1st, 08:00 IST)
  drafts one supplier return per supplier for batches entering the window, as **drafts only** for the store keeper.
- **Tests.** `test_rx_purchase.py::test_planner_drafts_only_within_window`.

## A9 — Manager override at the counter
- A pharmacist hitting a limit (discount cap, return past window, FEFO override when the setting requires a manager,
  staff sale) gets **Request override**: a modal where a user holding `RX_ADMIN` (or PHARMACY_MANAGER) types their
  username + password (re-authentication through the existing `check_password_hash`, `app/routers/admin.py:204`, no session switch). The server issues a
  60-second single-use override token bound to (sale id, action, amount). Audit records both people.
- **Tests.** `test_rx_pos.py::test_override_token_single_use_bound`, `::test_override_wrong_user_rejected`.

## A10 — Ward par levels and pick lists
- `rx_par_levels(store_id, item_id, par_units)` for ward sub-stores and OT trays. Daily 06:00 IST job drafts a transfer
  from the parent store for `par − on_hand` (positive only), as a **draft** for the store keeper. Indent and transfer
  pick lists print sorted by `rack_location`, then item name.
- **Tests.** `test_rx_transfers.py::test_par_level_draft_only_positive`, `::test_pick_list_rack_order`.

## A11 — Narcotic empty-ampoule return control
- Setting `narcotic_empty_return` (default on for `schedule='narcotic'` injectables). When a ward has outstanding
  empties (dispensed − administered-and-returned − returned unused) above `narcotic_empty_tolerance` (default 0), new
  dispenses of that item to that ward need an `RX_CONTROLLED` override with reason. Empties are recorded in
  `rx_empty_returns(dispense_line_id, units, returned_by, received_by)` (received_by ≠ returned_by).
- **Tests.** `test_rx_registers.py::test_empties_block_redispense`, `::test_empties_two_person`.

## A12 — Lost-sale and pending-balance call list
- Daily list: prescriptions signed yesterday at branches with a pharmacy and **not** billed, plus partial fulfilments
  with balance pending. Columns: patient, phone (masked; *Call* reveals for RX_DISPENSE with audit), doctor, items.
  No automatic message (a staff call is a service; a message would be marketing).
- **Tests.** `test_rx_reports.py::test_lost_sale_list`.

## A13 — Pharmacy shift Z-report
- On shift close (or any time): sales count/value by mode, returns, voids, discounts (with who approved overrides),
  Schedule H1/X counts, cash expected (the same number `opd_close_shift` uses after D1). 80 mm print.
- **Tests.** `test_rx_pos.py::test_z_report_matches_close_shift`.

## A14 — Medicine history and recorded allergies (display only)
- POS side panel for an identified patient: last 10 pharmacy purchases and the allergy information **already recorded**
  by clinicians (`family_members.allergies` / the latest signed Rx `allergy_review`, AUDIT A1). Shown as recorded text
  with its source and date. **No matching, interaction or allergy checking is performed** (ADR-RX-06; GAP G29) — the
  panel only puts what clinicians wrote in front of the pharmacist.
- **Tests.** `test_rx_pos.py::test_history_panel_read_only_sources`.

## Explicitly rejected ideas (and why)
| Idea | Why not |
|:-|:-|
| AI "suggest alternative medicine" | clinical substitution — ADR-RX-06 |
| Auto-dispense refills | needs pharmacist verification of a valid Rx |
| Drug–interaction alerts at the counter | clinical decision support belongs to the prescriber (GAP G29) |
| Patient-facing stock availability on WhatsApp | invites self-medication; KRIYA-KETU §3 |
| Auto-applying computed reorder levels | silent master changes; A7 is suggest-then-apply |
