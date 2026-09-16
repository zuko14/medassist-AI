# Session 04: Booking name fix ("Patient" instead of the real name)

**Date:** 2026-09-16
**Agent:** Claude Code (Opus 5)
**Branch / commits:** `main` working tree, **uncommitted** (owner to review and commit)
**Plan tasks covered:** none. This is a production bug fix that affects all plans. The specialty plan docs were updated to match.

## 1. Intent for this session
The owner asked to permanently fix the bug found in Session 03: WhatsApp bookings made through "Who is this appointment for?" ("For Me" or a saved family member) were confirmed and saved with the name **"Patient"**.

## 2. What was done
| Item | Result | Evidence |
|---|---|---|
| Root cause | Found | `_handle_selecting_family_member` wrote `context["patient_name"]`. The confirmation screen and both booking writers (paid and unpaid) read `context["booking_name"]`, which was missing, so they fell back to `"Patient"`. "For Me" also wrote the greeting placeholder `"there"` when the account had no name. |
| Second bug, same flow | Found and fixed | "+ Someone Else" set `is_family` but not `for_self=False`. `_handle_collecting_name` used `context.get("for_self", True)`, so naming a family member **overwrote the account holder's own name** in `patients`, and their later "For Me" bookings carried the wrong name. |
| Failing tests first | Done | `ImportError: cannot import name 'resolve_booking_name'`: the expected failure |
| Fix | Done | See §3 |
| Regression tests | 102 passed, 1 failed | The one failure is pre-existing (see §4) |

## 3. Files changed
- `app/services/conversation.py`
  - New module function `resolve_booking_name(context, patient=None)`: the first non-empty of `booking_name`, then `patient_name` (ignoring `"there"`), then the patient record's name, else `"Patient"`. Reading `patient_name` as well also covers sessions that were mid-booking during the deploy.
  - `_handle_selecting_family_member`: every branch now sets `booking_name` too. Family branches set `for_self: False`. "For Me" sets `is_family: False`. "For Me" with no saved name now asks for the name instead of using `"there"`.
  - `_handle_collecting_name`: renames the account holder only when `for_self` holds **and** `not is_family`.
  - Four readers now call `resolve_booking_name`: confirmation screen (both branches), paid booking `patient_name`, unpaid booking `patient_name`.
- `tests/test_family_member_booking_flow.py`: 17 new tests (saved member ×3 inputs, typed new member, For Me, For Me without a name, no account rename, self-rename still works, resolver ×6 cases, confirmation screen, unpaid write, paid write).
- `docs/specialty_plan/00-global-constraints.md` §G: now says the bug is fixed and new code must use `resolve_booking_name`.
- `docs/specialty_plan/07b-conversation-wiring.md` S2–S5: anchors updated to the fixed code.

`patient_name` is still written in the context, because `_handle_confirming_save_family_member` reads it.

## 4. Tests run
```
pytest tests/test_family_member_booking_flow.py tests/test_family_members_database.py tests/test_conversation_payment_mode.py tests/test_conversation_navigation_and_timeout.py tests/test_conversation_session_timeout.py tests/test_conversation_unreadable_messages.py tests/test_conversation_admin_sync_and_csv.py tests/test_lab_test_booking_conversation.py tests/test_lab_booking_production_fixes.py tests/test_webhook.py tests/test_lint_unscoped_queries.py -q
with fix:     1 failed, 102 passed in 64.09s
without fix:  1 failed, 85 passed in 43.06s   (same command, changes stashed)
```
**Pre-existing failure:** `tests/test_conversation_admin_sync_and_csv.py::test_doctor_cache_invalidation` (`assert 'clinic-1:Dr. Jones' in _doctor_cache`).
- It fails identically on unmodified code.
- It passes when run alone, so another test file leaves the shared `_doctor_cache` dirty.
- Unrelated to this fix, which does not touch `app/database.py`. Not fixed here.

## 5. Orphan-process check
No pytest orphans. A **user-started** `python -m uvicorn app.main:app --port 8000` (PID 22924, parent `powershell.exe` 36156, started 2026-09-15 20:14) is running locally. If it loads the production `.env`, it competes for production scheduler locks. Left running for the owner to decide.

## 6. Decisions and deviations
- The fix lives in one resolver used by every reader, plus the writers corrected at the source, so no path can drop the name again.
- The save-family-member step (`confirming_save_family_member`) is never entered by any transition, so new family members are not saved for next time. This was noticed, not changed, and needs an owner decision.

## 7. Production actions
None. Not committed or deployed.

## 8. Open items
- Owner: review and commit the change, then deploy. No migration needed.
- Owner: decide on the local uvicorn process (PID 22924).
- Separate tickets: the `test_doctor_cache_invalidation` order-dependence, and the unreachable save-family-member step.
