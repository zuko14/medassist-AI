# Kriya Pharmacy OS — Data Model

> Part of the Pharmacy OS set: start at [status.md](status.md). Decisions: [ADR.md](ADR.md). Evidence: [AUDIT.md](AUDIT.md).
> Migrations **134–142** (latest on disk at audit: `133_dx_instruments_qc.sql`). If a newer migration takes a number,
> renumber yours (resume protocol, status §2). Each migration ships `migrations/rollback/<n>_down.sql` and a
> verify block in `migrations/verify_supabase_schema.sql`.

## 0. Conventions (apply to every table below)
- **Tenant key.** `clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE`, `UNIQUE (clinic_id, id)`.
  Every child → parent FK is composite `(clinic_id, parent_id)`. Cross-tenant references are impossible by schema.
- **Money.** Integer paise (`INTEGER`, `BIGINT` for aggregates). No floats, no `NUMERIC` money.
- **Quantity.** Integer **base units** (tablet, capsule, ml, gram, vial, ampoule, piece). A strip of 10 = 10 units.
  `pack_size` converts packs↔units. Fractional ml is entered as whole ml; syrups are sold by bottle (`base_unit='bottle'`).
- **Time.** `TIMESTAMPTZ` stored UTC; business dates via `rx_ist_date(ts)` (= `ipd_ist_date`, 111:45). Expiry is a
  `DATE` = **last day of the printed expiry month** (label "EXP 03/2027" → `2027-03-31`).
- **Errors.** `rx_raise('rx_<code>')` → `ERRCODE P0001`; Python maps codes to HTTP (API.md §0).
- **Purge.** Every guard returns early when `rx_purging(clinic_id)` (GUC `kriya.rx_purge_clinic`).
- **Function-only writes.** Tables marked **[F]** accept inserts only while GUC `kriya.rx_tx = clinic_id` is set by an
  `rx_*` SQL function (pattern: `ipd_in_tx`, 111:38; `opd_guard_credit_note_insert`, 117:272).
- **Append-only.** Tables marked **[A]** reject UPDATE/DELETE outside purge (`rx_guard_append_only`).

## 1. Module state, settings, stores (134 — RX-M1)
```sql
ALTER TABLE clinics
  ADD COLUMN IF NOT EXISTS rx_state    TEXT  NOT NULL DEFAULT 'OFF',
  ADD COLUMN IF NOT EXISTS rx_settings JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE clinics ADD CONSTRAINT clinics_rx_state_check
  CHECK (rx_state IN ('OFF','ACTIVE','WIND_DOWN') AND jsonb_typeof(rx_settings) = 'object');

CREATE TABLE rx_stores (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id     UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
  branch_id     UUID NULL,
  parent_store_id UUID NULL,                         -- sub-stores replenish from parent
  code          TEXT NOT NULL CHECK (code ~ '^[A-Z0-9_-]{2,20}$'),
  name          TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 100),
  store_type    TEXT NOT NULL CHECK (store_type IN ('central','retail_counter','ward_substore','ot_tray','emergency_tray')),
  ward_id       UUID NULL,                           -- ward_substore only
  can_sell      BOOLEAN NOT NULL DEFAULT false,      -- POS allowed (retail_counter)
  can_receive_grn BOOLEAN NOT NULL DEFAULT false,    -- central (and retail if no central)
  drug_licence_no TEXT NULL CHECK (char_length(drug_licence_no) <= 60),  -- Form 20/21 printed on bills
  is_active     BOOLEAN NOT NULL DEFAULT true,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT rx_stores_clinic_id_id_key UNIQUE (clinic_id, id),
  CONSTRAINT rx_stores_code_key UNIQUE (clinic_id, code),
  CONSTRAINT rx_stores_ward_shape CHECK ((store_type = 'ward_substore') = (ward_id IS NOT NULL)),
  CONSTRAINT rx_stores_sell_shape CHECK (NOT can_sell OR store_type = 'retail_counter'),
  CONSTRAINT rx_stores_no_self_parent CHECK (parent_store_id IS DISTINCT FROM id),
  CONSTRAINT rx_stores_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
  CONSTRAINT rx_stores_parent_fk FOREIGN KEY (clinic_id, parent_store_id) REFERENCES rx_stores (clinic_id, id),
  CONSTRAINT rx_stores_ward_fk   FOREIGN KEY (clinic_id, ward_id) REFERENCES ipd_wards (clinic_id, id)
);
CREATE UNIQUE INDEX uq_rx_stores_one_central ON rx_stores (clinic_id, branch_id) WHERE store_type = 'central' AND is_active;
```
`rx_settings` keys (validated by API, defaults in `app/services/rx.py::DEFAULT_SETTINGS`):
`max_discount_bps` (1000), `near_expiry_days` (90), `min_shelf_days_at_sale` (30), `ipd_autopost` (true),
`allow_loose_sale` (true), `round_off_to_rupee` (true), `refill_lead_days` (5), `refill_enabled` (false),
`receipt_format` ('80mm' | 'a5'), `fefo_override_requires_reason` (true), `rx_validity_days` (30), `return_days` (7),
`audit_approval_paise` (500000), `ipd_price_basis` ('mrp' | 'cost_plus'), `ipd_cost_plus_bps` (only with cost_plus).
GST registration is always read from `opd_clinic_gstin` (117:47) — there is no pharmacy-specific GSTIN override.

## 2. Catalogue (135 — RX-M2)
```sql
CREATE TABLE rx_manufacturers (id, clinic_id, name TEXT NOT NULL, is_active, created_at,
  UNIQUE (clinic_id, id), UNIQUE (clinic_id, lower(name)));  -- expression unique via index

CREATE TABLE rx_generics (                         -- molecule(s) + strength + form = substitution class
  id UUID PK, clinic_id UUID NOT NULL,
  name           TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 200),   -- "Amoxicillin + Clavulanic acid"
  strength       TEXT NOT NULL CHECK (char_length(btrim(strength)) BETWEEN 1 AND 60), -- "500 mg + 125 mg"
  dosage_form    TEXT NOT NULL CHECK (dosage_form IN ('tablet','capsule','syrup','suspension','injection','drops',
                   'ointment','cream','gel','inhaler','powder','lotion','spray','patch','infusion','other')),
  route          TEXT NOT NULL,                     -- same enum as opd_prescription_items.route (103:381)
  is_active BOOLEAN NOT NULL DEFAULT true, created_at,
  CONSTRAINT rx_generics_clinic_id_id_key UNIQUE (clinic_id, id),
  CONSTRAINT rx_generics_class_key UNIQUE (clinic_id, name, strength, dosage_form)
);

CREATE TABLE rx_items (                            -- one sellable SKU (brand + pack)
  id UUID PK, clinic_id UUID NOT NULL,
  item_code      TEXT NOT NULL CHECK (item_code ~ '^[A-Z0-9_-]{2,40}$'),   -- immutable (as 120:52)
  brand_name     TEXT NOT NULL CHECK (char_length(btrim(brand_name)) BETWEEN 2 AND 200),
  generic_id     UUID NULL,                         -- NULL = OTC/consumable without a substitution class
  manufacturer_id UUID NULL,
  item_kind      TEXT NOT NULL DEFAULT 'drug' CHECK (item_kind IN ('drug','consumable','surgical','otc','nutrition','device')),
  schedule       TEXT NOT NULL DEFAULT 'none' CHECK (schedule IN ('none','G','H','H1','X','narcotic','psychotropic')),
  base_unit      TEXT NOT NULL CHECK (base_unit IN ('tablet','capsule','ml','gram','vial','ampoule','bottle','tube',
                   'sachet','piece','pair','box','inhaler','patch')),
  pack_size      INTEGER NOT NULL CHECK (pack_size BETWEEN 1 AND 10000),    -- base units per pack
  pack_label     TEXT NOT NULL DEFAULT 'strip' CHECK (pack_label IN ('strip','blister','bottle','vial','ampoule','tube','box','sachet','piece','pack')),
  allow_loose    BOOLEAN NOT NULL DEFAULT true,     -- sell < pack_size units
  hsn_code       TEXT NOT NULL CHECK (hsn_code ~ '^[0-9]{4,8}$'),
  tax_rate_bps   SMALLINT NOT NULL CHECK (tax_rate_bps IN (0,500,1200,1800,2800,4000)),  -- same CHECK as 117:55
  reorder_level_units INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level_units >= 0),
  reorder_qty_units   INTEGER NOT NULL DEFAULT 0 CHECK (reorder_qty_units >= 0),
  is_chronic     BOOLEAN NOT NULL DEFAULT false,    -- eligible for refill reminders (W10)
  is_cold_chain  BOOLEAN NOT NULL DEFAULT false,
  rack_location  TEXT NULL CHECK (char_length(rack_location) <= 40),
  is_active      BOOLEAN NOT NULL DEFAULT true,
  created_at, updated_at,
  CONSTRAINT rx_items_clinic_id_id_key UNIQUE (clinic_id, id),
  CONSTRAINT rx_items_code_key UNIQUE (clinic_id, item_code),
  CONSTRAINT rx_items_controlled_no_loose CHECK (schedule NOT IN ('X','narcotic') OR allow_loose),  -- narcotics dispensed per unit
  CONSTRAINT rx_items_drug_has_generic CHECK (item_kind <> 'drug' OR generic_id IS NOT NULL),
  CONSTRAINT rx_items_generic_fk FOREIGN KEY (clinic_id, generic_id) REFERENCES rx_generics (clinic_id, id),
  CONSTRAINT rx_items_mfr_fk     FOREIGN KEY (clinic_id, manufacturer_id) REFERENCES rx_manufacturers (clinic_id, id)
);
CREATE INDEX idx_rx_items_generic ON rx_items (clinic_id, generic_id) WHERE is_active;
CREATE INDEX idx_rx_items_name_trgm ON rx_items USING gin (lower(brand_name) gin_trgm_ops);  -- when pg_trgm exists (120 pattern)

CREATE TABLE rx_item_barcodes (id, clinic_id, item_id, barcode TEXT NOT NULL CHECK (barcode ~ '^[0-9A-Za-z]{6,40}$'),
  symbology TEXT NOT NULL CHECK (symbology IN ('ean13','ean8','gs1_128','gs1_datamatrix','upc','internal')),
  UNIQUE (clinic_id, id), UNIQUE (clinic_id, barcode), FK (clinic_id, item_id) → rx_items);

CREATE TABLE rx_item_aliases (                     -- learned free-text → item mappings (ADR-RX-07)
  id, clinic_id, item_id,
  alias_norm TEXT NOT NULL CHECK (char_length(alias_norm) BETWEEN 2 AND 200),  -- rx_normalize(drug_name||' '||strength)
  source TEXT NOT NULL CHECK (source IN ('manual','pharmacist_confirmed','import')),
  confirmed_by UUID NULL, created_at,
  UNIQUE (clinic_id, id), UNIQUE (clinic_id, alias_norm, item_id), FK (clinic_id, item_id) → rx_items);
```
Guard `rx_guard_item`: `item_code`, `generic_id` (once any movement exists), `pack_size` (once any batch exists),
`base_unit` and `schedule` downgrade (e.g. H1 → none) are immutable; schedule upgrade allowed. Changing
`tax_rate_bps`/`hsn_code` is allowed (rate notifications change) and is logged in `rx_item_changes` **[A]**
(`item_id, field, old, new, changed_by, changed_at, reason`) — sales snapshot the rate per line, so history is stable.

## 3. Batches and the stock ledger (136 — RX-M3)
```sql
CREATE TABLE rx_batches (
  id UUID PK, clinic_id UUID NOT NULL, item_id UUID NOT NULL,
  batch_no        TEXT NOT NULL CHECK (batch_no ~ '^[A-Za-z0-9/._-]{1,30}$'),
  expiry_date     DATE NOT NULL,                    -- last day of printed month
  mfg_date        DATE NULL,
  mrp_pack_paise  INTEGER NOT NULL CHECK (mrp_pack_paise BETWEEN 1 AND 100000000),   -- MRP incl. GST, per pack
  ptr_pack_paise  INTEGER NULL CHECK (ptr_pack_paise >= 0),   -- price to retailer (info)
  landed_unit_cost_paise_x100 BIGINT NOT NULL CHECK (landed_unit_cost_paise_x100 >= 0),  -- cost per base unit ×100 (sub-paise precision for valuation)
  first_grn_line_id UUID NULL,
  is_blocked      BOOLEAN NOT NULL DEFAULT false,   -- recall / quarantine
  block_reason    TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT rx_batches_clinic_id_id_key UNIQUE (clinic_id, id),
  CONSTRAINT rx_batches_identity_key UNIQUE (clinic_id, item_id, batch_no, expiry_date, mrp_pack_paise),
  CONSTRAINT rx_batches_dates CHECK (mfg_date IS NULL OR mfg_date < expiry_date),
  CONSTRAINT rx_batches_block_reason CHECK (NOT is_blocked OR char_length(btrim(block_reason)) >= 3),
  CONSTRAINT rx_batches_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);
```
Same batch number with a different MRP is a **different batch row** (DPCO revisions) — sale price can never silently
change mid-batch. Guard: only `is_blocked`, `block_reason` mutable.

```sql
CREATE TABLE rx_stock_movements (                  -- [A][F] the ledger — the ONLY source of stock truth
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  clinic_id       UUID NOT NULL,
  store_id        UUID NOT NULL,
  batch_id        UUID NOT NULL,
  item_id         UUID NOT NULL,                    -- denormalised; CHECKed equal to batch.item_id by trigger
  qty_units       INTEGER NOT NULL CHECK (qty_units <> 0),          -- + in, − out
  movement_type   TEXT NOT NULL CHECK (movement_type IN (
                    'opening','grn_in','sale_out','sale_return_in','ipd_dispense_out','ipd_return_in',
                    'transfer_out','transfer_in','supplier_return_out','adjust_in','adjust_out',
                    'expiry_writeoff_out','damage_writeoff_out','ward_issue_out')),
  ref_table       TEXT NOT NULL,                    -- 'rx_grn_lines','rx_sale_items',…
  ref_id          UUID NOT NULL,
  unit_cost_paise_x100 BIGINT NOT NULL,             -- batch landed cost at the time (valuation)
  actor_id        UUID NULL, actor_name TEXT NOT NULL,
  reason          TEXT NULL CHECK (char_length(reason) <= 300),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT rx_sm_clinic_id_id_key UNIQUE (clinic_id, id),
  CONSTRAINT rx_sm_sign CHECK ((movement_type LIKE '%\_in' ESCAPE '\' OR movement_type = 'opening') = (qty_units > 0)),
  CONSTRAINT rx_sm_reason_for_adjust CHECK (movement_type NOT IN ('adjust_in','adjust_out','damage_writeoff_out')
                                           OR char_length(btrim(COALESCE(reason,''))) >= 3),
  CONSTRAINT rx_sm_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
  CONSTRAINT rx_sm_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id),
  CONSTRAINT rx_sm_item_fk  FOREIGN KEY (clinic_id, item_id)  REFERENCES rx_items (clinic_id, id)
);
CREATE UNIQUE INDEX uq_rx_sm_ref ON rx_stock_movements (clinic_id, ref_table, ref_id, movement_type, batch_id, store_id);
CREATE INDEX idx_rx_sm_store_item ON rx_stock_movements (clinic_id, store_id, item_id, created_at DESC);
CREATE INDEX idx_rx_sm_batch ON rx_stock_movements (clinic_id, batch_id);

CREATE TABLE rx_stock_balances (                   -- derived projection, written ONLY by trg_rx_sm_apply
  clinic_id UUID NOT NULL, store_id UUID NOT NULL, batch_id UUID NOT NULL, item_id UUID NOT NULL,
  qty_units INTEGER NOT NULL CHECK (qty_units >= 0),   -- ← the hard negative-stock wall
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (clinic_id, store_id, batch_id),
  FKs (clinic_id, store_id) → rx_stores, (clinic_id, batch_id) → rx_batches
);
CREATE INDEX idx_rx_bal_item ON rx_stock_balances (clinic_id, store_id, item_id) WHERE qty_units > 0;
```
**Triggers on `rx_stock_movements`** (BEFORE INSERT, in order):
1. `rx_sm_require_fn` — `IF NOT rx_in_tx(NEW.clinic_id) THEN rx_raise('rx_movement_via_function_only')`.
2. `rx_sm_check_batch` — batch belongs to item; `rx_raise('rx_batch_item_mismatch')`.
3. `rx_sm_expiry_lock` — for `movement_type IN ('sale_out','ipd_dispense_out','transfer_out','ward_issue_out')`:
   `IF batch.expiry_date < rx_ist_date(now()) THEN rx_raise('rx_batch_expired')`; `IF batch.is_blocked THEN
   rx_raise('rx_batch_blocked')`. `supplier_return_out`, `expiry_writeoff_out`, `damage_writeoff_out` are allowed on
   expired/blocked batches (that is how they leave).
4. `rx_sm_apply` (AFTER INSERT) — `INSERT INTO rx_stock_balances … ON CONFLICT (clinic_id,store_id,batch_id) DO UPDATE
   SET qty_units = rx_stock_balances.qty_units + EXCLUDED.qty_units`. The row-level lock taken by `ON CONFLICT DO
   UPDATE` serialises concurrent movements on the same store×batch; the `CHECK (qty_units >= 0)` aborts the
   transaction with `23514` → mapped to `rx_insufficient_stock` (409). There is no code path that can leave a
   negative balance, and no way to update `rx_stock_balances` except this trigger (`rx_guard_balance` BEFORE
   UPDATE/DELETE requires `pg_trigger_depth() > 1` or purge).
5. `rx_guard_append_only` BEFORE UPDATE OR DELETE on movements.

**Reconciliation invariant** (job `rx_ledger_check`, nightly 02:30 IST, and TEST-PLAN §3):
`rx_stock_balances.qty_units = SUM(rx_stock_movements.qty_units) GROUP BY clinic_id, store_id, batch_id` — any
difference raises an `admin_notifications` P1 and is a release blocker.

**Views:** `rx_v_item_stock(clinic_id, store_id, item_id, qty_units, nearest_expiry)`;
`rx_v_fefo(clinic_id, store_id, item_id, batch_id, expiry_date, qty_units, mrp_pack_paise, rank)` ordered by
`expiry_date, created_at` over balances `> 0`, not blocked, not expired.

## 4. Suppliers, purchase orders, GRN (137 — RX-M4)
```sql
CREATE TABLE rx_suppliers (id, clinic_id, name TEXT NOT NULL, gstin TEXT NULL CHECK (gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'),
  state_code CHAR(2) NULL, drug_licence_no TEXT NULL, phone TEXT NULL, email TEXT NULL, address TEXT NULL,
  payment_terms_days SMALLINT NOT NULL DEFAULT 30 CHECK (payment_terms_days BETWEEN 0 AND 365),
  is_active BOOLEAN NOT NULL DEFAULT true, created_at, updated_at,
  UNIQUE (clinic_id, id), UNIQUE (clinic_id, gstin) WHERE gstin IS NOT NULL);

CREATE TABLE rx_purchase_orders (
  id, clinic_id, store_id, supplier_id,
  po_year SMALLINT NULL, po_seq INTEGER NULL,
  po_number TEXT GENERATED ALWAYS AS (CASE WHEN po_seq IS NULL THEN NULL ELSE opd_format_number('PO-', po_year, po_seq, 5) END) STORED,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','partially_received','received','cancelled')),
  expected_on DATE NULL, notes TEXT NULL, created_by, created_at, sent_at, cancelled_at, cancel_reason,
  UNIQUE (clinic_id, id), UNIQUE (clinic_id, po_year, po_seq),
  CHECK (status = 'draft' OR po_seq IS NOT NULL),
  FKs store, supplier);
CREATE TABLE rx_po_lines (id, clinic_id, po_id, line_no SMALLINT, item_id, ordered_packs INTEGER CHECK (>0),
  free_packs INTEGER NOT NULL DEFAULT 0, expected_rate_pack_paise INTEGER NULL, received_packs INTEGER NOT NULL DEFAULT 0,
  UNIQUE (clinic_id, id), UNIQUE (po_id, line_no), CHECK (received_packs >= 0));

CREATE TABLE rx_grns (                             -- header; posted GRNs are immutable
  id, clinic_id, store_id, supplier_id, po_id NULL,
  grn_year SMALLINT NULL, grn_seq INTEGER NULL,
  grn_number TEXT GENERATED ALWAYS AS (… opd_format_number('GRN-', grn_year, grn_seq, 5) …) STORED,
  supplier_invoice_no TEXT NOT NULL CHECK (char_length(btrim(supplier_invoice_no)) BETWEEN 1 AND 40),
  supplier_invoice_date DATE NOT NULL,
  is_interstate BOOLEAN NOT NULL,                  -- supplier state ≠ clinic GSTIN state → IGST
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted','void')),
  taxable_paise BIGINT NOT NULL DEFAULT 0, cgst_paise BIGINT NOT NULL DEFAULT 0, sgst_paise BIGINT NOT NULL DEFAULT 0,
  igst_paise BIGINT NOT NULL DEFAULT 0, freight_paise INTEGER NOT NULL DEFAULT 0, round_off_paise SMALLINT NOT NULL DEFAULT 0
    CHECK (round_off_paise BETWEEN -99 AND 99),
  invoice_total_paise BIGINT NOT NULL DEFAULT 0,   -- as printed by supplier; must equal computed ± round_off
  posted_at, posted_by, void_reason, created_by, created_at,
  UNIQUE (clinic_id, id), UNIQUE (clinic_id, grn_year, grn_seq),
  UNIQUE (clinic_id, supplier_id, supplier_invoice_no) -- same supplier bill cannot be inwarded twice
);
CREATE TABLE rx_grn_lines (
  id, clinic_id, grn_id, line_no SMALLINT, item_id, po_line_id NULL,
  batch_no TEXT NOT NULL, expiry_date DATE NOT NULL, mfg_date DATE NULL,
  packs INTEGER NOT NULL CHECK (packs > 0), free_packs INTEGER NOT NULL DEFAULT 0 CHECK (free_packs >= 0),
  rate_pack_paise INTEGER NOT NULL CHECK (rate_pack_paise >= 0),          -- purchase rate excl. GST
  scheme_discount_bps SMALLINT NOT NULL DEFAULT 0 CHECK (scheme_discount_bps BETWEEN 0 AND 10000),
  cash_discount_bps SMALLINT NOT NULL DEFAULT 0 CHECK (cash_discount_bps BETWEEN 0 AND 10000),
  tax_rate_bps SMALLINT NOT NULL CHECK (tax_rate_bps IN (0,500,1200,1800,2800,4000)),
  mrp_pack_paise INTEGER NOT NULL CHECK (mrp_pack_paise > 0),
  taxable_paise BIGINT NOT NULL, tax_paise BIGINT NOT NULL,             -- computed by rx_grn_line_calc trigger
  landed_unit_cost_paise_x100 BIGINT NOT NULL,                          -- computed (see ADR-RX-08 §GRN)
  batch_id UUID NULL,                                                   -- set when posted
  UNIQUE (clinic_id, id), UNIQUE (grn_id, line_no),
  CHECK (expiry_date > supplier_invoice_date)  -- via trigger (cross-table)
);
```
`rx_grn_line_calc` (BEFORE INSERT/UPDATE while draft): `gross = packs × rate`; `taxable = gross − round_half_up(gross
× scheme_bps/10000) − round_half_up(… × cash_bps/10000)`; `tax = opd_gst_amount(taxable, rate)`;
`landed_unit_cost_x100 = round_half_up((taxable + tax_if_no_itc + freight_share) × 100 / ((packs + free_packs) ×
pack_size))` where `tax_if_no_itc = tax` when the clinic has no GSTIN (cost includes GST) else 0. Free packs dilute
cost; they do not change MRP. A line whose `rate_pack > mrp_pack` warns; whose expiry is within
`near_expiry_days` warns; whose expiry is already past is rejected (`rx_grn_expired_batch`).

`rx_post_grn(clinic, grn_id, actor)` **[F]**: lock GRN; check totals within ±99 paise of `invoice_total_paise`;
assign `GRN-` number; for each line upsert `rx_batches` by identity key; insert `grn_in` movement of `(packs +
free_packs) × pack_size`; increase `rx_po_lines.received_packs`, recompute PO status. Idempotent on a posted GRN.
Void allowed only if every movement of its batches into that store is still on hand (reverses with `adjust_out`
movements referencing the GRN, reason required).

```sql
CREATE TABLE rx_supplier_returns (id, clinic_id, store_id, supplier_id, srn_year, srn_seq,
  srn_number GENERATED ('SRN-'), status TEXT CHECK (status IN ('draft','dispatched','cancelled')),
  reason TEXT CHECK (reason IN ('expired','near_expiry','damaged','recall','excess','other')),
  credit_received_paise BIGINT NULL, credit_ref TEXT NULL, dispatched_by, dispatched_at, created_at,
  UNIQUE (clinic_id, id), UNIQUE (clinic_id, srn_year, srn_seq));
CREATE TABLE rx_supplier_return_lines (id, clinic_id, return_id, batch_id, item_id, qty_units INTEGER CHECK (>0),
  rate_pack_paise INTEGER NOT NULL, tax_rate_bps SMALLINT NOT NULL,          -- from the batch's first GRN line
  UNIQUE (clinic_id, id));
```
`rx_supplier_return(clinic, return_id, actor)` **[F]** writes one `supplier_return_out` per line (permitted on expired
or blocked batches, ADR-RX-02).

## 5. POS sales, payments, returns, credit notes (138 — RX-M5)
```sql
CREATE TABLE rx_sales (
  id, clinic_id, store_id, branch_id NULL,
  sale_kind TEXT NOT NULL CHECK (sale_kind IN ('opd_rx','otc','ipd_discharge_meds','staff')),
  patient_id UUID NULL, family_member_id UUID NULL, walkin_name TEXT NULL, walkin_phone_masked TEXT NULL,
  opd_prescription_id UUID NULL,                   -- signed Rx fulfilled (composite FK → opd_prescriptions)
  prescriber_snapshot JSONB NULL,                  -- copied from opd_prescriptions.signer_snapshot or entered (external Rx)
  external_rx_ref TEXT NULL,                       -- paper Rx from outside doctor: reg. no. + name required for H/H1/X
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','completed','void')),
  sale_year SMALLINT NULL, sale_seq INTEGER NULL,
  sale_number TEXT GENERATED ALWAYS AS (… opd_format_number('PHS-', sale_year, sale_seq, 6) …) STORED,
  is_tax_invoice BOOLEAN NOT NULL DEFAULT false,   -- clinic GSTIN present
  clinic_gstin TEXT NULL, buyer_gstin TEXT NULL,
  gross_paise BIGINT NOT NULL DEFAULT 0,           -- Σ line MRP value (incl. GST)
  discount_paise BIGINT NOT NULL DEFAULT 0,
  round_off_paise SMALLINT NOT NULL DEFAULT 0 CHECK (round_off_paise BETWEEN -99 AND 99),
  net_paise BIGINT GENERATED ALWAYS AS (gross_paise - discount_paise + round_off_paise) STORED,
  taxable_paise BIGINT NOT NULL DEFAULT 0, cgst_paise BIGINT NOT NULL DEFAULT 0, sgst_paise BIGINT NOT NULL DEFAULT 0,
  paid_paise BIGINT NOT NULL DEFAULT 0, credited_paise BIGINT NOT NULL DEFAULT 0,
  idempotency_key TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
  completed_at, completed_by, completed_by_name, voided_at, voided_by, void_reason, created_by, created_at,
  UNIQUE (clinic_id, id), UNIQUE (clinic_id, sale_year, sale_seq),
  CHECK (status = 'draft' OR sale_seq IS NOT NULL),
  CHECK (status <> 'completed' OR paid_paise = net_paise),   -- POS is pay-at-counter; credit sales = RX-D2 (deferred)
  CHECK (discount_paise <= gross_paise),
  CHECK (patient_id IS NOT NULL OR walkin_name IS NOT NULL OR sale_kind = 'otc')
);
CREATE UNIQUE INDEX uq_rx_sales_idem ON rx_sales (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX uq_rx_sales_one_live_per_rx ON rx_sales (clinic_id, opd_prescription_id, store_id)
  WHERE opd_prescription_id IS NOT NULL AND status = 'draft';   -- two counters cannot draft the same Rx

CREATE TABLE rx_sale_items (
  id, clinic_id, sale_id, line_no SMALLINT CHECK (line_no BETWEEN 1 AND 200),
  item_id, batch_id, qty_units INTEGER NOT NULL CHECK (qty_units > 0),
  opd_prescription_item_id UUID NULL,              -- composite FK → opd_prescription_items (103:372)
  substituted_from_item_id UUID NULL, substitution_reason TEXT NULL,   -- ADR-RX-07
  fefo_override_reason TEXT NULL,                  -- a later-expiring batch picked over FEFO rank 1
  mrp_pack_paise INTEGER NOT NULL, pack_size INTEGER NOT NULL,          -- snapshots
  line_gross_paise BIGINT NOT NULL,                -- round_half_up(mrp_pack × qty / pack_size)
  line_discount_paise BIGINT NOT NULL DEFAULT 0,
  tax_rate_bps SMALLINT NOT NULL, hsn_code TEXT NOT NULL, schedule TEXT NOT NULL,   -- snapshots
  taxable_paise BIGINT NOT NULL, cgst_paise BIGINT NOT NULL, sgst_paise BIGINT NOT NULL,
  returned_units INTEGER NOT NULL DEFAULT 0 CHECK (returned_units >= 0),
  UNIQUE (clinic_id, id), UNIQUE (sale_id, line_no),
  CHECK (returned_units <= qty_units),
  CHECK (line_discount_paise <= line_gross_paise),
  CHECK ((substituted_from_item_id IS NULL) = (substitution_reason IS NULL)),
  CHECK (line_gross_paise <= mrp_pack_paise::BIGINT * qty_units / pack_size + 1)    -- never above MRP (DPCO)
);
CREATE TABLE rx_sale_payments (                    -- [A]
  id, clinic_id, sale_id, shift_id UUID NOT NULL,  -- composite FK → opd_cashier_shifts (103:483)
  mode TEXT NOT NULL CHECK (mode IN ('cash','upi','card','wallet','ipd_bill')),
  amount_paise BIGINT NOT NULL,                    -- negative = refund
  reference TEXT NULL, created_by, created_at, UNIQUE (clinic_id, id));

CREATE TABLE rx_returns (                          -- patient return; [F]
  id, clinic_id, sale_id, store_id, return_year, return_seq,
  return_number GENERATED ('PRT-'), reason TEXT NOT NULL CHECK (reason IN ('unopened_unused','wrong_item','adverse_reaction_doctor_advised','therapy_changed','other')),
  refund_mode TEXT NOT NULL CHECK (refund_mode IN ('cash','upi','card','credit_note')),
  amount_paise BIGINT NOT NULL CHECK (amount_paise > 0), credit_note_id UUID NULL,
  created_by, created_by_name, created_at, UNIQUE (clinic_id, id));
CREATE TABLE rx_return_lines (id, clinic_id, return_id, sale_item_id, qty_units INTEGER CHECK (>0),
  restock BOOLEAN NOT NULL,                        -- false → damage_writeoff (opened / cold chain broken)
  amount_paise BIGINT NOT NULL, UNIQUE (clinic_id, id));
CREATE TABLE rx_credit_notes (                     -- [A][F] same shape as opd_credit_notes (117:236) but on rx_sales
  id, clinic_id, sale_id, cn_year, cn_seq, cn_number GENERATED ('PCN-'), amount_paise, tax_paise,
  reason, issued_by, issued_by_name, created_at, UNIQUE (clinic_id, id), UNIQUE (clinic_id, cn_year, cn_seq));
```
`rx_complete_sale(clinic, sale_id, payments JSONB, shift_id, idem, actor)` **[F]**: lock sale `FOR UPDATE`; refuse if
not draft; re-price every line from the batch snapshot (client prices ignored); re-check Schedule H/H1/X
preconditions (§7); insert one `sale_out` movement per line (balance trigger enforces stock and expiry); compute
GST split (ADR-RX-08); assign `PHS-` number; insert payments (Σ = net); write register rows; mark completed. Any
failure rolls back everything — no half-sold sale, no orphan movement.

`rx_complete_sale` also writes `rx_rx_fulfilments(clinic_id, opd_prescription_item_id, sale_item_id, units)` so a
second counter sees the line as fulfilled; partial fulfilment (out of stock) leaves the remainder open for 7 days.

## 6. IPD indents and dispensing (139 — RX-M6)
```sql
ALTER TABLE ipd_charges DROP CONSTRAINT ipd_charges_source_check;            -- widen only
ALTER TABLE ipd_charges ADD CONSTRAINT ipd_charges_source_check
  CHECK (source IN ('manual','bed_day','order','pharmacy'));
ALTER TABLE ipd_med_administrations ADD COLUMN IF NOT EXISTS rx_dispense_line_id UUID NULL;   -- FK added below

CREATE TABLE rx_ipd_indents (
  id, clinic_id, admission_id, ward_id, from_store_id,            -- store asked to supply
  indent_year, indent_seq, indent_number GENERATED ('IND-'),
  priority TEXT NOT NULL DEFAULT 'routine' CHECK (priority IN ('routine','urgent','stat')),
  status TEXT NOT NULL DEFAULT 'raised' CHECK (status IN ('raised','partially_dispensed','dispensed','cancelled','closed')),
  raised_by, raised_by_name, raised_at, cancelled_reason NULL, idempotency_key,
  UNIQUE (clinic_id, id), FK (clinic_id, admission_id) → ipd_admissions, FK store);
CREATE TABLE rx_indent_lines (
  id, clinic_id, indent_id, line_no, ipd_order_id UUID NOT NULL,    -- composite FK → ipd_orders (111:307)
  item_id UUID NULL,                                                -- nurse's pick or pharmacist's mapping
  requested_units INTEGER NOT NULL CHECK (requested_units BETWEEN 1 AND 10000),
  dispensed_units INTEGER NOT NULL DEFAULT 0 CHECK (dispensed_units >= 0),
  returned_units INTEGER NOT NULL DEFAULT 0 CHECK (returned_units >= 0),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','partial','done','cancelled','not_available')),
  UNIQUE (clinic_id, id), UNIQUE (indent_id, line_no),
  CHECK (dispensed_units <= requested_units), CHECK (returned_units <= dispensed_units));
CREATE TABLE rx_dispenses (id, clinic_id, indent_id, store_id, dispensed_by, dispensed_by_name, dispensed_at,
  received_by_nurse UUID NULL, received_at NULL, idempotency_key, UNIQUE (clinic_id, id));   -- [F]
CREATE TABLE rx_dispense_lines (                   -- [A][F]
  id, clinic_id, dispense_id, indent_line_id, item_id, batch_id, qty_units INTEGER CHECK (>0),
  substituted_from_item_id NULL, substitution_reason NULL, fefo_override_reason NULL,
  unit_price_paise INTEGER NOT NULL,                -- per base unit, IPD tariff (ADR-RX-08 §IPD)
  ipd_charge_id UUID NULL,                          -- charge written in the same tx
  UNIQUE (clinic_id, id));
ALTER TABLE ipd_med_administrations ADD CONSTRAINT ipd_mar_rx_dispense_fk
  FOREIGN KEY (clinic_id, rx_dispense_line_id) REFERENCES rx_dispense_lines (clinic_id, id);
```
Triggers: `rx_check_indent_line` (BEFORE INSERT): the `ipd_order_id` is `order_type='medication'`, `status='active'`,
same admission as the indent, admission `admitted|discharge_pending` (reuses the 111:559 logic). Raised codes:
`rx_order_not_medication`, `rx_order_not_active`, `rx_admission_not_active`.

`rx_dispense_indent(clinic, indent_id, lines JSONB, store_id, idem, actor)` **[F]**: lock indent and lines; for each
line: batch FEFO-checked, `ipd_dispense_out` movement, `dispensed_units += qty` (CHECK caps over-dispense), write
`ipd_charges (category 'pharmacy', source 'pharmacy', source_ref 'rx:dl:'||line.id, quantity, unit_price_paise,
tax_rate_bps 0, hsn_sac item.hsn, status posted|proposed per ipd_autopost)`, store `ipd_charge_id`; update indent
status; write register rows for controlled drugs. Idempotent by `idempotency_key` and by `uq_ipd_charges_source_ref`.

**Quantity > 999**: `ipd_charges.quantity` is `SMALLINT BETWEEN 1 AND 999` (111:388). A dispense line >999 units is
split into charges of ≤999 with `source_ref` suffix `:p2`, `:p3` (rare: IV fluids by ml are sold by bottle).

## 7. Transfers and stock audits (140 — RX-M7)
```sql
CREATE TABLE rx_transfers (id, clinic_id, from_store_id, to_store_id, transfer_number GENERATED ('STR-'),
  status TEXT CHECK (status IN ('draft','dispatched','received','partially_received','cancelled')),
  dispatched_by, dispatched_at, received_by, received_at, notes, UNIQUE (clinic_id, id),
  CHECK (from_store_id <> to_store_id));
CREATE TABLE rx_transfer_lines (id, clinic_id, transfer_id, item_id, batch_id, sent_units INTEGER CHECK (>0),
  received_units INTEGER NULL CHECK (received_units BETWEEN 0 AND sent_units), discrepancy_reason NULL,
  UNIQUE (clinic_id, id));
-- in-transit stock lives in a virtual store per clinic: store_type 'transit' is NOT added; instead
-- transfer_out debits the source at dispatch and transfer_in credits the target at receipt; the difference
-- (sent − received) is written off with damage_writeoff_out against the SOURCE with the discrepancy reason.
CREATE TABLE rx_stock_audits (id, clinic_id, store_id, audit_number GENERATED ('AUD-'), scope TEXT CHECK (scope IN ('full','rack','item_list','controlled')),
  status TEXT CHECK (status IN ('counting','review','posted','cancelled')), freeze_sales BOOLEAN NOT NULL DEFAULT false,
  started_by, started_at, posted_by, posted_at, UNIQUE (clinic_id, id));
CREATE TABLE rx_stock_audit_lines (id, clinic_id, audit_id, batch_id, item_id,
  system_units_at_start INTEGER NOT NULL, counted_units INTEGER NULL CHECK (counted_units >= 0),
  movement_delta_since_start INTEGER NOT NULL DEFAULT 0,   -- sales during count (if not frozen)
  variance_units INTEGER GENERATED ALWAYS AS (counted_units - system_units_at_start - movement_delta_since_start) STORED,
  variance_reason TEXT NULL, UNIQUE (clinic_id, id), UNIQUE (audit_id, batch_id));
```
Trigger `rx_sm_audit_delta` (AFTER INSERT on movements): when a `counting` audit exists for `NEW.store_id` and has
a line for `NEW.batch_id`, add `NEW.qty_units` to that line's `movement_delta_since_start` (indexed lookup on
`rx_stock_audits (clinic_id, store_id) WHERE status = 'counting'`; no audit → one index probe, no write). When
`freeze_sales`, `rx_complete_sale` / `rx_dispense_indent` refuse that store with `rx_store_frozen_for_audit`.

`rx_post_audit` **[F]**: requires `variance_reason` on every non-zero line; writes `adjust_in`/`adjust_out` movements;
variance value above `rx_settings.audit_approval_paise` (default ₹5,000) needs `RX_ADMIN` second approver
(`approved_by ≠ started_by`).

## 8. Statutory registers (141 — RX-M8)
```sql
CREATE TABLE rx_register_entries (                 -- [A][F] tamper-evident, hash-chained per clinic×register
  id UUID PK, clinic_id UUID NOT NULL,
  register TEXT NOT NULL CHECK (register IN ('H','H1','X','narcotic','psychotropic')),
  seq BIGINT NOT NULL,                             -- gapless per (clinic, register)
  entry_date DATE NOT NULL,                        -- IST
  direction TEXT NOT NULL CHECK (direction IN ('in','out','return','writeoff','adjust')),
  item_id, batch_id, store_id, qty_units INTEGER NOT NULL,
  balance_after_units INTEGER NOT NULL,            -- per item×store, controlled registers
  source_table TEXT NOT NULL, source_id UUID NOT NULL,
  patient_snapshot JSONB NULL,                     -- name, age, gender, MRN, address line (H1/X/narcotic)
  prescriber_snapshot JSONB NULL,                  -- name, registration_number, council, address (required for out)
  supplier_snapshot JSONB NULL,                    -- name, licence, invoice no/date (for in)
  dispensed_by_name TEXT NOT NULL, pharmacist_reg_no TEXT NULL,
  prev_hash BYTEA NULL, row_hash BYTEA NOT NULL,    -- sha256(prev_hash || canonical jsonb of the row)
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (clinic_id, id), UNIQUE (clinic_id, register, seq),
  CHECK (direction <> 'out' OR prescriber_snapshot ? 'registration_number'),
  CHECK (register NOT IN ('H1','X','narcotic') OR direction <> 'out' OR patient_snapshot ? 'name')
);
```
Written only by `rx_register_append` (called from `rx_complete_sale`, `rx_dispense_indent`, `rx_post_grn`, returns,
audits, write-offs) under `pg_advisory_xact_lock(hashtext('rx_reg:'||clinic||register))`. `rx_register_verify(clinic,
register)` recomputes the chain → `{ok, first_bad_seq}`; nightly job alerts on break. Schedule H rows record the
sale (bill number, item, batch, qty, prescriber) — the H1/X/narcotic rows add the patient snapshot and running balance.

## 9. Refills and alerts (142 — RX-M9)
```sql
CREATE TABLE rx_refill_plans (id, clinic_id, patient_id, family_member_id NULL, item_id, sale_item_id,
  opd_prescription_item_id NULL, units_per_day NUMERIC(6,2) NOT NULL CHECK (units_per_day > 0),
  covered_until DATE NOT NULL, remind_on DATE NOT NULL,
  status TEXT CHECK (status IN ('scheduled','sent','requested','fulfilled','expired','opted_out','cancelled')),
  last_message_id TEXT NULL, UNIQUE (clinic_id, id),
  UNIQUE (clinic_id, sale_item_id));                -- one plan per sold line
CREATE TABLE rx_refill_requests (id, clinic_id, plan_id, patient_id, channel TEXT CHECK (channel IN ('whatsapp','counter','voice')),
  fulfilment TEXT CHECK (fulfilment IN ('pickup','home_delivery')), status TEXT CHECK (status IN ('new','verified','packed','completed','rejected')),
  rejected_reason NULL, sale_id NULL, created_at, UNIQUE (clinic_id, id));
CREATE TABLE rx_alerts (id, clinic_id, store_id, item_id NULL, batch_id NULL,
  kind TEXT CHECK (kind IN ('low_stock','near_expiry','expired_on_hand','ledger_mismatch','register_chain_break','cold_chain')),
  alert_date DATE NOT NULL, payload JSONB NOT NULL, acknowledged_by NULL, acknowledged_at NULL,
  UNIQUE (clinic_id, id), UNIQUE (clinic_id, kind, store_id, item_id, batch_id, alert_date));   -- one per day
```
`units_per_day` is derived **only** from the closed frequency map (`OD=1, BD=2, TDS=3, QID=4, HS=1`) × units per
dose parsed from `dosage` matching `^\d+(\.5)?$`; any other frequency/dosage → no plan is created (no guessing).
Plans are never created for `schedule IN ('H1','X','narcotic','psychotropic')` or `is_chronic = false`.

## 10. Helpers (134)
`rx_purging(clinic)`, `rx_in_tx(clinic)`, `rx_raise(code)`, `rx_ist_date(ts)`, `rx_normalize(text)` (lower, strip
punctuation, collapse spaces, `mg`/`ml` spacing), `rx_unit_price(mrp_pack, pack_size, units)` = `(mrp_pack::BIGINT ×
units + pack_size/2) / pack_size` (half-up), `rx_gst_split_inclusive(gross, rate_bps)` → `(taxable, cgst, sgst)`
(ADR-RX-08).

## 11. `rx_purge_clinic(p_clinic_id)` (defined in 134, redefined by each later RX migration to cover its tables)
```sql
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
  PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
  PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
  UPDATE ipd_med_administrations SET rx_dispense_line_id = NULL                -- 111 guard honours ipd purge GUC;
   WHERE clinic_id = p_clinic_id AND rx_dispense_line_id IS NOT NULL;         -- set kriya.ipd_purge_clinic too
  DELETE FROM rx_alerts WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_refill_requests WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_refill_plans WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_register_entries WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('register_entries', n);
  DELETE FROM rx_stock_audit_lines WHERE clinic_id = p_clinic_id;  DELETE FROM rx_stock_audits WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_transfer_lines WHERE clinic_id = p_clinic_id;     DELETE FROM rx_transfers WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_dispense_lines WHERE clinic_id = p_clinic_id;     DELETE FROM rx_dispenses WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_indent_lines WHERE clinic_id = p_clinic_id;       DELETE FROM rx_ipd_indents WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_credit_notes WHERE clinic_id = p_clinic_id;       DELETE FROM rx_return_lines WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_returns WHERE clinic_id = p_clinic_id;            DELETE FROM rx_rx_fulfilments WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_sale_payments WHERE clinic_id = p_clinic_id;      DELETE FROM rx_sale_items WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_sales WHERE clinic_id = p_clinic_id;              GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('sales', n);
  DELETE FROM rx_supplier_return_lines WHERE clinic_id = p_clinic_id; DELETE FROM rx_supplier_returns WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_grn_lines WHERE clinic_id = p_clinic_id;          DELETE FROM rx_grns WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_po_lines WHERE clinic_id = p_clinic_id;           DELETE FROM rx_purchase_orders WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_suppliers WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_stock_balances WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_stock_movements WHERE clinic_id = p_clinic_id;    GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('movements', n);
  DELETE FROM rx_batches WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_item_changes WHERE clinic_id = p_clinic_id;       DELETE FROM rx_item_aliases WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_item_barcodes WHERE clinic_id = p_clinic_id;      DELETE FROM rx_items WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_generics WHERE clinic_id = p_clinic_id;           DELETE FROM rx_manufacturers WHERE clinic_id = p_clinic_id;
  DELETE FROM rx_stores WHERE clinic_id = p_clinic_id;
  UPDATE clinics SET rx_state = 'OFF', rx_settings = '{}'::jsonb WHERE id = p_clinic_id;
  PERFORM set_config('kriya.rx_purge_clinic', '', true);
  PERFORM set_config('kriya.rx_tx', '', true);
  RETURN v;
END $$;
```
`ipd_charges` rows with `source='pharmacy'` are **not** deleted here — they belong to the patient bill and go with
`ipd_purge_clinic`, which the platform calls next (AUDIT A38). Register purge is permitted **only** by the platform
owner purge (account deletion) and is logged in `platform_audit_log` with row counts; statutory retention is the
owner's contractual duty before deletion (SECURITY §6).

## 12. `TENANT_OWNED_TABLES` additions (36)
`rx_stores`, `rx_manufacturers`, `rx_generics`, `rx_items`, `rx_item_barcodes`, `rx_item_aliases`, `rx_item_changes`,
`rx_batches`, `rx_stock_movements`, `rx_stock_balances`, `rx_suppliers`, `rx_purchase_orders`, `rx_po_lines`,
`rx_grns`, `rx_grn_lines`, `rx_supplier_returns`, `rx_supplier_return_lines`, `rx_sales`, `rx_sale_items`, `rx_sale_payments`, `rx_rx_fulfilments`, `rx_returns`,
`rx_return_lines`, `rx_credit_notes`, `rx_ipd_indents`, `rx_indent_lines`, `rx_dispenses`, `rx_dispense_lines`,
`rx_transfers`, `rx_transfer_lines`, `rx_stock_audits`, `rx_stock_audit_lines`, `rx_register_entries`,
`rx_refill_plans`, `rx_refill_requests`, `rx_alerts` — each registered in the task that creates it; DG1 asserts the
set equals `SELECT tablename FROM pg_tables WHERE tablename LIKE 'rx\_%'` (36 names; `rx_rx_fulfilments` lands in
138, the supplier-return pair in 137). `rx_rx_fulfilments(id, clinic_id, opd_prescription_item_id, sale_item_id,
units, created_at)` is **[A][F]** with `UNIQUE (clinic_id, sale_item_id)`.

## 13. RLS parity
Each migration ends with the 111:1334 block: `ENABLE` + `FORCE ROW LEVEL SECURITY`, `service_role_all_<t>` policy,
`REVOKE ALL … FROM anon, authenticated`. RLS is defence in depth only (AGENTS.md §1.1); application scoping is the
control.
