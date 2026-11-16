# Kriya Pharmacy OS — Test and Verification Plan

> Part of the Pharmacy OS set: start at [status.md](status.md). Harness rules (same as DX/IPD): `pytest` is hermetic
> (`tests/conftest.py` forces test credentials and fakes the distributed lock); **never** set `KRIYA_TEST_LIVE=1`;
> real-Postgres tests use the local Postgres fixtures and the PostgREST bridge of `tests/ipd_pg_bridge.py` (routes run
> their real queries, functions, triggers; only the HTTP hop is replaced). Extend the bridge only for query shapes
> the RX routes use, and note it in the bridge docstring.

## 1. Standing gates — run at the end of EVERY phase; paste summary lines into status §9
```bash
# RG1 — tenancy linter (baseline stays 0) + RX table registration
python -m pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py tests/test_rx_tenancy_registry.py -q
# RG2 — security matrices incl. every /admin/rx route
python -m pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py tests/test_rx_route_matrix.py -q
# RG3 — zero-LLM guards
python -m pytest tests/test_clinical_firewall.py tests/test_opd_no_llm.py tests/test_ipd_no_llm.py tests/test_dx_no_llm.py tests/test_rx_no_llm.py -q
# RG4 — live channels unchanged
python -m pytest tests/test_webhook.py tests/test_fsm_transitions.py tests/voice/ -q
# RG5 — OPD / IPD / GST / orders regression, files UNEDITED
python -m pytest tests/test_opd_billing_real_pg.py tests/test_ipd_api_e2e.py tests/test_ipd_finance_api_e2e.py \
  tests/test_migration_116_ipd_finance.py "tests/test_migration_11[5-7]*.py" tests/test_orders_api_e2e.py -q
git diff --stat main -- tests/test_opd_billing_real_pg.py tests/test_ipd_api_e2e.py tests/test_ipd_finance_api_e2e.py   # expect empty
# RG6 — migrations: all, incl. RX up/down chain on real PG
python -m pytest scripts/test_migration_runner.py "tests/test_migration_1*.py" tests/test_migration_rx_*.py -q
# RG7 — full suite, no -x; count ≥ baseline + new; 0 failed
python -m pytest -q -rs
# RG8 (P4+) — concurrency suite on real PG
python -m pytest tests/test_rx_concurrency.py -q
```
A failing gate stops the phase. Never skip, `xfail`, delete or edit an existing test to get green; never raise a
linter baseline.

## 2. New test files by phase
| Phase | File | Must cover |
|:-|:-|:-|
| P1 | `tests/test_migration_rx_m1.py` (real PG) | 134 applies on 133; idempotent; `rx_state` default/CHECK; stores composite FKs (no cross-tenant ward/branch/parent); one central per branch; rollback + re-apply; `rx_purge_clinic` v1 |
| P1 | `tests/test_rx_feature_gate.py` | off by default; owner enable; requires `opd_enabled` (400); enterprise wildcard alone does not enable; partner account 400; `/admin/me.rx_access`; 409 preview → WIND_DOWN → re-enable keeps data; every `/admin/rx` route 403 when OFF **before any rx_* query** (spy on `sb`); route count pin |
| P1 | `tests/test_rx_no_llm.py` | AST: `app/services/rx*.py`, `app/routers/rx.py`, RX jobs import nothing from `ai_engine`, `ai_gateway`, `hybrid_search`, `app.voice.llm*`, `openai`, `groq`; AI/voice code never names `rx_` tables |
| P1 | `tests/test_rx_tenancy_registry.py` | set of `rx_*` names in `TENANT_OWNED_TABLES` == tables created by RX migrations (parsed from SQL) |
| P2 | `tests/test_migration_rx_m2.py` | item immutables; schedule downgrade refused; `rx_item_changes` written; barcode unique per clinic (same barcode allowed in two clinics) |
| P2 | `tests/test_migration_rx_m3.py` | ledger: insert outside function refused; UPDATE/DELETE refused; balance == SUM after 1 000 random movements; direct balance write refused; negative refused (23514 → `rx_insufficient_stock`); expired/blocked out refused; write-off of expired allowed; IST midnight boundary with frozen clock |
| P2 | `tests/test_rx_catalogue_api.py` | CRUD, import dry-run/apply idempotent, resolve candidates deterministic (same input → same order), substitutes same generic only |
| P3 | `tests/test_migration_rx_m4.py`, `tests/test_rx_purchase.py` | W1/W6 acceptance list; GRN calc vectors (§4.2); supplier invoice unique; void rules |
| P4 | `tests/test_migration_rx_m5.py`, `tests/test_rx_pos.py`, `tests/test_rx_tax.py`, `tests/test_rx_returns.py` | W2/W3/W5a lists; tax vectors (§4.1); MRP ceiling; discount cap; shift link; void same-day only; receipt PDF fields |
| P5 | `tests/test_migration_rx_m6.py`, `tests/test_rx_ipd.py` | W4/W5b lists; `ipd_charges.source` widening keeps 111/115 rows valid; MAR FK optional; discontinue cascade to open indent lines; charge split > 999 units |
| P6 | `tests/test_migration_rx_m7.py`, `tests/test_rx_transfers.py`, `tests/test_rx_audit.py` | W7/W8 lists |
| P7 | `tests/test_migration_rx_m8.py`, `tests/test_rx_registers.py` | ADR-RX-05 list; W9 list |
| P8 | `tests/test_migration_rx_m9.py`, `tests/test_rx_refills.py`, `tests/test_rx_alerts.py` | W10 list; alert dedupe per day; jobs skip OFF clinics (0 queries beyond the clinic filter) |
| P9 | `tests/test_assistant_registry.py` (extended), `tests/test_rx_ketu.py` | RT-14 page table; stock answer R0; hand-offs; no write actions |
| P10 | `tests/test_rx_reports.py` | GSTR-1 golden file unchanged for non-RX clinic; RX sections correct; CSV formula-injection guard |
| P4–P11 | `tests/test_rx_concurrency.py` (real PG, threads with separate connections) | §3 |
| P11 | `tests/test_rx_release_readiness.py` | full lifecycle on one clinic: setup → GRN → POS → indent → transfer → audit → registers → refills → wind-down → purge; rollback chain 142→133 |

## 3. Concurrency and race conditions (RG8)
Each test uses N real connections in threads released by a barrier.
| Test | Setup | Assertion |
|:-|:-|:-|
| `test_parallel_checkouts_never_negative` | batch with 1 unit; 50 drafts each 1 unit | exactly 1 completed; 49 × 409 `rx_insufficient_stock`; balance 0; ledger sum 0 |
| `test_parallel_checkouts_two_batches` | 2 batches × 5 units; 12 one-unit sales with FEFO auto-pick at completion | 10 complete, 2 fail; no batch negative; FEFO order respected for every success |
| `test_double_click_complete` | one draft; 10 concurrent completes with the same idempotency key | 1 sale number; 1 movement per line; others `replayed` |
| `test_double_dispense_same_indent` | one indent line 10 units; 2 pharmacists dispense 10 each concurrently | 1 succeeds; other 409 (CHECK `dispensed ≤ requested`); one charge |
| `test_dispense_retry_one_charge` | dispense with key K; network-fail simulation; retry ×3 | one dispense, one charge (`uq_ipd_charges_source_ref`) |
| `test_sale_vs_transfer_dispatch` | 5 units; sale 5 and transfer 5 concurrently | one wins; other 409; balance 0 |
| `test_register_seq_gapless` | 20 concurrent H1 sales | seq 1..20 without gaps; chain verifies |
| `test_number_series_gapless` | 30 concurrent completes | `PHS-` seq contiguous, no duplicates |
| `test_audit_delta_during_sales` | counting audit; 10 concurrent sales on audited batch | `movement_delta_since_start` = −10; post with counted = start − 10 → zero variance |
| `test_same_rx_two_counters` | two counters open same Rx | one draft (unique index); both get same draft id |
| `test_order_discontinue_vs_dispense` | doctor discontinues while pharmacist dispenses | either dispense commits first (then lines remain dispensed, charge stands) or discontinue first (dispense 409 `rx_order_not_active`); never a charge for a cancelled line |

## 4. Vectors
### 4.1 Inclusive GST split (Python `rx_tax` == SQL `rx_gst_split_inclusive`)
| line_net (paise) | rate bps | taxable | CGST | SGST |
|:-|:-|:-|:-|:-|
| 10500 | 500 | 10000 | 250 | 250 |
| 11200 | 1200 | 10000 | 600 | 600 |
| 11800 | 1800 | 10000 | 900 | 900 |
| 9999 | 500 | 9523 | 238 | 238 |
| 1 | 500 | 1 | 0 | 0 |
| 10000 | 0 | 10000 | 0 | 0 |
Derivation for 9999 @ 5%: taxable = (9999×10000 + 5250) / 10500 = 9523 (integer division of 99 995 250 / 10 500 =
9523.35 → 9523); tax = 476; CGST = 238, SGST = 238. Property test: for every rate in the CHECK set and 10⁵ random
`line_net ∈ [1, 10⁸]`: `taxable + cgst + sgst == line_net`, `0 ≤ tax`, Python == SQL.
No-GSTIN clinic: every vector → taxable = line_net, tax 0.

### 4.2 Loose unit price
`rx_unit_price(mrp_pack=12000, pack_size=10, units=3)` = (36000 + 5)/10 = 3600; `(9999, 15, 7)` = (69993 + 7)/15 = 4666.
Sum of loose sales of a whole pack may differ from MRP by ≤ `pack_size/2` paise — asserted bounded and never above
MRP-proportional value + 1 paisa (the CHECK).

### 4.3 GRN landed cost
10 packs + 2 free, pack_size 10, rate 10000, scheme 10%, CD 2%, GST 5%, freight 0, clinic **with** GSTIN:
gross 100000 → after scheme 90000 → after CD 88200 (2% of 90000 = 1800) → tax 4410 (recoverable, excluded) → landed
per unit ×100 = 88200×100/120 = 73500 (₹7.35). Same clinic **without** GSTIN: (88200 + 4410)×100/120 = 77175.

## 5. Adversarial permission matrix (`tests/test_rx_route_matrix.py`)
For every `/admin/rx` route × principal: anonymous (401), staff without RX perms (403), PHARMACIST, PHARMACY_MANAGER,
STORE_KEEPER, NURSE (ward A), CASHIER, DOCTOR, clinic_admin of clinic B (404/403 on clinic A data), super_admin
without explicit clinic (400), clinic with pharmacy OFF (403 `pharmacy_not_enabled`), WIND_DOWN on new-work routes
(409). Store scope: PHARMACIST pinned to store S1 cannot read/sell from S2 (404). Nurse ward A cannot raise an indent
for an admission in ward B (404). Expected codes are a table in the test file generated from SECURITY §3.4; a route
missing from the table fails the test.

## 6. Zero-impact proofs (P1 and every phase)
1. RG4, RG5 unedited and green.
2. `test_rx_feature_gate.py::test_non_rx_clinic_zero_rx_queries` — drive a full OPD visit + IPD admission + discharge
   on a clinic with pharmacy OFF with a spy on `sb()`; assert no builder names an `rx_` table (except the documented
   `close_shift` union, which must return 0 rows) and the `ipd_orders` trigger performs no write.
3. Latency check (P11, documented in status §9): p95 of `/admin/opd/invoices` issue, `/admin/ipd/admissions/{id}`
   and `/webhook` on the perf fixture before vs after migrations 134–142 — regression ≤ 5%.
4. GSTR-1 export golden file for a non-RX clinic byte-identical.

## 7. UI verification (manual, recorded)
For each page at 1440 px and 390 px, dark and light: loads, empty state, error state, keyboard flow (POS), print
preview (80 mm, A5), no console errors. POS timing script `tests/ui/rx_pos_timing.md`: 3 runs of a 3-line Rx; record
median seconds in status §9 (target ≤ 3 s).
