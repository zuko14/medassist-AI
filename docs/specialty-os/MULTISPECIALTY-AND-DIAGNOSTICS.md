# Kriya Multi-Specialty Hospitals and Diagnostic Centres — gaps and build plan

## 1. Multi-specialty hospitals (`multispecialty`, `polyclinic`, `essential`, `enterprise`)
These tenants already have the broadest Kriya surface (OPD, IPD, OT, emergency, LIS, radiology, insurance/TPA, GST,
Pharmacy, Ketu, voice). Their gaps are not modules but **outcomes, money and switching**:

| ID | Gap | Close with | Priority |
|:-|:-|:-|:-|
| MS-1 | Chronic patients lost between visits (diabetes, hypertension, thyroid, depression) | Continuous Care core packs (docs/continuous-care) | P1 |
| MS-2 | Work done but not billed | Revenue Guard (docs/revenue-integrity) | P1 |
| MS-3 | Migration from the old HMS | Data import (docs/data-import) | P1 |
| MS-4 | Department-specific exam records (ophthalmology, dental, ANC, growth) inside one hospital | S2 forms engine with per-department enablement (EYE/DENTAL/WOMEN-CHILD docs) | P2 |
| MS-5 | Readmission prevention after discharge | CC `post_discharge` pack + structured discharge follow-up date | P2 |
| MS-6 | Live claims exchange (NHCX) and PM-JAY TMS integration | forensic report Gap 3 — separate lane after the NHCX sandbox is available to the clinic | P3 |
| MS-7 | Quality indicators for NABH (e.g. return to OT, readmission within 30 days, medication errors) | derived reports over existing tables | P3 |
| MS-8 | Teleconsultation (follow-ups) | CC-D2 | P3 |

## 2. Diagnostic centres (`diagstream`, `diagbooking`)
Kriya's Diagnostic OS (125–133) is complete for the lab itself. The gaps that grow a diagnostic centre:

| ID | Gap | Close with | Priority |
|:-|:-|:-|:-|
| DXG-1 | **Repeat-test recall** (HbA1c every 3 months, lipid yearly, TSH, vitamin D, kidney function for diabetics) with home-sample booking | Care Lite `dx_periodic` (CC-D5) — no doctor inbox; recall cadence from a clinic-approved test list | P1 |
| DXG-2 | Referring-doctor relationship: doctor-wise volumes, report delivery to the doctor's WhatsApp with patient consent | DX B2B/partner portal exists (132); add doctor digest (template) | P2 |
| DXG-3 | Corporate camps / health checks with employee follow-up | corporate health (095) + CC Care Lite | P3 |
| DXG-4 | Analyser drivers beyond the current bridge (forensic Gap 1) | DX bridge (133) driver library | P3 |

## 3. Notes
- Care Lite changes `cc_eligible` for diagnostic plans (ADR-CC-01 amendment): packs limited to `dx_periodic`; the
  "decide" step is a fixed interval, not a clinical rule; out-of-range results already go to the referring doctor via
  the DX delivery policy — Care Lite never interprets them.
- Every item here reuses the CC, RI and MI lanes; no diagnostic-specific schema beyond Care Lite settings.
