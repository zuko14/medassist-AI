# Session 21 — Menu Escape, Named Treatments, Treatment-Doctor Mapping, Weekly Summary

**Date:** 2026-09-23
**Migration:** none. No new context keys.
**Trigger:** production screenshots from 23 Sep (Aura MultiSpeciality, WhatsApp + admin Insights).

## 1. Reports and root causes

| # | Report | Root cause | Where |
|---|---|---|---|
| 1 | "I need the menu" / "Main menu" → language picker, repeatedly | The patient typed "change my language" the day before and never tapped a language. `selecting_language` never expires (`update_state` exempts it), and `_handle_selecting_language` rejects all typed text. The global escape block also skips that state, so the patient was stuck on the picker. Separately, only the **exact** words in `NAV_KEYWORDS` were read as "menu". | `_process_state`, `_handle_selecting_language` |
| 2 | "Do you provide new born checkup" → "Our Services" department picker, although What We Treat lists **Newborn Check-up** | `view_services` always opens the department list and throws the question away. Nothing looked at the treatment catalogue for a typed question. | global block of `_process_state` |
| 3 | A treatment booking should offer only the doctors mapped to it (all when none are mapped) | `show_treatment_doctors` already did this, but four paths bypassed it: (a) a **typed department name** in `selecting_doctor` reopened the whole department; (b) a **typed doctor name**, or a `doc_<id>` from an **older list**, was accepted without checking the mapping; (c) **Edit booking** on the confirmation showed the department list; (d) **"select another doctor"** (slot full) suggested the whole department. | `_handle_selecting_doctor`, `_handle_confirming_booking`, `_suggest_other_doctors` |
| 4 | Admin: "Weekly operational summary generated successfully", but the card still says "No summary generated yet" | (a) The placeholder row used `source="generating"`, which migration 085's `CHECK (source IN ('ai','template'))` rejects. The insert failed silently, the final `UPDATE` matched no row, and the API still returned `ready`. **No summary was ever saved.** (b) The panel rendered only `status === 'available'`; the API sends `'ready'`. Tests that mocked every DB call as returning no rows hid both bugs. | `weekly_summary.generate_weekly_summary`, `admin/index.html` |

**Bug found on the way:** "select another doctor" rows use the id `doc_{i}_{name}`, and `_handle_selecting_doctor` sent `"0_Dr X"` to the database as a UUID, which raised. Ids in exactly that form now fall back to name matching; `handle_message` already puts the name into `message`.

## 2. Changes

### `app/services/conversation.py`

- **`is_menu_request(message)`** is True for:
  - an exact `NAV_KEYWORDS` word;
  - a short message (≤ 8 words) that names the menu (`menu` / `मेनू` / `మెనూ`) and whose every other word is filler: "I need the menu", "show me the main menu please", "मुझे मेनू चाहिए".

  "menu card for the canteen" does **not** match.
- **Abandoned picker escape.** In `selecting_language`, when the patient **already has a language and has consented**:

  | They type | Result |
  |---|---|
  | A language name | Applied, the same as tapping the button |
  | "change my language", or the classifier says `change_language` | Picker again (unchanged) |
  | Anything else | State moves to `main_menu`, and the message is handled from there |

  First-contact patients and anyone without consent are unchanged: they must pick, and the DPDP consent step follows.
- **Menu request checked first** in the global escape block, so a classifier reading "I need the menu" as `view_services` can't divert it. The lab-name capture, the treatment-search capture and the lab-search exit words all use `is_menu_request`.
- **Named-treatment answer.** `specialty_flow.answer_named_treatment` runs before the existing routing when all of these hold:
  - the message is typed (not a button);
  - intent is `view_services`, `find_tests`, `unknown` or `book_appointment`;
  - state is `idle`, `main_menu`, `selecting_department` or `selecting_doctor`;
  - consent is given.

  It returns False, leaving behaviour exactly as before, when the clinic has no treatment menu, the question names nothing, or nothing matches.
- **Treatment doctor mapping enforced on every path:**
  - no department matching while `treatment_id` is in context;
  - any chosen doctor is checked against `specialty_flow._treatment_doctors(clinic, treatment, branch)`; an unmapped doctor gets the treatment's list again;
  - Edit booking → `show_treatment_doctors`;
  - other-doctor suggestions → the treatment's doctors only.

### `app/services/specialty_flow.py`

- **`service_question_terms(message)`** — `hybrid_search.strip_query_filler`, then drops words that never name a service (`services`, `treatments`, `facility`, `provide`…). "What are your services" → `""`.
- **`named_treatments(treatments, query, names_only=False)`** — strict matching:
  - **every** word (≥ 3 letters) must be found, or all the words run together ("new born checkup" ⊂ "newborncheckup" = "Newborn Check-up");
  - title hits come first, then the admin's order;
  - `names_only` searches the name alone. Bookings and unclassified text use it, so "I have fever" still starts a normal booking even though a concern mentions fever.
- **`answer_named_treatment(...)`:**
  - one match → the treatment card, introduced with "✅ Yes, we offer this at our clinic:";
  - several matches (up to 9) → the existing "Treatments that may help" list, now shared through `_send_treatment_matches`;
  - **no LLM call**; non-specialty plans return before any DB call.
- **`show_treatment_card(..., intro=None)`** — new optional intro line.
- **Specialty rule kept (master memory, Session 10):** the bot only names what the clinic lists. For doctor-decided procedures the card still says "planned after examination" and offers Book Examination.

### `app/services/weekly_summary.py`, `app/routers/admin.py`, `admin/index.html`

- **Placeholder row** uses `source="template"`, which the CHECK accepts. An empty `summary_text` marks it as not generated yet.
- **Save.** If the final `UPDATE` matched no row, the full row is `INSERT`ed. If that also fails, the endpoint returns **503** ("could not be saved") instead of success.
- **`GET /admin/insights/summary`** treats a row with empty text as `not_generated`, so an interrupted generation never shows as a blank "ready".
- **Panel:**
  - renders `status === 'ready'`;
  - shows "AI summary" / "Standard summary" and `updated_at` (the old code read `generated_at` / `model_used`, which the API never sent);
  - text still goes through `formatSimpleMarkdown`, which escapes it.

## 3. Tests

### `tests/test_session21_patient_questions.py` — 48 tests

**Menu and language picker:**
- menu phrasing, positive and negative;
- the screenshot flows ("I need the menu", "Main menu") from an abandoned picker;
- a language typed on the picker is applied;
- the picker is still required without a language or without consent;
- a menu phrase leaves the lab search.

**Named treatments:**
- `service_question_terms`, and `named_treatments` strictness;
- the newborn card; several named treatments are listed;
- a generic services question is unchanged and reads no catalogue;
- a symptom booking is not hijacked;
- mid-booking and free-text-answer states are skipped;
- non-specialty plans are untouched.

**Treatment-doctor mapping:**
- the mapping filters doctors, with the all-doctors default;
- an unmapped doctor from an older list is refused;
- a typed department does not reopen the department;
- a mapped doctor is accepted;
- a suggestion-row id is matched by name;
- Edit booking and "select another doctor" stay within the treatment.

### `tests/test_session21_weekly_summary.py`

- the placeholder's `source`;
- insert when the `UPDATE` matched no row;
- 503 when nothing could be saved;
- `GET` of an empty placeholder returns `not_generated`;
- the panel's status contract;
- on **real PostgreSQL**: the rows the code builds are accepted, and the pre-fix `"generating"` row raises `CheckViolation`.

### `tests/test_weekly_insights_summary.py`

Three generation tests mocked the final save as matching no row and still asserted success, so they encoded the bug. They now use a PostgREST-shaped mock: reads return empty, writes return the written row.

## 4. Operational notes

- **Existing summaries:** none. The insert always failed, so no row was ever saved and nothing needs migrating. The first generation after deploy works.
- **Rate limit:** at most 3 generations per clinic per IST day. It was never actually enforced before, because no row existed to count against.
- **The patient stuck on the picker** is freed by their next typed message after deploy. No data fix is needed.
