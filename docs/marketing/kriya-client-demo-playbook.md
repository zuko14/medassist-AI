# Kriya AI — Client Demonstration Playbook & Technical Script

**Document Version:** 1.0.0  
**Date:** August 2026  
**Publisher:** Xylarc AI Solutions Engineering & Sales Enablement  
**Target:** Solution Architects, Sales Engineers, Account Executives  

---

## 1. Demo Objectives & Non-Negotiable Ground Rules

The objective of a Kriya AI demonstration is to deliver an **undeniable, interactive proof-of-value** to healthcare decision-makers in 15 minutes or less.

```
+----------------------------------------------------------------------------------------------------+
|                                    DEMO SAFETY GROUND RULES                                        |
+----------------------------------------------------------------------------------------------------+
|  1. Interactive Participation First                                                                |
|     - Never deliver a one-way slide monologue. Have the doctor/administrator scan the demo QR code|
|       or message the demo WhatsApp number directly from their personal phone.                      |
+----------------------------------------------------------------------------------------------------+
|  2. Strict Synthetic Sandbox Data                                                                 |
|     - Always run demos on the isolated staging tenant: `City Care Hospital (Tenant ID: 001)`.     |
|     - Pre-configured synthetic doctors: `Dr. Arjun Reddy (Cardiology)`, `Dr. Priya Sharma (GP)`.  |
|     - Never use real patient phone numbers or clinical data during sales demonstrations.          |
+----------------------------------------------------------------------------------------------------+
|  3. Zero Hypothetical Features Rule                                                                |
|     - Only demonstrate verified, live capabilities present in the codebase. Never promise or fake |
|       unimplemented third-party integrations.                                                      |
+----------------------------------------------------------------------------------------------------+
|  4. Graceful Fallback Protocol                                                                     |
|     - In case of local Wi-Fi / carrier WhatsApp delays, have the secondary iPad hotspot active and |
|       the local web admin dashboard pre-authenticated.                                             |
+----------------------------------------------------------------------------------------------------+
```

---

## 2. Pre-Demo Environment Checklist

Before stepping into a client meeting or launching a Zoom demo screen-share, verify the following:

- [ ] **Meta Cloud API Webhook:** Verified green on staging server (`GET /health/ready` returns `status: ok`).
- [ ] **Demo WhatsApp Number:** Verified active, profile photo set to Kriya AI / City Care Hospital.
- [ ] **Synthetic Doctor Schedule:** Doctor shifts configured for today (Morning: 9:00 AM – 1:00 PM; Evening: 5:00 PM – 8:00 PM).
- [ ] **Razorpay Sandbox:** Test mode enabled for UPI payment link generation (auto-approves in sandbox).
- [ ] **Admin Dashboard Window:** Browser tab open to `admin/index.html` logged in as `admin / CityCare2026!`.
- [ ] **Demo QR Code Card:** High-contrast printed QR card or tablet display linking to `https://wa.me/<demo_phone>?text=Hi`.

---

## 3. The 15-Minute Minute-by-Minute Demonstration Script

```
+----------------------------------------------------------------------------------------------------+
|                                15-MINUTE DEMONSTRATION SCRIPT                                      |
+----------------------------------------------------------------------------------------------------+
```

### Minute 0:00 – 0:02 | Opening & Context Setting
* **Presenter Action:** Greet the prospect; state the concise goal of the session.
* **Talk Track:**
  > *"Dr. [Name] / Mr. [Administrator], thank you for meeting today. We know your front desk handles dozens of patient calls every morning for appointments, doctor timings, and lab reports. Today, I want to show you how Kriya AI turns WhatsApp into an autonomous, 24/7 front desk that handles these requests in 60 seconds with zero staff effort. Let's test this directly on your own phone."*

### Minute 0:02 – 0:05 | Patient Journey: Interactive WhatsApp Booking
* **Presenter Action:** Hand the prospect the QR card. Ask them to scan it and send *"Hi"*.
* **Demonstrated Flow:**
  1. Instant greeting with language picker (`[English]`, `[हिंदी]`, `[తెలుగు]`).
  2. Prospect selects *English*, types *"I want to see a cardiologist tomorrow"*.
  3. Kriya AI recognizes intent, suggests *Dr. Arjun Reddy (MD Cardiology · 14 yrs exp · ⭐4.8)*.
  4. System presents interactive list of verified available slots (Morning & Evening).
* **Talk Track:**
  > *"Notice that the system did not ask for your name or make you fill out a long form first. It answered your intent immediately. It checked Dr. Arjun's actual schedule, skipped his leaves, and gave you real-time available slots."*

### Minute 0:05 – 0:08 | Payment Gating & Anti-Double-Booking Lock
* **Presenter Action:** Ask prospect to select the *10:30 AM* slot.
* **Demonstrated Flow:**
  1. System generates an instant booking reservation and dispatches a Razorpay UPI payment link.
  2. System locks the slot for 10 minutes in the database.
  3. In test mode, prospect completes payment -> Instant booking confirmation card appears with Reference `MC-2026-4821` and Google Maps directions.
  4. Automated department-specific preparation guidelines arrive 2 seconds later (*"Avoid heavy meals 2 hours before cardiac consultation"*).
* **Talk Track:**
  > *"The moment you clicked 10:30 AM, our database locked that slot using distributed locks. If another patient tried to book 10:30 AM at the exact same second, they would be told the slot was just taken. This completely eliminates double-booking chaos."*

### Minute 0:08 – 0:10 | Clinical Safety Firewall: Live Malpractice Intercept
* **Presenter Action:** Ask prospect to type a medical prescription request into the chat: *"Give me medicine for fever and throat pain"*.
* **Demonstrated Flow:**
  1. The deterministic Clinical Safety Firewall intercepts the message in memory (Zero LLM inference).
  2. Instant response: *"⚠️ I handle appointment scheduling, not medical prescriptions. For fever, please consult our General Physician Dr. Priya Sharma. [Book Appointment]"*.
* **Talk Track:**
  > *"This is our Zero-LLM Clinical Safety Firewall. It screens over 100 Indian drug names and dosage requests. It will NEVER allow an AI to hallucinate a prescription. Your hospital is 100% legally protected under NMC telemedicine guidelines."*

### Minute 0:10 – 0:12 | Hospital Admin Dashboard & Live Queue Check-In
* **Presenter Action:** Switch screen to `admin/index.html` on laptop/tablet.
* **Demonstrated Flow:**
  1. Show the appointment just booked by the prospect appearing in real-time on the dashboard.
  2. Click **"Check-In"** -> System allocates Queue **Token #12**.
  3. Ask prospect to check their WhatsApp: A notification has arrived with their live token number.
  4. Ask prospect to message *"Queue status"* -> System replies: *"Currently serving: #10. You are 2 patients away."*
* **Talk Track:**
  > *"When the patient arrives at your clinic, reception clicks one button to check them in. The patient tracks their live waiting token directly on WhatsApp, decongesting your waiting room lobby."*

### Minute 0:12 – 0:14 | Doctor Leave Automation & Lab Report Dispatch
* **Presenter Action:** Demonstrate emergency doctor leave handling.
* **Demonstrated Flow:**
  1. Click **"Add Doctor Leave"** for Dr. Arjun today.
  2. Show system automatically flagging all affected appointments and dispatching personalized WhatsApp rescheduling alerts with 1-tap options.
  3. (Optional for Diagnostic Labs): Show automated PDF report upload and instant WhatsApp dispatch with patient plain summary.
* **Talk Track:**
  > *"When a doctor takes unexpected leave, your receptionist doesn't have to make 20 apology calls. Kriya AI reschedules every patient automatically on WhatsApp in 10 seconds."*

### Minute 0:14 – 0:15 | Commercial ROI & 30-Day Pilot Close
* **Presenter Action:** Present the simple pricing and propose the 30-day onboarding pilot.
* **Talk Track:**
  > *"Kriya AI runs on a flat subscription starting at ₹2,499 to ₹5,999/month. We can connect your clinic's WhatsApp number and configure your doctors in 48 hours without changing your current software. Let's launch a 30-day pilot this Monday."*

---

## 4. How to Handle Demo Edge Cases & Technical Glitches

| Glitch / Scenario | Immediate Recovery Protocol |
| :--- | :--- |
| **Meta Cloud API Message Delay (> 5s)** | State calmly: *"Meta occasionally queues messages during peak hours. Notice that the transaction is already logged in our real-time database dashboard here on screen."* |
| **Prospect Types Unexpected Dialect / Slang** | Highlight resilience: If NLP fallback triggers, point out how the 3-strike rule gracefully presents the main interactive menu or offers human staff escalation. |
| **Prospect Asks for Custom HMIS Integration** | Explain clearly: *"We support direct REST FHIR R4 APIs and our automated Playwright browser connectors that sync with systems like MocDoc without extra API fees."* |
