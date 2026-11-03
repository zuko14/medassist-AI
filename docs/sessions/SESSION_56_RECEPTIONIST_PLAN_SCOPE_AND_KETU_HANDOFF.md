# SESSION 56: AI Receptionist plan scope, call handling, LLM-to-rules learning; Kriya Ketu hands work to it

**Date**: 2026-11-03
**Branch**: `ccr-f9feccfb-gw9wo2`
**Ask (owner)**: the AI receptionist must serve only what each client's plan offers (a diagnostic centre's
receptionist handles diagnostics only, a hospital's that hospital only), handle hard real-world situations in
Telugu / Hindi / English, learn from what it got wrong, and Kriya Ketu must hand work to it (leads, calls,
reminder calls) from what staff say. Production: nothing existing may break.

**No migration, no new endpoint, no new permission, no new environment variable.** Every Ketu write still
shows a preview and needs Confirm. Every voice change is deterministic (no LLM writes speech or decides a
transition).

---

## 1. Receptionist: each hospital's plan decides what it offers (`app/voice/scope.py`, new)

**Before**: the voice receptionist never looked at the plan. A diagnostic centre (`diagstream` / `diagbooking`,
no doctor booking) answered "book an appointment" with the doctor flow ("which doctor?", then "we don't have a
…"); its lead calls asked "shall I book an appointment?"; its clinical refusal offered "a doctor's appointment";
a clinic whose plan has no lab booking (e.g. `essential`, `soloclinic`) still took lab-test bookings by phone,
although WhatsApp never offers them.

**Rule** (the same one WhatsApp uses: `conversation._is_diagnostics_only`, `_send_help_guide`):

| | |
|:-|:-|
| `labs` | `has_feature(clinic, "lab_test_booking")` |
| `doctors` | `has_feature(clinic, "booking")` and not (labs and the clinic has no active doctors) |

| Plan (examples) | Kind | What the receptionist does |
|:-|:-|:-|
| `polyclinic`, `multispecialty`, `womenchild`, `enterprise` with doctors | hospital | **unchanged** |
| `diagstream`, `diagbooking`, or labs with no doctors | diagnostic | "appointment" = a **test** booking; "fees" = "which test?" then its price; menus, help, clarify, lead greeting and the clinical refusal speak of tests; a doctor request is told honestly "we do tests and scans" and offered a test |
| `soloclinic`, `essential` | clinic | a test booking/price request is told "we don't take test bookings here" and offered a doctor's appointment |
| no doctors and no labs | none | told honestly, offered reception |

Safety of the change:
* `FULL` (everything on, exactly the old behaviour) whenever the clinic is unknown, its plan slug is not one
  this build knows, or the doctor read fails: **a database hiccup never narrows a call**.
* Decided once per call from the clinic row (`CallContext`), never from anything said. Logged on
  `CALL_STARTED` (`data.scope`); versions now carry `scope`.
* Only a plan with both consultations and lab booking reads the doctor list (one extra read per call).
* Reschedule is untouched (it only acts on the caller's own booking); a test booking being moved is no longer
  described as "already paid" (`reschedule_staff_handoff`).

Found on the way (all plans): after "Which test?", answering "CBC" was read as a price question, which
restarted the task and **dropped the day already given**. Naming the test now continues the booking.

## 2. Receptionist: what people say about the call itself (`nlu_rules`, `dialog._call_acts`)

| Said (te / hi / en, native or romanised) | Before | Now |
|:-|:-|:-|
| "mujhe call mat karo", "మళ్ళీ కాల్ చేయకండి", "don't call me again" | **"call mat karo" was read as YES** (on a lead call, a booking started) | sorry + "we won't call you again", call ends, staff told, **never dialled again** |
| "wrong number", "రాంగ్ నంబర్", "galat number" | not understood → misses → transfer | apology, call ends, staff told, never dialled again |
| "I'm driving", "busy", "call me in the evening", "ఇప్పుడు కాల్ చేయకండి, తర్వాత చేయండి" | inbound-style "team will call you" + "anything else?" | lead call: "sorry to disturb, our team will call at a better time", ends, staff note; reminder call: "please come on time", ends |
| "I'll be 15 minutes late", "కొంచెం లేట్ అవుతుంది" (reminder call) | not understood | "come as soon as you can, I'll tell reception", ends, staff note |
| "who is this?", "ఎవరు మాట్లాడేది", "kaun bol raha hai" (any call) | not understood (a miss) | "This is Kriya from {hospital}" + the pending question again; never a miss |
| "mat karo", "బుక్ చేయకండి", "don't book it" | the trailing do-word could win as YES | a negated do-word is never a yes |

Rules that keep it precise (tested): "don't call me **now** / later / busy / a time" is busy, never a
do-not-call; "who is the doctor" is not "who is this"; "late" means late only on a reminder call; on an
**inbound** call "busy, call me back" is still the existing callback request and "wrong number" acts on nothing.

**Never dialled again** without a schema change: the call ends with outcome `do_not_call` / `wrong_number`
(kept in `voice_calls` for analytics anyway); `outbound._eligible` (single calls, dial-time recheck, auto leads,
reminders) and `enqueue_bulk` refuse a phone with such an outbound call, **even with a staff consent
attestation**. Nothing else changes: the patient's WhatsApp opt-in is not touched (a spoken "don't call" is not
a WhatsApp STOP); inbound calls from that number are answered as always. To call such a person again, staff
can ask them to call in; there is no override switch in this session.

## 3. Learning: the LLM teaches the rules (`session._harvest`, `reception_learning`)

When the zero-token rules miss and the LLM fallback maps the phrase to one of **this** hospital's departments,
the call worked, but slowly (1-3 s), at a cost, and it fails whenever the model is down. That turn is now
harvested as `unmapped_entity` (already allowed by migration 106). The existing 6-hourly distiller proposes a
lexicon entry from the clinic's own catalogue; staff approve it in Learning; from the next call the rules
handle the phrase instantly, offline and free. A group that was understood but names nothing in the catalogue
is skipped: staff are never asked to write an FAQ answer for something that worked. Human approval, PII
scrubbing, safety screening and 30-day retention are unchanged.

## 4. Kriya Ketu hands work to the receptionist

Measured first (rules layer): "transfer the leads to the AI receptionist" → **`ipd.transfer`** (a bed move);
"make the AI receptionist call Ravi" → **`staff.role`**; calling one lead by name and every sentence about
reminder calls were not understood.

| Action | Says | Endpoint (existing) | Risk / who |
|:-|:-|:-|:-|
| `leads.call` (more phrasings) | "transfer the leads to the AI receptionist", "leads ni AI receptionist ki ivvu", "start lead calls", "follow up the leads by phone" | `POST /admin/voice/outbound/bulk` | R3 (typed count), VOICE_MANAGE |
| `voice.call_lead` (new) | "make the AI receptionist call Ravi", "have kriya call Sita about thyroid test", "receptionist tho Ravi ki call cheyinchu" | `POST /admin/voice/outbound` | R2, VOICE_MANAGE |
| `voice.reminder_calls` (new) | "turn on reminder calls", "reminder calls 1 day and 2 hours before", "call patients half an hour before their appointment", "reminder calls band karo" | `PUT /admin/voice/settings` (only `reminders` changes) | R2, VOICE_MANAGE |

`voice.call_lead` finds the lead by name exactly as `lead.message` does: no match / several matches / a
do-not-contact lead / a call already queued are refused before anything is sent; the lead's own interest is
used unless one is said; no phone number is stored in `assistant_runs`. `voice.reminder_calls` keeps saved
times when only "on" is said, refuses times outside 15 min - 1 day or more than 3, and reads the setting back.
`ipd.transfer` and `staff.role` no longer take sentences about leads / the AI receptionist / calls.

## 5. Verification

| Check | Result |
|:-|:-|
| `tests/voice/test_plan_scope.py` (new) | plan table incl. overrides, unknown plan, failed read; diagnostic / clinic-only / none / hospital conversations; session decides scope from the clinic row |
| `tests/voice/test_call_acts.py` (new) | every phrase above in te / hi / en; lead, reminder and inbound behaviour; never-again in `_eligible` and bulk |
| `tests/test_reception_learning.py` (+7) | rescue harvested only when the model found a clinic department, never in test mode; distilled to lexicon; never an FAQ |
| `tests/test_assistant_receptionist_handoff.py` (new, 38) | resolvers, routing incl. the two former wrong routes and their neighbours, both actions end to end: preview, nothing before Confirm, exact body, read-back, every refusal writes nothing, permission |
| Ketu accuracy gate | DEV 100 % · DEV2 93.9 % · DEV3 100 % · DEV4 100 % · DEV5 100 % · **DEV6 (new) 100 %** · HELDOUT 96.2 % · **0 wrong on every set** (identical to before on the old sets) |
| Every existing routed sentence (994 fixture + 677 Ketu corpus) old vs new | **0 decisions changed** |
| Voice NLU benchmark `scripts/voice_eval.py --check` | 19,120 cases, 100 % (te, hi, en, romanised te / hi) |
| AGENTS.md gates (tenant lint 4, scope matrix, adversarial matrix, clinical firewall, webhook, FSM) + all assistant + voice + learning tests | **4,341 passed, 0 failed**; tenant lint 4 passed |
| flake8 (non-E501) on changed files vs the previous commit | no new issues |

Existing tests changed on purpose: three bulk-import tests in `tests/voice/test_lead_calls.py` mock the database
as a fixed sequence of replies; each now includes the one new read (nobody said "don't call"). Their
assertions are unchanged. `tests/test_migration_112_voice_reminders.py` needs `pgserver` (a real Postgres),
which could not be installed in this environment; it fails the same way on the previous commit.

## 6. Before go-live

1. Native-speaker review (gate G-LANG-REVIEW) of the new Telugu and Hindi sentences in `app/voice/responses.py`:
   `greeting_outbound_lab`, `offer_test_booking`, `no_doctors_here`, `no_tests_here`, `clinical_refusal_lab`,
   `clinical_refusal_plain`, `offer_self_help_lab`, `help_menu_lab`, `clarify_lab`, `ask_info_topic_lab`,
   `reschedule_staff_handoff`, `do_not_call_ack`, `wrong_number_sorry`, `busy_ack`, `busy_ack_reminder`,
   `running_late_ack`, `who_am_i`.
2. Plans that change behaviour: a clinic on `essential` / `soloclinic` (no `lab_test_booking`) whose voice
   line was taking lab-test bookings will now say it does not; switch `lab_test_booking` on for that clinic in
   the owner console if it should (the same switch WhatsApp follows).
3. One test call per kind (hospital, diagnostic centre) from the admin Test Console.

## 7. Deliberately unchanged

* "Are you a robot?" is still answered truthfully (`ai_disclosure`). Sounding natural is the goal; denying
  being automated is not offered (consumer-protection and DPDP exposure for the hospital).
* Learned phrases still need a staff click. Auto-approval would let patient speech change live behaviour.
* Reminder calls still cover confirmed doctor appointments with a time (a diagnostic centre's test bookings
  have no time slot).
