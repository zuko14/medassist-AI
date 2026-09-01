# KRIYA AI — PLAN CAPABILITY & FEATURE MATRIX

**Document Type:** Commercial Service Tier Specification & Capability Matrix  
**Target Audience:** Enterprise Clients, Healthcare Networks, Hospital Leadership, Procurement Committees  
**Pricing Policy:** Strictly Capability, Workflow Depth, and Scale-Based (Zero Monetary Pricing Listed)  

---

## 1. Overview of Kriya AI Plan Tiers

Kriya AI delivers purpose-built operational configurations tailored to distinct healthcare delivery models:

1. **Solo Clinic:** Tailored for individual medical practitioners and single-doctor private clinics seeking 24/7 automated patient reception, direct scheduling, and upfront fee collection without administrative staff overhead.
2. **Essential:** The core healthcare automation package for standard outpatient clinics and community healthcare centers requiring end-to-end appointment scheduling, automated patient reminders, UPI payment collection, and DPDP consent compliance.
3. **PolyClinic:** Engineered for multi-specialty clinical facilities with multiple doctors, departments, dynamic shift rosters, family dependent bookings, and live OPD waiting room queue tokens.
4. **Diagnostream:** A specialized operational pipeline designed for pathology laboratories and diagnostic imaging networks to automate EMR/LIMS report scraping, patient identity verification, OCR extraction, AI clinical summarization, and direct WhatsApp PDF delivery.
5. **Enterprise:** The comprehensive platform tier for enterprise hospital chains, multi-branch healthcare networks, and medical centers requiring centralized administration, branch-level doctor partitioning, granular role-based access control (RBAC), HL7 FHIR interoperability, and custom EMR connector bridges.

---

## 2. Comprehensive Plan Feature Matrix

| Functional Dimension / Feature | Solo Clinic | Essential | PolyClinic | Diagnostream | Enterprise |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Target Operational Environment** | Single Practitioner | Standard Clinic | Multi-Specialty Center | Diagnostic Lab Network | Hospital Chain / Multi-Branch |
| **WhatsApp Conversational Channel** | Single Clinic WABA | Single Clinic WABA | Single Clinic WABA | Diagnostic WABA | Multi-Number / Multi-WABA |
| **Multilingual Support (EN / HI / TE)** | Included | Included | Included | Included | Included + Custom Locales |
| **Clinical Safety Firewall (Zero-LLM)** | Included | Included | Included | Included | Included |
| **Symptom-to-Department AI Triage** | Direct Doctor Routing | Core AI Triage | Multi-Specialty AI | Test Catalog Matching | Advanced Multi-Dept Triage |
| **Dynamic Slot Anti-Collision Engine** | Single Doctor | Multi-Doctor Basic | Full Dynamic Shifts | Sample Collection Slots | Multi-Branch Dynamic Rosters |
| **Doctor Leave & Holiday Management** | Included | Included | Included | N/A | Centralized Multi-Branch |
| **Doctor Capacity Limit** | 1 Doctor | Up to 3 Doctors | Up to 25 Doctors | N/A | Unlimited Doctors |
| **Branch / Location Support** | 1 Location | 1 Location | Up to 3 Locations | Unlimited Lab Centers | Unlimited Hospital Branches |
| **Razorpay Payment Integration** | Optional / Included | Full Payment Links | Full Payment Links | Test Fee Pre-Collection | Multi-Account Razorpay Route |
| **Automated Reminders (24h & 2h)** | Included | Included | Included | Test Prep Reminders | Custom Multi-Trigger Cron |
| **Live OPD Queue Tokens (e.g. Q-012)** | Optional | Included | Real-Time Live Queue | Sample Phlebotomy Queue | Multi-Dept Live Queue Engine |
| **Family Dependent Booking** | Included | Included | Included | Included | Included |
| **Diagnostream EMR/LIMS Ingestion** | Not Included | Not Included | Optional Add-on | Core Automation | Core Enterprise Automation |
| **Tesseract OCR & Parameter Extraction** | Not Included | Not Included | Optional Add-on | Included | Included |
| **Patient Match Fuzzy Safety Gate** | Not Included | Not Included | Optional Add-on | Included | Included |
| **PII-Sanitized AI Report Summary** | Not Included | Not Included | Optional Add-on | Included | Included |
| **Doctor Abnormal Flag Notifications** | Not Included | Not Included | Optional Add-on | Included | Included |
| **DPDP Act 2023 Consent & Retention** | Included | Included | Included | Included | Full Enterprise Tiered Audit |
| **Admin Portal & Dashboard Access** | Doctor View | Staff Admin | Multi-Role Admin | Lab Ops Console | Super-Admin + Branch RBAC |
| **Analytics & Operational Insights** | Basic Stats | Weekly Summary | Real-Time OPD Metrics | Turnaround Time Metrics | Enterprise Executive BI |
| **MocDoc / CallMedex Playwright Bridge** | Not Included | Not Included | Optional Add-on | Included | Custom EMR / LIMS Adapters |
| **HL7 FHIR R4 & ABDM Gateway** | Not Included | Not Included | Not Included | Not Included | Full Interoperability Suite |
| **Data Isolation & Security** | Tenant-Scoped | Tenant-Scoped | PostgreSQL RLS | Dedicated Lab Scoping | Custom Dedicated DB / RLS |
| **Deployment Model** | Cloud Multi-Tenant | Cloud Multi-Tenant | Cloud Multi-Tenant | Cloud / Hybrid Connector | Cloud / Dedicated VPC / Hybrid |
| **Service Level Agreement (SLA)** | Standard | Standard Business | Priority Business | Mission-Critical Lab | 99.9% Enterprise Dedicated |

---

## 3. Detailed Breakdown of Specialized Configurations

### 3.1 Solo Clinic Configuration
* **Operational Scope:** Configured specifically for solo consultants, dental practices, pediatricians, and private consulting rooms.
* **Core Workflow:**
  * Inbound WhatsApp queries greet patients in their preferred language (English, Hindi, Telugu).
  * Direct appointment scheduling against the doctor's weekly consultation timetable.
  * Instant map directions, pre-visit instructions, and UPI consultation deposit collection.
  * Instant notification to the doctor upon confirmed booking.

### 3.2 Essential Configuration
* **Operational Scope:** Designed for small clinics, general medicine centers, and primary healthcare centers with 1 to 3 consulting doctors.
* **Core Workflow:**
  * Patient intent detection maps inquiries to general medicine or visiting doctors.
  * Automated 24-hour and 2-hour appointment reminders sent to patients' WhatsApp.
  * Rescheduling and cancellation self-service workflows with automated slot release.
  * Receptionist dashboard for managing walk-ins, daily appointment logs, and patient communication.

### 3.3 PolyClinic Configuration
* **Operational Scope:** Multi-specialty medical centers housing diverse clinical departments (e.g., Cardiology, ENT, Gynecology, Orthopedics, Pediatrics, Dermatology).
* **Core Workflow:**
  * Symptom-based AI routing directs patients to the appropriate clinical department.
  * Dynamic doctor slot engine calculates complex multi-shift timings, room allocations, and individual doctor consultation fees.
  * Live digital queue tokens generated at booking, allowing patients in the waiting area to query live queue status on WhatsApp.
  * Family member profile management allowing parents/guardians to book for dependents under a single phone number.

### 3.4 Diagnostream Configuration
* **Operational Scope:** Standalone diagnostic centers, pathology lab chains, and radiology imaging networks.
* **Core Workflow:**
  * Playwright connector daemon polls laboratory EMR/LIMS systems (such as MocDoc) at configurable intervals.
  * Automated patient identity verification matching patient name and phone against lab records with fuzzy similarity scoring.
  * PDF extraction, local secure storage, and Tesseract OCR parsing of clinical test parameters.
  * PII-sanitized LLM summarization generating clear, plain-language patient summaries with abnormal value alerts.
  * Secure WhatsApp delivery of authorized PDF lab reports with doctor consultation call-to-action.

### 3.5 Enterprise Configuration
* **Operational Scope:** Large multispecialty hospitals, hospital chains, and regional healthcare networks.
* **Core Workflow:**
  * Multi-branch patient routing allowing patients to select preferred hospital campuses and view branch-specific doctors.
  * Multi-tiered administrative portal supporting Platform Super-Admins, Hospital Admins, Branch Managers, Doctors, and Receptionists.
  * Complete DPDP Act 2023 compliance automation (structured consent records, 30-day conversation transcript purges, and 7-year medical audit retention).
  * HL7 FHIR R4 REST API integration and custom enterprise HMIS connector development.
  * Comprehensive business intelligence dashboards tracking department revenue, doctor utilization, no-show rates, and patient feedback.

---

## 4. Scalability & Transition Path

Healthcare organizations can seamlessly transition across plan tiers as their operational footprint expands:
* **Zero Disruption Migration:** Upgrading from Essential to PolyClinic or Enterprise requires simple administrative configuration updates in the database—with zero downtime and zero re-installation for existing patients.
* **Modular Add-ons:** Facilities on PolyClinic can activate Diagnostream lab report automation as a modular capability, creating a unified hospital and diagnostic front desk under one WhatsApp number.
