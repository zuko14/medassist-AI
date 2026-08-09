# Design: Doctor Slot Timings, Department List, New Patient Features, Session-Timeout Fix

**Date:** 2026-08-09
**Status:** Approved for planning

## Problem

1. Admin panel lets an admin add a doctor with a specialization and fee, but cannot set that doctor's actual consultation slot timings — every doctor silently gets the same hardcoded default slots (`09:00–11:30`, `17:00–18:30`) regardless of what's entered in the form.
2. The department dropdown in the doctor form only offers 9 fixed options, too narrow for real multi-specialty Indian hospitals.
3. Patients occasionally get "Your booking session timed out. Here's the main menu to start again." immediately after starting a *new* booking, even though they haven't been idle.
4. The bot lacks a few high-value, India-specific features that are not currently offered anywhere: live OPD queue/token status, reusable family/dependent profiles, and post-discharge health check-ins. (Emergency fast-path already exists and only needs a small staff-alert enhancement.)

## Root cause — session timeout bug

`booking_context_expires_at` (on `conversations`) is set/refreshed only while a patient is mid-booking (`app/services/conversation.py:276-281`), but nothing ever clears it when the patient leaves that flow — whether by completing, cancelling, or abandoning a booking. `update_state()` (`app/services/conversation.py:89-117`), which every "return to main menu" transition routes through, never touches this field.

Sequence that triggers the bug:
1. Patient starts a booking, gets to e.g. `collecting_symptoms`, `booking_context_expires_at` = now+30min, refreshed a few times.
2. Patient abandons (goes silent, or types something that routes back to `main_menu`). The stale expiry timestamp is left in the DB.
3. Days later, patient starts a **new** booking. State transitions `main_menu` → `collecting_name` without touching `booking_context_expires_at` (still the old, long-expired value).
4. On the very next message, the guard at `conversation.py:258` reads `session["state"] in mid_booking_states` (true) and `booking_expires` (the old, already-past timestamp) → fires a false timeout, wiping out the booking the patient just started.

**Fix:** in `update_state()`, whenever `new_state == "main_menu"`, also set `booking_context_expires_at = None` in the same update payload. This is the single shared choke point every "back to main menu" transition already passes through, so one change fixes every call site.

## 1. Doctor slot timings — auto-generated from start/end/duration

**Data model** (`doctors` table, new migration):
- `morning_start TIME NULL`, `morning_end TIME NULL`
- `evening_start TIME NULL`, `evening_end TIME NULL`
- `slot_duration_minutes INT NOT NULL DEFAULT 30`

The existing `morning_slots` / `evening_slots` JSONB columns are unchanged in meaning: they remain the materialized list of `"HH:MM"` strings that `get_available_slots()` (`app/database.py:252`) reads at booking time. **The booking/slot-lookup runtime is not touched** — all the new logic lives at the admin write path, which regenerates `morning_slots`/`evening_slots` from the new start/end/duration columns whenever they're set. This keeps the highest-traffic, most safety-critical code path (live slot availability) completely unchanged and low-risk.

**Slot generation helper** — `generate_slots(start: time, end: time, duration_minutes: int) -> list[str]` in `app/utils/helpers.py`:
- Pure function, stdlib `datetime`/`timedelta` only.
- Walks from `start` to `end` in `duration_minutes` steps, formatting each as `"HH:MM"`.
- Returns `[]` if `start >= end` or `duration_minutes <= 0` (defensive — surfaced as a 422 from the API layer, not a silent empty schedule).

**Admin API** (`app/routers/admin.py`):
- `DoctorCreate` / `DoctorUpdate` gain the 5 new optional fields.
- In `create_doctor` / `update_doctor`: if `morning_start`/`morning_end` provided, call `generate_slots(...)` and set `morning_slots` from the result (same for evening). If a shift's start/end aren't provided, leave that shift's existing/default slots as-is (a doctor can be morning-only by leaving evening fields blank *and* explicitly clearing `evening_slots` to `[]` — the API sets `evening_slots = []` when `evening_start`/`evening_end` are explicitly sent as `null` vs. omitted, so "no evening clinic" is expressible).
- Validation: `morning_end > morning_start`, `evening_end > evening_start`, `1 <= slot_duration_minutes <= 180`, reject with 422 otherwise.

**Admin UI** (`admin/index.html` doctor form):
- Replace the current fee-only-config form with: Morning Start / Morning End (`<input type="time">`), Evening Start / Evening End (`<input type="time">`), Slot Duration (`<select>`: 10/15/20/30/45/60 min), and day-of-week checkboxes (Mon–Sun) for `available_days` — today `available_days` defaults to Mon–Fri and is not editable in the form at all, which is the same underlying gap the user is asking about, so it's fixed in the same pass.
- Small client-side preview text ("→ generates 12 morning + 8 evening slots") computed with the same start/end/duration arithmetic — cosmetic only, server is the source of truth.
- Edit-doctor pre-fill reads the new columns directly (no need to reverse-engineer from the slot array).

## 2. Department list

Converting the fixed `<select>` to a longer fixed list just delays the same complaint. Instead: swap to a native `<input list="deptOptions">` + `<datalist id="deptOptions">` combo box — an admin can pick from a curated list of ~35 common Indian multi-specialty-hospital departments *or* type any custom value. No JS component, no new dependency.

Curated list: General Medicine, Cardiology, Cardiothoracic Surgery, Neurology, Neurosurgery, Orthopedics, Gynecology & Obstetrics, Pediatrics, Dermatology, Ophthalmology, ENT, Dental, Urology, Nephrology, Gastroenterology, Endocrinology, Pulmonology, Oncology, Psychiatry, General Surgery, Plastic Surgery, Radiology, Pathology, Anesthesiology, Emergency Medicine, Physiotherapy, Dietetics & Nutrition, Ayurveda, Homeopathy, IVF & Fertility, Rheumatology, Diabetology, Bariatric Surgery, Vascular Surgery, Andrology, Geriatrics, Sports Medicine.

`department` on `doctors` is already free-text `VARCHAR(50)` — no backend/schema change required, UI-only change.

## 3. New features

### 3a. Live OPD queue/token status
- Migration: `appointments` gains `token_number INT NULL`, `queue_status VARCHAR(20) DEFAULT 'waiting' CHECK (queue_status IN ('waiting','in_consultation','done'))`.
- Admin panel: "Check In" action on today's appointment list → assigns the next sequential `token_number` for that doctor+date (`MAX(token_number)+1` scoped to `doctor_name`+`appointment_date`), sets `queue_status='waiting'`. A "Call Next" action per doctor sets the current in-consultation patient's `queue_status='in_consultation'` and the previous one to `'done'`.
- Bot: new keyword intent ("my token", "queue status", "token number") — looks up the patient's confirmed appointment for today, and if it has a `token_number`, replies with their token, the currently-serving token (`MIN(token_number) WHERE queue_status='waiting' OR 'in_consultation'` for that doctor+date), count of patients ahead, and an estimated wait (`patients_ahead * AVG_CONSULT_MINUTES`, a configurable constant, default 5). If the patient hasn't been checked in yet, reply that they haven't checked in at reception yet.
- Operational note (not a code fix): accuracy depends on reception actually using "Check In"/"Call Next" in order. This is called out to the hospital, not solved in code.

### 3b. Family/dependent profiles
- New table `family_members`: `id`, `primary_patient_id` (FK → `patients.id`, cascade delete), `name`, `relation` (free text: spouse/parent/child/other), `age INT NULL`, `created_at`.
- Builds on the existing `for_self` / `for_family` branch in `conversation.py` (`context["for_self"]`), which today just asks for a one-off name every time and forgets it. When "For Family" is chosen: if the patient has saved family members, list them as quick-reply options plus "Add new person"; otherwise fall straight into today's "type a name" flow and offer to save it afterward ("Save [Name] for future bookings? Yes/No").
- `appointments.patient_name` keeps being populated the same way it is today (no schema change to `appointments`) — `family_members` is purely a convenience lookup to pre-fill that field.
- Admin panel: no new UI needed for v1 (family members are patient-self-service only, managed entirely through the bot).

### 3c. Post-discharge health check-in
- Migration: `appointments` gains `health_checkin_3d_sent BOOLEAN DEFAULT false`, `health_checkin_7d_sent BOOLEAN DEFAULT false` — deliberately separate from the existing `followup_sent` column, which drives a same-day/next-day satisfaction survey (different purpose and timing; reusing it would conflate a clinical safety check with a CSAT ask).
- New APScheduler job in `app/services/scheduler.py`, following the existing `send_24h_reminders`/`send_2h_reminders` pattern: runs daily, queries `appointments` where `appointment_date = today - 3 days` (or `- 7 days`) AND `status IN ('confirmed','completed')` AND the corresponding `health_checkin_*_sent` is false. Sends "How are you feeling after your visit to Dr. X? [Feeling fine] [Still have symptoms]" and marks the flag sent.
- If patient taps "Still have symptoms": bot replies with the hospital's contact number and logs an `analytics_events` row (`event_type='discharge_checkin_concern'`) so it shows up in the existing admin analytics dashboard. No new staff-facing real-time alert channel is being built for this — flagged as a possible future extension, out of scope now (would require a staff-facing notification channel that doesn't exist yet).

### 3d. Emergency fast-path — staff alert enhancement
Already implemented (`_handle_emergency` in `conversation.py:2652`) — keyword-triggered from any conversation state, sends the hospital's emergency number and location to the patient. The only gap: it never notifies hospital staff. Add one send: if a new optional env var `HOSPITAL_STAFF_ALERT_NUMBER` is configured for a clinic, `_handle_emergency` also sends a WhatsApp message to that number with the patient's masked phone and timestamp, so reception can proactively call back. No alert sent if the env var isn't configured (opt-in per clinic, no behavior change for clinics that don't set it).

## Testing

- Unit tests for `generate_slots()` covering exact division, remainder truncation, zero/negative duration, start>=end.
- Unit test reproducing the session-timeout bug (stale `booking_context_expires_at` + fresh `main_menu`→mid-booking transition → confirm no false timeout) as a regression test.
- Admin API tests for doctor create/update slot auto-generation and validation errors.
- Queue token assignment test (sequential per doctor+date, resets across days).
- Scheduler job tests for the two new day-offset queries (mirroring existing reminder job tests).

## Out of scope (explicitly not building now)
- Bed availability check, insurance/TPA lookup (need data admin panel doesn't currently capture — flagged in original brainstorm, not selected).
- Real-time staff-facing alert dashboard/channel for discharge check-in concerns (noted above).
- Ambulance dispatch/tracking (beyond the existing emergency number + location message).
