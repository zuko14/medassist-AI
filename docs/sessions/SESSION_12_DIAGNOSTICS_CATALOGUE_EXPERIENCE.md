# Session 12 — Diagnostics catalogue experience, the "Hi" bug, and a help guide

**Date:** 2026-09-18
**Branch:** main
**Migration:** 083 (`lab_tests.description`)
**Scope:** diagnostic plans (diagstream / diagbooking) and every clinic that
books lab tests; the help guide and re-subscribe apply to every plan.

---

## 1. What was reported

Screenshots from the live Accumax Diagnostics bot and admin panel:

1. **"Exit", then "Hi" → "🔍 73 test(s) matching 'Hi'".** The patient could not
   get back to the menu.
2. **The main menu only offered Book Lab Test / Emergency / Talk to Staff** —
   nothing said the centre sells health packages, X-rays, MRI or CT.
3. **The admin Test Catalog is one list of 1,392 rows**, every one showing the
   grey "Lab Tests" fallback. Importing per section and adding a test per
   service type already existed (session 09), but nothing was filed.
4. **Insights says nothing about diagnostics** — no packages booked, no
   interest, no priority tests.
5. **Patients do not know the commands** — how to cancel, how to stop
   follow-ups.

## 2. Root causes

**(1) was a state-machine gap, not the classifier.** In `browsing_lab_tests`
the search guard steps aside for a `greeting` intent — but that state is not
in the list the global "greeting → main menu" rule covers, so the state
machine handed "Hi" straight back to `_handle_browsing_lab_tests`, which
searches whatever is typed. "Hi" was searched **regardless of what the LLM
returned**. Separately, "Hi" was sent to the LLM at all, and sometimes came
back `unknown`. ("Exit" becoming an opt-out was the LLM's classification;
"stop" is the documented opt-out word.)

**(2) and (3) were the same fact:** Accumax's catalogue has **0 of 1,392
tests filed** under a service type (checked read-only against production).
Session 09's headings only appear with two or more service types, so the bot
correctly showed one list — the classification simply had never been done,
and doing it by hand for 1,392 rows is not realistic.

**Two more live gaps found while building (5):**
* the bot's own "I didn't understand" reply says *"type 'help'"* — nothing
  answered `help`;
* the opt-out reply says *"Message us anytime to re-subscribe"* — nothing ever
  set `opted_in` back to true.

## 3. What changed

### The "Hi" bug
* `ai_engine.GREETING_WORDS` / `is_greeting()` — whole-message greetings in
  en/hi/te ("Hi", "Hii", "Good morning", "नमस्ते", "హాయ్"), punctuation and 👋
  trimmed. Never a substring: "hi" is inside "thiamine".
* `detect_intent` fast path: a greeting never reaches the LLM.
* `_handle_browsing_lab_tests` treats a greeting (or a `greeting` intent) as
  "take me home". `specialty_flow.handle_treatment_state` gets the same rule.

### Service types on the WhatsApp main menu (diagnostics-only clinics)
* With ≥ 2 service types the single "Book Lab Test" row becomes one row per
  type — `🧪 Lab Tests (Pathology)`, `📦 Health Packages`, `📷 Radiology &
  Imaging`, `🧲 Scans (CT / MRI)`, `Cardiac & Special Tests` — each with its
  count. More than seven types: six plus "🔬 All services".
* One service type (every unfiled catalogue) keeps "Book Lab Test" —
  **Accumax's menu does not change until it files its catalogue.**
* Row ids are `labsvc_<heading, lowercased>`, resolved against the catalogue
  *as it is at tap time*: a renamed heading opens the heading list.
* The chosen type survives multi-branch selection (`lab_category` in context).
* Headings are cached 60 s per worker; the classifier clears the cache.
  A catalogue read failure falls back to "Book Lab Test" — the menu never breaks.
* Icons are read off the centre's own wording; a title that would exceed 24
  characters drops the icon rather than a word. (🩻 was avoided: it renders as
  a blank box on older Android.)
* Hospitals (not diagnostics-only) keep "🧪 Book Lab Test" — their menu has no
  room for five more rows.

### One-click catalogue classification
* `app/services/lab_classifier.py` — deterministic rules, first match wins:
  Scans (MRI/CT/PET/HRCT/CBCT) → Health Packages → Radiology (X-ray,
  ultrasound/ultrasonogram, Doppler, mammography, BMD/DEXA, "AP/LAT view") →
  Cardiac & Special (ECG, 2D echo, TMT, EEG, PFT, Holter…) → Pathology.
  No LLM: same name, same answer, nothing leaves the building.
* On Accumax's real 1,392 names: **1,152 pathology, 140 scans, 94 imaging,
  6 cardiac, 0 packages** (it sells panels and profiles, which are pathology).
  The first dry run misfiled X-rays written as "hand AP/LAT view", every
  "ULTRASONOGRAM" and BMD; the rules were fixed against those real names.
* `POST /admin/lab-tests/auto-classify` — `apply=false` previews (counts +
  examples, writes nothing); `apply=true` writes in 200-id chunks with
  `.eq(clinic_id).is_("category","null")` **in the UPDATE itself**, so a
  heading a person chose — even between preview and apply — is never replaced.
  `LAB_TESTS_MANAGE`, audit-logged, covered by the cross-tenant route matrix.

### Package details (migration 083)
* `lab_tests.description` (≤ 500 chars) — "what a package includes / what a
  scan covers". Admin form field with counter; CSV aliases `details`,
  `includes`, `tests included`, `inclusions`, `components`, `parameters`,
  `package details`; the template carries it. Same **no-wipe rule** as the
  heading: a file without the column leaves descriptions untouched. A create
  with no details omits the key, so it never depends on the column.
* The WhatsApp test card prints `📝 Details: …`.

### Admin panel
* Test Catalog **service-type tabs** with counts (All · each type · Unfiled);
  the old dropdown is the hidden state holder, so Import CSV and Add Lab Test
  still pre-fill from the open tab.
* **✨ Organise your catalogue** card while anything is unfiled: Preview →
  table of proposals with examples → Apply.

### Insights — Diagnostics performance
Only for clinics with `lab_test_booking`. Tiles: tests & scans booked,
diagnostics revenue, service types opened, tests viewed. Tables: per service
type (bookings, revenue, patients interested, % who booked after viewing),
top booked tests & packages, and **most viewed — follow-up opportunities**
(opened on WhatsApp, not booked). Interest comes from two new events the bot
now logs: `lab_category_viewed` and `lab_test_viewed`. A booking is counted
under its test's *current* heading, so filing the catalogue re-files history.

### Help guide and re-subscribe (all plans)
* `help`, `how to use`, `guide`, `commands`, `?`, `मदद`, `సహాయం` — from any
  state, typed only, state untouched — and a **❓ How to use** row at the end
  of every main menu *while there is room* (never pushing Emergency out of
  Meta's 10).
* The guide lists only what the clinic's plan does (no "book a doctor" for a
  lab), in en/hi/te, under 1,024 characters. Every command it names is tested
  to be one the bot answers.
* `start` / `subscribe` / `resume` re-subscribes **an opted-out patient only**.

## 4. Files

**New:** `migrations/083_lab_test_details.sql`, `migrations/rollback/083_down.sql`,
`app/services/lab_classifier.py`, `tests/test_diagnostics_catalogue_experience.py`,
this file.

**Modified:** `app/services/ai_engine.py`, `app/services/conversation.py`,
`app/services/specialty_flow.py`, `app/services/analytics.py`,
`app/routers/admin.py`, `admin/index.html`.

**Tests updated deliberately:** menus that pin exact row ids now end in
`menu_help` (`test_specialty_conversation_wiring.py`,
`test_multispecialty_plan.py`); heading titles carry icons
(`test_lab_test_categories.py`); the Insights column list gained
`lab_test_id` (`test_specialty_booking_payment.py`).

## 5. Verification

`tests/test_diagnostics_catalogue_experience.py` — the production sequence
through the real `handle_message` with the LLM forced to answer `unknown`;
greeting recognition and non-recognition ("thiamine", "hiv"); the LLM never
called for "Hi"; menu rows for 1 / 3 / 12 service types inside Meta's limits;
a catalogue read failure; hospitals untouched; stale and renamed row taps;
the type surviving branch selection; titles never cut mid-word; the help
guide per plan and language under 1,024 chars; every named command real; help
from three states without changing state; the help row at exactly 10 rows;
re-subscribe for opted-out only; 19 classifier rules from real names; preview
writes nothing; apply is clinic-scoped and NULL-only and clears the menu
cache; details validation, CSV column and no-wipe rule; the card showing
details and logging interest; Insights arithmetic including unpaid bookings
and malformed event metadata.

**Mutation check:** with the "Hi" fix reverted, all 4 production-sequence
tests fail. Restored.

**Full suite** (`tests/` minus `test_multi_worker_smoke.py`): **2575 passed,
1 skipped, 0 failed** in 243.9s. No project python / uvicorn / pytest process
was left running afterwards. Panel inline JS passes `node --check`.

The classifier was also dry-run **read-only** against Accumax's production
catalogue (select of `name, category` only; nothing written).

## 6. Deploy order

1. Apply `migrations/083_lab_test_details.sql` (additive; safe with the old
   build — nothing reads the column yet; no index, so no lock on
   `analytics_events`).
2. Deploy the application.
3. Accumax: Lab Tests → **Organise your catalogue** → Preview → Apply. The
   WhatsApp menu shows the service types within a minute. Add package details
   from the edit form or with a `details` CSV column.

## 7. Open items

- **Service-type headings are the centre's own words, so not translated** —
  unchanged from session 09.
- **Insights interest starts from this deploy.** No view events existed
  before, so "patients interested" and "most viewed" fill in going forward.
- **Insights' diagnostics section ignores the branch filter** for catalogue
  and interest (events carry no branch); bookings respect it.
- **"Exit" is still classified by the LLM**, and it said opt-out. The guide
  teaches *stop* / *start*; making "exit" deterministic either way was left
  alone because both readings are defensible and it touches consent.
- **Accumax has no health packages in its catalogue.** The Health Packages
  row appears the moment it adds one (panel or CSV).
