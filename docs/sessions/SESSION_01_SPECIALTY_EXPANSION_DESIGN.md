# Session 01: Multi-Specialty Hospital Architecture Expansion

**Date:** 2026-09-15  
**Context:** Production-Active Kriya AI System  
**Lead Objective:** Comprehensive Forensic Analysis & End-to-End Production Implementation Plan for Dermatology, Eye (Ophthalmology), Dental, and IVF/Fertility Verticals.

---

## 1. Context & Baseline Audited

### Current Production State
- Multi-tenant FastAPI SaaS on Python 3.11 with Supabase PostgreSQL (76 completed migrations).
- Meta WhatsApp Cloud API integration with resilient backoff, 24h conversation windows, and interactive lists/buttons.
- Existing plan tiers: `soloclinic`, `diagstream`, `diagbooking`, `essential`, `polyclinic`, `enterprise`.
- Rigid anti-double-booking protection implemented via partial unique index `uq_appointment_active_slot` on `(clinic_id, doctor_id, appointment_date, appointment_time)`.
- UI architecture:
  - Hospital admin panel in `admin/index.html` (8,151 lines) with "Clinical Depth" design tokens.
  - Platform owner console in `admin/platform.html` (3,431 lines).
  - OpenRouter AI engine abstraction (`ILLMProvider` in `app/services/ai_engine.py`).

### The Challenge
- The existing system is doctor-centric: a patient chooses a doctor or department (Cardiology, ENT, etc.) and books an OPD slot.
- For specialty hospitals (Dermatology/Skin & Hair, Eye/Ophthalmology, Dental, IVF):
  - Patients seek specific **Treatments and Procedures**, or have specific **Symptoms & Aesthetic Concerns**.
  - General department menus are inadequate.
  - Patients need interactive 2-line AI descriptions explaining treatments and matching symptoms.
  - Admins need dedicated treatment catalogs, doctor-to-procedure mappings, and AI description generation tools.
  - Demos and clients require dedicated panel URLs (e.g. `/derma-panel`, `/eye-panel`, `/dental-panel`, `/ivf-panel`).

---

## 2. Key Decisions & Agreements Made in This Session

1. **Four New Core Plan Tiers:**
   - `derma`: Dermatology, Trichology, Cosmetology & Aesthetic Surgery
   - `eye`: Ophthalmology, Cataract, LASIK & Retinal Surgery
   - `dental`: Dental Surgery, Endodontics, Implants & Orthodontics
   - `ivf`: IVF, IUI, ICSI, Embryology & Reproductive Medicine
2. **Dedicated `specialty_treatments` Schema:**
   - Stores procedures with categorization, pricing, duration, pre/post instructions, assigned doctors, and multilingual AI descriptions.
3. **Additive-Only Database Migration (`077_specialty_treatments_and_plans.sql`):**
   - Widens `clinics_plan_check` and `plan_tiers_plan_name_check`.
   - Widens `appointments_booking_type_check` to include `treatment_procedure`.
   - Seeds `plan_tiers` for the 4 new specialties.
   - Absolutely zero modification to existing tenants or existing consultation rows.
4. **AI 2-Line Description Generator:**
   - Dedicated backend service and endpoint `POST /admin/treatments/generate-ai-description`.
   - Uses OpenRouter with prompt engineering tailored to patient comprehension, benefits, and safety.
5. **Adaptive Admin Panels & Dedicated Specialty Routes:**
   - Fast routes `/derma-panel`, `/eye-panel`, `/dental-panel`, `/ivf-panel` with consistent "Clinical Depth" design tokens.
   - Self-adapting `admin/index.html` based on `plan` returned by `GET /admin/me`.
   - Dedicated Treatments & Procedures Catalog UI tab with AI generator modal.
6. **WhatsApp Specialty Conversational Flow:**
   - Specialty-specific welcome and main menu.
   - Categorized interactive lists with Meta-compliant row constraints (24 char title, 72 char desc).
   - Rich 2-line AI description cards sent on treatment selection with direct booking actions.
   - AI symptom-to-treatment matching engine with deterministic keyword fallbacks.


---

## 4. Verification & Phone Number Strategy

A key practical question during expansion is: **Do we need brand new phone numbers to verify the panels and WhatsApp features for each new specialty plan?**

### 1. Panel & Web UI Verification (Zero Phone Numbers Needed)
- Admin Panels (`/admin-panel`, `/derma-panel`, `/eye-panel`, `/dental-panel`, `/ivf-panel`) and the Platform Owner Panel (`/platform-panel`) are pure browser web apps.
- We verify them via browser automation and direct REST API client tests:
  - Admin login & `GET /admin/me` returning the plan (`derma`, `eye`, etc.) and resolved features (`specialty_treatments`).
  - Treatment Catalog CRUD (add, edit, list, delete).
  - OpenRouter "✨ Generate AI Description" modal button triggering the 2-line patient copy.
  - Doctor assignment to procedures.

### 2. WhatsApp Bot Conversational Verification (3 Practical Strategies)
- **Strategy A — Simulated Webhook Test Suite (No Real Phone Required):**
  - Using `TestClient` / `pytest` and interactive runner scripts (like `test_script.py`), we simulate incoming WhatsApp webhooks with mock phone numbers.
  - Verifies 100% of the conversational state machine, interactive list limits (<= 10 rows, <= 24 char titles, <= 72 char descriptions), 2-line AI card formatting, and database booking writes with zero Meta API costs or SIM card dependencies.
- **Strategy B — Single Existing Test/Demo Number Reuse (No Extra SIM Needed!):**
  - If you already have 1 test WhatsApp number connected in Kriya AI (e.g., your development or sandbox number):
  - In `/platform-panel`, a clinic's plan can be toggled in 1 click!
  - You can switch your test clinic to `derma` ➔ message it on WhatsApp to test Dermatology ➔ switch to `eye` ➔ test Eye ➔ switch to `dental` ➔ test Dental ➔ switch to `ivf` ➔ test IVF!
  - **You DO NOT need 4 separate live phone numbers to verify all 4 plans.** A single test number tests all 4.
- **Strategy C — Meta Free Developer Sandbox Number:**
  - Meta provides a free test number in your Meta Developer App (WhatsApp > API Setup).
  - Can be registered as a test clinic (`is_sandbox=True`) with your personal phone whitelisted on Meta to test real phone-to-bot WhatsApp interaction.

---

## 5. Action Items for Execution Phase

- [ ] Run Migration `077_specialty_treatments_and_plans.sql` (additive).
- [ ] Update `app/services/tenant.py` (`PLAN_FEATURES`, `FEATURE_LABELS`, `ALL_FEATURES`).
- [ ] Update `app/services/message_accounting.py` plan tiers fallback.
- [ ] Update `app/routers/platform.py` and `app/routers/clinics.py` plan validators.
- [ ] Implement `app/database.py` treatments CRUD helpers.
- [ ] Implement `app/services/ai_engine.py` treatment description generator and symptom-to-treatment matcher.
- [ ] Implement `app/routers/admin.py` treatments endpoints (`/admin/treatments`).
- [ ] Implement WhatsApp specialty flows in `app/services/conversation.py`.
- [ ] Add dedicated specialty panel routes in `app/main.py` and adapt `admin/index.html` and `admin/platform.html`.
- [ ] Add automated regression test suite covering all 4 specialty plans, feature matrix invariants, AI description generation, and WhatsApp list safety.


