# SESSION 53: Kriya Ketu intent accuracy across the whole admin panel

**Date**: 2026-11-01
**Branch**: `ccr-ea78ec15-vz8zco`
**Ask (owner)**: Kriya Ketu must understand what staff say, for every task in the panel, not only doctors:
"above 98 %", production grade, without breaking any existing workflow.

No migration, no new endpoint, no new action, no permission change. Every write still goes through the
existing preview → Confirm step and the existing permission / feature checks.

---

## 1. How accuracy was measured (and why the numbers can be trusted)

Kriya Ketu routes a sentence in two layers: zero-token **rules** (`app/assistant/rules.py`), then, only on a miss
or a tie, **one LLM classification** (`app/assistant/llm.py`) whose answer is re-resolved by the same rules.
Only the rules can be measured offline, so the numbers below are the rule layer alone (the LLM fallback then
catches part of the remaining misses; `scripts/assistant_intent_eval.py --live` measures both together).

`tests/ketu_corpus.py`: 630 staff sentences across all 169 actions, English / Hinglish / Tenglish, typed and
dictated, texting shorthand, polite forms, word-order changes, and requests that must NOT be acted on.

| Set | Size | How it was used |
|:-|:-|:-|
| DEV | 231 | written from the action summaries only, before reading any trigger; tuned against |
| DEV2 | 82 | the busiest desk work in shorthand / Indian-English / reordered speech; tuned against |
| DEV3 | 185 | written as a blind set before any fix; scored 80.0 % blind; its 3 wrong actions were then read and fixed, so it became a tuning set |
| **HELDOUT** | **132** | written after all tuning, scored once, never tuned against (shares no sentence with the others, enforced by a test) |

Outcomes: **correct**, **ask** ("which did you mean?", safe), **none** (not understood → LLM fallback on the
server), **WRONG** (a different task: the one number that must be zero).

## 2. Results (rule layer, zero tokens)

| Set | Before | After |
|:-|:-|:-|
| DEV | 86.1 % · 8 wrong | **100 %** · 0 wrong |
| DEV2 | 50.0 % · 5 wrong | **93.9 %** · 0 wrong (misses: "CBC 250", "botox price 9000"-style fragments, left to the LLM on purpose) |
| DEV3 (first blind set) | **80.0 % blind** · 4 wrong | 100 % · 0 wrong (tuned after reading it) |
| **HELDOUT (blind)** | — | **95.5 % blind** (126 / 132) · 1 wrong · 2 ask · 3 none; 96.2 % · **0 wrong** after the wrong action was fixed |
| Existing fixture corpus (`tests/fixtures/assistant_corpus.json`, 994 phrases incl. near-miss negatives) | 19 misses | **17 misses, 0 newly broken** |

The blind set's single wrong action ("reports waiting for pathologist sign off" → the *sign a report* hand-off,
which executes nothing but is the wrong task) was fixed **after** the blind score was recorded; the 95.5 % above is
the honest pre-fix number. Of its other misses, one "ask" is the intended OPD-or-IPD vitals question (no bed or
token said); the 3 "none" go to the LLM fallback on the server.

**About "98 %"**: on the sentences it was tuned on, the rule layer is at 98–100 %; on genuinely new sentences it is
95.5 % on its own, with **0 wrong actions** after the fix. The end-to-end number (rules + LLM) needs the live key:
run `OPENROUTER_API_KEY=… python scripts/assistant_intent_eval.py --live` (a few rupees). Real-world accuracy also
depends on speech-to-text quality, which no offline test can measure; the eval script makes it cheap to re-measure
with real transcripts (add them to `tests/ketu_corpus.py`).

## 3. What changed

### Router (`app/assistant/rules.py`)
| Change | Why |
|:-|:-|
| Texting shorthand expanded (`resolve.expand_shorthand`: tmrw, appt, pls, doc, hrs …) | "dr anitha leave tmrw" was not understood. **Never inside a quoted message or after a colon**: text sent to a patient is sent exactly as typed (tested) |
| A doctor "not coming" on a stated day is a leave (`absence_as_leave`: will not come / won't be available / nahi aayenge / raru / on vacation) | "kal Dr Kiran nahi aayenge" was refused as *negated*. Needs a doctor AND a date; "Ravi will not come tomorrow" is untouched |
| Negation: a discontinuation with its own stop verb ("we don't do ESR anymore, stop the test") and "not on leave … remove it" are not refusals | both were refused; every other negation is still refused (tested: "Dr Rao is not on leave tomorrow", "don't cancel …", "mat karo") |
| Words inside a message are content, not instruction (quoted, or after a colon with a messaging cue) | "tell Ravi on whatsapp: clinic closed tomorrow" created a **holiday**; "message Ravi 'your report is ready'" became a report resend |
| `re:` pattern triggers (shape matters, weight of a 3-word phrase) | "Dr Kiran evenings 6 to 9" needs session + hours together; a loose word-set ("dr … mornings … to") turned "assign Dr Kiran to the Madhapur branch mornings" into an hours change |
| `ActionSpec.keep` (said outright overrides `unless`) and `asks_about` (a read may contain a change verb, in a question only) | "bed 9 patient going home today, start discharge" became an expected-discharge edit; "which bookings would the leave cancel" could not be asked |
| `resolve_tie()` in the router, **after** switched-off modules are dropped | "change CBC price to 350" asked lab test / treatment / service every time; now the item decides (CBC, profile → lab; PRP, botox → treatment; ward charges → service). Unknown items ("Gold Plan") and radiology words (a diagnostic centre may keep X-ray in its lab catalogue) still ask. A lab-only clinic never gets the treatment action |
| "how do I …" with no written answer, about something Kriya does | was "I can't do that"; now explains Kriya can do it (with a preview) and names the page; a read just answers |
| "retry" is a change verb | "retry the failed reports" *showed* them instead of resending |

### Registry (`app/assistant/registry.py`)
One block at the end, `PHRASINGS` (+ `_MORE`, `_THIRD`): added phrasings and exclusions per existing action, each
re-validated, triggers de-duplicated by normalised form (a duplicate counted twice and out-weighed other actions).
Deliberate limits kept from the original design (the fixture corpus pins them): no bulk cancel ("cancel all /
today's appointments" stays unrouted), a first "send the report" is not a resend, a bare page name is navigation.

### LLM fallback (`app/assistant/llm.py`)
The prompt now gives two real phrasings per action, says instructions may be Hindi / Telugu / mixed with
shorthand and STT slips, that a doctor not coming is a leave, that "how do I" is help, that a new doctor's profile
is `doctor.create`, and that a message's text is not the instruction. The user turn names the open page.
~4.1 k tokens per fallback call (was ~2.2 k), only on a rule miss.

### Tools
* `scripts/assistant_intent_eval.py`: per-set accuracy (`--show` lists misses; `--live` adds the real LLM).
* `tests/test_assistant_intent_accuracy.py`: CI gate. **0 wrong on every set**, floors DEV 99 / DEV2 92 / DEV3 99 /
  HELDOUT 95 %, plus unit tests of every mechanism above.

## 4. Existing tests changed on purpose (2)

| Test | Change | Reason |
|:-|:-|:-|
| `test_assistant_treatments.py::test_a_clinic_with_both_is_asked_which` | asks with "Gold Plan" (no catalogue evidence) instead of "PRP"; new `…_reads_the_catalogue_from_the_item` pins PRP → treatment, CBC / profile → lab | the tie is now resolved from the item's name; an unknown item still asks |
| `test_assistant_runs.py::test_llm_resolves_a_miss_into_navigation`, `…_gets_only_the_visible_pages` | a sentence the rules still miss | "who messaged us on whatsapp" is now understood by the rules, so it no longer exercises the LLM path |

## 5. Verification

| Check | Result |
|:-|:-|
| All assistant tests + accuracy gate + AGENTS.md gates (tenant lint, scope matrix, adversarial matrix, clinical firewall, webhook, FSM) + UI tests | **3,794 passed, 0 failed** |
| Fixture corpus (994) | 0 newly broken, 2 fixed |
| Every sentence in the assistant tests (1,903) old vs new | 64 decisions changed, all reviewed; every change reaching a write is the task the sentence asks for (and still needs Confirm) |
| flake8 (non-E501) on changed files | no new issues |

## Owner notes

* Run `python scripts/assistant_intent_eval.py --live` once with the production key to get the end-to-end number.
* Add real transcripts that went wrong to `tests/ketu_corpus.py` (DEV sets for fixing; keep HELDOUT blind).
* `ASSISTANT_LLM_MAX_TOKENS` (200) is for the answer only; the longer prompt costs input tokens on misses only.
