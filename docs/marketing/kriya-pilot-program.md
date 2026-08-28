# Kriya AI — 30-Day Healthcare Automation Pilot Program Charter

**Document Version:** 1.0.0  
**Date:** August 2026  
**Publisher:** Xylarc AI Clinical Onboarding & Customer Success  
**Target:** Hospital Managing Directors, Clinic Partners, Procurement Committees  

---

## 1. Pilot Program Philosophy & Executive Objective

The **Kriya AI 30-Day Healthcare Automation Pilot** is a structured, risk-free operational deployment designed to prove quantifiable business impact and clinical reliability in an active clinic or hospital environment.

```
+----------------------------------------------------------------------------------------------------+
|                                    PILOT PROGRAM CORE OBJECTIVES                                   |
+----------------------------------------------------------------------------------------------------+
|  1. Quantifiable Front-Desk Relief  | Demonstrate > 60% reduction in routine phone inquiry volume. |
|  2. Revenue Leakage Recovery        | Reduce appointment no-show rates from ~25% to under 10%.    |
|  3. Zero Disruption to Care         | 100% clinical safety compliance with zero medical advice bleed|
|  4. Staff Empowerment & Ease of Use | Full receptionist adoption of the admin portal within 48h.  |
+----------------------------------------------------------------------------------------------------+
```

---

## 2. 4-Week Structured Implementation Timeline

```
+----------------------------------------------------------------------------------------------------+
|                                  4-WEEK PILOT EXECUTION TIMELINE                                   |
+----------------------------------------------------------------------------------------------------+
```

### Week 1: Provisioning, Baseline Audit & Staff Training (Days 1–7)
* **Day 1–2 (Technical Provisioning):**
  * Setup dedicated clinic tenant on Supabase multi-tenant infrastructure.
  * Connect clinic's designated WhatsApp Business Phone Number (Meta Cloud API).
  * Configure clinic profile: branches, doctor rosters, shift timings (morning/evening), consultation fees, hospital location, and Google Maps link.
* **Day 3–4 (Baseline Measurement):**
  * Front desk logs total daily phone call volume, appointment bookings, and no-shows for 48 hours to establish the pre-Kriya operational baseline.
* **Day 5–7 (Staff Onboarding & Dry-Run):**
  * Conduct a 45-minute training session for receptionists and clinic coordinators on `admin/index.html` (checking in patients, allocating queue tokens, managing leaves).
  * Run 10 synthetic test bookings across staff phones to verify trilingual language responses and Razorpay payment link generation.

---

### Week 2: Controlled Patient Rollout (Days 8–14)
* **Day 8 (Soft Launch):**
  * Place branded tabletop QR code counter stands at clinic reception and update Google Business Profile with *"Book Instantly on WhatsApp"* link.
  * Receptionists guide walk-in and calling patients: *"You can now book and check your token directly on our WhatsApp number."*
* **Day 9–14 (Active Monitoring):**
  * Automated 24-hour and 2-hour appointment reminders activate for all scheduled patients.
  * Kriya AI technical account manager monitors message error logs and intent fallback rates daily.
  * **Mid-Week Check-in (Day 11):** Review initial booking conversion and staff feedback; tune custom FAQ responses if needed.

---

### Week 3: Full Feature Activation (Days 15–21)
* **Day 15 (Live Queue & Report Activation):**
  * Full rollout of the Live Waiting Room Queue Token engine. Receptionists check in arriving patients; patients receive real-time token tracking on WhatsApp.
  * (For Diagnostic Labs / Clinics with Labs): Activate automated lab report PDF ingestion and WhatsApp delivery receipts.
* **Day 16–21 (Adoption Acceleration):**
  * Post-visit WhatsApp feedback collection activates (4 hours post-consultation rating pings).
  * Doctor leave management tested live: Doctors/Staff mark leave in dashboard; system handles patient rescheduling automatically.

---

### Week 4: Business Review, ROI Audit & Paid Contract Transition (Days 22–30)
* **Day 22–25 (Operational Metric Aggregation):**
  * Kriya AI analytics engine compiles the 30-Day Operational Impact Report:
    * Total WhatsApp conversations processed.
    * Number of appointments booked autonomously.
    * Measured drop in patient no-show rate.
    * Estimated front-desk staff hours saved.
    * Average patient feedback rating (CSAT).
* **Day 26–28 (Executive ROI Presentation):**
  * Present findings to the Clinic Owner / Hospital Managing Director.
  * Compare baseline metrics vs. Week 4 performance.
* **Day 29–30 (Seamless Contract Finalization):**
  * Transition tenant from pilot status to active annual subscription tier (Clinic Starter, Polyclinic Growth, or Hospital Enterprise) with zero downtime.

---

## 3. Quantified Pilot Success Scorecard

The pilot is considered successful and ready for paid commercial conversion upon achieving the following verifiable benchmarks:

```
+----------------------------------------------------------------------------------------------------+
|                                     PILOT SUCCESS BENCHMARKS                                       |
+----------------------------------------------------------------------------------------------------+
```

| Metric / KPI | Pre-Pilot Baseline (Typical) | Target Pilot Benchmark | Verification Method |
| :--- | :---: | :---: | :--- |
| **Front-Desk Phone Call Reduction** | 100% baseline volume | **> 50% – 70% Reduction** | Staff call log comparison |
| **Appointment No-Show Rate** | 20% – 35% | **< 10%** | Admin appointment completion logs |
| **Booking Speed / Duration** | 3 – 5 mins over phone | **< 65 Seconds on WhatsApp** | Kriya AI session timestamp metrics |
| **System Uptime & Stability** | N/A | **> 99.8% Availability** | Server health check telemetry |
| **Clinical Safety Breaches** | Zero tolerance | **0 Unauthorized Prescriptions** | Clinical Firewall audit logs |
| **Patient Satisfaction Score (CSAT)**| 3.8 / 5.0 | **> 4.6 / 5.0** | Post-visit WhatsApp feedback ratings |

---

## 4. Pilot Program Terms & Zero-Risk Guarantee

1. **Flat Subsidized Pilot Fee:** The 30-day pilot is offered at a nominal setup & onboarding fee (e.g., ₹2,999 for clinics / ₹9,999 for hospitals), which is **100% credited** toward the first year's annual subscription upon conversion.
2. **Zero Lock-In:** If the clinic or hospital leadership determines at Day 30 that Kriya AI did not deliver substantial operational relief, they may cancel with zero ongoing contractual obligation.
3. **Data Ownership & Export:** All patient records, booking histories, and operational analytics remain 100% the property of the clinic and can be exported at any time via CSV or FHIR R4 standard endpoints.
