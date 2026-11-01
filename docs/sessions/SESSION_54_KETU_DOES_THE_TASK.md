# SESSION 54: Kriya Ketu does the task, not a form

**Date**: 2026-11-01
**Branch**: `ccr-ea78ec15-vz8zco`
**Trigger (owner, screenshot)**: "Register a new doctor. Name is Suresh Varma. His department is dermatologist …
fees ₹500 … Kukatpally branch … morning 9 to 11 … evening 6 to 7" opened a form asking the name, specialization
and working days. The owner's rule: Kriya Ketu must DO the task from what is said, across the whole admin panel.

No migration, no new endpoint, no permission change. Every write still shows a preview and needs Confirm; every
new action calls an endpoint the panel already uses, as the logged-in user, with the server's own checks.

---

## 1. The sentence from the screenshot

| | Before | After |
|:-|:-|:-|
| Name | not read ("Name is …" was not a naming form) | **Dr Suresh Varma** |
| Specialization / department | only "department dermatologist" said → specialization asked | **Dermatologist / Dermatology** (one specialty said fills both) |
| Working days | not said → asked | **Mon–Fri**, the default of the Doctors page AND of `POST /admin/doctors` (`DoctorCreate.available_days`); the preview says it was assumed and how to change it |
| Fee, hours, branch | read | read (₹500, 09:00–11:00, 18:00–19:00, Kukatpally) |
| Result | form with 3 fields | **preview → Confirm → doctor added**, verified on the Doctors list |

Mechanism: `ActionSpec.derive(params, text)` fills only what the sentence implies and never overrides a value
that was said (`registry._derive_new_doctor`, `resolve.doctor_department`). A profile with no specialty at all
still asks for it: it cannot be implied.

## 2. Whole-panel audit

`224` write endpoints exist under `/admin`. Before this session Kriya could reach `82`. The rest fall in three groups:

| Group | Examples | Decision |
|:-|:-|:-|
| Must stay with a person | login / logout / password / username; prescriptions, consultation notes, every clinical **signature** (NMC + the zero-AI clinical firewall); file uploads and imports; platform clinic management; hard deletes of records; staff logins (a password is never spoken) | **not automated, on purpose**: Kriya opens the page with the fields pre-filled (H hand-off) |
| Money / inpatient orders / insurance not yet designed | IPD charges and final bills, deposits refund, OPD cashier shift open/close, payment exceptions, claim policies | next round, each needs its own preview + limits |
| Plain admin tasks | below | **done this session** |

New executable actions (all R2: preview, Confirm, one call per write, read-back):

| Action | Says | Endpoint | Who may |
|:-|:-|:-|:-|
| `profile.update` | "change the hospital address to …", "rename the hospital to …", "update the hospital emergency number to …", "set the reception number to …", "turn off patient follow ups", "send follow ups after 5 days", "turn on health check-ins", "our maps link is https://…" | `PUT /admin/profile` (only the fields said; never the WhatsApp routing id or sandbox flag) | admins (`require_admin`) |
| `holiday.delete` | "remove the holiday on 25 December", "25 December is not a holiday anymore" | `DELETE /admin/holidays/{date}` | `HOLIDAYS_DELETE` |
| `doctor_leave.move` | "move Dr Anitha's leave from 5 Nov to 7 Nov", "shift Dr Kiran's leave tomorrow to Friday" | `POST /admin/leaves` then `DELETE /admin/leaves/{id}`: **added first**, so a failure part-way never leaves the doctor without leave; the remove permission is checked before anything runs | `DOCTOR_LEAVES_CREATE` + `DOCTOR_LEAVES_DELETE` |
| `treatment.create` | "add a new treatment Hair PRP under Hair & Scalp for 4000" | `POST /admin/treatments` | `TREATMENTS_MANAGE`, specialty clinics |
| `branch.doctor_unassign` | "remove Dr Kiran from the Madhapur branch" | `DELETE /admin/branches/{b}/doctors/{d}` | `DOCTOR_BRANCH_ASSIGN` |
| `voice.call_number` | "call 9876543210 about knee pain" | `POST /admin/voice/outbound` (opt-outs and duplicates refused by the server; already-queued numbers refused in the preview) | `VOICE_MANAGE`, AI receptionist on |

Not possible today for anyone (no endpoint in the panel): **editing or rescheduling an appointment** (only
check-in and cancel exist). Building it is a product decision (DP-05 kept future staff booking out of v1).

When a form is still needed, the voice orb's spoken reply now fills **the one open free-text field** ("Hair and
scalp" for a treatment's category): only a short reply, never a question, small talk, a command or a negation;
two open fields are never guessed from one reply (`rules.free_reply`).

## 3. Fixes found on the way

* `resolve_service_price` read "for 2 days" as ₹2 (also affected "add a service"): a number followed by days /
  minutes / hours / sittings / beds / patients is never a price.
* "move … leave … to …" tied with the OPD queue move; "emergency number" without "hospital" / "receptionist" is
  still asked (the hospital's number and the receptionist's transfer number are both real settings).
* A test now fails on any repeated key in the registry's tables (a repeat silently drops the earlier phrasings;
  caught while writing this session).

## 4. Verification

| Check | Result |
|:-|:-|
| `tests/test_assistant_autonomy.py` (new, 39) | the screenshot sentence end to end; every new action: preview, nothing before Confirm, exact body, read-back; every refusal writes nothing; neighbours keep their sentences; free reply rules |
| Accuracy gate | DEV 100 % · DEV2 93.9 % · DEV3 100 % · DEV4 (new actions) 100 % · HELDOUT 96.2 % · **0 wrong on every set** |
| All assistant tests + accuracy gate + AGENTS.md gates (tenant lint, scope matrix, adversarial matrix, clinical firewall, webhook, FSM) + UI tests | **3,842 passed, 0 failed** |
| Fixture corpus (994) vs the previous commit | **unchanged** (17 misses before and after; nothing broken) |
| Every sentence in the assistant tests (2,207) vs the previous commit | 33 decisions changed, all reviewed: each is a new action or a doctor profile that now completes; "update the emergency number" kept its old meaning (the receptionist's number) |
| Existing tests changed on purpose | 4 tests that pinned "ask for the working days / department" (`test_assistant_doctors.py::test_spoken_profile_fills_everything_said_and_asks_only_the_rest`, three in `test_assistant_doctor_intent.py`): they now pin the values sent and that nothing is written before Confirm |
