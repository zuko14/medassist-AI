# Kriya Pharmacy OS — Competitive Analysis

> Part of the Pharmacy OS set: start at [status.md](status.md). Researched 2026-11-16 from public vendor pages,
> software directories and reviews (sources at the end). **Caveat:** competitor capabilities below are what the
> vendors and listings publicly claim; none were verified hands-on. Before using any row in sales material, confirm
> it in a demo (ROLLOUT owner checklist). Where a competitor is stronger, this document says so.

## 1. Who we compete with, and for what
| Product | Category | Typical buyer | Deployment |
|:-|:-|:-|:-|
| **Marg ERP 9+** (Marg Compusoft) | Retail/distribution pharmacy ERP | Standalone chemists, distributors, hospital outlets run as shops | Windows desktop (with online add-ons) |
| **MocDoc HMS** (pharmacy module / MocDoc Pharmacy) | Cloud HMS with pharmacy | Small–mid hospitals, clinics | Cloud |
| **Insta HMS by Practo** | HMS with pharmacy & inventory / supply chain | Mid–large hospitals, chains | Cloud or on-premise |
| **MediXcel** (Plus91) | EMR / practice management / HIS | Clinics, specialty chains | Cloud |

Kriya's position: the hospital already runs OPD, IPD and diagnostics on Kriya, and patients already talk to it on
WhatsApp. Pharmacy is the module that closes the loop **doctor's signed Rx → counter → ward → bill → refill**, without a
second system, and with statutory registers that are tamper-evident by construction.

## 2. Feature benchmark
Legend: ✓ publicly claimed · ◐ partial / add-on / unclear · — not found in public material · **K** = Kriya design.

| Capability | Marg ERP 9+ | MocDoc | Insta HMS | MediXcel | Kriya Pharmacy OS |
|:-|:-:|:-:|:-:|:-:|:-|
| Batch & expiry tracking | ✓ | ✓ | ✓ | — | ✓ batch identity includes MRP (DPCO revisions are separate batches) |
| FEFO auto-pick | ✓ (vendor claims FEFO / auto batch selection) | ◐ | ◐ | — | ✓ ranked view + split across batches + override reason |
| **Hard DB lock on expired sale** | — (not claimed) | — | — | — | ✓ trigger refuses any expired/blocked out-movement regardless of UI (ADR-RX-02) |
| Stock as append-only ledger | — | ◐ ("stock movement" reports) | ◐ | — | ✓ ledger is the truth; balance is a trigger-only projection; nightly proof (ADR-RX-01) |
| Negative-stock prevention under concurrency | ◐ (single-PC desktop rarely concurrent) | — | — | — | ✓ row-locked balance + CHECK; 50-thread test (TEST-PLAN §3) |
| GST billing / returns support | ✓ strong (GSTR, e-way bill, e-invoice) | ✓ | ✓ (accounting push to Tally) | — | ✓ inclusive-MRP split, bill of supply gate, GSTR-1 sections; **weaker**: no e-way bill / e-invoice in v1 |
| Multi-store & transfers | ✓ (multi-godown) | ✓ multi-store, ward request | ✓ stock transfers | — | ✓ central / counter / ward sub-store / OT tray with dispatch–receipt acknowledgment |
| Purchase order & GRN | ✓ strong | ✓ | ✓ procurement | — | ✓ PO → GRN with scheme/CD/free goods, landed cost, duplicate-invoice block |
| Distributor connectivity (auto PO/order import) | ✓ (Marg ecosystem) | — | ◐ | — | — (GAP G17; manual) — **Marg is ahead** |
| Prescription → counter integration | — (shop ERP) | ✓ "prescription integration" | ✓ | ◐ (e-prescribing to pharmacies) | ✓ signed OPD Rx auto-loads by token/MRN/QR, lines mapped and learned (ADR-RX-07) |
| IPD ward indent → dispense → bill | — | ✓ ward request, IP issue | ✓ | — | ✓ indent bound to **active doctor order**, charge posted atomically, MAR batch traceability (ADR-RX-04) |
| Schedule H1 / X / narcotic registers | ✓ (H1 register reports are common in Indian chemist software) | ◐ | ◐ | — | ✓ written in the sale transaction, gapless, **hash-chained**, prescriber reg. no. enforced by CHECK (ADR-RX-05) |
| Stock audit with variance approval | ✓ physical stock entry | ✓ barcode audits | ◐ | — | ✓ blind count, live delta during sales, second approver above threshold |
| Keyboard-first fast billing | ✓ **strong** (Marg's core strength) | ◐ web forms | ◐ web forms | — | ✓ F-key POS, scanner wedge, 3-s target; **parity, not superiority**, with Marg's desktop speed |
| Offline billing | ✓ desktop works offline | — | ◐ on-prem | — | — (GAP G15) — **Marg is ahead** for unreliable internet |
| WhatsApp chronic refill reminders | — (Marg sells separate e-order apps) | — | — | ◐ patient engagement | ✓ derived from the actual sale, consented, deterministic, reply REFILL → queue (W10) |
| Staff voice/chat stock lookup | — | — | — | — | ✓ Kriya Ketu R0 "Is Augmentin 625 in stock?" in en/hi/te (KRIYA-KETU §2) |
| Zero-LLM clinical boundary | n/a | n/a | n/a | n/a | ✓ AST-enforced (ADR-RX-06) — AI never suggests a molecule or dose |
| One system with OPD, IPD, Lab, WhatsApp | — | ✓ HMS | ✓ HMS | ✓ EMR/HIS | ✓ plus WhatsApp booking/reports already live |
| Pricing (public) | ~₹5.5k–7.2k one-time per single user (listings vary) | not published | not published / transaction-based (listings vary) | not published | module subscription on top of Kriya plan (owner pricing decision) |

## 3. Where legacy desktop/web interfaces hurt hospitals (and how Kriya answers)
1. **Two systems for one patient.** A shop ERP at the hospital counter does not see the doctor's signed Rx; staff
   retype drug names from paper. *Kriya:* the Rx is already structured and signed in OPD (103); POS loads it by token.
2. **Ward medicine billing leaks.** When indents are paper or a separate module, dispensed medicines miss the IPD bill
   or get billed twice. *Kriya:* dispense and charge are one transaction with an idempotent `source_ref` (ADR-RX-04).
3. **Mutable stock numbers.** Counters edited "to match" leave no trail; inspectors and auditors cannot reconstruct.
   *Kriya:* every unit's path is a ledger row; variance needs reason and approver.
4. **Registers as reports.** A register printed from editable sales changes when a sale is edited. *Kriya:* register
   rows are snapshots chained by hash and verified nightly.
5. **Patients forget refills.** Chronic patients (diabetes, hypertension) drift to other chemists. *Kriya:* the
   hospital's own WhatsApp number reminds them from the actual quantity sold — no extra app.
6. **Training burden.** Desktop ERPs are fast but have hundreds of masters and menus. *Kriya:* eleven pages, one POS
   screen with a key map overlay, mobile screens for nurses and audit counts.

## 4. Honest gaps vs competitors (feed GAP-ANALYSIS)
| Gap | Who is ahead | Plan |
|:-|:-|:-|
| Offline POS | Marg (desktop) | G15 — evaluate a service-worker queue only for OTC cash sales; controlled drugs never offline |
| Distributor e-ordering / purchase import from distributor files | Marg | G17 — CSV/XLSX import of supplier invoices into GRN draft (P10 stretch) |
| E-invoice / e-way bill | Marg | G14b — not needed for B2C retail; needed only for B2B supply above thresholds |
| Accounting export (Tally vouchers) | Insta, Marg | G16 — CSV day-book export in P10; Tally XML later |
| Credit sales / patient accounts | Marg | O-1 deferred |

## 5. Positioning statement (for owner review)
"For hospitals already on Kriya, Pharmacy OS turns the doctor's signed prescription into a 3-second counter bill and
the ward's medication order into a stock-checked, auto-billed dispense — with expiry and narcotic controls the database
itself enforces, and refills that bring chronic patients back through the hospital's own WhatsApp."

## Sources
- Marg: [Marg vs eVitalRx comparison (vendor)](https://margcompusoft.com/m/compare-pharmacy-software-marg-erp-vs-evitalrx/),
  [Medical store software (vendor)](https://margcompusoft.com/retail/medical_store_software.html),
  [SoftwareSuggest listing](https://www.softwaresuggest.com/marg-pharmacy-management),
  [Techjockey listing](https://www.techjockey.com/detail/margerp-9), [Slashdot listing](https://slashdot.org/software/p/Marg-ERP-9/)
- MocDoc: [Stock control module blog (vendor)](https://mocdoc.com/blog/pharmacy-management-software-stock-control-module),
  [FitGap listing](https://us.fitgap.com/products/045691/mocdoc-pharmacy-management-system),
  [GetApp listing](https://www.getapp.com/all-software/a/mocdoc-hms/), [Pricing page](https://mocdoc.com/util/hms-pricing)
- Insta HMS: [Pharmacy and inventory (vendor)](https://instahms.com/pharmacy-and-inventory),
  [Supply chain (vendor)](https://www.instahms.com/hospital-supply-chain-management),
  [Techjockey listing](https://techjockey.com/detail/insta-hms), [GetApp AU](https://www.getapp.com.au/software/122155/insta-hms)
- MediXcel: [Capterra listing](https://www.capterra.com/p/10031353/MediXcel/), [Capterra India](https://www.capterra.in/software/1077940/MediXcel)
- GST on medicines (ADR-RX-08 context): [PIB rate document, Sep 2025](https://static.pib.gov.in/WriteReadData/specificdocs/documents/2025/sep/doc202594628401.pdf),
  [Business Today FAQ](https://www.businesstoday.in/industry/pharma/story/cancer-rare-disease-medicines-exempted-from-gst-other-drugs-at-5-check-faqs-492416-2025-09-03),
  [KPMG note on the 56th GST Council](https://kpmg.com/in/en/blogs/2025/09/gst-councils-56th-meeting-a-pivotal-development-for-indias-pharmaceutical-sector.html)
