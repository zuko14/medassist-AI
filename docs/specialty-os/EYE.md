# Kriya Eye Hospital OS (`eye` plan) — gaps and build plan

## 1. The segment
Eye hospitals in India range from single-surgeon cataract centres to chains (and to large charitable networks that run
outreach camps). Revenue lines: OPD, **cataract and refractive surgery packages** (IOL choice drives the bill), retina
injections, **optical shop** (often 20–30 % of revenue in small hospitals), pharmacy (drops), investigations (OCT,
fundus photo, perimetry, biometry). Typical software: a general HMS with an "ophthalmology EMR" add-on (e.g. Practo
Insta, MocDoc, KareXpert modules), specialty EMRs built by large eye institutes (e.g. LV Prasad's eyeSmart EMR), global
specialty EMRs (e.g. ModMed, Nextech) at the high end, plus a separate optical billing tool.

## 2. What Kriya has for eye hospitals (evidence)
| Capability | Evidence |
|:-|:-|
| Treatment catalogue with ophthalmology starter list, AI descriptions, concern search | `app/services/specialty_catalog.py`; migration 077 |
| Care pathway: doctor examines first, procedures are info cards that book an examination | migration 081 (`care_pathway`), SESSION_10 |
| OPD OS: encounters with vitals, free-text findings, ICD-10, signed prescriptions, orders | migration 103, 115, 120 |
| OT with WHO checklist, IPD packages | migrations 122, 116 |
| Pharmacy OS (drops), radiology PACS link fields | migrations 134–142, 123 (`pacs_viewer_url`) |

## 3. Gap register
| ID | Gap | Why it decides the sale | Close with | Priority |
|:-|:-|:-|:-|:-|
| EYE-1 | **Eye exam record**: VA (unaided/aided/pinhole, Snellen 6/x and logMAR), refraction (SPH/CYL/AXIS/ADD per eye, AR and subjective), IOP (method, time), slit-lamp findings by structure, fundus by structure, diagnosis per eye (OD/OS/OU) | Doctors will not leave a tool that cannot record refraction and IOP per eye | S2 forms engine: `eye_exam_v1` schema; printed spectacle prescription | P1 |
| EYE-2 | **Spectacle prescription** document (distance/near, PD, lens advice) separate from the drug prescription | handed to the optical shop and to the patient on WhatsApp | S2 + PDF block; WhatsApp document (existing `send_document`) | P1 |
| EYE-3 | **Optical shop**: frames/lenses/contact-lens items with barcodes, lens power stock, job order (Rx → fitting → lab → QC → ready → delivered), "spectacles ready" WhatsApp, advance + balance payment | 20–30 % of revenue; today done in a separate tool | S7: Pharmacy OS store kind `optical` + `optical_job_orders` | P1 |
| EYE-4 | Biometry & IOL calculation record (axial length, K1/K2, ACD, formula, chosen IOL power) per eye | surgical safety (wrong IOL is a never-event) | S2 `eye_biometry_v1` + OT checklist item "IOL power verified against biometry" (additive checklist item) | P2 |
| EYE-5 | **Surgery counselling & IOL estimate**: options (monofocal/toric/multifocal) with prices, patient chooses on WhatsApp | conversion of OPD to surgery | S5 estimates → IPD package (116) | P1 |
| EYE-6 | Post-op care schedule (day 1/week 1/month 1), drop schedule shown verbatim, danger signs | endophthalmitis risk; follow-up compliance | CC pack `eye_post_cataract` (voice for elderly) | P2 |
| EYE-7 | Anti-VEGF injection series tracking | missed injections lose vision; recurring revenue | CC `eye_anti_vegf` + S3 package | P2 |
| EYE-8 | Glaucoma review recall (IOP, fields) | chronic, lifelong | CC `eye_glaucoma` | P2 |
| EYE-9 | Diabetic retinopathy screening referrals from diabetes clinics | new patients for the eye hospital; cross-sell for multispecialty | CC `eye_dr_screen` + partner referral link | P2 |
| EYE-10 | Imaging links (OCT, fundus camera, perimeter, biometer) | doctors open scans from the record | S6 (extend 123's PACS link per encounter) | P3 |
| EYE-11 | **Cataract camp module**: camp registration, screening form, referral, transport, surgery date, post-op follow-up, NPCBVI-style counts | charitable and CSR-funded hospitals live on camps | S9 | P3 |
| EYE-12 | Paediatric: amblyopia/patching adherence, myopia-control follow-up | sticky long-term families | CC packs (later) | P3 |

## 4. Design notes
- **Form schema `eye_exam_v1`** (per eye OD/OS): `va {distance_unaided, distance_aided, pinhole, near}` (enumerated
  Snellen values + logMAR computed in code), `refraction {method: auto|subjective|cyclo, sph -30..+30 step 0.25, cyl
  -10..+10 step 0.25, axis 0..180, add 0..+4}`, `iop {mmHg 0..80, method: NCT|applanation|iCare, time}`,
  `slit_lamp {lids, conj, cornea, ac, iris, lens: grading LOCS-like text}`, `fundus {disc CDR 0..1, macula, vessels,
  periphery}`, `diagnosis [{icd10, eye}]`. Values are validated by the schema; nothing is computed except logMAR and
  spherical equivalent (display).
- **Optical job order** state: `draft → advance_paid → sent_to_lab → received → qc_passed → ready → delivered` or
  `remake`; frame and lens lines are RX items in an `optical` store (ledger, GST, POS reused); the Rx snapshot is copied
  from EYE-2 (never retyped). "Ready" sends the clinic's utility template `optical_ready_v1` with balance payment link.
- **GST:** spectacles, frames and lenses are goods with their own HSN and rates in the item master (verify current rates
  after the 2025 GST rate rationalisation); clinical services stay exempt via `gst_billing.py`.
- **Elderly patients:** the CC post-op pack defaults to `channel_pref=voice` when the patient's age ≥ 65 and the
  clinic has the AI receptionist.

## 5. Gating and zero impact
`specialty_forms_enabled(clinic, "eye")`: on for plan `eye` unless overridden False; for `multispecialty`, `womenchild`,
`enterprise` only with an explicit owner override (same rule as `specialty_enabled`, `app/services/tenant.py:908`).
Optical store requires `rx_access` and the owner flag `optical_enabled`. No existing table changes; the prescription
PDF prints the eye block only when an `opd_encounter_forms` row exists.

## 6. Tasks (IDs used in [README.md](README.md))
SP-EYE-01 schema + form UI + spectacle Rx PDF · SP-EYE-02 optical store kind + items import + POS · SP-EYE-03 job
orders + ready notification + balance link · SP-EYE-04 biometry form + OT checklist item · SP-EYE-05 IOL estimate
(S5) → package · SP-EYE-06 CC eye packs (CC-51) · SP-EYE-07 imaging links · SP-EYE-08 camp module (S9).
