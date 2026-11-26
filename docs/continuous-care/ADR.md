# Kriya Continuous Care — Architecture Decision Records

> Format: Decision · Why · Rejected alternatives · Evidence (the test that proves it). Evidence rows name tests that
> must exist before the owning task is VERIFIED ([status.md](status.md)).

## ADR-CC-01 — Owner opt-in module with a three-state lifecycle
- **Decision.** `OPT_IN_FEATURES["care_enabled"]`. `cc_eligible(clinic) = opd_enabled(clinic)` in v1. `cc_access(clinic)`
  → `ACTIVE` when `features.care_enabled is True` and eligible; `WIND_DOWN` when `clinics.cc_state='WIND_DOWN'`; else
  `None`. Disable with active enrollments → 409 with a preview (counts of active enrollments, open cases, queued
  prompts); confirm → `WIND_DOWN`: no new enrollments, no new cards; open cases can be closed, data exported,
  patients receive one "this service has ended, contact the hospital" message (utility template); → `OFF` when no
  enrollment is active. Re-enable restores `ACTIVE` with data intact. Never `has_feature(clinic, "care_enabled")`.
  The platform refuses disabling OPD while `cc_state ≠ 'OFF'` (mirrors RX D7).
- **Why OPD in v1.** Care plans come from signed prescriptions; doctor approvals need doctor logins with registration
  numbers; verification needs check-in and signed encounters. All three are OPD OS.
- **Rejected.** A plan feature (`PLAN_FEATURES`): the enterprise wildcard would switch patient messaging on for clinics
  that never consented to it. "Care Lite" for diagnostic-only and pharmacy-only tenants is CC-D5 (reduced packs, no
  doctor inbox).
- **Evidence.** `tests/test_cc_feature_gate.py` (plan × flag × state × route matrix).

## ADR-CC-02 — Zero-LLM clinical boundary
- **Decision.** No module under `app/services/cc_*` or `app/routers/cc*` imports `ai_engine`, `ai_gateway`,
  `app/assistant`, `app/voice/nlu_llm` or any HTTP client to a model provider. Readings are parsed by regex and
  closed vocabularies; risk is computed by the protocol engine; safety texts are fixed and clinic-approved. AI is
  allowed in exactly three places, all **outside** the CC modules and all non-deciding:
  1. the existing voice receptionist's speech recognition and NLU while it runs the `care_checkin` script (every
     number is read back and confirmed, ADR-CC-16);
  2. a Kriya Ketu coordinator summary ("summarise the last 30 days"), labelled *AI summary — check the record*, built
     only from rows the coordinator can already see, never written back (KRIYA-KETU.md);
  3. none at runtime for language: cards are pre-written in en / hi / te and approved with the template.
- **Why.** NMC and the Telemedicine Practice Guidelines 2020 put diagnosis and prescribing on the RMP; a model that
  mis-reads "142" as "42" would fire a hypoglycaemia card.
- **Evidence.** `tests/test_cc_no_llm.py` (AST import scan, like `test_rx_no_llm.py`); `test_clinical_firewall.py` unedited.

## ADR-CC-03 — The never / always list is enforced in code
| Never | Enforced by |
|:-|:-|
| Diagnose | no free-text generation; card texts are fixed keys; `test_cc_cards_fixed_text.py` |
| Add, remove or change a medicine or dose | care plan items are copies of signed lines (ADR-CC-05); no write path to `opd_prescription_items`; approvals cannot carry a drug or dose field (`extra="forbid"` models) |
| Write free-text food advice | no free-text field reaches the patient; education items reference clinic-approved library entries only |

| Always | Enforced by |
|:-|:-|
| Say it is an AI / automated | consent text, first card, voice greeting (`responses.ai_disclosure`, existing) |
| Send any self-harm signal to a human at once | PHQ-9 item 9, keyword list (CC-L1), voice safety list → crisis card + urgent case + coordinator alert, synchronously in the handler |
| Show what to do now for a low sugar reading | `hypo` safety card selected by code on every glucose < clinic threshold (default 70 mg/dL) |

## ADR-CC-04 — No medication change outside a signed consultation
- **Decision.** The doctor's one-tap actions are: `book_review_visit`, `order_lab`, `book_home_sample` (DX plans with
  home collection), `recheck_in_days`, `continue_plan`, `call_patient_now`, `refer`. None of them changes a medicine.
  A dose step-up (e.g. the IHCI amlodipine 5 → 10 mg step) is *proposed* by the protocol as text for the doctor
  ("protocol step 2 due"), and happens only inside a consultation that ends in a signed prescription; the next signed
  version then re-derives the care plan.
- **Why.** Telemedicine Practice Guidelines 2020 require a consultation for prescribing; Kriya has no teleconsultation
  mode today (AUDIT A12). A one-tap prescription would be prescribing without a consult.
- **Deferred.** CC-D2 teleconsultation mode (video/phone consult with consent, ends in the same signed-prescription
  flow) — then "adjust treatment" becomes "book teleconsult today".
- **Evidence.** `tests/test_cc_actions.py::test_no_action_accepts_drug_or_dose_fields`.

## ADR-CC-05 — Care plan = verbatim projection of the latest signed prescription
- **Decision.** Source is `opd_prescriptions.status='signed'` only. Lines map through closed tables: frequency
  `OD/BD/TDS/QID/HS` → times of day (clinic-configurable clock times, default 08:00/14:00/20:00/22:00), `timing`
  (before/after food) shown as text, `duration_days` → end date. Anything else (SOS, STAT, custom strings, tapers,
  injections, inhalers, insulin sliding scales) becomes a *display-only* item: shown exactly as signed, no "taken?"
  prompt. H1/X/narcotic/psychotropic lines never get adherence prompts. A later signed version supersedes atomically.
- **Rejected.** Letting staff type a care plan: it creates a second prescription that can drift from the signed one.
- **Evidence.** `tests/test_cc_care_plan.py` golden cases (BD after food, TDS 5 days, SOS, insulin text, H1 line).

## ADR-CC-06 — Protocol packs are versioned code; a doctor signs the clinic's activation
- **Decision.** Each pack (`t2dm`, `htn`, `phq9`, …) is a Python module with `VERSION`, `PARAMS` (each with default,
  min, max, unit, source citation) and `evaluate()`. A clinic activates `(pack, version, params)`; the activation
  must be signed by a doctor login whose doctor row has a registration number (same snapshot shape as prescription
  signing, AUDIT A6). Parameters outside `[min, max]` are refused (422). A new pack version never auto-applies:
  the activation stays on its version until re-signed.
- **Why.** "Rules in code" must be reviewable by the hospital's medical director, reproducible years later, and
  impossible to loosen silently.
- **Evidence.** `tests/test_cc_protocols.py` (golden vectors per pack), `tests/test_cc_activation.py`.

## ADR-CC-07 — Safety first: immediate deterministic cards, honest hours
- **Decision.** Safety cards (low sugar, very high BP with symptoms prompt, PHQ-9 item 9, antenatal danger signs, OHSS
  signs, post-op eye pain/vision loss) are sent **in the same request** that stored the observation, as an in-window
  reply. Each carries the hospital emergency number and, for self-harm, Tele-MANAS 14416. An urgent case is opened in
  the same transaction. Consent text and every card footer state the hours replies are read ("Replies are read
  Mon–Sat 9 am–6 pm. In an emergency call …"). CC never implies 24×7 monitoring.
- **Evidence.** `tests/test_cc_safety_cards.py` (each trigger → card + case in one call, even with quota exhausted).

## ADR-CC-08 — "Counts only when recorded"
- **Decision.** An action is verified only by: `appointments.checked_in_at IS NOT NULL` or a signed `opd_encounters` row
  for that appointment (visit); a `validated` `lab_results` row or a DX result approved (lab); a `completed` `rx_sales`
  row with the item (refill). **`appointments.status='completed'` is never evidence** (AUDIT A10).
- **Evidence.** `tests/test_cc_verify.py::test_auto_completed_no_show_is_not_verified`.

## ADR-CC-09 — Typed readings: no new FSM state, claimed only inside the locked handler  *(revised in VERIFICATION D2)*
- **Decision.** CC adds **no conversation state** and **never writes `conversations` from a job**. Inside
  `_handle_message_locked` (per-phone lock already held), a typed text message is claimed as a reading only when all
  hold: the clinic has `cc_access`; the whole message matches a strict pattern (`^\d{2,3}$` glucose,
  `^\d{2,3}\s*[/\- ]\s*\d{2,3}$` BP, `^\d{1,2}(\.\d)?$` HbA1c; Devanagari/Telugu digits normalised); the session state is
  `None`, `idle` or `main_menu`; and this phone has an **open reading prompt** (sent ≤ 12 h ago, unanswered) for a
  measure the pattern fits. Only then is one indexed `cc_prompts` lookup made. Two open prompts for different family
  members → one "Whose reading is this?" button reply. Value outside the plausible range → one "please check the
  number" reply. Anything else is not touched and continues exactly as today.
- **Why revised.** A `cc_reading` state would have been reset by the unknown-state branch with a main-menu reply
  (`conversation.py:1509`), and a job writing state races the per-phone lock while `update_conversation` has no
  compare-and-set and swallows errors (`app/database.py:528`).
- **Rejected.** "Any number after a card is a reading" (breaks typed answers in other states, e.g. the digit handling
  at `conversation.py:2539`). LLM extraction (ADR-CC-02).
- **Evidence.** `tests/test_cc_conversation_hook.py` (state `selecting_*` + "12" untouched; "sugar 142 dizzy" untouched;
  "142" with an open prompt stored; "142" without an open prompt untouched; "1420" re-asked; non-CC clinic makes zero
  `cc_*` queries), full FSM suite unedited.

## ADR-CC-10 — Window-aware sending and its own message quota
- **Decision.** If `_can_send_freeform` is true, the card goes as an interactive message (richer buttons, and under
  Meta's per-message pricing in force since 1 Jul 2025 a utility message inside an open customer-service window is
  not charged); otherwise as the approved utility template `cc_care_card_v1` with quick replies. Every non-safety
  send reserves one unit of quota kind `care` (`reserve_message_quota`, AUDIT A28); a failed send releases it.
  Safety cards and replies to patient messages do not consume quota.
- **Evidence.** `tests/test_cc_channels.py` (window open → interactive; closed → template; quota −1 → not sent;
  failed send → released).

## ADR-CC-11 — Care consent is its own DPDP purpose
- **Decision.** Two steps, both recorded in `cc_consents`: (1) **WhatsApp opt-in** captured in person or on a call
  before any care template is sent (Meta requires opt-in before business-initiated messages; revised in VERIFICATION
  D7) — method, staff, time; (2) **DPDP consent** to the care purpose: notice text version + SHA-256, language, method
  (`whatsapp_button`, `voice_dtmf`, `counter_signed`), who captured it, and withdrawal. Enrollment cannot become
  `active` without a live consent. For a family member (child, elderly parent) the account holder consents as
  guardian and the record says so. STOP / "stop care" / withdrawal → all CC prompts stop at once; data kept per
  retention policy; erasure follows the existing DPDP flow (`data_retention.py`) plus `cc_purge_patient`.
- **Why.** General WhatsApp opt-in covers appointment messages; daily health monitoring is a new purpose under the
  DPDP Act 2023 and needs its own notice (DPDP Rules 2025 phase in through 2027; build to them now).
- **Evidence.** `tests/test_cc_consent.py`.

## ADR-CC-12 — Observations are append-only with correction provenance
- **Decision.** `cc_observations` rows are never updated or deleted by the app; a correction inserts a new row with
  `corrects_id` and a reason (IPD precedent). Evaluations reference observation ids, so the "why" of every tier is
  reproducible.
- **Evidence.** guard trigger in CC-M3; `tests/test_migration_cc_m3.py`.

## ADR-CC-13 — The radar is a read model; no triggers on existing tables
- **Decision.** `cc_radar_rows` is rebuilt per clinic twice daily from indexed reads, and closed incrementally when CC
  sees the closing event during `cc_verify`. No trigger is added to `appointments`, `opd_*`, `lab_*`, `rx_*`, `ipd_*`.
- **Why.** Triggers on hot clinical tables are the largest regression risk in a live system (RX ARCHITECTURE §8 rule).
- **Evidence.** `tests/test_migration_cc_*.py::test_no_trigger_on_existing_tables` (catalog query).

## ADR-CC-14 — One open case per (enrollment, reason), with an SLA
- **Decision.** Partial unique index on `cc_cases (clinic_id, enrollment_id, reason) WHERE status IN ('open','acknowledged','awaiting_doctor')`.
  SLA defaults: urgent 2 working hours, watch 2 working days, overdue 5 working days (clinic-configurable within
  bounds). Breach → escalate to `CC_ADMIN` and show red on the console.
- **Evidence.** `tests/test_cc_cases.py`.

## ADR-CC-15 — Outcomes: honest, cohort-based, rupees from verified events only
- **Decision.** Return rate, share at target, labs on time and revenue are computed per protocol cohort against the
  same patients' 90 days **before** enrollment, and against non-enrolled patients with the same ICD-10 codes as a
  reference (not a control). Revenue counts only verified visits (invoice paid), validated labs (billed) and completed
  refill sales. The report says "associated with", never "caused by".
- **Evidence.** `tests/test_cc_outcomes.py` with a fixture whose numbers are worked by hand in OUTCOMES.md.

## ADR-CC-16 — Voice check-in: deterministic script, spoken yes/no, read-back always
- **Decision.** Purpose `care_checkin`; the dialog asks only the questions the plan's due items need ("Did you take your
  morning medicines today? Please say yes or no"; "Please tell me your fasting sugar reading"). Every spoken number is
  read back ("I heard one four two. Is that correct?") and stored only after a spoken yes; two failed confirmations
  store nothing for that item. Identity is a spoken yes/no to "Am I speaking with <first name>?" — a "no" ends the
  call politely (`care_identity_failed`). Self-harm / emergency phrases are caught by the existing voice safety
  screen (`app/voice/safety.py`, run in `session.py` before the dialog), so care calls use the same emergency path.
  Calls obey the existing window and do-not-call rules (A24, A25).
- **Revision (implementation, 2026-11-25): no DTMF.** The design said "DTMF first". The voice runner
  (`app/voice/gateway.py`) does not route Exotel `dtmf` stream events (`exotel_protocol.py:66`) into the dialog, so
  key presses never reach `DialogEngine`. Building DTMF routing would touch the shared runner used by every inbound
  and reminder call; instead the care script uses spoken yes/no plus numeric read-back, and all voice answers are
  stored with `source='voice_speech_confirmed'` (`app/services/cc_voice.py`). `voice_dtmf` stays in the CHECK for a
  later DTMF lane and is never written today.
- **Wiring (revised in VERIFICATION D3).** Today the script is chosen by the call packet: a `reminder` dict gives the
  reminder greeting, anything else the **lead follow-up** greeting (`app/voice/gateway.py:357`,
  `app/voice/session.py:177-192`, `app/voice/dialog.py:104`). So `care_checkin` needs its own `care` packet branch in
  those three files, mirroring `reminder` and leaving the reminder and lead branches byte-identical; new call outcomes
  (`care_answered`, `care_partial`, `care_identity_failed`) added to the outcome set (`session.py:46`); `job_finished`
  retry rules for the new purpose. Care calls get their own `outbound.care_daily_cap` so they never use up the lead /
  callback budget that `dispatch_due` shares today (`app/voice/outbound.py:271`). A low reading captured by voice gets
  the `hypo_now` guidance **spoken in the call**, not only on WhatsApp.
- **Evidence.** `tests/test_cc_voice_checkin.py`; existing voice suites unedited.

## ADR-CC-17 — Messaging budget: care messages never starve the clinic's transactional messages
- **Decision.** Meta limits how many people a business number can message outside the 24-hour window per rolling day
  (its messaging tier) and lowers that limit when the number's quality rating drops. Kriya reads neither today
  (no `messaging_limit` / `quality_rating` handling in `whatsapp.py`). CC therefore: (1) reads the number's tier and
  quality daily from the Graph API into `cc_settings.wa_health`; (2) caps care **templates** at ≤ `care_tier_share`
  (default 40 %, bounds 10–60 %) of the tier per rolling 24 h; (3) at quality MEDIUM halves the share, at LOW stops
  care templates and alerts owner + clinic; (4) sends in the lowest-priority lane after the day's appointment reminders;
  (5) prefers in-window interactive cards, which do not open new conversations. Safety replies are never capped.
- **Why.** Care volume (≈ 40 prompts/patient/month) could otherwise block appointment reminders, lab reports and
  payment links for the whole clinic — the worst possible regression.
- **Evidence.** `tests/test_cc_channels.py::test_tier_share_cap`, `::test_quality_low_stops_templates`,
  `::test_safety_reply_never_capped`.

## Open owner decisions (defaults in force)
| ID | Decision | Default |
|:-|:-|:-|
| O-1 | Pilot | One hospital, T2DM, 50 patients, 90 days (deck p.12) |
| O-2 | Medical-director sign-off of each pack's parameters | Required before activation (no default) |
| O-3 | Card frequency | Medication card daily for opted-in patients; reading prompts per pack schedule |
| O-4 | What the hospital charges patients | Hospital's choice; CC exposes a billable service "Chronic care package (monthly)" in masters |
| O-5 | Coordinator staffing and hours | Hospital nurse/counsellor; Mon–Sat 09:00–18:00 |
| O-6 | Kriya pricing for CC | Per active enrollment per month + message add-on through the existing quota billing |
| O-7 | Legal review of consent notices (en/hi/te) | Required before go-live |
| O-8 | Self-harm keyword latent fix (CC-L1) on WhatsApp for all clinics | Recommended yes — changes a live reply |
