# Kriya IPD OS — Competitive Analysis

Research performed **2026-10-15** from public web pages only (no demos, no trials, no proprietary material).
Every claim below is the vendor's **publicly documented marketing claim** unless stated; none was independently
verified. "—" = not mentioned on the page reviewed (which does not mean absent from the product).

## Vendor identification
| Name in brief | Identified as | Confidence |
|---|---|---|
| MocDoc | MocDoc HMS — https://mocdoc.com/india/hospital-management-system-delhi | URL given |
| CrelioHealth | CrelioHealth — https://creliohealth.com/in/ | URL given. It is a **laboratory (LIMS/PACS) platform**, not an inpatient system; relevant only as a lab integration benchmark |
| SoftClinic | SoftClinic GenX HIS — https://www.softclinicsoftware.com/hospital-information-system-his/ | URL given |
| Digital IPD | https://digitalipd.in/features | URL given |
| eHospital | NIC eHospital — https://ehospital.nic.in/ | URL given (government HMIS) |
| "Crio" | **Not identified.** A web search found no hospital product by that name. It may mean CrelioHealth (formerly "Crelio"), but this is an assumption and was not substituted | Unresolved — owner to confirm |
| "Soflam" | **Not identified.** No product found by that name; possibly SoftClinic. Not substituted | Unresolved — owner to confirm |

## Capability comparison (public claims)
| Capability | MocDoc | SoftClinic | Digital IPD | eHospital (NIC) | CrelioHealth | **Kriya IPD OS v1 (built, tested)** |
|---|---|---|---|---|---|---|
| Admission & bed management | "Real-time bed status, multi-category bed mapping, flexible admission types" | "Admission Details", "Bed Management & Status Chart" | — | "ADT … registered and allotted bed in the ward" | n/a | Admission from canonical registry; bed board; concurrency-safe allocation (DB-proven); reservations with expiry |
| Transfers | — | — | — (not claimed) | ADT module | n/a | Atomic transfer with stale-screen protection and full bed history |
| Nursing station | "update vitals, medications, investigations, intake-output" | — | "entered once, at the bed"; tablet UI | within IPD module | n/a | Vitals + I/O with correction provenance, nursing/handover notes, MAR with reasons; responsive layout |
| Doctor rounds | "Log consultant visits, observations, treatment instructions" | "Round Notes" | — | clinical data, orders | n/a | Doctor-linked progress/plan/procedure notes, amendment trail |
| Orders (meds / investigations) | pharmacy request/issue, lab/radiology orders with billing | prescription, investigation | "Orders written against the same record", e-Rx to pharmacy | "order investigations, procedures and medicines" | requisition → order (AI "AiReq") | Doctor-authored orders, MAR; **no pharmacy stock or LIS link yet** (integration boundary) |
| Billing / packages | real-time billing, packages, consolidated discharge billing | billing & accounting | "running bill" | billing, receipts, refunds | "Revenue Guard" | Proposed→posted charges, deterministic bed-day accrual, one final bill in the shared OPD ledger (receipts, shifts, links); **no deposits/packages/TPA yet** |
| Discharge | "auto-filled discharge summary" | "Discharge Summary" | "auto-built discharge summaries" | — | n/a | Checklist enforced in DB; attending-doctor signature with registration; versioned amendments; waiver path |
| Audit | — | — | "NABH-ready audit trail", "time-stamped and attributed" | — | — | Per-stay event timeline + staff audit log; immutable clinical records |
| AI | — | — | — | — | AiReq, Smart Reports | Kriya Ketu: multilingual (en/te/hi) natural-language reads + hand-offs, user's own permissions, zero-LLM clinical path |
| WhatsApp | — | — | contact option | — | — | Not used for IPD in v1 (deferred: attendant updates need approved templates + consent) |
| ABDM | — | — | — | Integrated | — | Not yet for IPD |
| Integrations | pharmacy, lab, radiology | optional pharmacy, lab | pharmacy & billing sync | — | 100+ (HL7, FHIR, Epic, Cerner) | Shares Kriya's OPD, registry, billing, branches, staff; FHIR export exists for other entities only |

## Where Kriya can credibly win (and what is proven)
1. **One operational context OPD → IPD.** Same patient, MRN, doctors, staff logins, cashier and ledger — no
   second system or re-registration. *Proven*: admission uses the OPD registry; final bill is issued through the real
   OPD issue route in the E2E test.
2. **Verifiable integrity, not just screens.** Double allocation is impossible at the database level and that is
   tested under concurrency; clinical records are immutable with provenance. No competitor page makes a comparable,
   checkable claim.
3. **Safe AI.** Kriya Ketu answers "how many beds are free?" / "kitne khali bed hain" with live, permission-checked
   data, and hands clinical/billing actions back to a person. *Proven*: registry drift tests; zero-LLM guard.
4. **Owner-controlled, wind-down-safe modules** for multi-tenant hospital groups.

Not claimed: superiority on pharmacy, LIS, packages/TPA, ABDM, or mobile apps — Kriya v1 does not have them.
