# Kriya AI — Website Information Architecture, Product Page & Hero Strategy

**Document Version:** 1.0.0  
**Date:** August 2026  
**Publisher:** Xylarc AI Web Engineering & Growth Marketing  
**Target:** Frontend Developers, UI/UX Designers, Product Marketing Leads  

---

## 1. XylarcAI.com Website Audit: Current vs. Recommended

```
+----------------------------------------------------------------------------------------------------+
|                                      WEBSITE AUDIT COMPARISON                                      |
+----------------------------------------------------------------------------------------------------+
```

| Dimension | Current State on `xylarcai.com` | Recommended Enhancement for Kriya AI |
| :--- | :--- | :--- |
| **Product Placement & Hierarchy** | Kriya AI is listed as one of two product cards on the homepage with high-level badges (`Zero-LLM Clinical Safety`, `WhatsApp Hospital OS`). | Provide a comprehensive, dedicated product landing page (`/products/kriya-ai`) with interactive simulations, live demo triggers, and clinical ROI calculators. |
| **Visual Proof & Interactive Demos** | Text descriptions with high-level badges; no live interactive WhatsApp click-to-chat preview. | Embed an interactive "Simulated WhatsApp Chat" directly on the page, allowing visitors to test 60-second booking, language switching, and the safety firewall in their browser. |
| **Healthcare Credibility & Trust** | Mentions security principles; lacks explicit deep-dives on DPDP 2023 consent, NMC liability firewalls, and Supabase RLS. | Dedicated "Security & Clinical Trust Center" section detailing deterministic firewalls, 438 passing automated tests, and data isolation. |
| **Call-to-Action (CTA) Clarity** | Generic "Book Demo ->" button linking to `/contact`. | Multi-tier conversion path: Primary = `[ Test Live on WhatsApp ]` (Instant gratification) / Secondary = `[ Book 15-Min Staff Walkthrough ]`. |
| **Commercial Packaging & ROI** | No visible pricing structure or ROI model. | Transparent pricing tier breakdown (Starter, Growth, Lab Pro, Hospital Enterprise) + Interactive ROI Savings Calculator. |

---

## 2. Dedicated Product Page Information Architecture (16 Sections)

```
+----------------------------------------------------------------------------------------------------+
|                               16-SECTION PRODUCT PAGE BLUEPRINT                                    |
+----------------------------------------------------------------------------------------------------+
|  1. HERO SECTION               | Master headline, 60s pitch, dual CTAs, interactive 3D UI composite.|
|  2. THE OPERATIONAL PROBLEM    | The 4 front-desk bottlenecks: call overload, no-shows, report lag. |
|  3. THE KRIYA AI SOLUTION      | The autonomous WhatsApp operating layer connecting patients to care|
|  4. THE PATIENT EXPERIENCE     | 60-second booking journey: symptom triage, slots, UPI, reminders.  |
|  5. CLINIC & POLYCLINIC FLOW   | Multi-doctor schedules, dynamic shifts, leave automation.          |
|  6. HOSPITAL ENTERPRISE FLOW   | Multi-branch governance, centralized analytics, high concurrency.  |
|  7. DIAGNOSTIC LAB FLOW        | Automated OCR report extraction, AI plain summaries, PDF delivery. |
|  8. CORE CAPABILITIES MATRIX   | Trilingual NLP, anti-double-booking, live queue tokens, reminders. |
|  9. INTEGRATIONS & HMIS SYNC   | Headless Playwright connectors (MocDoc/CallMedex), FHIR R4 API.    |
| 10. CLINICAL SAFETY FIREWALL   | Deterministic zero-LLM drug/dosage filter, NMC liability shield.   |
| 11. SECURITY & DATA PRIVACY    | Supabase RLS multi-tenancy, DPDP Act 2023 consent, 7-year retention|
| 12. ADMIN CONTROL PANEL        | Live queue management, doctor roster, payments, analytics tour.    |
| 13. INTERACTIVE ROI CALCULATOR | Input daily patients & no-shows -> Calculate recovered monthly rev.|
| 14. CUSTOMER TESTIMONIALS/PROOF| Quantified case studies from pilot clinics and diagnostic centers.  |
| 15. COMPREHENSIVE FAQ          | Answering technical, legal, and operational customer objections.   |
| 16. FINAL CONVERSION CTA       | Dual CTA: Test WhatsApp Demo or Apply for 30-Day Guided Pilot.     |
+----------------------------------------------------------------------------------------------------+
```

---

## 3. Website Hero Strategy (5 Evaluated Concepts)

```
+----------------------------------------------------------------------------------------------------+
|                                    HERO STRATEGY EVALUATION                                        |
+----------------------------------------------------------------------------------------------------+
```

### Concept 1: The Operational Reality Hook (RECOMMENDED PRIMARY)
* **Target Audience:** Clinic Owners, Managing Doctors, Hospital Administrators.
* **Headline:**  
  `Turn WhatsApp into Your Hospital's Autonomous Front Desk.`
* **Subheadline:**  
  `Kriya AI automates appointment booking, UPI payment gating, live queue tracking, and lab report delivery with zero staff overhead—slashing phone calls by 75% and eliminating patient no-shows.`
* **Primary CTA:** `[ Test Live on WhatsApp 💬 ]` (Triggers instant click-to-chat on demo number).
* **Secondary CTA:** `[ Book 15-Min Staff Walkthrough → ]`.
* **Visual Concept:** Interactive floating 3D composite: Left = iPhone showing trilingual WhatsApp booking flow; Right = Glowing translucent desktop admin view showing live queue tokens.
* **Conversion Rationale:** Directly states the exact business outcome, names the channel (WhatsApp), quantifies the operational relief (75% call drop), and provides an instant test.

---

### Concept 2: The Revenue & No-Show Recovery Hook
* **Target Audience:** Polyclinic Partners, Commercial Directors, Healthcare Investors.
* **Headline:**  
  `Eliminate Clinic No-Shows and Capture Every Lost Patient Inquiry.`
* **Subheadline:**  
  `Indian clinics lose 25% of booked consultations to no-shows. Kriya AI locks appointments with automated UPI payment gating, 24h/2h WhatsApp reminders, and real-time waiting room tokens.`
* **Primary CTA:** `[ Calculate Your Lost Revenue 📊 ]`.
* **Secondary CTA:** `[ See How It Works → ]`.
* **Visual Concept:** Dynamic split graphic: Red loss counter turning into an Emerald revenue recovery graph alongside the WhatsApp payment confirmation screen.

---

### Concept 3: The Clinical Safety & NMC Trust Hook
* **Target Audience:** Chief Medical Officers, Senior Consultants, Hospital Legal Counsel.
* **Headline:**  
  `Healthcare Automation Engineered for Complete Clinical Safety.`
* **Subheadline:**  
  `The only WhatsApp operating layer with a deterministic Zero-LLM Clinical Safety Firewall. Automate 100% of scheduling and report delivery while strictly protecting your practice from AI medical liability.`
* **Primary CTA:** `[ Explore Safety Architecture 🛡️ ]`.
* **Secondary CTA:** `[ Schedule Clinical Demo → ]`.
* **Visual Concept:** Architectural blueprint showing the regex clinical firewall intercepting drug queries and redirecting safely to certified doctor booking.

---

### Concept 4: The Speed & Patient Experience Hook
* **Target Audience:** Modern Specialist Clinics (Dental, Dermatology, Pediatrics).
* **Headline:**  
  `From WhatsApp "Hi" to Confirmed Doctor Slot in 58 Seconds.`
* **Subheadline:**  
  `Give your patients the fastest booking experience in your city. Trilingual symptom triage, verified doctor schedules, instant UPI payments, and live queue tracking—with zero apps to download.`
* **Primary CTA:** `[ Try the 58-Second Booking Demo ⚡ ]`.
* **Secondary CTA:** `[ View Pricing Plans → ]`.
* **Visual Concept:** High-speed motion mockup of a patient completing booking on WhatsApp with an active stopwatch ticking down to 58s.

---

### Concept 5: The Diagnostic Lab Automation Hook
* **Target Audience:** Pathology Laboratories, Diagnostic Imaging Chains, Radiologists.
* **Headline:**  
  `Deliver Lab Test Reports on WhatsApp the Second They're Approved.`
* **Subheadline:**  
  `Stop wasting hours manually searching folders and sending PDFs. Kriya AI automatically extracts reports, matches patient numbers, and delivers encrypted PDFs with patient-friendly AI summaries.`
* **Primary CTA:** `[ Automate Your Lab Reports 📋 ]`.
* **Secondary CTA:** `[ Book Diagnostic Demo → ]`.
* **Visual Concept:** Pathology report PDF transforming into an encrypted WhatsApp delivery card with a plain-language health summary.

---

## 4. Interactive Website Elements & Lead Conversion Mechanisms

```
+----------------------------------------------------------------------------------------------------+
|                                  INTERACTIVE CONVERSION SYSTEM                                     |
+----------------------------------------------------------------------------------------------------+
```

### 1. In-Browser Simulated WhatsApp Widget (Interactive Demo)
* Embedded on the hero section: A responsive iPhone container allowing desktop and mobile visitors to click buttons and type text into a sandbox instance of Kriya AI. Visitors can:
  * Select English, Hindi, or Telugu.
  * Type symptoms (*"fever"*, *"chest pain"*, *"back pain"*) and watch smart follow-up questions appear.
  * Test the clinical safety firewall by typing *"give me paracetamol dose"*.

### 2. Interactive Clinic ROI Calculator
* Sliders for prospective buyers:
  1. *Number of Active Doctors (1 to 20)*
  2. *Average Daily Patients per Doctor (10 to 60)*
  3. *Average Consultation Fee (₹200 to ₹1,500)*
  4. *Current Estimated No-Show Rate (10% to 40%)*
* Real-Time Output Cards:
  * **Recoverable Monthly Revenue:** `₹45,000 – ₹1,80,000 / month`
  * **Front-Desk Hours Saved:** `60 – 180 Hours / month`
  * **ROI Multiple on Kriya AI Subscription:** `8x to 15x Return`

### 3. Frictionless Pilot Application Modal
* Simple 4-field lead capture modal:
  1. *Clinic / Hospital Name*
  2. *City (e.g., Hyderabad, Visakhapatnam, Bengaluru)*
  3. *Number of Doctors / Daily OPD Volume*
  4. *WhatsApp Contact Number for Instant Demo Setup*
* Instant Response: Dispatches an automated WhatsApp confirmation from Kriya AI directly to the prospect’s phone within 5 seconds.
