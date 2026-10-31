# SESSION 52: Kriya Ketu "add a doctor" intent, sidebar groups, AI Receptionist mobile layout

**Date**: 2026-10-31
**Branch**: `ccr-ea78ec15-vz8zco`
**Trigger**: the owner's screenshots from production (`medassist-ai-docker.onrender.com/admin-panel`, Aura Hospital):
(1) AI Receptionist Analytics / Calls / Outbound tables running past their cards on a phone; (2) a long
sidebar (OPD OS and IPD OS sections); (3) Kriya Ketu opening "Change a doctor's specialization" /
"Change a doctor's hours" with **Dr. Suresh Kumar** picked when the owner dictated a **new** doctor,
Ramesh Kumar, and an empty "Add a doctor" form after "I mean, register a new doctor."

No migration, no endpoint, no permission and no tenancy change. Nothing new executes without the existing
preview → Confirm step.

---

## 1. Kriya Ketu: say "add a doctor", get "Add a doctor"

### Root causes (reproduced with the owner's exact transcripts against `rules.route`)

| # | Symptom | Cause |
|:-|:-|:-|
| K-1 | "Ramesh Kumar" became Dr. Suresh Kumar | `resolve_doctor` matches any single name word ≥ 80 % similar: the shared surname "Kumar" matched, the different first name was ignored |
| K-2 | "Doctor name **was** Ramesh Kumar, specialization is dermatologist, department dermatology, fees 500, morning 10 to 12, evening 5 to 8" → "Change a doctor's specialization" | Only "doctor name **is**" was a new-doctor sign; the edit action scored higher (`specialization+doctor`, `department+doctor`) |
| K-3 | "At the Dr. Ramesh Kumar, his fees is about 500, department is cardiology, from Ameerpet branch, morning timings 9 to 10, evening timings 6 to 10" → "Change a doctor's hours" | A whole spoken profile with no "add" word; `morning+timings` etc. picked the hours edit |
| K-4 | "I mean, register a new doctor." → an empty form | A correction was routed as a brand-new request; everything said just before was lost |
| K-5 | "fee 500, morning 10 to 12" → fee **₹12** | `resolve_price` read the bare "to 12" of the time window as "price to 12" |
| K-6 | "register a new doctor named Ramesh Kumar" → "Dr Named Ramesh" | "named"/"called" were taken as the first name |
| K-7 | "Sunil Kumar" → working days "Sun"; "Satish" → "Sat" | Day matcher accepted any word starting with a day abbreviation |
| K-8 | "10 o'clock to 12 o'clock", "the department dermatology" (no "is"), "from Ameerpet branch" not read | Window regex did not allow "o'clock"; department needed "is"; "from" was kept in the branch term |

### Fixes (all deterministic, zero-LLM; the clinical firewall is untouched)

| File | Change |
|:-|:-|
| `app/assistant/resolve.py` | `names_someone_else()`: a doctor is dropped from the matches only when **every** mention of their name in the text is directly preceded by a different first name (≥ 4 letters, not an instruction/day/month word, not part of this doctor's name). A dropped match falls back to the doctor picker, never to a guess. Latin script only. Applied inside `resolve_doctor` (the assistant's resolver only; the phone receptionist's `lexicon.match_doctors` is unchanged). |
| | `_NAMED_DOCTOR` accepts "was" / ":" and "doctor named/called X"; `_NEW_DOCTOR(_BARE)` skip "named/called/by name"; `_TITLED_DOCTOR` reads a capitalised "Dr Ramesh Kumar" as the last resort for a new doctor's name; `_NOT_NAME` stops at "was/his/her/name/named/the/also". |
| | `resolve_price`: a "to N" that ends a time window is never a price; a price stated after its word ("fees is about 500", "fee: 700") counts (never followed by %, a clock, "to N", minutes, days, slots). |
| | `no_oclock()` used by `resolve_time_window` / `resolve_bare_windows`; `_STATED_BARE` reads "department dermatology" / "department of cardiology" only for a specialty-looking word; `_BRANCH_NOISE` += from/his/her/is/and/also/of; day words must be real day forms (`_DAY_TAIL`). |
| | `doctor_profile_facets()`: which of fee / specialty / hours / days / branch a sentence states. |
| `app/assistant/registry.py` | "doctor('s) name was", "dr('s) name was" added to `_NAMING_DOCTOR` (they block the edit actions and trigger `doctor.create`). `DOCTOR_EDITS` = the five edit-an-existing-doctor actions. "doctor named" was deliberately **not** made a route trigger ("show appointments of the doctor named Rao" must stay a read). |
| `app/assistant/rules.py` | `prefer_new_doctor()`: a sentence routed only to edit-doctor actions that states **3 or more** profile parts, has **no change verb** (change/update/edit/set/is now/marchu/badlo …), with a **known** doctor list in which **nobody** it names exists → `doctor.create`. `is_correction()`: "I mean / I meant / I said / not that / it's a new …", optionally after no/sorry/actually/wait. |
| `app/routers/assistant.py` | `_corrected()`: when a correction routes to an action, the user's **latest** earlier run is still open (form or unconfirmed preview), is a **different action of the same module**, and its words add details, the details carry over (`rules.fill` on both texts), the old run moves to CANCELLED with `error_code="corrected"` (CAS on its state, so a run confirmed meanwhile is never touched). `_from_llm` applies `prefer_new_doctor` too, so the LLM fallback cannot reintroduce the bug. |
| `admin/index.html` (widget) | A run closed by a correction shows "Replaced below."; `refreshOpenForms` also re-reads open previews. |

### Result on the owner's sentences

| Said | Before | After |
|:-|:-|:-|
| "Doctor name was Ramesh Kumar … 5 o'clock to 8 o'clock." | Change specialization of **Dr. Suresh Kumar** | **Add a doctor**: Dr Ramesh Kumar, Dermatologist / Dermatology, ₹500, 10:00–12:00, 17:00–20:00; asks only the working days |
| "At the Dr. Ramesh Kumar, his fees is about 500 …" | Change hours of **Dr. Suresh Kumar** | **Add a doctor**: Dr Ramesh Kumar, Cardiology, ₹500, 09:00–10:00, 18:00–22:00, branch "ameerpet"; asks specialization and days |
| "I mean, register a new doctor." (after a wrong form) | Empty "Add a doctor" form | The details said above carried over; the wrong form shows "Replaced below." |

Unchanged on purpose: edits of existing doctors ("Dr Kumar evening is now 5 to 8" → Dr. Suresh Kumar's hours;
"change Dr Anitha's specialization to cosmetology"), a multi-detail sentence about an **existing** doctor
(still an edit / "which task?"), and any sentence with a change verb.

## 2. AI Receptionist on phones (Analytics, Calls, Outbound, …)

**Cause**: the page's tables are wrapped in `.table-wrap`, a class with **no CSS rule** (the panel's wrapper is
`.tbl-wrap`), so every table ran past its card's border. The Analytics two-column grid switched on the
**viewport** width (900 px), ignoring the 260 px sidebar; the bulk-import row had an inline
`grid-template-columns` that beat the phone rule.

Measured with Playwright on the real page (mocked API), right edge past the card:

| Width | Analytics | Calls | Outbound |
|:-|:-|:-|:-|
| 390 px before → after | +68 px → 0 | +686 px → 0 | +535 px → 0 |
| 980 px (owner's screenshots) before → after | +124 px → 0 | +372 px → 0 | +221 px → 0 |
| 1280 px before → after | +9 px → 0 | +96 px → 0 | 0 → 0 |

Fixes (`admin/index.html`): `.tbl-wrap, .table-wrap` share the scroll rule; Analytics uses `.vc-grid`
(columns follow the card's own width) with every table in `.tbl-wrap` and the "phrases Kriya did not understand"
as a tidy two-column list; Calls and the outbound queue are stacked label/value cards under 640 px
(`.vc-stack`, `data-label`), a scrolling table above; call rows are keyboard-openable (Enter); the tab strip is
one scrollable row below 1200 px that keeps the active tab centred; call-detail JSON wraps.

## 3. Sidebar: OPD OS / IPD OS / Diagnostics dropdowns

Three collapsible groups replace the two flat label sections: **OPD OS** (OPD Setup, Casualty / ER, Service
Catalogue, Front Desk, Live Queue, Consultation, Doctor Collections, OPD Billing, OPD Analytics), **IPD OS**
(Bed Board, Admissions, Inpatient Chart, Operation Theatre, IPD Receivables, Insurance / TPA, IPD Setup) and
**Diagnostics** (Diagnostic Worklist, Lab Sign-off, Radiology: shared by OPD and IPD and gated by their own flags,
so an OPD-only clinic no longer sees them under an "IPD OS" heading).

- Click / tap / Enter / Space on a group head opens it (chevron turns, count pill shows how many pages are inside);
  clicking a page navigates exactly as before. The choice is remembered per browser.
- The group holding the current page opens by itself (`go()` → `navGroupReveal`); folded, its head is highlighted
  so the user always sees where they are.
- A group with no page this login may see is hidden; a group with **one** page is shown flat (a phlebotomist sees
  "Diagnostic Worklist" directly, never a dropdown of one).
- The links are byte-for-byte the same elements (class, `data-page`, gates, onclick); only wrappers were added.
  Folding uses `grid-template-rows: 0fr` + `visibility: hidden`, **never `display:none`**, because
  `ketuVisiblePages()` reads `getClientRects()`. Verified in the browser: Kriya Ketu's visible-page list is
  **identical to the pre-change page** for six logins (full admin, OPD-only, no OPD, OPD doctor, cashier,
  phlebotomist), and folded links are out of the Tab order.
- Reduced-motion users get no animation.

## Verification

| Check | Result |
|:-|:-|
| New `tests/test_assistant_doctor_intent.py` | 73 passed (resolvers, routing, API end-to-end incl. confirm → POST body, correction carry-over, never across modules, never a confirmed run, LLM path) |
| New `tests/test_admin_sidebar_groups_and_voice_layout.py` | 13 passed (group contents/order, no page lost, ARIA, no display:none fold, go() hook, sync/flat/remember under node, voice layout guards) |
| All assistant tests (`tests/test_assistant*.py`) | baseline 2,475 passed → 2,547 passed after the intent change, 0 failed |
| AGENTS.md gates + full suite (`pytest tests/`) | running at the time of the first commit; result recorded in the follow-up commit |
| flake8 (non-E501) on changed files vs HEAD | no new issues (the repo's existing E501 baseline is unchanged in kind) |
| Browser (Playwright, real `admin/index.html`, mocked API) | 0 px overflow on Analytics/Calls/Outbound at 390/412/980/1280 px; sidebar behaviour and Ketu visible pages as above; no page errors |

## Owner notes

- Deploy is the usual merge to `main` (auto-deploy); there is no migration and no setting.
- The new-doctor rule needs **three** profile parts and a name not on the Doctors list. "Add a doctor …" still
  works with any number of details, and the form asks for whatever is missing; the preview always shows the
  full profile before Confirm, and an existing name is refused ("already on the Doctors list").
- If a spoken branch ("Ameerpet") does not match a branch on the list, the preview says so and lists the
  branches; nothing is saved.
