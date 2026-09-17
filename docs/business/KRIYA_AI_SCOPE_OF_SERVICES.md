# KRIYA AI — SCOPE OF SERVICES

**Document Classification:** Client-Facing Service Scope Agreement  
**Provider:** Zuko Labs (Meta Technology Provider — Business ID: `1602916427428175`)  
**Platform:** Kriya AI — Multi-Tenant WhatsApp Healthcare Operations System  
**Document Version:** 1.0 | Date: September 2026  

---

## 1. Executive Summary

Kriya AI is an **enterprise-grade, multi-tenant Healthcare Operations System** that automates outpatient scheduling, specialty procedure discovery, cashless digital payments, live waiting room queue management, and diagnostic laboratory report delivery — all through the hospital's own **verified WhatsApp Business Account**.

The platform operates as a **24/7 intelligent digital front door** that connects patients to hospitals, clinics, specialty centers, and diagnostic laboratories with zero app downloads, zero patient registrations, and zero staff overhead.

**Key Platform Credentials:**
- **Meta Cloud API v21.0** Official Partner Integration
- **Razorpay PCI-DSS Level 1** Certified Payment Gateway
- **DPDP Act 2023** Compliant Data Governance
- **HL7 FHIR R4** Standards-Ready Architecture
- **ABDM / ABHA** Gateway-Ready Framework

---

## 2. Platform Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         KRIYA AI 7-LAYER SYSTEM                             │
├──────────────────────────────────────────────────────────────────────────────┤
│ 1. INGESTION LAYER       │ Meta WhatsApp Cloud API v21.0 Webhooks           │
│ 2. PERIMETER SECURITY    │ HMAC-SHA256 Signature Verification + Rate Limiter│
│ 3. MULTI-TENANT ROUTING  │ Dynamic Clinic Resolution by Phone Number / WABA │
│ 4. CLINICAL FIREWALL     │ Zero-LLM Deterministic Medical Safety Filter     │
│ 5. STATE & LOGIC ENGINE  │ 22-State Finite State Machine + AI Triage        │
│ 6. TRANSACTION GATEWAY   │ Razorpay Payments + PostgreSQL Slot Locking      │
│ 7. PERSISTENCE & AUDIT   │ Supabase PostgreSQL + Row-Level Security + Audit │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Service Plans — 11 Modular Operational Tiers

Kriya AI delivers **11 purpose-built operational plans** structured across four healthcare delivery models. Each plan is independently activatable, upgradeable, and combinable.

### 3.1 General OPD Solutions

| Plan | Target Facility | Doctor Capacity | Key Capabilities |
|:---|:---|:---|:---|
| **Solo Clinic** (`soloclinic`) | Individual private practitioners, single-doctor practices | 1 Doctor | Direct WhatsApp booking, UPI pre-payment, automated reminders, instant doctor notifications |
| **Essential** (`essential`) | Growing clinics, community health centers (1–3 doctors) | Up to 3 Doctors | Multi-doctor scheduling, shift rosters, 24h/2h automated reminders, patient rescheduling self-service, receptionist dashboard |
| **PolyClinic** (`polyclinic`) | Multi-specialty medical centers with diverse departments | Up to 25 Doctors | Symptom-based AI department triage, dynamic multi-shift slot engine, live OPD queue tokens, family dependent booking, multi-branch support |
| **Enterprise** (`enterprise`) | Large hospital chains, multi-branch healthcare networks | Unlimited | All features (wildcard access), multi-tiered RBAC, centralized multi-branch administration, HL7 FHIR R4 interoperability, custom EMR connector bridges, 99.9% SLA |

### 3.2 Specialty Clinical Verticals

| Plan | Target Facility | Specialty Focus | Key Capabilities |
|:---|:---|:---|:---|
| **Dermatology** (`derma`) | Skin, hair & cosmetology clinics | Dermatology & Aesthetics | Pre-seeded treatment catalog (HydraFacial, Chemical Peels, Laser Hair Reduction, Carbon Laser, PRP), concern-based triage ("Acne scars", "Pigmentation", "Anti-aging"), automated pre/post care instructions |
| **Eye** (`eye`) | Ophthalmology hospitals & vision care centers | Ophthalmology | Specialized packages (Cataract Evaluation, LASIK Screening, Diabetic Retinopathy, Dry Eye), pupil dilation clinical alerts, escort travel warnings |
| **Dental** (`dental`) | Dental clinics, orthodontic chains | Dental Surgery & Orthodontics | High-ticket procedure booking (Root Canal, Clear Aligners, Scaling & Polishing, Implants), variable chair-time slot durations, post-procedure diet and care instructions |
| **IVF** (`ivf`) | Fertility & reproductive medicine centers | IVF & Fertility | Confidential couple consultations, IUI/IVF packages, semen analysis, cycle tracking, empathy-first communication guardrails, discrete messaging protocols |

### 3.3 Diagnostic Infrastructure

| Plan | Target Facility | Key Capabilities |
|:---|:---|:---|
| **Diagnostream** (`diagstream`) | Standalone pathology labs, diagnostic imaging centers | Autonomous EMR/LIMS scraping (MocDoc, CallMedex), Tesseract OCR parameter extraction, fuzzy patient identity matching, AI clinical summaries with abnormal value alerts, WhatsApp PDF report delivery, lab test booking, home sample collection scheduling |
| **Diagbooking** (`diagbooking`) | Lab test booking hubs, health checkup networks | WhatsApp lab test directory browsing, home sample collection scheduling, fasting preparation alerts, UPI test fee pre-collection |

### 3.4 Flagship Multi-Specialty Hospital OS

| Plan | Target Facility | Key Capabilities |
|:---|:---|:---|
| **Multi-Specialty** (`multispecialty`) | General hospitals combining OPD departments + specialty treatment catalogs | Unified 8-row WhatsApp menu (✨ Our Treatments · 🔍 Find by Concern · Book Appointment · Our Services · Our Doctors · 🧪 Book Lab Test · Emergency · Talk to Staff), dual-path triage (symptom → department + concern → treatment), combined OPD and specialty booking |

---

## 4. Detailed Scope of Services

### 4.1 WhatsApp Patient Communication Layer

| Service | Description | Included In |
|:---|:---|:---|
| **Verified WhatsApp Business Account Integration** | Hospital retains 100% ownership of its WhatsApp number and WABA. Kriya AI operates as the authorized technology backend. | All Plans |
| **24/7 Intelligent Digital Front Door** | Patients interact with the hospital's verified WhatsApp number any time of day or night with zero app downloads. | All Plans |
| **Multilingual Conversational Support** | Real-time patient interactions in English, Hindi, and Telugu with natural language understanding. | All Plans |
| **Interactive WhatsApp Menus** | Plan-specific button menus (Book Appointment, Our Treatments, Find by Concern, Our Services, Our Doctors, Book Lab Test, Emergency, Talk to Staff). | All Plans |
| **Automated Appointment Reminders** | WhatsApp reminders sent 24 hours and 2 hours before scheduled consultations with rescheduling/cancellation options. | All Plans (except diagstream) |
| **Patient Rescheduling & Cancellation Self-Service** | Patients can reschedule or cancel their own appointments via WhatsApp with automatic slot release. | All Plans (except diagstream) |
| **Post-Consultation Feedback Collection** | Automated patient satisfaction survey sent after doctor visit with structured rating collection. | Essential, PolyClinic, Enterprise, Specialty Plans |
| **Google Maps Navigation Links** | Confirmed booking cards include 1-click Google Maps directions to the hospital/clinic location. | All Plans |
| **Emergency Escalation Protocol** | Real-time front-desk staff notification via WhatsApp for patient emergency situations. | All Plans |

---

### 4.2 Appointment Scheduling & Slot Management

| Service | Description | Included In |
|:---|:---|:---|
| **Dynamic Doctor Slot Calculation** | Automatically computes available appointment slots based on doctor shift rosters, room allocations, declared leaves, and 30-minute advance booking buffers. | All Plans (except diagstream/diagbooking) |
| **Atomic Slot Locking (Zero Double-Booking)** | PostgreSQL ACID partial unique index constraints mathematically prevent two patients from booking the same doctor at the same time — even under concurrent traffic surges. | All Plans (except diagstream/diagbooking) |
| **10-Minute Temporary Slot Hold** | When a patient selects a slot, a temporary 10-minute database lock is applied. The slot is released automatically if payment is not completed. | All Plans (except diagstream/diagbooking) |
| **Doctor Leave & Holiday Management** | Doctors can declare single-day or multi-day leaves. All affected patients are automatically notified and offered rescheduling. | All Plans (except diagstream/diagbooking) |
| **Multi-Shift Roster Support** | Support for complex doctor schedules with morning, afternoon, and evening shifts across multiple rooms and branches. | Essential, PolyClinic, Enterprise, Specialty Plans |
| **Family Dependent Booking** | Parents and guardians can book appointments for family members (children, elderly, dependents) under a single phone number. | All Plans |
| **Walk-In Queue Management** | Receptionists can add walk-in patients to the digital queue from the reception console with sequential token issuance. | All Plans (except diagstream/diagbooking) |

---

### 4.3 AI-Powered Clinical Intelligence

| Service | Description | Included In |
|:---|:---|:---|
| **Symptom-to-Department AI Triage** | Groq Llama-3.3-70b AI maps patient-described symptoms (e.g., "chest pain", "skin rash", "blurred vision") to appropriate medical departments with clinical safety guardrails. | Essential, PolyClinic, Enterprise, MultiSpecialty |
| **Concern-Based Treatment Matching** | Patients describe aesthetic or clinical concerns (e.g., "acne scars", "teeth alignment", "hair loss") and Kriya AI maps them to specific treatments and packages from the clinic's catalog. | Derma, Eye, Dental, IVF, MultiSpecialty |
| **Zero-LLM Clinical Safety Firewall** | Deterministic regex interceptor strictly blocks the AI from dispensing medical advice, prescribing medications, or making unauthorized diagnoses — protecting hospitals from NMC malpractice liability. | All Plans |
| **AI Lab Report Summarization** | Generates plain-language patient summaries of complex laboratory test results with abnormal value highlighting, while redacting all PII per DPDP Act 2023. | Diagstream, PolyClinic, Enterprise, MultiSpecialty |

---

### 4.4 Specialty Treatment Catalog Management

| Service | Description | Included In |
|:---|:---|:---|
| **Pre-Seeded Clinical Starter Catalogs** | Every specialty plan comes pre-loaded with curated starter treatments (procedures, packages, and concern tags) ready for clinic review and activation within minutes. | Derma, Eye, Dental, IVF, MultiSpecialty |
| **Treatment Package Configuration** | Define treatments with name, description, duration, session counts, transparent pricing, category tags, and required doctor qualifications. | Derma, Eye, Dental, IVF, MultiSpecialty |
| **Concern-to-Treatment Mapping Engine** | Customizable tags linking patient concerns (e.g., "dark circles", "cavity pain") to specific treatment packages for intelligent WhatsApp discovery. | Derma, Eye, Dental, IVF, MultiSpecialty |
| **Doctor-to-Treatment Qualification Mapping** | Link specific treatments to qualified specialist doctors, ensuring patients are only shown doctors certified for their selected procedure. | Derma, Eye, Dental, IVF, MultiSpecialty |
| **Automated Pre-Procedure Care Instructions** | WhatsApp delivery of pre-procedure preparation guidelines (e.g., "Avoid retinol products 48 hours before your Chemical Peel", "No food/water 8 hours before surgery"). | Derma, Eye, Dental, IVF, MultiSpecialty |
| **Post-Procedure Follow-Up Instructions** | Automated post-treatment care advice sent to patient WhatsApp (e.g., "Apply SPF 50 sunscreen for 7 days post-treatment", "Avoid hot/spicy food for 24 hours"). | Derma, Eye, Dental, IVF, MultiSpecialty |

---

### 4.5 Cashless Digital Payments

| Service | Description | Included In |
|:---|:---|:---|
| **Razorpay UPI Payment Integration** | Seamless UPI, Google Pay, PhonePe, Credit/Debit Card, and NetBanking payment links generated within the WhatsApp conversation. | All Plans |
| **Consultation Fee Pre-Collection** | Full or partial consultation fee collected upfront during appointment booking to secure the slot and reduce no-shows. | All Plans (except diagstream/diagbooking) |
| **Procedure Deposit Collection** | Customizable token deposit amounts (e.g., ₹500 or 25% of treatment cost) for high-value elective procedures. | Derma, Eye, Dental, IVF, MultiSpecialty |
| **Lab Test Fee Pre-Collection** | Upfront payment for selected lab tests and health checkup packages during WhatsApp booking. | Diagstream, Diagbooking, PolyClinic, Enterprise, MultiSpecialty |
| **Automated Payment Expiry & Refund** | Expired payment holds automatically release slots back to the pool. Failed webhook signatures trigger automatic patient refunds. | All Plans |
| **HMAC-SHA256 Payment Verification** | All Razorpay payment webhooks are cryptographically verified using HMAC-SHA256 signatures to prevent fraud and tampering. | All Plans |
| **Configurable Payment Policies** | Hospitals can configure per-department or per-doctor payment policies: full advance payment, nominal token fee, percentage deposit, or zero-fee gated booking. | All Plans |

---

### 4.6 Live OPD Waiting Room Queue System

| Service | Description | Included In |
|:---|:---|:---|
| **Digital Queue Token Allocation** | Every confirmed patient receives a sequential digital queue token (e.g., `Q-014`) at the time of booking. | All Plans (except diagstream/diagbooking) |
| **Real-Time WhatsApp Queue Tracking** | Patients can check their live queue position on WhatsApp: _"Current token with Dr. Sharma is Q-011. 3 patients ahead of you."_ | All Plans (except diagstream/diagbooking) |
| **Receptionist Token Advance Console** | Front-desk staff advance queue tokens with a single click from the reception dashboard (`reception.html`), automatically alerting the next patient. | All Plans (except diagstream/diagbooking) |
| **Hallway TV Display Sync** | Queue status can be displayed on hospital lobby LED screens showing the currently serving token and doctor cabin number. | PolyClinic, Enterprise, MultiSpecialty |
| **Doctor Delay Notifications** | If a doctor is running behind schedule, patients in the queue receive automated WhatsApp updates with revised estimated wait times. | PolyClinic, Enterprise, MultiSpecialty |

---

### 4.7 Diagnostic & Laboratory Services

| Service | Description | Included In |
|:---|:---|:---|
| **WhatsApp Lab Test Directory** | Patients browse available lab tests and health checkup packages directly on WhatsApp with category navigation. | Diagstream, Diagbooking, PolyClinic, Enterprise, MultiSpecialty |
| **Home Sample Collection Scheduling** | Patients book home phlebotomist visits for blood sample collection with address, date, and time slot selection. | Diagstream, Diagbooking, PolyClinic, Enterprise, MultiSpecialty |
| **Fasting Preparation Alerts** | Automated pre-collection reminders with test-specific fasting guidelines (e.g., "12 hours fasting required for your Lipid Profile test tomorrow at 8:00 AM"). | Diagstream, Diagbooking |
| **Autonomous EMR/LIMS Report Scraping** | Playwright headless browser connector daemon polls laboratory information systems (MocDoc, CallMedex) every 10 minutes for newly authorized test reports — with zero legacy vendor API development. | Diagstream, Enterprise |
| **Patient Identity Match Safety Gate** | Fuzzy similarity scoring engine strips honorifics and matches patient names and phone numbers to prevent confidential report misrouting. Reports below 85% match confidence are routed to manual front-desk review. | Diagstream, Enterprise |
| **Tesseract OCR Parameter Extraction** | Automated extraction of clinical test parameters from lab PDF reports using Tesseract OCR and pdfplumber for structured data parsing. | Diagstream, Enterprise |
| **AI Clinical Report Summary** | Generates plain-language patient summaries highlighting out-of-range and abnormal values, with all PII redacted per DPDP Act 2023. | Diagstream, Enterprise |
| **WhatsApp PDF Report Delivery** | Instant delivery of authenticated lab report PDFs to the patient's WhatsApp with a 1-click CTA to book a follow-up doctor consultation. | Diagstream, Enterprise |
| **Doctor Abnormal Value Alerting** | Automatic notification to the referring doctor when critical or abnormal lab values are detected in patient test results. | Diagstream, Enterprise |

---

### 4.8 Administrative Web Consoles

Kriya AI provides **6 dedicated, role-tailored web consoles** requiring zero desktop software installation — accessible from any browser on desktops, tablets, or smartphones.

| Console | URL Route | Target User | Key Functions |
|:---|:---|:---|:---|
| **Live Reception Desk** | `/admin-panel` (reception.html) | Front-desk receptionists | 1-click patient check-in, walk-in queue token issuance, no-show marking, daily appointment log, hallway TV display sync |
| **Doctor Cabin Portal** | `/admin-panel` (doctor.html) | Consulting physicians & specialists | View daily patient queue, advance tokens, 1-click leave/delay declaration with automatic patient rescheduling, schedule sovereignty |
| **Specialty Treatment Console** | `/derma-panel`, `/eye-panel`, `/dental-panel`, `/ivf-panel` (specialty.html) | Specialty clinic administrators | Manage treatment catalogs, package pricing, session counts, concern tags, doctor qualification mapping, pre/post care instructions |
| **Multi-Specialty Hospital Command** | `/hospital-panel` (multispecialty.html) | Hospital administrators | Unified management of OPD doctor rosters + specialty treatment catalogs in a single interface |
| **Diagnostic Center Console** | `/admin-panel` (diagnostics.html) | Pathology lab managers | Monitor LIMS scraping queues, review OCR confidence scores, dispatch home collection phlebotomists, track report delivery status |
| **Platform Superadmin** | `/platform-panel` (platform.html) | Kriya AI platform owners | Multi-tenant clinic provisioning, plan upgrades, WhatsApp credential verification, cross-clinic analytics, financial dashboard (MRR, expenses, per-clinic profit) |

---

### 4.9 Security, Compliance & Data Governance

| Service | Description | Included In |
|:---|:---|:---|
| **Multi-Tenant Row-Level Security (RLS)** | PostgreSQL RLS guarantees strict mathematical data isolation. One clinic or branch can never view, query, or modify another tenant's patient records. | All Plans |
| **Meta HMAC-SHA256 Webhook Verification** | All inbound WhatsApp webhooks are cryptographically verified via `X-Hub-Signature-256` header validation, blocking spoofed and tampered requests. | All Plans |
| **Inbound Message Idempotency** | Atomic `processed_messages` ledger prevents duplicate message execution during WhatsApp network retries and webhook replays. | All Plans |
| **DPDP Act 2023 Compliance** | Automated conversational consent capture, structured consent audit logs, right-to-erasure API endpoints, and automated 30-day chat transcript purges while retaining 7-year medical audit records. | All Plans |
| **PCI-DSS Scope Minimization** | Zero cardholder or banking data is stored on Kriya AI servers. All financial transactions are processed through RBI-regulated Razorpay payment gateways. | All Plans |
| **Clinical Safety Firewall (NMC Compliance)** | Deterministic regex interceptor strictly prevents the AI from offering medical diagnoses, prescribing medications, or providing unauthorized clinical guidance — protecting hospitals from National Medical Commission malpractice liability. | All Plans |
| **Fail-Closed State Machine** | Ambiguous payment webhooks, conflicting patient identities, or uncertain AI responses are automatically transitioned to `pending_review` status for manual human verification rather than auto-confirming. | All Plans |
| **Encrypted Data at Rest & in Transit** | All patient data is encrypted at rest in PostgreSQL and in transit via TLS 1.3. | All Plans |
| **Session Token Authentication** | All admin consoles use secure bcrypt-hashed session tokens with sliding expiration and multi-role RBAC enforcement. | All Plans |

---

### 4.10 Interoperability & System Integration

| Service | Description | Included In |
|:---|:---|:---|
| **Non-Invasive EMR Connectors** | Headless Playwright browser daemons synchronize with web-based EMRs (MocDoc, CallMedex, and custom HMIS portals) with zero expensive API development from legacy vendors. | Diagstream, Enterprise |
| **HL7 FHIR R4 REST API** | REST API endpoints structured around international HL7 FHIR R4 healthcare data exchange standards for seamless integration with modern enterprise platforms. | Enterprise |
| **ABDM / ABHA Gateway Ready** | Built-in schema foundations and gateway stubs for Ayushman Bharat Digital Mission patient identifiers and ABHA-based electronic health record compliance. | Enterprise |
| **Zero-Disruption Parallel Deployment** | Kriya AI operates as an external digital front door alongside existing hospital management systems — without requiring rip-and-replace disruption to core billing or inpatient software. | All Plans |
| **Razorpay Payment Gateway** | Full Razorpay Orders API and Payment Links API integration supporting UPI, Google Pay, PhonePe, Credit/Debit Cards, and NetBanking. | All Plans |

---

### 4.11 Analytics & Operational Intelligence

| Service | Description | Included In |
|:---|:---|:---|
| **Daily Appointment Dashboard** | Real-time view of today's bookings, check-ins, no-shows, and completed consultations per doctor. | All Plans |
| **Revenue & Collection Tracking** | Track pre-collected consultation fees, procedure deposits, and lab test payments per doctor, department, or branch. | All Plans |
| **No-Show Rate Analytics** | Monitor appointment no-show percentages per doctor, department, and time period to identify scheduling optimization opportunities. | Essential, PolyClinic, Enterprise |
| **Doctor Utilization Reports** | Track doctor slot utilization rates, average consultation duration, and booking fill rates. | PolyClinic, Enterprise |
| **Patient Feedback & Satisfaction Scores** | Aggregated post-consultation sentiment and rating scores per doctor and department. | Essential, PolyClinic, Enterprise, Specialty Plans |
| **WhatsApp Message Accounting** | Per-clinic tracking of outbound message volumes against daily quota limits with automatic throttling protection. | All Plans |
| **Platform Financial Dashboard** | Platform-owner view of Monthly Recurring Revenue (MRR), operating expenses (hosting, AI, messaging), per-clinic billing rates, and net profit margins. | Platform Superadmin |

---

## 5. Implementation & Onboarding

### 5.1 Turnkey 14-Day Implementation Roadmap

| Phase | Duration | Activities |
|:---|:---|:---|
| **Phase 1: Provisioning** | Days 1–3 | Meta WhatsApp Business Account (WABA) verification, phone number registration, multi-tenant database provisioning, admin portal credential setup |
| **Phase 2: Clinical Setup** | Days 4–6 | Doctor consulting profiles and shift rosters, department configuration, pre-seeded specialty treatment catalog review and activation, Razorpay payment gateway linking |
| **Phase 3: Integration** | Days 7–10 | Diagnostream EMR/LIMS Playwright connector setup (if applicable), MocDoc/CallMedex synchronization testing, OCR parsing validation, WhatsApp message template approval |
| **Phase 4: Go-Live** | Days 11–14 | Reception staff console training, desk QR collateral placement, patient journey end-to-end testing, production launch with active engineering support |

### 5.2 Onboarding Deliverables

Upon successful onboarding, the client receives:
1. **Verified WhatsApp Business Account** with hospital branding and green-tick verification.
2. **Approved WhatsApp Message Templates** for appointment confirmations, reminders, and lab report delivery.
3. **Configured Admin Web Consoles** with role-based credentials for receptionists, doctors, and administrators.
4. **Active Specialty Treatment Catalogs** (for applicable plans) with pre-seeded starter treatments reviewed and activated.
5. **Configured Payment Gateway** with Razorpay credentials linked and payment policy customized per plan requirements.
6. **Reception QR Code Collateral** for desk placement enabling instant patient WhatsApp self-service activation.
7. **Staff Training Session** covering reception console operations, doctor portal usage, and emergency escalation workflows.

---

## 6. Service Boundaries & Exclusions

The following items are **outside the scope** of Kriya AI services:

| Exclusion | Rationale |
|:---|:---|
| **Medical diagnosis, prescriptions, or clinical advice** | The AI clinical safety firewall explicitly blocks medical guidance. Kriya AI routes patients to qualified doctors. |
| **Inpatient (IPD) management** | Kriya AI is purpose-built for outpatient (OPD) scheduling and front-desk operations. |
| **Core HMIS billing & accounting** | Kriya AI operates as a parallel front-door layer and does not replace core hospital billing, accounting, or ERP software. |
| **Pharmacy inventory & dispensing** | Medication management and pharmacy operations are outside the platform scope. |
| **Medical insurance claims processing** | Insurance TPA and claims adjudication are not handled by the platform. |
| **Custom mobile app development** | Kriya AI operates natively on WhatsApp. Custom iOS/Android apps are not part of the service. |
| **Hardware procurement** | Physical devices (lobby TV screens, QR code stands, reception terminals) are procured by the client. |

---

## 7. Plan Feature Summary Grid

| Feature | Solo | Essential | Poly | Diag-stream | Diag-booking | Enterprise | Derma | Eye | Dental | IVF | Multi-Specialty |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| WhatsApp 24/7 Front Door | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Multilingual (EN/HI/TE) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Clinical Safety Firewall | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Doctor Slot Booking | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Dynamic Shift Rosters | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Atomic Slot Anti-Collision | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Razorpay UPI Payments | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 24h/2h Appointment Reminders | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Live OPD Queue Tokens | ✅ | ✅ | ✅ | — | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Family Dependent Booking | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Symptom → Department Triage | — | ✅ | ✅ | — | — | ✅ | — | — | — | — | ✅ |
| Specialty Treatment Catalog | — | — | — | — | — | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| Concern-Based Discovery | — | — | — | — | — | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| Pre/Post Care Instructions | — | — | — | — | — | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lab Test Booking (WhatsApp) | — | — | ✅ | ✅ | ✅ | ✅ | — | — | — | ✅ | ✅ |
| Home Sample Collection | — | — | ✅ | ✅ | ✅ | ✅ | — | — | — | — | ✅ |
| EMR/LIMS Auto-Scraping | — | — | — | ✅ | — | ✅ | — | — | — | — | — |
| OCR & AI Report Summary | — | — | — | ✅ | — | ✅ | — | — | — | — | — |
| WhatsApp Report Delivery | — | — | — | ✅ | — | ✅ | — | — | — | — | — |
| Multi-Branch Support | — | — | ✅ | ✅ | ✅ | ✅ | — | — | — | — | ✅ |
| Multi-Role RBAC Admin | — | — | ✅ | — | — | ✅ | — | — | — | — | ✅ |
| HL7 FHIR R4 / ABDM | — | — | — | — | — | ✅ | — | — | — | — | — |
| DPDP Act Compliance | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| PostgreSQL RLS Isolation | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Patient Feedback & CSAT | — | ✅ | ✅ | — | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Revenue Analytics | — | ✅ | ✅ | — | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

---

## 8. Support & Service Level

| Support Tier | Coverage | Response Time |
|:---|:---|:---|
| **Standard** | Business hours (9 AM – 7 PM IST, Monday–Saturday) | < 4 hours |
| **Priority Business** | Extended hours (8 AM – 10 PM IST, Monday–Saturday) | < 2 hours |
| **Mission-Critical Lab** | 24/7 for lab pipeline incidents | < 1 hour |
| **Enterprise Dedicated** | 24/7 with dedicated account manager | < 30 minutes |

**Support Channels:** WhatsApp Business Support, Email, Phone (Enterprise only)

---

## 9. Scalability & Growth Path

Healthcare organizations can seamlessly transition across plan tiers as their operational footprint expands:

- **Zero-Downtime Upgrades:** Upgrading from Solo Clinic → Essential → PolyClinic → Enterprise requires only administrative configuration updates — zero system reinstallation, zero patient disruption.
- **Specialty Add-ons:** General OPD clinics adding aesthetic or specialty services can activate Derma/Dental/Eye/IVF treatment catalogs as modular extensions.
- **Diagnostic Integration:** Any plan can activate Diagnostream lab report automation as a modular capability, creating a unified hospital + diagnostic front desk under one WhatsApp number.
- **Multi-Specialty Convergence:** Growing hospitals can consolidate OPD departments and specialty treatment catalogs into the unified Multi-Specialty Hospital OS.

---

## 10. Contact & Engagement

| | Details |
|:---|:---|
| **Provider** | Zuko Labs |
| **Platform** | Kriya AI — Healthcare Operations System |
| **Meta Technology Partner ID** | `1602916427428175` |
| **Live Demo** | Available upon request — experience the full WhatsApp patient journey on your phone |
| **Pilot Program** | Deploy Kriya AI in a single department or branch before hospital-wide rollout |

---

*This document constitutes the complete Scope of Services for Kriya AI. All features, capabilities, and plan specifications described herein are production-active and operational as of the document date.*
