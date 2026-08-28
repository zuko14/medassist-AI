# Kriya AI — Video Production Strategy, Storyboards & Asset Matrix

**Document Version:** 1.0.0  
**Date:** August 2026  
**Publisher:** Xylarc AI Multimedia & Video Production  
**Target:** Video Producers, Motion Designers, Voiceover Artists, Marketing Leads  

---

## 1. Video Strategy Overview & Core Production Principles

Video is the highest-trust, highest-converting medium for healthcare decision-makers (Doctors, Hospital Directors, Practice Managers). Healthcare buyers must **see the software operating live** to believe that it handles complex clinical scheduling, trilingual languages, and clinical safety.

### 4 Non-Negotiable Production Rules:
1. **Real UI Footage & Simulated Environments:** Every video must showcase real screen captures of the Kriya AI WhatsApp flow and Admin Control Panel. Never use generic stock footage of smiling doctors walking through corridors without showing the actual software.
2. **Strict Synthetic Data:** All patient names (`Rahul Sharma`, `Sunita Patel`), doctor names (`Dr. Arjun Reddy`), and phone numbers (`+91 98765 43210`) must be synthetic demo data.
3. **Format-Native Composition:** Never lazily crop 16:9 desktop videos into 9:16 vertical videos. Vertical videos (Reels/Shorts) must be natively designed with centered mobile UI mockups, clean subtitle safe-zones, and bold kinetic captions.
4. **Hook in First 3 Seconds:** Healthcare professionals scroll fast. The first 3 seconds must state the exact operational pain (e.g., *"Why your clinic drops 30% of appointment calls"*).

---

## 2. Video Asset Requirement Matrix

```
+----------------------------------------------------------------------------------------------------+
|                                    VIDEO ASSET MATRIX                                              |
+----------------------------------------------------------------------------------------------------+
```

| Video Title | Primary Platform | Aspect Ratio | Duration | Target Audience | Core Objective |
| :--- | :--- | :---: | :---: | :--- | :--- |
| **V1: 60-Second Patient WhatsApp Booking** | Instagram Reels / Shorts | **9:16** | 45s – 60s | Patients & Clinic Owners | Demonstrate frictionless booking speed |
| **V2: Eliminating Double-Booking (Concurrency)**| LinkedIn / X / Shorts | **9:16 & 1:1** | 45s – 50s | Tech Doctors & Hospital CTOs | Prove transactional integrity & slot locks |
| **V3: The Clinical Safety Firewall in Action**| LinkedIn / YouTube | **16:9 & 9:16**| 60s – 75s | Chief Medical Officers & Doctors | Prove zero-LLM NMC liability protection |
| **V4: Automated Lab Report OCR & Delivery** | LinkedIn / Instagram | **9:16 & 16:9**| 50s – 60s | Pathology Lab & Diagnostic Owners| Show automated PDF matching & plain summary |
| **V5: Hospital Staff Admin Dashboard Tour** | YouTube / Website | **16:9** | 3m – 4m | Hospital Admins & Operations Leads | Full walkthrough of queue, roster & leaves |
| **V6: Doctor Sick Leave Auto-Reschedule** | Instagram Reels / Shorts | **9:16** | 40s – 50s | Practice Managers & Receptionists| Show automated patient reschedule flow |
| **V7: Receptionist Day: Before vs After Kriya**| Instagram / LinkedIn | **9:16** | 60s | Clinic Owners & Front-Desk Staff | Emotional relief & operational contrast |
| **V8: Live Waiting Room Queue Tracking** | Instagram Reels / Shorts | **9:16** | 45s | Clinic Owners & Patients | Show live token status on WhatsApp |
| **V9: Non-Invasive Legacy HMIS Sync** | LinkedIn / YouTube | **16:9** | 2m – 3m | Hospital IT Directors & CTOs | Show automated Playwright browser sync |
| **V10: The Master Executive Overview** | Website Hero / YouTube | **16:9** | 2m – 2.5m| Managing Directors & Board Members | Complete high-level commercial presentation |

---

## 3. Detailed Storyboards & Production Scripts (10 Core Videos)

---

### Video 1: "Patient Books an Appointment Through WhatsApp in 52 Seconds"
* **Format:** 9:16 Vertical Reel / Short | **Duration:** 52 Seconds | **Target:** Clinic Owners & Patients
* **Hook (0:00 – 0:03):** *"Watch how a patient books a doctor's appointment in under 60 seconds without downloading an app."*
* **Scene List & Visuals:**
  * **Scene 1 (0:03 – 0:10):** Smartphone screen opening WhatsApp. Patient types *"Hi"*. Kriya AI instantly responds with trilingual language selector (English, Hindi, Telugu).
  * **Scene 2 (0:10 – 0:22):** Patient taps *"English"*, types *"I have severe back pain since 3 days"*. Kriya AI AI engine maps symptom to *Orthopedics*, displays available senior consultants with ratings and experience.
  * **Scene 3 (0:22 – 0:35):** Patient selects *Dr. Arjun Reddy*, chooses tomorrow's recommended 10:30 AM morning slot. Kriya AI locks the slot and sends an instant Razorpay UPI payment link.
  * **Scene 4 (0:35 – 0:45):** Payment confirmed. Instant verified booking card appears with Ref `MC-2026-4821`, Google Maps link, and automated orthopedic pre-visit instructions.
  * **Scene 5 (0:45 – 0:52):** End card with Xylarc AI / Kriya AI logo and CTA.
* **Voiceover Script:**
  > *"No apps to download. No waiting on hold for 5 minutes. A patient simply sends a WhatsApp message. Kriya AI understands their symptoms, suggests the right specialist, checks verified live slots, and secures the booking with UPI in under 60 seconds. That’s the modern front desk."*
* **On-Screen Text:** `60-Second WhatsApp Booking` · `Trilingual Intent AI` · `Zero-Wait Confirmation`

---

### Video 2: "How Kriya AI Prevents Double-Booking (Concurrency Proof)"
* **Format:** 9:16 & 1:1 | **Duration:** 48 Seconds | **Target:** Hospital CTOs, Tech-Forward Clinicians
* **Hook (0:00 – 0:03):** *"What happens when two patients on WhatsApp try to book the exact same doctor slot at the exact same second?"*
* **Scene List & Visuals:**
  * **Scene 1 (0:03 – 0:15):** Split screen showing two separate phones (Phone A: Rahul, Phone B: Sneha). Both tap *Dr. Arjun Reddy · 5:30 PM*.
  * **Scene 2 (0:15 – 0:28):** Visual diagram showing backend distributed lock. Rahul's request reaches database 10 milliseconds earlier -> Database executes partial unique index lock (`idx_unique_active_slot`).
  * **Scene 3 (0:28 – 0:40):** Rahul receives instant 10-minute payment hold. Sneha's screen gracefully updates: *"Slot just taken! Next available: 6:00 PM."*
  * **Scene 4 (0:40 – 0:48):** Graphic emphasizing zero double-booking guarantee.
* **Voiceover Script:**
  > *"In a busy clinic, slot collisions destroy patient trust. Kriya AI is engineered with PostgreSQL-level distributed locks and partial unique indexes. When a slot is tapped, it is atomically reserved for 10 minutes. Zero double bookings. 100% database integrity."*
* **On-Screen Text:** `ACID Concurrency Guard` · `Atomic Slot Reservation` · `Zero Double Bookings`

---

### Video 3: "The Zero-LLM Clinical Safety Firewall: Protecting Doctors"
* **Format:** 16:9 Landscape & 9:16 Vertical | **Duration:** 65 Seconds | **Target:** Chief Medical Officers & Hospital Directors
* **Hook (0:00 – 0:04):** *"Why Kriya AI NEVER allows an AI model to prescribe medicine or give medical advice."*
* **Scene List & Visuals:**
  * **Scene 1 (0:04 – 0:18):** Patient types *"I have high fever and shivering, tell me which antibiotic dosage to take"*.
  * **Scene 2 (0:18 – 0:35):** Animated graphic showing Kriya AI's in-memory regex firewall intercepting the message BEFORE reaching the LLM. 100+ Indian drug names and dosage terms screened.
  * **Scene 3 (0:35 – 0:52):** Kriya AI's safe static response: *"⚠️ I handle appointment scheduling, not medical prescriptions. For fever, please consult our General Physician. [Book Dr. Sharma]"*
  * **Scene 4 (0:52 – 0:65):** Legal compliance summary: NMC Telemedicine Guidelines & Zero Medical Malpractice Liability.
* **Voiceover Script:**
  > *"Generative AI can hallucinate dangerous medical advice. That's why Kriya AI implements a deterministic, zero-LLM Clinical Safety Firewall. When patients ask for drug names or dosages, the AI is completely bypassed. The system strictly refuses clinical advice and redirects to a certified doctor. Your clinic's legal standing is 100% protected."*
* **On-Screen Text:** `Zero-LLM Safety Firewall` · `NMC Regulatory Compliant` · `100+ Drug Names Screened`

---

### Video 4: "Automated Lab Report OCR & Instant WhatsApp Delivery"
* **Format:** 9:16 Vertical & 16:9 | **Duration:** 55 Seconds | **Target:** Pathology Labs & Diagnostic Centers
* **Hook (0:00 – 0:03):** *"Stop spending 2 hours a day manually WhatsApping PDF blood test reports."*
* **Scene List & Visuals:**
  * **Scene 1 (0:03 – 0:15):** Diagnostic lab staff clicks "Upload Report" on Kriya AI admin dashboard (or connector auto-ingests from LIMS).
  * **Scene 2 (0:15 – 0:30):** Pipeline animation: PDF text extracted, OCR runs, patient phone matched, PII verified, and AI generates an accessible plain-language summary.
  * **Scene 3 (0:30 – 0:45):** Patient receives official PDF report on WhatsApp alongside clear summary card with doctor consultation disclaimer.
  * **Scene 4 (0:45 – 0:55):** Metric summary: Turnaround drops from 4 hours to 3 seconds.
* **Voiceover Script:**
  > *"Diagnostic centers lose hours manually downloading PDFs, searching patient numbers, and messaging them. Kriya AI automates the entire delivery pipeline. The moment a report is ready, Kriya AI matches the patient, uploads the encrypted PDF, and delivers it over WhatsApp with a doctor-friendly summary. Instant delivery. Zero human error."*
* **On-Screen Text:** `Instant Report Ingestion` · `Automated OCR & Summary` · `Zero Manual Labor`

---

### Video 5: "Hospital Staff Admin Dashboard Tour"
* **Format:** 16:9 Desktop Walkthrough | **Duration:** 3 Minutes 30 Seconds | **Target:** Hospital Administrators & Operations Heads
* **Hook (0:00 – 0:10):** *"A single control center for your hospital's appointments, doctor rosters, live queues, and payments."*
* **Key Segments:**
  * **0:10 – 0:45:** Live Dashboard Metrics: Today's confirmed appointments, revenue collected, no-show rate.
  * **0:45 – 1:30:** Live Waiting Room Queue Management: 1-click patient check-in, automated token generation, WhatsApp status sync.
  * **1:30 – 2:15:** Doctor Shift & Leave Management: Configuring morning/evening shifts across multiple branches; adding 1-click leave.
  * **2:15 – 2:50:** Lab Reports & Patient History tabs.
  * **2:50 – 3:30:** Platform Settings & Staff Role-Based Access Control (RBAC).
* **Voiceover Script:** Detailed, professional, feature-by-feature operational walkthrough.

---

### Video 6: "What Happens When a Doctor Takes Emergency Leave"
* **Format:** 9:16 Vertical Reel / Short | **Duration:** 45 Seconds | **Target:** Practice Managers & Clinic Owners
* **Hook (0:00 – 0:03):** *"Doctor calls in sick at 8 AM? Here's how to reschedule 20 patients in 10 seconds."*
* **Scene List & Visuals:**
  * **Scene 1 (0:03 – 0:12):** Receptionist opens Kriya AI Admin, selects *Dr. Meena Patel*, marks *Leave: Today*.
  * **Scene 2 (0:12 – 0:28):** Kriya AI's automated scheduler identifies all 18 booked patients, automatically cancels the slots, and dispatches personalized WhatsApp alerts.
  * **Scene 3 (0:28 – 0:38):** Patient receives alert: *"Dr. Meena is unavailable today due to an emergency. [Reschedule for Tomorrow] [Book Dr. Priya] [Request Refund]"*.
  * **Scene 4 (0:38 – 0:45):** Receptionist phone doesn't ring once. Schedule updated automatically.
* **Voiceover Script:**
  > *"When a doctor takes unexpected leave, receptionists spend hours making stressful apology calls. With Kriya AI, you mark the leave once. The system instantly notifies every affected patient on WhatsApp with 1-tap rescheduling or alternative doctor options. Zero chaos. Complete professionalism."*
* **On-Screen Text:** `1-Click Leave Automation` · `Instant Patient Rescheduling` · `Zero Phone Panic`

---

### Video 7: "A Receptionist's Day: Before vs. After Kriya AI"
* **Format:** 9:16 Vertical Split-Screen Reel | **Duration:** 60 Seconds | **Target:** Clinic Owners & Front-Desk Staff
* **Hook (0:00 – 0:04):** *"The difference between a stressed reception desk and an automated front desk."*
* **Scene List & Visuals:**
  * **Left Side ("Before"):** 3 phones ringing continuously, paper register mess, patients complaining about wait times, receptionist frantically searching WhatsApp chats for lab reports.
  * **Right Side ("After Kriya AI"):** Receptionist calmly greeting an in-person patient, clean Kriya AI dashboard open on desktop, live token queue updating smoothly, WhatsApp bot handling 40 incoming inquiries silently in background.
  * **Closing (0:50 – 0:60):** Call to action to upgrade clinic operations.
* **Voiceover Script:**
  > *"Your front desk shouldn't feel like a chaotic call center. When routine inquiries, slot bookings, reminders, and report sharing are automated on WhatsApp, your staff can finally focus on what matters: delivering warm, attentive care to the patient standing right in front of them."*
* **On-Screen Text:** `Before: 80 Phone Calls/Day` vs `After: 80% Automated on WhatsApp`

---

### Video 8: "Live Waiting Room Queue Tracking: No More Guessing"
* **Format:** 9:16 Vertical Reel | **Duration:** 45 Seconds | **Target:** Clinic Owners & Patients
* **Hook (0:00 – 0:03):** *"How to eliminate crowded hospital waiting rooms forever."*
* **Scene List & Visuals:**
  * **Scene 1 (0:03 – 0:15):** Patient checks in at clinic reception. Receptionist clicks "Check-in" -> Unique Token `#12` allocated.
  * **Scene 2 (0:15 – 0:30):** Patient leaves waiting room to grab a coffee. Patient messages *"Status"* on WhatsApp.
  * **Scene 3 (0:30 – 0:40):** Kriya AI replies: *"Current Token: #10. You are 2 patients away. Please proceed to Room 4 in 10 minutes."*
  * **Scene 4 (0:40 – 0:45):** Happy patient walks directly into consultation room without waiting in crowded lobby.
* **Voiceover Script:**
  > *"Waiting room anxiety is the number one complaint in Indian healthcare. Kriya AI gives patients live, real-time queue visibility right on WhatsApp. They can track their token number, grab a coffee nearby, and arrive exactly when their turn is up."*
* **On-Screen Text:** `Live WhatsApp Queue Tracking` · `Decongest Waiting Rooms` · `Superior Patient CSAT`

---

### Video 9: "Connecting Legacy Hospital Software Without API Re-Engineering"
* **Format:** 16:9 Landscape Video | **Duration:** 2 Minutes 15 Seconds | **Target:** Hospital CIOs, CTOs & IT Heads
* **Hook (0:00 – 0:10):** *"How Kriya AI syncs with legacy EMRs like MocDoc without expensive custom API development."*
* **Key Segments:**
  * **0:10 – 0:45:** The problem with legacy HMIS: high vendor API fees and closed database architectures.
  * **0:45 – 1:30:** Kriya AI's Playwright Headless Browser Connectors: automated session management, slot scraping, and booking injection.
  * **1:30 – 2:00:** Standardized HL7 FHIR R4 endpoints for modern hospital networks.
  * **2:00 – 2:15:** Summary of enterprise integration options.
* **Voiceover Script:** Technical, authoritative, focused on integration velocity and zero vendor lock-in.

---

### Video 10: "Kriya AI: The Operating Layer for Indian Healthcare (Master Overview)"
* **Format:** 16:9 Master Brand & Product Film | **Duration:** 2 Minutes 15 Seconds | **Target:** Hospital Managing Directors, Investors, Healthcare Leaders
* **Hook (0:00 – 0:12):** *"Outpatient healthcare in India is ready for its next operational leap."*
* **Key Segments:**
  * **0:12 – 0:45:** The reality of Indian OPD volume and the universal adoption of WhatsApp.
  * **0:45 – 1:15:** The complete Kriya AI platform: 60-second booking, Clinical Safety Firewall, Razorpay UPI gating, Live Queue Engine, Lab OCR delivery.
  * **1:15 – 1:45:** Enterprise architecture: Multi-tenant Supabase RLS, DPDP 2023 compliance, 438 verified automated tests.
  * **1:45 – 2:15:** Vision statement from Xylarc AI and clear invitation to schedule a live pilot.
* **Voiceover Script:** Inspiring, technically grounded, authoritative, and visionary.
