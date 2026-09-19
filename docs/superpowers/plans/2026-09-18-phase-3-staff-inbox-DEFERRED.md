# Phase 3 — Staff Inbox with AI-Suggested Replies (DEFERRED)

**Status:** Deferred on 2026-09-18. Not built. This is the ready-to-send spec
for when it is picked up.
**Depends on:** Phase 1 (migration 084, `ai_gateway`) and Phase 2 (migration
085) being deployed. Both are.
**Next migration number when built:** check `migrations/`. It was 086 at the
time of writing.

---

## 1. Why it was deferred

1. **The value depends on staff behaviour, not code.** The inbox only helps if
   someone at the clinic watches the admin panel during working hours. If
   nobody does, a patient waits 15 minutes and then gets the reception number
   anyway, which is worse than today's immediate handoff.
2. **Demand is unknown.** It's not yet known how often patients tap "Talk to
   Staff".
3. **Highest risk of the seven AI features.** It is the only one that changes
   the live patient conversation flow. It adds stored chat transcripts (new
   personal data under DPDP) and has to follow Meta's 24-hour
   customer-service window.
4. Phases 1 and 2 had just shipped and needed time to settle with real
   clinics.

## 2. When to build it

Build it when **either** is true:

- a clinic asks for in-chat staff replies, **or**
- one clinic has more than about **15 "Talk to Staff" taps a week**. That
  clinic becomes the pilot.

Check demand (read-only, run in the Supabase SQL editor):

```sql
SELECT clinic_id, count(*) AS talk_to_staff_last_30_days
FROM analytics_events
WHERE event_type = 'human_escalation' AND created_at > now() - interval '30 days'
GROUP BY clinic_id ORDER BY 2 DESC;
```

Before building, also confirm with the pilot clinic that a named person will
watch the inbox during opening hours.

## 3. How "Talk to Staff" works today (must stay the default)

`ConversationManager._handle_human_escalation` in `app/services/conversation.py`:

- sends the clinic's reception number (`get_clinic_contact(clinic, "staff_phone", ...)`);
- sets the state to `escalated_to_human`;
- logs `human_escalation`.

Nothing stores chat text today:

- `inbound_messages` is a processing queue, purged after 30 days;
- outbound text isn't kept (`outbound_message_ledger` is billing only).

---

## 4. Prompt to send to the implementing agent

> **Phase 3: the Staff Inbox with AI-suggested replies (feature 1).** It's
> opt-in per clinic. Rules:
> - one additive migration with a rollback in `migrations/rollback/`;
> - everything scoped to the clinic, with new tables added to
>   `app/tenancy.py` `TENANT_OWNED_TABLES`;
> - AI calls only through `call_ai_gateway`, and read the AI text with
>   `app.services.ai_engine._completion_text()`;
> - mask phone numbers in logs, no stack traces in API responses,
>   audit-log every send via `log_admin_action`;
> - **nothing is sent to a patient without a staff member clicking Send.**
>
> **1. Opt-in and zero change for clinics that don't enable it**
> - Controlled by `clinics.config.staff_inbox_enabled`, default **false**.
>   Only the platform owner can switch it on, from `admin/platform.html`
>   (`app/routers/platform.py`, `verify_owner_credentials`).
> - With it off, "Talk to Staff" behaves **exactly** as it does today.
>   Add a test that proves the flag-off path is byte-for-byte unchanged.
>
> **2. With it on:**
> - "Talk to Staff" tells the patient a staff member will reply here, and
>   puts the conversation in a "handed to staff" state. While in that state,
>   the bot doesn't auto-reply to the patient's normal messages.
> - These still work in that state, exactly as today, and never get stuck:
>   - the emergency keywords and all 5 critical guards at the top of
>     `handle_message()`;
>   - opt-out (stop) and data deletion;
>   - "menu", which hands the chat back to the bot.
> - **Timeout:** if no staff reply comes within 15 minutes, or it's outside
>   clinic hours when the patient asks, the patient gets one message (in
>   their language) with the reception number, and the bot resumes. A chat
>   must never be left in silence.
>   - Run this check with the existing APScheduler pattern and
>     `scheduler_locks`, only when `APP_ENV` is production.
>   - Also run the same check lazily on the next inbound message, so it
>     still works if the scheduler is late.
> - When a staff member resolves the chat, or 24 hours pass, the bot takes
>   over again.
>
> **3. Transcript (new table)**
> - Store message text **only** for conversations handed to staff: inbound
>   and outbound, with direction, sender (patient / bot / staff user_id),
>   timestamp and WhatsApp message id.
> - Keep it 30 days. Purge through the existing `data_retention` job.
> - `delete_patient_data` must delete it too, with a test.
> - `ENABLE ROW LEVEL SECURITY` and add the idempotent `service_role`
>   policy, the same pattern as 084 and 085, so the Supabase linter shows no
>   `rls_enabled_no_policy` warnings.
>
> **4. Meta's 24-hour window**
> - Free-text replies are allowed only within 24 hours of the patient's
>   **last inbound message**.
> - After that, both the API and the panel block sending, with a clear
>   message: "The 24-hour reply window has closed — the patient must message
>   first." Never try a template as a workaround.
>
> **5. Suggested replies**
> - `POST /admin/staff-inbox/{conversation_id}/suggest` uses
>   `call_ai_gateway(task_type="staff_reply")`.
> - Ground the prompt only in this clinic's own data:
>   - name, address, timings and phone;
>   - active lab tests and prices, found via the existing search tiers
>     (`_match_lab_tests` + `hybrid_search`) from the patient's message, at
>     most about 20 tests, never the whole catalogue;
>   - doctors and departments;
>   - the patient's upcoming appointments.
> - **Rules for the AI:**
>   - if the answer isn't in the data, say "Let me check and get back to
>     you". Never invent prices, timings or availability;
>   - no medical advice, diagnosis or medicine suggestions;
>   - run the result through `clinical_firewall.validate_llm_output`;
>   - reply in the patient's language.
> - Budget exhausted or AI unavailable: return no suggestion, and staff
>   simply type their reply. Never block sending.
>
> **6. Sending**
> - `POST /admin/staff-inbox/{conversation_id}/send`:
>   - the text is whatever staff typed or edited, up to 1,000 characters;
>   - sent with the existing `whatsapp.send_text`, so the outbound ledger
>     and accounting work as today;
>   - also saved to the transcript;
>   - record whether the AI suggestion was used unchanged, edited, or not
>     used.
> - Guard against double-clicks with a client-generated idempotency key, so
>   one click never sends twice.
>
> **7. Permissions**
> - Add a real permission, `STAFF_INBOX`, to `app/services/permissions.py`
>   `PERMISSIONS`, so clinic admins can delegate it to staff. (Don't use a
>   permission name that isn't defined there. `require_permission` lets
>   clinic_admin through any name, so a typo silently skips the check.)
> - Branch-pinned staff see only conversations for their branch's patients,
>   if a conversation has a branch; otherwise all of their clinic's.
> - Every route uses `enforce_clinic_access` plus
>   `require_permission("STAFF_INBOX")`. The existing route matrices
>   (`test_admin_super_admin_scope_matrix.py`,
>   `test_phase2_route_adversarial_matrix.py`) must pass.
>
> **8. Panel**
> - A "Staff Inbox" page, shown only when the clinic has it enabled and the
>   user has `STAFF_INBOX`:
>   - a list of waiting chats (oldest first, showing time waiting and a
>     badge for the 24-hour window);
>   - the transcript;
>   - an AI suggestion box with Use / Edit / Regenerate;
>   - Send and Resolve buttons.
> - Poll every 10–15 seconds while the page is open, and stop when it's
>   hidden.
> - Escape all patient text with `esc()`: it's untrusted.
>
> **9. Tests required**
> - flag off = unchanged;
> - emergency, stop, delete-my-data and "menu" all work while the chat is
>   handed to staff;
> - the 15-minute timeout resumes the bot and sends exactly one message;
> - out-of-hours handoff;
> - sending after the 24-hour window is refused;
> - double-click sends once;
> - a staff member in clinic A can't read or send to clinic B;
> - branch-pinned staff scoping;
> - a suggestion with an invented price is caught, or the prompt contains
>   only real catalogue prices (prove it);
> - a medical-advice suggestion is blocked by the firewall;
> - budget exhausted → no suggestion, but sending still works;
> - transcript deleted by `delete_patient_data`;
> - mocks of `call_ai_gateway` use the **real** OpenRouter response shape:
>   `{"choices":[{"message":{"content":"..."}}],"model":"...","usage":{...}}`;
> - a panel contract test: every field the Staff Inbox JS reads exists in
>   the API response.
>
> **10. Finish**
> - No leftover local python, uvicorn, pytest or postgres processes.
> - Full suite `pytest tests -q --ignore=tests/test_multi_worker_smoke.py`
>   with **0 failed and 0 errors**, plus `node --check` on the panel JS.
> - Write a `docs/sessions/SESSION_NN_STAFF_INBOX.md`.
> - One commit.
> - Report: exact counts, `git log --oneline -3`, `git diff --stat HEAD~1`,
>   every new route with the permission **as written in the code**, and
>   confirmation that the flag-off test passes.
>
> Then stop for review.

---

## 5. Lessons from the Phase 1 and 2 reviews (check these first)

These bugs passed the agent's own test suite and were caught only by
running the code directly:

| Phase | Bug | How it slipped through |
|---|---|---|
| 1 | Search matched short abbreviations as substrings ("calcium" → CT scan) | No negative tests |
| 1 | Clean-up flagged IgG/IgM, B12/B1, AP/PA as duplicates, pre-ticked for delete | No negative tests |
| 1 | Clean-up wrote a non-existent column (`preparation`, not `prep_instructions`) | Mocks accept any column |
| 1 | Clean-up scan took 45 s on the event loop | No timing test |
| 1 | AI Draft button read a non-existent element id | No panel check |
| 1 | Budget route used a non-existent permission, so any clinic admin passed | `require_permission` lets clinic_admin through any name |
| 2 | Both AI services read `response["content"]`; the real text is at `choices[0].message.content` | Tests mocked a fake response shape |
| 2 | Panel read `price_inr` / `source_snippet` / `revenue_inr`; backend sent other keys | No panel↔API contract test |
| 2 | Migration tables had RLS with no policy (Supabase linter INFO) | Supabase auto-enables RLS; no migration test runs 084/085 |

**Review checklist for Phase 3:**
- Run the AI path with the real OpenRouter response shape.
- Diff every JS field read against the API response.
- Grep every permission name against `PERMISSIONS`.
- Test the flag-off path against today's behaviour.
- Run the migration in Supabase and re-run the linter.
