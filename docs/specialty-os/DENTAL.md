# Kriya Dental OS (`dental` plan) — gaps and build plan

## 1. The segment
Single-chair to multi-chair clinics and dental chains. Revenue comes from **treatment plans** (RCT, crowns, implants,
aligners) sold after an estimate, paid in instalments, delivered over many sittings, plus 6-month hygiene recalls.
Typical software: practice-management tools with an odontogram, treatment plans, estimates, instalments, lab tracking
and recall SMS (e.g. Practo Ray in India; Dentrix / Open Dental / Curve as global references).

## 2. What Kriya has (evidence)
| Capability | Evidence |
|:-|:-|
| Dental starter catalogue, AI descriptions, concern search, care pathway | 077, 081 |
| **Dental treatment plans**: course with `tooth_numbers` (free text ≤ 60), `planned_sittings` 1–30, `quoted_amount_paise`, each sitting an appointment; WhatsApp consent; doctor digests; review buttons; owner-set quotas; per-clinic Meta templates | `migrations/089_dental_treatment_plans.sql:59-71`; `app/services/dental_plans.py` |
| Photos of handwritten sitting notes | `migrations/094_dental_sitting_photos.sql` |
| OPD OS, GST, payments, Pharmacy OS | 103, 117, 134–142 |

## 3. Gap register
| ID | Gap | Close with | Priority |
|:-|:-|:-|:-|
| DEN-1 | **Odontogram** (FDI two-digit notation, adult 11–48 + primary 51–85; per-tooth/per-surface status: caries, filled, missing, crown, implant, RCT, mobility, planned) — today teeth are a free-text field | S2 schema `dental_chart_v1`; chart UI; `tooth_numbers` keeps working (the plan form fills it from the chart selection) | P1 |
| DEN-2 | **Per-tooth treatment plan with estimate** (line per tooth/procedure, alternatives, discount, validity), accepted by the patient on WhatsApp | S5 estimates linked to `dental_treatment_plans` (estimate accept → plan `quoted_amount_paise` set; no change to 089 columns) | P1 |
| DEN-3 | **Instalments** for implants/aligners (schedule, payment links, reminders, receipts) | S3 instalments on OPD invoices | P1 |
| DEN-4 | **Recall engine** (6-month hygiene, perio 3-month, fluoride for kids) — the revenue engine of a dental clinic | CC pack `dental_recall` (or Care Lite if the clinic has no OPD OS) | P1 |
| DEN-5 | Ortho / aligner tracking (adjustment visits, aligner number n of N, change reminders, retainer check) | CC `dental_ortho` + plan sittings | P2 |
| DEN-6 | **Dental lab orders** (crown/bridge/denture/aligner: lab, shade, due date, sent, received, fitted, remake, cost) | S8 `dental_lab_orders` linked to the plan sitting | P2 |
| DEN-7 | Perio chart (pocket depths 6 sites/tooth, bleeding, recession) | S2 `dental_perio_v1` | P3 |
| DEN-8 | RVG/OPG image links and intraoral photos with consent | S1 + S6 | P2 |
| DEN-9 | Chair-wise scheduling (chair as a resource in addition to the dentist) | additive `resource_id` on sittings (design review needed — touches the slot index; **not** in v1) | P3 |
| DEN-10 | Compliance: AERB registration of X-ray units (licence numbers, renewal dates), biomedical waste logs | small compliance tracker (dates + reminders) | P3 |
| DEN-11 | Consent templates (extraction, implant, RCT, sedation) | S4 via `clinical_consents` (119) | P2 |

## 4. Design notes
- **Chart schema:** `{teeth: {"16": {"surfaces": {"O": "caries", "M": "filled"}, "status": "present|missing|implant|crown|rct", "mobility": 0..3, "planned": ["rct","crown"]}}}`;
  FDI keys validated against the closed set; primary teeth allowed only when the patient's age < 14 or staff confirms.
  The chart is versioned per encounter (signed with it) so history shows how a mouth changed.
- **Estimate accept on WhatsApp:** template `dental_estimate_v1` (utility, sent only to a patient who already has
  consented WhatsApp contact with the clinic) with buttons Accept / Call me; the PDF estimate is a document message;
  acceptance is recorded with the message id (no money moves on accept).
- **Quotas:** dental's own quota kinds stay unchanged; CC dental packs use the `care` kind.

## 5. Gating and zero impact
All features dental-only through the `DENTAL_ONLY_FEATURES` pattern (`app/services/tenant.py:649`) except S1/S3/S5,
which are shared blocks gated per clinic. Existing dental plan routes, jobs and templates unchanged
(`tests/test_dental_plans.py`, `tests/test_migration_089_dental.py` unedited).

## 6. Tasks
SP-DEN-01 chart schema + UI · SP-DEN-02 estimates (S5) + WhatsApp accept · SP-DEN-03 instalments (S3) · SP-DEN-04
CC dental packs (CC-52) · SP-DEN-05 lab orders (S8) · SP-DEN-06 perio chart · SP-DEN-07 imaging/photos · SP-DEN-08
consent templates · SP-DEN-09 compliance tracker.
