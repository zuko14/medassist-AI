# Kriya Diagnostic OS — Rollout, Cutover and Rollback

> Part of the Diagnostic OS set: start at [status.md](status.md). Kriya is in production with live diagnostic clients on the
> MocDoc connector (e.g. Accumx, Visakha, Aura per `docs/agent-context/13`). Nothing in this module may change their
> behaviour until the owner enables DX **and** switches a branch's delivery owner to `dx`.

## 0. Safety properties that make each phase deployable on its own
1. Every migration is additive (new tables, nullable columns, defaulted columns, constraints added `NOT VALID` then
   validated) and has a rollback that refuses to run while DX data exists.
2. `dx_enabled` is off for every clinic; with it off, every `/admin/dx/*` route returns 403 and no DX code path runs
   for WhatsApp, voice, lab booking, home collection, connectors or report delivery.
3. The only edits to existing behaviour are listed and regression-tested: `_lis_scope` and G1 worklist scope widened
   (`or dx_access`), G1 worklist filtered to `source IN ('opd','ipd')`, LIS per-test delivery skipped for `dx`
   orders, LIS routes refuse `dx` orders, `flag_value` reads two new keys with old defaults, `opd_orders` constraint
   widened, `opd_invoices`/`opd_invoice_items` new nullable columns, platform toggle branch, `verify_credentials`
   allowlists for new confined roles, `admin/me` new fields — **plus, from the re-audit ([AUDIT.md](AUDIT.md))**: G1
   `collect_sample` / `complete_order` refuse `source='dx'`; LIS review queue excludes DX orders; generic Lab Reports
   resend / resolve refuse `source='dx'` rows; `get_family_members` hides `dx_b2b_account_id` dependants; the lab-test
   delete route refuses tests ordered in DX; additive triggers on `opd_invoice_items` (DX bill-line guard),
   `opd_receipts` and `opd_invoices` (outbox, only for invoices with `accession_id`); phlebotomist allowlist gains the
   samples and field-collection routes. Each item has a named regression test (TEST-PLAN §2) and the existing suites
   for those files pass unedited.
4. Production deploy order for any phase with a migration: apply the migration to Supabase (SQL editor or
   `scripts/migrate.py`), record it in `schema_migrations`, **then** deploy code (Render pre-flight refuses to boot if
   disk migrations are ahead of the database; see `docs/agent-context/11`).

## 1. Environments
| Step | Gate |
|:-|:-|
| Local | DG1–DG7 green (TEST-PLAN §1) |
| Staging (Supabase branch) | 115–124 applied there first (Hospital OS open items), then DX migrations; E2E suites with `TEST_DATABASE_URL`; grants/RLS check; panel walkthrough with real logins on the staging deploy |
| Sandbox clinic in production | owner enables DX on an internal sandbox tenant; full W1–W14 run with test patients; delete via owner purge |
| Pilot | one real centre in **shadow** mode (§2) |
| General availability | per clinic, per branch, owner-driven |

## 2. Cutover for a centre currently on MocDoc (per branch)
| Stage | Duration (suggested) | DX setting | What happens | Exit criteria |
|:-|:-|:-|:-|:-|
| A. Setup | 1–3 days | `dx_enabled` on, `delivery_owner=connector` | sites, departments, containers (codes matching the MocDoc tenant: 01, 04, 07, 11 …); signatories; seed or import test master; lab director approves tests; templates (clone current report header); rate cards and B2B accounts; **import each connector `report_routing_providers` key as an `insurance_tpa` account with `report_routing_phone` as desk phone** (ADR-DX-20); record MocDoc's current highest sample sequence and set `sample_seq_floor` above it with margin (AUDIT A8) | checklist green; 100 % of the branch's top-50 tests approved; every TPA key mapped |
| B. Shadow | 7–14 days | `shadow` | staff register in DX **and** keep using MocDoc; DX binds the **MocDoc sample ID** as an `external` barcode (no DX label is printed, no DX number used) so results can be diffed by barcode; DX renders releases but never sends; daily diff of values/flags/report layout and of delivery decisions (would DX have held this report for dues? routed it to the TPA?) for a sample of visits | ≥ 200 visits diffed, zero unexplained value/flag differences, zero delivery-decision differences against what the centre actually did, layout signed off by the lab director |
| C. Parallel billing | 2–3 days | `shadow` | bills raised in DX only for walk-ins; MocDoc used for results; day-end collections reconcile | collections match to the rupee for 3 days |
| D. Go live | — | owner disables the MocDoc connector row for the branch, then `delivery_owner=dx` | DX delivers; MocDoc read-only for old visits | first 48 h: every released visit checked; delivery failure rate ≤ existing baseline |
| E. Decommission | after 30 days | — | MocDoc subscription ended by the client; historical reports remain in `lab_reports` | — |

Rollback during D: switch `delivery_owner` back to `connector` and re-enable the connector row (no data loss; visits
already released stay delivered; DX keeps rendering). A full module rollback is the owner disable → WIND_DOWN, never a
migration rollback in production with data present.

## 3. Pilot prerequisites (owner)
- Owner decisions O-DX-1…11 (ADR.md) accepted or changed for the pilot centre.
- Report documents reuse the centre's **already approved** lab-report templates (`config.lab_report_template_name`,
  `lab_report_summary_template_name`, AUDIT A18). New templates to approve on the centre's own WABA:
  `dx_report_copy_referrer`, `dx_recollection_required`, `dx_payment_due`, `dx_b2b_statement`,
  `dx_urgent_result_referrer` (if used).
  Until approved, in-session messages work inside 24 h; outside it the release stays `queued` with a visible reason.
- Thermal label printer model confirmed (ZPL or driver-based PDF) and a test print signed off.
- Pathologists' doctor records have registration numbers and signature images.
- Paper backup for the first two weeks of stage D (NABL continuity).

## 4. Daily reconciliation queries (run by ops during pilot; all must return 0 rows)
1. Visits with a patient bill whose total ≠ Σ active items (price − discount).
2. Orders `source='dx'` with `status='sample_collected'` whose sample is not `collected|in_transit|received|stored`.
3. Samples in a dispatched batch for > 24 h without receipt.
4. Validated DX results with approval levels = 3 lacking a matching verification (should be impossible).
5. Releases `queued|failed` older than 2 h while the branch is `dx`.
6. B2B accounts whose ledger-derived balance exceeds `credit_limit_paise` without a hold.
7. `lab_reports` rows with `source='dx'` and no matching release.

## 5. Monitoring
Log lines `DX_REGISTER`, `DX_COLLECT`, `DX_RECEIVE`, `DX_APPROVE`, `DX_RELEASE`, `DX_DELIVERY` with clinic id, visit
id, masked phone (`mask_phone`), never names or values. Alert on release failures, TAT breach spikes, bridge
heartbeat gaps (> 10 min during lab hours).

## 6. Deactivation and deletion
Owner disable → 409 preview → confirm → WIND_DOWN (ADR-DX-14). Clinic deletion runs `dx_purge_clinic` first. The
connector setup is never touched by DX deactivation; the owner re-enables it if the centre returns to MocDoc.
