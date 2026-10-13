# Kriya Ketu — Playbooks (deterministic multi-step automation)

Parent: [spec.md](spec.md) §2.8 · ADR: [ADR-006](decisions/ADR-006-autonomy-levels-and-standing-instructions.md)

A **playbook** is a fixed, reviewed sequence of registry actions. It is what makes "give one instruction,
Kriya does the whole job" safe and cheap:

- **0 LLM tokens.** Rules pick the playbook, and resolvers fill its slots. The LLM is used only if the
  sentence itself could not be matched (ADR-002).
- **One confirmation per plan** (autonomy level L3). The preview lists **every** write the plan will make,
  with counts. If at execution time any step's real input differs from the preview (a new duplicate
  patient, a booking changed), the executor **stops before that step** and asks again for the remaining
  steps only.
- **Every write step is verified by read-back**, and the report separates done, skipped and failed.
- **Bounded.** Each playbook declares its caps, and a request beyond them is split ("first 100 now").

Notation: `[R]` read step, `[W]` write step, `→ verify` read-back.

---

## PB-01 Doctor leave + affected appointments
Trigger examples: "Dr Srinivas on leave tomorrow", "Dr Rao leave 14 to 16 Oct morning only",
"డాక్టర్ శ్రీనివాస్ రేపు సెలవు".
1. [R] Resolve the doctor (`lexicon.match_doctors`; ambiguity → picker), date range, leave type.
2. [R] `GET /admin/leaves?doctor=` → already on leave? → skip (no unique constraint exists, D-11).
3. [R] Impact: confirmed, non-walk-in bookings (the half-day session filter mirrors `check_doctor_leaves`).
4. Preview: consequence text + **D-01 warning** when the date is today, or tomorrow after 08:00 IST.
5. [W] `POST /admin/leaves` → verify `GET /admin/leaves`.
6. (Only if the user ticks "also cancel and notify now", shown when D-01 applies) [W×N]
   `DELETE /admin/appointments/{id}` → verify each (status `cancelled`, or the refund path's status).
Caps: range ≤ 31 days; step 6 ≤ 25 bookings. Tier: R2 (R3 when step 6 is chosen).

## PB-02 Report delivery failures → resend
1. [R] `GET /admin/reports/deliveries?days=1&state=failed`.
2. Per-row "Resend" (R2) or "Resend all failed (N)" (R3, N ≤ 20). Rows delivered < 24 h ago need an
   explicit tick (duplicate risk).
3. [W] `POST /admin/reports/{id}/resend` → verify the newer attempt in deliveries.

## PB-03 WhatsApp leads → voice receptionist calls (lead conversion)
Trigger examples: "call today's hot leads", "call all dermatology leads from this week",
"leads ki call cheyyi", "इन लीड्स को कॉल करो".
Requires: `ai_receptionist_enabled(clinic)` + `LEADS_MANAGE` + `VOICE_MANAGE` (or clinic_admin).
1. [R] `GET /admin/leads?segment=hot|open&interest=&days=` (paged ≤ 100). The `booked` and `dnc`
   segments are never called.
2. [R] **Dry run.** Proposed new flag `dry_run=true` on `POST /admin/voice/outbound/bulk` (KK-P4-06a). It
   reuses `outbound.enqueue_bulk`'s own rules without inserting, and returns
   `{would_queue[], skipped[{phone, reason}]}`. Reasons are the existing `_SKIP_TEXT`: invalid number,
   duplicate, opted out, never contacted the clinic, already queued, called in the last 7 days. *Until
   KK-P4-06a ships, the preview is labelled "estimate" and the exact skip list is shown after execution.*
3. Preview: N to call, M skipped with reasons, outbound window (`voice_config.outbound.window_start/end`),
   daily cap, and the ETA "calls go out one by one between 10:00 and 19:00; about X today, the rest
   tomorrow". Consent: these are existing WhatsApp contacts, so no attestation is needed. The assistant
   **never** sets `consent_attested=true` itself. Imported lists go through the Voice page (hand-off).
4. Confirm (R2 if N ≤ 10, else R3 with typed N; clinic_admin or VOICE_MANAGE).
5. [W] `POST /admin/voice/outbound/bulk {contacts:[{phone, interest}], consent_attested:false}` → returns
   `{queued, skipped, truncated}`.
6. → verify: `GET /admin/voice/outbound` has `queued` jobs for those phones created after step 5
   (≤ 100 per run so the read-back covers all of them).
Caps: ≤ 100 contacts per run (the endpoint allows 500, but the read-back window is 100).

## PB-04 Lead conversion report (R0)
1. [R] `GET /admin/voice/outbound` (latest 100 jobs: status, attempts, `call_id`).
2. [R] `GET /admin/voice/calls/{call_id}` for completed jobs (≤ 50) → outcome.
3. [R] `GET /admin/appointments?q=<phone>&date_from=<job date>` (≤ 50) → booked?
4. Funnel card: queued → dialled → connected → booked, by interest; plus `GET /admin/leads?segment=booked`.
   Planned improvement: one aggregate read endpoint (KK-P4-06b) to replace the N+1 reads.

## PB-05 Automatic lead calls on/off
1. [R] `GET /admin/voice/settings`.
2. Preview: `auto_leads` off→on, `daily_cap`, window (09:00–21:00 enforced by the endpoint), and "the hourly
   job calls new WhatsApp leads from the last 24 h, skipping booked, opted-out and recently called people".
3. [W] `PUT /admin/voice/settings` with the **whole** settings object read in step 1 and one field changed
   (the endpoint replaces the object). The plan hash includes the settings snapshot, so a concurrent edit
   between preview and confirm triggers a re-preview (D-13).
4. → verify `GET /admin/voice/settings`.

## PB-06 Register patient + walk-in token (OPD live)
1. [R] `POST /admin/opd/patients/duplicate-check {phone, name}` (read-only POST).
2. If candidates exist: pick card (existing patient → skip to step 5; "new person, same phone" → family
   member / new).
3. **Consent card** with two required questions, *asked to the patient by staff*: "Patient agrees to their
   data being stored" (`data_consent`) and "Patient agrees to WhatsApp messages" (`whatsapp_opt_in`).
   There is no default, and Kriya never infers consent from the sentence.
4. [W] `POST /admin/opd/patients` (with `acknowledged_duplicates` when applicable) → verify
   `GET /admin/opd/patients/{id}`.
5. [W] (if a doctor was named) `POST /admin/opd/walk-ins {patient_id, doctor_id, visit_type}` → verify
   `GET /admin/opd/queue` contains the token.

## PB-07 Lab test price change (single or bulk)
1. [R] Resolve tests: by name (fuzzy over `GET /admin/lab-tests`), category, or "all".
2. Compute new prices: absolute (₹350), percent (±x %), rounding rule (to ₹1/5/10, stated in the
   preview). Prices ≤ 0 are refused.
3. Preview: diff table (old → new), count, total change. Single = R2; > 1 = R3 typed count; cap 200.
4. [W×N] `PUT /admin/lab-tests/{id} {price_rupees}` sequentially → verify each from `GET /admin/lab-tests`.
5. Report: updated N, unchanged, failed (with reason). There is no automatic revert. "Undo" = a new PB-07
   back to the old prices, which are kept in the run's preview.
Not blocked: the endpoint already refuses prices ≤ 0 (update validator already exists: `LabTestUpdate.validate_price`; D-10 withdrawn).

## PB-08 Catalogue cleanup
1. [R] `GET /admin/lab-tests/cleanup-suggestions` (deterministic `catalogue_cleaner.analyze_catalogue_quality`).
2. Pick card grouped by the endpoint's three kinds: duplicate deletions, preparation-text fixes, price
   fixes. Nothing is pre-ticked for deletions.
3. [W] `POST /admin/lab-tests/cleanup-apply {delete_duplicate_ids, prep_fixes, price_fixes}`
   (`LabCleanupApplyRequest`, `admin.py:4630`) → verify via `GET /admin/lab-tests`.
Tier R3.

## PB-09 Catalogue import from a price list (attachment)
1. Attachment (≤ 10 MB; the endpoint validates size and magic bytes, `admin.py:4162`) →
   [W-staging] `POST /admin/lab-tests/import-preview` (creates a preview row only).
2. [R] `GET /admin/lab-tests/import-preview/{id}` until ready → diff (added / changed price / unchanged /
   unparseable).
3. [W] `POST /admin/lab-tests/import-apply/{id}` → verify counts via `GET /admin/lab-tests`.
Tier R3. Previews expire through the existing purge (`purge_expired_catalogue_import_previews`).

## PB-10 Treatment catalogue update (specialty plans)
Single: `PUT /admin/treatments/{id}` (price_from_rupees, description, prep, duration, active) — R2.
Bulk percent by category — R3, cap 100. Publish/hide: `POST /admin/treatments/status` — R2. Doctor mapping:
`PUT /admin/treatments/{id}/doctors` — R2. Every step is verified via `GET /admin/treatments`.
AI description: the existing `POST /admin/treatments/ai-description` returns a **draft** shown to the user;
saving is a separate R2 update.

## PB-11 Doctor schedule change
1. [R] The doctor's current row (`GET /admin/doctors`).
2. Compute new session times and slots (same fields as `DoctorCreate`: days, morning/evening start/end,
   slot duration, fee).
3. [R] Bookings from today onward that fall **outside** the new slots → listed as "need attention (not moved
   automatically)".
4. [W] `PUT /admin/doctors/{id}` → verify. A multi-branch doctor: the branch fields are omitted (Session 22).
5. Offer per-booking cancel/notify (separate R2/R3).

## PB-12 Morning briefing (R0) — the daily habit-former
Parallel reads, each shown only if the user's role can read it: `GET /admin/stats`,
`GET /admin/appointments` (today), `GET /admin/leaves` (today), `GET /admin/opd/queue`,
`GET /admin/bookings/pending-review`, `GET /admin/reports/deliveries?days=1&state=failed`,
`GET /admin/leads?segment=hot&days=1`, `GET /admin/voice/outbound`, `GET /admin/notifications/unread-count`.
Zero LLM, with fixed sentences ("6 hot leads since yesterday — call them?"). Each line has one action chip.

## PB-13 End of day (R0)
Confirmed bookings not arrived or checked in today; `pending_payment` holds; failed deliveries; tomorrow's
leaves (D-01 warning); tomorrow's first appointments.

## PB-14 Home collection assignment
`GET /admin/home-collection/visits?date=` → preview the auto-assign result per phlebotomist (leave days
respected by the service) → `POST /admin/home-collection/auto-assign` → verify the visits' `phlebotomist_id`.
R2.

## PB-15 Dental treatment plan
`POST /admin/dental/plans` → `GET /admin/dental/slots` → pick → `POST /admin/dental/plans/{id}/sittings`
(goes through `book_appointment`, so leave and holiday rules apply) → optional
`POST /admin/dental/sittings/{id}/notify` → verify `GET /admin/dental/plans/{id}`. R2.

---

## Playbook rules (enforced in the executor)
| Rule | Why |
|---|---|
| A playbook may only call registered actions; no ad-hoc endpoints | Single policy surface |
| Steps run sequentially; reads may run in parallel | Predictable side effects |
| The first failed write stops later writes → `PARTIALLY_COMPLETED` | Never compound errors |
| Divergence from the preview → stop and re-confirm the remainder | The confirmation stays truthful |
| No automatic retry of writes | Avoid duplicates (T-10) |
| Each playbook has a unit test with a fake API, plus one real-route test on fixtures | Regression safety |
