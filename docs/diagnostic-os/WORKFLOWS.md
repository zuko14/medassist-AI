# Kriya Diagnostic OS — End-to-End Workflows

> Part of the Diagnostic OS set: start at [status.md](status.md). Screens are in [UI.md](UI.md), routes in [API.md](API.md),
> tables/functions in [DATA-MODEL.md](DATA-MODEL.md). Each workflow lists the phase that delivers it; the phase's
> E2E test must replay the workflow exactly (TEST-PLAN.md §3).

Legend: **Desk** front desk/cashier · **Col** collector/phlebotomist · **Run** runner/logistics · **Rcv** lab receiving ·
**Tech** technician · **Sup** senior technician (verifier) · **Path** pathologist (doctor-linked) · **Disp** dispatch ·
**B2B** partner-hospital user · **Own** platform owner.

---

## W1 — Walk-in, self-referred, pays at the desk (P3, P4, P6, P7)
1. **Desk** opens *DX Registration*, searches the phone/name/MRN (registry search shared with OPD). Not found →
   registers (name, sex, DOB or age, phone, DPDP consent) → MRN.
2. Adds tests by code or name (`CBC`, `LIPID`, `HBA1C`) or a package; the cart shows MRP, container dots, fasting
   requirement and prep warnings (from `lab_tests.prep_instructions`, `fasting_required`). Sets priority, fasting
   status/last meal time, clinical notes.
3. **Save & Bill** → `POST /admin/dx/visits` (idempotency key per click) → `LAB-2026-000123`, items, orders, samples
   grouped by container, sharing the visit's sample sequence: e.g. `260100016570` (01 plain/serum: lipid) and
   `260700016570` (07 EDTA: HbA1c, CBC) — the same pattern the centre's MocDoc labels use today (ADR-DX-05). Draft invoice built
   (`dx_bill_visit`), discount within the user's limit, **Issue** → `INV-2026-…` → receipt (cash/UPI/card) on the
   user's open shift. Bill PDF printed / sent on WhatsApp.
4. **Print labels** → thermal PDF (50×25 mm) per tube: barcode, sample ID, patient name, age/sex, visit no., test
   short codes, tube colour band, collection time placeholder.
5. **Col** draws blood, sticks labels, presses **Collected** (or scans each label) → `dx_collect_samples` → orders
   `sample_collected`, visit `in_process`, TAT clock starts (if `tat_clock=registration` it started at step 3).
6. Same-site lab: **Rcv** scans → `received` (W6 for another site). Bench work W8. Approval W8/W9.
7. All tests signed → release `final` (auto, `when_complete`) → PDF → WhatsApp from the centre's number with the
   clinic's already-approved lab-report template (`template_name_for`; summary template when configured) → delivery
   receipts on the visit timeline. **Only because the bill is fully paid** (DELIVERY-POLICY rule 7); with dues the
   patient gets a payment link instead and the report follows automatically when the payment lands.

**Failure paths**: double click on Save → same visit (idempotency). Test not approved by lab director → 409 with the
test name. Network loss after Issue → retry is idempotent (OPD issue). Unpaid & `collect_before_payment` → collection
refused with "Awaiting payment".

## W2 — Doctor-referred walk-in with a report copy to the doctor (P3, P7)
As W1, plus at step 2 **Desk** picks a referrer (type-ahead on `dx_referrers.name_key`; "add new" inline with name,
qualification, phone). If the referrer has `report_copy='whatsapp'` and the patient consents (checkbox recorded in
`delivery_prefs.consent_at`), the release also goes to the referrer's number with the `dx_report_copy_referrer`
template. Report prints "Ref. by: DR N ANURADHA MD DM (NEURO)" from `referrer_snapshot`.

## W3 — WhatsApp (or voice) booking → visit (P3)
1. Patient books on WhatsApp today (live flow): test, date, centre/home, pays by Razorpay/PhonePe → `appointments`
   row `booking_type='lab_test'`, payment confirmed.
2. On the day, **Desk** sees *Today's bookings* in DX Registration (filter: branch, date, status confirmed). Taps
   **Arrived → Register** → `dx_register_visit` with `source='whatsapp_booking'` and **all of that patient's confirmed
   bookings for the day** (one test per booking, AUDIT A9) linked through `dx_accession_bookings`; patient and tests
   pre-filled; extra tests may be added.
3. Bill: the DX invoice has `appointment_id` NULL; after issue the service calls the existing `_apply_prepaid` once per
   linked booking (idempotent on the gateway payment id), so each online payment — full or deposit — becomes one
   `prepaid_online` receipt. Balance due = deposits not covering the test + added tests; the report waits for it.
4. Continues as W1 step 4. Booking is marked arrived/checked-in through the existing appointment path, so reminders
   and no-show logic stay correct.

## W4 — Home collection (P4 on top of migration 097)
1. Booking with `collection_mode='home'` (live). The visit is **not** created inside the payment-confirmation paths
   (four live money paths stay untouched). It is created by the dispatch desk's **Prepare today's route** action
   (*Home Collections* page, one call per day/branch, idempotent per booking via `dx_accession_bookings` unique `(clinic_id, appointment_id)`) or lazily
   on the phlebotomist's first scan (`source='home_collection'`). Samples are `pending_collection` with
   `barcode_source='preprinted'` when the centre uses sticker rolls, else generated barcodes printed for the route.
2. Phlebotomist portal (existing `GET /admin/home-collection/my`) gains a **Samples** panel: scan the sticker on
   each tube (camera barcode scan via `BarcodeDetector` API where supported, manual entry fallback) →
   `dx_bind_barcode` + `dx_collect_samples(mode='home')`; GPS and time already captured by 097 status `collected`.
3. Status `delivered` (097) = handed to the lab → creates/joins a batch from the phlebotomist to the processing site
   (W6), or direct receipt if the lab is the destination.
4. Visit bill: the booking's online payment (full or deposit, `payment.py` `deposit_percent`) is applied as a prepaid receipt; the home-collection fee that was folded into the booking amount appears as a `fee` item. Any balance collected at the doorstep is recorded by the phlebotomist (**Cash collected**, `dx_field_collections`) and becomes a receipt only when the desk cashier accepts the handover on their shift; until then the report is held (DELIVERY-POLICY rule 7, O-DX-12).

## W5 — B2B hospital (tie-up), white-label report to the hospital (P5, P7, P8)
Two variants; both carry the hospital as **Provider** (`b2b_account_id`) and optionally the hospital's doctor as referrer.

*Variant A — the hospital uses the partner portal:*
1. **B2B** user (confined login) opens *Partner → New sample*, enters patient (name, age, sex, hospital OP/IP no. =
   `external_ref`, phone optional), tests from the account's rate card, priority. → `dx_register_visit`
   (`source='partner_portal'`, `billing_mode` from the account) → B2B ledger `charge` rows at contract price.
2. The hospital nurse labels tubes with **pre-printed stock** issued to that account (`dx_barcode_stock`) or prints
   labels from the portal on its own printer.
3. Lab runner picks up: **Run** creates a batch from the account's pickup point (a `collection` site representing the
   hospital, or the account itself as source) → W6.
4. Report: template cascade picks the hospital's published white-label template (hospital logo, hospital address)
   with the mandatory footer "Tested at: <lab legal name, address, reg./NABL no.>" and the lab pathologist's
   signature. Delivery per `report_delivery`: `account` → hospital desk WhatsApp + portal download; `both` → also the
   patient (if phone and consent). Hospital prints from the portal (print log).

*Variant B — the hospital sends paper requisitions:* **Desk** at the lab registers with `source='b2b'`, the account
and `external_ref`; rest identical.

*Variant C — insurance / TPA / corporate health-check panel* (e.g. MEDIBUDDY, MD INDIA TPA, HEALTH ASSURE TPA):
1. **Desk** picks the payer account (kind `insurance_tpa` / `corporate`); the cart header shows a red **PAYER-ROUTED —
   report goes to <account> only** banner. `billing_mode` comes from the account: `b2b_credit` (payer pays all) or
   `b2b_split` (co-pay: the patient pays their share at the desk; the rest is a B2B ledger charge).
2. Collection, bench and approval as W1/W8.
3. Release → only the `account` delivery is created and sent to the payer's desk WhatsApp (`ai_summary=False`). The
   patient's phone is **never** a target (DELIVERY-POLICY rule 3); print-for-patient and share links are refused
   unless the account carries a recorded patient-copy authorisation. On `b2b_split`, the account copy goes out at
   once; nothing goes to the patient side until the co-pay is paid (and only if authorised).
4. Patient calls or messages "is my report ready?" → status only: "Your report has been sent to <account name>."

*Who is the patient record for B2B samples?* If the hospital gives the patient's own mobile, the patient is a normal
registry patient. If it gives none, or its own desk number (common: one number for many patients), the subject is
registered as a dependant of the account's holder patient with `dx_b2b_account_id` set — hidden from the WhatsApp
"who is this for" picker — and receives no patient-channel delivery (ADR-DX-21).

**Credit control**: an account `on_hold` (manual, over limit, or overdue beyond `credit_days`) cannot register new
visits (`409 dx_b2b_on_hold`); an IPD-style override with reason by `DX_B2B_MANAGE` is logged. Prepaid accounts are
refused when the balance does not cover the charge.

## W6 — Hub-and-spoke transit and receipt (accession) at the processing lab (P5)
1. Collection site **Run/Col** opens *Logistics → New batch* to destination (processing site or reference lab).
   Scans tubes in (`dx_batch_add`; refused if the tube's destination differs or it is already in a live batch).
2. **Dispatch** with carrier name and box temperature → samples `in_transit`; stability timers run from
   `collected_at` using the container's room/cold stability hours.
3. At the hub, **Rcv** opens the incoming batch, scans each tube; finishes with receipt temperature →
   `dx_receive_batch`: scanned → `received` (the "Pending Accession" queue empties), unscanned → `missing`,
   damaged marked; discrepancies notify the source site dashboard. Unknown barcodes are listed, never auto-received.
4. Received tubes appear on the bench worklists of their departments.

## W7 — Rejection and recollection (P4)
1. **Rcv/Tech** finds the tube haemolysed → *Reject* with reason `HEMOLYSED`, notes, "recollect" ✓.
2. `dx_reject_sample`: sample `rejected`; its live orders `cancelled` (`sample_rejected:HEMOLYSED`); replacement sample
   (`recollect_of`) and replacement orders `pending_collection`; event; TAT for those tests restarts at recollection.
3. Patient gets the `dx_recollection_required` WhatsApp template (reason in plain words, "no extra charge", centre
   timings / "book home visit" button). B2B visits notify the account desk instead.
4. If a result was already signed for that order, rejection is refused (`dx_sample_results_signed`) — use amendment.

## W8 — Result entry → verification → approval (P6, on top of migration 118)
1. **Tech** opens the department worklist tab *Entry*, sorted STAT → urgent → oldest; TAT bar per test.
2. Worksheet (existing LIS worksheet, extended): parameters in print order with the band that applies to this
   patient's age/sex, unit, reference text; instrument values prefilled when present (P10). Calculated parameters
   are read-only and recomputed server-side on save (Mean Blood Glucose, VLDL, LDL, ratios, eGFR…).
3. Save → `dx_record_results` → `lis_record_results`; flags computed server-side; delta check against the patient's
   previous validated value (`delta_check_pct` within `delta_window_days`); critical values create a detection event
   and the visit turns red on every dashboard.
4. Levels = 3: **Sup** opens *Verification*, reviews (deltas highlighted, previous values shown), **Verify**
   (`expected_updated_at`) or **Return** with comment (back to Tech; any re-save invalidates earlier verifications).
5. **Path** opens *Approval*: one result or a list; reviews, **Approve & sign** → `dx_approve_results` →
   `lis_validate_result` (signature snapshot incl. designation and signature image from `dx_signatories`).
6. Critical value with `critical_call_required`: before approval, Path or Sup records the call (who, role, channel,
   time, read-back) in the *Critical* drawer → `dx_critical_events` row. The referrer also gets an in-app/WhatsApp
   "urgent result" template if configured.

## W9 — Batch approval of normal results (P6)
*Approval → Normal results* lists results where every value is `normal`, no delta failure, no critical. Path
selects (or "select all on this page", max 50), reviews the compact grid, **Sign selected** → one call; each item is
validated in its own sub-transaction; the response lists signed / refused (with reason) per item; refused ones stay
in the queue. Never automatic.

## W10 — Release, dues hold, delivery, print, resend, share link, verify (P7)
1. Signature of the last pending test (policy `when_complete`) or **Release now** (partial) by Disp →
   `dx_create_release` → background render (template snapshot) → upload → `upload_and_send`.
2. Dues pending → every patient-side delivery `held_dues` (DELIVERY-POLICY rule 7 — an invariant, not a setting);
   patient receives `dx_payment_due` (template) with a payment link for the balance; the existing OPD payment-link
   settlement writes the receipt, the `dx_outbox` trigger re-evaluates, and the held deliveries go out — no staff
   action. Desk print is refused for the patient copy too unless an admin override with reason exists.
3. Delivery targets from `routing_snapshot`: patient, referrer copy, B2B desk, none (shadow). Receipts update the
   release and `lab_reports.delivery_status`.
4. *Reports & Dispatch* page: tabs **To release · Held (dues) · Sent · Failed · Printed**; actions Print (logs
   `dx_print_log`), Resend WhatsApp, Send e-mail (later), Copy share link (W10.5), Download.
5. Share link: `/public/dx/r/<token>` valid `share_link_ttl_hours`; asks for the last 4 digits of the registered phone;
   5 attempts; serves the stored PDF.
6. QR on every page → `/public/dx/verify/<verify_code>`: shows lab name, visit date, test names, release no.,
   status (current / superseded) and the SHA-256; no values, no patient name beyond initials.

## W11 — Amendment after release (P6, P7)
Tech re-enters values with an amendment reason (LIS v2), verification/approval as W8; `dx_create_release(kind='amended')`
includes the new version; the earlier release → `superseded`; patient/referrer/account receive the corrected report
with "Amended report — replaces report R1 of <date>"; verify page of R1 shows "superseded".

## W12 — Cancel a test / refund (P3)
Before collection: **Desk** cancels the item → orders cancelled; draft bill line removed. After issue: refund first
(existing OPD refund, idempotent), then cancel; B2B: `charge_reversal` ledger row. In process / resulted tests cannot be
cancelled (`dx_item_in_progress`).

## W13 — Outsourcing to a reference lab (P5, P7)
Routing rule sends a test to `dx_outsource_partners` X → tube grouped into a sample with `processing_site_id NULL` and
orders `outsource_partner_id = X` → batch to the partner → partner result arrives (PDF/e-mail): **Tech** enters
values on the worksheet with "performed at partner" (or attaches the partner PDF as `dx_result_attachments.kind='pdf'`),
Path approves. The report prints "Performed at: <partner legal name, address, NABL no.>" for those tests.

## W14 — B2B month-end (P8)
`DX_B2B_MANAGE` opens *B2B → Statements*, picks the period → `dx_b2b_issue_statement` → `B2B-2026-00012` PDF
(visits, patients, tests, contract prices, credits, opening/closing) → WhatsApp/e-mail to the account with a payment
link or bank details. Payments recorded (`payment` ledger rows, UTR required for NEFT). Ageing view 0–30/31–60/61–90/90+.
Daily `dx_b2b_hold_evaluation` applies/clears automatic holds.

## W15 — Hospital with in-house lab: OPD/IPD orders on the DX bench (P6b, task DX-41)
A doctor orders CBC in an OPD consult (G1). For a clinic with DX ACTIVE the order also appears in DX *Collection*
(as an internal visit, `billing_mode='internal'`, created by `dx_adopt_orders` — the money is already on the OPD bill
or IPD charges). Samples, logistics, verification and releases are DX; the OPD/IPD record shows the result as today.
Until DX-41 is VERIFIED, OPD/IPD lab orders keep using the G1 worklist unchanged.

## W16 — Analyser result (P10)
Tube loaded → analyser host-queries the bridge with the barcode → bridge `GET /internal/dx/bridge/orders?barcode=` →
returns machine codes for that tube's open orders → analyser runs → bridge `POST /internal/dx/bridge/results` →
`dx_instrument_results` → mapped to parameters → worksheet prefilled (or entered as `instrument:<id>` when
`instrument_auto_enter`) → W8 from step 4.

## W17 — Owner enables / disables the module (P1)
**Own** opens the clinic in the owner console → *Diagnostic OS* card → Enable: eligibility check, `provision_defaults`
(sites from branches, departments, containers, rejection reasons, settings) → `ACTIVE`; the panel shows *DX Setup*
with a go-live checklist (sites, at least one processing site, signatory with registration, approved tests, template,
delivery owner per branch). Disable with open visits → 409 preview (visits in process, samples in transit, unreleased
results, unpaid B2B) → confirm → `WIND_DOWN`.

## W18 — Day end
Cashier closes the shift (existing blind close; DX receipts count like OPD). *DX Dashboard* day-end: visits, tests,
revenue by site/referrer/account, pending by stage, TAT breaches, rejections by reason, critical calls; CSV export.
