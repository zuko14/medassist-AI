# Kriya AI — Marketing Claims & Truth Verification Register

**Document Version:** 1.0.0  
**Date:** August 2026  
**Publisher:** Xylarc AI Legal, Compliance & Product Verification  
**Target:** Sales Reps, Copywriters, Marketers, Executive Spokespersons  

---

## 1. Compliance Standard & The Truth-in-Marketing Protocol

In healthcare technology, misleading, unverified, or exaggerated marketing claims violate medical advertising ethics, destroy doctor trust, and create severe regulatory liabilities under the **National Medical Commission (NMC)** and the **Consumer Protection Act**.

Every claim made in Kriya AI sales decks, website pages, social media posts, and videos must be categorized under this register:

```
+----------------------------------------------------------------------------------------------------+
|                                    CLAIM CLASSIFICATION TAXONOMY                                   |
+----------------------------------------------------------------------------------------------------+
|  [CATEGORY A] VERIFIED INTERNAL EVIDENCE  | Backed by tested codebase, SQL schema & test suites.   |
|  [CATEGORY B] EXTERNAL SOURCE-BACKED      | Backed by published industry, government or API data.  |
|  [CATEGORY C] MODELLED / ESTIMATED        | Transparent financial models with stated assumptions.  |
|  [CATEGORY D] STRICTLY PROHIBITED         | Unsubstantiated, fabricated, or illegal medical claims.|
+----------------------------------------------------------------------------------------------------+
```

---

## 2. Category A: Verified Internal Evidence (Codebase & Test Backed)

| Marketing Claim | Verification Evidence in Repository | Status | Safe Sales Language |
| :--- | :--- | :---: | :--- |
| **"Trilingual WhatsApp intent understanding in English, Hindi, and Telugu."** | Implemented in `app/services/ai_engine.py` and `app/services/conversation.py` with localized fallback dictionaries. | **VERIFIED** | *"Supports seamless patient conversations in English, Hindi, and Telugu."* |
| **"Deterministic Zero-LLM Clinical Safety Firewall blocks prescription advice."** | Implemented in `app/services/clinical_firewall.py` with 100+ regex drug patterns; LLM is never called when triggered. | **VERIFIED** | *"Implements an in-memory clinical safety firewall that blocks medical advice and drug requests."* |
| **"Database-level anti-double-booking protection."** | Verified by PostgreSQL partial unique indexes (`idx_unique_active_slot` in `migrations/008_payments.sql`) & distributed locks. | **VERIFIED** | *"Engineered with database concurrency guards that prevent double-booking collisions."* |
| **"Automated 24-hour and 2-hour WhatsApp appointment reminders."** | Implemented via APScheduler jobs in `app/services/scheduler.py`. | **VERIFIED** | *"Sends automated 24-hour and 2-hour WhatsApp notifications to scheduled patients."* |
| **"Live waiting room queue token tracking on WhatsApp."** | Implemented in `app/database.py` (`check_in_appointment`) & `app/services/conversation.py` (`handle_queue_status_inquiry`). | **VERIFIED** | *"Enables patients to check their live queue token status directly over WhatsApp."* |
| **"Multi-branch doctor scheduling with morning and evening shift support."** | Schema defined in `migrations/010_branches.sql` & `migrations/029_doctor_branch_assignment.sql`. | **VERIFIED** | *"Supports multi-branch clinic networks with dynamic morning and evening doctor shifts."* |
| **"Automated lab report OCR extraction and WhatsApp delivery."** | Implemented in `app/services/lab_reports.py` using PDF text extraction, Tesseract OCR, and Supabase Storage. | **VERIFIED** | *"Extracts text from lab reports and delivers encrypted PDFs to patients via WhatsApp."* |
| **"438 automated test cases verified across platform suites."** | Verified via Pytest suite execution (`438 PASSED`, `1 SKIPPED`, `0 FAILED`). | **VERIFIED** | *"Backed by 438 automated test suites verifying security, payments, and scheduling."* |
| **"Multi-tenant PostgreSQL architecture with Row-Level Security (RLS)."** | Verified in `migrations/003_multi_tenant.sql`, `028_core_tables_rls.sql`, and `049_force_row_level_security.sql`. | **VERIFIED** | *"Enforces strict tenant data isolation at the PostgreSQL Row-Level Security layer."* |

---

## 3. Category B: External Source-Backed Claims

| Marketing Claim | External Source & Citation | Status | Safe Sales Language |
| :--- | :--- | :---: | :--- |
| **"Over 500 Million Indians actively use WhatsApp as their primary communication tool."** | Meta Industry Reports / Digital India Telecommunication Statistics (2025–2026). | **SOURCE-BACKED** | *"Leverages WhatsApp, India's most ubiquitous digital platform with 500M+ active users."* |
| **"Average OPD patient no-show rate in Indian private practices is between 20% and 35%."** | Healthcare Practice Management Surveys / National Health Studies (2024–2026). | **SOURCE-BACKED** | *"Industry benchmarks indicate Indian clinics suffer 20% to 35% no-shows on telephonic bookings."* |
| **"DPDP Act 2023 mandates explicit data consent and right to erasure for personal data."** | Ministry of Electronics and Information Technology (MeitY) — DPDP Act 2023 Provisions. | **SOURCE-BACKED** | *"Designed in strict alignment with India's DPDP Act 2023 data consent requirements."* |
| **"NMC guidelines mandate 7-year retention of medical consultation records."** | National Medical Commission (NMC) Regulations on Maintenance of Medical Records. | **SOURCE-BACKED** | *"Reconciles transient chat privacy with NMC-mandated 7-year medical record retention."* |

---

## 4. Category C: Modelled / Estimated Financial Claims

When presenting ROI or cost savings, always clearly label them as **Modelled Business Estimates with Stated Assumptions**:

```
+----------------------------------------------------------------------------------------------------+
|                                    MODELLED ESTIMATE GUIDELINES                                    |
+----------------------------------------------------------------------------------------------------+
```

* **Claim:** *"Can save 60 to 120 staff hours per month."*
  * **Status:** **MODELLED ESTIMATE**
  * **Stated Assumptions:** Based on a clinic handling 40–80 daily inquiry calls at an average of 3 minutes per call = 2 to 4 hours saved daily × 30 days = 60 to 120 hours/month.
  * **Safe Language:** *"In a typical 40-patient/day clinic, automating routine booking calls can free up an estimated 60 to 120 front-desk hours monthly."*
* **Claim:** *"Can recover ₹45,000+ in lost monthly revenue."*
  * **Status:** **MODELLED ESTIMATE**
  * **Stated Assumptions:** Based on recovering 3 no-show appointments per day across a 3-doctor clinic at ₹500/consultation = 90 recovered consultations/month × ₹500 = ₹45,000.
  * **Safe Language:** *"For a multi-doctor clinic, reducing no-shows by just 3 patients daily can recover over ₹45,000 in monthly consultation fees."*

---

## 5. Category D: Strictly Prohibited Claims (Must Never Be Made)

```
+----------------------------------------------------------------------------------------------------+
|                                  STRICTLY PROHIBITED MARKETING CLAIMS                              |
+----------------------------------------------------------------------------------------------------+
```

1. ❌ **NEVER Claim Medical Diagnoses:**  
   * *Prohibited:* "Kriya AI diagnoses diseases and tells patients what illness they have."  
   * *Why:* Illegal under NMC regulations; Kriya AI only performs administrative symptom triage and slot routing.
2. ❌ **NEVER Claim "100% Uptime" or "Zero Glitches":**  
   * *Prohibited:* "Our WhatsApp bot is 100% fail-safe and never has outages."  
   * *Why:* Meta Cloud API and telecom networks experience occasional transit latency. Always state: *"Engineered for 99.9% high availability with resilient offline fallbacks."*
3. ❌ **NEVER Claim Fabricated Customer Numbers or Endorsements:**  
   * *Prohibited:* "Used by 10,000+ hospitals across India" (unless independently audited and true).  
   * *Why:* Fraudulent trade practice. Speak accurately about active pilot deployments and certified capabilities.
4. ❌ **NEVER Make Defamatory Competitor Claims:**  
   * *Prohibited:* "Competitor X has broken security and leaks patient data."  
   * *Why:* Unverifiable and legally defamatory. Focus strictly on factual architectural differences (e.g., *"Unlike marketplace aggregators, Kriya AI keeps your patients exclusively on your own clinic's dedicated WhatsApp number."*).
