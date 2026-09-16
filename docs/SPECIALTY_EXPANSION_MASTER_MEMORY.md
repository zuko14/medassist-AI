# Kriya AI — Multi-Specialty Hospital WhatsApp OS Expansion
# MASTER ARCHITECTURE & SESSION MEMORY SPECIFICATION

> **Status:** ACTIVE ARCHITECTURAL EXPANSION  
> **Initial Baseline Date:** 2026-09-15  
> **Production Status:** LIVE IN PRODUCTION — ZERO REGRESSION TOLERANCE  
> **Target Verticals:**  
> 1. Dermatology, Cosmetology & Trichology (Skin & Hair Specialty Clinics/Hospitals)  
> 2. Ophthalmology & Vision Care (Eye Specialty Hospitals)  
> 3. Dental Surgery, Implantology & Orthodontics (Dental Clinics & Hospitals)  
> 4. IVF, Fertility & Reproductive Medicine (Fertility Clinics & Hospitals)

---

## 1. Executive Context & Strategic Intent

### 1.1 The Operational Reality
Kriya AI is currently active in 100% genuine healthcare production, serving solo doctors, polyclinics, multi-branch hospitals, and standalone diagnostic laboratories. The current production engine utilizes a doctor-centric and department-centric booking paradigm:
- In general polyclinics and multi-specialty hospitals, patients book appointments by choosing a medical department (e.g. Cardiology, Orthopedics, General Medicine) or selecting a named doctor for OPD consultation slots.
- Symptom mapping routes broad complaints (e.g., "chest pain" ➔ Cardiology, "knee swelling" ➔ Orthopedics).

### 1.2 The Specialty Sector Gap
In competitive, specialized medical sectors (Dermatology/Skin & Hair, Ophthalmology/Eye, Dental Care, and IVF/Fertility):
1. **The Facility Is Already a Single Specialty:** Routing a patient at a Dermatology hospital to "Dermatology" is redundant and poor UX.
2. **Patients Are Treatment & Procedure Driven:** Patients seeking specialty care do not just look for a doctor's name; they seek specific **Treatments & Procedures** (e.g., Root Canal, Clear Aligners, Cataract Surgery, LASIK, Hair PRP, Chemical Peels, IVF cycle, Egg Freezing) or have specific aesthetic/clinical concerns (e.g., acne scars, cloudy vision, tooth sensitivity, fertility challenges).
3. **Information & Education Deficit:** Patients often do not know what procedure addresses their issue, what it entails, what pre-procedure preparation is required, or what recovery looks like.
4. **Differentiation & High Conversion:** Specialty hospitals require their WhatsApp bot to act as a **Treatment Concierge & Clinical Procedure Guide**, providing crisp, patient-friendly AI descriptions, procedure discovery, symptom-to-treatment matching, and direct procedure/consultation booking.

---

## 2. Core Clinical Taxonomy & Specialty Modules

### 2.1 Vertical 1: Dermatology, Cosmetology & Trichology (`derma`)
* **Medical Dermatology:** Acne vulgaris, Psoriasis, Eczema, Vitiligo, Urticaria, Fungal/Bacterial infections, Warts, Skin allergy testing.
* **Aesthetics & Cosmetology:** Chemical Peels (Salicylic, Glycolic, Yellow peel), Laser Hair Reduction (Triple wavelength diode), Carbon Laser Facial, Microneedling Radiofrequency (MNRF) for Acne Scars, Subcision, Mole & Skin Tag Removal, Pigmentation/Melasma therapy, Anti-aging (Botox, Dermal Fillers, Thread lifts).
* **Trichology & Hair Health:** Hair PRP (Platelet-Rich Plasma), GFC (Growth Factor Concentrate) therapy, Mesotherapy, Scalp Trichoscopy analysis, FUE Hair Transplantation counseling.
* **Dermato-Surgery:** Skin Biopsy (Punch/Excision), Sebaceous Cyst Excision, Keloid Injections, Nail Surgery (Ingrown toenail).
* **AI 2-Line Hook Pattern:** e.g., *"✨ Acne Scar Subcision & MNRF: Gently releases tethered scars and triggers fresh collagen production for visibly smoother skin. Performed with local numbing, virtually painless with fast healing."*

### 2.2 Vertical 2: Ophthalmology & Eye Specialty (`eye`)
* **Cataract Services:** Robotic Laser Cataract Surgery, Micro-incision Phacoemulsification, Premium IOLs (Monofocal, Toric for astigmatism, Multifocal/Trifocal).
* **Refractive Surgery (Specs Removal):** Blade-free LASIK, Contoura Vision (topography-guided), SMILE (Small Incision Lenticule Extraction), PRK, ICL (Implantable Collamer Lens for high power).
* **Retina & Vitreo-Retina:** Diabetic Retinopathy screening, Retinal Detachment surgery (Vitrectomy, Scleral Buckling), Anti-VEGF injections (Lucentis, Accentrix, Eylea), Macular degeneration.
* **Cornea & Anterior Segment:** C3R (Corneal Collagen Cross-linking for Keratoconus), Corneal Transplants (PK, DALK, DSAEK), Pterygium excision.
* **Glaucoma Care:** Applanation Tonometry, Visual Field Perimeter analysis, Trabeculectomy, Laser Peripheral Iridotomy.
* **Pediatric & Strabismus (Squint):** Squint correction surgery, Amblyopia (lazy eye) therapy, Vision therapy.
* **Dry Eye & Oculoplasty:** Intense Pulsed Light (IPL) for Meibomian Gland Dysfunction, Blepharoplasty, Ptosis correction.
* **AI 2-Line Hook Pattern:** e.g., *"👁️ Contoura Vision LASIK: Customized blade-free laser vision correction mapped to 22,000 elevation points of your cornea for sharp 6/6 vision without spectacles in a quick 10-minute outpatient procedure."*

### 2.3 Vertical 3: Dental Care, Surgery & Orthodontics (`dental`)
* **Endodontics (Root Canal):** Single-Sitting Rotary RCT, Re-RCT, Post and Core, Painless microscopic canal disinfection.
* **Orthodontics & Aligners:** Clear Aligners (Invisible Braces), Ceramic Braces, Self-ligating Braces, Interceptive Pediatric Orthodontics.
* **Implantology:** Single Tooth Titanium Dental Implant, Immediate Loading Implants, All-on-4 / All-on-6 Full Arch Rehabilitation, Bone Grafting & Sinus Lift.
* **Cosmetic Dentistry:** Professional In-Office Teeth Whitening, Porcelain Veneers & Lumineers, Composite Edge Bonding, Digital Smile Design (DSD).
* **Prosthodontics:** CAD/CAM Zirconia Crowns, Ceramic Bridges, Flexible Dentures, Nightguards for Bruxism.
* **Oral Surgery & Periodontics:** Painless Surgical Extraction of Impacted Wisdom Teeth, Ultrasonic Scaling & Deep Polishing, Flap Surgery, Laser Gingivectomy.
* **Pediatric Dentistry:** Pulpectomy for milk teeth, Fluoride Varnish application, Pit & Fissure Sealants.
* **AI 2-Line Hook Pattern:** e.g., *"🦷 Single-Sitting Rotary RCT: Saves your natural infected tooth in under 45 minutes using painless rotary technology and bio-ceramic filling, eliminating toothache immediately."*

### 2.4 Vertical 4: IVF, Fertility & Reproductive Medicine (`ivf`)
* **Core ART Procedures:** IVF (In Vitro Fertilization), IUI (Intrauterine Insemination), ICSI (Intracytoplasmic Sperm Injection), Natural Cycle IVF.
* **Advanced Embryology:** Blastocyst Culture (Day 5 embryo growth), Assisted Hatching, Time-Lapse Embryo Monitoring, Preimplantation Genetic Testing (PGT-A / PGT-M).
* **Fertility Preservation:** Egg Freezing (Oocyte Vitrification), Embryo Cryopreservation, Sperm Banking.
* **Reproductive Surgeries:** 3D Laparoscopy for Endometriosis & Fibroids, Diagnostic & Operative Hysteroscopy (Septum resection, Polyps).
* **Male Infertility & Andrology:** Advanced Computer-Assisted Semen Analysis (CASA), Micro-TESE / TESA (surgical sperm retrieval).
* **AI 2-Line Hook Pattern:** e.g., *"🌱 ICSI (Intracytoplasmic Sperm Injection): Advanced micro-fertilization technique where a single handpicked healthy sperm is delicately injected into the egg, achieving high pregnancy success even with severe male factor challenges."*

---

## 3. System Architecture & Zero-Regression Guardrails

### 3.1 Tenancy & Plan Expansion
1. **New Plan Slugs:**
   - `derma` — Dermatology, Trichology & Aesthetic Surgery Hospital/Clinic Plan
   - `eye` — Ophthalmology & Vision Care Specialty Hospital Plan
   - `dental` — Dental Hospital & Multi-Chair Clinic Plan
   - `ivf` — Fertility, IVF & Reproductive Medicine Center Plan
   - `multispecialty` — General hospital that ALSO runs the treatments
     catalogue (**added in Session 07, migration 078**). Deliberately NOT in
     `SPECIALTY_BY_PLAN`: that map decides whether the patient menu drops its
     departments row, and a hospital with fifteen departments must keep it.
     It lives in `HYBRID_SPECIALTY_PLANS` instead. Feature set is
     `polyclinic` ∪ `{specialty_treatments}` — every feature, enumerated
     rather than wildcarded.
2. **Zero Disturbance to Existing Plans:**
   - Existing plans (`soloclinic`, `diagstream`, `diagbooking`, `essential`, `polyclinic`, `enterprise`) remain 100% unchanged.
   - CHECK constraints on `clinics(plan)` and `plan_tiers(plan_name)` are widened additively via dynamic constraint inspection (following the proven migration 016/072 pattern).
   - All existing feature gates (`has_feature()`, `require_feature()`) and billing matrices maintain strict backward compatibility.

### 3.2 Specialty Treatments Catalog Model (`specialty_treatments`)
A new dedicated table isolated by `clinic_id` stores procedures/treatments:
- Columns: `id`, `clinic_id`, `branch_id`, `specialty_type`, `category`, `name`, `short_name` (<=24 chars for WhatsApp list titles), `ai_description` (<=150 chars for WhatsApp), `ai_description_te`, `ai_description_hi`, `duration_minutes`, `price_paise`, `prep_instructions`, `post_instructions`, `doctor_ids` (UUID array), `is_active`, `display_order`.
- RLS enabled with `service_role` full access and tenant-scoped security.

### 3.3 Slot Protection & Anti-Double Booking Invariant
> ⚠️ **CORRECTED IN SESSION 02. The original text here was wrong. Do not widen `booking_type`.**

- Migration 064 created `uq_appointment_active_slot` (and `_unassigned`) with the predicate
  `WHERE status IN (...) AND booking_type = 'consultation' AND doctor_id IS NOT NULL`.
  **A row with any other booking_type is NOT covered by the index.**
- Migration 039 constraint `appointments_time_required_for_consultation` only accepts
  `booking_type = 'consultation'` (with time) or `'lab_test'`. A `'treatment_procedure'` row fails it, so every insert would be rejected.
- Code that only runs for `booking_type == 'consultation'`: doctor_id-required guard and slot pre-check (`app/database.py:1039,1064`), reminders (`app/services/scheduler.py:513,574`), department analytics (`app/services/analytics.py:187`).
- **Decision:** treatment bookings stay `booking_type = 'consultation'` and carry two new nullable columns, `treatment_id` and `treatment_name`. The index, the guards, reminders, payments and refunds keep working unchanged.

### 3.4 AI Treatment Description Generator Engine
- Endpoint: `POST /admin/treatments/generate-ai-description`
- Takes treatment name, category, specialty type, and clinic context.
- Invokes OpenRouter LLM (`ILLMProvider`) with strict clinical safety prompt to generate a 2-line patient-friendly benefit-oriented description in English, Hindi, and Telugu.
- Fallback keyword/template descriptions ensure 100% uptime even if LLM is unreachable.

### 3.5 Unified Admin Panel & Dedicated Specialty URL Routing
- **Consistent Design Language:** Shares the exact "Clinical Depth" design tokens (`--bg: #040A11`, `--surface: rgba(28,45,63,0.72)`, `--brand-grad`, `--mint: #C7EAE1`).
- **Dedicated Friendly URLs for Demos & Portals:**
  - `/derma-panel` ➔ Dermatology & Aesthetic Clinic OS
  - `/eye-panel` ➔ Eye & Ophthalmology Hospital OS
  - `/dental-panel` ➔ Dental Hospital & Clinic OS
  - `/ivf-panel` ➔ IVF & Fertility Center OS
  - `/hospital-panel` ➔ Multi-Specialty Hospital OS (Session 07)
  - Standard `/admin-panel` continues to work and auto-adapts based on the clinic's plan returned by `GET /admin/me`.
- **Specialized UI Features:**
  - Treatments & Procedures Catalog management
  - AI Description Auto-Generator Modal
  - Treatment-to-Doctor linking
  - Procedure-aware Appointments management
  - Specialized analytics (Top Procedures, Procedure vs Consultation ratio, Procedure Revenue)

### 3.6 WhatsApp Conversational Engine Flow
- **Intent Detection & Routing:**
  - When patient messages a specialty tenant:
    1. Greeting highlights the clinic's specialty identity.
    2. Menu presents: **Explore Treatments**, **Book Procedure / Doctor**, **Symptom / Problem Guide**, **Pre/Post Care Guidance**, **Timings & Location**.
    3. Interactive WhatsApp lists (respecting Meta's 10-row, 24-char title, 72-char description limits) display categorized treatments with pagination.
    4. Selecting a treatment outputs the rich 2-line AI description + estimated duration/pricing + option to book with an expert specialist.
    5. Patient describing a symptom (e.g. "severe toothache when biting" or "cloudy eyesight") triggers specialty symptom-to-treatment matching, suggesting the exact clinical procedure to consult for.

---

## 4. Session Log Index

| Session | Date | Objective | Lead Artifacts |
| :--- | :--- | :--- | :--- |
| **Session 01** | 2026-09-15 | Forensic Project Analysis & Complete Multi-Specialty Architecture Expansion Plan | `docs/SPECIALTY_EXPANSION_MASTER_MEMORY.md`, `docs/sessions/SESSION_01_SPECIALTY_EXPANSION_DESIGN.md`, `implementation_plan.md` |
| **Session 02** | 2026-09-15 | Code-verified review of the Session 01 plan: 3 blockers, 6 design corrections, and the revised build order. **Read this before implementing.** | `docs/sessions/SESSION_02_PLAN_FORENSIC_REVIEW.md` |
| **Session 03** | 2026-09-16 | Step-by-step production implementation plan for Antigravity / any agent (12 tasks, tests, deploy runbook, acceptance) | `docs/specialty_plan/README.md`, `docs/sessions/SESSION_03_IMPLEMENTATION_PLAN.md` |
| **Session 04** | 2026-09-16 | Fixed the "Patient" booking name bug (all plans) and the account-holder rename; plan anchors updated | `docs/sessions/SESSION_04_BOOKING_NAME_FIX.md` |
| **Session 05** | 2026-09-16 | Complete execution of Tasks 1-9 (Migration 077, Registry, Catalog, AI drafts, Admin API, UI, WhatsApp flow, Wiring, Bookings/Payments/Analytics, Targeted Regression) | `docs/sessions/SESSION_05_SPECIALTY_EXPANSION_EXECUTION.md` |
| **Session 06** | 2026-09-16 | Admin panel form controls: root-caused checkbox stretching (`.field input` sized checkboxes as text inputs), styled the 6 unstyled textareas, added per-language AI draft buttons for Hindi/Telugu | `docs/sessions/SESSION_06_ADMIN_PANEL_FORM_CONTROLS.md` |
| **Session 07** | 2026-09-16 | The `multispecialty` plan: a general hospital that also runs the treatments catalogue (migration 078, `/hospital-panel`). No WhatsApp flow code and no schema change were needed — see the doc for why. | `docs/specialty_plan/11-multispecialty-plan.md`, `docs/sessions/SESSION_07_MULTISPECIALTY_PLAN.md` |

> **Rule for every future session:** add `docs/sessions/SESSION_NN_<topic>.md` covering intent, decisions, files changed, tests run with results, and open items. Then add a row here. If a session proves an earlier statement in this file wrong, correct it in place and mark it `CORRECTED IN SESSION NN`.
