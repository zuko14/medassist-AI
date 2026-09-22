# Session 19 — Patients' Own Words: Language Requests, Test Questions, Firewall Precision

**Date:** 2026-09-22
**Branch:** main (uncommitted at time of writing)
**Migration:** none — no schema change. One new optional key in `conversations.context`: `lab_pending_query` (string).
**Trigger:** production WhatsApp screenshot, 22 Sep, 4:53 PM — "Change my language" answered with "What would you like to do?".

---

## 1. What the screenshot showed, and what the owner asked for

| Patient typed | Bot replied | Should have replied |
|---|---|---|
| "Change language" | Language picker ✅ | (already correct) |
| "Change my language" | "What would you like to do?" + main menu | Language picker |
| "Where is location" | Address ✅ (Session 18) | (already correct) |

The owner's brief went beyond the one phrase: patients should be able to talk freely ("I have sugar, what kind of test can I do?") and get an informative, catalogue-grounded answer — **never medical advice** — with no damage to production workflows.

---

## 2. Root causes

| # | Root cause | Where |
|---|---|---|
| 1 | Only the **exact** guide command `"change language"` was recognised. Any other wording went to the LLM, whose intent list and whitelist had **no language intent**; it came back `unknown` and fell to the main menu. With the LLM down, `"change"` hit `reschedule_appointment`. | `ai_engine.GUIDE_COMMAND_INTENTS`, `detect_intent` prompt + whitelist |
| 2 | A question about tests had **no intent**. "Do you have thyroid test" became `view_services`, which at a diagnostics centre opens the category picker and **throws the question away** — the patient has to retype "thyroid". | `detect_intent`, `_process_state` |
| 3 | A sentence typed into the catalogue search was searched **word for word, all words required**. "I have sugar, what kind of test I can have" needs a test containing *i, have, what, kind, can* — nothing matches, and the patient got `No test matched "…"`. | `_match_lab_tests`, `hybrid_search` (conjunctive) |
| 4 | **Clinical firewall false positives** — drug names matched as substrings of any text, before any state handling. Verified on production code: a patient named **Pandey** ("pan") typing their name, and searches for **lipid panel**, **Vitamin B12**, **Serum Calcium**, **Fasting Insulin**, **HCV genotype** ("eno"), **Adenosine deaminase**, **Venous blood gas** were all answered "I cannot provide medical advice". "Which test should I take for sugar" and "can I take the test on Sunday" were blocked by the `should i take` / `can i take` phrases. | `clinical_firewall.screen_message` |
| 5 | Bug found on the way: a health package's **Details** line was overwritten (`=` instead of `+=`) whenever the test also required fasting. | `_handle_browsing_lab_tests` |

---

## 3. Design: understand freely, act deterministically

The LLM got better at **understanding**; it was *not* given the power to **act** or to **author medical content**. Every action still runs through the existing state machine and its guards (emergency, opt-out, consent, deletion, escalation, help all still run first). This is the production-safe form of "agentic": free-form input, structured and verified execution.

| Layer | What it does | Works with OpenRouter down? |
|---|---|---|
| Deterministic fast paths | Language requests in ~all common wordings (en/hi/te/Hinglish); conversational filler stripped from test questions | ✅ yes |
| LLM classifier (`detect_intent`) | Two new intents — `change_language`, `find_tests` — for phrasings no list anticipated | falls back to keywords |
| LLM term extraction (`extract_catalogue_terms`) | Only when a sentence matched nothing: names the test/organ/condition the patient **named**, in any language → looked up in **the clinic's own catalogue** | returns `[]` → previous behaviour |
| Catalogue | The only source of any test name or price shown to a patient | ✅ |

---

## 4. Changes

### A. `app/services/ai_engine.py`

- **`language_change_request(message)`** → `"en"` / `"hi"` / `"te"` (language named), `"ask"` (change requested, none named), or `None`.
  - A message qualifies only if **every** word is a language name, a language word, a change verb, or from a small helper-word list. So "Change my language", "switch to Telugu", "hindi mein baat karo", "मुझे हिंदी में बात करनी है", "తెలుగులో మాట్లాడండి" are recognised, while "Is the report in english?" (*report*) and "do you have Telugu speaking staff" (*have, staff*) are left to the classifier.
  - "change from english to telugu" → the language after *to/into/in* wins.
- **`detect_intent`**: fast-path 2c — `language_change_request` hit → `change_language`, no LLM call.
- LLM prompt + whitelist: **`change_language`** (all clinics) and **`find_tests`** (only clinics with `lab_test_booking`; a clinic without a catalogue sees the exact intent list it saw before, and a stray `find_tests` is mapped to `view_services`).
- **`keyword_intent_fallback`**: test words → `find_tests` only **after** every existing keyword, so "test report", "book test", "cancel my test" keep their meaning. The three bare `keyword_intent_fallback(message)` calls inside `detect_intent` now pass `clinic` (it was already passed on the injection path).
- **`extract_catalogue_terms(message, clinic)`** — JSON-mode call, 4s timeout, 60 tokens:
  - input sanitised; injection attempts never reach the model;
  - prompt forbids inferring tests from symptoms, medicines, advice;
  - output validated: list of strings, ≤ 3 terms, ≤ 40 chars, strict character set, de-duplicated; **any** failure → `[]`.
- **`generate_response`** (the free-text reply when nothing else understood the patient) now gets `_reply_grounding(clinic)`: *never state a price, test, doctor, timing, address or service not given above*, and end by naming **one command this clinic actually answers** (same rule as the help guide). Output still passes `validate_llm_output`.

### B. `app/services/clinical_firewall.py`

- Latin-script drug names matched as **whole words** (a trailing digit still counts: `dolo650`, `pan40`). Hindi/Telugu names keep substring matching — their vowel signs are not regex word characters, so `\b` is unreliable there.
- **`ANALYTE_NAMES`** = calcium, zinc, vitamin d3, vitamin b12, insulin — lab test names as much as drugs. Blocked only next to a dosage form (tablet, injection, dose, mg, iu…) or a take-verb that isn't about a test.
- `should i take` / `can i take` / `what can i take for` and the first treatment-seeking regex are skipped **only** for a test question with **no dosage form** in it.
- **Unchanged:** every diagnosis phrase ("do I have diabetes", "what disease", "is this cancer"), every medicine phrase, the PCPNDT block, and the output validator.

### C. `app/services/hybrid_search.py`

- `QUERY_FILLER` + `strip_query_filler()` — conversational words that are never a test name on their own (English + common Hinglish). Catalogue words (*blood, full, body, checkup, profile*) are kept.

### D. `app/services/conversation.py`

- **Language handler**: a typed request that names the language is applied through `_handle_selecting_language` — the same path a tapped language button already uses — for consented patients only. Everyone else gets the picker, which leads into the DPDP consent step.
- **`find_tests` routing** in the global escape-hatch block, right after the "book test" keyword check → `_start_lab_booking(..., query=message)`. Guards:
  - not in `_FREE_TEXT_ANSWER_STATES` (`collecting_name`, `collecting_symptoms`, `asking_symptoms`) — "I have sugar" typed as the symptom answer **is** the answer;
  - consent answered (`data_consent is not None`), as with Session 18's `clinic_info`;
  - typed only, never a button; clinic without lab booking → `view_services`, exactly what the question got before.
- **`_start_lab_booking(query=)`**: single centre → search results immediately. Multi-centre → branch picker first, question kept in `context["lab_pending_query"]` and answered from **that centre's** catalogue once picked.
- **`_interpret_lab_query`** — runs only when the literal search matched nothing:
  1. strip filler → search again (free, deterministic: "I have sugar, what kind of test I can have" → `sugar`);
  2. still nothing and ≥ 3 words → `extract_catalogue_terms` → search those.
- **`_match_lab_tests`**: a comma means *either* ("glucose, thyroid") — only when the whole query matched nothing, so every existing search is unchanged. Paging ("More options") re-runs the stored interpreted query, so page 2 stays within the answer.
- **Result body**: an interpreted answer adds *"ℹ️ These are the tests we offer for what you mentioned. Only your doctor can advise which one you need."* (en/hi/te).
- **No match on a sentence** (usually a symptom): *"I can't advise which test you need — a doctor can. Type a test name…, pick from the list, or type talk to staff."* Short misses keep the old "No test matched" reply word for word.
- **Details line** bug fixed (`instructions_line +=`).

---

## 5. Medical-safety line (why this is not advice)

- The bot never **authors** a test recommendation. Every test name and price shown comes from the clinic's catalogue rows.
- The model may only **translate what the patient named** ("sugar" → glucose tests). Symptom → test mapping is refused in the prompt and, if the model returns nothing, the patient is told a doctor decides.
- Interpreted results always carry the "only your doctor can advise" line.
- Medicine and diagnosis requests are still blocked before any LLM call (17 block cases pinned in tests, including `"which test should i take, and what tablet for sugar"` and `"do i have diabetes test"`).

---

## 6. Tests

`tests/test_agentic_understanding.py` — **101 tests**:

- **Language (33):** 17 recognised phrasings incl. the screenshot, Hindi/Telugu scripts, glued postpositions and "from X to Y"; 11 non-requests (report/staff questions, "change my appointment", test names); no LLM call for "Change my language"; LLM `change_language` accepted; named language applied for a consented patient; picker for "Change my language"; picker (not direct apply) without consent.
- **find_tests (14):** offered only to lab clinics and mapped to `view_services` elsewhere; offline fallback runs after existing keywords; menu question becomes the search; never taken from name/symptom answers; waits for consent; clinic without labs → services; question survives the branch picker.
- **Search (13):** the screenshot sentence lists the sugar tests **without** an LLM call and not "Prothrombin **Time**"; Telugu sentence via mocked model terms; symptom sentence → "can't advise", no list filter; short miss unchanged and no model call; literal match unchanged; comma union; a long question read past the 60-char display cap; 6 filler-strip cases.
- **Term extraction (7):** validation and cap; exception / non-JSON / list / non-list terms → `[]`; injection never reaches the model; grounding lists only real commands.
- **Firewall (33):** 16 names/test questions allowed (Pandey, Pankaj, lipid panel, B12, calcium, insulin, genotype…); 17 medicine/diagnosis requests still blocked.
- **Details (1):** package description and fasting line both shown.

---

## 7. Verification

- `tests/test_agentic_understanding.py` — **101 passed** (100 + the long-question test added after review).
- Full suite excluding `tests/test_multi_worker_smoke.py` (boots real uvicorn workers against production — see the standing note on stolen `scheduler_locks`): **2903 passed, 1 skipped, 0 failed** (5m49s).
- After a final edit (interpretation reads the whole question, up to 300 chars; only the echo is capped at 60): search, firewall, clinic-info, hybrid-search and new tests re-run — **244 passed**; every file that touches the lab-list path without the new file — **380 passed**.
- `python -c "import app.main"` — clean.
- Orphan sweep after the runs: no `python.exe` left with a dead parent.

### Test-harness finding (existing, not caused by this session)

Running the new file together with `test_branch_context_integrity.py` / `test_diagnostics_catalogue_experience.py` as a *subset* fails 10 tests. The same 10 fail **without** the new file when `SUPABASE_URL=https://test.supabase.co` is exported (`10 failed, 88 passed`).

Cause: those tests call the full `handle_message`, which takes the distributed phone lock through a Supabase RPC. `app.config` loads the real `.env` unless `SUPABASE_URL` is already in the environment, so in a normal run the lock is taken on the **live** project. The new file sorts first alphabetically and sets the test URL before `app.config` loads, which is what exposed it. In the full suite an earlier file imports `app.config` first, so the result is unchanged there.

Worth fixing on its own: patch `acquire_phone_lock_with_timeout` / `distributed_lock_manager` in those `handle_message` tests (or in conftest), so no test depends on reaching a live database.

---

## 8. Files changed

```
app/services/ai_engine.py              language_change_request, find_tests/change_language intents,
                                       extract_catalogue_terms, _reply_grounding
app/services/clinical_firewall.py      whole-word drug names, analyte rule, test-question carve-out
app/services/hybrid_search.py          QUERY_FILLER, strip_query_filler
app/services/conversation.py           language apply, find_tests routing, _interpret_lab_query,
                                       comma union, lab_pending_query, details-line fix
tests/test_agentic_understanding.py    new, 101 tests
docs/sessions/SESSION_19_AGENTIC_UNDERSTANDING.md
```

---

## 9. Open items

- Changes are **uncommitted**; commit + push to `main` deploys to Render.
- **Not built, on purpose:** a full tool-calling agent loop (LLM choosing and chaining actions). It would put a model between the patient and bookings/cancellations and add seconds per turn; the state machine's guards are what keep this bot safe in production. Revisit if the analytics show `unknown` intents staying high after this change.
- Language change mid-booking still returns the patient to the menu (existing behaviour of the picker path).
- Existing, not changed here: `INTENT_KEYWORDS` substring matching in the offline fallback (e.g. "return" contains "turn" → `queue_status`; "cancel my test booking" → `book_appointment` because `book` is checked before `cancel`). Only reached when OpenRouter is down; worth a whole-word pass with its own tests.
- Latency: `extract_catalogue_terms` is only called for a ≥3-word search that matched nothing literally or after filler stripping. Worst case (timeout + one retry) ≈ 10s on that path; message processing runs as a background task under a renewed phone lease, so Meta's webhook timeout is not at risk.
