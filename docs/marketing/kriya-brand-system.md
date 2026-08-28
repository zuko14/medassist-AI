# Kriya AI — Brand Architecture, Identity System & Master Design Specification

**Document Version:** 1.0.0  
**Date:** August 2026  
**Publisher:** Xylarc AI Global Brand & Creative Direction  
**Target:** Brand Designers, UI/UX Engineers, Marketing Directors, External Agencies  

---

## 1. Brand Hierarchy & Parent-Child Architecture

```
+----------------------------------------------------------------------------------------------------+
|                                    XYLARC AI BRAND ARCHITECTURE                                    |
+----------------------------------------------------------------------------------------------------+
|                                                                                                    |
|                                       XYLARC AI                                                    |
|                   (Parent Technology & Intelligent Systems Engineering Org)                        |
|                                 "Intelligence, Engineered for Action"                              |
|                                                                                                    |
|                 +-----------------------------------+-----------------------------------+          |
|                 |                                   |                                   |          |
|                 v                                   v                                   v          |
|            CALLMEDEX                            KRIYA AI                        ENTERPRISE AI      |
|    (Healthcare Tech Platform)           (Hospital OS SaaS Platform)        (Custom Automation Eng) |
|   Diagnostics & Home Healthcare          "The AI Operating Layer            Agents, RPA & Workflows|
|        CallMedex.com                          for Hospitals"                    Xylarc.ai          |
|                                                                                                    |
+----------------------------------------------------------------------------------------------------+
```

### Relationship Rules:
1. **Endorsed Brand Architecture:** Kriya AI is positioned with the formal endorsement line:  
   `KRIYA AI — Engineered by Xylarc AI`.
2. **Distinct Product Identity:** While reflecting Xylarc AI’s engineering depth and sleek dark-mode aesthetic (`#05070b`), Kriya AI carries its own distinct healthcare operational identity, anchored by **Clinical Emerald Green (`#54d59a`)**, **Precision Electric Blue (`#168cff`)**, and **Clinical Pure White (`#ffffff`)**.

---

## 2. Brand Meaning: Why "Kriya"?

* **Etymology & Sanskrit Origin:** *Kriya* (क्रिया) translates directly to **Action, Execution, Purposeful Deed, and Medical Procedure**.
* **Strategic Resonance:**
  * Generic chatbots only *talk*. Kriya AI **acts**—it locks slots, processes payments, coordinates waiting queues, extracts lab reports, and reschedules appointments.
  * In Indian healthcare, *Kriya* represents clinical precision, positive action, and health restoration.

---

## 3. Master Color Palette (Design Tokens)

```
+----------------------------------------------------------------------------------------------------+
|                                    KRIYA AI COLOR PALETTE TOKENS                                   |
+----------------------------------------------------------------------------------------------------+
```

### 3.1 Primary Brand Colors
* **Obsidian Deep Space (Background):** `#05070b` (hsl(220, 38%, 3%)) — Primary background for website, dark UI, and B2B marketing collateral.
* **Clinical Emerald (Primary Accent):** `#54d59a` (hsl(152, 60%, 58%)) — Represents health, operational success, verified bookings, and vitality.
* **Precision Electric Blue (Secondary Accent):** `#168cff` (hsl(210, 100%, 54%)) — Represents technological reliability, database integrity, and enterprise engineering.
* **Cyan Illumination (Glow & Highlights):** `#55dcff` (hsl(192, 100%, 67%)) — Used for subtle button glows, active states, and focus rings.

### 3.2 Neutral UI & Typography Tokens
* **Pure Text White (`--text-0`):** `#ffffff` — Headers, primary titles, critical metrics.
* **Slate Light (`--text-1`):** `#e6edf8` — Body copy, subheadings, key bullet points.
* **Muted Grey (`--text-2`):** `#b8c1cf` — Secondary descriptions, metadata, inactive labels.
* **Subtle Border Line (`--line`):** `rgba(255, 255, 255, 0.08)` — Card borders, glass panel dividers.
* **Glass Panel Surface (`--panel-bg`):** `rgba(13, 19, 28, 0.75)` with `backdrop-filter: blur(16px)`.

---

## 4. Typography Hierarchy

```
+----------------------------------------------------------------------------------------------------+
|                                      TYPOGRAPHY SYSTEM                                             |
+----------------------------------------------------------------------------------------------------+
```

| Type Role | Font Family | Weight | Tracking (Letter Spacing) | Usage |
| :--- | :--- | :---: | :---: | :--- |
| **Primary Display Title** | **Outfit** or **Geist Sans** | `600 / 700` (Bold) | `-0.03em` (Tight) | Hero headlines, major campaign headers, slide titles |
| **Section & Card Headers** | **Outfit** or **Inter** | `600` (Semi-Bold) | `-0.02em` | Product feature titles, modal headers, pricing tiers |
| **Body & Narrative Text** | **Inter** or **Geist Sans** | `400 / 500` (Regular/Medium) | `0em` (Normal) | Paragraphs, documentation, WhatsApp message templates |
| **System Metrics & Code** | **Geist Mono** or **JetBrains Mono** | `500 / 600` | `+0.05em` | Token numbers (`MC-2026-4821`), timestamps, API endpoints |

---

## 5. Kriya AI Logo Design Specification

```
+----------------------------------------------------------------------------------------------------+
|                                    LOGO DESIGN SPECIFICATION                                       |
+----------------------------------------------------------------------------------------------------+
```

### 5.1 The Geometric Symbol Concept ("The Kriya Node")
* **Concept Description:** The symbol synthesizes three core concepts without resorting to clichéd medical crosses:
  1. **The Dynamic Hexagonal Cell:** An outer geometric hexagon representing structured system architecture and data isolation.
  2. **The Clinical Pulse / Action Vertex:** An inner stylized kinetic wave flowing into an upward node, symbolizing vital health and positive action (*Kriya*).
  3. **The WhatsApp Conversational Loop:** The curves subtly intersect at a central focal dot, symbolizing continuous two-way communication between patient and hospital.
* **Visual Construction:**
  * Clean vector stroke width: `2.0px` on a `32x32` grid.
  * Gradient fill on the kinetic stroke: Linear gradient from **Clinical Emerald (`#54d59a`)** at 0% to **Precision Blue (`#168cff`)** at 50% to **Cyan (`#55dcff`)** at 100%.

### 5.2 The Wordmark Specification
* **Text:** `KRIYA AI`
* **Typography:** Clean geometric sans-serif (Customized Outfit/Geist Sans Bold), all uppercase, with letter-spacing of `+0.06em`.
* **Subtitle Lockup (Optional):** `HOSPITAL OPERATING SYSTEM` set in Geist Mono, tracking `+0.12em`, color `#b8c1cf`.

### 5.3 Color Variations & Applications

| Variant | Symbol Styling | Wordmark Color | Background | Usage |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Dark (Master)** | Emerald-to-Cyan Gradient | Pure White (`#ffffff`) | Obsidian Dark (`#05070b`)| Website, social headers, dark mode apps |
| **Primary Light (Clinical)**| Deep Emerald & Royal Blue | Dark Navy (`#0b1320`) | Pure White (`#ffffff`) | Printed sales decks, clinical contracts, letterheads |
| **Monochrome White** | 100% Solid White (`#ffffff`)| 100% Solid White (`#ffffff`)| Dark Backgrounds | Video watermarks, dark print collateral |
| **Monochrome Dark** | 100% Solid Dark (`#0b1320`)| 100% Solid Dark (`#0b1320`)| White / Light Backgrounds| Black & white printouts, faxes, receipts |

---

## 6. Master 1:1 Social Profile Specification (Circular-Safe)

All social media platforms (Instagram, X, LinkedIn, YouTube, WhatsApp Business) render profile pictures inside a circular crop. A single **Master Square (1:1)** specification ensures zero clipping across all channels:

```
+----------------------------------------------------------------------------------------------------+
|                         MASTER PROFILE ICON SPECIFICATION (1024 x 1024 px)                         |
+----------------------------------------------------------------------------------------------------+
|                                                                                                    |
|    +------------------------------------------------------------------------------+                |
|    |                                OUTER CANVAS                                  |                |
|    |                             1024 x 1024 px Square                            |                |
|    |                       Background: Solid Obsidian #05070b                     |                |
|    |                                                                              |                |
|    |                  . - - - - - - - - - - - - - - - - - - .                     |                |
|    |              '                                             '                 |                |
|    |           '            CIRCULAR SAFE ZONE                    '               |                |
|    |          '             Diameter: 820 px                       '              |                |
|    |         |                                                       |            |                |
|    |         |               +-----------------------+               |            |                |
|    |         |               |                       |               |            |                |
|    |         |               |     KRIYA SYMBOL      |               |            |                |
|    |         |               |     560 x 560 px      |               |            |                |
|    |         |               |   Centered Exactly    |               |            |                |
|    |         |               |                       |               |            |                |
|    |         |               +-----------------------+               |            |                |
|    |         |                                                       |            |                |
|    |          '                                                     '             |                |
|    |           '                                                   '              |                |
|    |              '                                             '                 |                |
|    |                  ' - - - - - - - - - - - - - - - - - - '                     |                |
|    |                                                                              |                |
|    |                Padding between Symbol and Circle Edge: 130 px                |                |
|    +------------------------------------------------------------------------------+                |
|                                                                                                    |
+----------------------------------------------------------------------------------------------------+
```

### 6.1 Platform-Specific Export Specs

| Platform | Canvas Dimensions | Safe Zone Shape | Symbol Size | Background Color | Output Format |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Master Asset** | **1024 × 1024 px** | Square | 560 × 560 px | `#05070b` | Master PNG / SVG |
| **WhatsApp Business** | **500 × 500 px** | Circular (400px dia) | 275 × 275 px | `#05070b` (Dark) or `#ffffff` (Light) | High-Res PNG |
| **LinkedIn Company Page**| **400 × 400 px** | Square/Circular | 220 × 220 px | `#05070b` | High-Res PNG |
| **Instagram Profile** | **320 × 320 px** | Circular (260px dia) | 175 × 175 px | `#05070b` | High-Res PNG |
| **X (Twitter) Profile**| **400 × 400 px** | Circular (320px dia) | 220 × 220 px | `#05070b` | High-Res PNG |
| **YouTube Channel Icon**| **800 × 800 px** | Circular (640px dia) | 440 × 440 px | `#05070b` | High-Res PNG |
| **Website Favicon** | **16×16 / 32×32 / 48×48** | Square (Scalable) | Full (with 2px pad)| Transparent / Dark | `.ico` & SVG |

---

## 7. Social Media Profile Bios & Taglines

### Instagram Bio
```text
Kriya AI by Xylarc AI
Hospital & Clinic Operations on WhatsApp 🏥
⚡ 60-Second Appointment Booking & UPI Gating
📊 Live Queue Tracking & Automated Lab Reports
👇 Experience the Live WhatsApp Demo
[link.xylarc.ai/kriya-demo]
```

### LinkedIn Bio / About
```text
Kriya AI is the autonomous WhatsApp operating layer for Indian clinics, polyclinics, and hospitals. Engineered by Xylarc AI, Kriya AI automates appointment booking, pre-consultation UPI payments, live waiting room queue tracking, and diagnostic lab report delivery with zero staff overhead—slashing front-desk phone congestion by 75% and eliminating no-shows.
```

### X (Twitter) Bio
```text
Kriya AI (@KriyaAI) — The WhatsApp operating layer for Indian hospitals. Autonomous booking, UPI payment gating, live queue tokens & AI lab reports. By @XylarcAI.
```

### YouTube Channel Description
```text
Welcome to the official channel for Kriya AI (Engineered by Xylarc AI). We build intelligent, clinical-grade operating systems that turn WhatsApp into an autonomous front desk for clinics, polyclinics, diagnostic centers, and enterprise hospital networks across India.
```
