# Session 35 — Receptionist acts on a one-sentence request

**Date:** 2026-10-12 · **Branch:** `main` (uncommitted) · **Migrations:** none

## Evidence

Live call `CALL-20261011-3F59FF` (te-IN) plus every `NOT_UNDERSTOOD` live phrase from the last 30 days,
replayed through `nlu_rules.understand`.

| Caller said | Kriya did | Root cause | Fix |
|---|---|---|---|
| "బుక్ చేయు జనరల్ మెడిసిన్ రేపు సాయంకాలం 5 గంటలకి" | "ఉదయం కావాలా, సాయంత్రం కావాలా?" | `సాయంకాలం` missing from period words; "5 గంటలకి" not a time without one | `dates.PERIOD_WORDS` variants; `parse_clock_time` accepts o'clock / గంటలకి / बजे markers, number words (ఐదు, पाँच, five, సిక్స్), ఐదింటికి, ఐదున్నర / साढ़े, పీఎం / पीएम |
| "Monday 5 o'clock" (English) | same re-ask | no o'clock marker | same |
| "ചെയ്യൂ." / "ഉം ചെയ്യൂ." (Telugu in Malayalam letters) | "Which language do you want?" | STT script misfire | `nlu_rules.to_telugu_script` (Malayalam/Kannada → Telugu, ≤3 words only), applied in `session.handle` next to the Gurmukhi mapping; "do it" forms in `SHORT_AFFIRM` |
| "డాక్టర్ అర్జున్ రెడ్డితో సిక్స్ ఓ క్లాక్" | time lost | English numbers in Telugu script | as above |
| (latent) exact time taken | "There are no slots on Monday" (false) | `find_slots(clock=…)` returns only that exact time | `dialog._wf_booking`: re-query the same day, say `time_not_free`, offer the 2 nearest times |

Also: when the exact time asked for is free, `present_one` opens with `yes_sentence` ("అవునండి, ఖాళీ ఉంది.") —
it answers "is it available?" first.

## Deliberately unchanged

One spoken "yes" to the read-back (doctor, time, fee) before `book()` stays mandatory (policy.py) even when the
caller said "book it" up front: the caller has not yet heard the doctor or fee.

## Verification

- `pytest tests/voice tests/test_reception_learning.py tests/test_clinical_firewall.py` → 341 passed.
- 6 new dialog regressions fail on the pre-change code and pass after.
- `tests/voice/test_dialog.py::FakeTools.find_slots` now honours `clock` like `KriyaTools.find_slots`
  (the fake ignoring it is why the "no slots" bug was invisible).
- `scripts/voice_eval.py --check` → gates pass (19,120 cases).
- Versions bumped: `nlu-rules`, `dialog`, `templates` → `2026.10.12`.

## Native-speaker review (gate G-LANG-REVIEW)

New strings: `yes_sentence`, `time_not_free` (te/hi/en) in `app/voice/responses.py`.
