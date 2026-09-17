# Session 10 — Care Pathway: who decides the treatment, the patient or the doctor

**Date:** 2026-09-17
**Branch:** main
**Migration:** 081 (`specialty_treatments.care_pathway`)
**Scope:** specialty plans (derma / eye / dental / ivf / multispecialty)

---

## 1. The question that started it

From a sales meeting with an eye hospital:

> "This works for skin — the patient knows they have acne. But our doctor
> examines the patient and *then* writes the treatment. So what does your
> chatbot do?"

It is the correct question, and it applies to eye, dental and IVF. Nobody
books "phacoemulsification with a multifocal IOL" off a WhatsApp menu. They
say "I can't see properly at night."

## 2. What was already true

Worth stating, because the objection implied more was wrong than was:

* the treatment card's button already said **Book Consultation**, not "Book
  Surgery";
* every card already closed with *"Our specialist will examine you and confirm
  what suits you."*;
* a treatment has never been a booking type — migration 077 made it a **tag on
  a consultation**, precisely so slot uniqueness, the time CHECK and the
  reminder jobs keep applying. Every "treatment booking" ever made was a
  consultation;
* the shipped eye catalogue was already mostly *evaluations*: Cataract
  Evaluation, LASIK Evaluation, Retina Consultation, Glaucoma Evaluation.

## 3. What was actually wrong

**1. Two kinds of row looked identical.** "Comprehensive Eye Check-up" and
"Cataract Surgery" were both one tap that books a consultation. A patient
tapping *Cataract Surgery* believed they were booking surgery, and nothing on
the card said otherwise. The admin had no way to say otherwise either.

**2. The menu led with the catalogue.** A specialty main menu ran
`Our Treatments → Find by Concern → Book Appointment`. An eye patient who
simply wants an examination was pushed through a procedure catalogue first.

**3. There was no "I don't know" path.** When the concern search matched
nothing it dumped the category list — which reads as *your* failure to
describe it properly. For a consult-led clinic the honest answer is "book an
examination and the specialist will tell you", and that row did not exist.

## 4. The model

One column, three values, on `specialty_treatments`:

| value | meaning | what the bot does |
| :--- | :--- | :--- |
| `entry` | the first visit | leads the main menu; where "not sure" goes |
| `direct` | the patient may ask for it by name | **today's behaviour, and the default** |
| `assessment_first` | the doctor decides this after examining | information card; its button books an **examination** |

**Nothing is hardcoded per plan**, deliberately. An eye hospital *does* sell
directly bookable items (routine check-up, contact lens fitting) and a derma
clinic *does* have doctor-ordered ones (skin biopsy). The clinic's own
catalogue decides, exactly as the diagnostic service types of session 09 do.

`direct` being the default is the zero-regression property: a clinic that
classifies nothing behaves precisely as it does today, and `care_pathway()`
maps anything missing, blank, unknown or non-string back to `direct` rather
than raising — it runs while rendering the main menu and every card.

## 5. What changed

### Migration 081

Additive column + CHECK, plus a backfill of rows this codebase authored
(`source = 'starter'`, matched on the exact name `specialty_catalog.py` wrote).
No admin can have classified anything yet, because the column does not exist
until the migration runs. Every unlisted name keeps the `direct` default.
Rollback in `migrations/rollback/081_down.sql`.

### `specialty_catalog.py`

`_PATHWAY_BY_NAME` classifies the shipped starters; `_t()` looks each name up.
One reviewable list beside the SQL backfill it must match — and
`test_the_python_and_sql_classifications_cannot_drift` parses the SQL and
fails if they diverge, because otherwise a clinic's rows would depend on which
side of the deploy it onboarded.

Result: ophthalmology 1 entry / 9 direct / 2 assessment_first; dental 1/7/6;
fertility 1/4/7; **dermatology 14 direct + 1 assessment_first (Skin Biopsy)
and no entry** — the live derma tenant's flow does not move.

### `specialty_flow.py`

* `care_pathway()`, `entry_treatments()`, and the three `PATHWAY_*` constants.
* `_card_buttons(treatment, lang)` — takes the row, not its id, because the
  first button now depends on the pathway. `assessment_first` gets
  `trtexam_<id>`; everything else keeps `trtbook_<id>`.
* `show_treatment_card()` — an `assessment_first` card states, above the
  buttons, *"Planned by your specialist after an examination — this is not
  booked directly."* The button alone cannot carry that.
* `start_entry_consultation()` — books the clinic's first-visit row. One entry
  row books straight through; several are offered rather than guessed; none
  falls back to booking the treatment itself (still a consultation) or to the
  ordinary department flow.
* `start_examination_for()` — "Book Examination" on a procedure card. The
  **appointment is the examination**; the procedure travels along as
  `treatment_interest` and lands in the appointment's `symptoms` line as
  `Treatment: Comprehensive Eye Check-up (asked about: Cataract Surgery)`, so
  reception and the doctor are not guessing why the patient came.
* `treatment_menu_rows(lang, has_entry)` — with a first-visit row published the
  menu becomes `🩺 Book Consultation → 🔍 Not sure? Tell us → ✨ What We Treat`.
* `prompt_concern()` — offers a **Book Examination** button, with *"Not sure
  how to describe it? That is completely fine."*
* Concern search: no match now offers Book Examination / What We Treat / Talk
  to Staff and says *"We will not suggest a treatment before a doctor has seen
  you"*; results are framed *"These are treatments our clinic offers that
  relate to X … This is not a diagnosis. Your specialist examines you first."*
  and end with a Book Examination row. A clinic with no entry row keeps the
  old copy and the old category fallback.

### Admin panel

A **"Who decides this treatment?"** select on the treatment form, a Pathway
column in the table, and copy explaining both non-default options. API
validates against the three values and normalises case.

## 6. What was deliberately NOT built

**A guided triage questionnaire that infers what the patient needs.** It was
on the table and was rejected. Asking "how long? one eye or both?" and then
naming a likely condition is a clinical claim, and it is the single fastest
way to lose an eye hospital's trust. The system's value here is routing to the
right sub-specialist with the right prep and the right duration — not
diagnosing. If this is revisited, the answers must be attached to the
appointment for the doctor and must never drive a suggestion.

## 7. What to tell a consult-led clinic

> Kriya never asks a patient to diagnose themselves or choose a procedure. The
> patient describes what they are experiencing — or simply asks to be seen —
> and Kriya books a consultation with the right sub-specialist. The treatment
> list is a directory of what you treat: its job is routing, pre-visit
> instructions and visit duration, not prescribing. Anything your doctor plans
> only after an examination is marked as such; a patient can read about it, but
> its button books an examination, and the message says your doctor confirms
> the plan. What happens after that examination is entirely your call.

## 8. Verification

Full suite: **2446 passed, 0 failed.**

`test_platform_messaging_usage_success` had been failing since `d72fd98` and is
now fixed. It was a stale mock, not a product bug: session 08 made the billing
sweep paginate (`scan_outbound_ledger` must `.range()`, or PostgREST silently
caps the select at 1000 rows), and the test's `table_router` still terminated
the chain at `.gte()`. `.range()` therefore returned a bare `MagicMock`, the
scan rejected it as an unexpected payload type, and every count read zero. The
mock now terminates the paged chain.

### Adversarial audit (scratch harnesses, not committed)

Both flows were driven over hostile catalogue shapes in all three languages,
with every outbound message checked against Meta's limits (10 rows, 24-char
titles, 72-char descriptions, 1024-char bodies, 20-char buttons, 3 buttons,
non-empty and unique row ids) **and** every catalogue item checked for
reachability by actually tapping through headings and pages:

| Catalogue | Result |
| :--- | :--- |
| 1,392 unfiled lab tests | 1392/1392 reachable, 0 violations |
| 938 filed across 3 service types | 938/938 reachable, 0 violations |
| 120 tests across 25 service types | 120/120 reachable, 0 violations |
| empty / single-test catalogues | no empty lists, no crashes |
| 50 treatments, 14 first-visit rows, 120-char names | 0 violations |

Stale and malformed button ids (`labcat_999`, `labcat_abc`, `labtest_nonexistent`,
`trtexam_` on a deleted row) and hostile text (400-char queries, whitespace-only,
non-Latin) produced no crashes. The harness was checked for sensitivity first —
it reports 39 violations against a deliberately malformed message.

**One real defect was found and fixed by this audit**, described in §5 under
`start_entry_consultation`: the first-visit list paged with `trt_more`, which
means "next page of the chosen category". With no category set it fell back to
the category list, so a tenth first-visit row was silently unreachable — the
same class as the PostgREST 1000-row cap and the Meta 10-row truncation this
codebase has fixed before. It now pages with its own `trtentry_more`.

**A second gap was found in the admin panel**: the appointment carried the
`(asked about: Cataract Surgery)` interest but nothing rendered it, so the claim
in §5 that "reception and the doctor are not guessing" was not yet true.
`bookingSubjectCell` now shows the patient's stated reason under the treatment
chip — suppressed when it only repeats the chip, escaped through `esc()`
because it is patient-supplied text.

`tests/test_treatment_care_pathway.py` — 56 tests across: the unclassified
guarantee, menu shape and WhatsApp limits in all three languages, the
doctor-decided card's copy and buttons, examination booking in all four
catalogue shapes, the interest tag reaching `symptoms` and being cleared with
the flow, the concern paths with and without an entry row, starter-catalogue
classification, Python↔SQL parity, and admin validation.

Updated: `tests/test_specialty_whatsapp_flow.py` for the `_card_buttons`
signature. `admin/index.html` inline JS passes `node --check`.

## 9. Deploy order

1. Apply `migrations/081_treatment_care_pathway.sql` — additive, and safe with
   the old build running: nothing reads the column yet, and the backfill only
   touches starter rows.
2. Deploy the application.
3. The eye/dental/IVF clinic classifies anything else from the panel. A clinic
   that does nothing keeps today's behaviour.

---

## 10. Open items

- **Two small reads per main menu for specialty clinics.**
  `treatment_menu_active()` and `has_entry_treatment()` are separate
  `limit(1)` queries. Both are indexed and the table holds tens of rows per
  clinic, so this was left alone; merge them into one read of the active
  pathways if the menu ever shows up in latency traces.
- **The migration backfill matches starter rows by exact name.** A clinic that
  renamed "Cataract Surgery" before migration 081 ran keeps the `direct`
  default and has to classify that row by hand. Correct behaviour — we must not
  guess at a name a clinic chose — but worth knowing during onboarding.
- **The admin panel's pathway labels are English only,** like the rest of the
  panel. Only the patient-facing WhatsApp copy is trilingual.
- **No analytics on `care_pathway`.** We cannot yet answer "how many
  examinations were booked from a doctor-decided card?", which is exactly the
  number that would prove the feature's value to a consult-led clinic. The data
  is there (`treatment_interest` lands in `appointments.symptoms`); nothing
  aggregates it.
- **The multi-entry consultation list wraps** from the last page back to the
  first rather than stopping, which is `_page_rows`' documented behaviour for
  every list in the product. Consistent, if slightly odd on a 2-page list.
- **Triage questions remain deliberately unbuilt** — see §6. If revisited, the
  answers attach to the appointment for the doctor and must never drive a
  suggestion.
- **Not verified here:** no live Meta API calls, no real Supabase. Migrations
  080 and 081 are validated by SQL review and the Python↔SQL parity test, not
  by running them against a database.
