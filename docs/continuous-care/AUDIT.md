# Kriya Continuous Care — Code Audit (file:line evidence)

> Taken on `main` @ `c8c37ff` (2026-11-24). Every CC design decision that depends on existing behaviour cites a row
> here. **Before touching an existing file, re-locate the row by symbol** (line numbers drift) and record a moved line
> in [status.md](status.md) §7. A row that turns out wrong must be corrected here *and* in every design doc that
> cites it, in the same commit.

## 0. Corrections to the brief

| Brief said | Code says | Consequence |
|:-|:-|:-|
| "Refill reminders are already a start" | True, but only for Pharmacy OS clinics (`rx_access`), only for pharmacist-opted-in chronic lines, and only through the clinic's approved template (A20) | CC reuses the pattern (closed maps, template, button prefixes) — not the table |
| "Follow-up visits" exist | The generic follow-up job sends a satisfaction/check-in message after a visit (A9). It does not schedule or chase the next visit | CC's radar is new |
| A completed appointment means the patient came | No — the auto-complete job marks every past confirmed visit `completed`, no-shows included (A10) | Verification uses check-in / signed encounter (ADR-CC-08) |

## 1. Plan registry, feature gates, lifecycle

| # | Fact | Evidence |
|:-|:-|:-|
| A1 | `PLAN_FEATURES` is the plan → feature registry; hybrids derive from `polyclinic` | `app/services/tenant.py:483`, `:600`, `:605` |
| A2 | `OPT_IN_FEATURES` holds owner-only modules (OPD, IPD, DX, RX, voice, Ketu); kept out of `PLAN_FEATURES` so the enterprise `*` wildcard never switches them on | `app/services/tenant.py:666` |
| A3 | Module access pattern: `rx_access()` returns `ACTIVE` / `WIND_DOWN` / `None`; explicit `features.<flag> is True`, never `has_feature()` | `app/services/tenant.py:737`; same for `ipd_access` `:683`, `dx_access` `:713` |
| A4 | OPD gate: `opd_enabled()` needs the owner opt-in **and** `has_feature(clinic, "booking")` | `app/services/tenant.py:755` |
| A5 | Patients / family members get an MRN from `opd_assign_mrn` | `migrations/103_opd_os_core.sql:220`; family members table `migrations/020_family_members.sql:4` |

## 2. Clinical facts CC reads (never writes)

| # | Fact | Evidence |
|:-|:-|:-|
| A6 | Encounters and prescriptions are versioned; only `signed` rows carry `signer_snapshot ? 'registration_number'`; one signed per appointment | `migrations/103_opd_os_core.sql` `opd_encounters` `:262`, `opd_prescriptions` `:325` |
| A7 | Prescription lines: `drug_name`, `formulation`, `strength`, `dosage`, `route`, `frequency` (OD/BD/TDS/QID/HS/SOS/STAT or custom), `timing`, `duration_days` (NULL = SOS/until review); writable only while the parent is a draft | `migrations/103_opd_os_core.sql:372` |
| A8 | `opd_encounters.diagnoses` is a JSON array `[{"system":"ICD-10","code":"J06.9","text":…}]` (≤ 20) | `migrations/103_opd_os_core.sql` `:292`; written by `app/services/opd_clinical.py:345` |
| A9 | `opd_encounters.follow_up_date` is stored (validated not in the past at `app/routers/opd.py:298`) and printed on the prescription (`app/services/opd_clinical.py:383`, `:607`) — **no scheduler job reads it**. The generic follow-up job is a post-visit message only | `migrations/103_opd_os_core.sql:296`; `app/services/scheduler.py:1100` (`send_followups`) |
| A10 | `auto_complete_appointments` marks every past **confirmed** appointment `completed` at 00:30 IST and "deliberately counts a no-show as completed" | `app/services/scheduler.py:1046` |
| A11 | IPD discharge summary `follow_up` is free text ≤ 2000 chars | `migrations/111_ipd_os_core.sql:429` (`ipd_discharge_summaries`, table at `:416`) |
| A12 | No teleconsultation mode on appointments; `booking_type` is `consultation` or `lab_test` | `migrations/039_appointments_lab_test_booking.sql:23` |
| A16 | `appointments.checked_in_at` exists (OPD front desk; DX visits also set it) | `migrations/103_opd_os_core.sql:133`; `migrations/127_dx_visits_and_billing.sql:689` |
| A17 | OPD/IPD investigation orders: one `opd_orders` row per line; status `ordered → billed → sample_collected → in_progress → resulted → completed` or `cancelled`; price snapshotted | `migrations/115_investigation_and_procedure_orders.sql:1-75`; `app/services/orders.py:146` (`_billing_by_order`) |
| A18 | LIS results: `lab_results` (versioned, `validated` = signed) and `lab_result_values.numeric_value` NUMERIC(10,3) + `flag` (normal/low/high/critical_*); `lab_tests.loinc_code` exists | `migrations/118_lab_information_system.sql:76`, `:129`; `migrations/126_dx_catalogue.sql:95` |
| A19 | Booking goes through `database.book_appointment`; double booking is stopped by `uq_appointment_active_slot` (consultation + doctor) | `app/database.py:1223`, `:1028` (`get_available_slots`); `migrations/103_opd_os_core.sql:174`, `:181` |

## 3. Messaging, consent, templates

| # | Fact | Evidence |
|:-|:-|:-|
| A13 | `send_text` refuses outside the 24-hour window unless `_window_open=True`; `_can_send_freeform` decides | `app/services/whatsapp.py:324`, `:966` |
| A14 | `send_template` sends an approved template; credentials are the clinic's own — no platform fallback in multi-tenant mode | `app/services/whatsapp.py:371`, `:138` (`_get_credentials`); AGENTS.md §1.6 |
| A15 | Legacy medication reminders (`prescriptions` table) send **free text** every 5 min window; outside the window `send_text` refuses, so delivery fails silently; the gate is `automated_outbound_allowed` (subscription), not `accepts_engagement` | `app/services/prescriptions.py:103`, `:168`; `app/services/subscription.py:344` |
| A20 | Refill engine: closed frequency map `{"OD":1,"BD":2,"TDS":3,"QID":4,"HS":1}`, dose regex, schedules H1/X/narcotic/psychotropic never get a plan, template `rx_refill_due_v1`, button prefixes `rx_refill:` / `rx_refill_off:`, 3 send failures → expired, `accepts_engagement` checked | `app/services/rx_refills.py:33-44`, `:145`, `:171`, `:205`; `migrations/142_rx_refills_alerts.sql:15` |
| A21 | Lost-sales read: signed prescriptions not billed by the pharmacy, part-filled lines; phones masked | `app/services/rx_dashboard.py:160` |
| A22 | Engagement consent: only an explicit `opted_in = False` suppresses engagement; transactional messages still go | `app/services/consent.py:20`; `patients.opted_in` `migrations/001_initial_schema.sql:7`; `data_consent_declined_at` `migrations/093_leads_consent_declined.sql:23` |
| A23 | Health check-ins day+3/+7: opt-in per clinic, approved template with quick replies `checkin_ok` / `checkin_concern`, retried 2 days | `app/services/scheduler.py:1286`, `:96`; `app/services/conversation.py:215` |
| A28 | Dental: owner-set monthly quotas with atomic `reserve_message_quota` (kinds `patient`/`doctor`/`review`), per-clinic Meta template submission via the clinic's WABA, review buttons `dentrev:` | `migrations/089_dental_treatment_plans.sql:136`, `:151`; `app/services/dental_plans.py:306`, `:858` |

## 4. Voice receptionist

| # | Fact | Evidence |
|:-|:-|:-|
| A24 | Outbound eligibility: a `patients` row of this clinic, not opted out, no consent refusal, not told us "never again" | `app/voice/outbound.py:35`, `:48`, `:62` |
| A25 | Calls only inside the clinic window, under a daily cap, ≤ max attempts, one live job per (phone, purpose) | `app/voice/outbound.py:1-11`; unique index `uq_voice_outbound_active` `migrations/098_ai_receptionist.sql:134` |
| A26 | Purposes: `lead_followup`, `callback_request`, `appointment_reminder` (CHECK widened in 112) | `migrations/098_ai_receptionist.sql:120`; `migrations/112_voice_appointment_reminders.sql:30` |
| A27 | DTMF digits arrive as stream events | `app/voice/exotel_protocol.py:66` |
| A32 | Voice safety list includes "suicide", "kill myself", "end my life" | `app/voice/safety.py:25` |

## 5. WhatsApp conversation pipeline (where CC hooks in)

| # | Fact | Evidence |
|:-|:-|:-|
| A29 | Order inside `_handle_message_locked`: duplicate guard → patient → session timers → unreadable media → **clinical firewall** (`:655`) → template-button hooks (dental `dentrev:`, then `rx_refill:`; `:679-696`) → `TEMPLATE_BUTTON_PAYLOADS` → `detect_intent` → … → emergency from any state (`:1148`) | `app/services/conversation.py:551-1150` |
| A30 | Firewall is deterministic, runs before any LLM, has PCPNDT handling | `app/services/clinical_firewall.py:410` |
| A31 | WhatsApp `EMERGENCY_KEYWORDS` (fast path of `detect_intent`) contains no self-harm phrase | `app/services/ai_engine.py:569`, `:944` |
| A34 | `MID_BOOKING_STATES` + 30-minute expiry guard | `app/services/conversation.py:222`, `:575` |
| A35 | Main menu is built per plan: `diagnostics_only` branch, treatment rows, lab row | `app/services/conversation.py:1790` |

## 6. Scheduler, locks, Ketu, tenancy

| # | Fact | Evidence |
|:-|:-|:-|
| A36 | Every job runs under `distributed_job_lock(name, lease_seconds)`; IST cron triggers | `app/services/distributed_lock.py:124`; `app/services/scheduler.py:160-460` |
| A37 | `TENANT_OWNED_TABLES` = 176 names; AST lint fails on unscoped queries | `app/tenancy.py:43`; `tests/test_lint_unscoped_queries.py` |
| A33 | Kriya Ketu `REGISTRY` of actions; page/action drift test | `app/assistant/registry.py:2239`; `tests/test_assistant_registry.py` |
| A38 | Latest migration on disk: `142_rx_refills_alerts.sql` → CC-M1 takes **143** | `migrations/` |

## 7. What does not exist (confirmed absent)

Searched `app/`, `migrations/`, `admin/` on 2026-11-24: no care plan, care card, protocol engine, observation store,
coordinator queue, PHQ-9/GAD-7, teleconsultation mode, structured IPD follow-up date, nightly unbilled-charge job,
self-harm WhatsApp keywords, immunisation schedule, antenatal schedule, odontogram, refraction record, IVF cycle
record. (`grep -rli "phq\|immuniz\|vaccin\|odontogram\|refraction\|oocyte\|embryo" app migrations` returns only
catalogue text and ICD-10 seed rows.)
