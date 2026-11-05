# Kriya Diagnostic OS — Architecture Decision Records

Each ADR: context → alternatives → decision → consequences → evidence (the test that proves it; written in the phase
named). Status **Accepted** unless marked. Open owner decisions are at the end; each has a default in force so no
phase blocks on them, but the pilot centre must accept or change them before go-live (ROLLOUT §3).

## ADR-DX-01 — One result engine: diagnostic tests are `opd_orders` rows
- **Context.** G1 (mig 115) made `opd_orders` the worklist row and bill source for OPD/IPD; G2 (mig 118) built
  parameters, ranges, entry, signing and immutable versioned results on top of it. A diagnostic centre needs the same
  result engine plus visits, tubes and logistics.
- **Alternatives.** (a) A separate `dx_tests` / `dx_results` stack. (b) Extend `opd_orders` with `source='dx'`.
- **Decision.** (b). Every test of a visit is one `opd_orders` row with `source='dx'`, `catalog_type='lab_test'`,
  linked to `dx_accessions` and `dx_accession_items`. `lis_record_results` / `lis_validate_result` are used unchanged.
- **Consequences.** One LIS for hospital and lab; a hospital with OPD+IPD+DX has one pathologist queue. Risk: code that
  reads `opd_orders` assuming `opd`/`ipd` (F5) — every such reader is listed and filtered in P3 (DX-19).
- **Evidence.** P3 `test_dx_orders_use_lis_unchanged`, `test_g1_worklist_excludes_dx_rows`; 115/118 suites green unedited.

## ADR-DX-02 — Own owner opt-in, independent of OPD
- **Context.** `opd_enabled()` needs plan feature `booking`; `diagstream`/`diagbooking` have none (F1). A lab cannot
  be forced to buy an OPD module to run its lab.
- **Decision.** `OPT_IN_FEATURES["dx_enabled"] = "Diagnostic OS (Registration, Samples, Lab, Reports, B2B)"`.
  `dx_eligible(clinic)`: `account_type == 'tenant'` **and** the plan's own feature set (not the enterprise wildcard)
  contains `lab_test_booking` or `lab_reports`; enterprise clinics are eligible only by explicit owner opt-in (the
  toggle is the opt-in). `dx_access(clinic)` → `ACTIVE` (`features.dx_enabled is True` and eligible), `WIND_DOWN`
  (`dx_state='WIND_DOWN'`), else `None`. Never `has_feature(clinic, "dx_enabled")`.
- **Eligibility by plan** (plan feature sets in `app/services/tenant.py` at `56f719f`):

  | Plan | `lab_test_booking` | `lab_reports` | DX eligible | WhatsApp report delivery from DX |
  |:-|:-|:-|:-|:-|
  | `diagstream` | ✓ | ✓ | yes | yes |
  | `diagbooking` | ✓ | — | yes | **no** — print / download / share link only; owner console warns "upgrade to diagstream for WhatsApp reports" (the plan deliberately sends no proactive messages, AUDIT A12) |
  | `essential` | — | ✓ | yes (in-house lab) | yes |
  | `polyclinic`, `multispecialty`, `womenchild` | ✓ | ✓ | yes | yes |
  | `ivf` | ✓ | — | yes (andrology/hormone lab) | no (as `diagbooking`) |
  | `soloclinic`, `derma`, `eye`, `dental` | — | — | **no** (400) | — |
  | `enterprise` | wildcard | wildcard | yes, only through the explicit toggle | yes |
  | any `corporate_partner` account | — | — | **no** (400) | — |

  A per-clinic `features.lab_reports` override (owner console) changes the delivery column like any feature.
- **Consequences.** OPD/IPD routes stay gated by their own flags; DX routes call registry/billing **services** under
  `_dx_scope`, not OPD routes.
- **Evidence.** P1 `test_dx_off_by_default`, `test_dx_enterprise_wildcard_does_not_enable`,
  `test_dx_partner_account_refused`, `test_dx_does_not_need_opd`.

## ADR-DX-03 — Extending `opd_orders` safely
- **Decision.** One migration (DX-M3) adds: `source` value `dx`; nullable columns `accession_id`,
  `accession_item_id`, `dx_sample_id`, `replaces_order_id`, `outsource_partner_id` with composite FKs; drops
  `NOT NULL` on `doctor_id` and adds `CHECK (source = 'dx' OR doctor_id IS NOT NULL)`; replaces
  `opd_orders_source_shape` with a 3-branch version (`dx` ⇒ `accession_id`+`accession_item_id` NOT NULL, encounter /
  admission / IPD order NULL). Constraints are added `NOT VALID` then `VALIDATE CONSTRAINT` (short lock, no rewrite).
  `opd_guard_order`'s mutable list gains `dx_sample_id` only (re-pointing is never needed: rejection creates a
  replacement order, ADR-DX-05). `opd_guard_invoice_item_order` is **not** changed: DX bill lines link
  `dx_accession_item_id`, never `order_id`.
- **Consequences.** OPD/IPD rows keep every 115 rule. DX orders carry `unit_price_paise = 0` (money lives on
  accession items only, so nothing can double count).
- **Evidence.** `test_migration_dx_m3`: OPD/IPD shape still enforced; `dx` row without accession → 23514; OPD row with
  NULL doctor → 23514; 115 suite unchanged.

## ADR-DX-04 — Money: patient bills on the OPD ledger, B2B on its own ledger
- **Context.** The OPD ledger already has gapless numbering, derived `paid_paise`, receipts, refunds, shifts, payment
  links, prepaid apply, GST lines and credit notes, tested under concurrency. B2B credit bills an institution monthly,
  not a patient.
- **Decision.** (1) `opd_invoices.accession_id` (nullable, composite FK, indexed, not unique);
  `opd_invoice_items.dx_accession_item_id` with at most one live line per item. A DX visit's patient share is billed
  on one invoice, plus supplementary invoices for items added after issue; lines are written only by DX functions
  (bill-line guard, AUDIT A10); issue/receipt/refund/void through `opd_billing.py`. The invoice's `appointment_id`
  stays NULL; DX calls `_apply_prepaid` once per linked booking so each online payment (full or deposit) is applied
  exactly once (AUDIT A9).
  (2) `billing_mode in ('b2b_credit','b2b_prepaid')`: no patient invoice; `dx_b2b_post_charge` writes one append-only
  `charge` ledger row per item at the account's rate; statements group a period's rows into a numbered bill of supply.
  (3) Diagnostic services default to GST-exempt (rate 0, SAC from clinic data); rates are data, not code.
- **Consequences.** One cashier workflow for walk-ins; B2B receivables have their own ageing. Mixed mode ("hospital pays
  lab rate, patient pays nothing to lab") is `b2b_credit`.
- **Evidence.** P3 `test_dx_bill_equals_sum_of_active_items`, `test_dx_prepaid_booking_applied_once`; P8
  `test_b2b_ledger_append_only`, `test_statement_total_equals_period_charges`, `test_two_parallel_statements_one_wins`.

## ADR-DX-05 — Tubes are samples; rejection creates a replacement order
- **Context.** Real labs put many tests on one tube (screenshot: one serum ID for CRP, uric acid, calcium…; one EDTA ID
  for CBC). Migration 115 freezes `specimen_barcode` once a sample is collected.
- **Decision.** `dx_samples` = one physical container. At registration the service groups the visit's tests by
  `lab_tests.container_id` (and processing site, since one tube cannot be in two labs) into `pending_collection`
  samples. **Barcode = 12 digits `YY` + `cc` + `SSSSSSSS`**, matching what the owner's MocDoc tenant prints (AUDIT A7:
  `260100016583` serum and `260400016583` second container of the *same* visit; `261100016459` urine): `YY` = IST
  year, `cc` = `dx_containers.code`, `SSSSSSSS` = the visit's **sample sequence**, taken once per visit from
  `opd_next_counter(clinic, 'dx_sample_seq:YY')` and shared by all its tubes. A second tube of a container code already
  used in the same visit (split by processing site) and every recollection take a **fresh** sequence number, so a
  barcode can never repeat. The counter starts at `dx_settings.sample_seq_floor` (set above the previous LIMS's last
  number at go-live, AUDIT A8). Code128-C; pre-printed labels (`barcode_source='preprinted'`) and, in shadow mode, the
  old LIMS's own label (`'external'`) are bound at collection and consume no DX number. `uq_dx_samples_barcode
  (clinic_id, barcode)`. Collection sets every order on the sample to `sample_collected` with `specimen_barcode = sample.barcode`
  in one transaction. **Rejection** (`dx_reject_sample`): the sample → `rejected` with reason code; each live order on
  it → `cancelled` (`cancellation_reason='sample_rejected:<code>'`); a replacement sample and replacement orders
  (`replaces_order_id`) are created `pending_collection`; patient notified (P4).
- **Consequences.** Full audit of every tube; no mutable barcode; the LIS sees a new order for the recollected sample.
- **Evidence.** P4 `test_tests_grouped_by_container_and_site`, `test_barcode_unique_per_clinic_parallel`,
  `test_reject_creates_replacement_and_keeps_history`.

## ADR-DX-06 — Verification levels enforced in the database; approval is always human
- **Context.** MocDoc advertises completion → verification → approval and auto-approval of normals. Indian rules
  require a qualified registered signatory for interpretive reports (COMPETITIVE-ANALYSIS §4).
- **Decision.** `dx_settings.approval_levels ∈ {2, 3}` (default 2 = entry + approval, as 118 today). For 3:
  `dx_result_verifications` (append-only: result id, the `updated_at` verified, verifier, decision `verified|returned`).
  Trigger `trg_dx_lab_results_gate` (BEFORE UPDATE on `lab_results`, **only** when the order's `source='dx'`) refuses
  `entered → validated` unless a `verified` row matches `OLD.updated_at`. Four-eyes defaults: verifier ≠ technician;
  approver may be the verifier (owner decision O-DX-3). **Auto-verification** (skip the verifier) is allowed per test
  when every value is `normal`, no delta failure and no critical — recorded as a verification row by the system actor.
  **Auto-approval is not offered.** Instead, **batch approval**: the pathologist signs many all-normal results in one
  action; each item still goes through `lis_validate_result` with its own `expected_updated_at`.
- **Consequences.** OPD/IPD signing is untouched (trigger ignores non-DX orders). The legal signer is always a person.
- **Evidence.** P6 `test_level3_requires_verification_of_current_version`, `test_reentry_invalidates_verification`,
  `test_batch_approval_partial_failure_reports_each_item`, `test_gate_ignores_opd_orders`.

## ADR-DX-07 — Visit-level, versioned report releases; no per-test delivery for DX
- **Context.** `lis.validate_and_sign_report` spawns `deliver_report()` per test (F4). Patients receive one report per
  visit, often in parts (top-ups), and centres block delivery while dues are pending (MocDoc behaviour seen in the
  connector).
- **Decision.** For `source='dx'` orders the LIS per-test delivery is skipped (one guarded branch in
  `lis.validate_and_sign_report`, tested). `dx_create_release(visit, result_ids)` composes signed results into a
  release (`partial` or `final`; `amended` when a result version > 1), snapshots the resolved template, letterhead,
  signatories and routing, renders the PDF from snapshots only, stores `pdf_sha256` and a `verify_code`, and creates
  one `dx_release_deliveries` row **per target** (patient / referrer / account). Each target is decided by
  [DELIVERY-POLICY.md](DELIVERY-POLICY.md) and sent with its own id
  `upload_and_send(external_report_id='dx-<release_id>-<p|r|a>', source='dx')` — one id per release would let the
  pipeline's `(clinic_id, external_report_id)` dedup skip the second and third recipient (AUDIT A3). Auto-release
  policy: `dx_settings.auto_release` = `off` | `per_department` | `when_complete` (default `when_complete`). Dues and
  payer routing are invariants (ADR-DX-19, -20), not settings. A held delivery sends a payment link instead.
- **Consequences.** A reprint is byte-identical; an amended result supersedes the earlier release (its PDF re-rendered
  with a SUPERSEDED banner on request, never overwritten).
- **Evidence.** P7 `test_dx_signature_does_not_send_per_test`, `test_release_pdf_is_deterministic`,
  `test_dues_hold_then_payment_releases`, `test_amendment_supersedes_release`.

## ADR-DX-08 — Templates: versioned, resolved by cascade, snapshotted; white-label is honest
- **Decision.** `dx_report_templates` rows are immutable once published (edit = new version). Resolution at release
  time: visit override → B2B account template (if `white_label`) → registration branch → clinic default → built-in.
  The snapshot is stored on the release. Layout is a validated JSON document (REPORT-TEMPLATES §4) — no HTML or code
  from users. **A white-label report always prints "Tested at: <processing lab legal name, address, registration /
  NABL no.>"** and the pathologist block of the lab that signed; this line cannot be removed by configuration.
  Outsourced tests print "Performed at: <reference lab>".
- **Why.** The hospital's patients get hospital-branded reports (owner requirement) without misrepresenting who
  performed and signed the test (NABL 112 referral-lab disclosure; signatory rules).
- **Evidence.** P7 `test_white_label_keeps_tested_at_line`, `test_template_cascade_order`,
  `test_published_template_is_immutable`.

## ADR-DX-09 — B2B accounts are not corporate partner accounts
- **Context.** Migration 096 `account_type='corporate_partner'` is a dashboard-only *clinic row*. A B2B client of a lab
  (hospital, clinic, franchise collection point, corporate, TPA) belongs **to the lab's tenant**.
- **Decision.** `dx_b2b_accounts` inside the lab's `clinic_id`. Partner logins are `clinic_admins` rows with
  `staff_role='B2B_PARTNER'` and `dx_b2b_account_id`, confined at `verify_credentials` to `/admin/dx/partner/*` (same
  mechanism as `PHLEBOTOMIST`), every query additionally filtered by the account id server-side.
- **Consequences.** No new auth system; tenancy linter covers partner reads. A hospital that is *also* a Kriya tenant
  stays two tenants; cross-tenant sample exchange is DEFERRED (DX-DEF-X).

## ADR-DX-10 — AI boundaries
- **Decision.** DX services and routers import nothing from `app/services/ai_*`, `app/voice`, `app/assistant`;
  AI/voice/assistant code never names `dx_*` clinical tables, `lab_results` or `lab_result_values`
  (`tests/test_dx_no_llm.py`). Reference ranges, flags, formulas, interpretation text and signatures are human-entered
  or deterministic. Optional AI uses (all outside the clinical path, off by default, behind `ai_gateway` spend caps):
  (a) the existing patient-friendly summary in `lab_reports` (per-clinic toggle, already live), (b) a *layout*
  suggestion for a letterhead from an uploaded image (produces template JSON a human publishes). Importing parameter
  masters from a PDF uses deterministic `pdfplumber` extraction and creates **drafts** that a lab director approves
  one by one.
- **Evidence.** `tests/test_dx_no_llm.py`; P2 `test_imported_parameters_are_inactive_until_approved`.

## ADR-DX-11 — No referral-commission or "doctor incentive" ledger
- **Context.** Many Indian LIS products track per-doctor referral payouts. NMC conduct rules bar practitioners from
  accepting commissions or gifts for referrals; state rules vary; IJME (2026) documents the harm.
- **Decision.** Kriya stores referrers for clinical routing and report copies, and shows referral *volume* analytics.
  It does not compute, store or export payouts, incentives or "cuts" to referring doctors. B2B institutional pricing
  (a contract rate to a hospital/clinic account, invoiced to the institution) is allowed and transparent.
- **Consequences.** A prospect asking for incentive reports is told no; this is a product position, recorded here.

## ADR-DX-12 — Hub-and-spoke via sites, routing rules and batches
- **Decision.** `dx_sites` (per branch: `collection`, `processing`, or both). `dx_routing_rules`
  (`from_site`, `lab_test_id` or `department_id`, `to_site` or `outsource_partner_id`, priority) resolve the processing
  site per test at registration; tubes are grouped per destination. `dx_batches` move samples between sites with a
  manifest; receipt is the lab's **accession** step (scan; missing/extra/damaged recorded). Visibility for a pinned
  user = registration site ∪ processing sites of the visit's samples. Processing-site users see patient name, age,
  sex, MRN, sample and tests — not phone, address or bill (data minimisation).
- **Evidence.** P5 `test_routing_rule_precedence`, `test_receipt_discrepancy_recorded`,
  `test_processing_site_user_sees_no_phone`.

## ADR-DX-13 — Analyser integration through an outbound-only local bridge
- **Decision.** A small Windows/Linux service at the lab (`kriya-lab-bridge`, separate repo or `connectors/bridge/`)
  speaks ASTM E1381/E1394 or HL7 v2 (ORU^R01, host query) to instruments over serial/TCP and talks to Kriya only by
  outbound HTTPS: `GET /internal/dx/bridge/orders?barcode=` (host query) and `POST /internal/dx/bridge/results`.
  Auth: per-agent key (hashed in `dx_bridge_agents`), HMAC-SHA256 over body + timestamp, 300 s replay window, nonce
  (CallMedex pattern). Results land in `dx_instrument_results` (raw, append-only), are mapped by
  `lab_test_parameters.machine_code`, and **prefill** the worksheet; saving them as `entered` happens only if
  `dx_settings.instrument_auto_enter` is on, with actor "instrument:<id>", and still needs human
  verification/approval.
- **Why.** No inbound port at the lab, works behind any ISP; a compromised bridge can only submit values that a human
  still reviews.

## ADR-DX-14 — Deactivation is wind-down, never deletion
- **Decision.** Same as ADR-IPD-07: with open visits (samples not released) disabling needs `confirm_disable` and sets
  `WIND_DOWN` (no new visits, sites, tests or batches; everything for existing visits works). With none → `OFF`.
  Re-enable → `ACTIVE` with data intact. Clinic deletion calls `dx_purge_clinic` before `ipd_purge_clinic` /
  `opd_purge_clinic` (DX rows reference OPD orders and invoices).

## ADR-DX-15 — Cutover alongside the MocDoc connector (shadow mode)
- **Decision.** `dx_settings.delivery_owner[branch_id] ∈ {connector, shadow, dx}`; default `connector`. In `shadow`
  DX renders and stores releases but never sends; the panel shows a side-by-side "would have sent" log. Switching a
  branch to `dx` is refused while an active MocDoc connector row exists for that branch (409 with instructions).
- **Evidence.** P7 `test_shadow_mode_never_sends`, `test_dx_owner_refused_with_active_connector`.

## ADR-DX-16 — Background jobs (few, locked, tenant-explicit)
- **Decision.** Only these scheduler jobs, each with `scheduler_locks` and an explicit per-clinic loop:
  `dx_tat_breach_scan` (every 5 min: marks orders past TAT, notifies site dashboard; no state change to results),
  `dx_sample_stability_scan` (every 10 min: batch/sample stability alerts), `dx_b2b_hold_evaluation` (daily 02:10 IST:
  sets/clears automatic holds by limit and overdue days, logged), `dx_release_retry` is **not** new — the existing
  lab-report retry job covers sends. Everything else (stages, TAT, balances) is derived on read.

## ADR-DX-17 — Numbering and time
- Visit `LAB-YYYY-NNNNNN` (gapless per clinic per IST year, `opd_next_counter('dx_accession:YYYY')`), sample
  12-digit (ADR-DX-05), batch `TRN-YYYY-NNNNN`, B2B statement `B2B-YYYY-NNNNN`, release `R<n>` per visit. Prefixes are
  `dx_settings` data. IST rules as ADR-IPD-11.

## ADR-DX-18 — Parallel build process (how multiple agents work without collisions)
- **Decision.** One migration per phase. A migration's **number is assigned at merge time**, not when work starts:
  the lane rebases on `main`, takes the next free number after the highest file in `migrations/`, renames its file,
  records it in status.md §5 and merges; merges that carry a migration are strictly one at a time. (Reason: the Render
  pre-flight only checks `highest_applied >= highest_on_disk`; if 131 were applied before 130, the server would boot
  with 130 never applied.) During development the file is named `migrations/<NNN>_dx_<name>.sql` with `NNN` = next
  free number at the time and is renumbered on rebase if `main` moved. Lanes own disjoint files (AGENT-PROMPT.md §4). Shared hot files
  (`app/tenancy.py`, `app/services/permissions.py`, `app/services/tenant.py`, `app/main.py`, `app/routers/platform.py`,
  `admin/index.html`, `admin/platform.html`, `app/assistant/registry.py`) are edited only inside marked DX blocks and
  merged one lane at a time.

## ADR-DX-19 — "No payment, no report" is a database invariant across every channel
- **Context.** Owner requirement (2026-11-05): a patient who has not paid must not receive the report automatically.
  MocDoc refuses report download while a bill has dues (seen by the connector, `_check_bill_payment_error`). The first
  draft had this as a setting checked once (AUDIT A1).
- **Decision.** `dx_delivery_decision()` (SQL, STABLE) is the only rule source; `dx_claim_delivery()` moves a
  delivery `queued → sending` only if the decision still allows it; only `dx_reports.send_delivery()` may call the
  pipeline with `source='dx'` (AST test). Dues = live invoice not `paid` (zero-total issued counts as paid) or a
  doorstep collection not yet handed over. Receipts / invoice status changes enqueue a re-evaluation through
  `dx_outbox` triggers, so no existing payment code is edited. Override = append-only `dx_delivery_overrides` by
  `DX_ADMIN`/`clinic_admin` with reason, rule 7 only. Critical-value calls are never held.
- **Evidence.** DELIVERY-POLICY §7 test list.

## ADR-DX-20 — Payer-routed visits (insurance, TPA, corporate) are delivered to the payer, never the patient
- **Context.** Today centres route TPA panel reports (MEDIBUDDY, MD INDIA TPA …) to a desk number via connector config
  (`app/services/report_routing.py`; AUDIT A2). The patient is deliberately not a recipient.
- **Decision.** `dx_b2b_accounts.kind IN ('insurance_tpa','corporate')` ⇒ CHECK `report_delivery IN ('account','none')`
  unless `patient_copy_authorised_at`, `_by`, `_reason` are set (then `both` is allowed). The decision function refuses
  patient, print-for-patient and share-link targets for those visits; referrer copies need the same authorisation.
  Generic Lab Reports resend/resolve paths refuse `source='dx'` rows (they can re-address a report to a new phone,
  AUDIT A20). **Co-pay**: `billing_mode='b2b_split'` with the account's `copay_mode` (`none` / `percent` / `fixed`) —
  the patient share is billed on the OPD ledger (dues gate applies to patient-side targets), the payer share is a B2B
  ledger charge. Cutover imports each connector `report_routing_providers` key as an `insurance_tpa` account with its
  `report_routing_phone` as desk phone (ROLLOUT §2 stage A).
- **Evidence.** `test_tpa_visit_never_targets_patient`, `test_copay_visit`, `test_generic_resend_refuses_dx_rows`.

## ADR-DX-21 — Subjects without their own phone (B2B and hospital-shared numbers)
- **Context.** `patients.phone` is NOT NULL and unique per clinic; LIS rows need a `patients` row; hospitals enter one
  desk number for many patients (AUDIT A6).
- **Decision.** Each B2B account gets a **holder patient** (`dx_b2b_accounts.holder_patient_id`: a `patients` row whose
  phone is the account's desk/contact phone and name is the account name, created on first use). A B2B subject whose
  phone is missing or equals that phone is registered as a `family_members` row of the holder with
  `family_members.dx_b2b_account_id` set (new nullable column). `app/database.py::get_family_members` (the only
  patient-facing family list; three callers in `conversation.py`) adds `.is_("dx_b2b_account_id", "null")`, so the
  desk phone's WhatsApp chat never lists hospital patients. Such subjects never get a `patient` delivery (rule 5); the
  account copy is their delivery. A subject with their own distinct phone is a normal registry patient.
- **Evidence.** `test_b2b_subject_without_phone`, `test_family_picker_hides_b2b_dependants` (+ existing conversation
  tests unedited).

## ADR-DX-22 — Non-specimen items (imaging, ECG, PFT, audiometry) in a diagnostic visit
- **Decision.** `lab_tests.dx_result_mode` = `parameters` (LIS bands, default) / `document` (no tube; result = one
  text parameter *Impression* + the device PDF/image as `dx_result_attachments`, signed by a doctor-linked login
  holding `LAB_VALIDATE`) / `none` (billable only, e.g. a fee-like service). `document` and `none` orders have no
  sample; their stage starts at `pending_entry`. Full radiology reporting (G13 templates, PCPNDT Form F) for DX visits
  is DEFERRED (`DX-DEF-RAD`): G13 routes are OPD/IPD-gated and PCPNDT rules must be re-validated for walk-in USG.
- **Consequence.** A diagnostic centre can bill and report ECG/X-ray impressions now without bending the LIS.

## Open owner decisions (defaults in force)
| ID | Question | Default |
|:-|:-|:-|
| O-DX-1 | Collect sample before payment? | Allowed (`collect_before_payment=false`): walk-ins usually pay first at the desk; B2B never pays at collection |
| O-DX-2 | ~~Release report while dues pending?~~ | **Decided by the owner 2026-11-05: never** (ADR-DX-19); only an audited override |
| O-DX-3 | Approval levels; may the verifier also approve? | 2 levels; with 3 levels verifier ≠ technician, approver may equal verifier |
| O-DX-4 | Auto-verification of all-normal results per test | Off; per-test opt-in by lab director |
| O-DX-5 | Auto-release policy | `when_complete` (one final report), partial release by staff action |
| O-DX-6 | Separate invoice prefix for lab bills (`LAB-INV-`) vs shared `INV-` | Shared `INV-` (one ledger, one series) |
| O-DX-7 | Referrer copy of every report by default | Off; per referrer opt-in, patient consent captured at registration |
| O-DX-8 | Seed test library (common Indian tests with *indicative* ranges) | Offered as drafts; each test inactive until lab director approves its ranges |
| O-DX-9 | B2B white-label default for hospital accounts | On for `hospital` accounts, with the mandatory "Tested at" line |
| O-DX-10 | Retention of samples / reports | Report records kept (redact-in-place on erasure, OPD D3); physical sample retention days per department as data |
| O-DX-11 | Public report link TTL and verification | 72 h, last 4 digits of the registered phone, 5 attempts |
| O-DX-12 | Doorstep cash collected by a phlebotomist: does it unlock the report before the cashier accepts the handover? | No — held until handover (`dx_field_collections`) |
| O-DX-13 | Does a partial payment unlock the tests it covers? | No — any balance holds all patient-side targets |
| O-DX-14 | A "send my report again" button in patient WhatsApp chat | No (keeps the current product decision: reports are pushed, chat answers status only) |
| O-DX-15 | Label mode | One label per tube (`per_sample`) + optional register-book label; `per_test` selectable per print |
| O-DX-16 | Patient copy for corporate health-check panels | Off; per account authorisation with reason |
