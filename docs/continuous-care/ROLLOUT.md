# Kriya Continuous Care — Rollout, Pilot and Operations

## 0. Zero-downtime deployment order (every phase)
1. Merge code with CC **OFF everywhere** (default). Code tolerates missing `cc_*` tables only behind `cc_access`
   (which is None while `cc_state` does not exist → every CC route 403, every job filters to zero clinics).
2. Apply the phase's migration on Supabase (in number order, after 142), then run
   `migrations/verify_supabase_schema.sql` — the CC section must be all PASS.
3. Smoke: `/health`, one WhatsApp message on a test clinic, one booking, one voice call (if enabled), one OPD bill.
4. Only then enable `care_enabled` for the sandbox clinic, then the pilot clinic.
Rollback: disable (→ WIND_DOWN/OFF) is instant; code rollback is safe because nothing outside CC reads `cc_*`;
schema rollback scripts run in reverse order 147 → 143 and refuse while they would destroy patient data.

## 1. Opt-in switch (owner console)
Clinic detail → **Continuous Care** toggle. Refused unless OPD OS is on. Shows the Care Setup checklist link.

## 2. Care Setup checklist (go-live blocked until all green)
| # | Item | Owner |
|:-|:-|:-|
| 1 | Reply hours, emergency number, medication clock times | clinic admin |
| 2 | CC templates APPROVED in en (+ hi / te if used) | clinic admin submits; Meta approves |
| 3 | Consent notice approved in each language (legal review, O-7) | clinic + counsel |
| 4 | Pack activation signed by a doctor (O-2) | medical director |
| 5 | Safety card texts approved (hypo, crisis at minimum) | medical director |
| 6 | ≥ 1 `CARE_COORDINATOR` login, trained (§5) | clinic |
| 7 | Monthly `care` message limit set | Kriya owner |
| 8 | Lab mapping for HbA1c / FBS (t2dm) | lab in-charge |
| 9 | Legacy reminders reviewed (L2 count shown) | clinic admin |

## 3. Pilot protocol (deck p.12: "first pilot: diabetes, one hospital, 50 patients")
| Item | Plan |
|:-|:-|
| Site | One OPD-enabled hospital with an in-house lab (HbA1c) and, ideally, Pharmacy OS |
| Cohort | 50 adults with T2DM (E11.*) seen in the last 6 months, own WhatsApp, consenting; exclusions: pregnancy, eGFR < 30 if known, type 1, under 18 |
| Recruitment | radar `candidate` list → doctor approves invites at consultation; target 50 active within 3 weeks |
| Baseline | per patient: visits, HbA1c tests, refills in the 90 days before enrollment (`cc_enrollments.baseline`) |
| Duration | 90 days of care, report at day 90; continue if criteria met |
| Primary measures | 3-month return rate vs baseline; HbA1c tested on time; share at target (≤ clinic target) among those tested |
| Business measures | extra verified visits, labs, refills in ₹ vs the clinic's CC fee |
| Safety measures | every hypo/urgent card acted on within SLA; zero self-harm signals missed; zero dose/medicine changes outside a consult |
| Operational | answered-card rate ≥ 60 %; unparsed ≤ 5 %; coordinator time ≤ 2 h/day for 50 patients |
| Stop rules | any missed urgent safety case → pause new enrollments, root cause, fix, resume; template quality "low" → pause cards |
Success = return rate and labs-on-time up, safety measures clean, and "the hospital earns more than it pays".

## 4. Expansion order after the pilot
1. `htn` at the same hospital (shares coordinators and patients — many have both).
2. `phq9` with a counsellor (MANAS model) where the hospital has psychiatry/psychology.
3. Second hospital; then specialty packs per plan (docs/specialty-os) — eye `eye_dr_screen` first because it links
   diabetes patients to eye hospitals.

## 5. Staff training (1 hour, recorded)
Coordinator: queue, SLA, calling script, notes, when to request a doctor, safety escalation, what never to say
(no dose advice). Doctor: inbox, one-tap actions, pack activation, why the system never changes medicines.
Front desk: the "care" chip and checking enrolled patients in (verification depends on it).

## 6. Monitoring after go-live
| Signal | Threshold | Action |
|:-|:-|:-|
| Card send failures (template/Meta) | > 5 % / day | alert owner + clinic; check template status |
| Unanswered rate | > 50 % for 7 days | review timing/language; voice fallback |
| Urgent cases older than SLA | any | escalate to clinic admin; daily owner digest |
| Unparsed readings | > 5 % | review parser vectors with real (masked) samples |
| `care` quota at 90 % | — | owner warned (dental pattern) |
| Job duration (`cc_evaluate`, `cc_radar_refresh`) | > 60 s per clinic | profile; add index via reviewed migration |
| Webhook p95 | +5 % vs baseline | investigate hook; disable CC for the clinic if needed |

## 7. Communications
Patients: consent message (W2), first card explains what to expect and the reply hours. Clinic: a one-page
"What Kriya Care does / never does" for doctors and coordinators (from ADR-CC-03).
