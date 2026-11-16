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
| POST | `/admin/rx/items` | RX_ADMIN | `{item_code,brand_name,generic_id|generic:{…},manufacturer_id,item_kind,schedule,base_unit,pack_size,pack_label,allow_loose,hsn_code,tax_rate_bps,reorder_level_units,reorder_qty_units,is_chronic,is_cold_chain,rack_location}` → 201 item. *audit: rx_item_create* |
| PATCH | `/admin/rx/items/{id}` | RX_ADMIN | immutables → 409 `rx_item_field_locked:<field>`; rate/HSN change requires `reason`. *audit* |
| POST | `/admin/rx/items/{id}/barcodes` | RX_ADMIN | `{barcode,symbology}`; 409 if barcode on another item |
| DELETE | `/admin/rx/items/{id}/barcodes/{bid}` | RX_ADMIN | |
| GET/POST/PATCH | `/admin/rx/generics`, `/admin/rx/manufacturers` | RX_READ / RX_ADMIN | |
| POST | `/admin/rx/items/import?dry_run=true` | RX_ADMIN | CSV/XLSX (columns ROLLOUT §4.2) → `{valid,errors:[{row,field,msg}],would_create,would_update}`; `dry_run=false` applies idempotently on `item_code`. *audit: rx_item_import* |
| GET | `/admin/rx/items/resolve?text=&strength=&form=` | RX_READ | mapping candidates `[{item_id,score,source:'alias'|'barcode'|'trgm'}]` (≤5). Deterministic. |
| GET | `/admin/rx/items/{id}/substitutes` | RX_READ | same `generic_id`, active, in stock in `store_id` |

## 3. Stock (P2–P3)
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/rx/stock?store_id&q&below_reorder&schedule` | RX_READ | `[{item, qty_units, packs_loose:{packs,loose}, nearest_expiry, value_paise}]` |
| GET | `/admin/rx/stock/{item_id}/batches?store_id` | RX_READ | FEFO-ranked batches with `expiry_status: ok|near|short_shelf|expired|blocked` |
| GET | `/admin/rx/ledger?store_id&item_id&batch_id&from&to&type` | RX_READ | movements with running balance |
| GET | `/admin/rx/expiry?within_days&store_id` | RX_READ | near-expiry and expired-on-hand |
| GET | `/admin/rx/reorder?store_id` | RX_PURCHASE | items below reorder level with suggested packs and last supplier |
| POST | `/admin/rx/batches/{id}/block` | RX_ADMIN | `{reason}` recall/quarantine. *audit: rx_batch_block* |
| POST | `/admin/rx/batches/{id}/unblock` | RX_ADMIN | `{reason}`. *audit* |
| POST | `/admin/rx/writeoffs` | RX_STOCK | `{store_id, lines:[{batch_id,qty_units,kind:'expiry'|'damage',reason}]}` → movements. *audit* |
| POST | `/admin/rx/opening-stock?dry_run=` | RX_ADMIN | CSV (ROLLOUT §4.3); allowed only before the store's first non-opening movement. *audit* |

## 4. Purchase (P3)
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET/POST/PATCH | `/admin/rx/suppliers[/{id}]` | RX_READ / RX_PURCHASE | GSTIN checksum validated (`gst_billing.valid_gstin`) |
| GET | `/admin/rx/purchase-orders?status&supplier_id` | RX_READ | |
| POST | `/admin/rx/purchase-orders` | RX_PURCHASE | `{store_id,supplier_id,expected_on,lines:[{item_id,ordered_packs,free_packs,expected_rate_pack_paise}]}` (draft) |
| PATCH | `/admin/rx/purchase-orders/{id}` | RX_PURCHASE | draft only |
| POST | `/admin/rx/purchase-orders/{id}/send` | RX_PURCHASE + RX_ADMIN *or* PHARMACY_MANAGER role | assigns `PO-`. *audit* |
| POST | `/admin/rx/purchase-orders/{id}/cancel` | RX_PURCHASE | `{reason}` |
| GET | `/admin/rx/purchase-orders/{id}.pdf` | RX_READ | |
| GET/POST/PATCH | `/admin/rx/grns[/{id}]` | RX_READ / RX_PURCHASE | draft header + lines; line response includes computed taxable/tax/landed and `warnings[]` |
| POST | `/admin/rx/grns/{id}/post` | RX_PURCHASE | `{idempotency_key}` → `rx_post_grn`. 409 `rx_grn_total_mismatch` with diff. *audit: rx_grn_post* |
| POST | `/admin/rx/grns/{id}/void` | RX_ADMIN | `{reason}`; 409 `rx_grn_stock_consumed`. *audit* |
| GET | `/admin/rx/grns/{id}/labels.pdf?copies=per_unit|per_pack|1` | RX_READ | |
| GET/POST | `/admin/rx/supplier-returns[/{id}]` | RX_READ / RX_PURCHASE | |
| POST | `/admin/rx/supplier-returns/{id}/dispatch` | RX_PURCHASE | `rx_supplier_return`; debit note PDF link. *audit* |
| PATCH | `/admin/rx/supplier-returns/{id}/credit` | RX_PURCHASE | `{credit_received_paise, credit_ref}` |

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
| GET | `/admin/rx/pos/day-summary?store_id&date` | RX_READ | sales, returns, by mode, by GST slab, by schedule |

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

## 8. Registers (P7)
| Method | Path | Perm | Notes |
|:-|:-|:-|:-|
| GET | `/admin/rx/registers/{register}?from&to&store_id&item_id` | RX_REGISTER_EXPORT | register ∈ H, H1, X, narcotic, psychotropic |
| GET | `/admin/rx/registers/{register}/export.pdf` / `.csv` | RX_REGISTER_EXPORT | *audit: rx_register_export* (range, rows) |
| GET | `/admin/rx/registers/{register}/verify` | RX_REGISTER_EXPORT | `{ok, rows, first_bad_seq?}` |

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

## 10. Kriya Ketu read endpoints (P9)
Ketu reuses the routes above in-process with the speaker's session. One extra read:
`GET /admin/rx/stock/answer?q=` (RX_READ or IPD_NURSING) → `{matches:[{item, generic, store, qty_units, packs_loose,
nearest_expiry, substitutes_in_stock:[{brand, qty}]}], resolved_by:'barcode'|'alias'|'trgm'}` — used for "Is Augmentin
625 in stock?" (KRIYA-KETU.md).

## 11. Route count guard
DG2 asserts the `/admin/rx` route count (pinned per phase; the number is updated in the same commit that adds a route
and recorded in status §9) in `tests/test_rx_feature_gate.py` and that every route is
in the adversarial matrix (`tests/test_rx_route_matrix.py`). A new route without a matrix row fails CI.
