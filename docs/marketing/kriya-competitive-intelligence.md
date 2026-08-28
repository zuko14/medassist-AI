# Kriya AI — Competitive Intelligence & Market Differentiation Matrix

**Document Version:** 1.0.0  
**Date:** August 2026  
**Analyst:** Xylarc AI Competitive Intelligence Team  
**Scope:** Direct & Indirect Competitors in the Indian Healthcare SaaS & Patient Communication Market  

---

## 1. Competitive Landscape Overview

The Indian healthcare technology space comprises four distinct categories of solutions:

```
+----------------------------------------------------------------------------------------------------+
|                               COMPETITOR CATEGORY TAXONOMY                                         |
+----------------------------------------------------------------------------------------------------+
|  1. Patient Discovery Marketplaces & EMR Suites                                                    |
|     - Examples: Practo / Practo Ray, Pristyn Care.                                                 |
|     - Model: Marketplace-first, charges per lead/booking or monthly listing + basic practice tools.|
+----------------------------------------------------------------------------------------------------+
|  2. Doctor-Centric EMR & Digital Prescription Software                                            |
|     - Examples: Eka Care, HealthPlix.                                                              |
|     - Model: Free or low-cost EMR for doctors to write prescriptions; monetization via add-ons,    |
|       pharma analytics, or outbound communication credits.                                         |
+----------------------------------------------------------------------------------------------------+
|  3. Cloud Hospital / Clinic Management Information Systems (HMIS/LIMS)                             |
|     - Examples: MocDoc, DocPulse, Clinicea / Clinicia.                                             |
|     - Model: Comprehensive back-office software covering IPD, OPD, billing, pharmacy, and LIMS.    |
|       WhatsApp features are typically one-way outbound notification SMS/WhatsApp blasts.          |
+----------------------------------------------------------------------------------------------------+
|  4. Generic Enterprise WhatsApp Chatbot Platforms                                                  |
|     - Examples: Yellow.ai, LimeChat, Gupshup, Wati, Interakt.                                      |
|     - Model: Generic conversational AI; requires extensive custom engineering, lacks native        |
|       healthcare database models (slots, doctors, leaves, DPDP/NMC retention, FHIR/ABDM).          |
+----------------------------------------------------------------------------------------------------+
```

---

## 2. In-Depth Competitor Profiles

### 2.1 Practo / Practo Ray
* **Primary Focus:** Consumer healthcare discovery marketplace and basic clinic management software (Ray).
* **Business Model:** Subscription fees (₹999–₹1,499/mo) plus marketplace lead fees, "Pro" listing marketing charges, and commission on marketplace-directed appointments.
* **Key Strengths:** Massive brand recognition among consumers in Tier 1 Indian cities; large directory of active doctors.
* **Verified Limitations & Clinic Friction:**
  * **Brand Hijacking:** Patients booking through Practo are in Practo's ecosystem, not the clinic's own brand. Competitor clinics are often recommended on the same search page.
  * **WhatsApp Experience:** Primarily one-way notification reminders; does not provide an autonomous, two-way conversational booking bot natively on the clinic’s dedicated WhatsApp Business number without marketplace lock-in.
  * **Pricing Predictability:** Clinics experience variable costs based on marketplace volume.

### 2.2 Eka Care
* **Primary Focus:** Doctor-centric EMR, AI medical scribing (EkaScribe), and Ayushman Bharat Digital Mission (ABDM) pioneer.
* **Business Model:** Freemium / Tiered annual plans (Free, ₹2,999 Booster, ₹5,999 Pro, ₹9,999 Premium, up to ₹27,999+).
* **Key Strengths:** Outstanding clinical scribing speed, mobile-first EMR for doctors, first-mover advantage in ABDM M1/M2/M3 compliance and ABHA health data exchange.
* **Verified Limitations & Clinic Friction:**
  * **Doctor Consultation Focus vs. Hospital Front-Desk Operations:** Highly optimized for what happens *inside* the consultation room (prescriptions, vitals, medical history); less focused on autonomous multi-branch front-desk queue management, deposit collection, and legacy EMR web scraping.
  * **WhatsApp Booking Depth:** Uses WhatsApp predominantly for prescription sharing and outbound reminder broadcasts; limited deep multi-state conversational slot locking with deterministic clinical safety firewalls.

### 2.3 MocDoc (Hospital & Lab HMS)
* **Primary Focus:** Comprehensive cloud-based Hospital Information Management System (HIMS) and Laboratory Information Management System (LIMS).
* **Business Model:** Enterprise custom quote based on bed count, module requirements, and doctor user seats.
* **Key Strengths:** Deep clinical breadth: Inpatient (IPD), Outpatient (OPD), Pharmacy inventory, Barcode LIMS, NABH/ISO compliance, multi-branch administration.
* **Verified Limitations & Clinic Friction:**
  * **Front-Office Automation:** Traditional form-based software; front-desk receptionists must manually manage appointment schedules on desktop browsers.
  * **WhatsApp Capabilities:** Outbound alert engine (dispatching appointment confirmations or PDF links); lacks a conversational AI agent capable of negotiating slot availability, answering hospital FAQs, or managing trilingual patient inquiries dynamically.
  * **Kriya Relationship:** MocDoc is **not** a direct enemy; Kriya AI features a dedicated headless browser connector (`connectors/mocdoc/worker.py`) that syncs with MocDoc to provide the missing modern WhatsApp front-end!

### 2.4 HealthPlix
* **Primary Focus:** Artificial Intelligence-powered EMR for doctors, assisting in fast digital prescription generation in 14+ Indian languages.
* **Business Model:** Annual subscription per doctor (Pro: ₹11,999/yr, Elite: ₹17,999/yr).
* **Key Strengths:** Fast prescription generation, massive doctor network, drug-to-drug interaction alerts at point of prescription.
* **Verified Limitations & Clinic Friction:**
  * **Scope:** Tailored exclusively to the doctor at their consultation desk. It does not provide autonomous patient-facing WhatsApp slot negotiation, UPI payment collection, or live waiting room queue tracking.

### 2.5 Generic Enterprise WhatsApp Bots (Yellow.ai / Gupshup / Wati)
* **Primary Focus:** Horizontal customer support chatbots for e-commerce, banking, and general enterprises.
* **Business Model:** High platform subscription (₹15,000–₹1,00,000+/mo) + per-conversation/per-message charges + hefty custom integration setup fees (₹1 Lakh–₹5 Lakhs).
* **Key Strengths:** Powerful no-code flow builders, high scalability.
* **Verified Limitations & Clinic Friction:**
  * **No Native Healthcare Domain Logic:** Lacks pre-built doctor shift/leave engines, anti-collision slot locking, emergency triage firewalls, DPDP 7-year retention logic, and FHIR/ABDM compatibility.
  * **Extremely Costly & Fragile for Healthcare:** Clinics must build and maintain custom APIs and middleware connecting the bot to their database, which breaks whenever schedules change.

---

## 3. Comprehensive Competitor Feature Matrix

| Functional Capability | Kriya AI | Practo Ray | Eka Care | MocDoc | HealthPlix | Generic Bot (Wati/Yellow) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Two-Way Conversational WhatsApp Booking** | **YES** | PARTIAL | PARTIAL | NO | NO | PARTIAL (Needs Dev) |
| **Deterministic Zero-LLM Clinical Firewall** | **YES** | NO | NO | NO | NO | NO |
| **Atomic Anti-Double-Booking Slot Guard** | **YES** | YES | YES | YES | YES | NO (Needs Custom DB) |
| **Trilingual Vernacular (EN / HI / TE)** | **YES** | PARTIAL | PARTIAL | PARTIAL | YES | PARTIAL |
| **UPI / Razorpay Consultation Fee Gating** | **YES** | PARTIAL | PARTIAL | YES | NO | PARTIAL (Add-on) |
| **Live Waiting Room Queue & Token Tracking** | **YES** | NO | PARTIAL | YES (Internal) | NO | NO |
| **Lab Report OCR & AI Plain-Language Summary**| **YES** | NO | NO | PARTIAL (PDF only)| NO | NO |
| **Automated 24h & 2h WhatsApp Reminders** | **YES** | YES (SMS/WA)| YES | YES | YES | YES |
| **Doctor Leave Auto-Cancellation & Reschedule**| **YES** | PARTIAL | PARTIAL | YES (Internal) | NO | NO |
| **Daily Prescription Adherence WhatsApp Reminders**| **YES** | NO | NO | NO | NO | NO |
| **Multi-Branch Doctor Routing & Timings** | **YES** | YES | PARTIAL | YES | NO | NO (Needs Dev) |
| **Family Member / Dependent Profiling** | **YES** | PARTIAL | YES | YES | NO | NO |
| **Non-Invasive Legacy HMIS Browser Sync** | **YES** | NO | NO | N/A (Is HMIS) | NO | NO |
| **HL7 FHIR R4 REST API Standard** | **YES** | UNKNOWN | YES | PARTIAL | PARTIAL | NO |
| **DPDP 2023 Consent & 7-Year NMC Retention** | **YES** | PARTIAL | YES | PARTIAL | PARTIAL | NO |
| **Direct Clinic-Owned Branding (No Marketplace)**| **YES** | NO | YES | YES | YES | YES |

---

## 4. The Core Competitive Moat of Kriya AI

Kriya AI does not compete by attempting to replace 500-table hospital back-office ERPs, nor does it compete as a consumer patient aggregator marketplace. Kriya AI occupies the **uncontested high-value operational layer**:

```
+----------------------------------------------------------------------------------------------------+
|                                    KRIYA AI STRATEGIC MOAT                                         |
+----------------------------------------------------------------------------------------------------+
|                                                                                                    |
|   [ PATIENT WORLD ]  ========> [ KRIYA AI LAYER ]  ========> [ CLINICAL & BACK-OFFICE WORLD ]     |
|   - WhatsApp Natural Text      - Trilingual Intent Triage    - Doctor Shift & Leave Engine         |
|   - Zero App Download          - Zero-LLM Clinical Firewall  - Razorpay Instant UPI Settlement     |
|   - Instant Live Queue Status  - Distributed Slot Locks      - Playwright Browser / FHIR Connectors|
|   - AI-Summarized Reports      - DPDP Consent Lifecycle      - Clinic Staff Admin Dashboard        |
|                                                                                                    |
+----------------------------------------------------------------------------------------------------+
```

### The 4 Pillars of Defensibility:
1. **Clinical Safety & NMC Liability Shield:** Unlike generic chatbots that pass patient queries directly to an LLM (risking hallucinated dosages and medical lawsuits), Kriya AI uses an in-memory deterministic regex firewall (`app/services/clinical_firewall.py`) that strictly intercepts 100+ drug names and medical advice requests before any LLM inference, safely redirecting to appointment booking.
2. **Anti-Collision Transactional Integrity:** Engineered with PostgreSQL partial unique indexes (`idx_unique_active_slot`, `idx_unique_queue_token`) and distributed locks. It is physically impossible for two patients on WhatsApp to book the same doctor slot simultaneously or receive duplicate queue tokens.
3. **Non-Invasive Integration via Browser Connectors:** Kriya AI solves the "Legacy HMIS Problem" through autonomous Playwright connectors (`connectors/runner.py`), allowing clinics to connect their existing legacy software (e.g., MocDoc, CallMedex) without paying lakhs for custom API development.
4. **Clinic Brand Ownership & Flat SaaS Economics:** Unlike marketplaces that monetize by redirecting patients or charging per-lead commissions, Kriya AI operates on the clinic's dedicated WhatsApp Business number, reinforcing the hospital's brand loyalty with 100% predictable SaaS subscription tiers.
