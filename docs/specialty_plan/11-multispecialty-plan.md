# Task 11 — The `multispecialty` plan: a general hospital that also runs treatments

**Status:** IMPLEMENTED (migration 078)

Hospitals like Aayush advertise emergency & accident care, heart and stroke
care, maternity, dialysis, ICU, laboratory, pharmacy and CT/ultrasound on the
board outside — and *also* sell skin, eye or dental procedures inside. The six
general plans give them departments but no treatments catalogue; the four
specialty plans give them a catalogue but assume the whole facility is one
specialty. This plan is the intersection.

---

## The one idea that carries the plan

`multispecialty` is **specialty-enabled but is NOT in `SPECIALTY_BY_PLAN`.**

`SPECIALTY_BY_PLAN` answers "this facility IS one specialty". It decides three
things: which starter list to seed, which concern examples the WhatsApp prompt
shows, and — via `specialty_flow.is_specialty_plan()` — whether the patient
menu **drops its "Our Services" departments row**. A dermatology clinic should
drop it (one department is not a menu). A hospital with fifteen departments
must keep it.

So the slug goes in a second, separate set:

```python
HYBRID_SPECIALTY_PLANS: frozenset[str] = frozenset({"multispecialty"})
```

and `specialty_enabled()` gains one clause. Everything else follows from that
one decision, which is why the WhatsApp flow needed **no** new code:
`conversation.py` already had the branch (`"Override-enabled general clinics
keep both"`), written for clinics that switch the catalogue on by feature
override. This plan is that case, promoted to a first-class product.

## What the patient sees

| | derma / eye / dental / ivf | **multispecialty** | polyclinic |
| :-- | :-- | :-- | :-- |
| ✨ Our Treatments | ✅ | ✅ | ❌ |
| 🔍 Find by Concern | ✅ | ✅ | ❌ |
| Book Appointment | ✅ | ✅ | ✅ |
| Our Services (departments) | ❌ dropped | ✅ **kept** | ✅ |
| Our Doctors | ✅ | ✅ | ✅ |
| 🧪 Book Lab Test | ivf only | ✅ | ✅ |
| Emergency · Talk to Staff | ✅ | ✅ | ✅ |

Eight rows. Meta allows ten per interactive list and there is no search box in
the sheet, so an eleventh row would be silently dropped rather than scrolled
to — `test_menu_fits_the_whatsapp_list_limit` pins this. Two rows of headroom.

## Files changed

| File | Change |
| :-- | :-- |
| `migrations/078_multispecialty_plan.sql` | Widen `clinics.plan` + `plan_tiers.plan_name` CHECKs by one value; insert the tier row (5,000 msgs, price 0). No table, no column, no backfill. |
| `migrations/rollback/078_down.sql` | Refuses while any clinic is on the plan. No data loss — 078 created no schema. |
| `app/services/tenant.py` | Plan comment; `PLAN_FEATURES["multispecialty"]`; `HYBRID_SPECIALTY_PLANS`; one clause in `specialty_enabled()`. |
| `app/routers/admin.py` | `TreatmentStarterRequest`, `_starter_specialty()`, and the starter endpoint takes an optional body. |
| `app/routers/platform.py` | `valid_plans`, `clinics_by_plan`. |
| `app/routers/clinics.py` | Both `plan` Literals (create + update). |
| `app/services/message_accounting.py` | Fallback tier row. |
| `app/main.py` | `/hospital-panel` added to the existing `specialty_admin_panel()` stack. |
| `admin/platform.html` | Badge CSS, `#planFilter`, `#ccPlan`, `PLANS_WITH_LAB_REPORTS`. |
| `admin/index.html` | Starter-list specialty picker, shown only when the clinic has no single specialty. |
| `tests/test_multispecialty_plan.py` | 42 tests. |

Nothing was needed in `permissions.py` (TREATMENTS_MANAGE already exists),
`tenancy.py` (both tables already tenant-owned), `specialty_flow.py`,
`conversation.py`, `payment.py`, `scheduler.py` or `analytics.py`.

## Feature set

```python
PLAN_FEATURES["multispecialty"] = set(PLAN_FEATURES["polyclinic"]) | {"specialty_treatments"}
```

Derived, not retyped, so the two can never drift — and that is every feature
in `ALL_FEATURES`. Deliberately **not** the `enterprise` `{"*"}` wildcard:

1. a wildcard plan cannot be trimmed per tenant from the owner console;
2. `has_feature()` answers True for features the tenant was never sold;
3. the wildcard is the whole reason `specialty_enabled()` is not `has_feature()`.

Because it carries `lab_reports`, the slug **must** be in
`PLANS_WITH_LAB_REPORTS` in `admin/platform.html` — unlike the four specialty
plans. `test_report_automation_list_matches_the_backend_registry` compares
that JS constant against `PLAN_FEATURES` and fails on drift in either direction.

## Starter treatments

A single-specialty clinic's list is decided by its plan and the request body is
ignored — a dermatology clinic must not load the dental list by posting a
different slug. A multi-specialty hospital has no single list, so it names one:

```python
POST /admin/treatments/starter   {"specialty": "dental"}
```

Validated against the keys of `STARTER_TREATMENTS` itself, so a new starter
list is accepted the day it is added and a typo never reaches the seeder.
There is **no default**: silently seeding fifteen dermatology treatments into
an eye-and-dental hospital is worse than a 400 the admin can act on.

The panel's starter card stays available for these clinics after the first
load (it hides for single-specialty clinics once the catalog fills), because a
hospital adding eye treatments this week will add dental next week. Seeding
stays idempotent and hidden-by-default, exactly as on the specialty plans.

## Admin panel

`/hospital-panel` — one more route onto `admin/index.html`, like
`/derma-panel`. The URL changes nothing about tenancy, features or data; the
page asks `GET /admin/me`. Treatments tabs appear from `specialty_enabled`,
and Departments, Branches, Lab Tests, Reports, Payments, Staff and Insights
stay because the features are there. Specialty gating in the panel is purely
additive (`[data-specialty]` elements are shown, never hidden), so a hybrid
tenant sees the union with no new branching.

AI description and concerns drafts resolve `specialty` to `"general"` for this
plan, which the prompt renders as "an Indian hospital clinic". No
category→specialty guessing map was added: the treatment's category is already
in the prompt, and that is the context that actually matters.

## Onboarding a hospital

1. Owner console → Create Client → plan **Multi-Specialty Hospital**.
2. No starter treatments are seeded at onboarding (the hospital has no single
   specialty to seed). The clinic admin loads the lists they want.
3. Clinic admin: Departments and Doctors as on any hospital plan, then
   Treatments → choose a list → edit prices and descriptions → "Show to
   patients". Nothing reaches a patient until that last step.
4. Link each treatment to the doctors who perform it, or leave it open to any
   doctor.

## Invariants this plan inherits unchanged

A treatment booking is still an ordinary `booking_type = 'consultation'` row
carrying `treatment_id` / `treatment_name` tags. It therefore keeps, with no
new code: the `uq_appointment_active_slot` double-booking index (064), the
time-required CHECK (039), the doctor_id guard and slot pre-check, reminders,
payment capture, refunds, and department analytics.
