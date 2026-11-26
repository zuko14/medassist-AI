# Kriya Revenue Guard — Nightly Unbilled-Charge Check (lane **RI**)

> Gap #3 of the product review: "small to build on top of the existing unbilled-orders feature, and easy to show in
> rupees". Tracker: [status.md](status.md). Read-only over billing and clinical tables; writes only `ri_*` tables.

## 1. Why
Indian hospitals lose money on work that was done but never billed: a test collected before the bill was issued, an
IPD charge left `proposed`, a discharge billed before late charges posted, a consultation seen without an invoice.
Kriya already has every fact needed — nothing looks at them together every night and puts a rupee figure on it.

## 2. What exists (evidence)
| Fact | Evidence |
|:-|:-|
| OPD/IPD orders carry a price snapshot; billing is optional before collection (`ordered → (billed) → sample_collected …`) | `migrations/115_investigation_and_procedure_orders.sql:13`; `opd_orders.unit_price_paise` |
| "Live bill line per order" lookup | `app/services/orders.py:146` (`_billing_by_order`) |
| "Add all to invoice" for a visit's unbilled orders | `app/services/orders.py:170` (`bill_pending_orders`) |
| IPD charges `proposed → posted → (invoice_id) / void`, `total_paise` generated | `migrations/111_ipd_os_core.sql:380` |
| Admissions `admitted / discharge_pending / discharged` | `migrations/111_ipd_os_core.sql:132` |
| OT posts exactly one procedure charge at completion (`ot_surgeries.charge_id`) | `migrations/122_operation_theatre.sql:24`, `:89` |
| Pharmacy lost sales (signed Rx not billed, part-filled) | `app/services/rx_dashboard.py:160` |
| DX B2B ledger rows are "unbilled" until a statement takes them | `migrations/132_dx_b2b.sql:427` |
| Check-in time on appointments | `migrations/103_opd_os_core.sql:133` |
| In-app admin notifications | `migrations/034_broadcasts_notifications_clinic_deletion.sql:19` |

## 3. Rules (each a pure function over one clinic and one day; amounts from snapshots, never recomputed)
| Code | Kind | Condition | Amount at stake | Module needed |
|:-|:-|:-|:-|:-|
| `opd_seen_not_billed` | leakage | consultation with `checked_in_at` on D or a signed encounter on D, and no non-void `opd_invoices` for the appointment | `doctors.consultation_fee` (rupees, `001_initial_schema.sql:73`) × 100 at scan time — labelled "estimate"; 0 → skipped | OPD |
| `prepaid_not_invoiced` | review | as above but the appointment has `payment_id` (WhatsApp / voice prepayment captured online) — money was collected; only the invoice/GST record is missing (prepayments are applied when staff issue the invoice, `app/services/opd_billing.py:450` `_apply_prepaid`) | 0 leakage; shows the prepaid amount for reconciliation | OPD |
| `order_done_not_billed` | leakage | `opd_orders.source='opd'`, status in (`sample_collected`,`in_progress`,`resulted`,`completed`), no live bill line | `unit_price_paise` | OPD |
| `order_stale_unbilled` | review | `opd_orders.status='ordered'` older than 48 h, not cancelled | `unit_price_paise` | OPD |
| `draft_bill_stale` | review | `opd_invoices.status='draft'` with lines, older than 24 h | draft subtotal | OPD |
| `ipd_charge_unposted` | leakage | `ipd_charges.status='proposed'` older than 24 h | `total_paise` | IPD |
| `ipd_discharged_unbilled` | leakage | admission `discharged`, charge `posted` with `invoice_id IS NULL` | `total_paise` | IPD |
| `ipd_discharge_pending_open` | review | admission `discharge_pending` > 24 h with unposted / unbilled charges | sum | IPD |
| `ot_done_no_charge` | integrity | surgery completed and `charge_id IS NULL` | tariff if known, else 0 + flag | IPD + OT |
| `rx_dispense_no_charge` | integrity | IPD pharmacy dispense without its `ipd_charges` row (should be impossible — atomic in 139) | line value | RX + IPD |
| `rx_signed_not_sold` | opportunity | `lost_sales` `unbilled_rx` / `partial_fulfilment` (patient may have bought elsewhere — never "leakage") | MRP × pending units where the item maps | RX |
| `dx_b2b_unstatemented` | review | ledger rows older than the period end + 7 days and not on a statement | sum | DX |

`opd_seen_not_billed` excludes appointments with `payment_id` (they go to `prepaid_not_invoiced`; VERIFICATION D12).
**Billing-adoption gate:** OPD rules run only when the clinic issued ≥ 20 OPD invoices in the last 30 days (bounded
setting); otherwise the page shows "this clinic does not bill OPD in Kriya yet" instead of a wall of false findings.
Exemptions (clinic settings, bounded): free review visit within N days of a paid visit (0–30), staff/VIP visits
flagged by the existing discount reason, charity cases. Every exemption is visible in the finding detail.

## 4. Data model — migration 148 (provisional) `148_revenue_integrity.sql`
- `ri_runs` — `clinic_id, run_date, started_at, finished_at, rule_counts JSONB, error TEXT NULL`; unique `(clinic_id, run_date)`.
- `ri_findings` — `clinic_id, rule_code, kind (leakage/review/integrity/opportunity), source_table, source_id,
  patient_id NULL, branch_id NULL, service_date, amount_paise BIGINT, status (open/resolved/dismissed), first_seen_on,
  last_seen_on, resolved_on NULL, resolution (billed/voided/exempt/dismissed/auto_closed), recovered_paise BIGINT NULL,
  dismissed_by NULL, dismiss_reason NULL (≥ 5 chars)`; unique open row per `(clinic_id, rule_code, source_table, source_id)`.
- `clinics.ri_settings JSONB DEFAULT '{}'`. Every table in `TENANT_OWNED_TABLES`. No trigger on any existing table.
- Rollback drops `ri_*` and the column.

## 5. Job and lifecycle
- `ri_nightly_scan` at 01:30 IST (after `auto_complete_appointments` 00:30), `distributed_job_lock("ri_scan")`, per
  clinic with the module on and at least one of OPD/IPD/DX/RX access; scans D−1 plus re-checks every open finding.
- A finding whose condition no longer holds is resolved automatically: `billed` (a live bill line / invoice now exists
  → `recovered_paise` = billed amount), `voided` (order/charge cancelled), else `auto_closed`.
- 08:00 IST digest to the clinic admin: in-app notification + WhatsApp alert to the admin phone (existing alert path,
  clinic's own credentials): "Yesterday: ₹18,400 done but not billed (7 items). Open > 7 days: ₹42,000."

## 6. Gate and permissions
`OPT_IN_FEATURES["revenue_guard"]` (owner toggle, default OFF; ADR pattern of RX — never `has_feature`). Rules run only
for modules the clinic has. Permissions: `RI_VIEW` (clinic admin, `CASHIER`, `BRANCH_MANAGER`), `RI_RESOLVE`
(dismiss with reason: clinic admin, `BRANCH_MANAGER`). Branch-pinned logins see their branch only.

## 7. API and UI
| Method | Path | Notes |
|:-|:-|:-|
| GET | `/admin/ri/summary?from=&to=` | totals by kind and rule, recovered ₹, open > 7 d |
| GET | `/admin/ri/findings?status=&rule=&branch_id=` | paged; deep link per row (visit bill, admission bill, order) |
| POST | `/admin/ri/findings/{id}/dismiss` | reason required; audit |
| POST | `/admin/ri/run` | re-run today for this clinic (rate-limited 3/day) |
UI (blueprint: [UI spec §7.7](../design/NEXT-LANES-UI-SPEC.md); icon Lucide `receipt-indian-rupee`; chips per UI spec §1.3; gates UI-G1…G12): **Revenue Guard** page in the Billing nav group (`<!-- RI:page -->`): yesterday card, open by rule, recovered ₹
this month (trend), table with "Bill now" deep links (the existing bill screens do the billing — RI never writes a
bill). Dashboard tile "₹ unbilled yesterday". Ketu R0 action `ri.yesterday`. Owner console: per-clinic "₹ flagged /
₹ recovered this month" (sales proof).

## 8. Zero impact
Read-only over existing tables (indexed `(clinic_id, created_at/status)` reads, D−1 window); no trigger; UI edits to
existing code limited to one `loaders` line, one guarded chart-repaint line in `toggleTheme()` and an additive main-dashboard tile block; job filters
to enabled clinics; module OFF by default; existing billing tests unedited.

## 9. Tests
`test_migration_ri.py` (real PG: apply, idempotent, rollback); `test_ri_rules.py` (one fixture per rule: fires, does
not fire, exemption, auto-resolve with recovered amount); `test_ri_feature_gate.py`; `test_ri_route_matrix.py`;
zero-impact spy (no `ri_*` query on OPD/IPD/RX/DX routes); standing gates as CC TEST-PLAN §1 (CG1–CG7).
