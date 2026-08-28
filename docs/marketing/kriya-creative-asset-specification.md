# Kriya AI — Master Creative Asset Specification & Production Matrix

**Document Version:** 1.0.0  
**Date:** August 2026  
**Publisher:** Xylarc AI Creative Direction & Marketing Production  
**Target:** Visual Designers, UI/UX Marketers, Motion Graphic Artists, Frontend Engineers  

---

## 1. Master Creative Asset Specification Matrix

```
+----------------------------------------------------------------------------------------------------+
|                               MASTER CREATIVE ASSET SPECIFICATION                                  |
+----------------------------------------------------------------------------------------------------+
```

| Asset Name | Category | Ratio | Dimensions | Safe Zone | Primary Purpose | Required CTA / Content |
| :--- | :--- | :---: | :---: | :---: | :--- | :--- |
| **Brand Master Icon (1:1)** | Brand Identity | **1:1** | `1024 × 1024 px` | 820px Central Circle | Master square profile asset for all social profiles | Geometric Kriya Node symbol on `#05070b` |
| **Favicon Bundle** | Web / App Icon | **1:1** | `16×16`, `32×32`, `48×48`, SVG | 2px padding | Browser tab icon, PWA shortcut | Scalable Kriya vector node |
| **Website Hero 3D Composite** | Product Marketing | **16:9** | `2880 × 1620 px` | 200px outer buffer | Main website landing page hero visual | Floating WhatsApp Chat + Desktop Admin View |
| **Admin Dashboard UI Render** | Product Marketing | **16:9** | `1920 × 1080 px` | 100px padding | Showcasing live queue, doctor roster, and analytics | `admin/index.html` simulated high-res UI |
| **WhatsApp Chat UI Mockup** | Product Marketing | **9:16** | `1080 × 1920 px` | 120px top/bottom | In-app mobile screen showing trilingual booking flow | Real message bubbles with interactive buttons |
| **LinkedIn Master Carousel** | Social Collateral| **4:5** | `1080 × 1350 px` | 80px all sides | B2B thought leadership & operational case studies | *"Swipe ->"* / *"Book Live Demo ->"* |
| **Instagram Feed Carousel** | Social Collateral| **1:1 / 4:5**| `1080 × 1080 / 1350 px`| 80px all sides | Educational patient journey & clinic tips | *"Link in bio to try live demo"* |
| **YouTube Video Thumbnail System**| Social Video | **16:9** | `1920 × 1080 px` | 100px outer buffer | High-CTR thumbnails for walkthroughs and explainers | Bold 3-word title + High-contrast UI card |
| **Short-Form Video (Reels/Shorts)**| Motion Video | **9:16** | `1080 × 1920 px` | Top 220px, Bottom 320px | Fast 45s–60s product demos and problem teardowns | Kinetic captions + Centered UI demo |
| **Architecture / Firewall Visual**| Technical Sales | **16:9** | `1920 × 1080 px` | 100px padding | Security overview, DPDP compliance & sales deck | Flowchart: Webhook -> Regex Firewall -> RLS DB |

---

## 2. Detailed Technical Specifications by Asset Category

```
+----------------------------------------------------------------------------------------------------+
|                               2.1 PRODUCT MARKETING HERO RENDERS                                   |
+----------------------------------------------------------------------------------------------------+
```

### Asset: Website Hero Composite Visual (`hero-kriya-composite.png`)
* **Canvas Size:** `2880 × 1620 px` (High DPI @ 2x for Retina screens).
* **Composition Structure:**
  * **Background Layer:** Obsidian space `#05070b` with a subtle top-right emerald glow (`rgba(84, 213, 154, 0.14)`) and bottom-left blue accent (`rgba(22, 140, 255, 0.08)`).
  * **Left Foreground Element (Mobile WhatsApp View):** High-precision smartphone mockup tilted at a `12-degree` isometric angle, displaying a live WhatsApp chat conversation:
    * `Patient:` *"I need a cardiologist consultation tomorrow"*
    * `Kriya AI:` *"👨‍⚕️ Dr. Arjun Reddy (MD Cardiology · 14 yrs exp) is available tomorrow at 10:30 AM. [Confirm Slot]"*
  * **Right Background Element (Desktop Admin View):** Translucent glassmorphism desktop window displaying `admin/index.html` with live waiting room tokens (`Token #14`, `Token #15`), doctor availability toggles, and revenue metrics.
  * **Connecting Element:** Glowing cyan data stream illustrating real-time synchronization between the patient's phone and the hospital's admin dashboard.

---

```
+----------------------------------------------------------------------------------------------------+
|                             2.2 SOCIAL MEDIA CAROUSEL SPECIFICATIONS                                |
+----------------------------------------------------------------------------------------------------+
```

### Asset: LinkedIn & Instagram B2B Carousel Slide Deck (1080 × 1350 px)
* **Canvas Size:** `1080 × 1350 px` (4:5 Aspect Ratio — maximizes mobile feed real estate).
* **Slide Count:** 5 to 7 slides per carousel.
* **Standard Slide Architecture:**
  * **Slide 1 (Cover Slide):**
    * Top Tag: Category Pill Badge (`OPERATIONAL TEARDOWN`).
    * Main Title: Display Font (Outfit Bold `48px`, `#ffffff`), 4-line maximum.
    * Subtitle: Slate Light (`20px`), `#e6edf8`.
    * Visual Anchor: 3D illustrated card or high-contrast UI snippet.
    * Bottom Indicator: *"Swipe for Solution →"*.
  * **Slides 2–5 (Content & Proof Slides):**
    * Top Left: Slide Number Pill (e.g., `02 // THE PROBLEM`).
    * Slide Header: Semi-bold `32px`.
    * Central Content: Structured comparison cards, metrics, or flowchart diagrams.
    * Bottom Bar: Small branding line (`Kriya AI — Hospital Operating System`).
  * **Slide 6 / Final (Call-to-Action Slide):**
    * Main Title: *"Ready to Automate Your Clinic's Front Desk?"*
    * 3 Key Takeaways summary pills.
    * High-contrast Button: `[ Schedule a 15-Minute Live Demo ]`.
    * QR Code & Website URL (`xylarc.ai/products/kriya-ai`).

---

```
+----------------------------------------------------------------------------------------------------+
|                             2.3 YOUTUBE THUMBNAIL DESIGN SYSTEM                                    |
+----------------------------------------------------------------------------------------------------+
```

### Asset: High-CTR YouTube Thumbnail System (1920 × 1080 px)
* **Canvas Size:** `1920 × 1080 px` (16:9 Aspect Ratio).
* **Safe Zone:** Keep all text and critical elements `100 px` away from the right edge to avoid being covered by the video duration stamp.
* **Typography Rule:** Maximum **3 to 4 bold words** set in Ultra-Bold Display Font (`80px` to `100px`).
* **Composition Formula:**
  * **Left 55% of Canvas:** High-contrast text on obsidian background with emerald/cyan gradient keywords. Examples:
    * `NO MORE` / `NO-SHOWS` (Emerald text)
    * `AI FRONT DESK` / `IN 60 SECONDS`
    * `ZERO-LLM` / `SAFETY FIREWALL`
  * **Right 45% of Canvas:** High-resolution 3D render of the smartphone displaying the live WhatsApp interaction with a glowing green verification checkmark.

---

```
+----------------------------------------------------------------------------------------------------+
|                             2.4 TECHNICAL ARCHITECTURE INFOGRAPHICS                                |
+----------------------------------------------------------------------------------------------------+
```

### Asset: Clinical Safety Firewall & Security Overview Infographic
* **Canvas Size:** `1920 × 1080 px` (Sales Deck & Whitepaper Slide).
* **Visual Components:**
  1. **Input Gateway:** Inbound patient message on WhatsApp.
  2. **Security Gateway:** HMAC-SHA256 signature verification & distributed idempotency lock.
  3. **The Clinical Firewall (Highlighted in Neon Emerald):** In-memory deterministic regex engine screening 100+ drug names and medical queries.
  4. **The Split Path:**
     * *Medical Advice / Drug Request:* Trigger safe static refusal redirecting to doctor booking (Zero LLM inference).
     * *Administrative Inquiry:* Pass to Groq Llama-3.3-70b for intent mapping and slot retrieval.
  5. **Data Storage:** PostgreSQL multi-tenant database with strict Row-Level Security (RLS) policies.
  6. **Legal Badges:** `DPDP Act 2023 Compliant` · `NMC Telemedicine Safe` · `FHIR R4 Ready`.
