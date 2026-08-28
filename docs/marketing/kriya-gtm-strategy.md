# Kriya AI — 12-Month Go-To-Market (GTM) Strategy & Commercial Architecture

**Document Version:** 1.0.0  
**Date:** August 2026  
**Publisher:** Xylarc AI Commercial Strategy Group  
**Target:** Executive Management, Regional Sales Directors, GTM Leads  

---

## 1. Customer Segmentation & ICP Scorecard

Not all healthcare facilities are equally suitable initial prospects. To maximize conversion velocity and minimize sales cycle friction, target segments are evaluated across 7 operational criteria:

```
+----------------------------------------------------------------------------------------------------+
|                                    CUSTOMER SEGMENT SCORECARD                                      |
+----------------------------------------------------------------------------------------------------+
```

| Healthcare Segment | Pain Intensity (1-10) | Willingness to Pay (1-10) | Ease of Sale & Onboarding (1-10) | Kriya AI Fit (1-10) | Competitive Pressure | Priority Tier |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Multi-Doctor Polyclinics (3–10 Doctors)** | 9/10 | 8/10 | 9/10 | 10/10 | Medium | **TIER 1 (Immediate Beachhead)** |
| **Standalone Diagnostic / Pathology Labs** | 9/10 | 8/10 | 8/10 | 9/10 | Low | **TIER 1 (Immediate Beachhead)** |
| **Busy Single-Doctor Specialist Clinics** | 8/10 | 7/10 | 9/10 | 9/10 | High | **TIER 1 (Fast Sales Velocity)** |
| **Medium Private Hospitals (20–100 Beds)**| 8/10 | 8/10 | 6/10 | 9/10 | Medium | **TIER 2 (High LTV Growth)** |
| **Multi-Branch Polyclinic Networks** | 9/10 | 9/10 | 6/10 | 9/10 | Low | **TIER 2 (High LTV Growth)** |
| **Large Tertiary Hospital Chains (200+ Beds)**| 7/10 | 9/10 | 3/10 | 7/10 | High (Legacy EMR Lock-in) | **TIER 3 (Enterprise Phase 3)** |
| **Rural Primary Health Clinics** | 5/10 | 2/10 | 4/10 | 5/10 | Low | **TIER 3 (Non-Target Currently)**|

### Tier 1 Primary Beachhead Rationale:
* **Multi-Doctor Polyclinics & Busy Specialist Clinics (Dental, Ortho, Gynaec, Derm, Paediatrics):** High daily patient volume (50–200 patients/day), intense front-desk phone bottleneck, high no-show financial impact, single decision-maker (Managing Doctor / Owner), and fast sales cycle (7–14 days).
* **Diagnostic Centers:** Immediate operational pain around delivering 50–300 daily lab reports via WhatsApp; Kriya AI's automated OCR and PDF dispatch solves their single biggest recurring manual labor expense.

---

## 2. Product Packaging & Commercial Pricing Strategy

Based on Indian healthcare SaaS purchasing patterns and Meta Cloud API cost structures, Kriya AI operates on a **predictable subscription model** with zero per-lead marketplace commissions:

```
+----------------------------------------------------------------------------------------------------+
|                                KRIYA AI PRODUCT TIERS & PRICING                                    |
+----------------------------------------------------------------------------------------------------+
```

### Plan 1: Kriya Clinic Starter
* **Target:** Single-doctor clinics and standalone specialist practices (Dental, Dermatology, ENT, Pediatrics).
* **Price:** **₹2,499 / month** (billed annually at ₹24,999/yr) or ₹2,999 month-to-month.
* **Included Features:**
  * Dedicated Clinic WhatsApp Business integration.
  * 1 Doctor profile, up to 1,000 conversational appointment sessions/month.
  * Trilingual NLP intent classification (English, Hindi, Telugu) with Clinical Safety Firewall.
  * Automated 24-hour and 2-hour WhatsApp appointment reminders.
  * Optional Razorpay UPI consultation fee collection.
  * Clinic Staff Web Admin Dashboard.
  * DPDP 2023 consent tracking & right-to-erasure support.

### Plan 2: Kriya Polyclinic & Growth (Flagship)
* **Target:** Multi-doctor clinics (up to 6 doctors) and 2-branch polyclinics.
* **Price:** **₹5,999 / month** (billed annually at ₹59,999/yr) or ₹6,999 month-to-month.
* **Included Features:**
  * Everything in Starter plus:
  * Up to 6 Doctor profiles and multi-branch schedule routing.
  * Up to 4,000 conversational appointment sessions/month.
  * Real-time Waiting Room Queue Token Management & Patient Live WhatsApp Queue Tracking.
  * Family member / dependent appointment booking.
  * Automated Doctor Leave Rescheduling Engine.
  * Post-visit WhatsApp patient feedback and rating collection.
  * Standard HL7 FHIR R4 REST API access.

### Plan 3: Kriya Diagnostic & Lab Pro
* **Target:** Pathology laboratories and standalone diagnostic testing centers.
* **Price:** **₹6,999 / month** (billed annually at ₹69,999/yr) or ₹7,999 month-to-month.
* **Included Features:**
  * Automated Lab Report Ingestion Pipeline (PDF text extraction & OCR).
  * AI-powered patient plain-language report summarizer with clinical disclaimers.
  * Automatic matching with patient phone and test booking reference.
  * Secure Supabase Storage hosting and instant WhatsApp PDF delivery receipts.
  * Up to 3,000 delivered lab reports per month.

### Plan 4: Kriya Hospital Enterprise
* **Target:** Hospitals (20–150 beds), multi-specialty hospital chains, and large lab networks.
* **Price:** **Custom Enterprise Contract (Starting at ₹14,999 – ₹39,999 / month / facility)**.
* **Included Features:**
  * Unlimited doctors, departments, and multi-branch administrative governance.
  * Dedicated Playwright EMR/HMIS browser connectors (MocDoc, CallMedex, custom DB sync).
  * High-volume WhatsApp message throughput with custom dedicated Meta WABA.
  * Full ABDM M1/M2/M3 milestones and ABHA ID verification integration.
  * Custom SLA (99.9% uptime), dedicated account engineer, and on-site staff training.

---

## 3. 12-Month Phased GTM Execution Roadmap

```
+----------------------------------------------------------------------------------------------------+
|                                    12-MONTH GTM ROADMAP PHASES                                     |
+----------------------------------------------------------------------------------------------------+
|  MONTH 1–2: Foundation & Pilot Tooling                                                             |
|  - Deploy Production Demo Sandboxes with synthetic data for clinics and diagnostic labs.           |
|  - Finalize sales collateral, one-pagers, and interactive WhatsApp click-to-chat demo links.       |
|  - Target: Onboard 5 lighthouse pilot clinics across Hyderabad and Visakhapatnam.                 |
+----------------------------------------------------------------------------------------------------+
|  MONTH 3–4: Controlled Pilot Validation & Conversion                                               |
|  - Execute 30-Day Guided Pilots for the initial 5 clinics and 3 diagnostic centers.                |
|  - Collect baseline operational metrics: front-desk call reduction, no-show drop, report speed.    |
|  - Target: Convert 80% (6+ accounts) to paid annual contracts; reach ₹50k MRR.                    |
+----------------------------------------------------------------------------------------------------+
|  MONTH 5–6: Case Study Flywheel & Regional Channel Expansion                                       |
|  - Publish 3 verified before-and-after case studies with quantified ROI.                           |
|  - Initiate local medical association (IMA branch) outreach and local HMIS distributor partnerships.|
|  - Target: 25 active paid clinics across Andhra Pradesh & Telangana; reach ₹1.5 Lakhs MRR.         |
+----------------------------------------------------------------------------------------------------+
|  MONTH 7–9: Multi-City Expansion (Bengaluru, Chennai, Pune)                                        |
|  - Expand direct sales and digital outbound targeting polyclinics in Bengaluru & Chennai.          |
|  - Introduce specialized diagnostic center campaign with automated OCR report delivery hooks.     |
|  - Target: 60 active paid clinics/labs; reach ₹4 Lakhs MRR.                                        |
+----------------------------------------------------------------------------------------------------+
|  MONTH 10–12: Hospital Enterprise & National Scale                                                 |
|  - Target 20–100 bed private hospital chains requiring multi-branch live queue and HMIS sync.     |
|  - Launch partner program for local healthcare IT service providers.                              |
|  - Target: 120+ active clinics/hospitals; reach ₹10 Lakhs MRR (₹1.2 Cr ARR run-rate).             |
+----------------------------------------------------------------------------------------------------+
```

---

## 4. Multi-Channel Lead Generation Strategy

```
+----------------------------------------------------------------------------------------------------+
|                                LEAD GENERATION CHANNEL MATRIX                                      |
+----------------------------------------------------------------------------------------------------+
```

| Channel | Target Audience | Tactical Execution | Expected Lead Quality | Conversion Rate |
| :--- | :--- | :--- | :---: | :---: |
| **1. Direct Offline Field Sales ("Drop-In Demos")** | Polyclinic Owners & Managing Doctors | Sales reps visit clinics with 3-minute interactive WhatsApp live demo on tablets; leave branded one-pager. | **HIGH** | 20% – 30% to Pilot |
| **2. Diagnostic Lab Partnerships** | Pathology & Imaging Center Owners | Focus exclusively on the "Automated Report Delivery via WhatsApp" pain point. | **VERY HIGH** | 25% – 35% to Pilot |
| **3. HMIS / LIMS Reseller Channels** | Local Healthcare IT Vendors | Partner with local vendors who sell billing software to offer Kriya AI as their "WhatsApp Add-on". | **HIGH** | 15% – 25% to Pilot |
| **4. Medical Conferences & IMA Meets** | Specialist Doctors (Ortho, Pedia, Gynaec) | Tabletop booth demonstrating live trilingual booking and clinical safety firewall. | **MEDIUM-HIGH**| 10% – 20% to Demo |
| **5. Targeted LinkedIn & WhatsApp Outreach** | Hospital COOs, Medical Directors | Personalized B2B case-study teardowns showing front-desk payroll savings. | **MEDIUM** | 5% – 10% to Demo |

---

## 5. Unit Economics & North-Star Operational Metrics

### 5.1 Unit Economics Model (Per Clinic Average)
* **Average Revenue Per Account (ARPU):** ₹5,000 / month (~₹60,000 / year).
* **Meta Cloud API Message Costs:** ~₹600 – ₹1,000 / month (based on utility/service conversation rates).
* **Infrastructure Cost (FastAPI, Supabase, LLM Token Inference):** ~₹400 / month.
* **Gross Margin:** **70% – 80%**.
* **Estimated Customer Acquisition Cost (CAC):** ₹8,000 – ₹12,000 (Field sales + onboarding).
* **Payback Period:** **2 – 2.5 Months**.
* **Target Annual Retention:** **> 88%**.

### 5.2 North-Star Metrics Dashboard

| Metric Category | Primary North-Star KPI | Target Benchmark |
| :--- | :--- | :---: |
| **Product Reliability** | Booking & Queue Allocation Failure Rate | **< 0.05%** |
| **Patient Experience** | Median Time to Complete WhatsApp Booking | **< 65 Seconds** |
| **Customer Operational Impact** | Reduction in Front-Desk Phone Call Volume | **> 60% Reduction** |
| **Revenue Impact** | Patient No-Show Rate Reduction | **Drop from ~28% to < 10%** |
| **Business Health** | Net Revenue Retention (NRR) | **> 110%** |
