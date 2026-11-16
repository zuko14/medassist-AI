# Kriya Pharmacy OS — Rollout, Opt-in, Opening Stock and Cutover

> Part of the Pharmacy OS set: start at [status.md](status.md). Precedent: `docs/diagnostic-os/ROLLOUT.md`,
> `docs/ipd-os/ROLLOUT.md`.

## 0. Zero-downtime deployment
1. **Migrations first, code second.** Apply 134–142 to Supabase (SQL editor or migration runner) **before** deploying
   code that reads them. Every migration is additive and idempotent: new tables, new nullable/defaulted columns, one
   widened CHECK (`ipd_charges.source`), new functions/triggers. Running code that predates them is unaffected.
2. **Order:** 134 → 142, each followed by its block in `migrations/verify_supabase_schema.sql` (must report `OK`).
3. **Lock impact:** `ALTER TABLE clinics ADD COLUMN … DEFAULT` (constant default — metadata-only in PG ≥ 11);
   `ALTER TABLE ipd_charges DROP/ADD CONSTRAINT … CHECK` — the ADD validates existing rows (short `ACCESS EXCLUSIVE`
   on `ipd_charges`). Run 139 off-peak (02:00–04:00 IST) or use `ADD CONSTRAINT … NOT VALID` then `VALIDATE
   CONSTRAINT` (lighter lock) — the migration uses the NOT VALID + VALIDATE form. No other existing table is altered
   (the v1 MAR column was dropped from the design — VERIFICATION D2). 138 redefines `opd_close_shift` (`CREATE OR
   REPLACE`, instant; identical results for shifts without pharmacy cash).
4. **Deploy code** (Render): router mounted, every route gated → no behaviour change for any clinic.
5. **Verify in production** (no clinic enabled yet): `GET /health` OK; `GET /admin/rx/setup` as any clinic → 403
   `pharmacy_not_enabled`; scheduler log shows RX jobs with `clinics=0`.
6. **Rollback** (any time before a clinic is enabled): redeploy previous code; optionally run
   `migrations/rollback/142…134_down.sql` in reverse (each refuses if any `rx_*` row exists for a clinic not purged).
   After a clinic is live, roll back code only (tables stay; module just disappears from the UI).

## 1. Tenant opt-in switch
| Step | Who | Action |
|:-|:-|:-|
| 1 | Platform owner | Clinic must have OPD enabled (`opd_enabled`). For IPD dispensing also IPD ACTIVE |
| 2 | Platform owner | `PATCH /platform/clinics/{id}/features {"pharmacy_enabled": true}` (owner console toggle) → `rx_state='ACTIVE'`, default store "Main Pharmacy" (`retail_counter`, `can_receive_grn=true`) provisioned |
| 3 | Clinic admin | Pharmacy Setup checklist (below) |
| 4 | Platform owner | After go-live sign-off, note in clinic record; enable refills only after template approval |
| Disable | Platform owner | toggle off → 409 preview (open drafts, indents, transfers, audits) → confirm → `WIND_DOWN` (returns, exports, audits only) → OFF when preview empty. Data retained |

## 2. Pharmacy Setup checklist (per clinic, shown on `rxsetup`)
1. Stores: central/counter/ward sub-stores, drug licence numbers (Form 20/21 numbers print on bills).
2. GSTIN present in OPD settings (or confirm bill-of-supply mode).
3. Staff: create PHARMACIST / PHARMACY_MANAGER / STORE_KEEPER logins; name `RX_CONTROLLED` custodians.
4. Item master import (§4.2) → review → fix errors.
5. Suppliers import or entry.
6. Opening stock import per store (§4.3) on the cutover day.
7. Settings: discount cap, near-expiry days, receipt format, return days, IPD autopost and price basis (ADR open O-2/O-3).
8. Printer test: 80 mm thermal (USB/Bluetooth via OS driver) or A5; barcode scanner test on POS search.
9. Cashier shift flow test: open shift → test sale → return → close shift totals include pharmacy.
10. WhatsApp: template `rx_refill_due_v1` (utility, two quick-reply buttons) and `rx_bill_v1` submitted/approved in
    the clinic's WABA — only if using refills/e-bills. If Meta re-categorises the refill template as **marketing**,
    tell the clinic the per-message cost; refills stay opt-in only (VERIFICATION §9).
11. Legal: register formats confirmed for the state (owner decision O-4).

## 3. Go-live gates (pilot clinic)
- All P0–P11 tasks VERIFIED (status §4); RG1–RG8 green on the release commit.
- Opening stock reconciled: imported value vs legacy system's closing stock report within 0.5% (or every difference
  explained in the cutover sheet).
- Two full days of parallel run (§5) with zero unexplained variance.
- Drug registers: opening balances for narcotics entered and countersigned by the custodian.
- Pharmacist trained (POS keyboard flow ≤ 3 s on a 3-line Rx, three times).

## 4. Opening stock CSV import runbook
### 4.1 Rules
- One file per store; UTF-8 CSV; header row exact; amounts in rupees with up to 2 decimals (converted to paise);
  dates `MM/YYYY` for expiry; quantities in **base units** (`qty_units`) or `packs` + `loose` (one or the other).
- Import is allowed only while the store has no non-`opening` movements (so it cannot hide shrinkage later).
- Always `dry_run=true` first; the response lists row errors; nothing is written until a clean apply.
- Apply is idempotent per (item, batch, expiry, MRP, store): re-uploading the same file writes nothing new.
- Expired rows are **rejected** (they cannot be opening stock for sale); export them separately as a write-off list
  for the legacy system.

### 4.2 Item master CSV (`/admin/rx/items/import`)
`item_code, brand_name, generic_name, strength, dosage_form, route, manufacturer, item_kind, schedule, base_unit,
pack_size, pack_label, allow_loose, hsn_code, gst_rate_percent, reorder_level_units, reorder_qty_units, is_chronic,
is_cold_chain, rack_location, barcode`
Validation: `gst_rate_percent ∈ {0,5,12,18,28,40}`; HSN 4–8 digits; schedule ∈ {none,G,H,H1,X,narcotic,psychotropic};
generic created on first sight of (generic_name, strength, dosage_form).

### 4.3 Opening stock CSV (`/admin/rx/opening-stock?store_id=`)
`item_code, batch_no, expiry_mm_yyyy, mrp_per_pack, purchase_rate_per_pack, gst_rate_percent, qty_units | packs,loose`
Landed cost = purchase rate (excl. GST if clinic has GSTIN, incl. otherwise) / pack_size. Written as `opening`
movements; controlled items also write register `in` rows with "Opening balance" as the source.

## 5. Cutover from legacy systems (Marg / MocDoc / others)
| Day | Action |
|:-|:-|
| T−7 | Export item master from legacy (Marg: item list export to Excel; MocDoc: drug master export) → map columns to §4.2 → import dry-run → fix |
| T−3 | Supplier list import; staff logins; printer/scanner test; training on sandbox clinic |
| T−1 evening | Freeze purchases in legacy; legacy **closing stock by batch** report exported after last sale |
| T−1 night | Physical spot-count of top 100 value items + all narcotics; correct the export; import opening stock dry-run → apply |
| T0 | All sales in Kriya. Legacy kept read-only for returns of old bills (returns of pre-cutover bills are processed in Kriya as a stock `adjust_in` with the legacy bill number in the reason, RX_ADMIN) |
| T0–T+1 | Parallel: end-of-day stock of 50 sampled items counted vs Kriya; variance log |
| T+7 | Full stock audit (W8) on Kriya; legacy retired |
Registers: legacy H1/narcotic registers are closed with a signed closing entry; Kriya registers start with opening
balances on T0 (the first page of the export shows "Opened on T0, continued from legacy register no. …").

## 6. Monitoring after go-live
- Nightly jobs: `rx_ledger_check`, `rx_register_verify`, `rx_alerts_scan` (low stock, near expiry, expired on hand) —
  failures → `admin_notifications` + platform owner alert.
- Weekly owner review: void count, discount over cap, FEFO overrides, audit variance value, refill opt-out rate.
- Logs: route latency for `/admin/rx/sales/*/complete` p95 alert at 2 s.
