# 00 — Global Constraints (apply to EVERY task)

Every task below implicitly includes this whole file. If a step seems to conflict with a rule here, stop and ask the owner. Do not guess.

## A. Production safety (non-negotiable)

1. **Zero behaviour change for existing plans.** `soloclinic`, `diagstream`, `diagbooking`, `essential`, `polyclinic` and `enterprise` must produce exactly the same:
   - WhatsApp menus;
   - admin panel tabs;
   - API responses, except the two new `/admin/me` keys;
   - database writes.

   Every new code path is gated by `specialty_enabled(clinic)`, and the WhatsApp flow is additionally gated on the clinic having active treatments.
2. **Never widen `appointments.booking_type`.** Treatment bookings are `booking_type='consultation'`.
   - `uq_appointment_active_slot` (migration 064) only covers `'consultation'`.
   - The `appointments_time_required_for_consultation` CHECK (migration 039) rejects any new value.
   - Reminders (`scheduler.py`) and the doctor_id guards (`database.py`, `payment.py`) only run for `'consultation'`.
3. **Never gate specialty behaviour on `has_feature(clinic, "specialty_treatments")` alone.** `enterprise` is a `*` wildcard (`app/services/tenant.py`), so that check is true for every enterprise clinic. Always use `specialty_enabled(clinic)` (created in Task 2).
4. **Schema changes are additive only.** New tables and new nullable columns. No existing column, index or constraint changes, except widening the two plan CHECK constraints exactly as migration 072 did.
5. **Deploy order:**
   1. Migration 077 is applied and verified in production.
   2. Only then is application code deployed.

   Code from Tasks 5–8 selects `treatment_name` and writes `treatment_id`; against a database without migration 077 those queries fail.
6. **Do not weaken the clinical firewall** (`app/services/clinical_firewall.py`). Do not remove or edit any of its phrases or patterns.
7. **Tenant isolation is enforced in application code only.** The app uses `service_role`, which bypasses RLS.
   - Every query on a tenant table must contain `.eq("clinic_id", <verified clinic id>)` **in the same statement**, or use `scoped_query(...)`.
   - An INSERT carries `clinic_id` in its payload and gets the annotation comment `# unscoped: insert_scoped_by_payload` on the line directly above.
   - `tests/test_lint_unscoped_queries.py` enforces this with a ratchet. `TOTAL_BARE_BASELINE` must stay `0`, and `TOTAL_LEGACY_ANNOTATIONS` must not rise above `54`. New annotations may only use reasons from `ALLOWED_REASONS`.
8. **Never expose stack traces in API responses.** Log with `logger.error(...)`, raise `HTTPException` with a human sentence.
9. **Phone numbers in logs:** mask them (`phone[:6] + "***"`, the existing style). Admin-facing WhatsApp alerts and admin notifications may include the full number; the existing payment alerts already do.
10. **Every outbound WhatsApp text goes through the existing `self.whatsapp` / `whatsapp_service` methods.** No new HTTP clients.

## B. WhatsApp limits (Meta Cloud API)

| Element | Limit | Enforce with |
|---|---|---|
| List rows (all sections) | 10 | `manager._page_rows(rows, page, more_id, lang)`; never build >10 rows |
| List row title | 24 chars | `[:24]` |
| List row description | 72 chars | `[:72]` |
| List section title | 24 chars | `[:24]` |
| List header text | 60 chars | `[:60]` |
| Reply buttons | 3 max, title 20 chars | `send_interactive_buttons` already truncates; still write titles ≤20 |
| Interactive body | 1024 chars | truncate to 1020 + "…" |
| Row/button id | 200 chars | ids used here are ≤ 45 chars |

## C. Languages
Patient-facing bot text must exist in `en`, `hi` and `te`, using the `{"en": …, "hi": …, "te": …}.get(lang, en)` pattern already used throughout `conversation.py`. This plan gives the exact strings. Copy them verbatim.

## D. Clinical copy rules (India: NMC ethics, ART Act 2021, PCPNDT Act)
- **No outcome promises.** Never use: painless, pain-free, guaranteed, permanent, cure, success rate, best, risk-free, no side effects, instant results, 6/6, miracle.
- **No sex selection or gender language** (boy, girl, gender) in anything the IVF plan produces or seeds.
- **No medicine names, doses or prices in AI-generated descriptions.**
- **Keep the disclaimer:** "Our specialist will examine you and confirm what suits you."

## E. Testing rules — READ BEFORE RUNNING PYTEST
- **`.env` points at the PRODUCTION Supabase.** Running the app locally, or the full test suite, can leave orphaned worker processes holding production scheduler locks.
- **NEVER run bare `pytest` / the full suite. NEVER run `tests/test_multi_worker_smoke.py`.** Run only the explicit test files named in each task.
- Real-database tests use the `real_pg_conn` fixture (`tests/conftest_db.py`), which starts a throwaway local Postgres through `pgserver` and applies all migrations. They never touch Supabase.
- After every pytest session, run this orphan check (PowerShell) and stop any listed process whose `ParentProcessId` no longer exists:
  ```powershell
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'multiprocessing-fork|uvicorn' } | Select-Object ProcessId, ParentProcessId, CommandLine
  ```
- Tests mock `sb`, `supabase`, WhatsApp and OpenRouter. **If a mock needs more return values than the plan lists, extend the mock. Never change production code just to make a test pass.**

## F. Git
- Work on branch `feat/specialty-plans` (created in Task 1, Step 0). Never commit to `main`.
- One commit per task, with the message given in the task. End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```
- Never use `--no-verify`. Never force-push.

## G. Booking name — already fixed on `main` before this plan (do not redo)
"For Me" and saved-family bookings used to be saved as "Patient", and naming a new family member overwrote the account holder's name. This was fixed separately (Session 04):
- every reader uses `resolve_booking_name(context, patient)` in `app/services/conversation.py`;
- the family-member handler sets `booking_name` and `for_self: False`;
- `_handle_collecting_name` only renames the account holder when `not context.get("is_family")`.

The Part B snippets in `07b-conversation-wiring.md` already match that code. Never read `context["booking_name"]` directly in new code; call `resolve_booking_name`.

## H. Glossary of names used across tasks (must match exactly)

| Name | Kind | Defined in |
|---|---|---|
| `SPECIALTY_BY_PLAN` | `dict[str, str]` = `{"derma": "dermatology", "eye": "ophthalmology", "dental": "dental", "ivf": "fertility"}` | `app/services/tenant.py` (Task 2) |
| `specialty_enabled(clinic: Optional[dict]) -> bool` | function | `app/services/tenant.py` (Task 2) |
| `TREATMENTS_MANAGE` | permission string | `app/services/permissions.py` (Task 2) |
| `specialty_treatments`, `treatment_doctors` | tables | migration 077 (Task 1) |
| `appointments.treatment_id`, `appointments.treatment_name` | columns | migration 077 (Task 1) |
| `is_uuid(value) -> bool` | function | `app/database.py` (Task 3) |
| `get_specialty_treatments(clinic_id, active_only=True) -> list[dict]` | async | `app/database.py` (Task 3) |
| `has_active_treatments(clinic_id) -> bool` | async | `app/database.py` (Task 3) |
| `get_treatment_by_id(clinic_id, treatment_id, active_only=True) -> Optional[dict]` | async | `app/database.py` (Task 3) |
| `get_treatment_doctor_ids(clinic_id, treatment_id) -> set[str]` | async | `app/database.py` (Task 3) |
| `STARTER_TREATMENTS`, `CONCERN_EXAMPLES`, `seed_starter_treatments(clinic_id, specialty) -> dict` | data + async | `app/services/specialty_catalog.py` (Task 3) |
| `PROMISE_PATTERN`, `generate_treatment_description(name, category, specialty, clinic) -> dict`, `rank_treatments_for_concern(concern, treatments, clinic) -> list[str]` | regex + async | `app/services/ai_engine.py` (Task 4) |
| `specialty_flow` module | module | `app/services/specialty_flow.py` (Task 7) |
