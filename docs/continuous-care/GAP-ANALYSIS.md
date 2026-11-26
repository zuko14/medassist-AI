# Kriya Continuous Care — Gap Analysis (what exists vs what the care loop needs)

> Inputs: the "Kriya Continuous Care" deck (13 pages, Nov 2026) and the product review that ranked the gaps
> ("build only the gaps that help you sell or prove results": continuous care, data import, nightly unbilled-charge
> check; AI scribe and review requests wait for a customer to ask). Evidence for every "exists" claim is in
> [AUDIT.md](AUDIT.md) with file:line. Lane prefix **CC**.

## 1. Summary

Kriya already reaches patients (WhatsApp FSM, voice receptionist with outbound calls) and already holds the clinical
facts a care loop needs (signed OPD prescriptions with dose/frequency/duration, `follow_up_date`, ICD-10 diagnoses,
LIS numeric results with flags, pharmacy refill plans, check-in timestamps). **What is missing is the loop itself**:
nothing turns those facts into a per-patient plan, nothing measures the patient between visits, nothing decides who
is off target, nobody owns the follow-up list, and nothing proves the patient came back.

The deck's six steps map to the code like this:

| Step | Deck promise | What exists today | Gap |
|:-|:-|:-|:-|
| 1 Find | "Who is overdue, missed a lab or refill, or is off target" | `opd_encounters.follow_up_date` is stored but **no job reads it** (AUDIT A9); `rx_dashboard.lost_sales` lists unbilled prescriptions (A21); `rx_refill_plans` knows supply end (A20) | No radar. No candidate list from diagnoses (E11/I10…). No "missed lab" view across OPD orders |
| 2 Reach | WhatsApp daily care card; voice call in their language | Template sending with window check (A13, A14); voice outbound queue with consent rule and purposes (A24–A27) | No care card template, no care-card send job, no `care_checkin` voice purpose, no care script |
| 3 Measure | Medicines taken, sugar/BP readings, PHQ-9, lab results | `lab_result_values.numeric_value` + `flag` (A18); legacy medication reminders send text only (A15) | No observation store. Typed "142" today falls into the FSM as an intent, not a reading (A31). No PHQ-9 |
| 4 Decide | Protocol rules in code: stable / watch / urgent | Deterministic firewall exists (A30) — no clinical rule engine | Whole protocol engine is new (zero-LLM, versioned, signed off by a doctor) |
| 5 Act | Doctor approves next step in one tap; visit or lab booked | Booking (`book_appointment`, slot index), lab orders (`opd_orders`), home collection (DX plans only) | No coordinator queue, no doctor approval inbox, no action→booking bridge |
| 6 Verify | "Counts only when the visit or lab is recorded" | `appointments.checked_in_at`, signed `opd_encounters`, validated `lab_results`, completed `rx_sales` | **`appointments.status='completed'` does not prove attendance**: the 00:30 job marks every past confirmed visit completed, no-shows included (A10). Verification must use check-in / signed encounter / validated result / completed sale |
| — | Outcome report: return rate, share at target, visits, labs, refills in ₹ | Insights/analytics count bookings | No cohort, no baseline, no ₹ attribution |

## 2. The three ranked gaps from the product review

| Gap | Why it matters | Where it is specified | Size |
|:-|:-|:-|:-|
| **Continuous care** (care plans, follow-up list, protocol steps, WhatsApp care card) | The differentiator and the investor story; refill reminders are the start (RX P8) | this folder | Large — 10 phases |
| **Data import from old systems** | Removes the biggest reason hospitals refuse to switch; also seeds Continuous Care with chronic patients and their history | [docs/data-import/SPEC.md](../data-import/SPEC.md) | Medium |
| **Nightly unbilled-charge check** | Small to build on the existing unbilled-orders / lost-sales reads; shows value in rupees within a week | [docs/revenue-integrity/SPEC.md](../revenue-integrity/SPEC.md) | Small |

The review's deferrals stand: **AI scribe** and **review requests** are not in this plan (review requests already
exist for dental sittings, AUDIT A28).

## 3. Gap register (CC)

IDs are referenced by [status.md](status.md) tasks. "Reuse" names the existing code the gap is closed with.

| ID | Gap | Today | Close with | Reuse | Phase |
|:-|:-|:-|:-|:-|:-|
| G1 | Module gate and lifecycle | none | `OPT_IN_FEATURES["care_enabled"]`, `cc_access()` OFF→ACTIVE→WIND_DOWN | `rx_access` pattern (A3) | P1 |
| G2 | Care-specific consent (DPDP purpose) | general WhatsApp opt-in, `opted_in`, `data_consent_declined_at` (A22) | `cc_consents` with purpose, language, method, text hash, withdrawal | consent service + STOP handling | P1 |
| G3 | Protocol engine | none | pure-Python, versioned packs; evaluation stored with inputs hash and reasons | firewall's deterministic style (A30) | P2 |
| G4 | Doctor sign-off of protocol thresholds | none | `cc_protocol_activations` signed by an RMP login (registration number required, like prescription signing) | OPD signer snapshot (A6) | P2 |
| G5 | Enrollment | none | `cc_enrollments` (patient or family member, protocol, doctor, coordinator, language, channel) | patients / family_members MRN (A5) | P3 |
| G6 | Care plan from prescription | prescription lines carry dose/frequency/duration (A7) but nothing reads them after printing | `cc_care_plans` + `cc_plan_items` derived from a **signed** prescription; re-derived on the next signed version | `rx_refills.units_per_day` closed map (A20) | P3 |
| G7 | Care card (WhatsApp) | legacy reminders use `send_text` only → silently dropped outside the 24-hour window (A15) | utility template `cc_care_card_v1` (outside window) / interactive card (inside window, not charged) | `send_template`, `_can_send_freeform` (A13, A14), dental template submission (A28) | P4 |
| G8 | Reading capture | a typed number is classified as an intent | no new FSM state: a strictly numeric message is claimed only when the session is idle/main menu and an open reading prompt exists (ADR-CC-09 revised); everything else untouched | button-hook pattern (A29) | P4 |
| G9 | Adherence taps | none | quick replies `cc:taken:` / `cc:missed:` payloads | `rx_refills.handle_button` pattern (A29) | P4 |
| G10 | PHQ-9 / GAD-7 | none | 9 + 7 button questions; item 9 > 0 → immediate human | voice safety list has "suicide" (A32) | P4 |
| G11 | Self-harm signal on WhatsApp | **WhatsApp `EMERGENCY_KEYWORDS` has no self-harm phrases** (A31); only voice has them (A32) | latent fix CC-L1: self-harm phrases (en/hi/te) → human escalation card with Tele-MANAS 14416, in every state | `_handle_emergency` | P0b |
| G12 | Voice care check-in | outbound purposes: lead_followup, callback_request, appointment_reminder (A26) | purpose `care_checkin`; deterministic script; numbers by speech or DTMF | `voice_outbound_jobs`, `dispatch_due`, Exotel DTMF events (A27) | P5 |
| G13 | Follow-up radar | nothing reads `follow_up_date`; IPD discharge follow-up is free text (A11) | read model over encounters, appointments, orders, results, refills, evaluations; candidates from ICD-10 | `opd_encounters.diagnoses` (A8) | P6 |
| G14 | Coordinator queue | none | `cc_cases` (one open case per enrollment per reason), SLA, assignment, contact log | Kriya Ketu read handlers for summaries (A33) | P7 |
| G15 | Doctor approval inbox | none | one-tap actions that **book** (visit, lab, home sample) or **record** (continue, recheck); never prescribe | `book_appointment`, `opd_orders`, home collection | P7 |
| G16 | Medication change between visits | no teleconsultation mode in `appointments` (A12) | **out of v1**: dose changes happen only inside a consultation that ends in a signed prescription (ADR-CC-04); teleconsult mode is CC-D2 | — | deferred |
| G17 | Verification | completed ≠ attended (A10) | `cc_verifications`: check-in, signed encounter, validated lab result, completed refill sale | 103, 118, 138 tables | P8 |
| G18 | Outcome report in ₹ | none | cohort report vs 90-day pre-enrollment baseline; revenue from verified visits/labs/refills | invoice / sale totals | P8 |
| G19 | Message cost control | dental has atomic monthly quotas (A28) | new quota kind `care` in `clinic_message_quota_usage` (CHECK widened) | `reserve_message_quota` | P4 |
| G20 | Specialty protocol packs | specialty plans stop at the treatment catalogue | packs per plan (derma, eye, dental, IVF, women & child, diagnostics, pharmacy) — [PROTOCOLS.md](PROTOCOLS.md) §6 and [docs/specialty-os](../specialty-os/README.md) | dental treatment plans, refill plans | P9 |
| G21 | Staff copilot answers | Ketu R0 reads for OPD/IPD/RX/DX | `cc.*` read actions: "who is overdue today", "summarise Lakshmi's last 30 days" | registry drift test (A33) | P10 |

## 4. Latent defects found while tracing (exist in production today, not caused by CC)

| ID | Defect | Evidence | Proposed handling |
|:-|:-|:-|:-|
| L1 | Self-harm phrases ("want to die", "suicide", "मरना चाहता", "చనిపోవాలని") are not WhatsApp emergency keywords; the message reaches the LLM intent classifier | A31, A32 | CC-L1 (P0b): add phrases + a dedicated human-escalation card; needs owner approval because it changes live replies |
| L2 | Legacy medication reminders (`prescriptions` table) are sent with `send_text`, which refuses outside the 24-hour window — most reminders after day 1 are never delivered and the job logs no error per patient | A15 | CC-L2: report the count per clinic on the CC setup page; migrate active rows to care plans when CC is on; do **not** silently change the legacy job |
| L3 | Legacy reminder confirmation text promises "Reply STOP anytime" — STOP sets `opted_in=False` but the legacy job checks only `automated_outbound_allowed` (subscription), not `accepts_engagement` | A15, A22 | CC-L2 adds the `accepts_engagement` check (one line, guarded by a test) — owner approval |
| L4 | `appointments.status='completed'` includes no-shows | A10 | not a bug for its original purpose; CC and analytics must not read it as attendance (ADR-CC-08) |
| L5 | IPD discharge `follow_up` is free text (≤ 2000 chars) — no date to remind on | A11 | CC adds an optional structured `cc_discharge_followups` row written from the discharge screen (no change to the 111 column) |

## 5. What Continuous Care deliberately does not do (v1)

- Diagnose, add/remove/change a medicine or dose, or write free-text food advice (deck p.11; ADR-CC-03, ADR-CC-04).
- Read a reading through an LLM. Numbers are parsed by code; the LLM never sees a reading (ADR-CC-02).
- Claim 24×7 monitoring. Every card and the consent text say when the hospital reads replies (SECURITY §4).
- Run on WhatsApp numbers the clinic does not own (AGENTS.md §1.6: the clinic's credentials or nothing).
- Device integrations (glucometer Bluetooth, BP cuffs) — CC-D4.
- Teleconsultation with prescribing — CC-D2.
- Marketing broadcasts. Care messages are utility templates tied to the patient's own enrollment.

## 6. Gaps by plan (summary — detail in docs/specialty-os)

| Plan | Continuous-care pack that sells it | Pre-requisite module |
|:-|:-|:-|
| soloclinic / essential / polyclinic / multispecialty / enterprise | T2DM, hypertension, thyroid, depression/anxiety (PHQ-9/GAD-7), post-discharge | OPD OS (IPD OS for post-discharge) |
| derma | isotretinoin and methotrexate monitoring, procedure-series reminders, chronic psoriasis/vitiligo review | OPD OS |
| eye | post-cataract schedule, anti-VEGF series, glaucoma review, diabetic-eye screening recall | OPD OS |
| dental | 6-month recall, orthodontic/aligner cadence, implant review (on top of dental treatment plans) | OPD OS or dental plans |
| ivf | monitoring-scan and beta-hCG dates, OHSS danger-sign card, cryo-storage renewal | OPD OS |
| womenchild | antenatal schedule and danger signs, immunisation schedule, growth review | OPD OS |
| diagstream / diagbooking | periodic test due (HbA1c, lipid, TSH) with home-sample offer — "Care Lite", no doctor approvals | DX OS (CC-D5) |
| pharmacy (new retail plan) | chronic refills (exists), adherence card — "Care Lite" | Pharmacy OS (CC-D5) |
