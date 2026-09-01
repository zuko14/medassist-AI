# KRIYA AI — HEALTHCARE BUYER & OPERATIONAL PERSONAS

**Document Type:** Client Persona Intelligence, Stakeholder Objectives, Pain Points & Value Alignment  
**Target Audience:** Commercial Strategy Teams, Solution Architects, Healthcare Sales Consultants  

---

## 1. Executive Summary of Stakeholder Ecosystem

Healthcare buying decisions involve multiple stakeholders with distinct priorities:
* **Executive Leadership (CEO/Owner):** Growth, brand reputation, patient retention, and revenue capture.
* **Medical Leadership (CMO/Doctors):** Clinical safety, malpractice liability protection, schedule respect, and uninterrupted consultations.
* **Operational Leadership (COO/Front Desk):** Call volume reduction, waiting room decongestion, and staff workload relief.
* **Technology Leadership (CTO/IT):** Data security, tenant isolation, system reliability, EMR interoperability, and DPDP compliance.
* **Diagnostic Leadership (Lab Director):** Report turnaround efficiency, paper cost elimination, and zero patient dispatch errors.

---

## 2. Detailed Stakeholder Personas

### 2.1 Persona 1: The Hospital Managing Director / CEO / Founder
* **Profile:** Executive responsible for overall hospital profitability, multi-branch expansion, patient footfall, and institutional reputation.
* **Core Pressures & Pain Points:**
  * High patient churn due to frustrating front-desk phone delays and crowded waiting rooms.
  * Revenue leakage from 20–35% OPD appointment no-shows and uncollected consultation fees.
  * Over-reliance on third-party aggregators (e.g. Practo) who control patient data and charge high commissions.
* **What They Care About in Kriya AI:**
  * **24/7 Digital Front Door:** Captures patient bookings after OPD hours (evenings and weekends).
  * **Direct Brand Ownership:** Operates under the hospital’s verified WhatsApp number with zero competitor ads.
  * **Measurable ROI:** Slashes no-shows via automated WhatsApp reminders and pre-collects consultation fees via UPI.
* **Executive Elevator Pitch:**
  > "Kriya AI converts your hospital’s verified WhatsApp number into an autonomous 24/7 digital front desk. It captures after-hours patient demand, cuts appointment no-shows by more than half, and pre-collects consultation fees—all while keeping 100% of the patient relationship under your own hospital brand."

---

### 2.2 Persona 2: The Chief Medical Officer (CMO) / Medical Director
* **Profile:** Senior physician responsible for clinical governance, doctor satisfaction, healthcare quality, and medical-legal liability.
* **Core Pressures & Pain Points:**
  * Fear of AI chatbots providing inaccurate medical advice or drug dosages that expose the hospital to NMC malpractice lawsuits.
  * Doctor frustration over double-booked slots, schedule overruns, and walk-in chaos.
  * Disrupted doctor schedules due to unmanaged leaves and sudden emergency room duty.
* **What They Care About in Kriya AI:**
  * **Zero-LLM Clinical Safety Firewall:** Deterministic blocker completely prevents AI from dispensing medical prescriptions or diagnostic advice.
  * **Dynamic Roster & Leave Engine:** Doctors have full control over consultation shifts, buffers, and planned leaves with automatic patient notifications.
  * **Appropriate Specialty Routing:** Groq AI symptom classification guides patients to the correct department rather than random doctor selection.
* **Clinical Elevator Pitch:**
  > "Kriya AI protects your hospital’s clinical integrity with a deterministic safety firewall that refuses to generate medical advice or drug dosages. It respects doctor consultation schedules, eliminates double-booking through database-level locks, and accurately routes patient symptoms to the correct clinical specialty."

---

### 2.3 Persona 3: The Chief Technology Officer (CTO) / Chief Information Officer (CIO) / IT Head
* **Profile:** Technical leader overseeing hospital infrastructure, cybersecurity, EHR integrations, data governance, and regulatory compliance.
* **Core Pressures & Pain Points:**
  * Security risks of shadow IT, unvetted chatbot APIs, and cross-tenant data leaks.
  * Compliance liability under the India Digital Personal Data Protection (DPDP) Act 2023.
  * Complex integration hurdles with legacy HMIS/EMRs (MocDoc, CallMedex, custom SQL databases).
* **What They Care About in Kriya AI:**
  * **Defense-in-Depth Architecture:** PostgreSQL Row-Level Security (RLS), Meta HMAC-SHA256 signature verification, and bcrypt session tokens.
  * **ACID Concurrency & Anti-Collision:** Partial unique indexes on PostgreSQL preventing slot collisions under heavy load.
  * **Turnkey EMR Connectors:** Playwright-based headless workers and HL7 FHIR R4 interfaces that extract data without requiring invasive vendor backend rewrites.
* **Technical Elevator Pitch:**
  > "Kriya AI is engineered with enterprise architectural rigor: PostgreSQL Row-Level Security, HMAC-SHA256 webhook verification, append-only payment audit logs, and built-in DPDP Act compliance. Our Playwright connector seamlessly extracts lab reports and rosters from your existing EMR without requiring complex database migrations."

---

### 2.4 Persona 4: The Chief Operating Officer (COO) / Hospital Operations Head
* **Profile:** Leader managing day-to-day outpatient department (OPD) flow, front-desk reception staff, waiting room logistics, and patient feedback.
* **Core Pressures & Pain Points:**
  * Reception desks swamped with hundreds of repetitive phone calls for doctor timings and report statuses.
  * Crowded OPD waiting rooms where patients wait blindly for 45–90 minutes, leading to frequent verbal altercations at the counter.
  * High front-desk staff turnover due to high-stress, repetitive administrative tasks.
* **What They Care About in Kriya AI:**
  * **Front-Desk Call Deflection:** Deflects 60–80% of routine appointment and inquiry calls to automated WhatsApp self-service.
  * **Live WhatsApp Queue Tokens:** Patients receive dynamic queue tokens (e.g. `Q-015`) and can query live waiting status from anywhere.
  * **Simplified Reception Dashboard:** Real-time visibility into today's appointments, walk-ins, doctor leaves, and patient check-ins.
* **Operations Elevator Pitch:**
  > "Kriya AI frees your front-desk staff from answering the same 200 phone calls every day. Patients self-book on WhatsApp, check their live token number in the waiting room, and receive their test reports automatically—reducing lobby congestion and allowing your staff to focus on in-person patient care."

---

### 2.5 Persona 5: The Diagnostic Center Owner / Pathology Laboratory Director
* **Profile:** Commercial director or chief pathologist managing standalone diagnostic centers or pathology laboratory chains.
* **Core Pressures & Pain Points:**
  * Reception phones ringing continuously with patients asking "Is my blood report ready?".
  * High recurring expenditure on paper printing, envelopes, and dispatch logistics.
  * Patients delayed in collecting critical test reports, slowing down medical follow-ups.
* **What They Care About in Kriya AI (Diagnostream):**
  * **Automated LIMS Scrape & Deliver:** Headless connector syncs with lab software and dispatches reports immediately upon technician authorization.
  * **Patient Match Safety Gate:** Fuzzy name and phone matching ensures confidential reports are never sent to incorrect recipients.
  * **AI Clinical Summary:** Translates complex lab values into plain-English summaries with abnormal parameter alerts and doctor review CTAs.
* **Diagnostic Elevator Pitch:**
  > "Diagnostream eliminates 90% of report status calls by automatically delivering authenticated lab reports directly to patient WhatsApp within minutes of lab sign-off. It includes fuzzy identity safety checks to prevent misrouting and provides an AI-generated clinical summary that patients understand."

---

### 2.6 Persona 6: The Solo Clinic Doctor / Private Specialist
* **Profile:** Independent medical practitioner (e.g. Pediatrician, Gynecologist, Dermatologist, Orthopedic Consultant) running a private clinic.
* **Core Pressures & Pain Points:**
  * Constant phone interruptions during patient physical examinations.
  * Missing appointment booking requests sent after clinic operating hours.
  * Managing appointment diaries on paper or informal WhatsApp chats without payment security.
* **What They Care About in Kriya AI (Solo Clinic):**
  * **Automated WhatsApp Receptionist:** Operates 24/7 without needing full-time administrative staff.
  * **Pre-Consultation Fee Collection:** Collects consultation deposits via UPI before booking confirmation, eliminating ghost no-shows.
  * **Direct Location & Instructions:** Automatically sends clinic Google Maps directions, parking info, and preparation guidelines.
* **Solo Clinic Elevator Pitch:**
  > "Kriya AI gives your private clinic an automated 24/7 WhatsApp receptionist that schedules appointments around your exact consulting hours, pre-collects consultation fees via UPI, and sends patients clinic directions—without interrupting you during patient consultations."
