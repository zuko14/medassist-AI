# Session 02: Code-Verified Review of the Specialty Expansion Plan

**Date:** 2026-09-15
**Agent:** Claude Code (Opus 5)
**Scope:** Review only. No application code or migrations were changed.
**Input:** The Session 01 implementation plan (derma / eye / dental / ivf plans, `specialty_treatments`, AI descriptions, specialty panels)
**Verdict:** The direction is right, but the plan is **not safe to implement as written**. It has three blockers that would break production. The corrected plan is below.

---

## 1. Intent (unchanged, confirmed with the user)
Extend Kriya beyond doctor- and department-based booking. Dermatology, eye, dental and IVF hospitals get:
- a treatment catalog with a 2-line AI description per treatment, written as an admin-reviewed draft;
- concern → treatment guidance;
- treatment-aware booking;
- dedicated plans in the owner console;
- the same admin panel design, with a dedicated URL per specialty.

Existing tenants must see zero change.

## 2. Claims in the Session 01 plan checked against the code

| Plan claim | Reality | Evidence |
|---|---|---|
| 76 migrations applied | ✅ True (001–076) | `migrations/` |
| `ILLMProvider` / OpenRouter in ai_engine | ✅ True | `app/services/ai_engine.py:25,42,160` |
| Design tokens `--bg #040A11`, `--surface rgba(28,45,63,0.72)` | ✅ True | `admin/index.html:24,28` |
| `withScope`, `GET /admin/me` returns plan | ✅ True | `admin/index.html:3527,3127` |
| New treatment bookings are protected by `uq_appointment_active_slot` | ❌ **FALSE.** The index only covers `booking_type='consultation'` | `migrations/064_fix_slot_uniqueness_key.sql:137-148` |
| Adding `treatment_procedure` to the `booking_type` CHECK is enough | ❌ **FALSE.** `appointments_time_required_for_consultation` rejects the new value | `migrations/039_appointments_lab_test_booking.sql:40-47` |
| Gate the flow on `has_feature(clinic, "specialty_treatments")` | ❌ **Leaks to existing tenants.** `enterprise` is a `*` wildcard, so every enterprise clinic returns True | `app/services/tenant.py:531-535,587` |
| RBAC `TREATMENTS_MANAGE` exists | ⚠️ It does not exist yet. It must be added next to `LAB_TESTS_MANAGE` | `app/services/permissions.py:13-31` |
| Symptom matcher handles "treatment for acne scars" | ⚠️ The clinical firewall blocks the phrase `"treatment for"` before any matcher runs | `app/services/clinical_firewall.py:163` |
| 5 "Menu Buttons" | ⚠️ Reply buttons max out at 3. The existing menu is a list (10 rows max), which works | `conversation.py:1405-1452` |

## 3. BLOCKERS (must change before any code)

### B1. Do not widen `appointments.booking_type`
If it is widened, every `treatment_procedure` insert fails the migration-039 CHECK. If that CHECK were also widened, those rows would silently lose:
- the slot uniqueness index, so **double booking becomes possible** (064);
- the doctor_id-required guard and slot pre-check (`app/database.py:1039,1064`);
- appointment reminders (`app/services/scheduler.py:513,574`).

Payment pricing would also fall into the consultation branch (`app/services/payment.py:149`).

**Fix:** treatment bookings stay `booking_type='consultation'`. Add nullable `appointments.treatment_id UUID REFERENCES specialty_treatments(id) ON DELETE SET NULL` and `treatment_name TEXT`. The index, guards, reminders, Razorpay flow, refunds and expiry are then all inherited with zero new code paths.

### B2. Gate on plan membership plus catalog data, not only on the feature flag
`has_feature()` returns True for every enterprise clinic, so a feature-only gate changes every enterprise tenant's WhatsApp menu.

**Fix:** `SPECIALTY_BY_PLAN = {"derma": "dermatology", "eye": "ophthalmology", "dental": "dental", "ivf": "ivf"}` in `tenant.py`. The WhatsApp specialty menu shows only when `has_feature(clinic, "specialty_treatments")` **and** the clinic has at least one active treatment. With no treatment rows, nothing changes. Enterprise tenants will still see an empty "Treatments" admin tab; that is acceptable, or the tab can be hidden unless the plan is in `SPECIALTY_BY_PLAN`.

### B3. Bookings are consult/evaluation slots, not procedure slots
Slots are single start times, and the index guards only the start minute. A 90-minute implant or hair transplant booked into a 15-minute slot leaves the following slots open. Clinically, LASIK, implants, IVF, and cataract surgery all need a doctor's evaluation before scheduling.

**Fix (v1):** "Book consultation for <treatment>". The treatment is shown on the appointment, and the procedure date is set by the clinic after assessment. `duration_minutes` is display-only in v1. Multi-slot procedure blocking is a separate, later project.

## 4. Design corrections (smaller diff, same result)

1. **Drop `specialty_treatments.specialty_type`.** The vertical comes from the clinic's plan. Use `category` for grouping.
2. **Replace `doctor_ids UUID[]` with a join table** `treatment_doctors(treatment_id, doctor_id)`, with FKs and `ON DELETE CASCADE`. Arrays cannot enforce FKs, so cross-tenant or deleted doctor IDs would go undetected. Follow the `doctor_branches` precedent (`migrations/010_branches.sql:53`).
3. **Copy the `lab_tests` catalog pattern:** table (`038`), CRUD with `require_permission`, name uniqueness per branch (`076`), and the unscoped-query linter annotations. Add `TREATMENTS_MANAGE` to `PERMISSIONS`.
4. **Price display:** `price_paise = 0` shows "Price on consultation". Otherwise show "From ₹X". Specialty prices vary per case, so a fixed price invites disputes. The WhatsApp payment stays the doctor's consultation fee (current flow). No procedure deposits in v1, because the refund paths in `payment.py` are consultation-shaped.
5. **AI descriptions are drafts.** The admin clicks Generate, edits, and saves; the bot never generates copy live for a patient. Prompt and seed copy must not promise outcomes: no "painless", "6/6 vision", "high success", "eliminates immediately", no testimonials. Run the output through `clinical_firewall.validate_llm_output`. Seeded starter menus are static curated text, not LLM output. Session 01's example hooks (§2 of the master doc) break this rule and must not be used verbatim.
6. **Plan feature sets:** define one `_SPECIALTY_FEATURES` set and reuse it for all 4 plans. Add `multi_branch` and `staff_training`, because the real target clients are chains (dental, eye, IVF). `lab_test_booking` (IVF: AMH, semen analysis) is enabled per clinic through the existing `clinics.features` override.
   > ⚠️ CORRECTED IN SESSION 03: the owner asked for test booking on the new plans, so the implementation plan puts `lab_test_booking` directly in the `ivf` plan. Other specialty clinics still get it through the override.

## 5. Clinical / legal guardrails (India)
- **IVF:** sex selection and sex determination are illegal (ART Act 2021, PCPNDT Act). Add firewall phrases (gender selection, "boy baby", "ladka hoga", Telugu/Hindi equivalents). This blocks only illegal requests, so apply it to all tenants.
  > ⚠️ CORRECTED IN SESSION 03: kept **out** of the implementation plan, which forbids any firewall change so existing tenants see zero change. It is still recommended, as a separate, owner-approved ticket. Until then, the plan's safeguards are: no gender words in seeded or AI copy (enforced by tests), and the bot never suggests anything beyond the clinic's own catalogue.
- **IVF privacy:** keep message text neutral ("your appointment", not "your IVF cycle") in reminder and confirmation templates. This is sensitive DPDP data on shared phones.
- **Firewall collision:** do not weaken `"treatment for"`. For specialty clinics, add a "Browse our treatments" row to the firewall's safe reply, plus a deterministic name/keyword search of the clinic's own catalog. The LLM matcher only ranks the clinic's catalog items and responds "consult our specialist about…", never "you need X".
- The drug-name firewall blocks catalog names containing words like "steroid" (for example "Steroid injection for alopecia"). Admins should use names like "Intralesional therapy". Surface the blocked word in the admin UI.
- NMC RMP Conduct Regulations 2023 (testimonial and result-image ban) are in abeyance; IMC 2002 still applies. Follow the stricter rule anyway.

## 6. Dedicated panel URLs
`/derma-panel` etc. are plain aliases that serve `admin/index.html`. **The URL must never change tenant scope or features.** The panel adapts only from `GET /admin/me` ([[tenant-scope-fails-closed]]).

> ⚠️ CORRECTED IN SESSION 03: the aliases do **not** need adding to the route allowlists. Both matrices only enumerate paths starting with `/admin` (`tests/test_admin_super_admin_scope_matrix.py`, `tests/test_phase2_route_adversarial_matrix.py`), and `/derma-panel` does not start with `/admin`.

## 7. Every place a plan slug is hardcoded (from the diagbooking commit `fa9b038`)
- `migrations/077_*.sql`: both CHECKs, the `bad_count` pre-check list (must list all 10 slugs), and `plan_tiers` seed rows
- `app/services/tenant.py`: `PLAN_FEATURES`, `FEATURE_LABELS` (guarded by a symmetry test)
- `app/services/message_accounting.py:261`: fallback dict
- `app/routers/platform.py:356`: `clinics_by_plan`; `:1603` `valid_plans`
- `app/routers/clinics.py:58,275`: both `Literal[...]`
- `admin/platform.html:580`: badge CSS; `:1026` and `:1495` both `<select>`s
- `migrations/verify_supabase_schema.sql`: new columns and table; `migrations/rollback/077_down.sql`
- tests: `test_plan_features.py`, `test_platform_roster_and_plan_features.py`, `test_clinic_settings.py`

## 8. Revised build order (one commit per step, tests green before the next step)
1. Migration 077: plans + `specialty_treatments` + `treatment_doctors` + 2 nullable appointment columns. No `booking_type` change. Include the rollback file.
2. Plan registry (§7 list) + `TREATMENTS_MANAGE`. Owner can onboard a derma clinic. Regression check: the other 6 plans' feature sets are byte-identical.
3. Admin treatments CRUD + admin tab + AI draft generator + curated seed menus.
4. WhatsApp: specialty menu gate (B2) → category list → treatment card → doctor list filtered by `treatment_doctors` → existing slot/payment flow with `treatment_id` in context.
5. Concern search: catalog keyword search first, then LLM ranking over the clinic's catalog, with firewall guardrails (§5).
6. Panel aliases, analytics "Top treatments".

## 9. Verification warning
`.env` points at **production Supabase**. A full pytest run leaves orphaned workers holding production scheduler locks ([[local-app-runs-steal-prod-locks]]). Run targeted test files, and after any full run, kill orphaned `python.exe --multiprocessing-fork` processes.

## 10. Open questions for the user
1. Pre-seed starter treatments per specialty? Recommended: yes, static curated text, all rows `is_active=false` until the admin reviews them.
2. WhatsApp payment for treatments? Recommended v1: consultation fee only; deposits later.

## 11. Research sources
- WhatsApp list limits (10 rows, 24/72 chars, 3 reply buttons): https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-list-messages/
- ART (Regulation) Act 2021: https://www.indiacode.nic.in/bitstream/123456789/17031/1/A2021-42%20.pdf
- NMC RMP Conduct Regulations 2023: https://www.nmc.org.in/rules-regulations/national-medical-commission-registered-medical-practitioner-professional-conduct-regulations-2023-reg/

## 12. Files changed this session
- `docs/SPECIALTY_EXPANSION_MASTER_MEMORY.md`: §3.3 corrected; session log row and per-session rule added
- `docs/sessions/SESSION_02_PLAN_FORENSIC_REVIEW.md`: this file
