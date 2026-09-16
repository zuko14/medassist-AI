# Session 03: Production implementation plan for the specialty plans

**Date:** 2026-09-15 → 2026-09-16
**Agent:** Claude Code (Opus 5)
**Branch / commits:** `main`, no commits. Docs only; nothing staged or committed.
**Plan tasks covered:** none implemented. This session **wrote** the plan in `docs/specialty_plan/`.

## 1. Intent for this session
The owner asked for a step-by-step implementation plan that an Antigravity agent can follow for the derma, eye, dental and IVF plans. It had to cover:
- the new plans, payment settings and integration, and test booking;
- branch selection;
- starter services that are auto-applied and that the admin can publish, edit or delete, plus custom services;
- useful real-world features;
- genuinely production-level quality, with **zero change for existing plans**.

## 2. What was done
| Item | Result | Evidence |
|---|---|---|
| Read the code paths the feature touches | Done | `conversation.py`: menu, routing, booking, symptom steps, doctor selection, confirmation. `payment.py`: create/notify. `database.py` lab catalogue helpers. `admin.py`: lab-test CRUD, `/me`. `clinics.py` provisioning. `platform.py`, `platform.html`, `index.html` gating. `tenancy.py`. Unscoped-query linter. Route matrices. `pgserver` fixtures |
| Wrote the implementation plan | Done | `docs/specialty_plan/README.md`, `00`–`10` + `07b` |
| Corrected Session 02 | Done | 3 `CORRECTED IN SESSION 03` notes in `SESSION_02_PLAN_FORENSIC_REVIEW.md` |

## 3. Files changed
- `docs/specialty_plan/*`: new, 12 files (the plan)
- `docs/sessions/SESSION_02_PLAN_FORENSIC_REVIEW.md`: route allowlist claim corrected; IVF `lab_test_booking` decision updated; firewall sex-selection phrases deferred
- `docs/sessions/SESSION_03_IMPLEMENTATION_PLAN.md`: this file
- `docs/SPECIALTY_EXPANSION_MASTER_MEMORY.md`: session log row

## 4. Tests run
None. No application code changed.

## 5. Orphan-process check
Not applicable; no app or pytest runs.

## 6. Decisions taken in the plan (binding for implementers)
1. **Treatment bookings stay `booking_type='consultation'`**, tagged with nullable `appointments.treatment_id` / `treatment_name` (migration 077). A real-Postgres test proves a tagged booking still collides on `uq_appointment_active_slot`.
2. **`specialty_enabled(clinic)`**, not `has_feature()`, gates everything. The enterprise wildcard never turns the feature on; an explicit override can.
3. **The WhatsApp treatment menu appears only when the clinic has ≥1 active treatment.** A newly onboarded clinic keeps the normal menu until its admin publishes treatments.
4. **Starter treatments are seeded hidden at onboarding** (15 derma, 12 eye, 14 dental, 12 IVF), priced 0, with static copy whose rules are test-enforced. The admin shows, edits or deletes them, or adds custom ones.
5. **Catalogue is clinic-wide** (no per-branch catalogue in v1). Branch availability comes from `treatment_doctors` ∩ `doctor_branches`.
6. **Payment = the chosen doctor's consultation fee**, under the clinic's existing Razorpay mode. `price_from_paise` is display-only.
7. **The `ivf` plan includes `lab_test_booking`**; all specialty plans include `payments_razorpay`, `multi_branch` and `staff_training`.
8. **New real-world features:**
   - Request Callback lead capture (admin notification + WhatsApp alert, 24h dedupe);
   - Find by Concern (keyword match, then AI ranking restricted to the catalogue);
   - "Before your visit" note after confirmation;
   - an Our Treatments button after firewall blocks;
   - treatment chips in admin;
   - treatment names in Insights.
9. **`/derma-panel`, `/eye-panel`, `/dental-panel`, `/ivf-panel`** serve the same page; no allowlist change is needed.
10. **The clinical firewall is untouched.** Sex-selection phrases are deferred to a separate ticket (see open items).

## 7. Production actions
None.

## 8. Open items / next session starts at
- **Next:** the implementer starts at `docs/specialty_plan/01-migration-077.md`, Step 0.
- **Owner decision needed (separate ticket):** add sex-selection and sex-determination phrases (EN/HI/TE) to the clinical firewall for all tenants.
- **Pre-existing bug found, not in scope:** saved-family-member and "For Me" bookings record `patient_name` instead of `booking_name`, so bookings show "Patient" (`_handle_selecting_family_member`). Recommend a separate fix.
- **Owner to confirm on the Render dashboard** whether `preDeployCommand: python scripts/migrate.py` actually runs in production (`09-verification-deploy.md`, Task 10 Step 1).
