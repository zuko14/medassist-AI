# SESSION 57: A WhatsApp-booked patient phones the receptionist; per-hospital isolation; re-audit

**Date**: 2026-11-04
**Branch**: `ccr-f9feccfb-gw9wo2` (on top of session 56)
**Ask (owner)**: a patient who booked on WhatsApp calls for their appointment details, availability, to cancel
or anything else, and the AI receptionist must handle it like the hospital's own front-desk person. Every
hospital / diagnostic centre / clinic (any plan: derma, eye, IVF ...) gets a receptionist that knows and acts for
THAT hospital only. Re-audit everything before the PR to main.

**No migration, no new endpoint, no new permission, no new environment variable.**

---

## 1. Method

A scenario harness drove the real rules + dialog engine with a caller who has a WhatsApp booking, through
everything such a caller says, in English, Telugu and Hindi. Every reply was read as a receptionist would hear it.

## 2. What was wrong, and the fix

| Caller says | Before | Now |
|:-|:-|:-|
| "is my appointment confirmed" / "నా అపాయింట్మెంట్ కన్ఫర్మ్ అయిందా" | read the booking, never said "yes, confirmed" | "... It is confirmed." (a booking under review is **never** called confirmed: "our team is checking it") |
| "I want to change my appointment time", "can I come tomorrow evening instead", "अपॉइंटमेंट का समय बदलना है" | read the booking back (a status question) / started a **second** booking; the Hindi one then also asked about hospital timings ("समय") | moves THIS booking: same doctor, new time; the old one is cancelled only after the new one is verified |
| "I booked the wrong doctor, change it to cardiology" | booked a new one and left the old booking | the booking moves to cardiology; the old one is cancelled after the new one is verified |
| "when will I get my refund", "रिफंड कब आएगा", "paisa wapas kab milega" | transferred to staff at once | read from the caller's own paid bookings cancelled in the last 30 days: refund started → said; cancelled but not refunded / nothing found / lookup failed → "our team will check and call you" (never "no refund is due") |
| "I cancelled my appointment, where is my refund" | answered as an **address** question ("where") | the refund answer above |
| "I paid but did not get confirmation", "payment failed", "I didn't get the payment link" | read the booking, nothing about payment | open payment → the team is asked to check and send a fresh link (callback); paid → "your payment has been received" |
| "I will be late for my appointment", "నేను కొంచెం లేట్ అవుతుంది", "मैं थोड़ा लेट हो जाऊँगा" | read the booking back | "no problem, come as soon as you can, I'll tell reception" + staff note; the call continues ("anything else?") |
| "how long will I have to wait", "ఎంతసేపు ఆగాలి", "कितना इंतज़ार" | not understood / also started a **fees** question ("ఎంత", "कितना") | the queue answer only |
| "the doctor did not come", "డాక్టర్ గారు రాలేదు", "डॉक्टर नहीं आए", "the staff was rude" | listed the hospital's doctors | noted as a complaint; staff call back |
| Reminder call: "no, I'm busy that day, I can't come" | **ended the call** (read as a callback request, since before session 56) | offered a new time for that appointment, as "can't come" always was |

Precision kept (tested): "another day", "change the doctor", "instead" said **mid-booking** are answers, never a
reschedule of an older booking (only phrases naming the appointment / booking count everywhere); "cancel my
appointment and refund my money" is one cancel (the cancel already refunds and says so); "my report did not
come" is a report question; "I didn't come yesterday, can I book again" is a booking; "do you have a late
evening slot" is not "running late".

## 3. Isolation: each hospital's receptionist is that hospital's only

Audited every read and write a call can make (tools, knowledge, lexicon, learning, outbound, store):

| Boundary | How it holds |
|:-|:-|
| Which hospital answers the phone | The dialled Exotel number → `voice_numbers` row; **unique per number** (`uq_voice_numbers_exophone`), so one number can never route to two hospitals. The loaded clinic id is re-checked against it (`gateway.admit`) |
| What the receptionist reads | Every query carries this clinic's id from the call (never from what was said); personal data (bookings, reports, refunds, queue, profile) also the caller's own number |
| What it changes | Bookings / cancels re-read the row under this clinic and this caller before acting; another hospital's booking id, even on the same phone, is refused |
| What it says | Only this hospital's doctors, departments, treatments, tests, branches, timings and staff-written answers; the model that phrases answers sees only these facts and its answer is rejected if it names a doctor or number not in them |
| What it sends | WhatsApp goes out with this hospital's own credentials (no platform fallback, AGENTS.md rule 6) |
| What it offers | Only what this hospital's plan sells (session 56 scope), now pinned for every plan incl. derma, eye, dental (consultations + treatments) and IVF (also its own tests) |

New guard: `tests/voice/test_tenant_isolation.py` runs the real tools on a fake database holding **two
hospitals with the same patient phone**; it applies the filters each query really sets, so a query missing its
clinic filter returns the other hospital's rows. Verified by mutation: removing the clinic filter from the
appointments read fails 2 tests; restored, all pass.

## 4. Re-audit of session 56 (found and fixed before the PR)

| Found | Fix |
|:-|:-|
| A plan with neither consultations nor tests was told "we do tests and scans" | neutral "I can't arrange that on this call" + offer reception |
| Four staff sentences routed to **calling** leads (R3, still needed the typed count, but wrong): "pass me the leads list", "hand me the leads report", "follow up leads list", "show me leads to follow up" | hand-off phrasings must name the receptionist / Kriya; a list, report, export or download is never a lead call |
| `\b` written as a control character by an edit script in a new regex (caught before commit) | fixed; every source file checked for control characters |

Re-verified unchanged: all 1,671 existing Ketu sentences route exactly as on `main` (994 fixture + 677 corpus).

## 5. Verification

| Check | Result |
|:-|:-|
| `tests/voice/test_whatsapp_booked_caller.py` (new, 47) | every row of section 2, te / hi / en; refund read scoped to clinic + caller, unpaid cancels have no refund |
| `tests/voice/test_tenant_isolation.py` (new, 5) | two hospitals, same phone: bookings, reports, refunds, cancel, lexicon, notes, knowledge facts |
| `tests/voice/test_plan_scope.py`, `test_call_acts.py`, `test_assistant_receptionist_handoff.py` | + specialty plans, + reminder busy/can't-come, + lists of leads never a call |
| Voice NLU benchmark (19,120) | 100 % |
| Ketu accuracy gate | DEV 100 · DEV2 93.9 · DEV3 100 · DEV4 100 · DEV5 100 · DEV6 100 · HELDOUT 96.2 % · **0 wrong** (identical to `main` on the old sets) |
| All voice + learning tests; Ketu hand-off, lead-call and accuracy tests | 494 + 127 passed, 0 failed |

## 6. Before go-live (in addition to session 56's list)

* Native-speaker review of the new te / hi sentences: `confirm_note`, `review_note`, `payment_help`,
  `payment_received`, `refund_started`, `refund_pending_staff`, `refund_check_callback`, `not_offered_here`.
* `refund_started` says the refund "reaches your account in a few working days, depending on your bank": confirm
  this matches the hospital's payment gateway wording.
