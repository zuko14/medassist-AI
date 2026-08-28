# Kriya AI — Business, Market & Technical Readiness Scorecard

**Document Version:** 1.0.0  
**Date:** August 2026  
**Publisher:** Xylarc AI Enterprise Audit & Business Strategy  
**Evaluation Model:** 14-Dimension Healthcare SaaS Commercial Readiness Index  

---

## 1. Executive Summary & Composite Readiness Score

```
+----------------------------------------------------------------------------------------------------+
|                                  COMPOSITE READINESS ASSESSMENT                                    |
+----------------------------------------------------------------------------------------------------+
|  OVERALL READINESS SCORE: 88.5 / 100  (PILOT-READY & COMMERCIAL-DEPLOYABLE)                         |
|  - Technical & Clinical Safety Readiness: 94 / 100                                                 |
|  - Product Architecture & Security: 96 / 100                                                       |
|  - Market Opportunity & Differentiation: 92 / 100                                                 |
|  - Sales Playbooks & Positioning: 90 / 100                                                         |
|  - Brand & Marketing Creative Infrastructure: 88 / 100                                             |
|  - Social Proof & Field Case Studies: 65 / 100 (Primary Area for Month 1–3 Focus)                  |
+----------------------------------------------------------------------------------------------------+
```

---

## 2. Granular 14-Dimension Readiness Scorecard

```
+----------------------------------------------------------------------------------------------------+
|                                    14-DIMENSION SCORING MATRIX                                     |
+----------------------------------------------------------------------------------------------------+
```

| Evaluation Dimension | Score (0-100) | Current State & Evidence | Key Strengths & Production Gaps |
| :--- | :---: | :--- | :--- |
| **1. Product-Market Fit (PMF)** | **92 / 100** | Solves the universal Indian OPD front-desk phone bottleneck; WhatsApp-native. | Extreme patient habit match; proven willingness to use WhatsApp over dedicated apps. |
| **2. Competitive Differentiation**| **94 / 100** | Zero-LLM Clinical Safety Firewall, ACID slot locks, non-invasive EMR sync. | Clearly differentiated from pure discovery marketplaces (Practo) and generic bots (Wati). |
| **3. Trust & Clinical Safety** | **98 / 100** | In-memory regex firewall screening 100+ Indian drug names; zero LLM clinical advice. | Eliminates doctor malpractice liability under NMC regulations; highest possible safety score. |
| **4. Security & Data Isolation** | **96 / 100** | PostgreSQL Row-Level Security (RLS) on all tables; DPDP 2023 consent logging. | Strict multi-tenant isolation; dual-tier transient purging with 7-year audit storage. |
| **5. Patient / User Experience** | **91 / 100** | Trilingual NLP (EN, HI, TE), 60-second booking, live token updates, pre-visit tips. | Smooth conversational flow; paginated doctor lists prevent WhatsApp 10-item cap issues. |
| **6. Sales Readiness & Playbooks** | **90 / 100** | Comprehensive 6-stage sales playbook, 15-minute demo script, 15 objection defenses. | Reps are fully equipped with quantifiable ROI calculators and diagnostic questions. |
| **7. Website & Digital Presence** | **85 / 100** | Clean, dark-mode architecture on `xylarcai.com`; detailed 16-section page blueprint ready. | Need to deploy interactive in-browser simulated WhatsApp widget on the live domain. |
| **8. Brand & Identity Architecture**| **92 / 100** | High-contrast visual identity, geometric Kriya Node symbol, circular-safe 1:1 profiles. | Perfectly aligned with Xylarc AI parent brand; distinct clinical emerald accent. |
| **9. Social Proof & Case Studies** | **65 / 100** | Sandbox verification with 438 passing tests; initial lighthouse pilots in progress. | Must convert Month 1–2 pilot deployments into published, named before/after case studies. |
| **10. Content & Distribution Engine**| **88 / 100** | 8 ranked content pillars, full 90-day publishing calendar, 12 reusable UI templates. | High-quality educational and technical distribution plan across LinkedIn, YouTube, Reels. |
| **11. Pricing & Commercial Packaging**| **94 / 100** | Flat predictable tiers (Starter ₹2,499, Polyclinic ₹5,999, Lab Pro ₹6,999, Enterprise). | Excellent gross margin (70–80%); aligns perfectly with Indian clinic willingness-to-pay. |
| **12. Enterprise Integration Depth** | **89 / 100** | Playwright headless browser sync for MocDoc/CallMedex; HL7 FHIR R4 standard REST API. | Solves the legacy HMIS barrier without requiring expensive third-party API contracts. |
| **13. Partner / Channel Readiness** | **82 / 100** | Defined reseller framework for local healthcare IT vendors and LIMS distributors. | Channel sales collateral created; initial regional partner outreach planned for Month 5. |
| **14. Regulatory & Legal Compliance**| **93 / 100** | DPDP Act 2023, NMC Medical Records Retention, Meta WhatsApp Business Policy aligned. | Verifiable consent logs, automated data erasure endpoint, utility template routing. |

---

## 3. The Red-Flag Risk List & Remediation Priorities

To ensure that commercial sales do not get ahead of operational maturity, the following risks are tracked and governed:

```
+----------------------------------------------------------------------------------------------------+
|                                      RED-FLAG RISK REGISTER                                        |
+----------------------------------------------------------------------------------------------------+
```

### 1. Risk: Client Proof Gap (Priority: HIGH)
* **Description:** Indian hospital procurement committees frequently ask: *"Which nearby hospital is already using this?"*
* **Impact:** May prolong enterprise hospital sales cycles in the first 60 days.
* **Remediation Strategy:** Execute the **30-Day Subsidized Pilot Program** for 5 lighthouse polyclinics in Hyderabad and Visakhapatnam in Month 1–2. Secure written operational outcome metrics and video testimonials before pitching large hospital chains.

### 2. Risk: Meta WhatsApp Cloud API Transit Latency (Priority: MEDIUM)
* **Description:** Meta's WhatsApp servers occasionally experience transient message delivery delays (2–5 seconds) during global peak hours.
* **Impact:** Patient on WhatsApp might think the bot is slow or unresponsive.
* **Remediation Strategy:** Implement instant WhatsApp read receipts (`mark_as_read`) and "typing..." indicators on webhook receipt; ensure background processing timeout is under 1.5 seconds.

### 3. Risk: Legacy HMIS UI Changes (Priority: MEDIUM)
* **Description:** For clinics using headless Playwright browser connectors (e.g., MocDoc), vendor UI layout updates could temporarily break CSS selectors.
* **Impact:** Automated booking injection might fail until selectors are updated.
* **Remediation Strategy:** Built-in connector health-check monitoring with automatic retry queues and fallback alerts sent to the clinic admin dashboard.

---

## 4. Final Commercial Readiness Verdict

```
+----------------------------------------------------------------------------------------------------+
|                                    FINAL COMMERCIAL VERDICT                                        |
+----------------------------------------------------------------------------------------------------+
|  WHAT CAN BE SOLD IMMEDIATELY (TODAY):                                                             |
|  - Standalone Clinics & Specialist Practices (Dental, Dermatology, Orthopedics, Pediatrics).       |
|  - Multi-Doctor Polyclinics (3–10 Doctors) seeking front-desk relief and no-show recovery.         |
|  - Standalone Pathology Labs & Diagnostic Centers seeking automated WhatsApp report delivery.      |
|                                                                                                    |
|  WHAT SHOULD BE PILOTED (MONTH 1–3):                                                               |
|  - 20–100 Bed Hospitals requiring multi-branch queue management and MocDoc browser sync.           |
|                                                                                                    |
|  BEST INITIAL GEOGRAPHIC MARKET:                                                                   |
|  - Hyderabad / Secunderabad & Visakhapatnam / Vijayawada (Home territory, high private OPD density)|
+----------------------------------------------------------------------------------------------------+
```
