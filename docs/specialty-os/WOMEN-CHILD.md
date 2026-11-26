# Kriya Women & Child OS (`womenchild` plan) — gaps and build plan

## 1. The segment
Women & child hospitals (paediatrics with NICU, obstetrics and gynaecology, fertility) live on **long relationships**:
a pregnancy is ~10 antenatal contacts, a delivery, postnatal visits, then the child's vaccination and growth visits for
years. Typical software: general HMS with OBG/paediatric templates, separate vaccination reminder apps; government
systems (RCH portal, MCP card, U-WIN for routine immunisation) run alongside.

## 2. What Kriya has (evidence)
`womenchild` = multispecialty features (`app/services/tenant.py:605`); service lines child/women/fertility with
picker only when ≥ 2 lines (082); PCPNDT sex-determination guard and obstetric/newborn emergency phrases (SESSION_11);
radiology with PCPNDT Form F (123); OT, IPD, LIS, insurance; ICD-10 Z-codes for immunisation in masters (120).

## 3. Gap register
| ID | Gap | Close with | Priority |
|:-|:-|:-|:-|
| WC-1 | **Pregnancy episode (ANC record)**: LMP, EDD (doctor-confirmed; dating-scan EDD wins), gravida/para/abortion/living, risk factors, blood group/Rh, each ANC contact (BP, weight, fundal height, FHR, oedema, urine, Hb), scans with Form F link, GDM test result, Td doses, IFA/calcium as prescribed | `obs_pregnancies`, `obs_anc_visits` (S2 forms) | P1 |
| WC-2 | ANC schedule + danger-sign education + home BP | CC `anc` (PROTOCOLS §6) | P1 |
| WC-3 | Delivery record (date/time, mode, outcome, baby details) → creates the newborn as a family member under the mother's account | `obs_deliveries` + `family_members` row (existing table) | P1 |
| WC-4 | **Immunisation module**: schedule from DOB (clinic picks IAP 2023 or national UIP), due/overdue, vaccine given (brand, batch, expiry, site, given by) with stock decrement through Pharmacy OS when on, certificate PDF, catch-up decided by the doctor | `imm_schedules` (code-defined, versioned), `imm_doses`; CC `child_immunisation` | P1 |
| WC-5 | **Growth charts**: weight, length/height, head circumference, BMI → WHO z-scores (0–5 y WHO standards, 5–19 y WHO references; IAP charts optional) computed in code from LMS tables | S2 `child_growth_v1`; CC `child_growth` | P2 |
| WC-6 | Postnatal care (mother) and newborn checks; postnatal depression screen (EPDS/PHQ-9) | CC `postnatal` | P2 |
| WC-7 | Birth reporting reminders (registration with the Registrar within the statutory period), birth certificate data sheet | report + reminder | P2 |
| WC-8 | Government interoperability: RCH ID / MCP card fields; U-WIN export for vaccines | store IDs; CSV export in the government format where a public spec exists — **no unofficial portal automation** | P3 |
| WC-9 | NICU specifics (daily weight, feeds, phototherapy, ventilation) | IPD nursing charts are generic today; NICU flowsheet via S2 forms on IPD | P3 |
| WC-10 | Delivery / ANC packages and estimates | S3 + S5 + IPD packages | P2 |

## 4. Safety and law
- **PCPNDT:** no template, card, form field or AI output may disclose fetal sex; ANC cards are fixed approved texts;
  the firewall rule stays unchanged; Form F stays in radiology.
- **Child data:** guardian (account holder) consent; growth and vaccine data visible to the guardian only.
- **Danger signs:** `anc_danger_signs` and newborn danger signs (poor feeding, fast breathing, fever/cold, convulsions,
  yellow palms/soles) are fixed cards with the hospital emergency number.

## 5. Gating and zero impact
On for `womenchild` unless the owner overrides; for `multispecialty`/`enterprise` only with an explicit override
(`obgyn_enabled`, `paediatrics_enabled`). New tables only; the newborn uses the existing `family_members` table through
its existing creation path.

## 6. Tasks
SP-WC-01 pregnancy episode + ANC form · SP-WC-02 delivery → newborn · SP-WC-03 immunisation module (+ Pharmacy OS
stock link) · SP-WC-04 growth z-scores · SP-WC-05 CC packs (CC-54) · SP-WC-06 birth reporting · SP-WC-07 RCH/U-WIN
fields + export · SP-WC-08 NICU flowsheet.
