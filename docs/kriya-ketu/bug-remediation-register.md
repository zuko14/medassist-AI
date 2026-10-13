# Kriya Ketu — Bug Remediation Register (existing-system defects)

Parent: [spec.md](spec.md) §6 · Tasks: [implementation-plan.md](implementation-plan.md) P0 · Status: [status.md](status.md) §9.

**Owner approval (2026-10-14):** fix decisions approved (DP-01, DP-02, DP-09, DP-12, DP-13, DP-14).
**Fixes implemented 2026-10-14** in the working tree on `main` (**not committed, not deployed**). Each fix
had a failing test first, then the fix, then the module's existing tests and the standing gates.

**Scope.** Every defect found by the Kriya Ketu audits (2026-10-13/14) and while fixing them, plus the
open items in `docs/agent-context/13-KNOWN-ISSUES-AND-GAPS.md`. This is not a claim that Kriya has no
other bugs. Task **KK-P0-15** still runs a hardening checklist over each endpoint before Kriya Ketu
uses it.

Status legend: **Fixed** (code + regression test, suite green) · **Fixed in code / migration pending**
(needs `migrations/107_…` applied to production) · **Withdrawn** (audit was wrong) · **Open** · **Limitation**
(a missing feature, not a defect) · **Ops** (configuration, not code) · **Accepted**.

| ID | Status | Defect (corrected where the audit was wrong) | Fix | Regression test |
|---|---|---|---|---|
| D-01 | **Fixed** | Leaves for today, or entered after 08:00 for tomorrow, never cancelled or notified bookings: the daily sweep looked at tomorrow → +7 only | `scheduler.check_doctor_leaves` window now starts **today**; `create_leave` processes today/tomorrow leaves **immediately** via `SchedulerService.process_new_leaves()` under the same `doctor_leaves` lock (retries while the sweep runs, so nothing is processed twice); today's visits whose time has passed are not cancelled | `tests/test_doctor_leave_same_day.py` |
| D-02 | Limitation | Leaves keyed by doctor name text | Unchanged; Kriya Ketu resolves the exact stored name. `doctor_id` keying is a future schema change | — |
| D-03 | **Fixed** (scope adjusted) | `POST /admin/lab-reports/{id}/resend` returned raw exception text on a 500 and wrote no audit row; `POST /admin/reports/{id}/resend` turned staff-facing messages ("re-upload the report") into a 500 with raw text | Both routes: generic 500 text, staff-facing `ValueError` messages kept as 404/400, audit row on success. **Permission not tightened** (deviation from DP-02): upload and list on the same page are open to all staff, so resend-only restriction would block lab front desks from re-sending what they may send | `tests/test_error_detail_hygiene.py` |
| D-04 | **Fixed** | Staff appointment cancellation wrote no audit row | `cancel_appointment_by_admin` logs `cancel_appointment` on success only | `tests/test_error_detail_hygiene.py` |
| D-05 | Open by design | `Permissions-Policy: microphone=()` blocks voice input | Deliberate security setting; relaxed to `microphone=(self)` only when voice input ships (KK-P3-01) | — |
| D-06 | **Fixed** | Leave sweep used the container clock (UTC), not IST | Uses `_today_local()` / `_now_local()` (Asia/Kolkata), with D-01 | `tests/test_doctor_leave_same_day.py` |
| D-07 | Limitation | No patient broadcast feature | — | — |
| D-08 | Limitation | No admin view of failed WhatsApp messages | PLANNED (KK-P4-04) | — |
| D-09 | **Fixed** | Editing a staff member's role/permissions/branch/doctor link, or deactivating them through the edit form, left their sessions alive (permissions are a sign-in snapshot) for up to 12 h | `update_staff` revokes that user's sessions when any of those actually change or the account is deactivated; contact-only edits keep the session; audit records `sessions_revoked` | `tests/test_staff_edit_revokes_sessions.py` |
| D-10 | **Withdrawn** | Audit claimed lab-test price edits were unvalidated | Wrong: `LabTestUpdate.validate_price` (`admin.py`) already rejects ≤ 0 and has a passing test (`tests/test_admin_to_patient_sync.py`). The audit read only the top of the class | — |
| D-11 | **Fixed** | Saving a leave over an existing date failed (whole batch) or could duplicate | `create_leave` skips dates already covered, **widens** an existing half day to a full day (other half or full day requested), reports `already_on_leave` / `widened_to_full_day`; widened near dates also trigger D-01 processing | `tests/test_leave_rows_combined.py` |
| D-12 | **Fixed** | `PUT /admin/voice/settings` validated the body before checking `VOICE_MANAGE` (422 instead of 403) | Permission checked first | `tests/voice/test_voice_routes.py` |
| D-13 | **Open** | Whole-object rewrites of `clinics.config` / `branches.config` can overwrite a concurrent save (inferred; 10 writers across admin, platform, home_collection, voice_admin, voice_platform) | Not fixed: a correct fix is an atomic DB-side merge (new migration + all 10 writers). A partial fix would move the race, not remove it. Needs a reproduction and a separate change | — |
| D-14 | **Fixed** | 21 server-error responses echoed raw exception text (DB errors, provider messages, a key-configuration detail); one of them did not log at all | Fixed sentences everywhere (admin ×9, platform ×11, integrations ×1); exceptions still logged (the missing log added). `_friendly_db_error` sites confirmed safe (fixed sentences only). An AST test fails CI if any router again puts the exception in a 5xx | `tests/test_error_detail_hygiene.py` |
| D-15 | **Fixed** | `ai_gateway.py` comment pointed to a non-existent `app/voice/usage.py` | Points to `app/voice/store.budget_exceeded` | — |
| D-16 | **Fixed** (half) | `autoqueue_leads` applied the daily cap before skipping booked / recently-called leads, so fewer calls were queued than the cap allows | Cap now counts calls actually queued. The suspected phone-format mismatch was **disproved**: WhatsApp numbers are normalised to `+91…` at ingress (`normalize_phone`) | `tests/voice/test_lead_calls.py` |
| D-17 | **Fixed** (restated) | `LeaveCreate.leave_type` accepted any string; the DB `CHECK` (migration 001) rejected unknown types, so the API answered a vague 500 | `Literal["full","half_morning","half_evening"]` → clear 422 | `tests/test_doctor_leave_same_day.py` |
| D-18 | **Fixed** (new) | The leave sweep sent "doctor on leave" WhatsApp even when its guarded cancel changed nothing (booking already cancelled by the patient) | Message sent only if the row was actually cancelled (or refunded) | covered by leave tests |
| D-19 | **Fixed** (new; defence in depth) | Slot picker and OPD walk-in read only the first leave row of an unordered query | Shared `database.leave_blocked_sessions()` combines all rows (full or both halves = whole day; unknown legacy type = whole day) | `tests/test_leave_rows_combined.py` |
| D-20 | **Fixed in code / migration pending** (new) | `doctor_leaves` still has the pre-tenancy `UNIQUE(doctor_name, leave_date)` across **all clinics**: a clinic could not record leave for a doctor whose exact name another clinic also has on leave that day (500) | Code: clear 409 explaining the clash. Schema: `migrations/107_doctor_leaves_unique_per_clinic.sql` (+ `rollback/107_down.sql`) replaces it with `UNIQUE(clinic_id, doctor_name, leave_date)` after merging any duplicates. **Verified on embedded PostgreSQL (migrations 001→107); not applied to production** | `tests/test_migration_107_doctor_leaves.py`, `tests/test_leave_rows_combined.py` |
| K-01 | **Fixed** | `get_treatment_doctor_ids` returned "no mapping" (= any doctor) on a DB error, offering doctors who don't perform the treatment | Returns `None` on error; `_treatment_doctors` then offers none, and patients see the existing "no specialist available for online booking right now" | `tests/test_treatment_doctors_fail_closed.py` |
| K-02 | Ops | 3 live clinics lack `config.integration_secret` | Per-clinic rollout with their senders (not code) | — |
| K-03 | Accepted | Tenant cache staleness ≤ 30 s across workers | Documented trade-off | — |

## How "permanent" these fixes are
- Each fixed defect has a regression test that fails if the bug returns, and they run in the normal suite.
- D-14 has a structural guard (AST scan of every router), so new code is checked too, not only the 21 sites.
- **Nothing reaches users until it is committed, deployed and (for D-20) migration 107 is applied.**

## Fix rules (unchanged)
Failing test first; smallest change in the shared path; standing gates green; patient-facing changes
under owner sign-off; one defect per commit when committing.
