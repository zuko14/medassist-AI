# Kriya AI — Social Media UI Design System & Reusable Template Specifications

**Document Version:** 1.0.0  
**Date:** August 2026  
**Publisher:** Xylarc AI Design System & Creative Operations  
**Target:** Visual Designers, UI Marketers, Social Media Creators  

---

## 1. Design System Overview & Global Layout Rules

The Kriya AI Social Design System ensures that every promotional post, educational carousel, and video thumbnail communicates enterprise engineering credibility, clinical precision, and visual consistency with the parent **Xylarc AI design language**.

```
+----------------------------------------------------------------------------------------------------+
|                                    GLOBAL LAYOUT PARAMETERS                                        |
+----------------------------------------------------------------------------------------------------+
|  Base Canvas Theme      | Obsidian Dark `#05070b` with Glassmorphism Panels `rgba(13, 19, 28, 0.75)`|
|  Global Padding         | 64 px on 1080x1080 / 1080x1350 canvases (Minimum 80 px safe margins)      |
|  Border Radius Tokens   | Cards: `16px` | Buttons: `8px` | Badges: `4px` | Modals: `20px`             |
|  Border Stroke Token    | `1px solid rgba(255, 255, 255, 0.08)` (Subtle crisp division)            |
|  Glow & Lighting Accents| Subtle emerald radial gradient `rgba(84, 213, 154, 0.12)` at top-right;    |
|                         | subtle blue radial gradient `rgba(22, 140, 255, 0.08)` at bottom-left.   |
+----------------------------------------------------------------------------------------------------+
```

---

## 2. The 12 Reusable Social Template Specifications

```
+----------------------------------------------------------------------------------------------------+
|                                   SOCIAL TEMPLATE SPECIFICATIONS                                   |
+----------------------------------------------------------------------------------------------------+
```

### Template 1: Feature Announcement Card
* **Primary Formats:** `1080 × 1080 px` (1:1 Square) & `1080 × 1350 px` (4:5 Portrait)
* **Safe Zone:** `80 px` inner margin on all four sides.
* **Layout Structure:**
  * **Top Bar (Y: 80px):** Left = Category Eyebrow Badge (e.g., `NEW CAPABILITY // LIVE QUEUE ENGINE`); Right = Kriya AI Symbol + Wordmark (Height: `24px`).
  * **Headline Area (Y: 160px):** Display Font (Outfit 600, `44px`), Line-height `1.15`, Color `#ffffff`. Example: *"Real-Time Waiting Room Queue Tracking on WhatsApp."*
  * **Central Visual Mockup (Y: 280px to 880px):** Centered floating glass container with 3D elevation shadow (`0 20px 40px rgba(0,0,0,0.6)`), showcasing high-definition WhatsApp message chat UI with token counter.
  * **Bottom Bar (Y: 960px):** Left = 3 Sub-feature pills (`Sequential Token RPC`, `WhatsApp Live Status`, `Zero Staff Intervention`); Right = Primary CTA Button (`Book Live Demo →`).

---

### Template 2: Product Tip / Operational Best Practice
* **Primary Formats:** `1080 × 1080 px` & `1080 × 1350 px`
* **Layout Structure:**
  * **Header:** Pill Tag: `CLINIC EFFICIENCY TIP #04`.
  * **Problem Hook:** *"How to eliminate morning appointment no-shows without hiring extra staff."*
  * **Body Graphic:** 3 numbered step cards arranged vertically inside a structured glass panel:
    1. `Step 1`: Enable automated 24-hour WhatsApp reminder at 9:00 AM.
    2. `Step 2`: Offer 1-tap `[Confirm]` or `[Reschedule]` interactive buttons.
    3. `Step 3`: Automatically release unconfirmed slots 3 hours prior to consultation.
  * **Footer:** Small text: *"Engineered into Kriya AI's automated scheduler."*

---

### Template 3: Customer Story / Before & After Case Teardown
* **Primary Formats:** `1080 × 1350 px` (Multi-Slide Carousel Cover & Summary)
* **Layout Structure:**
  * **Top Section:** Clinic Identity Badge (e.g., `CASE STUDY // 5-DOCTOR POLYCLINIC, HYDERABAD`).
  * **Main Headline:** *"How City Care Polyclinic Cut Phone Call Volume by 72% in 30 Days."*
  * **Dual Metric Cards (Side-by-Side):**
    * **Left Card (Red Tone):** `BEFORE KRIYA` -> `85 Phone Calls/Day` · `28% No-Show Rate` · `3h Daily Manual WhatsApp Labor`.
    * **Right Card (Emerald Green Tone):** `WITH KRIYA AI` -> `18 Phone Calls/Day` · `6% No-Show Rate` · `0 Manual Report Dispatches`.
  * **Footer Quote:** 1-sentence endorsement from Managing Doctor with verified avatar and clinic location.

---

### Template 4: Security & Compliance Insight Card
* **Primary Formats:** `1080 × 1080 px`
* **Layout Structure:**
  * **Top Tag:** `HEALTHCARE DATA GOVERNANCE // DPDP ACT 2023`.
  * **Core Concept Title:** *"Why Multi-Tenant Healthcare Bots Must Enforce Database Row-Level Security (RLS)."*
  * **Central Visual:** Technical architecture diagram illustrating tenant isolation: Tenant A (PostgreSQL Schema Policy) is mathematically isolated from Tenant B.
  * **Bottom Proof Points:** `Strict DPDP Consent` · `Zero Data Bleed` · `NMC 7-Year Audit Trail`.

---

### Template 5: Healthcare Industry Statistic Card
* **Primary Formats:** `1080 × 1080 px`
* **Layout Structure:**
  * **Giant Stat Display:** `35%` set in Outfit Bold `110px`, colored with Emerald-to-Cyan gradient.
  * **Sub-Headline:** *"of Indian OPD patients report abandoning appointment booking due to busy front-desk phone lines."*
  * **Source Citation:** Set in Geist Mono `14px`, `#b8c1cf`: `Source: Indian Private Healthcare Operational Survey 2025–2026`.
  * **Bottom Solution Line:** *"Kriya AI responds in under 1 second on WhatsApp, 24 hours a day."*

---

### Template 6: Before vs. After Workflow Diagram
* **Primary Formats:** `1080 × 1350 px`
* **Layout Structure:**
  * **Header:** `WORKFLOW COMPARISON // PATIENT CONSULTATION JOURNEY`.
  * **Left Column ("The Traditional Way"):** 5 red-tinted steps (Call clinic -> Busy tone -> Call again -> Verbal slot agreement -> No reminder -> Patient forgets).
  * **Right Column ("The Kriya AI Way"):** 5 emerald-tinted steps (WhatsApp text -> 60s slot booking -> UPI confirmation -> 24h/2h reminders -> Live queue token on phone).
  * **Bottom Summary:** *"Save 4 hours of staff labor every single day."*

---

### Template 7: System Architecture & Workflow Diagram
* **Primary Formats:** `1080 × 1080 px` & `16:9` (1920 × 1080 px for LinkedIn/Web)
* **Layout Structure:**
  * **Header:** `KRIYA AI SYSTEM ARCHITECTURE // CLINICAL SAFETY FIREWALL`.
  * **Diagram Canvas:** Clean, high-contrast dark-mode flowchart:
    * `WhatsApp Patient Inbound` -> `HMAC SHA-256 Webhook Verification` -> `Deterministic Regex Firewall (Zero-LLM)` -> `Groq Intent Classifier` -> `PostgreSQL RLS Engine` -> `Live Response`.
  * **Engineering Attribution:** `FastAPI 0.115+ · Supabase PostgreSQL · Meta Cloud API v21.0`.

---

### Template 8: Executive / Doctor Quote Card
* **Primary Formats:** `1080 × 1080 px`
* **Layout Structure:**
  * **Top Left:** Large stylized typographic quote mark `“` in translucent emerald (`rgba(84, 213, 154, 0.2)`).
  * **Quote Text:** Display text (Outfit 500, `32px`), `#ffffff`: *"In a busy clinic, you cannot afford to have your receptionist acting like a telephone switchboard operator while in-person patients are waiting."*
  * **Author Block:** Circular profile photo with emerald border (`48px`), Doctor Name (Bold `18px`), Specialization and Hospital Location (`14px` Muted Grey).

---

### Template 9: New Integration / EMR Connector Announcement
* **Primary Formats:** `1080 × 1080 px`
* **Layout Structure:**
  * **Header Tag:** `NEW INTEGRATION // LEGACY HMIS CONNECTORS`.
  * **Visual Center:** Two interconnected brand badges: `Kriya AI Logo` <--- `Bidirectional Sync` ---> `MocDoc / CallMedex EMR Logo`.
  * **Core Value Statement:** *"Sync your hospital's existing doctor roster, leaves, and appointments directly with WhatsApp—zero API re-engineering required."*

---

### Template 10: Product Update & Changelog Card
* **Primary Formats:** `1080 × 1080 px`
* **Layout Structure:**
  * **Header:** `PRODUCT RELEASE // KRIYA AI v2.0`.
  * **Changelog List (3 Bullet Cards):**
    * `+` **Family Member Booking:** Support for multiple dependents under one phone number.
    * `+` **Dynamic Doctor Shifts:** Multi-branch morning/evening session scheduling.
    * `+` **Razorpay Idempotent Webhooks:** Automated 10-minute temporary slot reservation.
  * **Footer:** `438 Automated Tests Passing` · `Live for All Clinic Tenants`.

---

### Template 11: Interactive Demo Invitation Card
* **Primary Formats:** `1080 × 1080 px` & `1080 × 1920 px` (Instagram Story / Reel Card)
* **Layout Structure:**
  * **Headline:** *"Test the Hospital Front Desk of 2026 on Your Own Phone."*
  * **Central QR Code Container:** High-contrast white QR code box with embedded Kriya AI logo in center, linking to `https://wa.me/your-demo-number?text=Hi`.
  * **Instructions:** *"Scan the QR code or send 'Hi' on WhatsApp to experience trilingual appointment booking, UPI payment gating, and live queue tracking in 60 seconds."*
  * **Bottom Assurance:** `Free Interactive Demo · No App Required`.

---

### Template 12: Myth vs. Fact in Healthcare AI
* **Primary Formats:** `1080 × 1080 px`
* **Layout Structure:**
  * **Header Tag:** `HEALTHCARE TECH FACTS // CLINICAL AI BOUNDARIES`.
  * **Top Card (Red Tone):** `MYTH:` *"AI chatbots in hospitals will give wrong medical diagnoses and create doctor malpractice liability."*
  * **Bottom Card (Emerald Tone):** `FACT:` *"Kriya AI uses a deterministic Zero-LLM Clinical Safety Firewall that completely blocks medical advice and only automates administrative scheduling and report delivery."*
  * **Bottom Trust Stamp:** `100% NMC Telemedicine Guidelines Compliant`.
