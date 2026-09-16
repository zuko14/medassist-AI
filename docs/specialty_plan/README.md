# Specialty Hospital Plans — Implementation Plan (derma / eye / dental / ivf)

> **For agentic workers (Antigravity, Claude Code, any agent):** implement this plan task by task, **in file order**. Each step uses a checkbox (`- [ ]`). Do not start a task until the previous task's tests pass and its commit exists. Never skip a step, never "improve" code outside the lines a step names.

**Goal:** Add four specialty plans (Dermatology & Hair, Eye Hospital, Dental Clinic, IVF & Fertility). Each gets a treatment catalogue that clinic admins manage: starter treatments are pre-loaded, and admins publish, edit, delete, or add their own. Patients browse treatments on WhatsApp, search by concern, request a callback, and book a paid or unpaid consultation tagged with the treatment. All of this ships with zero behaviour change for existing tenants.

**Architecture:** A treatment booking is an ordinary `booking_type='consultation'` appointment with two new nullable columns (`treatment_id`, `treatment_name`). The double-booking index, doctor guards, reminders, Razorpay payment, refunds and expiry therefore apply unchanged. The WhatsApp specialty flow lives in one new module (`app/services/specialty_flow.py`), and `conversation.py` only gains routing lines. The flow switches on only when `specialty_enabled(clinic)` is true **and** the clinic has at least one active treatment. That check deliberately ignores the enterprise `*` wildcard.

**Tech Stack:** Python 3.12, FastAPI, supabase-py (PostgREST), PostgreSQL (Supabase), OpenRouter via `app/services/ai_engine.py`, Meta WhatsApp Cloud API, vanilla JS single-file admin panels, pytest (+ `pgserver` real-Postgres fixture).

**Spec:** `docs/SPECIALTY_EXPANSION_MASTER_MEMORY.md`, corrected by `docs/sessions/SESSION_02_PLAN_FORENSIC_REVIEW.md`. Where the two disagree, SESSION_02 and this plan win.

---

## Files in this plan (implement in this order)

| File | Task | What it builds |
|---|---|---|
| `00-global-constraints.md` | — | Rules every task must obey. **Read fully first.** |
| `01-migration-077.md` | Task 1 | Schema: plans, `specialty_treatments`, `treatment_doctors`, appointment tag columns, rollback, real-Postgres tests |
| `02-plan-registry.md` | Task 2 | New plans in every registry (tenant, permissions, platform, onboarding, owner console) |
| `03-catalog-and-db.md` | Task 3 | DB helpers, starter treatment lists, auto-seed on onboarding |
| `04-ai.md` | Task 4 | AI 2-line description generator + concern → treatment ranking (safe fallbacks) |
| `05-admin-api.md` | Task 5 | `/admin/treatments*` endpoints + `/admin/me` specialty fields |
| `06-admin-ui.md` | Task 6 | Admin panel Treatments page, staff permission, specialty panel URLs, booking chips |
| `07-whatsapp-flow.md` | Task 7 (Part A) | `app/services/specialty_flow.py`: browse, treatment card, concern search, callback, specialist pick |
| `07b-conversation-wiring.md` | Task 7 (Part B) | Exact edits wiring the flow into `conversation.py` (menu, routing, booking seed, symptom-step routes, context hygiene) |
| `08-booking-payment-analytics.md` | Task 8 | Tag bookings (paid + unpaid), prep note, admin alerts, insights |
| `09-verification-deploy.md` | Tasks 9–11 | Regression run, production deployment runbook, manual acceptance checklist |
| `10-session-memory.md` | Task 12 | Session log update |

## What gets built (feature list)

1. **Four new plans:** `derma`, `eye`, `dental`, `ivf`. They can be selected in the owner console onboarding and have plan tiers and quotas the owner can edit.
2. **Payments:** every specialty plan includes `payments_razorpay`. The existing Payment Settings page, per-clinic Razorpay keys, full/partial/none payment modes, refunds and expiry all work unchanged.
3. **Branches:** every specialty plan includes `multi_branch`. The existing branch selection, doctor↔branch mapping and branch-scoped slots work unchanged.
4. **Lab test booking:** included in the `ivf` plan (AMH, hormone profiles, semen analysis). Any other clinic can get it through the existing owner feature override.
5. **Treatment catalogue** per clinic:
   - Starter treatments are seeded **hidden** at onboarding.
   - The admin ticks the ones they offer and clicks "Show to patients", or edits, deletes, or adds new ones.
   - Fields: category, short WhatsApp title (24-character counter), "price from", duration, concerns, English/Hindi/Telugu descriptions, pre-visit instructions, and the doctors who perform it.
6. **AI description draft** button: a 2-line EN/HI/TE draft through OpenRouter. It blocks promise words ("painless", "guaranteed", "cure"…), runs the clinical firewall output check, and falls back to a template. Nothing reaches patients until the admin saves.
7. **WhatsApp for patients:**
   - "✨ Our Treatments" → categories → treatments → treatment card, with buttons: Book Consultation / Request Callback / All Treatments.
   - "🔍 Find by Concern" → keyword match, then AI ranking over the clinic's own catalogue, with a disclaimer.
   - Book Consultation → existing branch → who-for → name steps → **only the doctors mapped to that treatment** (branch-aware) → existing date → slot → confirm → payment.
   - The confirmation shows the treatment and the "Before your visit" instructions.
8. **Request Callback (lead capture for high-value treatments):** creates an in-app admin notification plus a WhatsApp alert to the clinic admin phone, deduplicated to once per 24 hours per treatment per patient.
9. **Firewall safety net:** when the clinical firewall blocks a message like "what treatment for acne", specialty clinics also get an "Our Treatments" button. The firewall itself is not weakened.
10. **Admin visibility:**
    - Treatment chip on Appointments/Payments rows.
    - Insights "by service" uses the treatment name.
    - Treatment shown in admin WhatsApp and in-app alerts.
11. **Dedicated URLs:** `/derma-panel`, `/eye-panel`, `/dental-panel`, `/ivf-panel` serve the same admin panel, with the same design and the same security. What the panel shows comes only from `GET /admin/me`, never from the URL.
12. **Staff permission** `TREATMENTS_MANAGE`, grantable per staff account.

## Deliberately NOT in this version (do not build)

- **Per-branch treatment catalogues or prices:** the catalogue is clinic-wide, and availability per branch comes from the doctors mapped to the treatment. Add `branch_id` only when a chain needs different catalogues per branch.
- **Multi-slot procedure scheduling:** bookings are consultation/evaluation slots, and the clinic schedules the procedure itself. `duration_minutes` is display-only.
- **Procedure deposits priced from `price_from_paise`:** WhatsApp payment stays the doctor's consultation fee under the clinic's existing payment mode.
- **Post-procedure aftercare messages:** these need approved Meta templates outside the 24-hour window.
- **Changing the clinical firewall's blocked phrases.**
