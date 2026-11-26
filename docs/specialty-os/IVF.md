# Kriya IVF & Fertility OS (`ivf` plan; Fertility Care line of `womenchild`) — gaps and build plan

## 1. The segment
ART clinics and banks registered under the **ART (Regulation) Act 2021** and ART Rules 2022, many also under the
Surrogacy (Regulation) Act 2021 and the PCPNDT Act. Work is organised around a **couple** and a **cycle** (IUI, IVF,
ICSI, FET, donor cycles), with an embryology lab, cryo-storage, high-value packages and intense patient anxiety.
Typical software: dedicated IVF EMRs (e.g. Meditex IVF, IDEAS) and electronic witnessing systems (e.g. RI Witness) at
the high end; many Indian centres run a general HMS plus spreadsheets for the lab.

## 2. What Kriya has (evidence)
`ivf` plan = specialty features + `lab_test_booking` (`app/services/tenant.py:587`); fertility starter catalogue with
care pathways; women & child service line `fertility_care` (082); LIS for AMH/hormones/semen analysis results (118);
radiology with PCPNDT Form F and the sex-determination firewall rule (123, `clinical_firewall.screen_message`);
`clinical_consents` (119); OT and IPD packages.

## 3. Gap register
| ID | Gap | Close with | Priority |
|:-|:-|:-|:-|
| IVF-1 | **Couple record**: two linked people (female partner, male partner or donor), shared history, both identities verified (photo ID type + last 4), ART Act eligibility facts (age, marital/consent status as the Act requires) | `ivf_couples` linking two `patients`/`family_members` rows; identity check stored | P1 |
| IVF-2 | **Cycle record**: type, protocol name (text by doctor), start date, stimulation chart (day, drug & dose **as prescribed in signed Rx**, follicle sizes per ovary, endometrium, E2/LH/P4 from LIS), trigger date-time, OPU, fertilisation, transfer, freeze, outcome | `ivf_cycles`, `ivf_cycle_days` (S2 forms per day) | P1 |
| IVF-3 | **Embryology lab**: oocytes (count, maturity MII/MI/GV), fertilisation (2PN), embryo grading by day (Gardner for blastocysts), disposition (transfer/freeze/discard with consent ref), **double witnessing** at each critical step (two staff logins, timestamps) | `ivf_gametes`, `ivf_embryos`, `ivf_witness_events` (append-only, hash-chained like RX registers 141) | P1 |
| IVF-4 | **Cryo inventory**: tank → canister → cane/goblet → straw/device position; storage start, consent expiry, renewal billing, discard with consent | `ivf_cryo_locations`, `ivf_cryo_items`; CC `ivf_cryo_renewal` | P1 |
| IVF-5 | **ART registers & reporting**: data the Act/Rules require the clinic to keep and report to the National ART & Surrogacy Registry; donor records and limits (e.g. single lifetime oocyte donation as the Act provides) enforced in code; retention as prescribed | register views + exports; donor-limit checks; **legal confirmation of formats required (owner decision, like RX O-4)** | P1 |
| IVF-6 | Cycle timeline on WhatsApp for the couple: scan dates, **trigger time exactly as entered by the doctor with an acknowledgement button**, OPU/transfer instructions (fixed approved texts), beta-hCG date | CC `ivf_cycle` (PROTOCOLS §6; SECURITY H4) | P1 |
| IVF-7 | **Sensitive result delivery**: beta-hCG and cycle outcomes are never auto-sent; the doctor/counsellor releases them (a DX-style delivery hold) | delivery policy flag on the result for IVF couples | P1 |
| IVF-8 | Cycle packages, estimates and instalments (drugs often excluded) | S3 + S5 | P2 |
| IVF-9 | Counselling notes and psychological support (PHQ-9 optional, opt-in) | CC `phq9` pack with counsellor | P3 |
| IVF-10 | Andrology: semen analysis (WHO 6th edition reference values), sperm freezing | LIS template + cryo items | P2 |
| IVF-11 | PGT orders and results (PGT-A/PGT-M) — **never sex reporting** (PCPNDT) | orders through LIS outsourcing (DX logistics); results stored without sex fields; firewall rule unchanged | P3 |

## 4. Safety and law (non-negotiable)
- **No AI on embryos, scans or results.** Grading is entered by the embryologist; Kriya computes nothing clinical.
- **PCPNDT:** no field, report, template or message may carry fetal or embryo sex; the existing sex-determination
  firewall rule covers patient messages; exports strip any such free text by schema (no free-text field in PGT results).
- **Witnessing:** a disposition (transfer/freeze/discard) cannot be recorded without two distinct logins within a
  short window; the witness chain is append-only and verified nightly (RX register pattern).
- **Consent before disposition:** every freeze/discard/donation references a `clinical_consents` row of the right
  template and the correct partner(s).

## 5. Gating and zero impact
Plan `ivf`, or `womenchild`/`multispecialty` with an owner override `ivf_lab_enabled` (never the enterprise wildcard),
and OPD OS on. All tables new; existing catalogue, booking and LIS flows unchanged; results under delivery hold use
the existing LIS status machine plus a new hold flag read only by the IVF delivery path.

## 6. Tasks
SP-IVF-01 couple + identity · SP-IVF-02 cycle + day chart · SP-IVF-03 embryology + witnessing chain · SP-IVF-04 cryo
inventory + renewal billing · SP-IVF-05 registers/exports (after legal confirmation) · SP-IVF-06 CC `ivf_cycle`
(CC-53) · SP-IVF-07 sensitive-result hold · SP-IVF-08 packages/estimates · SP-IVF-09 andrology templates.
