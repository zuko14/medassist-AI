# Session 11 — The `womenchild` plan: Women & Child hospitals

**Date:** 2026-09-18
**Branch:** main
**Migration:** 082 (`womenchild` plan slug + `specialty_treatments.service_line`)
**Plan doc:** `docs/specialty_plan/12-women-child-plan.md`

---

## 1. Intent

Photographs of the Rainbow Children's / BirthRight site (Visakhapatnam Health
City). Three mega-menus: **Child Care** (≈45 paediatric sub-specialties,
NICU/PICU, therapies), **Women Care** (≈22: pregnancy, delivery, fetal
medicine, gynae surgery, menopause, breast clinic) and **Fertility Care**
(≈13: IVF, ICSI, IMSI, IUI, FET, donor programmes). The ask:

* a dedicated plan for women & child hospitals;
* admin-panel sections — a child care treatment list, a women care list and a
  fertility list;
* the session-10 care pathway applied (the patient does not know which
  treatment they need; the doctor decides);
* real-world situations handled before a doctor has to ask;
* zero regression for the two live clients.

## 2. Decisions

| Decision | Choice | Why |
| :-- | :-- | :-- |
| Plan shape | Hybrid, `PLAN_FEATURES["womenchild"] = set(PLAN_FEATURES["multispecialty"])`, in `HYBRID_SPECIALTY_PLANS` | Paediatric and obstetric OPDs are departments with doctors; lab and scans are the diagnostics half. Derived so it cannot drift. |
| Sections | One nullable column `service_line` (6 slugs, CHECK) | "Surgery" is a category in Women Care *and* Fertility Care; category alone cannot separate lines. A column beats a naming convention the admin must remember. |
| When patients see sections | Only when published rows span **≥ 2** lines | The zero-regression rule: every pre-082 row is NULL, and a single-specialty clinic has at most one line. |
| Rows with no line in a split catalogue | Shown under "➕ Other Services" | Otherwise they would vanish from browsing. |
| Where the line is written | Seeding passes it **only for hybrid plans**; the panel sends it **only when the Section field is visible**; a create without one omits the key | A single-specialty clinic's insert and save payloads are byte-identical to before 082 — and cannot fail if the migration lags. |
| Onboarding | Seed `pediatrics`, `womens_health`, `fertility`, hidden, each under its line | Unlike multispecialty, this hospital's lists are known in advance. Each list is independent; one failing cannot stop the others or onboarding. |
| Care pathway | Paediatric / Gynaecology Consultation = `entry`; NICU, delivery, epidural, VBAC, gynae laparoscopy/hysteroscopy = `assessment_first`; sub-specialist rows `direct` | A parent describes the child; the paediatrician refers. A parent already referred to a child cardiologist can still book one by name. |
| Fertility list | Unchanged | It already serves the IVF plan; changing it would change that plan's starters. |
| PCPNDT | Deterministic firewall guard, all tenants | Fetal sex determination is a criminal offence in India; a maternity bot is asked constantly. The answer is fixed and must never reach an LLM. |
| Obstetric/newborn emergencies | Whole phrases only | "water broke", "baby not moving", "convulsion" are emergencies; "labour pain relief", "painless delivery?" are questions. |
| Picker header | "What We Treat", not "Our Services" | A hybrid hospital's main menu already has an "Our Services" row, and it lists departments. |

## 3. What was already true (and needed no code)

* **Booking for a child** — the "Who is this appointment for?" family-member
  flow puts the child's name on the booking (session 04 fixed the name).
* **"I don't know what my child needs"** — session 10's entry rows and the
  "Not sure? Tell us" path; with three entry rows the patient is *offered*
  Child / Gynae / Fertility consultation rather than guessed at.
* **Doctor-decided procedures** — session 10's `assessment_first` cards.
* **Branches, payments, staff accounts, holidays, reminders, follow-ups** —
  all inherited from the multispecialty feature set.

## 4. Files changed

**New:** `migrations/082_women_child_plan.sql`,
`migrations/rollback/082_down.sql`, `tests/test_women_child_plan.py`,
`docs/specialty_plan/12-women-child-plan.md`, this file.

**Modified:** `app/services/tenant.py`, `app/services/specialty_catalog.py`,
`app/services/specialty_flow.py`, `app/services/clinical_firewall.py`,
`app/services/ai_engine.py`, `app/routers/admin.py`, `app/routers/clinics.py`,
`app/routers/platform.py`, `app/services/message_accounting.py`,
`app/main.py`, `admin/platform.html`, `admin/index.html`,
`docs/CLIENT_ONBOARDING_SOP.md` (plan table, templates, E5 section, URLs).

**Tests updated deliberately:**
* `test_multispecialty_plan.py` — the hybrid set now has two members.
* `test_treatment_care_pathway.py` — the 081 parity check stays strict for
  every name 081 listed; names first shipped in 082 are declared in
  `PATHWAYS_ADDED_AFTER_081`.
* `test_specialty_admin_ui.py` and `test_phase2_route_adversarial_matrix.py`
  — **pre-existing breakage found on clean HEAD.** The venv has FastAPI
  0.138.2 (`requirements.txt` allows `>=0.115,<1.0`), which wraps every
  `include_router()` in an `_IncludedRouter`, so `app.routes` no longer lists
  sub-routes. The first test failed outright. The second was worse: the
  per-route **cross-tenant security matrix collected 1 test instead of ~100**
  and passed vacuously. Both now unwrap `original_router`, as
  `test_admin_super_admin_scope_matrix.py` already did; the matrix runs every
  admin route again and all pass. The app itself routes correctly on 0.138
  (`/admin/treatments` → 401, `/women-child-panel` → 200).

`clinics.provision_clinic`'s new loop was extracted into
`seed_plan_starter_lists()` so it can be tested without mocking onboarding.

## 5. Verification

`tests/test_women_child_plan.py` — **109 tests**: plan registry and the
untouched plans; registry sweep (validators, platform router, message
accounting, owner console, panel URL, migration, SQL↔Python slug parity);
catalogue (unique names across the three lists, one entry row per new list,
doctor-decided delivery rows, PCPNDT prep note, labels ≤ 24 chars in en/hi/te);
seeding (line written only when asked, never an unknown line, ordering after
existing rows, onboarding seeds three lists, one failing list isolated, no
other plan seeds anything new); admin API (validation, create omits the key,
update clears with "", hybrid-only starter filing, `/admin/me` sections);
WhatsApp (unsplit catalogue unchanged in four shapes, line picker, same-named
categories never mix, single-category line, stale/hostile `trtline_` taps,
card → All Treatments); **a full walk of the real 48-treatment catalogue in
en, hi and te** — every treatment reached by tapping, zero Meta-limit
violations; main menu within 10 rows; emergencies; PCPNDT positives and
false positives ("Gender: Female, need pregnancy scan" is not refused).

Mutation check: breaking the ≥ 2-line rule fails 2 tests; dropping the line
from a category tap fails 1. Source restored after each.

Panel inline JS passes `node --check`.

**Full suite** (`tests/` minus `test_multi_worker_smoke.py`, which boots
real workers against production): **2487 passed, 1 skipped, 0 failed** in
246.8s. The count rose from ~2390 because the cross-tenant matrix now runs
every admin route. Afterwards no project python / uvicorn / pytest process
was left running (the `local-app-runs-steal-prod-locks` check).

## 6. Deploy order

1. Apply `migrations/082_women_child_plan.sql` **before** the application —
   the new build writes `service_line` when a hybrid clinic seeds starters.
   Safe with the old build running: nothing reads the column yet.
2. Deploy the application.
3. Owner console → Create Client → **Women & Child Hospital**.
4. `/women-child-panel` → Treatments → per section: price, edit, link
   doctors, *Show to patients*.

## 7. Open items

- **Multispecialty clinics gain the Section field too.** If one files rows
  under two lines (or loads a second starter list after this deploy), its
  patients get the section picker, and pre-082 rows appear under "Other
  Services". Intended, but a visible change the first time it happens.
- **Vaccination and pregnancy (ANC) schedules are not built.** They are the
  two highest-value features for this plan, but both need proactive Meta
  templates approved per client WABA, a child DOB / LMP captured with
  consent, and a reminder job. The next session, not squeezed into this one.
- **Telugu obstetric emergency phrases** were not added — only English and
  Hindi phrases whose meaning is certain. A native-speaker review should
  supply them.
- **The fertility starter list was left unchanged** (no IMSI, donor
  programmes or ovulation induction rows). Donor programmes fall under the
  ART (Regulation) Act 2021 and should not be seeded by default.
- **FastAPI is unpinned within 0.x.** Worth pinning to the version the Docker
  image actually runs, so a test-only breakage like the one above cannot
  recur silently.
- **No live Meta or Supabase run.** Migration 082 was checked by review and by
  the SQL↔Python parity test, not applied to a database here.
