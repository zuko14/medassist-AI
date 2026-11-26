# Owner Platform (admin/platform.html) — plan catalogue, segments and module configuration

## 1. What the owner console does today (evidence)
Single-page dashboard: KPI cards, clinic leaderboard with `#planFilter` (12 plans hard-coded, `admin/platform.html`
~1227–1240) and status filter; **Create Hospital / Clinic** modal with `#ccPlan` select (`:2405-2418`) and
`applyPlanFieldVisibility()` (hides the daily report limit for plans without lab reports, `:3072-3085`); clinic detail
modal with toggles for lab reports, corporate health, Kriya Ketu, voice receptionist, OPD, IPD, Diagnostic OS, Pharmacy
OS (`:1946-2042`, 409-preview flows `toggleIpd/Dx/Rx`); broadcasts; financial control centre (079); support inbox.
Backend: `/platform/*` in `app/routers/platform.py` (`clinics_by_plan` `:379`, `update_plan_tier` `:2336`).

## 2. Gaps
| ID | Gap | Risk today | Fix |
|:-|:-|:-|:-|
| OC-1 | **A plan slug lives in 7+ places** (tenant registry, clinics.py ×2, platform.py ×2, message_accounting, platform.html ×3, migration CHECKs) | a new plan missed in one place → 400s or silent KPI gaps | `tests/test_plan_registry_drift.py`: parses each place and asserts the set equals `PLAN_FEATURES` keys (labels via `FEATURE_LABELS`); no runtime change |
| OC-2 | No **segments**: hospitals, specialty, diagnostics and (new) pharmacies are one list | owner cannot run the pharmacy business separately | segment tabs (All · Hospitals & clinics · Specialty · Diagnostics · Pharmacies) computed from plan; per-segment KPI strip (§3) |
| OC-3 | One create form for every kind of customer | wrong fields (doctors for a shop, report limits for a pharmacy) | segment-specific wizards: hospital/clinic, specialty, diagnostic, **pharmacy** |
| OC-4 | Module toggles do not explain eligibility | owner clicks a toggle and gets a 400 | eligibility line under each toggle from a new `GET /platform/clinics/{id}/modules` (state, eligible, reason, dependencies: IPD→OPD, RX→OPD or pharmacy plan, CC→OPD) |
| OC-5 | New modules need toggles and limits | — | toggles: **Continuous Care** (`care_enabled`), **Revenue Guard** (`revenue_guard`), specialty modules (`optical_enabled`, `ivf_lab_enabled`, `obgyn_enabled`, `paediatrics_enabled`, specialty form packs); monthly **care** message limit next to the dental limits |
| OC-6 | No value view for renewals | renewals argued on features, not money | per clinic "Value this month": RI ₹ flagged / ₹ recovered, CC verified visits/labs/refills ₹, messages sent and estimated Meta cost |
| OC-7 | No onboarding progress | go-lives slip silently | per clinic checklist from MI wizard + module setup pages (OPD setup, Pharmacy setup, Care setup, templates approved) |
| OC-8 | Toggles that change patient messaging are one click | accidental patient-facing change | typed confirmation (clinic name) for CC, voice, broadcast enablement |

## 3. Pharmacies segment (with docs/pharmacy-os/RETAIL-PLAN.md)
- **List:** shops/chains on plan `pharmacy` (and, as a filter, hospitals with Pharmacy OS on): stores, today's sales
  (from `rx_dashboard`), expiring stock value (90 days), open refill and order requests, last login, subscription state.
- **Create pharmacy wizard:** legal name, WhatsApp number (E.164), GSTIN (checksum), shops → branches with drug licence
  numbers (retail/wholesale), pharmacist name + council registration, default store per shop, message limit, plan tier.
  On submit: clinic (plan `pharmacy`), branches, `pharmacy_enabled` on, default stores provisioned (existing RX-06 path),
  admin login invite.
- **Detail modal for a pharmacy:** Pharmacy OS state (cannot be disabled while stock or open sales exist — existing
  WIND_DOWN), Ketu, voice; OPD/IPD/DX/CC toggles hidden (not eligible) with the reason shown.

## 4. API additions (owner-only, `verify_owner_credentials`)
| Method | Path | Notes |
|:-|:-|:-|
| GET | `/platform/plan-catalogue` | slugs, display names, segment, features (labels), default message quota — read from `tenant.py`/`plan_tiers` |
| GET | `/platform/clinics/{id}/modules` | every module: state, eligible, reason, dependencies |
| GET | `/platform/segments/summary` | KPI strip per segment |
| POST | `/platform/pharmacies` | the pharmacy wizard (transactional; idempotency key) |
| GET | `/platform/clinics/{id}/value?month=` | RI + CC + messaging value figures |
Existing `/platform/clinics` create/update stays the path for every other plan.

## 4a. Visual rules
Blueprint and rules: [UI spec §7.10](../design/NEXT-LANES-UI-SPEC.md). The console is **dark-only** with its own tokens
(`--radius: 16px`), so additions use `admin/platform.html`'s tokens and sprite (`<symbol id="i-…">`), not the clinic
panel's; segment tabs and wizard reuse the console's existing filter-select, stat-card, modal and switch markup.

## 5. Zero impact
Additive endpoints; UI changes inside `<!-- OC:… -->` blocks; existing selects keep their options (new option appended);
drift test is test-only. Existing owner flows (create clinic, toggles, plan tiers, broadcasts, finance) unchanged and
their tests unedited.

## 6. Tasks
OC-01 drift test · OC-02 plan catalogue API · OC-03 modules API + eligibility lines · OC-04 segments + KPI strip ·
OC-05 pharmacy wizard (with RXR-06) · OC-06 CC/RI/specialty toggles + care limit · OC-07 value view · OC-08 onboarding
progress · OC-09 typed confirmations.
