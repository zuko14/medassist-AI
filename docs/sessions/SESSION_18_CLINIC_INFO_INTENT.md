# Session 18 — Patients' Own Questions: the `clinic_info` Intent

**Date:** 2026-09-20
**Branch:** main (uncommitted at time of writing)
**Migration:** none — no schema change.
**Trigger:** production WhatsApp screenshots from Accumax Diagnostics (+91 92812 35959), 20 Sep, 10:07–10:12 PM.

---

## 1. What the screenshots showed

| Patient typed | Bot replied | Should have replied |
|---|---|---|
| "Where are you located" (state `main_menu`) | 🏥 *What would you like to book?* — the service picker | The centre's address |
| "What are yours timings" (state `main_menu`) | the same service picker | Collection / consultation hours |
| "Where are you located" (state `browsing_lab_tests`) | `No test matched "Where are you located". Showing the full list…` | The address, search left intact |
| "Talk to someone one" | "Connecting you to our staff…" ✅ | (already correct) |
| "Blood glucose" | 2 tests matching ✅ | (already correct) |

The bot was not broken at the edges — it was answering a different question from the one asked, which is exactly the "generic chatbot" behaviour we exist to avoid.

---

## 2. Root causes

Three defects, one theme: **an information question had no destination in this system.**

| # | Root cause | Where |
|---|---|---|
| 1 | Every intent in the classifier was an **action** — book / cancel / view. The LLM whitelist and the keyword fallback had no category for a question *about* the clinic, so a question was force-fit into the nearest action. `"timing"` is a `doctor_availability` keyword and `"services"` a `view_services` one; at a diagnostics-only clinic **both** call `_start_lab_booking` → the service picker. | `ai_engine.INTENT_KEYWORDS`, `detect_intent` whitelist + prompt |
| 2 | `app/services/faq_engine.py` existed but **nothing imported it** — 208 lines of dead code. Worse, its answers were hardcoded fictions ("visiting hours 4:00–7:00 PM", "parking free for the first 2 hours") that are false for most tenants. Wiring it up as-was would have shipped a lie. | `faq_engine.py` (grep: zero importers in `app/`, `tests/`, `admin/`) |
| 3 | `build_system_prompt` read `clinic["address"]` / `["phone"]` / `["emergency_phone"]`, but `provision_clinic` writes those to **`clinic["config"]`** as `address` / `phone` / `emergency_number`. So even the free-text LLM fallback had no address to quote. | `ai_engine.build_system_prompt` |

Screenshot row 3 is the same bug one layer down: the mid-flow branches in `_process_state` hand **any** free text to the open catalogue search, so a question asked mid-search was searched as a test name.

---

## 3. Changes

### A. `app/services/faq_engine.py` — rewritten as a data-backed clinic-info engine

- `detect_topic(message, clinic, lang)` → `"location"` | `"hours"` | `"contact"` | a clinic's own topic | `None`. Phrase-based across en/hi/te. Phrases are deliberately multi-word or unambiguous: this list is consulted while a 1,392-test catalogue search may be open, so `"number"` and `"open"` are absent while `"your number"` and `"are you open"` are in.
- `answer(clinic, topic, lang)` builds from **real data only**:
  - **location** — `branches.address/landmark/maps_link/phone` (every branch listed when there are several), else `config.address` / `landmark` / `maps_link`.
  - **hours** — derived from what is actually bookable: min/max of the active doctors' `morning_slots`/`evening_slots` + `available_days`, and for a lab plan the configured `get_lab_collection_window`. Nothing is a constant.
  - **contact** — `config.staff_phone` → `config.phone` → `whatsapp_number`, plus `config.emergency_number`.
- Returns **`None` when the clinic has no data** for a topic. It never invents a fact.
- `topic=None` (the classifier saw an info question but not which kind) returns every topic with data.
- Per-clinic `config.custom_faqs` / `config.custom_faq_keywords` still work and are matched **first**, so a clinic adds parking/insurance/canteen itself.
- All the fabricated global FAQ text was **deleted**.

### B. `app/services/ai_engine.py` — the `clinic_info` intent

- `detect_intent`: new deterministic fast-path — `detect_topic` hit → `clinic_info`, ahead of the LLM. Zero latency and it survives an OpenRouter outage.
- `clinic_info` added to the LLM prompt and the strict whitelist, with explicit guidance so novel phrasings route correctly and a bare test/treatment name never does:
  > *Do NOT use clinic_info for the name of a test, scan, package, treatment or department on its own ("blood glucose", "MRI brain", "hair fall").*
  > *Use doctor_availability, not clinic_info, when the question names a doctor.*
- `keyword_intent_fallback(message, clinic=None)`: same check, placed **before** the action-intent loop (that loop matches substrings, which is how "timing" won). Second arg is optional — all 20 existing call sites are unchanged.
- `_DOCTOR_WORDS` guard in `detect_topic`: a question naming a doctor skips the hours topic, so **"doctor timings" still reaches the doctor list** exactly as before.
- `build_system_prompt` now reads `config.address` / `landmark` / `phone` / `emergency_number` (top-level keys still honoured for older rows), and adds the landmark line.

### C. `app/services/conversation.py` — the handler

- `_answer_clinic_info(clinic, phone, message, state, lang) -> bool`, next to `_send_help_guide` and following its precedent: it answers and **leaves the conversation state untouched**, so a patient halfway through a booking who asks where to come keeps their place.
- Wired into `_process_state` after the help-guide block and **before** the free-text capture branches (lab search, treatment search, typed patient name) — that ordering is what fixes screenshot row 3.
- Reply carries a contextual next step: `"Type *menu* for all options."` at rest, `"↩️ You can carry on from where you left off…"` mid-flow.

**Three guards that make it safe to ship into production:**

1. **Consent first.** Skipped entirely in `selecting_language` / `awaiting_consent` — DPDP consent is answered before this bot holds a conversation.
2. **The LLM alone cannot interrupt a flow.** Mid-flow, a *deterministic* `detect_topic` match is required; if only the model said `clinic_info`, `_answer_clinic_info` returns `False` and the message falls through to the routing that ran before this change. A misread test name still gets searched.
3. **No data → no invention.** When a clinic has nothing configured for the topic, the reply says so and gives the reception number, rather than reaching the free-text LLM that could produce a plausible-sounding address.

Emergency, opt-out, data deletion, human escalation and the help guide all still run **ahead** of this block and are unaffected.

---

## 4. Why this is not diagnostics-only

Nothing in the change is plan-specific. `location` and `contact` read config/branches every tenant has; `hours` assembles whichever blocks apply — consultation hours for a clinic with doctors, a collection window for one with a lab, both for a hospital that has both. A clinic with neither gets the honest fallback. Custom topics are per-clinic config, so a dental, derma, eye, IVF or women-&-child tenant extends it without code.

---

## 5. Tests

`tests/test_clinic_info_questions.py` — **67 tests, all passing**:

- **Classifier (deterministic path — must hold with OpenRouter down):** both screenshot messages verbatim, plus `"where r u located"`, `"whats ur address"`, `"send me location"`, `"do you work on sunday"`, `"are you open today"`, `"ur timings kya hai"`, `"your phone number please"`, and Hindi/Telugu equivalents.
- **No regressions:** `Our Doctors` / `doctor list` / `हमारे डॉक्टर` / `మా డాక్టర్లు` → `doctor_availability`; `Our Services`, `departments` → `view_services`; `Book Appointment`, `book a slot` → `book_appointment`; `My Reports`, `lab reports`, `blood report` → `view_reports`; `delete my data`, `stop messaging me`, `talk to human`, `heart attack help`, `severe bleeding`, `token status` unchanged.
- **`doctor timings` / `Dr Sharma timings` are never `clinic_info`.**
- **Catalogue items are never info questions:** `thyroid`, `Blood glucose`, `MRI brain`, `HBA1C`, `lipid profile`, `urine sodium`, `COMPLETE BLOOD COUNT (CBC)`, `24 Hrs URINE MICROALBUMIN`, `hair fall`.
- **Answers:** address/landmark/maps quoted from config; every branch listed for a multi-branch clinic; hours derived from real slots (`9:00 AM` – `6:30 PM`, `Mon-Sat`) and from the collection window; `None` rather than an invented address or invented hours; custom clinic topic.
- **Routing:** both screenshot failures reproduced and fixed; mid-search answer leaves the search open and calls no `update_state`; an LLM-only `clinic_info` on `"Blood glucose"` still reaches the search; the same on a typed patient name still reaches the name handler; consent states answer nothing; emergency still wins; a clinic with no data offers a human.

---

## 6. Files changed

```
app/services/faq_engine.py          rewritten (dead + fabricated → live + data-backed)
app/services/ai_engine.py           clinic_info intent, fast-path, prompt, config-key fix
app/services/conversation.py        _answer_clinic_info + _process_state wiring
tests/test_clinic_info_questions.py new, 67 tests
```

---

## 7. Verification

- `tests/test_clinic_info_questions.py` — **68 passed**.
- Full suite excluding `tests/test_multi_worker_smoke.py` (it boots real uvicorn workers against the production DB — see the standing note on stolen `scheduler_locks`): **2803 passed, 1 skipped, 0 failed** (4m04s).
- `python -c "import app.main"` — clean, so the new module-level `ai_engine → faq_engine` import introduces no cycle.
- Orphan sweep after the run: no `python.exe` with a dead parent, so no production `scheduler_locks` were left held.

> Note for the next session: an earlier verification run was stopped mid-flight, which left `tmp_pg_pytest_data/` unclean and made the *following* run error in `test_lab_tests_unique_name_migration.py` with a misleading "antivirus is interfering" hint. That test passes on its own; do not chase it, and avoid `-x` on a full-suite run because it hides the other ~1300 results.

---

## 8. Open items

- Changes are **uncommitted**; commit + push to `main` deploys to Render.
- Topics deliberately not modelled, because no tenant has the data for them today: visiting hours, parking, insurance/TPA, pharmacy, admission and discharge. Each is one `config.custom_faqs` entry per clinic whenever a client asks — no code change needed.
- The admin panel has no UI for `custom_faqs` yet; it is set through clinic config. Worth a Hospital Profile field if clients start asking.
