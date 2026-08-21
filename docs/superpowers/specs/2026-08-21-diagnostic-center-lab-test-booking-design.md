# Diagnostic Center Lab Test Booking — Design Spec

**Date:** 2026-08-21
**Status:** Approved by user, proceeding to implementation plan
**Trigger:** Production crash — `Accumax Diagnostics` (a diagnostics-only clinic
with zero doctors) offered the standard doctor-appointment WhatsApp flow.
Every attempt to book fell through to a "no doctors available" message that
recursed into itself forever (`_show_department_list` ⇄ `_show_doctor_list`
mutual recursion, no base case when the clinic's only department has no
doctors and `multi_department` is off). Root cause fixed same-day in
`app/services/conversation.py::_show_doctor_list` (bounded fallback to main
menu instead of infinite retry) — see commit for that fix. This spec is the
actual product fix: diagnostics-only clinics should never enter a
doctor/department booking flow at all. They need to book **lab tests**.

## Scope

In scope: a WhatsApp conversation flow letting patients browse a clinic's
lab test catalog and book a collection slot within an admin-configured daily
window, with Razorpay payment collected at booking time, and an admin panel
page for diagnostic-center staff to manage that catalog (manual CRUD + CSV
bulk import) and the collection window. Explicitly **not** in scope: doctor
consultations for diagnostic centers (confirmed not needed), per-test time
slots or capacity limits (real diagnostic centers run walk-in-style
collection within a window — a single shared window per branch is sufficient
and matches actual operations), home sample collection / phlebotomist
dispatch (no current requirement voiced).

## Key architecture decision: reuse `appointments` + `payment_events`, not a new booking table

The original chat-level design proposed a standalone `lab_test_bookings`
table with its own payment methods. Reading `app/services/payment.py` end to
end changed that: the existing `PaymentService` around `appointments` already
provides HMAC-verified webhook processing, atomic idempotent confirmation,
amount-mismatch quarantine to `pending_review`, stale-hold recovery
(`expire_stale_bookings` double-checks Razorpay before expiring — never
silently drops a paid booking), refund with eligibility windows, admin
manual confirm/reject/cancel, and daily reconciliation — all covered by the
security invariants documented at the top of that file and exercised by the
existing test suite. Forking this into a second table means re-implementing
and re-verifying every one of those guarantees independently. That is
strictly worse for reliability, not better.

Instead: `appointments` gets a `booking_type` column (`'consultation'`
default, `'lab_test'` new value) plus nullable lab-test columns. Every payment
code path (`create_booking_with_payment`, `process_payment_webhook`,
`expire_stale_bookings`, `initiate_refund`, `admin_confirm_booking`,
`admin_reject_booking`, `admin_cancel_confirmed_booking`,
`get_daily_reconciliation`) continues operating on one table and needs only
small, additive changes (mainly: branch on `booking_type` to resolve the fee
and the notification copy). This is the single highest-leverage decision in
this spec for hitting "100% reliability" — it means lab test payments inherit
years of production hardening on day one instead of starting from zero.

One consequence worth calling out explicitly: the existing anti-double-booking
constraint is `UNIQUE (clinic_id, doctor_name, appointment_date,
appointment_time) WHERE status IN ('pending_payment','confirmed')`. Lab test
bookings will have `doctor_name = NULL`. Postgres unique indexes treat NULL
as distinct from every other NULL, so this constraint silently does not
restrict lab-test rows — which is exactly the desired behavior (many patients
can book the same collection date), but it is an implicit reliance on NULL
semantics rather than a stated design. The implementation plan must add a
code comment on the migration and on the model documenting this, so a future
developer doesn't "fix" it by making `doctor_name NOT NULL`.

## Data model changes

**`appointments` table** (extend, don't replace):
- `booking_type TEXT NOT NULL DEFAULT 'consultation' CHECK (booking_type IN ('consultation', 'lab_test'))`
- `lab_test_id UUID REFERENCES lab_tests(id) ON DELETE SET NULL` — nullable
- `lab_test_name TEXT` — nullable, denormalized snapshot (mirrors the existing `doctor_name` denormalization pattern — `doctors` has no FK from `appointments` either, by original design)
- Existing `appointment_time TIME NOT NULL` relaxes to nullable, with a `CHECK ((booking_type = 'consultation' AND appointment_time IS NOT NULL) OR booking_type = 'lab_test')` — lab test bookings record only `appointment_date` (the collection date); the collection window itself is a branch-level setting, not stored per-booking
- `department` becomes optional context for lab_test rows (store `'Lab Test'` literal, or leave null) — no behavior currently depends on it being non-null for lab_test rows since it's never used to look up doctors for that booking_type

**New `lab_tests` table** — clinic's test catalog:
- `id UUID PK`, `clinic_id UUID NOT NULL REFERENCES clinics(id)`, `branch_id UUID REFERENCES branches(id)` (nullable — clinic-wide test if unset)
- `name TEXT NOT NULL`, `sample_type TEXT` (e.g. "Blood", "Urine"), `prep_instructions TEXT`, `fasting_required BOOLEAN DEFAULT false`
- `price_paise INTEGER NOT NULL`, `turnaround_hours INTEGER` (informational, shown to patient)
- `is_active BOOLEAN DEFAULT true`
- `created_at`, `updated_at`

**Branch-level collection window** — stored on `branches.config` JSONB (that
column and pattern already exists for per-branch settings; no new table):
`{"lab_collection": {"start": "07:00", "end": "11:00", "days": "Mon-Sat"}}`.
When a clinic has no branches (single-location), the same shape lives on
`clinics.config.lab_collection`.

**`lab_reports` table** (extend):
- `matched_booking_id UUID REFERENCES appointments(id) ON DELETE SET NULL` — best-effort link from an incoming report to the open lab-test booking it fulfills. Populated by matching the most recent `confirmed` `booking_type='lab_test'` appointment for the same `matched_patient_id` at ingestion time. This is additive to `patient_match.py`'s existing safety gate, not a replacement — delivery safety still depends solely on the existing phone+name match logic in `PatientMatchService`, unchanged. The booking link only drives two things: marking the booking `completed` when its report arrives, and an admin-visible "awaiting report" count.

## Conversation flow (WhatsApp)

New states in `app/services/conversation.py`, parallel to the existing
department/doctor states, not a replacement of them:

`browsing_lab_tests → selecting_lab_test → confirming_collection_date → awaiting_payment → confirmed`

**Routing decision** (the actual fix for the production crash): at the "Book
Appointment" main-menu tap, before entering the existing department flow,
check whether the clinic is diagnostics-only — `has_feature(clinic,
"lab_test_booking")` is true AND the clinic has zero active doctors (a live
`doctors` count check, not a static plan assumption, since a `polyclinic`
could have both). If diagnostics-only, enter `browsing_lab_tests` directly;
otherwise the existing flow runs completely unchanged. This means:
diagnostics-only clinics can now never reach `_show_department_list` /
`_show_doctor_list` at all — a second, independent layer of protection
against the crash class, on top of the bounded-recursion fix already shipped.

`browsing_lab_tests`: send an interactive list of the clinic's active
`lab_tests` (name, price, prep note truncated) — same WhatsApp list-message
pattern already used for doctors.

`selecting_lab_test`: patient picks one test → show its full prep
instructions/fasting requirement + the branch's collection window + ask to
confirm a collection date (next N valid days per `days` config, similar to
existing doctor `available_days` handling).

`confirming_collection_date`: patient picks a date → call
`PaymentService.create_booking_with_payment(..., booking_type="lab_test",
lab_test_id=..., lab_test_name=...)` → send the Razorpay payment link.

Payment webhook confirms exactly as it does today (unchanged code path) →
`_notify_payment_confirmed` needs a `booking_type` branch to send lab-test
copy ("Your blood test is booked for...") instead of doctor copy — this is
the one required change to that method, gated on `booking.get("booking_type")`.

## Admin panel

New "Lab Tests" nav item (`admin/index.html`), gated the same way existing
sections are — via `data-feature="lab_test_booking"` — visible only to
clinics with that feature and staff holding the new `LAB_TESTS_MANAGE`
permission (added to `PERMISSIONS` and granted by default to the
`DIAGNOSTIC_OPERATOR` and `LAB_OPERATOR` role presets in
`app/services/permissions.py`, following the exact pattern
`CONNECTOR_MANAGE`/`REPORTS_VIEW` already use for those same roles).

Page contents: a table of the clinic's `lab_tests` with add/edit/delete
(mirrors the existing Doctors page form pattern), a CSV bulk-import button
(new — no CSV precedent exists elsewhere in this codebase, so this
introduces the pattern: expected columns `name,sample_type,price_rupees,
turnaround_hours,fasting_required,prep_instructions`; each row upserted by
`(clinic_id, branch_id, name)`; malformed rows reported back per-row, not an
all-or-nothing failure), and a small form for the branch's collection window
(start time, end time, active days).

New endpoints in `app/routers/admin.py`, mirroring the existing
`/doctors` CRUD block exactly (same `require_permission` dependency style,
same branch-scoping conventions already used for doctors):
`GET/POST /admin/lab-tests`, `PUT/DELETE /admin/lab-tests/{id}`,
`POST /admin/lab-tests/import-csv`, `PUT /admin/branches/{id}/collection-window`.

## Feature/plan gating

New feature `lab_test_booking`, added to `diagstream` and `polyclinic` in
`app/services/tenant.py::PLAN_FEATURES`. The `diagstream` plan's existing
comment ("Diagnostics / lab-only centres — lab reports, no booking") is
updated to clarify it means no *doctor* booking; lab-test booking is exactly
what that plan is for. `essential` and `soloclinic` are untouched — they
model clinics with doctors and don't need this feature by default (though
nothing stops an `essential` clinic from being granted it via per-clinic
override, using the existing override mechanism).

Immediate operational note (outside code scope): `Accumax Diagnostics`'
current plan/feature configuration needs to be corrected in production
(Supabase) once this ships — granting `lab_test_booking` and removing/not
relying on `booking`. That is a data change tracked in the implementation
plan's rollout steps, not a code change.

## Testing

Every new branch gets the smallest test that fails if the logic breaks,
matching the existing suite's style (see `tests/test_department_selection.py`
for the pattern just added for the crash fix):
- Routing decision: diagnostics-only clinic → `browsing_lab_tests`, not
  `_show_department_list`.
- `booking_type='lab_test'` payment creation, webhook confirmation, and
  notification copy — reusing/extending the existing payment test suite's
  fixtures rather than duplicating them.
- The NULL-doctor_name double-booking non-interference behavior (documented
  above) gets one integration-style test against a real Postgres constraint,
  not just a unit mock, since it depends on actual index semantics.
- CSV import: valid rows upsert correctly; malformed rows report per-row
  errors without aborting the whole import.

## Explicitly out of scope for this iteration (YAGNI)

- Per-test time slots / capacity limits — shared daily window covers the
  real operating model.
- Home sample collection logistics.
- Multi-test single booking (cart-style) — v1 is one test per booking; a
  patient needing multiple tests books multiple times. Revisit if diagnostic
  centers report this as friction.
- Doctor consultations at diagnostic centers — confirmed not needed.
