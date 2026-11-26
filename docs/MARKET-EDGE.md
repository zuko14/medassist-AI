# Kriya — Market Edge: what we still lack, and where we can lead (Nov 2026)

> Companion to [KRIYA_NEXT_ROADMAP.md](KRIYA_NEXT_ROADMAP.md). "Kriya has" claims were checked by searching `app/`,
> `migrations/`, `admin/` on `main` @ `c8c37ff`. Competitor descriptions are what Indian HIMS/specialty vendors
> typically offer; **confirm any named-vendor claim in a live demo before using it in sales material.**

## 1. Honest position
Kriya is ahead on: WhatsApp + voice front door in Indian languages, a deterministic clinical firewall, append-only
ledgers (stock, registers, observations), Diagnostic OS depth, and module isolation for live hospitals. After the
planned lanes (Continuous Care, Revenue Guard, Data import, retail pharmacy, specialty depth) it will also be the only
one that **proves outcomes in rupees**. It is **not** yet "nothing missing": the gaps below are what hospital RFPs and
doctors will ask for.

## 2. Parity gaps — things buyers expect that Kriya does not have today
| # | Gap | Evidence today | Who asks | Recommendation |
|:-|:-|:-|:-|:-|
| P1 | **ABDM beyond ABHA verification** (HIP/HIU: care-context linking, consent-based record sharing, scan-and-share registration) | `app/services/abdm.py` verifies ABHA / parses QR / fetches profile only | PM-JAY-empanelled and government-linked hospitals; ABDM incentive schemes | Build next: share signed prescriptions, discharge summaries and lab reports as FHIR bundles; push CC care plans later (CC-D3) |
| P2 | **Drug–drug interaction and dose-range checks** when the doctor prescribes | only allergy review exists on prescriptions (103 `allergy_review`) | every doctor evaluating an EMR | Deterministic checks against a licensed Indian drug database; warnings for the doctor only; never shown to patients; zero-LLM |
| P3 | **Doctor payouts / revenue sharing** (visiting consultants, per-procedure share, referral fees policy) | none found | almost every private multi-specialty hospital | Read-only payout statements from paid invoice lines + rules per doctor; no money movement in v1 |
| P4 | **More Indian languages** — WhatsApp and voice support en / hi / te only | voice `SUPPORTED_LANGS = ("te-IN","hi-IN","en-IN")` | any hospital outside AP/TS and the Hindi belt | Add ta, kn, mr, bn, ml, gu in order of target markets; templates per language per clinic |
| P5 | **Claims exchange (NHCX) and PM-JAY TMS** | none (forensic report Gap 3) | insurance-heavy hospitals | separate lane once a hospital has NHCX sandbox access |
| P6 | **Teleconsultation** (TPG 2020-compliant, ends in a signed prescription) | no teleconsult mode (CC AUDIT A12) | chronic-care follow-ups, rural patients | CC-D2 — also unlocks one-tap "adjust treatment" |
| P7 | **Procurement / trust pack**: ISO 27001 path, yearly third-party VAPT report, DPDP readiness note, data-residency statement, uptime SLA, DR drill evidence | process, not code | every chain and corporate hospital | prepare before the first enterprise tender |
| P8 | Offline counter resilience | forensic report Gap 2 | pharmacies, small towns | paper-fallback runbook now; offline design later (RETAIL-PLAN §7) |
| P9 | Accounting export (Tally) and GST e-invoice for taxable B2B | forensic report Gap 5; no IRN code | finance teams, pharmacy wholesale | Tally voucher XML export; e-invoice only where turnover rules require |
| P10 | Hospital support modules NABH checks: CSSD tracking, dietary orders, biomedical equipment maintenance, MRD file tracking, quality indicators | none found | NABH-accredited hospitals | light modules on request; quality-indicator report first (reads existing data) |
| P11 | Blood bank / blood storage centre registers | none found | hospitals with a licensed blood storage unit | build when a customer with a licence asks |
| P12 | HR / payroll / attendance | none | large hospitals | integrate with an HR product; do not build |

## 3. Where Kriya can lead (differentiators — build or deepen)
| # | Advantage | Why competitors struggle | Status |
|:-|:-|:-|:-|
| E1 | **Continuous Care with verified ₹ outcomes** | desktop HIMS have no patient channel; chatbots have no clinical record | planned (CC) |
| E2 | **WhatsApp + voice as the patient app** — booking, reports, payments, care cards, estimates, vaccine dues, "spectacles ready" | needs both the channel and the transactional core | live + planned |
| E3 | **Revenue Guard** — nightly ₹ leakage with recovered amounts | needs every module's data in one place | planned (RI) |
| E4 | **Switch in a weekend** — guided import with dry runs and undo | vendors charge for migrations and take weeks | planned (MI) |
| E5 | **Specialty depth that ends on the patient's phone** (eye, dental, derma, IVF, women & child) | specialty EMRs lack the channel; HIMS lack depth | planned (SP) |
| E6 | **WhatsApp QR self check-in at reception** (scan → identity → token → queue TV), using the existing queue display | needs WhatsApp + OPD queue together | new idea — small, very visible in demos |
| E7 | **UPI Autopay (e-mandate) for chronic-care packages and instalments** through the existing gateway | recurring hospital revenue without counter visits | new idea — needs gateway mandate support check |
| E8 | **WhatsApp Flows forms** (pre-visit intake, consent, PHQ-9, feedback) — native forms instead of chat | structured, validated input on the patient's phone | CC-D1 |
| E9 | **Doctor-reviewed AI scribe** (ambient notes → draft encounter; the doctor edits and signs; audio not kept; patient consent) | becoming a buying criterion in 2026; must stay NMC-safe | deferred by the product review until a customer asks — design it right after the CC pilot so it is ready |
| E10 | **Cross-specialty referral network** (diabetes → eye screening; pregnancy → GDM test; IVF positive → antenatal; newborn → vaccines) | requires one platform across specialties | CC PROTOCOLS §5 |

## 4. What we will not build (and why it is still an advantage to say so)
AI diagnosis from photos, scans, fundus images or embryos (diagnosis; would be a regulated medical device); AI
prescribing or dose changes (NMC); a separate patient mobile app (WhatsApp is already installed); marketing blasts on
care numbers (Meta quality risk to the hospital's number).

## 5. Recommended order after the current lanes
1. P7 trust pack (no code) and P4 languages (ta, kn first) — unblock new markets.
2. P1 ABDM HIP/HIU and P2 interaction checks — doctor and government-scheme credibility.
3. E6 QR self check-in and E7 UPI Autopay — small, visible, revenue-linked.
4. P3 doctor payouts — closes multi-specialty deals.
5. E9 doctor-reviewed AI scribe — after the CC pilot, with its own ADR (doctor signs, audio not retained, consent).
6. P5 NHCX, P9 Tally, P10 quality indicators — as customers need them.
