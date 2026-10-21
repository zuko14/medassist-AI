# Session 38 — AI Receptionist Appointment Reminder Calls

**Date:** 2026-10-21  
**Branch:** `main` (uncommitted working tree)  
**Scope:** Kriya (the AI voice receptionist) automatically calls patients before their confirmed appointment, in Telugu by default, to remind them and ask them to come on time. Hospital admins switch it on and choose the timing in the admin panel.  
**Migrations:** `112_voice_appointment_reminders.sql`, `migrations/rollback/112_down.sql`  
**Test Suite:** 29 new tests (`tests/voice/test_reminder_calls.py`, `tests/test_migration_112_voice_reminders.py`). Full suite: **6,837 passed, 5 skipped, 0 failed**.

---

## 1. Why

Hospitals already send WhatsApp reminders (24 h and 2 h before). Many patients do not read WhatsApp, miss the visit or come late. A phone call from the hospital's own receptionist reaches them. WhatsApp reminders continue unchanged; the call is an addition.

## 2. What was built

Reminder calls reuse the existing outbound-call pipeline (`voice_outbound_jobs` → Exotel dial → audio gateway → dialog engine) with a new job purpose, `appointment_reminder`. No second dialer was built.

```
every 2 min: autoqueue_reminders()          every 2 min: dispatch_due()
  clinics with reminders ON                   re-check: still before call_by? 09:00-21:00?
  confirmed doctor appointments today/tmrw    re-check: appointment still confirmed, same date/time?
  due offset reached? -> insert job ──────►   opt-out? -> dial via Exotel
  (once per appointment+time+offset, DB-enforced)        │
                                                         ▼
                                       Kriya: greeting_reminder -> yes / no / question
```

### 2.1 What the patient hears

Telugu (default primary language):
> నమస్కారం, నేను {hospital} నుండి Kriya మాట్లాడుతున్నాను. Lakshmi గారికి ఈ రోజు సాయంత్రం 5:30కి డాక్టర్ Ramesh Rao గారితో అపాయింట్మెంట్ ఉంది. అది గుర్తు చేయడానికే ఫోన్ చేశాను. దయచేసి సమయానికి, వీలైతే పది నిమిషాలు ముందుగానే రండి. మీరు వస్తున్నారా అండి?

Hindi and English versions exist; the existing language auto-switch applies. If the appointment has no plain patient name, it says "మీకు / you have / आपका".

| Patient says | Kriya does | Recorded outcome |
|---|---|---|
| Yes / I'll come | Thanks them, ends the call | `reminder_confirmed` |
| No / can't come | "No problem. Shall I move your appointment?" → straight into reschedule of **this** appointment | `reminder_cant_come` |
| Both yes and no | Treated as "can't come" (safer: they can decline the move) | `reminder_cant_come` |
| Cancel / hospital question | Existing cancel / knowledge flows | as usual |
| Unclear / "hello?" | Repeats only "Are you coming to the appointment?" | — |

Outcomes appear under workflow `REMINDER` in voice analytics automatically.

### 2.2 Admin panel

AI Receptionist → Settings → **Appointment reminder calls**:
- Toggle: "Call patients before their appointment" (off by default).
- Up to 3 times: 15 min, 30 min, 1 h, 2 h, 3 h, 1 day, or a custom value (15–1440 minutes).
- Call queue shows reminder rows ("Appointment reminder", doctor, date, time) and plain-language skip reasons (`too_late`, `appointment_changed`, `superseded`).

Stored at `clinics.config.voice.reminders = {"enabled": bool, "minutes_before": [int]}`. Validated server-side (`Reminders` model in `voice_admin.py`). A client that does not send `reminders` (older UI, Kriya Ketu) keeps the stored value.

## 3. Reliability rules

| Rule | Where |
|---|---|
| One reminder per appointment slot per offset, enforced by the DB (`uq_voice_reminder_once` on `context->>'reminder_key'`) | migration 112 |
| `reminder_key = <appt id>\|<date>T<time>\|<minutes>`: a rescheduled appointment gets a fresh reminder | `outbound.reminder_key` |
| After downtime or when first enabled, only the latest due offset is called (never a burst) | `outbound.reminder_due` |
| An offset that passed before the booking was made is skipped (patient just booked) | `outbound.reminder_due` |
| Calls only 09:00–21:00 and never later than 10 min before the visit (`call_by`); never deferred past it | `dispatch_due` |
| Appointment re-read before dialling: cancelled/moved → skipped; read failure → retry in 1 min (never guess) | `_reminder_still_valid` |
| Unanswered → retry after 10 min, only while before `call_by`, up to `outbound.max_attempts` | `job_finished` |
| An older queued reminder for the same visit is cancelled (`superseded`) when a later one is due | `_queue_clinic_reminders` |
| Opt-outs never called (recorded once as `skipped/do_not_contact`); suspended clinics, shadow mode and exhausted budget skipped | sweep |
| Not limited by the lead `daily_cap` (transactional); monthly voice budget still applies | sweep / dispatch |
| One clinic's sweep failure never stops others; distributed lock prevents parallel sweeps | `autoqueue_reminders`, scheduler |
| Only confirmed, non-walk-in consultations with a doctor name and slot time (same set as WhatsApp reminders) | sweep query |

Lead-call behaviour (window, cap, 2 h retry) is unchanged; `test_lead_retry_is_unchanged` pins it.

## 4. Files changed

| File | Change |
|---|---|
| `migrations/112_voice_appointment_reminders.sql` | Widen `purpose` check; add `uq_voice_reminder_once` |
| `migrations/rollback/112_down.sql` | Rollback (deletes reminder jobs, restores old check) |
| `app/voice/outbound.py` | Reminder sweep, due-time logic, dial-time guards, reminder retry |
| `app/voice/store.py` | `reminders` default config (off, 60 min) |
| `app/voice/dialog.py` | Reminder greeting, `expect == "reminder"` handling, `offer_reschedule` |
| `app/voice/responses.py` | Templates (te/hi/en): `greeting_reminder`, `reminder_ask_coming`, `reminder_confirmed`, `no_problem`, `offer_reschedule` |
| `app/voice/session.py` | `start(reminder=...)`, shared `_plain_name`, new success outcomes |
| `app/voice/gateway.py` | Passes the reminder from `handoff_packet` to the session |
| `app/services/scheduler.py` | `voice_reminder_autoqueue` job every 2 min (only when `VOICE_ENABLED`) |
| `app/routers/voice_admin.py` | `Reminders` settings model and validation |
| `admin/index.html` | Settings section and call-queue labels |
| `tests/voice/test_reminder_calls.py`, `tests/test_migration_112_voice_reminders.py` | New tests |

Versions bumped: `dialog-2026.10.21`, `templates-2026.10.21`.

## 5. Verification

- 27 unit/route tests + 2 real-Postgres migration tests; rollback → reapply → reapply (idempotent) verified on real Postgres.
- Mutation check: breaking the offset choice or retry deadline makes tests fail.
- Admin page JavaScript syntax-checked with `node --check`.
- Full suite: 6,837 passed, 5 skipped.

## 6. Before go-live

1. Deploy (migration 112 runs via `scripts/migrate.py`).
2. Native-speaker review of the new Telugu and Hindi text (gate G-LANG-REVIEW).
3. One live test call on a test clinic: no real Exotel call was placed in this session.

## 7. Known limits / open items

- If a call is answered but the gateway refuses it (capacity, budget at that moment), the reminder may not be retried. This behaviour already exists for lead calls and was not checked or changed here.
- Two appointments on the same phone at overlapping reminder times are called one after the other (one live call per number and purpose).
- Lab-test bookings and walk-ins do not get reminder calls (no slot time / doctor).
- Branch name is not spoken in the call.
