# Kriya Diagnostic OS — Competitive Analysis (MocDoc, CrelioHealth)

> Part of the Diagnostic OS set: start at [status.md](status.md).
> Research date: **2026-11-05**. Method: public vendor pages and listings (via web search; direct page fetches
> were blocked by this session's network policy, so every vendor claim below is **vendor-stated, not verified
> product behaviour**), plus the owner's reference screenshots of a live MocDoc LIMS tenant
> (`mocdoc.com/investigation/listbydate/order/visakha-multispeciality-clinics`) and a real printed report
> (Taiyo Labs HbA1c + Lipid Profile). Re-verify anything legal or regulatory against primary documents before a
> phase ships.

The goal is **not** to copy either product (screens, names, workflows verbatim). It is to cover the *intent* of
every job a diagnostic centre does in them, then remove the work Kriya can automate.

---

## 1. What we observed in MocDoc (owner's screenshots)

### 1.1 Order list (`Investigation → List by date → Laboratory`)
| Observed element | What it tells us about the job |
|:-|:-|
| Stage tabs: **Pending Accession · Pending Completion · Pending Verification · Pending Approval · Pending Print · Printed · All · Misc** | A test moves through 5 work stages after billing. Each stage is a queue owned by a different person (receiving desk → technician → senior technician → pathologist → dispatch). Three-tier authorisation (completion → verification → approval) is also what MocDoc advertises publicly. |
| Filters: branch address (`#6-107/1B, Opp. Tiles Mart, Chandrampale…`), category (`Laboratory`), date range | Work is filtered by **site** and **department category**; a chain has many sites. |
| Columns: **Patient** (name, gender, age, `ID: VAM-57134` = patient ID/MRN, mobile, `OP No: 18328` = visit number) · **Referred By** (`DR VISIT HOMECARE`, `DR N ANURADHA MD DM(NEURO)`) · **Provider** (`DR VISIT HOME CARE`, `ARKA HOSPITAL`) · **Type** (`O/P`) · **Date** · **Status** (`Approved`) | Every visit carries a **referring doctor** *and* a separate **provider / B2B source** (a hospital or a home-care service that sent the patient or sample). These are different fields. `O/P` vs `I/P` distinguishes outpatient vs inpatient (hospital ward) samples. |
| Row icons (clock, printer) | Per-visit TAT/status indicators. |
| Expanded row: branch tag (`Madhurawada, Visakhapatnam`), one line per test with tube-colour icon (red = serum/plain, lavender = EDTA), test name, parent profile in italics (`(RENAL FUNCTION TESTS ACCUMAX)`), `No: 9452` (order line number), `SampleID: 260100016570`, date, **Track Sample**, sample edit pencil, **TAT: 30 mins** with a % progress bar | (a) A **profile/panel** explodes into member tests that are worked individually. (b) **One sample ID per tube**, shared by every test on that tube: CRP, uric acid, phosphorous, calcium all on `260100016570` (serum) while CBC is on `260700016570` (EDTA). The ID looks like `YY` + container code (`01`, `07`) + running number. (c) Per-test TAT targets and live elapsed %. (d) Sample tracking is a first-class action. |
| Visit action bar: info · bill · tag · barcode · search · WhatsApp · SMS · e-mail · download · print · edit · close | Per-visit actions the desk uses all day: view bill, print barcode labels, resend report by WhatsApp/SMS/e-mail, download PDF, print, edit registration. |

### 1.2 Kriya's own connector confirms more behaviour
`connectors/mocdoc/worker.py` (in production) scrapes the **Pending Print** tab and downloads approved reports.
It already handles: per-test top-up deliveries (`_topup_report_id`, `_plan_delivery` — a visit's tests are
released at different times), a **"bill payment" refusal** when a visit has dues (`_check_bill_payment_error`:
MocDoc blocks report download until the bill is paid), and provider-based routing to TPA desks
(`app/services/report_routing.py`). These are real-world requirements the Diagnostic OS must meet natively.

### 1.3 What the printed report shows (Taiyo Labs sample)
| Element | Requirement it implies |
|:-|:-|
| Branded header (logo, colour band, "DIAGNOSTIC REPORT") and footer page counter `Page 2 of 14` | Letterhead template per lab (and per B2B client), multi-page document with total page count. |
| Patient block: name, age/sex, **Referral** (`SELF`), **Bill ID**, **Collected / Received / Reported** timestamps, Sample ID + **barcode** | Three sample timestamps are printed; the report is tied to the bill and the sample barcode. |
| Department heading (`CLINICAL BIOCHEMISTRY`), test heading (`HbA1c (Glycated Hemoglobin)`, `LIPID PROFILE`) | Tests are grouped by department, each test has a print title. |
| Package heading (`TAIYO AARUSH EXECUTIVE`) above the parameters | Health packages are sold as one item but reported per test with the package name shown. |
| Columns: Test Description · Value(s) · Unit(s) · Reference Range | Standard 4-column layout. |
| `Method: High Performance Liquid Chromatography`, `Method: CHO-POD`, `Method: Calculated` under each parameter | **Method per parameter.** |
| Multi-line, *category* reference ranges (HbA1c: Non-diabetic / Pre-diabetic / Diabetic; LDL: Optimal / Near-optimal / Borderline / High / Very high; TG: Normal / Borderline / High / Very high) | Ranges are not just low–high numbers: printed **reference text** with several bands, while flagging still needs a numeric normal band. |
| Abnormal values in **bold** (7.5 %, TG 162) | Deterministic flagging drives typography. |
| **Calculated parameters**: Mean Blood Glucose 168.55 (= 28.7 × 7.5 − 46.7), VLDL 32 (= TG / 5), Non-HDL 94 (= TC − HDL), LDL 61, CHOL/HDL 3.35, LDL/HDL 1.54 | A safe **formula engine** with per-parameter decimals and rounding. (LDL printed 61 while Friedewald from unrounded VLDL is 61.6 → the lab truncates or rounds differently: rounding mode must be configurable per parameter and tested.) |
| Interpretation paragraphs, control-band tables ("Excellent Control 6–7 %…"), notes ("Correlate clinically", LAI screening advice) | Per-test **interpretation and notes text** printed after the values. |
| **HPLC graph** image from the analyser | Result **attachments** (instrument graphs, histograms, scatter plots) rendered into the PDF. |
| `**END OF REPORT**`, pathologist **signature image**, name, `MD Pathology`, `Consultant Pathologist` | Signatory block with signature image, qualification and designation; one signatory per department/page is common. |

---

## 2. MocDoc LIMS — public capability claims

| Area | Claim (vendor-stated) | Source |
|:-|:-|:-|
| Workflow | Three-tier authorisation: completion, verification, approval; customisable approval workflows; results within normal range can be auto-approved | [MocDoc lab results software](https://mocdoc.com/medical-lab-test-results-management-software) |
| Samples | Sample tracking from collection → testing → validation → approval → reporting, barcode support; "sample accession" listed as a feature | [MocDoc sample management](https://mocdoc.com/lab-sample-management-software), [MocDoc vs Probeta](https://mocdoc.com/blog/mocdoc-vs-probeta-lims) |
| Instruments | Analyser integration, multi-level validation, NABL-ready | [Top 5 LIMS India 2026](https://mocdoc.com/blog/top-5-laboratory-management-software-india-2026), [Cloud LIMS](https://mocdoc.com/lab-management-software) |
| Quality | Quality-control management module | [MocDoc QC](https://mocdoc.com/util/lab-management-system/quality-control-management) |
| Delivery | e-reports via WhatsApp and SMS; "WhatsApp report" feature | [Why MocDoc 2025](https://mocdoc.com/blog/why-mocdoc-is-the-best-laboratory-management-software-in-2025), [MocDoc MP](https://mocdoc.com/india/mp) |
| Reports | "Fully customised as per the need of the laboratory" | [Cloud LIMS benefits](https://mocdoc.com/blog/why-cloudbased-lims-software-like-mocdoc-is-transforming-indian-labs) |
| Field | Phlebotomist mobile app for pickups/collections | [Cloud LIMS](https://mocdoc.com/blog/cloud-laboratory-management-software-modernizing-labs) |
| Commercial | Invoice payment options for credit providers, franchise and corporate | [Techjockey listing](https://www.techjockey.com/detail/mocdoc-lab-management-system) |
| Scale | Single lab → multi-location chains; multi-location monitoring | [Capterra India](https://www.capterra.in/software/188380/mocdoc-lims) |
| Platform | Same vendor sells HMS (OPD/IPD/pharmacy); ABDM milestones and NABH HIS certification claimed | see [Hospital OS gap analysis §2.1](../hospital-os/GAP_ANALYSIS_AND_ROADMAP.md) |

Not publicly documented (found nothing): outsourced/send-out workflow detail, referral-doctor commission, franchise
revenue share, white-label B2B report templates. The owner's tenant shows a **Provider** column, so B2B sources exist.

## 3. CrelioHealth (formerly LiveHealth) — public capability claims

| Area | Claim (vendor-stated) | Source |
|:-|:-|:-|
| Accession | Unique label per sample, accept/reject at accession, scan to acknowledge in-house, at collection centres or outsourced sites; bulk printing; **pre-printed barcode** support; multiple barcodes per sample for vial tracking | [Sample management](https://creliohealth.com/lims/lab-sample-management/sample-management-system), [LIMS system](https://creliohealth.com/lab-software/lims-system/), [Q2 2025 recap](https://blog.creliohealth.com/q2-2025-recap-catch-up-on-whats-new-at-creliohealth), [accession & TAT](https://blog.creliohealth.com/how-sample-accession-helps-improve-overall-tat-and-efficiency-of-your-laboratory/) |
| Multi-centre | Collection-centre types incl. **franchise** and **corporate** logins; revenue sharing with franchisees; credit management with online payments | [Collection centre](https://creliohealth.com/solutions/multicenter-management/collection-center/), [multiple collection centres](https://creliohealth.com/solutions/multi-center-lab-management/collection-center/) |
| B2B | Automated billing/invoicing per customer account, e-mailed invoices, online and bulk settlement; **prepaid credit control with automatic lock/unlock** by balance and credit days; payment reminders via centre login, e-mail or login paywall | [B2B management](https://creliohealth.com/solutions/b2b-management), [B2B payment reminders](https://blog.creliohealth.com/manage-b2b-payment-collection-better-in-lab-with-automated-payment-reminders/) |
| Reference labs | Referral portal, automated referral communication, B2B inventory visibility | [Reference labs](https://creliohealth.com/solutions/reference-labs/) |
| Instruments | Bidirectional analyser interfacing (HL7, serial, TCP) | [Medical lab software](https://creliohealth.com/lims/medical-lab/medical-lab-software), [vs LabGuru](https://creliohealth.com/compare/creliohealth-vs-labguru) |
| Reports | Formats customisable without developers; "AI-based report format configuration"; accredited report formats | [G2](https://www.g2.com/products/creliohealth-for-diagnostics-product), [Reporting software](https://creliohealth.com/lims/diagnostic-lab/diagnostic-lab-reporting-software) |
| Delivery | WhatsApp, SMS, e-mail, fax; read tracking; branded patient portal and apps (book, pay, view results) | [vs Flabs](https://creliohealth.com/compare/creliohealth-vs-flabs), [Diagnostic lab management](https://creliohealth.com/us/lims/diagnostic-lab/diagnostic-lab-management) |
| Field | Phlebotomist dispatch app, home-collection management | same |
| Other | QC with anomaly detection, inventory add-on, PACS add-on; consent/AOE form templates | [Q1 2025](https://blog.creliohealth.com/creliohealth-top-features-updates-in-q1-2025/), [FitGap](https://us.fitgap.com/products/creliohealth-lims) |
| White label | Full white-labelling and branded portal in higher tiers (US/Canada pricing page) | [Pricing](https://creliohealth.com/pricing.html) |
| Price signal | Third-party: ~US$278 per centre per month (multi-centre); India LIMS ~₹10k–50k/month (competitor-published, treat with caution) | [SoftwareSuggest](https://www.softwaresuggest.com/us/live-health), [Relaso](https://billingpro.relaso.com/vs/creliohealth) |
| Weak spots (reviews) | Limited sample-handling flexibility and bulk report extraction mentioned in reviews | [G2 reviews](https://g2.com/products/creliohealth-for-diagnostics-product/reviews) |

## 4. Market and regulatory context (India)

| Topic | Finding | Design consequence |
|:-|:-|:-|
| Hub-and-spoke | Vijaya Diagnostic collects across a catchment and processes at hubs (new Kolkata hub); Dr Lal PathLabs FY24: 280 labs, 5,762 patient service centres, 11,619 pick-up points; Metropolis hub-and-spoke across 197 cities | Sites, routing rules, transit batches and receipt-at-hub are core, not optional ([FundsIndia on Vijaya](https://fundsindia.com/blog/equities/alpha-vijaya-diagnostic-centre-ltd-equity-research-desk-2/35151), [Business Today](https://www.businesstoday.in/amp/magazine/deep-dive/story/why-organised-diagnostic-players-are-targeting-small-town-india-m-474354-2025-05-06), [Metropolis](https://www.plindia.com/stocks/metropolis-healthcare-ltd/)) |
| NABL / ISO 15189 | Accreditation voluntary but expected; ISO 15189:2022 makes LIS verification mandatory; NABL 112 sets referral-lab selection/review rules; collection centres away from the main lab must comply too; acceptance/rejection of samples recorded with reasons and communicated back | Rejection reason codes, receipt records per site, referral-lab ("performed at") disclosure, LIS verification evidence ([NABL 112](https://nabl-india.org/wp-content/uploads/2019/02/NABL-112_Issue-No.-04.pdf), [ICMR GCLP](https://ethics.ncdirindia.org/asset/pdf/GCLP_Guidelines_2020_Final.pdf), [JLP 15189 comparison](https://jlabphy.org/translating-global-standards-to-local-practice-comparing-international-organization-for-standardization-151892012-and-2022-with-implementation-insights-from-the-national-accreditation-board-for-test/)) |
| TAT | Measure order→result (clinician view) and receipt→result (lab view) | Two TAT clocks per test ([OneCity LIS NABL](https://onecity.biz/blog/hospital-laboratory-information-system-nabl-india/)) |
| Who signs | MCI position: pathology reports countersigned by an RMP with PG pathology; later Clinical Establishments rules for basic labs: interpretive reports signed by at least an MBBS RMP; litigation on non-medical signatories; pathologist presence norms. **State adoption of the CEA varies.** | Signer = doctor-linked login with registration (already enforced by migration 118); signatory eligibility per department is clinic **data**, not code ([Medical Dialogues](https://medicaldialogues.in/news/health/hospital-diagnostics/mbbs-doctors-medical-mscs-can-run-basic-laboratories-health-ministry-notifies-new-clinical-establishment-rules-63197), [MCI clarification](https://medicaldialogues.in/amp/mci-board-of-governors-clarify-on-who-can-sign-laboratory-reports), [Delhi HC](https://medicaldialogues.in/amp/news/health/can-medical-msc-phd-sign-medical-diagnostic-reports-delhi-hc-asks-health-ministry-nmc-72543)) |
| Referral commissions | NMC conduct regulations (2002 code; 2023 regulations reported in abeyance — verify status) bar RMPs from commissions/gifts; IJME (2026) on commission-driven lab referral networks | Kriya builds **no** referral-commission or "doctor incentive" ledger (ADR-DX-11) ([IJME 2026](https://ijme.in/articles/diagnostic-and-laboratory-referrals-balancing-clinical-judgement-ethics-and-patient-autonomy/?galley=html), [National Herald](https://www.nationalheraldindia.com/national/nmc-allows-docs-to-refuse-treatment-of-violent-patients-relatives-bars-endorsing-drugs-or-getting-gifts)) |
| ABDM | NRCeS FHIR IG has `DiagnosticReportLab` / imaging profiles; diagnostic reports are an ABDM HI type | Structured results now (already) make an ABDM M2 export possible later (DX-DEF-ABDM) ([DiagnosticReportLab](https://nrces.in/ndhm/fhir/r4/StructureDefinition-DiagnosticReportLab.html)) |

## 5. Feature matrix — intent coverage and where Kriya goes further

Legend: ✓ public claim / observed · ~ partial · — not found · **K** = Kriya design (phase in [status.md](status.md)).

| Job | MocDoc | Crelio | Kriya Diagnostic OS | Kriya advantage |
|:-|:-|:-|:-|:-|
| Patient registration, MRN, family | ✓ | ✓ | **K P3** reuses OPD registry (one patient across OPD/IPD/DX) | One record across hospital + lab; no duplicate patient |
| WhatsApp test booking + payment | ~ | ~ | **Live today**; P3 converts a booking into a visit in one tap, prepaid applied | Booking → bill → sample without retyping |
| Home collection + phlebotomist app | ✓ | ✓ | **Live (mig 097)**; P4 adds barcode bind at doorstep | GPS, least-busy assignment already in production |
| Billing, receipts, shifts, refunds | ✓ | ✓ | **K P3** on the OPD ledger (gapless numbers, derived paid, blind shift close) | Money invariants enforced in Postgres and tested under concurrency |
| Referrer + B2B provider on the visit | ✓ | ✓ | **K P3/P8** | — |
| Panels / profiles / packages | ✓ | ✓ | **K P2** explode to per-test work units | — |
| Tube-wise sample IDs + label print | ✓ | ✓ | **K P4** container grouping is automatic | Wrong-tube prevention: a test can only go on its container |
| Pre-printed barcodes | ? | ✓ | **K P4/P8** | Bound at collection, unique per clinic in the DB |
| Accept / reject / recollect | ~ | ✓ | **K P4** reason codes, auto WhatsApp recollection request | Patient is told within minutes, not when they call |
| Hub-and-spoke transit batches | ~ | ✓ | **K P5** manifests, scan-in discrepancy, stability timers | Sample-stability timer alerts before a sample goes bad |
| Outsourcing to reference labs | ? | ✓ | **K P5** + "performed at" on report | NABL 112 disclosure is mandatory, not optional |
| Result entry, ranges by age/sex, flags | ✓ | ✓ | **Live (mig 118)**; P6 adds formulas, delta check, attachments | Deterministic, zero-LLM, tested pure functions |
| Completion → verification → approval | ✓ | ✓ | **K P6**: 2 or 3 levels per clinic, DB-enforced | Signature binds to the exact version reviewed (`expected_updated_at`) |
| Auto-approval of normals | ✓ | ? | **K P6**: auto-*verification* only; approval stays human, with **batch sign** | Legal signer always a human; still one click for 40 normal reports |
| Critical values | ? | ? | **K P6** critical-call log with read-back | NABL-style evidence of who was told when |
| Amendments | ? | ? | **Live (mig 118)** versioned; P7 re-release supersedes | Old PDF watermarked SUPERSEDED |
| Report templates / letterhead | ✓ | ✓ | **K P7** template cascade: B2B account → branch → clinic | Versioned templates, snapshot on release: reprint is byte-identical |
| White-label for B2B hospitals | ? | ✓ (tiered) | **K P7/P8** | Built-in, with mandatory "Tested at" line |
| Partial (progressive) release | ✓ (top-up) | ? | **K P7** | Native; no scraper |
| Report delivery WhatsApp/SMS/e-mail | ✓ | ✓ | **Live pipeline** (`lab_reports`), P7 adds referrer + B2B desk routing | Sent from the centre's own WhatsApp number, delivery receipts already tracked |
| Report gated on dues | ✓ | ~ | **K P7** + payment link on WhatsApp | Patient can pay and receive in the same chat |
| Report authenticity | — | — | **K P7** QR verify + SHA-256 | Anti-forgery for fake-report fraud |
| Cumulative / trend values on report | ? | ✓ | **K P7** previous value printed per parameter | — |
| B2B credit, prepaid lock, statements | ✓ | ✓ | **K P8** ledger append-only, auto-hold on limit | Statements and dues on WhatsApp |
| Partner (hospital) portal | ? | ✓ | **K P8** confined login | Same tenancy guards as staff |
| Analyser interfacing | ✓ | ✓ | **K P10** outbound-only local bridge | No inbound port at the lab; HMAC + replay window like CallMedex |
| IQC (Levey-Jennings, Westgard) | ✓ | ✓ | **K P10** | — |
| MIS, TAT dashboards | ✓ | ✓ | **K P9** | — |
| Staff AI assistant | — | — | **Ketu P9** R0 reads + hand-offs | "Kitne samples pending hain?" answered with the user's own permissions |
| Voice receptionist | — | — | **Live** booking; P9 "is my report ready?" (status only) | — |
| Hospital OPD/IPD + lab in one system | ✓ (HMS) | — | **Live** OPD/IPD + DX | Ward orders reach the lab bench without a requisition slip |
| Referral commissions | ? | ? | **Refused** (ADR-DX-11) | Compliance by design |
