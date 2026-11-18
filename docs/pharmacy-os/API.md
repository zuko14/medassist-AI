# Kriya Pharmacy OS — API Map (`/admin/rx/*`)

> Part of the Pharmacy OS set: start at [status.md](status.md). Router `app/routers/rx.py`. Workflows that call these
> routes: [WORKFLOWS.md](WORKFLOWS.md). Permissions: [SECURITY.md](SECURITY.md) §3.

## 0. Conventions
- **Auth & order of checks** (every route, in this order, before any data read):
  1. `verify_credentials` (session) → 401.
  2. Permission (`holds_rx_permission(user, PERM)`) → 403 `Missing permission: RX_…`.
  3. `_rx_scope(user, clinic_id)`: `is_valid_clinic_scope` (AUDIT A31), tenant match (super_admin must pass an explicit
     clinic), `rx_access(clinic)` not None → 403 `pharmacy_not_enabled`.
  4. Wind-down rule: routes tagged **new-work** refuse in `WIND_DOWN` → 409 `pharmacy_winding_down`.
  5. Store scope: `store_id` (body/query/derived from the row) must be in `allowed_store_ids(user)` → 404 (not 403:
     do not reveal existence).
- **Errors.** SQL `rx_<code>` → HTTP via `RX_ERROR_MAP` (e.g. `rx_insufficient_stock`/23514 → 409,
  `rx_batch_expired` → 409, `rx_batch_blocked` → 409, `rx_movement_via_function_only` → 500 + alert,
  `rx_order_not_active` → 409, `rx_controlled_requires_inhouse_rx` → 422). Response body `{detail, code, line_no?}`.
  Never a stack trace (CLAUDE.md).
- **Money** paise integers; **quantities** base units; dates `YYYY-MM-DD` IST; expiry input accepts `MM/YYYY`.
- **Idempotency.** Every state-changing POST that creates a numbered document accepts `idempotency_key` (8–80 chars);
  a replay returns the original with `"replayed": true` and HTTP 200.
- **Audit.** Every write calls `log_admin_action(user, request, action, entity, id, meta)` (existing helper); meta
  never contains patient phone numbers (masked) or full addresses. Actions are listed per route as *audit:*.
- **Pagination.** `?limit` (≤200, default 50) & `?cursor` (opaque, created_at+id).

## 1. Setup & settings (P1)
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/rx/setup` | RX_READ or RX_ADMIN | settings, store list (scoped), branches, IPD wards (names, only when IPD is on), clinic-wide checklist (stores, drug master, GSTIN, settings). |
| GET / PATCH | `/admin/rx/settings` | RX_READ / RX_ADMIN | PATCH with `If-Match: <settings_digest>` (CAS, 409 on stale); validates keys (DATA-MODEL §1). *audit: rx_settings_update* |
| GET | `/admin/rx/stores` | RX_READ | scoped list |
| POST / PATCH | `/admin/rx/stores`, `/admin/rx/stores/{store_id}` | RX_STORE_MANAGE or RX_ADMIN | P1: code/type/branch/ward set once (PATCH: name, parent, licence, GRN flag, active); ward of this clinic (and branch); branch login only its branch; re-activating is new work (409 in WIND_DOWN); deactivate refused with stock on hand from P2. *audit* |
| GET | `/admin/rx/deactivation-preview` | RX_ADMIN | open drafts, indents, transfers, audits (also used by platform toggle) |
| GET | `/admin/me` (existing) | — | + `rx_access`, `rx_state`, `rx_store_ids`, `rx_perms` |

Platform (existing router `app/routers/platform.py`): `opd_enabled=false|null` → 409 while `rx_state ≠ 'OFF'` (D7);
`PATCH /platform/clinics/{id}/features {pharmacy_enabled}` →
eligibility 400 (`opd_enabled` required) → provision default store → disable with non-empty preview → 409 → `confirm`
→ `WIND_DOWN`. `GET /platform/clinics/{id}/rx/deactivation-preview`.

## 2. Catalogue (P2)
| Method | Path | Perm | Request → Response |
|:-|:-|:-|:-|
| GET | `/admin/rx/items?q&generic_id&schedule&active` | RX_READ | `[{id,item_code,brand_name,generic:{name,strength,dosage_form},schedule,pack_size,base_unit,tax_rate_bps,hsn_code,stock_units?}]` |
| GET | `/admin/rx/items/{id}` | RX_READ | item + barcodes + aliases + change log |
| POST | `/admin/rx/items` | RX_ADMIN / RX_CATALOGUE | `{item_code,brand_name,generic_id|generic:{…},manufacturer_id,item_kind,schedule,base_unit,pack_size,pack_label,allow_loose,hsn_code,tax_rate_bps,reorder_level_units,reorder_qty_units,is_chronic,is_cold_chain,rack_location}` → 201 item. *audit: rx_item_create* |
| PATCH | `/admin/rx/items/{id}` | RX_ADMIN / RX_CATALOGUE | immutables → 409 `rx_item_field_locked:<field>`; rate/HSN change requires `reason`. *audit* |
| POST | `/admin/rx/items/{id}/barcodes` | RX_ADMIN / RX_CATALOGUE | `{barcode,symbology}`; 409 if barcode on another item |
| GET/POST | `/admin/rx/generics`, `/admin/rx/manufacturers` | RX_READ / RX_CATALOGUE (or RX_ADMIN) | List or create generic/manufacturer records |
| POST | `/admin/rx/items/import/dry-run` | RX_ADMIN / RX_CATALOGUE | CSV/JSON rows → `{valid,errors:[{row,field,msg}],would_create,would_update,sample:[...]}` |
| POST | `/admin/rx/items/import/apply` | RX_ADMIN / RX_CATALOGUE | CSV/JSON rows → applied idempotently on `item_code` within transaction. *audit: rx_item_import* |
| GET | `/admin/rx/items/resolve?q=&limit=` | RX_READ | deterministic candidate matches + substitutes on same generic |

## 3. Stock (P2–P3)
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/rx/stock?store_id&q&below_reorder&schedule` | RX_READ | `[{item, qty_units, packs_loose:{packs,loose}, nearest_expiry, value_paise}]` |
| GET | `/admin/rx/batches?item_id=&store_id=&status=` | RX_READ | FEFO-ranked batches with `expiry_status: ok|near|short_shelf|expired|blocked` |
| GET | `/admin/rx/expiry?within_days&store_id` | RX_READ | near-expiry and expired-on-hand batches |
| GET | `/admin/rx/reorder?store_id` | RX_READ | items below reorder level with suggested packs and deficit |
| POST | `/admin/rx/batches/{id}/block` | RX_ADMIN / RX_ADJUST | `{reason}` recall/quarantine. *audit: rx_batch_block* |
| POST | `/admin/rx/batches/{id}/unblock` | RX_ADMIN / RX_ADJUST | `{reason}`. *audit* |
| POST | `/admin/rx/write-offs` | RX_ADJUST / RX_ADMIN | `{store_id, lines:[{batch_id,qty_units,kind:'expiry'|'damage',reason}]}` → movements. *audit* |
| POST | `/admin/rx/stock/opening-import` | RX_ADJUST / RX_ADMIN | CSV / rows with `dry_run: true|false`; allowed only before the store's first non-opening movement. *audit* |

## 4. Purchase (P3)
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET/POST/PATCH | `/admin/rx/suppliers[/{id}]` | RX_READ / RX_PURCHASE | GSTIN checksum validated (`gst_billing.valid_gstin`) |
| GET | `/admin/rx/purchase-orders?status&supplier_id&store_id` | RX_READ | store-scoped list with supplier name |
| GET | `/admin/rx/purchase-orders/{id}` | RX_READ | header + lines (item incl. `tax_rate_bps`) + supplier |
| POST | `/admin/rx/purchase-orders` | RX_PURCHASE | `{store_id,supplier_id,expected_on,lines:[{item_id,ordered_packs,free_packs,expected_rate_pack_paise}]}` (draft) |
| PATCH | `/admin/rx/purchase-orders/{id}` | RX_PURCHASE | draft only; `lines` replaces all lines |
| POST | `/admin/rx/purchase-orders/{id}/send` | RX_PURCHASE + RX_ADMIN *or* PHARMACY_MANAGER role | assigns `PO-`. *audit* |
| POST | `/admin/rx/purchase-orders/{id}/cancel` | RX_PURCHASE | `{reason}` |
| GET | `/admin/rx/purchase-orders/{id}.pdf` | RX_READ | |
| GET/POST/PATCH | `/admin/rx/grns[/{id}]` | RX_READ / RX_PURCHASE | draft header + lines; line response includes computed taxable/tax/landed and `warnings[]` |
| POST | `/admin/rx/grns/{id}/post` | RX_PURCHASE | `{idempotency_key}` → `rx_post_grn`. 409 `rx_grn_total_mismatch` with diff. *audit: rx_grn_post* |
| POST | `/admin/rx/grns/{id}/void` | RX_ADMIN | `{reason}`; 409 `rx_grn_stock_consumed`. *audit* |
| GET | `/admin/rx/grns/{id}/labels.pdf?copies=per_unit|per_pack|1` | RX_READ | |
| GET/POST | `/admin/rx/supplier-returns[/{id}]` | RX_READ / RX_PURCHASE | `{store_id,supplier_id,reason,notes,lines:[{batch_id,qty_units}]}`; item, rate and GST are filled from the batch's first GRN line (else batch PTR + item GST); detail carries `debit_note {taxable,tax,total}` |
| POST | `/admin/rx/supplier-returns/{id}/dispatch` | RX_PURCHASE | `rx_supplier_return`; debit note PDF link. *audit* |
| PATCH | `/admin/rx/supplier-returns/{id}/credit` | RX_PURCHASE | `{credit_received_paise, credit_ref}` |
| GET | `/admin/rx/gs1?code=` | RX_READ / RX_PURCHASE / RX_DISPENSE | A3 / RX-71: parse a GS1 DataMatrix / pharma QR (`]d2`, `]Q3`, GS, or `(01)…` form) → `{gtin,batch_no,expiry_date,mfg_date,serial,item}`; 422 `rx_gs1_invalid` |

P3 notes (as built, migration 137): the PO `send` needs RX_PURCHASE **and** RX_ADMIN (a Pharmacy Manager holds both).
GRN `post` is idempotent per GRN (a retry returns `replayed: true`); the posted `round_off_paise` is the invoice
total minus the computed total and must be within ±99. Freight is split over lines by taxable value (largest
remainder) into landed cost; GST on freight is not modelled. Write paths for supplier returns stay open in
WIND_DOWN (clearing stock); new suppliers, POs and GRNs are new work (409).

## 5. POS (P4)
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/rx/pos/inbox?store_id&since` | RX_DISPENSE | Rx Inbox (ADVANCED-FEATURES A1) |
| POST | `/admin/rx/sales/{id}/park` / `/recall` | RX_DISPENSE | A2 |
| POST | `/admin/rx/overrides` | (approver credentials in body) | A9: `{action, sale_id?, return_id?, amount_paise?, reason, approver_username, approver_password}` → `{override_id, expires_at}`; approver must hold RX_ADMIN or be PHARMACY_MANAGER and differ from requester. Rate-limited 5/min per user. *audit: rx_override_grant* |
| GET | `/admin/rx/sales/{id}/labels.pdf` | RX_READ | dispensing labels (A4) |
| GET | `/admin/rx/pos/lookup?q=` | RX_DISPENSE | q = token / MRN / phone / Rx QR id / bill no. → `{prescriptions:[{id,doctor,signed_at,lines:[{id,drug_name,strength,formulation,frequency,duration_days,fulfilled_units,pending}]}], patient}` |
| POST | `/admin/rx/sales` | RX_DISPENSE | `{store_id, sale_kind, opd_prescription_id?, patient_id?, walkin_name?, external_rx?:{prescriber_name,registration_number,council,rx_date}}` → draft with auto-mapped lines (W2). *new-work* |
| GET | `/admin/rx/sales/{id}` | RX_READ | draft or completed with lines, taxes, payments, returns |
| PUT | `/admin/rx/sales/{id}/lines` | RX_DISPENSE | full replace of draft lines `[{item_id,batch_id?,qty_units,opd_prescription_item_id?,substituted_from_item_id?,substitution_reason?,fefo_override_reason?,line_discount_paise?}]` → server re-prices, picks FEFO when `batch_id` absent, returns totals + `warnings[]` (short shelf, schedule needs) |
| PATCH | `/admin/rx/sales/{id}` | RX_DISPENSE | `{discount_paise?, buyer_gstin?}`; discount cap → 403 `rx_discount_over_limit` |
| POST | `/admin/rx/sales/{id}/complete` | RX_DISPENSE | `{payments:[{mode,amount_paise,reference?}], shift_id?, idempotency_key, send_whatsapp_bill?}` → completed sale + `receipt_url`. *audit: rx_sale_complete* |
| DELETE | `/admin/rx/sales/{id}` | RX_DISPENSE | draft only (no number, no stock) |
| POST | `/admin/rx/sales/{id}/void` | RX_ADMIN | same IST day, no returns, `{reason}` → reversing movements, register `return`, negative payments. *audit* |
| GET | `/admin/rx/sales/{id}/receipt?format=80mm|a5` | RX_READ | PDF / HTML for thermal |
| GET | `/admin/rx/sales?from&to&store_id&q&status` | RX_READ | history |
| POST | `/admin/rx/sales/{id}/returns` | RX_DISPENSE (+RX_ADMIN past window / restock opened) | `{lines:[{sale_item_id,qty_units,restock}],reason,refund_mode,shift_id?,idempotency_key}` → `PRT-`, refund/PCN. *audit* |
| GET | `/admin/rx/credit-notes/{id}.pdf` | RX_READ | |
| GET | `/admin/rx/pos/day-summary?store_id&date&shift_id` | RX_READ | sales, returns, by mode, by GST slab, by schedule, overrides; with `shift_id` also `cash_expected_paise` (= `rx_shift_expected_cash`, the `opd_close_shift` formula) |

P4 notes (as built, migration 138):
- `POST /sales` also takes `repeat_of_sale_id` (A5: same items, fresh FEFO, today's prices; scheduled items listed
  `needs_rx` and left out) and `pick: true` (A1 inbox pick → `picked_at/by`). The response carries `mapping[]`: per Rx
  line `{rx_item_id, text, candidates[≤5], mapped_item_id|null}`; a line is auto-mapped only for a confirmed alias, an
  exact brand / generic match, or a single clear candidate — never guessed. `sale_kind: 'staff'` needs RX_ADMIN;
  `ipd_discharge_meds` arrives with P5.
- `PUT /lines` does **not** accept `line_discount_paise`: the header discount (`PATCH discount_paise`) is spread over
  lines by gross (largest remainder) in SQL so GST is always computed on what the patient pays. A non-FEFO batch needs
  `fefo_override_reason` (setting `fefo_override_requires_reason`); a substitute must share the item's `generic_id`
  (422 `rx_substitute_not_same_generic`).
- `PATCH` with a discount above `max_discount_bps` is accepted on the draft and flagged `discount_needs_override`;
  completion then needs RX_ADMIN or an override (403 `rx_discount_over_limit`).
- Lookup: `q` = token (today), MRN, phone (10 digits), Rx id (printed QR), or `PHS-…` bill number; patients come
  with the A14 panel (last 10 purchases + allergies as recorded, no checking).
- Receipt `format=80mm` returns a same-origin HTML print view; `format=a5` a PDF. `send_whatsapp_bill` is accepted
  and ignored until the e-bill template lands (P8).
- `GET /admin/opd/collections/summary` gains `pharmacy_gross_paise`, `pharmacy_refunds_paise`, `pharmacy_net_paise`,
  `pharmacy_cash_paise`, `pharmacy_by_mode` — only for clinics with Pharmacy OS (absent otherwise).

## 6. IPD (P5) — additionally require `ipd_access(clinic) is not None` (ACTIVE or WIND_DOWN, VERIFICATION D6) and an active admission
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/rx/ipd/admissions/{id}/indentable` | IPD_NURSING or RX_DISPENSE | active medication orders + last dispensed + ward stock |
| POST | `/admin/rx/ipd/indents` | IPD_NURSING | `{admission_id, from_store_id, priority, lines:[{ipd_order_id,requested_units,item_id?}], idempotency_key}` → `IND-`. *audit: rx_indent_raise* |
| GET | `/admin/rx/ipd/indents?status&ward_id&priority` | RX_DISPENSE or IPD_NURSING (own ward) | queue |
| GET | `/admin/rx/ipd/indents/{id}` | same | |
| POST | `/admin/rx/ipd/indents/{id}/dispense` | RX_DISPENSE | `{store_id, lines:[{indent_line_id,item_id,batch_id?,qty_units,substituted_from_item_id?,substitution_reason?,not_available?}], idempotency_key}` → dispense + charges. *audit: rx_dispense* |
| POST | `/admin/rx/ipd/indents/{id}/cancel` | IPD_NURSING or RX_DISPENSE | `{reason}`; only undispensed lines |
| POST | `/admin/rx/ipd/dispenses/{id}/receive` | IPD_NURSING | nurse acknowledgment |
| GET | `/admin/rx/ipd/dispenses/{id}/slip.pdf` | RX_READ | |
| POST | `/admin/rx/ipd/ward-issue` | IPD_NURSING + store scope | `{admission_id, store_id, lines:[{ipd_order_id,item_id,batch_id?,qty_units}], idempotency_key}`. *audit* |
| POST | `/admin/rx/ipd/returns` | IPD_NURSING | `{admission_id, lines:[{dispense_line_id,qty_units}]}` (pending) |
| POST | `/admin/rx/ipd/returns/{id}/accept` | RX_DISPENSE | `{restock per line}` → void/re-issue or credit note. *audit* |
| GET | `/admin/rx/ipd/admissions/{id}/dispense-lines?order_id` | IPD_NURSING | for the MAR batch select (W4 step 6) |

P5 notes (as built, migration 139 — 9 routes, route pin 77):
- Built: `indentable`, `POST/GET indents`, `GET indents/{id}`, `dispense`, `cancel`, `dispenses/{id}/receive`,
  `dispenses/{id}/slip.pdf`, and **`POST /admin/rx/ipd/dispenses/{id}/returns`** (IPD_NURSING or RX_DISPENSE;
  `{lines:[{dispense_line_id,qty_units,restock}], reason, idempotency_key}`) — a one-step ward return in place of
  the `returns` + `returns/{id}/accept` pair: stock comes back (`ipd_return_in`, opened / expired → write-off), an
  unbilled charge is voided and re-issued for the units kept (`source_ref rx:dl:<line>:r:<return>`), a charge on an
  issued IPD bill gets an `opd_issue_credit_note` (a draft bill → 409, regenerate or issue it first). The idempotency
  key is the return id (uuid5), so a retry replays. Restocking H1 / X / narcotic needs RX_ADMIN.
- Deferred (not routed in P5): `ward-issue`, the two-step `returns/{id}/accept`, `admissions/{id}/dispense-lines`, and
  the MAR route change. `rx_link_mar` (SQL) + `rx_ipd.link_mar()` (best effort, never raises) are built and tested;
  wiring them into the MAR route edits an IPD handler, which P5 leaves untouched.
- `indentable` also returns `stores` (active non-ward stores in scope): a nurse holds no RX_READ for `/stores`.
  `default_units` = 24 h from the closed frequency map (oral solid doses), else one unit per dose, PRN → 1.
- Raising an indent is *new work* (refused in pharmacy WIND_DOWN); dispense, cancel, receive, slip and returns
  continue. All need `ipd_access` (IPD WIND_DOWN keeps dispensing to admitted patients, D6).
- Dispense lines without `batch_id` are FEFO-split from the dispensing store (a named later batch needs
  `fefo_override_reason`); `not_available: true` settles a line (also the way to close a line whose order stopped:
  dispensing it is 409 `rx_order_not_active`). Response carries `final_bill_exists` (D10) and `slip_url`.
- Price per base unit: MRP ÷ pack (half-up), or landed cost + `ipd_cost_plus_bps` capped at MRP; charges are
  `category/source 'pharmacy'`, posted or proposed per `ipd_autopost`, split at 999 units (`:p2`, `:p3` …).

Existing MAR route `POST /admin/ipd/admissions/{id}/administrations` (`app/routers/ipd.py:701`): `AdministrationIn`
gains optional `rx_dispense_line_id`; the handler **pops it** before `ipd.record_administration` (the body is inserted
verbatim, VERIFICATION A53), records the MAR unchanged, then calls `rx_link_mar` when RX is on. A wrong line → MAR
still saved, response `rx_link: "refused: <reason>"`. Pre-RX clients that omit the field see no change.

## 7. Transfers & audits (P6)
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET/POST/PATCH | `/admin/rx/transfers[/{id}]` | RX_READ / RX_STOCK | draft |
| POST | `/admin/rx/transfers/{id}/dispatch` | RX_STOCK (+RX_CONTROLLED for X/narcotic) | `STR-`, challan PDF. *audit* |
| POST | `/admin/rx/transfers/{id}/receive` | RX_STOCK or IPD_NURSING with target-store scope | `{lines:[{line_id,received_units,discrepancy_reason?}]}`. *audit* |
| POST | `/admin/rx/transfers/{id}/cancel` | RX_STOCK | draft only |
| POST | `/admin/rx/audits` | RX_STOCK | `{store_id, scope, item_ids?, rack?, freeze_sales}` |
| PUT | `/admin/rx/audits/{id}/counts` | RX_STOCK | `[{batch_id,counted_units}]` (batched, idempotent per batch) |
| POST | `/admin/rx/audits/{id}/review` | RX_STOCK | moves to review; returns variance list |
| POST | `/admin/rx/audits/{id}/post` | RX_STOCK (+second RX_ADMIN over threshold: `{approver_session}`) | *audit: rx_audit_post* |

P6 notes (as built, migration 140 — 18 routes, route pin 95):
- Permissions: the v1 `RX_STOCK` is `RX_TRANSFER` (transfers, par levels) and `RX_AUDIT` (audits), as decided in P1
  (SECURITY §2). Dispatch / receipt of X / narcotic / psychotropic lines also need `RX_CONTROLLED`, by two different
  people (409 `rx_controlled_two_person`; CHECK `rx_transfers_controlled_two_person`).
- Transfers: `GET /transfers?status&store_id` (+ `stores` the caller may use — a nurse holds no RX_READ for `/stores`),
  `POST` / `PATCH /transfers[/{id}]` `{from_store_id, to_store_id, lines:[{item_id, batch_id?, units}], notes?}` (no
  `batch_id` → FEFO split at the source; a PATCH replaces a draft), `…/dispatch` (STR- number, `challan_url`),
  `…/receive` `{lines:[{line_id, received_units, discrepancy_reason?}]}` (every line once; short needs a reason),
  `…/cancel {reason}` (draft only), `…/challan.pdf` (rack-sorted; a draft prints as the pick list).
- Short receipt: the short units go back to the source on paper (`transfer_in`) and leave it as `damage_writeoff_out`
  with the reason, so the loss is recorded against the source without debiting it twice.
- Store scope (RX-43): `allowed_store_ids` (branch-pinned logins reach their branch's stores; 404 outside). A nurse
  (IPD_NURSING, no RX grant) reaches only transfers INTO ward-type stores (`ward_substore`, `ot_tray`,
  `emergency_tray`) in scope, and their par levels. There is no staff→ward assignment in the schema yet, so two
  wards of one branch are not separated.
- Par levels (A10): `GET /par-levels?store_id` (par, on hand = sellable units, deficit, rack-sorted),
  `POST /par-levels {store_id, rows:[{item_id, par_units}]}` (0 removes), `POST /par-levels/replenish {store_id}` →
  `{transfer, short}`: a **draft** from the parent store (else the branch's central store) for positive deficits,
  FEFO; pressing it again refreshes the same par draft. The 06:00 IST daily job is not scheduled yet.
- Audits: also `GET /audits?status&store_id` and `POST /audits/{id}/cancel {reason}` (an abandoned count must never
  leave a store frozen). `POST /audits {store_id, scope: full|rack|item_list|controlled, rack?, item_ids?,
  freeze_sales}`; `GET /audits/{id}?show_system` (blind while counting; only RX_ADMIN may show the system quantity);
  `PUT …/counts [{batch_id, counted_units}]` (1–500, a re-send SETs; a batch outside the snapshot but in scope joins as
  found stock); `…/review` (409 `rx_audit_uncounted` until every line is counted — 0 for an empty shelf);
  `…/post {reasons:[{line_id, variance_reason}], approver_username?, approver_password?}` — above
  `audit_approval_paise` a second RX_ADMIN re-authenticates (not the starter, not the poster; rate-limited 5/min).
- Freeze: while an audit of a store is `counting|review` with `freeze_sales`, `sale_out` / `ipd_dispense_out` /
  `ward_issue_out` from that store are refused in the ledger itself (409 `rx_store_frozen_for_audit`).

## 8. Registers (P7)
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/rx/registers/{register}?from&to&store_id&item_id` | RX_REGISTER_EXPORT | register ∈ H, H1, X, narcotic, psychotropic |
| GET | `/admin/rx/registers/{register}/export.pdf` / `.csv` | RX_REGISTER_EXPORT | *audit: rx_register_export* (range, rows) |
| GET | `/admin/rx/registers/{register}/verify` | RX_REGISTER_EXPORT | `{ok, rows, first_bad_seq?}` |

P7 notes (as built, migration 141 — 6 routes, route pin 101):
- Permission: the v1 `RX_REGISTER_EXPORT` is `RX_REGISTERS` (P1, SECURITY §2; PHARMACIST and PHARMACY_MANAGER by role,
  or `RX_ADMIN`). `RX_READ` alone does **not** open a register: the rows carry patient names and addresses. The list and
  exports honour store scope (a branch login sees its stores' rows; `store_id` outside them → 404); verify is clinic-wide.
- `GET /registers/{register}?from&to&store_id&item_id&limit(1–500)&offset` → `{entries:[…+item, batch, store, receipt],
  has_more}`; hashes are never returned. `receipt` is the ledger sign (`direction` alone is coarse).
- `…/export.csv` (UTF-8 with BOM) and `…/export.pdf` (A4 landscape, wrapping cells, headings repeat, "page n of N",
  hospital, period, store + drug licence, chain status, generation time, pharmacist signature line). H / H1 / X /
  psychotropic: Seq, Date, Bill / Doc No., Entry, Patient Name & Address, Prescriber Name & Reg. No., Drug, Batch, Qty,
  Balance, Supplier (PDF folds it into the patient column), Pharmacist. Narcotic: Seq, Date, Doc No., Drug, Batch,
  Store, Opening Balance, Quantity Received, Quantity Issued, Balance, Patient / Supplier, Prescriber, Signature.
  Over 20,000 rows → 422 `rx_register_export_too_large`. *audit: rx_register_export* `{format, from, to, store_id,
  item_id, rows}`. No phone number appears in any export (the snapshots hold none).
- `…/verify` → `{ok, rows, first_bad_seq, verified_at}`.
- `GET /batches/{batch_id}/trace` (RX_ADMIN; A6 / RX-74) → `{batch, sales:[…, phone_masked], ipd:[…, ward, bed,
  mar_doses], transfers, on_hand, summary}`; 404 for another clinic's batch. *audit: rx_batch_trace*. The optional
  "Notify patients" (`rx_recall_v1`) is not built yet.
- `POST /dispenses/{dispense_line_id}/empties` (RX_CONTROLLED + IPD on; A11 / RX-79) `{units, returned_by}`: the caller
  receives; `returned_by` must be an active login of the clinic and a different person (409 `rx_empties_two_person`);
  never more than dispensed − returned (409 `rx_empties_exceed_dispensed`); Schedule X / narcotic / psychotropic only.
  *audit: rx_empties_received*. The "outstanding empties block re-dispense" override is not built yet.

## 9. Refills, alerts, dashboard (P8, P9)
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/rx/refills/requests?status` | RX_DISPENSE | queue |
| POST | `/admin/rx/refills/requests/{id}/verify` / `/pack` / `/reject` | RX_DISPENSE | pack → draft sale id; reject `{reason}` → fixed-text WhatsApp. *audit* |
| GET | `/admin/rx/refills/plans?patient_id&status` | RX_READ | |
| POST | `/admin/rx/refills/plans/{id}/cancel` | RX_DISPENSE | |
| GET | `/admin/rx/alerts?kind&store_id&ack=false` | RX_READ | |
| POST | `/admin/rx/alerts/{id}/ack` | RX_READ | |
| GET | `/admin/rx/dashboard?store_id&date` | RX_READ | sales today, margin (RX_ADMIN only), pending indents, near-expiry value, low-stock count, refill requests |
| GET | `/admin/rx/reports/{kind}.csv` | RX_ADMIN | kind ∈ sales_register, purchase_register, gst_summary, hsn_summary, stock_valuation, movement, expiry, margin |
| GET | `/admin/rx/expiry/return-plan?store_id` | RX_READ / RX_PURCHASE | A8 suggestion grouped by GRN supplier; drafts go through `POST /supplier-returns` |

P8 notes (as built, migration 142 — 9 routes, route pin 110; dashboard and reports remain P9 / P10):
- Permissions: the refill queue and plans read with `RX_READ` or `RX_DISPENSE`; verify / pack / reject / cancel need
  `RX_DISPENSE` (or `RX_ADMIN`); verify and pack are new work (refused in WIND_DOWN). Alerts: `RX_READ`; a store alert
  outside the login's stores is 404, clinic-wide integrity alerts are visible to every reader.
- `POST /sales/{id}/complete` takes `refill_opt_in` (the pharmacist's tick, X16) and returns `refill_plans` (count).
  A completed draft that a refill request packed marks the request `completed` and the plan `fulfilled`.
- `…/verify`: the plan's original OPD prescription must still be `signed` and within `rx_validity_days` (409
  `rx_refill_rx_invalid` / `rx_refill_rx_expired` → reject instead). `…/pack {store_id}`: creates a **draft** OPD-Rx
  sale against that prescription with the plan's item and the originally sold quantity, FEFO batch; nothing is
  dispensed until the counter completes it. A refill beyond what the prescription allows needs the A9 manager override
  (`rx_over_fulfil`) at completion, as for any over-fulfilment. `…/reject {reason, notify_patient}`: plan cancelled;
  the fixed text "…Please consult your doctor for a new prescription." is sent when `notify_patient`. Every step is a
  compare-and-set on the request status (409 `rx_refill_request_state`). *audit* on each.
- `GET /alerts?kind&store_id&ack` (`ack` default false = open). `POST /alerts/{id}/ack` needs a named login (403
  otherwise); a second ack is a no-op. Kinds: low_stock, near_expiry, expired_on_hand, ledger_mismatch,
  register_chain_break, cold_chain (reserved), stat_indent.
- Jobs (scheduler.py, `distributed_job_lock`, IST): `rx_alerts_scan` 02:40 (every clinic not OFF; ledger mismatch and
  register chain break also write a `[P1]` admin notification) and `rx_refill_reminders` 09:30 (ACTIVE clinics with
  `refill_enabled`, the clinic's own WhatsApp credentials, `accepts_engagement`, template `rx_refill_due_v1` with
  `rx_refill:<plan>` / `rx_refill_off:<plan>`; three failed sends → `expired`). A STAT indent raises an alert and a
  notification at once. The A8 monthly drafting job is not scheduled: the planner is on demand.

## 10. Kriya Ketu read endpoints (P9)
Ketu reuses the routes above in-process with the speaker's session. One extra read:
`GET /admin/rx/stock/answer?q=` (RX_READ or IPD_NURSING) → `{matches:[{item, generic, store, qty_units, packs_loose,
nearest_expiry, substitutes_in_stock:[{brand, qty}]}], resolved_by:'barcode'|'alias'|'trgm'}` — used for "Is Augmentin
625 in stock?" (KRIYA-KETU.md).

## 11. Route count guard
DG2 asserts the `/admin/rx` route count (pinned per phase; the number is updated in the same commit that adds a route
and recorded in status §9) in `tests/test_rx_feature_gate.py` and that every route is
in the adversarial matrix (`tests/test_rx_route_matrix.py`). A new route without a matrix row fails CI.
