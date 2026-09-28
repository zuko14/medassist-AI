# Session 26 — Dental Treatment Plans (multi-sitting courses)

**Date:** 2026-09-26 · **Migration:** 089 · **Plans affected:** `dental` only
**Origin:** dental client onboarding call. Dental care is a course of sittings (a Root Canal is 2-4,
implants/aligners many more), the next sitting is fixed by the front desk after the dentist sees the
patient — often with a different dentist — and clinics want WhatsApp reminders to BOTH patient and
dentist plus a review after each sitting.

## Decisions (read before changing anything here)

1. **A sitting IS an `appointments` row** (`booking_type='consultation'`, new `treatment_plan_id`,
   `sitting_number`). Booked only through `database.book_appointment` from `get_available_slots`, so the
   existing slot-uniqueness index, doctor leave, holidays, off-days, check-in, queue, Appointments page and
   payments all apply unchanged. `uq_appointments_plan_sitting_active` = one live booking per sitting number;
   reschedule = cancel + rebook the same number.
2. **Dental-only** = `plan == 'dental'` AND feature `dental_treatment_plans`
   (`tenant.dental_plans_enabled`). `has_feature` alone is not used: the enterprise wildcard would leak it.
   `DENTAL_ONLY_FEATURES` documents the exception to "multispecialty has every feature".
3. **Consent.** A patient imported from old software or a walk-in never opted in on WhatsApp. They are
   messageable only if the desk ticks "patient agreed" on the plan (recorded with who/when) or they have
   messaged the clinic before (a `patients` row). A non-messageable patient's sittings are booked with every
   reminder flag pre-set, so NO job (dental or generic) messages them. The doctor is still messaged.
4. **Zero change to existing jobs.** Sittings are booked with `followup_sent=true` (the review replaces the
   generic follow-up). The dental reminder job (08:30 IST) sets `reminder_24h_sent` only on success, so the
   generic 09:00 reminder is the fallback when a dental template is unapproved or the limit is reached.
5. **Monthly limits are atomic.** `reserve_message_quota()` (plpgsql, conditional `ON CONFLICT` update)
   cannot be overspent by concurrent workers (tested: 4 threads x 10 at a limit of 7 -> exactly 7). A failed
   send calls `release_message_quota()`. Quota-service errors refuse the send (fail closed on spend).
   Bell notification at 90 % and 100 %, exactly once per kind per month (`warned_90/100` CAS).
6. **Review taps** arrive as `message_type="button"` with our payload `dentrev:<sitting uuid>:<1|2|3>`.
   `conversation.py` has one branch after all guards claiming only that prefix. The rating is recorded only
   if the sitting belongs to this clinic AND the sender's phone. "Excellent" can send the clinic's Google
   review link; "Needs improvement" raises an admin bell notification.

## What shipped

| Area | Change |
|---|---|
| DB (089) | `dental_treatment_plans`; sitting columns on `appointments`; `doctors.whatsapp_phone`; `specialty_treatments.default_sittings`; `dental_doctor_digests` (exactly-once doctor digest); `clinic_message_quota_usage` + reserve/release RPCs; `platform_invoices.messaging_addon_paise`. FORCE RLS on new tables. Rollback `rollback/089_down.sql`. |
| Service | `app/services/dental_plans.py` (plans, sittings, sends, quotas, jobs, review reply, Meta template submit/status) |
| Jobs | 08:30 patient reminders · 19:00 + 20:30 retry doctor schedule digest · every 30 min (09-21 IST) review requests |
| Admin API | `/admin/dental/*`: overview, plans CRUD, free slots, book/complete/cancel/notify sitting, patient lookup (WhatsApp + imported), patient history (+ link a WhatsApp booking as sitting 1), settings (admin only). Staff need `DENTAL_PLANS_MANAGE`. |
| Admin UI | "Treatment Plans" page (dental only), plan detail, book sitting from free slots, mark done with notes + amount collected, balance vs estimate, consent capture, message-usage bars, automation settings; doctor WhatsApp field; treatment "typical sittings"; staff permission checkbox; 90 %/100 % banner. |
| Owner | `/platform/dental-messaging` module: usage per kind, est. Meta cost (count x utility rate), limits + monthly add-on (billed on next invoice and in expected MRR), Submit Templates / Template Status against the clinic's own WABA. |
| Templates | `dental_sitting_confirmation`, `dental_sitting_reminder`, `dental_doctor_sitting`, `dental_doctor_schedule`, `dental_sitting_review` (3 quick replies). All UTILITY. |

## Verification

| What | Result |
|---|---|
| `tests/test_dental_plans.py` (gate, permissions, consent flags, slots, quota, fallback, review security, jobs, owner limits, invoice) | 62 pass |
| `tests/test_migration_089_dental.py` on real Postgres (uniqueness, formats, concurrent quota, RLS) + migration applied twice | pass |
| Tenant linter, super-admin scope matrix, adversarial cross-tenant matrix (new routes auto-included) | pass |
| Full suite | 3,368 passed, 0 failed |

**Not verified here:** Meta's approval of the five templates (submit from the owner panel; review can take
up to a day, and Meta may re-categorise the review template as MARKETING), live PostgREST behaviour against
production Supabase, and the two panels in a real browser.

## Deploy + go-live order

1. Apply `migrations/089_dental_treatment_plans.sql` (additive; safe with the current build running).
2. Deploy the code.
3. Owner panel -> Dental Messaging -> **Submit Templates** for the dental clinic (needs its Meta token + WABA id),
   then **Template Status** until APPROVED. Until then patients get the standard reminder and doctors get none.
4. Owner: set monthly limits + add-on price. Admin: add each dentist's WhatsApp number (Doctors page),
   set typical sittings on treatments, set the Google review link.
5. Smoke test with the test clinic: create a plan for your own number, book sitting 1 for tomorrow, check the
   confirmation, the 19:00 doctor digest, mark it done, and tap a review button.

## Follow-up (same day): items resolved

1. **Branch scoping (migration 090).** `dental_treatment_plans.branch_id`. Same rule as appointments
   (`restrict_to_branch` / `enforce_branch_scope`): a pinned receptionist sees and acts on their branch's plans
   and clinic-wide (branch-less) plans only; their new plans are forced into their branch; an admin's chosen
   branch is validated with `resolve_owned_branch`. A branch plan's sitting can only be booked with a dentist
   assigned to that branch, in that dentist's session there (`doctor_branch_session`), and the sitting carries
   `branch_id`/`branch_name` so the branch's Appointments view shows it.
2. **Health check-ins (all plans) — they had never delivered.** Three independent defects: selected
   `status='confirmed'` visits that the 00:30 job had already marked `completed`; sent a free-form message Meta
   refuses outside the 24h window; no feature gate or clinic switch. Fixed: visits confirmed or completed,
   consultations only, dental sittings excluded, `reminders` feature + **per-clinic opt-in (default OFF, so no
   live client starts receiving a new message at deploy)**, STOP respected, approved template
   `patient_health_checkin` with two quick replies (in-window interactive fallback), 2-day retry. Template
   quick-reply taps (`message_type="button"`, payload `checkin_ok`/`checkin_concern`) are now routed to the
   existing handlers via `conversation.TEMPLATE_BUTTON_PAYLOADS`. Admin switch next to Patient Follow-ups.
3. **Found while documenting outbound messages:**
   - The built-in follow-up filled "call us at {{2}}" with `settings.hospital_phone` — one platform-wide number
     for every clinic. Now the clinic's front-desk phone → clinic phone → WhatsApp number (`clinic_callback_phone`).
   - `docs/CLIENT_ONBOARDING_SOP.md` registered `appointment_reminder_2h` with ONE variable while the code sends
     TWO (location, doctor) → every 2-hour reminder on such a WABA fails with Meta 132000. SOP corrected, with a
     note for clients registered from the old text. The SOP now also covers `followup_custom_message_v1` in the
     script, `patient_health_checkin`, the connector `admin_alert` template, the five dental templates, owner
     settings (storage, dental messaging), and an Outbound Message Master Reference.

Tests: `tests/test_scheduler_health_checkin.py` rewritten for the fixed contract (11 cases);
`test_engagement_opt_out` check-in fixture updated (same assertions); 9 branch tests added to
`tests/test_dental_plans.py`.

## Open items

- Which admin sections to hide for dental beyond lab/diagnostics (already hidden): awaiting client input.
- Live clients whose `appointment_reminder_2h` was registered from the old 1-variable SOP text: check their
  WABA's template (Meta → WhatsApp Manager → Message templates) and re-register with 2 variables.


---

## Addendum (2026-09-28): photos of sitting notes + per-patient export (migration 094)

**Why:** front desks do not type sitting notes; the dentist writes them on paper. Staff now photograph the paper.

- **Upload:** "Mark sitting done" / "Notes / photos" modal → *Photo of written notes*. Up to 6 photos per sitting.
  The browser shrinks each photo to ~1600 px JPEG before upload (a 4000×3000 test image went up as ~21 KB).
  Save stores notes + amount **first**, then uploads photos; a failed photo never loses the notes.
- **Server:** `POST /admin/dental/sittings/{id}/photos`, `DELETE .../photos/{photo_id}` (DENTAL_PLANS_MANAGE,
  branch-scoped via `_sitting_or_404`). Type is sniffed from the bytes (JPEG/PNG/WebP only), max 3 MB read with a
  cap, path built from ids only: `{clinic}/dental-sittings/{appointment}/{uuid}.{ext}` in the private `lab-reports`
  bucket. Index rows in `dental_sitting_photos`; if the row insert fails the file is removed. Viewing uses 15-minute
  signed URLs returned with `GET /admin/dental/plans/{id}` (`sittings[].photos`).
- **Export:** plan view → *Download history* → `GET /admin/dental/patient-history/export?phone=`: one CSV row per
  sitting (plans without sittings get one row) with estimate / collected / balance, notes, photo count and review.
  Same query as the history view (`_dental_history`, branch-scoped), formula-safe cells (`client_data.csv_cell`),
  audited as `DENTAL_PATIENT_EXPORT` with a masked phone.
- **Known limits:** photos are not inside the CSV (it lists how many; they are viewed in the panel). A DPDP
  erasure request keeps clinical records (NMC), and photos follow `sitting_notes`, which is also kept.
