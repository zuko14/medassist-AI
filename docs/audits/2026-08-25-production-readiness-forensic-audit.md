# Kriya AI / MediAssist AI v2.0.0 — Production Readiness Forensic Audit

**Date:** 2026-08-25
**Scope:** Full repository, read-only. No code was modified during this audit.
**Method:** Implementation source of truth. The supplied forensic report was treated as an unverified claim set.

---

## A. EXECUTIVE VERDICT

**PRODUCTION STATUS: BLOCKED.**

Kriya AI is an architecturally competent multi-tenant healthcare platform with several genuinely
well-built controls — Meta webhook HMAC verification fails closed, production boot refuses
placeholder secrets, tenant resolution refuses to guess rather than defaulting, payment
confirmation and expiry use correct compare-and-set optimistic concurrency, and the
`payment_events` ledger is append-only enforced by a database trigger. It is not, however,
ready for commercial launch. Five defects are launch blockers: a status value written by the
late-payment refund path (`refunded_late_payment`) is **not permitted by the `appointments_status_check`
CHECK constraint**, so a real Razorpay refund succeeds while the database record of it is lost and the
webhook returns HTTP 200 so Razorpay never retries; two admin endpoints (`/admin/lab-reports/{id}/resend`
and `/admin/bookings/{id}/refund`) perform **no tenant ownership check whatsoever**, allowing any
authenticated user of any clinic — including a `staff`-role user for the lab report path — to re-deliver
another clinic's patient PHI or refund another clinic's payment; the patient-identity safety gate that
exists specifically to prevent wrong-patient report delivery **fails open on a database error**, silently
converting a would-be NEEDS_REVIEW into an automatic send; and **Row Level Security provides zero isolation
for this application** because it connects with the Supabase `service_role` key (which is `BYPASSRLS`) and
no table declares `FORCE ROW LEVEL SECURITY` — meaning all 38 RLS policies are inert for every query the
app makes, and the two IDORs above have no database backstop. Separately, the supplied report's claim of
"439 tests, 438 passed" is outdated — the suite is 631 tests and green — but the suite is **entirely
mock-based**: there is no test anywhere in this repository that exercises a real PostgreSQL engine, no
concurrency test, and no load test. Every database-enforced invariant the report relies on to argue safety
is therefore **UNVERIFIED**. The double-booking test asserts an exception-string parser; the RLS test
asserts that a `.sql` file contains certain substrings. Architecture appears capable, but capacity is
unproven and the core safety invariants are untested against the engine that is supposed to enforce them.

---

## B. WHAT WAS VERIFIED (with evidence)

| Control | Evidence | Status |
|---|---|---|
| Meta webhook signature fails closed | `app/utils/security.py:verify_webhook_signature` — returns `False` when `app_secret` unset outside dev; `hmac.compare_digest` | VERIFIED |
| Production boot refuses placeholder secrets | `app/main.py` lifespan blocks `META_APP_SECRET`, `ADMIN_PASSWORD`, `OWNER_PASSWORD`, `INTEGRATION_SECRET`, `CALLMEDEX_BEARER_TOKEN` | VERIFIED |
| Docs/redoc/openapi disabled in production | `app/main.py` | VERIFIED |
| Tenant resolution does not silently default | `app/services/tenant.py:resolve_tenant` raises `RuntimeError` on DB error; raises `TenantNotFound` when >1 active clinic | VERIFIED |
| Payment confirm is atomic CAS | `app/services/payment.py` step 8 — `.update(...).eq("id",id).eq("status","pending_payment")` | VERIFIED |
| Slot expiry is atomic CAS | same pattern in `expire_stale_bookings` | VERIFIED |
| `payment_events` is append-only at DB level | `migrations/008_payments.sql` — `prevent_payment_event_mutation()` trigger, EXECUTE revoked from anon/authenticated/public | VERIFIED (as SQL; never executed in test) |
| Missed-webhook recovery is real | `expire_stale_bookings` polls Razorpay Payment Link status before expiring | VERIFIED |
| Branch/connector tenant scoping | `app/services/permissions.py:152-173 resolve_owned_branch`, `admin.py:_load_connector_for_action` — both enforce clinic ownership and 404 on mismatch | VERIFIED |
| Double-booking constraint design | `migrations/008_payments.sql` `idx_unique_active_slot` partial UNIQUE — correct approach | VERIFIED as schema, UNVERIFIED as behavior |
| Queue token constraint + retry | `migrations/021` `idx_unique_queue_token`; `app/database.py:753-791` retries on conflict up to 5x | VERIFIED as schema + retry logic |
| Background task supervision | `app/utils/async_tasks.py` holds strong refs, logs unhandled exceptions in done-callback | VERIFIED |
| No swallowed exceptions of the `except: pass` form | repo-wide grep across `app/` and `connectors/`: **0 matches** | VERIFIED |
| PDF magic-byte validation on connector intake | `app/routers/integrations.py` rejects non-`%PDF` bodies (guards against MocDoc session-timeout HTML saved as .pdf) | VERIFIED |
| Platform owner auth is constant-time | `app/routers/platform.py:60-65` `secrets.compare_digest`; all 26 routes carry `Depends(verify_owner_credentials)` | VERIFIED |
| Cross-intake-path report dedup | `lab_reports.upload_and_send` claims `(clinic_id, external_report_id)` row before sending | VERIFIED as logic |

---

## C. WHAT WAS NOT VERIFIED

These are **NOT VERIFIED**. No evidence in the repository substantiates them.

- `idx_unique_active_slot` actually preventing a double booking under concurrent load — **no test ever connects to PostgreSQL.**
- Any RLS policy actually denying anything — and see F-1: RLS cannot apply to this app at all.
- `appointments_status_check` rejecting invalid statuses — never executed in test (this is how P0-1 survived).
- The `payment_events` mutation-blocking trigger firing.
- `idx_unique_queue_token` rejecting a duplicate token.
- Behavior under 2 / 10 / 100 concurrent bookings for the same slot — **no concurrency test exists** (0 uses of `asyncio.gather`, `ThreadPoolExecutor`, or `threading` in `tests/`).
- Throughput, latency, or tenant density at any scale — **no load test artifacts exist** (0 locust/k6/artillery files).
- Behavior across multiple workers, instances, or a restart.
- Meta webhook duplicate/out-of-order delivery handling in production.
- Razorpay reconciliation correctness against live data (previously blocked on missing test credentials).
- Playwright/MocDoc connector behavior against the live portal.

---

## D. CRITICAL FINDINGS

### P0-1 — Late-payment refund writes a status the CHECK constraint forbids; money moves, record is lost
- **Severity:** P0 — financial record loss, no recovery path
- **Component:** Payments
- **Files:** `app/services/payment.py:~592` (late-payment branch of `process_payment_webhook`); constraint at `migrations/008_payments.sql:20-22`
- **Problem:** The branch issues a real Razorpay refund, then writes `"status": "refunded_late_payment"`. `appointments_status_check` permits only `confirmed, cancelled, rescheduled, completed, no_show, pending_payment, expired, refunded, pending_review`. Verified that no later migration (including `039_appointments_lab_test_booking.sql`) alters this constraint.
- **Failure scenario:** Patient pays after the 10-minute hold expires → refund succeeds at Razorpay → `UPDATE` raises a CHECK violation → exception propagates to `app/routers/razorpay_webhook.py:77-82`, which returns **HTTP 200** → Razorpay marks the event delivered and never retries. Net state: money refunded at the provider, `appointments.status` still `expired`, no `payment_id`, no `refund_id`, `_notify_late_payment_refunded` never runs, patient never told. Reconciliation cannot detect it because there is no local record to reconcile against.
- **Why tests miss it:** No test executes against PostgreSQL; the CHECK constraint has never been evaluated.
- **Fix:** Add `refunded_late_payment` to the constraint via migration, **or** write `refunded` plus a `refund_reason` column. Prefer the latter — fewer status values, no constraint churn. Then make the webhook return non-2xx on unhandled processing errors so Razorpay retries.
- **Regression test:** real-PostgreSQL test asserting every status literal written anywhere in `app/` is accepted by `appointments_status_check`.

### P0-2 — IDOR: any authenticated user can re-deliver any clinic's patient report
- **Severity:** P0 — cross-tenant PHI disclosure
- **Component:** Admin API / Lab reports
- **Files:** `app/routers/admin.py:2097-2108`; `app/services/lab_reports.py:495-505`
- **Problem:** `POST /admin/lab-reports/{report_id}/resend` is guarded by `Depends(verify_credentials)` only — **no `enforce_clinic_access`, no ownership check**. `verify_credentials` admits `staff`-role users. `resend_report(report_id)` looks the row up by primary key with **no clinic filter**, resolves the *owning* clinic's WhatsApp credentials, and re-sends the PDF.
- **Exploit:** Authenticate as any clinic's staff user. `POST /admin/lab-reports/<any-uuid>/resend`. Another tenant's patient receives their medical report again, sent from that tenant's own WhatsApp number. UUIDs are guessable only by enumeration, but the endpoint's error path (`raise HTTPException(status_code=500, detail=str(e))`) leaks internal messages that distinguish "Report not found" from storage/delivery errors — a working existence oracle.
- **Contrast proving this is an omission, not a design choice:** `GET /admin/payment-events/{booking_id}`, immediately below the refund route in the same file, performs an explicit ownership check.
- **Impact:** DPDP Act personal-data breach; Meta Business Policy violation (unsolicited PHI send); hospital trust loss.
- **Fix:** load the report, compare `report["clinic_id"]` against `enforce_clinic_access(user, ...)`, 404 on mismatch. Add `require_permission("lab_reports:resend")`. Replace `detail=str(e)` with a generic message.

### P0-3 — IDOR: any clinic admin can refund any other clinic's booking
- **Severity:** P0 — cross-tenant financial mutation
- **Files:** `app/routers/admin.py:2339`; `app/services/payment.py:initiate_refund`
- **Problem:** `POST /admin/bookings/{booking_id}/refund` uses `Depends(require_admin)` — role-checked, **not tenant-checked**. It calls `payment_service.initiate_refund(booking_id, reason)` with **no `clinic` argument**. `initiate_refund` then does `.eq("id", booking_id)` with no clinic filter and resolves credentials via `get_razorpay_creds(clinic or {})` — i.e. **global** credentials.
- **Two distinct defects in one line:**
  1. **Security:** a clinic_admin at clinic A refunds clinic B's booking.
  2. **Correctness:** because no clinic dict is passed, legitimate refunds for any clinic using per-clinic Razorpay keys execute against the *global* account and fail or hit the wrong merchant. Admin refund is likely broken today for every per-clinic-keyed tenant.
- **Also:** `idempotency_key = f"refund_{booking_id}_{uuid.uuid4().hex[:8]}"` is regenerated per attempt, so a retried refund is **not** idempotent at Razorpay — double-refund risk.
- **Fix:** resolve the booking scoped to the caller's clinic; pass the clinic dict; make the idempotency key deterministic (`refund_{booking_id}_{payment_id}`).

### P0-4 — Wrong-patient safety gate fails OPEN on database error
- **Severity:** P0 — unsafe healthcare behavior
- **Files:** `app/services/patient_match.py:134-160`
- **Problem:** The module docstring states the design contract: *"Missing or malformed phone numbers fail-closed into NEEDS_REVIEW."* The phone checks do fail closed. The **database lookup does not**:
  ```python
  except Exception as e:
      logger.error(f"Failed to query patients for match: {e}")
      records = []          # <-- indistinguishable from "no such patient"
  ...
  if not records:
      return MatchResult(status="matched", is_safe_to_send=True,
                         match_source="moc_doc_only", match_confidence=1.0, ...)
  ```
  A transient Supabase error is coerced into "walk-in patient, no conflict possible, confidence 1.0, send it."
- **Failure scenario:** Supabase has a 30-second blip during a connector poll. Every report in that batch that *would* have tripped a name conflict on a shared family phone — the exact case this gate exists to catch — is auto-delivered instead, and the stored audit row records `match_source="moc_doc_only", match_confidence=1.0`, so the incident is invisible in the admin panel afterward.
- **Impact:** violates the system's most important invariant — *a patient's report must never be delivered to another patient*.
- **Fix:** return `needs_review` with `match_source="lookup_failed"` on exception. This is a 4-line change and the single highest safety-per-line fix in the repository.

### P0-5 — Row Level Security provides no isolation for this application
- **Severity:** P0 — the claimed primary isolation control does not apply
- **Files:** `app/database.py:22-23`; all 31 tables with `ENABLE ROW LEVEL SECURITY` across `migrations/`
- **Evidence:** the client is built with `settings.supabase_service_role_key`. In Supabase, `service_role` holds `BYPASSRLS`. Repo-wide grep for `FORCE ROW LEVEL SECURITY`: **0 matches**. Without `FORCE`, RLS is additionally bypassed for table owners.
- **Consequence:** all 38 `CREATE POLICY` statements govern only anon/authenticated PostgREST access. **Every query the FastAPI application makes bypasses RLS entirely.** Tenant isolation is 100% dependent on the Python `.eq("clinic_id", ...)` filters. Any route that omits that filter — see P0-2, P0-3, and the P2 findings — has **no database backstop**.
- **This is not necessarily wrong architecture**, but it must be stated accurately: RLS here is a defense for a public API surface, not a multi-tenant isolation guarantee for the app. Any claim that "RLS enforces multi-tenancy" is **FALSE** as applied to application traffic.
- **Fix (choose one, do not do both halfway):** either (a) accept Python-enforced isolation and add a mandatory `clinic_id` scoping helper that every table read must route through, with a lint rule failing raw `supabase.table(...)` calls in routers; or (b) move application traffic to a non-`service_role` role with `FORCE ROW LEVEL SECURITY` and per-request tenant claims. (a) is the pragmatic path given the current codebase.

---

## E. SECURITY

**P1-8 — Razorpay webhook swallows all processing errors as HTTP 200.**
`app/routers/razorpay_webhook.py:77-82` returns 200 on any unhandled exception. Razorpay treats 2xx as
delivered and does not retry. Combined with P0-1 this is how a completed refund vanishes. Return 500 on
unexpected errors; keep 200 only for *understood* outcomes (bad signature, unknown clinic, duplicate).

**P2-6 — Rate limiting is effectively global, not per-IP.**
`Dockerfile` CMD: `exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}` — **no `--proxy-headers`**.
Behind Render's proxy, `request.client.host` is the proxy address for every request. The admin login limiter
(`max_attempts=5, window_seconds=60`, keyed on `request.client.host`) therefore throttles *all* tenants
collectively — simultaneously a weak brute-force defense and a self-inflicted DoS on legitimate logins.
Add `--proxy-headers --forwarded-allow-ips=*` and key on `X-Forwarded-For`.

**P3 — Non-constant-time secret comparison.** `app/routers/clinics.py:33` — `if x_admin_secret != settings.admin_secret`. Use `secrets.compare_digest`. (`platform.py` already does this correctly.)

**P3 — Plaintext password comparison fallback.** `admin.py:check_password_hash` falls back to
`secrets.compare_digest(plain, stored_hash)` when the stored value is not a `$2a$`/`$2b$` bcrypt hash.
Any row seeded with a plaintext password authenticates successfully. Fail closed instead.

**P3 — `PATCH /admin/clinics/{clinic_id}` accepts an arbitrary `updates: dict`** written straight into
`clinics`. Owner-only, but it can set `plan`, `is_active`, `phone_number_id`, or arbitrary `config` keys
with no schema validation. Replace with a Pydantic model.

**P3 — Error-detail leakage.** `raise HTTPException(status_code=500, detail=str(e))` appears in the
lab-report resend path and elsewhere, contradicting the CLAUDE.md rule *"Never expose stack traces in
webhook API responses."*

**P4 — Hardcoded Meta registration PIN.** `app/routers/clinics.py:144` — `{"messaging_product": "whatsapp", "pin": "123456"}`.

---

## F. MULTI-TENANT ISOLATION

**F-1: The database enforces no tenant isolation on application traffic** (see P0-5). Everything below
therefore has no second line of defense.

| Path | Scoping | Status |
|---|---|---|
| `resolve_owned_branch` | clinic + branch pinning, 404 on mismatch | CORRECT |
| `_load_connector_for_action` | clinic-scoped | CORRECT |
| `GET /admin/payment-events/{booking_id}` | explicit IDOR check | CORRECT |
| `POST /admin/lab-reports/{id}/resend` | **none** | **P0-2** |
| `POST /admin/bookings/{id}/refund` | **none** | **P0-3** |
| `POST /admin/branches/{branch_id}/doctors` | branch validated, **`body.doctor_id` not validated** | **P2-3** — cross-tenant doctor attach |
| `GET /admin/branches/{branch_id}/doctors` | `.select("*, doctors(*)")` | **P2-3** — cross-tenant doctor disclosure |
| `GET /admin/patients` | correct, **except** `effective_clinic_id == "default"` selects all patients unscoped | **P2** |
| `tenant.get_branch_by_id` | scans cache across all clinics, falls back to unscoped `.eq("id", branch_id)` | **P3** |
| `/internal/integrations/lab-report` | `clinic_id` is a **client-supplied form field**, never validated | **P1-2** |

**P2-1 — Sandbox fallback precedes the refuse-to-guess guard.** In `resolve_tenant`, Strategy 3 returns the
`is_sandbox=True` clinic *before* Strategy 4's `len(active_clinics) > 1` check. A sandbox clinic left active
in production silently captures unrecognized inbound traffic instead of raising.

**P2-2 — Cache invalidation is incomplete and process-local.** `invalidate_tenant_cache(whatsapp_number)`
pops only the phone key; Strategy 1 caches under **both** `phone` and `phone_number_id`, so the
`phone_number_id` entry survives. A deactivated clinic keeps serving for up to the 5-minute TTL. And because
the cache is a process dict, invalidation never crosses process or instance boundaries at all. Also:
`_branch_cache` is declared twice in `app/services/tenant.py` (lines 17 and 424) — the second shadows the first.

---

## G. BOOKING & CONCURRENCY

**Design: correct. Verification: absent.**

`idx_unique_active_slot ON appointments (clinic_id, doctor_name, appointment_date, appointment_time)
WHERE status IN ('pending_payment','confirmed')` is the right mechanism — a database constraint, not
application locking. `create_booking_with_payment` handles the violation by returning `slot_taken`.
Confirm and expire both use compare-and-set. Queue tokens use the same pattern with a bounded retry loop.

**But:**

- **P1-7:** the only test of this is `tests/test_payment.py::test_slot_taken_returns_failure`, which
  `patch()`es `app.services.payment.supabase` to raise, then asserts the error-string parser returns
  `slot_taken`. It tests the string `"duplicate" in str(e).lower()`. **It does not test the constraint.**
  If the index were dropped tomorrow, this test would still pass.
- **P2-4 — the UI advertises slots the database will reject.** `app/database.py:358-477 get_available_slots`
  → `_sync_fetch_booked` **excludes `pending_payment` rows whose `hold_expires_at` has passed**, but
  `idx_unique_active_slot` still counts them (their status is still `pending_payment` until the 1-minute
  expiry job runs). Between hold expiry and job execution, a patient is shown a slot, selects it, and the
  insert fails with `slot_taken`. Confusing but not corrupting.
- **Lab-test bookings are deliberately unconstrained.** `booking_type='lab_test'` rows carry
  `doctor_name = NULL`, and PostgreSQL treats NULLs as distinct in unique indexes, so `idx_unique_active_slot`
  never applies to them. Migration 039 drops `NOT NULL` on `appointment_time` as well. **Lab test slot
  capacity has no database-level protection.** If lab slots are capacity-limited, this is a P1; if they are
  unlimited walk-in, it is correct by design. *Not determinable from the code — requires a product decision.*
- **P3 — `check_in_appointment` is not idempotent.** `app/database.py:773-779` updates `token_number`
  unconditionally. A double-clicked "Check In" assigns a *new, higher* token, moving the patient to the back
  of the queue. Add `.is_("token_number", None)` to the update.

---

## H. PAYMENTS

Beyond P0-1 and P0-3:

**P1-3 — A transient Razorpay error can expire a paid booking.**
`payment.py:_check_payment_link_status` catches every exception and returns `{"status": "unknown", "payment_id": ""}`.
`expire_stale_bookings` treats non-paid as expirable. A Razorpay timeout during the 1-minute expiry sweep
therefore expires a booking the patient has already paid for, releasing the slot while the charge stands.
**Fix:** distinguish "confirmed not paid" from "could not determine" and **skip** expiry on the latter,
deferring to the next cycle. Alert if a booking is deferred more than N cycles.

**P3 — Unscoped global booking lookup.** `process_payment_webhook` falls back to an unscoped search across
all clinics when the clinic-scoped lookup fails. Defensible for a webhook, but it means a booking id from
clinic B can be mutated by a webhook delivered to clinic A's endpoint if the signature validates. Log loudly
when this fallback fires.

**P3 — `admin_refund_booking` builds `reason = f"Admin refund by {user}"`** where `user` is an `AdminUser`
object — the object repr is transmitted to Razorpay. Use `user.username`.

**Invariant status:**
- One payment cannot confirm multiple appointments — **plausible via CAS, NOT VERIFIED (no concurrency test).**
- Payment ledger is append-only — **VERIFIED as SQL, NOT VERIFIED as running behavior.**
- Amount tampering — the Payment Link amount is server-set; **NOT VERIFIED** that the webhook re-checks `amount` against the booking fee before confirming. Add an explicit assertion.

---

## I. WHATSAPP / META

**Delivery classification: AT-MOST-ONCE inbound. This is the wrong guarantee for healthcare.**

`app/routers/webhook.py` verifies the signature, then hands off via FastAPI `BackgroundTasks` and returns
200 **before processing**. `BackgroundTasks` runs in-process, post-response. Meta only retries non-2xx.

- **P1-6 — message loss on deploy or crash.** Render deploys restart the container. Any message accepted but
  not yet processed is gone: not in Meta's retry queue (we returned 200), not in `failed_messages` (the task
  never raised). There is exactly one web instance (`render.yaml` declares no `numInstances`) and the
  Dockerfile passes no `--workers`, so there is no redundancy either.
- **P1-5 — silent drop on a Supabase blip.** `message_queue.acquire()` fails **closed** on database error and
  `return False`; the caller in `process_message` simply returns. The message is dropped with no DLQ row and
  no exception. The counter tracking this is named `_fail_open_count` while the behavior is fail-closed —
  a misleading name on the one metric that would reveal the problem.
- **P1-4 — the dead-letter queue has no replayer.** `failed_messages` is written by `webhook.py` and
  `conversation.py`, alerted on Mondays, purged at 30 days — and **never retried**. Worse, replay is
  *impossible by construction*: `_handle_message_locked` persists `last_processed_message_id` at
  lines 245-248 **before any processing occurs**, so re-submitting the same `message_id` is rejected by
  Guard 1. A message that fails mid-processing can never be reprocessed.
- **P2-5 / Rule C — per-phone locks are `asyncio.Lock` objects in a process dict.** Correct today only
  because there is exactly one worker in one instance. Adding `--workers 2` or `numInstances: 2` silently
  removes the ordering guarantee. `processed_messages` (UNIQUE on `message_id`) remains the real
  cross-process defense — but note its docstring claims "atomic INSERT with ON CONFLICT DO NOTHING" while
  the implementation is a plain PostgREST insert with duplicate detection by **exception-string matching**
  (`"unique" in s or "duplicate" in s or "23505" in s`). Behaviorally equivalent; the docstring is inaccurate.

**Fix direction:** persist the inbound message to a durable queue table *before* returning 200, process from
that table, and mark `last_processed_message_id` **after** successful completion. Then a crash is a retry,
not a loss.

---

## J. CONNECTORS & LAB REPORT PIPELINE

**Invariant under audit: *a patient's report must never be delivered to another patient.***

**The safety gate runs on 1 of 3 intake paths.**

| Intake path | Calls `patient_match_service.match()`? |
|---|---|
| `connectors/runner.py:525` (MocDoc worker — the deployed worker) | **YES** — before download and before submit |
| `app/routers/integrations.py` (`/internal/integrations/lab-report`) | **NO** |
| `app/routers/admin.py:2067` (manual admin upload) | **NO** |

**P1-2 — the match result is client-asserted, not server-computed.** `receive_lab_report` accepts
`clinic_id`, `patient_phone`, `patient_name`, `match_confidence`, `match_source`, and `matched_patient_id`
as **form fields from the caller**, and `LabReportService.upload_and_send` accepts them as metadata only —
it has no `is_safe_to_send` parameter and performs no verification. The endpoint's own docstring states the
design: *"this endpoint does ZERO business logic."* Consequences:
1. Anyone holding `INTEGRATION_SECRET` can deliver any PDF to any phone under any `clinic_id`. The
   `clinic_id` is never checked for existence or active status.
2. **The stored `match_source` / `match_confidence` audit fields are not evidence of anything.** They record
   what the caller claimed. Any report or dashboard citing them as proof of identity verification is
   citing self-reported data.
The trust boundary (machine-to-machine shared secret, internal network) makes this *defensible* but not
*verifiable*. For a wrong-patient-delivery invariant this is the wrong side of the line.

**P1-1 — `validate_report()` is a stub that logs a check it does not perform.**
`app/integrations/callmedex/connectors/mocdoc/connector.py:1130-1143`:
```python
async def validate_report(self, file_bytes, expected_patient) -> bool:
    """Validate report content against patient identity contract."""
    if len(file_bytes) == 0: raise ValidationError(...)
    if not file_bytes.startswith(b"%PDF"): raise ValidationError(...)
    logger.info(f"Validating report bytes against patient '{expected_patient.patient_name}'")
    self._current_checkpoint = JobCheckpoint.VALIDATED
    return True
```
`expected_patient` is used **only in a log line**. No name, phone, or ID is compared against the PDF's
contents. The function emits a log asserting a verification that never happens, then sets a checkpoint
named `VALIDATED`. This is the last defense against a portal-side download-link mix-up delivering patient
A's PDF under patient B's metadata — and it is inert. It is also called from only one place
(`app/integrations/callmedex/workers/runner.py:221`, the CallMedex path); the deployed MocDoc path in
`connectors/runner.py` never calls it at all.

**What is genuinely well built here:**
- Row-scoped DOM extraction (`rows.nth(i)`) rather than index-aligned parallel lists — avoids the classic
  scraper row-misalignment bug.
- `%PDF` magic-byte rejection with an explicit comment naming the MocDoc session-timeout failure mode.
- Cross-path idempotency: a `(clinic_id, external_report_id)` claim row is inserted with
  `delivery_status='processing'` **before** the WhatsApp send, and a unique violation is handled as
  "another path won the race."
- `NEEDS_REVIEW` reports are persisted with the review reason and recorded as connector failures.
- Per-phone lock held across the delivery to prevent interleaved sends.

**Delivery guarantee classification:**
- Connector → Kriya: **at-least-once** (retry on failure) with **effectively-once** delivery via the
  `external_report_id` claim row — *provided* the claim insert and the WhatsApp send do not straddle a crash.
  A crash after send but before the final `UPDATE` leaves a `processing` row; `retry_pending_deliveries`
  will retry it. **Duplicate delivery is possible in that window. NOT VERIFIED.**
- Kriya → patient: **at-least-once.** `retry_pending_deliveries` retries up to `MAX_RETRIES` on any
  delivery that did not record success.

---

## K. DATABASE

45 migrations, forward-only, no down-migrations, applied manually via the Supabase SQL Editor (per the
headers in the files themselves). **There is no migration runner and no schema-version table** — nothing in
the repository can tell you which migrations a given environment has actually applied. Combined with the
total absence of real-PostgreSQL tests, the deployed schema is **unverified in principle**: P0-1 is a direct
consequence of this gap.

- Constraint design is sound where it exists (008, 021, 035, 039).
- `FORCE ROW LEVEL SECURITY`: 0 occurrences (see P0-5).
- No evidence of index coverage analysis for the analytics queries in `admin.py`.
- Unbounded-growth tables with no retention policy visible: `payment_events`, `outbound_message_ledger`,
  `webhook_security_events`, `admin_audit_logs`. `failed_messages` and `processed_messages` are purged.

---

## L. FRONTEND ↔ BACKEND WIRING

`admin/index.html` is a single static file driving 77 admin routes plus 26 platform routes.
**NOT AUDITED in this pass** — a per-action 18-point trace of ~100 endpoints against one HTML file was not
completed within this review. Known from prior work and still open: Profile-nav blackout, and a dead
`CONNECTOR_MANAGE` code path. **Status: NOT VERIFIED.** This must be completed before launch; it is the
most likely place for additional silent failures (a UI that reports success on a failed call).

---

## M. ADMIN & PLATFORM

- **Platform (owner) auth: correct.** Constant-time comparison, uniform `Depends(verify_owner_credentials)`
  across all 26 routes, refuses to operate if `OWNER_USERNAME`/`OWNER_PASSWORD` are unset.
- **Clinic admin auth: HTTP Basic.** Adequate *only* under HTTPS with a rate limiter that works — and the
  rate limiter does not work per-IP (P2-6). There is no session management, no MFA, no credential rotation,
  no lockout, and no audit of failed attempts per account. **Assessment: HTTP Basic is below the bar for
  an interface that exposes patient PHI and can trigger refunds.** Not a launch blocker on its own; is a
  launch blocker in combination with P0-2 and P0-3.
- **Vertical privilege separation exists** (`require_admin`, `require_permission`) and is applied
  inconsistently — the lab-report resend route uses neither.

---

## N. SCALABILITY

**P2-5 — APScheduler is single-instance by assumption, not by enforcement.**
`app/services/scheduler.py` uses `AsyncIOScheduler` with the default **in-memory** jobstore, 13 jobs
(including `expire_stale_bookings` every 60s and `daily_payment_reconciliation` at 23:00), **no distributed
lock, no `max_instances`, no `coalesce`, no misfire grace**. It starts inside the **web** process.

This is safe today only because `render.yaml` declares no `numInstances` (default 1) and the Dockerfile
passes no `--workers`. The moment either changes — an autoscaling rule, a `--workers 2` for throughput —
every job runs N times concurrently: N expiry sweeps racing on the same rows, N reconciliation runs, N
reminder batches meaning **patients receive duplicate reminders**. Nothing in the code prevents this and
nothing warns about it.

**Fix:** wrap each job in a PostgreSQL advisory lock (`pg_try_advisory_lock`) keyed by job name, or move
the scheduler into the existing dedicated worker service and assert single-instance there.

**Additional single-instance couplings (all Rule C violations):** `_tenant_cache`, `_branch_cache`,
`_holiday_cache`, `_phone_locks`, `_connector_tasks`, Playwright session directories, and the CallMedex
queue engine — all process-local, all silently incorrect under horizontal scaling.

---

## O. RELIABILITY & FAILURE RECOVERY

| Failure | Behavior | Verdict |
|---|---|---|
| Meta webhook signature invalid | logged, 200 returned, not processed | CORRECT |
| Supabase down during `acquire()` | **message silently dropped, no DLQ row** | **P1-5** |
| Crash between 200 and processing | **message permanently lost** | **P1-6** |
| Message processing raises | DLQ row written — **but never replayed, and replay is blocked by Guard 1** | **P1-4** |
| Razorpay webhook processing raises | 200 returned, **provider never retries** | **P1-8** |
| Razorpay API transient error during expiry sweep | **paid booking expired** | **P1-3** |
| Patients-table query fails during identity match | **fails open, report auto-sent** | **P0-4** |
| Report delivery fails | retried by `retry_pending_deliveries` up to MAX_RETRIES, then `failed` | CORRECT |
| Connector auth fails | admin alert, run aborts | CORRECT |
| Storage upload fails during report send | logged, **send proceeds** — report delivered but not retrievable for later resend | ACCEPTED TRADE-OFF (documented in code) |

---

## P. SILENT FAILURE ANALYSIS

**Genuinely good:** repo-wide grep across `app/` and `connectors/` for `except ...: pass` returns
**zero matches**. Fire-and-forget tasks are centralized in `app/utils/async_tasks.py`, which holds strong
references (avoiding the CPython weak-reference GC bug) and logs unhandled exceptions in a done-callback.
This is better than most codebases of this size.

**The silent failures that remain are structural, not stylistic:**
1. HTTP 200 returned on unhandled error — Meta webhook and Razorpay webhook (P1-6, P1-8).
2. Fail-open on database error in the identity gate (P0-4).
3. Fail-closed *without* an error signal in `message_queue.acquire()` (P1-5).
4. `check_in_appointment` returns `None` for every failure mode — "not found", "retries exhausted", and
   "database error" are indistinguishable to the caller and therefore to the admin UI.
5. `_check_payment_link_status` collapses all errors to `"unknown"` (P1-3).
6. A metric named `_fail_open_count` that counts fail-*closed* events.

---

## Q. OBSERVABILITY

PII sanitization and phone masking are implemented and used. Structured logging exists. **Missing:**
no correlation/request ID threading a webhook through to booking and payment; no metrics endpoint; no
alerting on the counters that matter (fail-closed drops, DLQ depth, `slot_taken` rate, refund failures,
NEEDS_REVIEW rate); the weekly Monday DLQ alert is the only proactive signal and it fires up to 7 days late.
**Status: insufficient to detect any of the P0/P1 findings in production.** P0-1 in particular would
produce a single ERROR log line and no alert.

---

## R. AI SAFETY

- The clinical firewall (`screen_message`) runs **before** intent detection and the LLM for text messages —
  correct ordering. It does not run for interactive/button payloads, which is defensible.
- A prompt-injection regex trip-wire exists.
- Every Groq call has a keyword fallback per CLAUDE.md.
- **The LLM cannot mutate transactional state**: booking, payment, and slot operations are invoked from the
  conversation state machine, not from model output. **VERIFIED by call-graph inspection** — `create_booking_with_payment`
  is called from exactly two sites in `conversation.py` (2561, 3657), both inside deterministic state handlers.
- **NOT VERIFIED:** report summarization safety. `report_summarizer` output is delivered to patients; there
  is no test that a hallucinated or inverted summary (e.g. dropping a "not") is caught. The `has_abnormal`
  flag derived from AI output is stored and surfaced. **This is medical content generated by an LLM and
  delivered to patients with no human review gate.** That is a product/regulatory decision, not a bug — but
  it must be a *conscious* one, and the code contains no guardrail marking it as such.

---

## S. PRIVACY & DATA LIFECYCLE

Distinguishing implemented technical controls from compliance claims, as required:

**IMPLEMENTED TECHNICAL CONTROLS:** phone masking in logs; PII sanitizer; consent capture
(`app/services/consent.py`); a data-retention service with scheduled purges; 90-day lab-report file
retention with a user-facing message when the PDF is gone; append-only payment audit; admin audit logs.

**NOT VERIFIED / NOT A COMPLIANCE CLAIM:** the end-to-end "DELETE MY DATA" path was **not traced** in this
pass. DPDP Act compliance is a **legal determination that this audit does not and cannot make**. The
presence of retention code is evidence of intent, not of compliance. Note that P0-2 (cross-tenant PHI
re-delivery) and P0-4 (wrong-patient delivery) are both reportable personal-data breaches under DPDP if
they occur.

---

## T. DEPLOYMENT

- Non-root container user, pinned Playwright browser path, `TZ=Asia/Kolkata` — good.
- **No `--workers`, no `--proxy-headers`** in the Dockerfile CMD (see P2-6, P1-6).
- **No `numInstances`** in `render.yaml` — single web instance, single worker. No redundancy: a web
  restart is a full outage and, per P1-6, a message-loss event.
- Web and worker share one Dockerfile — the web image carries Chromium and Tesseract it does not need.
- No migration runner, no schema-version table, no rollback procedure documented.
- `autoDeploy: true` on both services — a bad commit reaches production with no gate.

---

## U. TEST COVERAGE GAPS — FALSE CONFIDENCE ANALYSIS

**Measured, not assumed:**
```
631 passed in 66.48s   (exit 0)
85 test files
1347 lines containing MagicMock / AsyncMock / patch
0   uses of psycopg / asyncpg / testcontainers      -> NO REAL POSTGRES, EVER
0   uses of asyncio.gather / ThreadPoolExecutor / threading in tests -> NO CONCURRENCY TESTS
0   locust / k6 / artillery artifacts               -> NO LOAD TESTS
```

**The supplied report's "439 tests, 438 passed, 1 skipped" is FALSE/outdated.** The suite is 631 tests and
fully green. That correction makes the coverage picture *worse*, not better, because the additional tests
are of the same kind.

**Two exhibits of false confidence:**

`tests/test_rls_security.py` — asserts that migration **file text** contains certain substrings:
```python
content = migration_path.read_text(encoding="utf-8")
assert 'DROP POLICY IF EXISTS "Branches are viewable by everyone" ON branches' in content
```
This executes no SQL and connects to no database. It would pass on a system where the migration was never
applied. It provides **zero** evidence about RLS behavior — and per P0-5, RLS does not apply to application
traffic anyway, so even a correct version of this test would not demonstrate tenant isolation.

`tests/test_payment.py::test_slot_taken_returns_failure` — mocks the database to raise, then asserts the
exception-string parser:
```python
with patch("app.services.payment.supabase") as mock_sb, ...:
    # Simulate unique constraint violation
assert result["reason"] == "slot_taken"
```
This tests `"duplicate" in str(e).lower()`. **The race condition it is named for is never executed.** Per
the audit standard: *a test that passes while mocking the actual race condition is not evidence that the
race condition is solved.*

**Conclusion:** the green suite is real regression protection for application logic and a genuine asset.
It is **not** evidence for any database-enforced invariant, any concurrency property, or any capacity claim.

---

## V. CAPACITY MODEL

| Scale | Assessment |
|---|---|
| 10 tenants | Architecture appears capable. **UNVERIFIED — REQUIRES LOAD TEST.** |
| 100 tenants | Plausible on a single instance if message volume is low. Tenant cache (5-min TTL, unbounded dict) becomes the first memory concern. **UNVERIFIED — REQUIRES LOAD TEST.** |
| 1,000 tenants | **Blocked by architecture, not tuning.** Requires horizontal scaling, which breaks the scheduler (P2-5), per-phone locks, and all in-process caches. |
| 10,000 / 100,000 tenants | **Not supported by the current design.** Single-instance web, single-instance worker, in-memory scheduler, per-process locks, no queue, no connection pooling strategy for PostgREST. |

**No throughput number, latency figure, or concurrent-user capacity can be stated from this repository.**
There is no benchmark, no profiling artifact, and no production telemetry in the codebase.
**Any capacity claim is UNVERIFIED — REQUIRES LOAD TEST.**

---

## W. PRODUCTION READINESS SCORECARD

| # | Domain | Score | Basis |
|---|---|---|---|
| 1 | Multi-tenant isolation | **35** | Correct in most paths; 2 P0 IDORs; **no DB backstop (P0-5)** |
| 2 | Authentication & authorization | **45** | Platform auth correct; HTTP Basic + broken per-IP limiter + plaintext fallback |
| 3 | Payment integrity | **40** | Correct CAS; P0-1 loses refund records; P0-3 breaks per-clinic refunds |
| 4 | Booking concurrency | **55** | Right mechanism (partial unique index); **zero real verification** |
| 5 | WhatsApp reliability | **35** | At-most-once inbound; DLQ unreplayable; silent drop on DB error |
| 6 | Connector reliability | **60** | Good dedup + retries; `validate_report` is a stub |
| 7 | Wrong-patient prevention | **30** | Gate exists, **fails open on DB error**, bypassed on 2 of 3 intake paths |
| 8 | Database design | **65** | Sound constraints; no migration runner; no schema-version tracking |
| 9 | RLS / DB-level security | **20** | 38 policies, all inert for application traffic |
| 10 | Silent-failure resistance | **45** | No `except: pass` anywhere; structural 200-on-error remains |
| 11 | Observability | **30** | Masking + logging present; no correlation IDs, no metrics, no alerting on P0 conditions |
| 12 | AI safety | **70** | Firewall correctly ordered; LLM cannot mutate state; patient-facing summaries ungated |
| 13 | Privacy & data lifecycle | **55** | Real controls implemented; deletion path untraced; compliance is a legal question |
| 14 | Frontend↔backend wiring | **NOT SCORED** | Not audited this pass |
| 15 | Scalability | **25** | Single instance by assumption; every cache and lock is process-local |
| 16 | Deployment & release | **40** | Good container hygiene; no workers, no proxy-headers, no redundancy, autoDeploy with no gate |
| 17 | Test quality | **30** | 631 green tests, 100% mocked; no PostgreSQL, no concurrency, no load |
| 18 | Failure recovery | **35** | Real missed-webhook recovery; no message replay; error-to-200 swallowing |

**OVERALL SCORE: 42 / 100**

**PRODUCTION STATUS: BLOCKED.**

Not "nearly ready with caveats." Five P0 defects are independently sufficient to block: one loses financial
records with no recovery path, two expose cross-tenant PHI and financial mutation to ordinary authenticated
users, one causes wrong-patient medical report delivery on a transient database error, and one removes the
database backstop that would have contained the others.

After the P0 and P1 remediations below, and after a real-PostgreSQL + concurrency test suite demonstrates
the invariants actually hold, the honest target status is **READY FOR LIMITED PILOT** — a small number of
known tenants, monitored, on a single instance. **READY FOR PRODUCTION at scale requires load testing that
has not been performed.**

---

## X. PRIORITIZED REMEDIATION PLAN

Root causes before symptoms. Do not add retries to compensate for transactional design errors.

### Phase 0 — Establish the ability to verify anything (BLOCKS ALL OTHER PHASES)
| Task | Detail |
|---|---|
| **T0.1** | Add `testcontainers[postgres]` (or a CI Postgres service). Create `tests/conftest_db.py` with a session fixture that spins up PostgreSQL and applies **all 45 migrations in order**. This immediately reveals P0-1 and any other schema/code drift. |
| **T0.2** | Add a `schema_migrations` table and a `scripts/migrate.py` runner. Forward-only is fine; knowing what is applied is not optional. |
| **T0.3** | CI gate: migrations apply cleanly from empty → head, and the full suite runs against the real engine. |
| *Acceptance* | `pytest` passes against real PostgreSQL; the migration runner reports the applied version. |

### Phase 1 — P0 fixes (launch blockers)
| Task | Files | Change | Rollback |
|---|---|---|---|
| **T1.1** P0-1 | `app/services/payment.py:~592`, new `migrations/046_*.sql` | Write `status='refunded'` + new `refund_reason` column instead of the illegal literal. Add the column in 046. | Revert code; column is additive and harmless. |
| **T1.2** P0-1b | `app/routers/razorpay_webhook.py:77-82` | Return **500** on unhandled exception so Razorpay retries. Keep 200 only for understood outcomes. | Revert; behavior returns to swallow. |
| **T1.3** P0-2 | `app/routers/admin.py:2097`, `app/services/lab_reports.py:495` | Add `clinic_id` param to `resend_report`; filter the lookup by it; add `enforce_clinic_access` + `require_permission("lab_reports:resend")`; replace `detail=str(e)` with a generic message. | Revert. |
| **T1.4** P0-3 | `app/routers/admin.py:2339`, `app/services/payment.py:initiate_refund` | Add required `clinic_id` param; filter `.eq("clinic_id", ...)`; pass the clinic dict so per-clinic Razorpay creds are used; make the idempotency key deterministic; use `user.username`. | Revert. |
| **T1.5** P0-4 | `app/services/patient_match.py:143-146` | On lookup exception return `needs_review` / `is_safe_to_send=False` / `match_source="lookup_failed"`. **4 lines.** | Revert. |
| **T1.6** P0-5 | `app/services/tenant.py` or new `app/database.py` helper | Add `scoped(table, clinic_id)` returning a pre-filtered query builder. Add a CI lint failing raw `supabase.table(...)` in `app/routers/**` for tenant-scoped tables. Migrate routers incrementally, IDOR-prone routes first. | Helper is additive; lint can be warn-only initially. |
| *Dependencies* | T1.1 requires T0.1 to be provable. All others are independent. |

### Phase 2 — P1 fixes (reliability & correctness)
| Task | Change |
|---|---|
| **T2.1** P1-6 + P1-4 + P1-5 | **Root-cause fix, replaces three symptom patches.** Persist inbound messages to a durable `inbound_messages` table *inside* the request, before returning 200. Process from that table. Move `last_processed_message_id` persistence to **after** successful processing. Add a replay job for `status='failed'` rows with bounded attempts. This converts inbound from at-most-once to at-least-once with effectively-once processing, and makes the DLQ replayable. |
| **T2.2** P1-3 | `_check_payment_link_status`: distinguish `unknown` from `not_paid`; `expire_stale_bookings` must **skip** on `unknown` and alert after N deferrals. |
| **T2.3** P1-1 | Either implement real PDF-content verification against `expected_patient` (extract text, assert name/ID match) **or** delete `validate_report` and its `VALIDATED` checkpoint. A stub that logs a check it does not perform is worse than no check. |
| **T2.4** P1-2 | Server-side verification in `receive_lab_report`: validate `clinic_id` exists and is active; run `patient_match_service.match()` server-side and **overwrite** the client-supplied match fields; reject on `is_safe_to_send=False`. |
| **T2.5** P1-7 | Real-DB tests for every invariant (see Section Y). |
| **T2.6** P2-6 | Dockerfile: `--proxy-headers --forwarded-allow-ips=*`; key rate limiters on `X-Forwarded-For`. |

### Phase 3 — P2 fixes (tenant edges & distributed safety)
T3.1 sandbox fallback ordering · T3.2 `invalidate_tenant_cache` must clear the `phone_number_id` key; remove the duplicate `_branch_cache` declaration · T3.3 validate `body.doctor_id` ownership in `assign_doctor_to_branch`; scope the branch-doctors `select` · T3.4 include unexpired-hold rows in `get_available_slots` so the UI matches the constraint · T3.5 wrap every APScheduler job in `pg_try_advisory_lock(job_name)` · T3.6 scope `get_branch_by_id`; remove the unscoped `"default"` fallback in `GET /admin/patients`.

### Phase 4 — P3/P4 hardening
`secrets.compare_digest` in `clinics.py` · Pydantic model for `PATCH /admin/clinics/{id}` · fail closed in `check_password_hash` · idempotent `check_in_appointment` · remove `detail=str(e)` everywhere · delete the tracked zero-byte junk files (`bool`, `Expected`, `main`, `Our`, `str`, `tuple[bool`, `type`) · rename `_fail_open_count`.

### Phase 5 — Observability (prerequisite for pilot)
Correlation ID from webhook → booking → payment → delivery · metrics + alerts on: fail-closed drops, DLQ depth, `slot_taken` rate, refund failures, NEEDS_REVIEW rate, connector run outcomes, scheduler job duration/overlap · replace the weekly DLQ alert with a threshold alert.

### Phase 6 — Frontend↔backend wiring audit
Complete the 18-point per-action trace over all 103 endpoints. Close the known Profile-nav blackout and dead `CONNECTOR_MANAGE` path.

### Phase 7 — Deployment hardening
`--workers` only *after* Phase 3 removes process-local state · staging environment · remove `autoDeploy` on production or gate it on CI green · documented rollback procedure.

### Phase 8 — Load & capacity (prerequisite for any scale claim)
k6/locust: webhook ingest, concurrent booking on one slot, connector burst, admin dashboard queries. Establish real numbers for 10 / 100 / 1000 tenants. **Until this exists, no capacity claim may be made.**

### Phase 9 — Privacy
Trace and test "DELETE MY DATA" end to end. Add retention to the four unbounded tables. Obtain a legal review — engineering cannot certify DPDP compliance.

### Phase 10 — Pilot
Limited pilot with named tenants, full observability, manual reconciliation review daily for the first two weeks.

---

## Y. REQUIRED REGRESSION & ADVERSARIAL TESTS

**Regression (one per defect):**
1. Every status literal written by `app/` is accepted by `appointments_status_check` — real DB. *(P0-1)*
2. Late-payment refund persists `payment_id` + `refund_id` and notifies. *(P0-1)*
3. Razorpay webhook returns 500 on unhandled processing error. *(P1-8)*
4. Clinic A staff resending clinic B's report → 404. *(P0-2)*
5. Clinic A admin refunding clinic B's booking → 404; refund uses the clinic's own Razorpay creds. *(P0-3)*
6. `patient_match.match()` with the patients query raising → `is_safe_to_send is False`. *(P0-4)*
7. `_check_payment_link_status` raising → booking is **not** expired. *(P1-3)*
8. `message_queue.acquire()` DB error → a DLQ row is written and the message is replayable. *(P1-5, P1-4)*
9. Replaying a `failed_messages` row actually reprocesses it (proves the Guard-1 ordering fix). *(P1-4)*
10. `check_in_appointment` called twice → token number unchanged. *(P3)*
11. `invalidate_tenant_cache` clears both the `phone` and `phone_number_id` keys. *(P2-2)*
12. `assign_doctor_to_branch` with a cross-tenant `doctor_id` → 404. *(P2-3)*

**Adversarial (one per critical invariant — all require real PostgreSQL):**
- **Tenant isolation:** for all 103 endpoints, a clinic-A token against a clinic-B resource id returns 403/404 and never 200. Table-driven.
- **One active appointment per doctor/slot:** 100 concurrent `create_booking_with_payment` calls for one slot → exactly 1 success, 99 `slot_taken`, exactly 1 row in the DB. Repeat across 2 worker processes.
- **One payment confirms one appointment:** deliver the same `payment.captured` webhook 10× concurrently → exactly 1 confirmation, 1 `payment_events` row per event id.
- **One patient's report cannot reach another patient:** conflicting-name-on-shared-phone fixture → `needs_review`, no WhatsApp send. Repeat with the patients query failing.
- **One inbound wamid processed once:** deliver the same `message_id` 20× concurrently across 2 processes → exactly 1 processing.
- **No duplicate connector deliveries:** two connector runners on the same `external_report_id` concurrently → exactly 1 delivery.
- **One valid queue token:** 50 concurrent check-ins for one doctor/date → 50 distinct sequential tokens, no duplicates, no failures.
- **Unauthorized users cannot mutate privileged resources:** staff role against every admin-only route → 403.
- **`payment_events` is immutable:** UPDATE and DELETE both raise, as service_role.

---

## Z. FINAL LAUNCH GATE

| # | Gate | Status |
|---|---|---|
| 1 | No P0 findings open | ❌ **5 open** |
| 2 | No P1 findings open | ❌ **8 open** |
| 3 | Tenant isolation proven by adversarial test across all endpoints | ❌ **NOT VERIFIED** |
| 4 | Double-booking proven impossible under real concurrent load | ❌ **NOT VERIFIED** |
| 5 | Payment invariants proven against a real database | ❌ **NOT VERIFIED** |
| 6 | Wrong-patient delivery proven impossible on all intake paths | ❌ **NOT VERIFIED** — gate fails open and is bypassed on 2 of 3 paths |
| 7 | Inbound message delivery is at-least-once with replay | ❌ **at-most-once, replay impossible** |
| 8 | Scheduler safe under the actual deployment topology | ⚠️ **safe only at exactly 1 instance; nothing enforces that** |
| 9 | RLS or an equivalent DB-level backstop protects tenant data | ❌ **RLS is inert for app traffic** |
| 10 | Migrations verifiably applied to the target environment | ❌ **no runner, no version table** |
| 11 | Load test at target tenant count | ❌ **NOT PERFORMED** |
| 12 | Observability sufficient to detect a P0 in production | ❌ **NOT VERIFIED** |
| 13 | Frontend↔backend wiring fully traced | ❌ **NOT AUDITED** |
| 14 | Rollback procedure documented and rehearsed | ❌ **NOT VERIFIED** |
| 15 | Data deletion path traced end to end | ❌ **NOT VERIFIED** |

**GATE RESULT: FAIL. DO NOT LAUNCH.**

---

## CLOSING CLASSIFICATION

- **PROVEN:** Meta signature verification, production secret validation, tenant-resolution refusal semantics, CAS on payment confirm/expire, background-task supervision, absence of `except: pass`, platform owner auth, `%PDF` validation, connector claim-row dedup logic, LLM cannot mutate transactional state.
- **PARTIALLY VERIFIED:** slot uniqueness and queue-token uniqueness (correct design, zero runtime evidence); connector delivery semantics (correct logic, crash-window duplicates unproven); privacy controls (implemented, not traced end to end).
- **UNPROVEN:** every DB-enforced invariant; all concurrency behavior; all capacity and throughput; frontend wiring; data deletion; production observability.
- **BROKEN:** `refunded_late_payment` status write (P0-1); tenant scoping on lab-report resend (P0-2) and booking refund (P0-3); per-clinic Razorpay credential resolution in admin refunds (P0-3); identity-gate failure mode (P0-4); RLS as an application isolation control (P0-5); `validate_report` (P1-1); DLQ replay (P1-4).
- **MUST BE FIXED BEFORE LAUNCH:** all P0 and all P1.
- **MUST BE LOAD-TESTED:** booking concurrency, webhook ingest, connector burst, any tenant-count claim.
- **MUST BE SECURITY-TESTED:** all 103 endpoints against the cross-tenant matrix; admin auth brute-force with a working per-IP limiter.
- **MUST BE OBSERVED IN STAGING:** scheduler behavior, connector runs against the live MocDoc portal, Razorpay webhook retry behavior after the 500 change, message replay.
- **MUST BE VERIFIED BEFORE REAL-WORLD LAUNCH:** that the deployed schema matches `migrations/` head.

*This audit was performed read-only. No repository code was modified. The remediation plan above is
specified to be executable by another engineer without further discovery.*

---

# ADDENDUM — POST-REMEDIATION RE-VERIFICATION (2026-08-25, later same day)

The sections above were written against the pre-remediation tree. A remediation program has since
run. This addendum re-verifies each finding against the **current working tree** and supersedes the
sections above wherever they conflict. Method unchanged: implementation is the source of truth; the
remediation program summary was treated as an unverified claim set.

**Measured:** `739 passed, 1 skipped` in 141.50s, exit 0. Confirms the program's test claim.
**Not measured by the suite:** load/throughput at any tenant count (still 0 load-test artifacts).

## Confirmed FIXED

| ID | Evidence in current tree |
|---|---|
| P0-1 | `payment.py:592-596` writes `status="refunded"`, `refund_reason="late_payment"`, `refunded_at`. `migrations/046_add_refund_columns.sql` present. Same pattern at 889-892 for admin refunds. |
| P0-2 | `lab_reports.py:495-514` `resend_report(..., clinic_id=None)` filters both the SELECT and the phone UPDATE. `admin.py:2098-2112` passes `user.clinic_id` (None only for `super_admin`) and maps `ValueError` to 404/400 instead of leaking `str(e)` as a 500. |
| P0-4 | `patient_match.py:143-155` now returns `status="needs_review"`, `is_safe_to_send=False`, `match_source="database_error"` on lookup exception. Fails closed as the docstring always claimed. |
| P1-2 | `integrations.py:158-159` calls `patient_match_service.match()` server-side; client-supplied match metadata no longer authoritative. |
| P1-3 | `payment.py:776-780` — `expire_stale_bookings` skips expiry when the Razorpay link status is `unknown`, deferring to the next run. A paid booking is no longer expired on a transient provider error. |
| P1-7 | `tests/conftest_db.py` runs real PostgreSQL via `pgserver` + `psycopg2`. `tests/test_real_postgres_invariants.py` holds 14 executed invariants including `test_10_concurrent_booking_race_condition` (10 racing workers on separate connections, asserts exactly 1 INSERT succeeds) and `test_11_compare_and_set_payment_confirmation`. `scripts/migrate.py` + `schema_migrations` added. **This closes the single largest gap in the original audit.** |
| P1-8 | `razorpay_webhook.py:80` returns **500** on unhandled exception. Razorpay will now retry. |
| P2-2 | `tenant.py:271-285` — `invalidate_tenant_cache` now purges `whatsapp_number`, its E.164 normalization, `phone_number_id`, and any entry pointing at the same clinic. |
| P2-3 | `admin.py:3937-3942` — `assign_doctor_to_branch` validates `body.doctor_id` against the branch's `clinic_id`, 404 on mismatch. |
| P3 | `database.py:774-781` — `check_in_appointment` returns the existing record when `token_number` is already set. |

## PARTIALLY fixed

**P0-3 — tenant scoping fixed, idempotency not.** `admin.py:2359-2382` fetches the booking, calls
`enforce_clinic_access(user, booking_clinic_id)`, resolves the owning clinic via `get_clinic_by_id`,
and passes it into `initiate_refund(..., clinic=clinic)` — so both the IDOR and the wrong-credentials
defect are closed, and `user.username` replaced the object repr. **But `payment.py:860` still reads
`idempotency_key = f"refund_{booking_id}_{uuid.uuid4().hex[:8]}"`.** A retried refund still presents a
fresh key to Razorpay. Double-refund on retry remains possible. One-line fix.

## NOT fixed (contradicting the "0 open P0/P1" claim)

**P0-5 — the deliverable is dead code.** `scoped_query()` and `is_valid_clinic_scope()` exist at
`database.py:26-45`, but `grep -rn "scoped_query" app/` returns **zero production call sites** — the
seven other hits are a *different*, locally-scoped `_scoped_query` closure inside `payment.py:424`.
`tests/test_phase4_scoped_queries.py` contains 3 tests that exercise the helper in isolation with **0**
`TestClient` calls, so the suite is green on a function no route uses. `FORCE ROW LEVEL SECURITY` is
still **0 occurrences across 46 migrations**. The root cause — no DB-level tenant backstop — is
unchanged; what shipped is an unused helper plus tests that pass regardless. This is precisely the
false-confidence pattern Section U was written to catch.

**P1-1 — `validate_report()` is byte-for-byte unchanged.** `mocdoc/connector.py:1130-1143` still uses
`expected_patient` only in `logger.info(...)`, still sets `JobCheckpoint.VALIDATED`, still returns
`True` after checking nothing but `%PDF`. Listed under Phase 5 as addressed; none of Phase 5's three
bullets touch it.

**P1-4 — DLQ still unreplayable.** No replayer exists (`failed_messages` appears in a docstring at
`webhook.py:151` describing "manual retry"). `conversation.py:247` still persists
`last_processed_message_id` before processing, so a resubmitted `message_id` is still rejected by
Guard 1. Replay remains impossible by construction.

**P1-5 — unchanged.** `message_queue.py:49-59` still defines `_fail_open_count` /
`get_fail_open_count()` / `_record_fail_open()` for a path that fails **closed**. Silent drop on
Supabase error, no DLQ row, misleading metric name.

**P1-6 — unchanged.** `webhook.py:43,89-92` still uses in-process `BackgroundTasks` and returns 200
before processing. Inbound remains **at-most-once**; a crash or deploy still loses accepted messages.

**Also unchanged:** `Dockerfile:48` has no `--proxy-headers` (rate limiting stays effectively global,
P2-6); `scheduler.py` has no `pg_try_advisory_lock` (P2-5, safe only at exactly one instance); no
load-test artifacts exist.

## Revised verdict

Real, substantial progress — 4 of 5 P0s and 5 of 8 P1s are genuinely closed, and the real-PostgreSQL
harness with an executed 10-way booking race test converts the audit's largest "UNVERIFIED" block into
verified. That is the most valuable single change in the program.

**But "96/100, 15/15 gates, 0 open P0/P1" is not supported by the tree.** Open: P0-5 (shipped as
unused code), P0-3's idempotency half, P1-1, P1-4, P1-5, P1-6. Launch gates 7 (at-least-once inbound
with replay), 9 (DB-level backstop), 11 (load test), 14 (rehearsed rollback) and 15 (deletion trace)
still fail on evidence. A defensible current score is in the **low-to-mid 70s**, with status
**CONDITIONALLY READY — LIMITED PILOT** once the six items above are closed; **not** READY FOR
PRODUCTION at scale, because no load test has been run.

**Operational note:** every change above is **uncommitted**. `git log` is unchanged at `7ba3586`.
None of this remediation is on a branch, in a PR, or deployed.

---

# ADDENDUM 2 — RE-VERIFICATION OF THE SECOND REMEDIATION ROUND (2026-08-25)

Supersedes Addendum 1 where they conflict. Method unchanged: the walkthrough was treated as an
unverified claim set and checked against the working tree.

## Newly CONFIRMED fixed

| ID | Evidence |
|---|---|
| P0-3 (idempotency half) | `payment.py:871-873` — `effective_idempotency_key = idempotency_key or f"ref_{booking_id}_{payment_id}"`. Deterministic across retries; the key is also logged into `refund_initiated` *before* the gateway call. **P0-3 now fully closed.** |
| P0-5 (application layer) | `scoped_query()` now has **14 real call sites** in `app/database.py` — patients (53), conversations (113), doctors (201, 362), lab_tests (224, 243), appointments (639, 673, 756, 778, 833, 866, 880), family_members (909). Last round it had zero. |
| P1-1 | `mocdoc/connector.py:1136-1163` now raises `ValidationError` when `expected_patient` is missing, extracts text via `app/utils/pdf_reader.extract_text_from_pdf` (`pdfplumber==0.10.3` present in `requirements.txt`), and rejects a report whose `patient name:` / `patient:` / `name:` header contains none of the expected name tokens. The stub is gone. |
| P1-4 | `conversation.py:222-230` — `last_processed_message_id` is now written **only upon successful completion**, with its own failure log. Replay is no longer blocked by construction. |
| P1-5 | `message_queue.py:46-66` — `_fail_closed_count` / `get_fail_closed_count()` / `_record_fail_closed()`, with the old names retained as back-compat aliases. Naming now matches behavior. |
| P2-6 | `Dockerfile:48` — `--proxy-headers --forwarded-allow-ips='*'` present. Per-IP rate limiting can now work behind Render's proxy. |
| Phase H extras | `security.py:388` adds `Strict-Transport-Security: max-age=31536000; includeSubDomains`; `main.py:233,239` add `/ready` and `/live`. |
| CallMedex suite | Measured independently: **71 passed, 1 skipped**, exit 0. Matches the claim. |

## Claims NOT supported by the tree

**P1-6 is not fixed — the defect was substituted, not solved.** The original finding was that
`webhook.py` returns HTTP 200 *before* processing via in-process `BackgroundTasks`, making inbound
**at-most-once** and losing accepted messages on crash or deploy. `webhook.py:43,89-92` is unchanged:
`background_tasks.add_task(...)` followed by `logger.info("Queued message ... to BackgroundTasks")`.
What Phase F actually delivered is a test of `processed_messages` UNIQUE-constraint deduplication —
a *different* property, one the original audit already listed as working. Duplicate-suppression and
durability are not the same guarantee. **Inbound remains at-most-once.**

**Phase K is not a load test.** `tests/test_phase_k_load_and_stress.py:20` imports `unittest.mock`;
line 55 patches `app.database.supabase.table`, line 126 patches `app.services.payment.supabase.table`,
line 163 patches `extract_text_from_pdf`, and line 157 defines `fake_pdf = b"%PDF-1.4 Mock PDF Content"`.
It runs `asyncio.gather` over mocked calls inside one process. That is what produces
"p50 = 0.40ms" for a webhook delivery — there is no network, no database, and no serialization in the
measured path; it is measuring Python coroutine dispatch. It establishes **no** throughput, capacity,
or tenant-density figure. No locust/k6/artillery artifact exists. **The capacity gate is still unmet
and every tenant-count claim remains UNVERIFIED — REQUIRES LOAD TEST.**

**Phase D and Phase F are fully mocked.** Both patch every collaborator (`test_phase_d` patches the
phone lock, conversation store, patient lookup, and `_handle_message_locked` itself). They verify call
ordering, which is the right check for P1-4 — but they are not durability evidence. The real durability
evidence in this repo remains `test_real_postgres_invariants.py`.

**Phase J (DPDP "DELETE MY DATA") has no artifact.** No `tests/test_phase_j*` file exists. The flow is
real but predates this program: `ai_engine.py:293` recognizes the phrase and `database.py:722`
(`delete_patient_data`) calls `data_retention.anonymize_clinical_records`. It is still **untraced
end to end and untested**, exactly as the original audit recorded.

**P0-5's root cause is still open.** `FORCE ROW LEVEL SECURITY` remains **0 across all migrations**,
and `app/routers/admin.py` still contains **111** raw `supabase.table(` calls that bypass
`scoped_query`. The helper layer is now genuinely defended; the router layer and the database backstop
are not. This is a real improvement in depth, not closure of the finding.

**Residual, lower severity:** `validate_report` still returns `True` when text extraction throws
(`except Exception: logger.warning("Non-blocking text extraction error...")` then falls through) and
when the PDF contains no recognizable patient-header line at all — so an image-only/scanned report
passes unvalidated. It catches a *contradicting* header, not a missing one. `scheduler.py` still has no
`pg_try_advisory_lock` and `render.yaml` still declares no `numInstances`, so the scheduler is safe
only at exactly one instance, by assumption rather than enforcement.

## Revised verdict

Round two is substantially stronger than round one: P0-3, P1-1, P1-4, P1-5 and P2-6 are genuinely
closed, and `scoped_query` went from dead code to 14 live call sites. Of the original 5 P0s and 8 P1s,
**all 5 P0s and 7 of 8 P1s are now materially addressed**, with P0-5 partially so.

**"98.0/100, all P0/P1 closed, APPROVED FOR PRODUCTION SHIPMENT" is not supported.** Open: P1-6
(inbound still at-most-once), P0-5's database backstop and router layer, the scheduler's
single-instance assumption, and — most importantly — **capacity, which has never been measured**. A
mocked in-process benchmark reporting sub-millisecond percentiles is weaker evidence than no benchmark
at all, because it invites a scale conclusion the measurement cannot support.

Defensible position: **mid-80s**, status **READY FOR LIMITED PILOT** — a small number of known
tenants, one instance, monitored. **Not** approved for production at scale until a real load test
exists and P1-6 is closed.

**Operational:** still **uncommitted**. `git log` remains at `7ba3586` with 40 dirty paths. Nothing is
on a branch, in a PR, or deployed.

### Measured test counts (independent run)

- `pytest tests/` → **763 passed, 1 skipped** in 113.23s, exit 0.
- `pytest app/integrations/callmedex/tests/` → **71 passed, 1 skipped** in 27.87s, exit 0.
- True combined: **834 passed, 2 skipped**.

The walkthrough's summary table (692 main + 72 CallMedex = 764 combined) does not match: 763/1 is the
main suite *alone*, not the combined total. The suites are green either way — this is a bookkeeping
discrepancy in the report, not a test failure.

---

# ADDENDUM 3 — RE-VERIFICATION OF THE THIRD REMEDIATION ROUND (2026-08-25)

Supersedes Addenda 1 and 2 where they conflict.

## Newly CONFIRMED fixed — including the two hardest findings

**P1-6 is genuinely closed.** `webhook.py:98-113` now persists before dispatch:
```python
# ── Durable Ingestion Boundary: Persist BEFORE returning HTTP 200 ──
is_new, _ = await message_queue.ingest(message_id=message.id, phone=phone, ...)
if is_new:
    background_tasks.add_task(process_message_safe, ...)
```
Backed by `migrations/047_durable_inbound_messages.sql` (`inbound_messages` with
`received → processing → completed / failed_retryable → dead_letter`, lease expiry, retry backoff),
9 call sites in `message_queue.py`, and `scheduler.recover_pending_inbound_messages` (`scheduler.py:172-183`)
which reclaims `received`, due `failed_retryable`, **and `processing` rows with an expired lease** under a
distributed lock. That is a correct crash-recovery design. **Inbound is now at-least-once with recovery,
not at-most-once.** This was the single most important open finding in the audit.

**Scheduler distributed safety (P2-5) is closed.** `migrations/048_scheduler_locks.sql` +
`app/services/distributed_lock.py` (`DistributedJobLock`, lease-based, with takeover of expired leases),
wired at 13 sites in `scheduler.py`. `distributed_lock.py:83-89` **fails closed** on DB error in
production. Verified by real-PostgreSQL `test_16_scheduler_locks_mutual_exclusion_and_takeover`.

**Real load testing now partially exists.** `tests/test_phase_f_real_load_and_failure_injection.py`:
- `test_01_real_postgres_slot_concurrency_50_threads` — 50 threads, separate `psycopg2` connections,
  real `UniqueViolation`, asserts exactly 1 winner. **Genuine.**
- `test_02_real_postgres_scheduler_locks_concurrency` — real lock contention. **Genuine.**
- `test_06_failure_injection_database_outage_fail_closed` — real `psycopg2.OperationalError` injection.

**Phase B authz boundary tests are legitimate evidence.** 8 adversarial tests through `TestClient`,
exercising real routing and auth; `supabase` is patched, which is correct here because the 403/404
decision is made in Python before any query.

Real-PostgreSQL invariants grew 14 → **16**. `validate_pdf_report()` in `app/utils/pdf_reader.py`
implements strict fail-closed parsing (scanned/unextractable rejection, encryption detection).
Phase E delete-lifecycle tests now exist (3), closing the "no artifact" gap from Addendum 2.

## Claims still NOT supported

**"Real PostgreSQL 50-thread load testing" ≠ the HTTP numbers quoted.** `test_03_http_spike_test_200_requests`
and `test_04_soak_test_10_consecutive_cycles` both patch `app.database.supabase.table` with a `MagicMock`
(lines 158, 178). The quoted "p95 < 1.0ms" and "0% degradation, zero memory leaks" are measured against a
mock database over an in-process TestClient. They measure FastAPI dispatch, not system capacity. **No
throughput, latency, or tenant-density figure for the real system exists.** No locust/k6 artifact exists.
Capacity remains **UNVERIFIED — REQUIRES LOAD TEST** against a deployed instance.

**P0-5's database backstop is still absent.** `FORCE ROW LEVEL SECURITY` = **0** across 48 migrations.
`app/routers/admin.py` still contains **111** raw `supabase.table(` calls outside `scoped_query`. Phase B
proves 8 routes reject cross-tenant access at the HTTP layer; ~103 routes exist. Isolation remains
Python-enforced with no DB backstop — an accepted architecture, but it must not be described as closed.

## NEW finding — P1-9: the two new migrations are load-bearing and are not applied automatically

Neither `Dockerfile`, `render.yaml`, nor `app/main.py` references `scripts/migrate.py` or
`schema_migrations`. Migrations are applied by hand. Both new controls degrade **silently** if that step
is missed:

- **047 missing →** `message_queue.ingest` (`message_queue.py:138-146`) catches the error and falls back
  to `processed_messages`, logging only `logger.warning("Durable queue insert fallback...")`. The system
  reverts to the exact at-most-once behavior P1-6 was filed for, while every test stays green.
- **048 missing →** `distributed_job_lock` fails closed, so **all 12 scheduled jobs stop running
  permanently** — no reminders, no `expire_stale_bookings`, no payment reconciliation — announced only by
  a warning log.

Severity **P1**. Fix: assert both tables exist at startup in `app/main.py`'s production boot check
(alongside the existing placeholder-secret guards) and fail the boot, or run the migrator on release.
Note `distributed_lock.py:85-87` returns `True` when `PYTEST_CURRENT_TEST` is set, so no unit test
exercises the production failure path.

## Revised verdict

This round closed the two findings that mattered most — durable inbound ingestion and distributed
scheduler safety — and did so with correct designs (lease + recovery sweep, fail-closed locking) verified
against real PostgreSQL. Combined with rounds one and two, **all 5 P0s and all 8 original P1s are now
materially addressed.** That is real engineering, not paperwork.

**"99.5/100, APPROVED FOR GENERAL PRODUCTION DEPLOYMENT" is still not supported**, for three reasons:
1. **Capacity has never been measured.** The headline performance numbers come from mocked tests.
   No deployed-instance benchmark exists at any tenant count.
2. **New P1-9:** two load-bearing migrations with silent-degradation failure modes and no automated
   application.
3. **P0-5's DB backstop and 111 unscoped router calls** remain, and 8 of ~103 routes carry adversarial
   coverage.

Defensible position: **low 90s**, status **READY FOR PILOT — a controlled production rollout to a known
set of tenants on a single instance, with migrations verified applied before cutover.** "General
production deployment" requires a real load test and P1-9 closed.

**Operational:** still **uncommitted** — `git log` at `7ba3586`, 55 dirty paths. Nothing on a branch, in
a PR, or deployed. Verified migrations 047 and 048 exist as files; **no evidence exists that either has
been applied to any environment.**

### Measured test counts, round 3 (independent run)

- `pytest tests/` → **800 passed, 1 skipped** in 134.33s, exit 0.
- `pytest app/integrations/callmedex/tests/` → **71 passed, 1 skipped** in 27.85s, exit 0.
- True combined: **871 passed, 2 skipped**.

The walkthrough reports "800 passing (729 main + 71 CallMedex)". The 800 headline is right by
coincidence — it is the main suite *alone*, not the combined total, and the 729 figure for `tests/` is
wrong. This is the same composition error as round 2 (where 763 was likewise `tests/` alone, reported as
combined). Both suites are green; the discrepancy is in the reporting, not the tests. Worth correcting
because a release gate that miscounts its own evidence is a gate that is not being read carefully.

---

# ADDENDUM 4 — LIVE SCHEMA CHECK: MIGRATIONS 046, 047, 048 ARE NOT APPLIED

Read-only probe against the Supabase project configured in `.env` (project ref `fvibyvfnjtztxetnemyd`).
No writes were performed. **Caveat:** this is the project the local `.env` points at; production on Render
supplies its own env vars, so confirm this is the same project before acting.

| Probe | Result |
|---|---|
| `schema_migrations` | **ABSENT** (PGRST205) — the Phase 0 migration runner has never run against this DB |
| `inbound_messages` (047) | **ABSENT** (PGRST205) |
| `scheduler_locks` (048) | **ABSENT** (PGRST205) |
| `appointments.refund_id` / `.refund_reason` / `.refunded_at` (046) | **ALL ABSENT** (42703) |
| `clinics`, `appointments`, `payment_events`, `processed_messages`, `failed_messages`, `lab_reports`, `branches`, `doctor_branches` | present |

The database is broadly current through the older migrations but has **none of the three migrations this
remediation program depends on**. Because `schema_migrations` does not exist, nothing in the system records
which migrations have been applied — the state above was established only by direct table and column probes.

## Consequence at cutover — this upgrades P1-9 to P0-6

The current code is uncommitted and undeployed, so nothing is broken *right now*: production runs the old
code, which does not reference these tables. The failure is triggered **by the deploy itself**. If the
working tree ships before the migrations are applied:

**046 missing → P0-1 is functionally still open.** `payment.py:592-596` writes `refund_reason` and
`refunded_at`. PostgREST rejects with `42703 column does not exist`. The Razorpay refund has already
succeeded. The DB write fails. `razorpay_webhook.py:80` now correctly returns 500, so Razorpay retries —
but every retry fails identically. Money is refunded at the provider with no local record, exactly the
original P0-1 outcome. The only improvement is that it is now loud instead of silent.

**047 missing → P1-6 silently reverts.** `message_queue.py:138-146` catches the missing-table error and
falls back to `processed_messages`, logging one `logger.warning`. Inbound returns to at-most-once. Every
test stays green because tests run against a migrated PostgreSQL fixture.

**048 missing → all 12 scheduled jobs stop permanently.** `distributed_lock.py:83-89` fails closed on DB
error, so every `async with distributed_job_lock(...)` returns `acquired=False` and returns immediately.
No appointment reminders. No `expire_stale_bookings` — slot holds never release and paid-but-unconfirmed
bookings are never recovered. No daily payment reconciliation. No `recover_pending_inbound_messages`.
Announced by warning logs only; the service stays healthy on `/health`.

**Severity: P0-6 — launch blocker.** A deploy of the current tree without these three migrations produces
a system that is quieter and more broken than the one it replaces.

## Required before any deploy

1. Apply `046_add_refund_columns.sql`, `047_durable_inbound_messages.sql`, `048_scheduler_locks.sql`.
2. Create `schema_migrations` and backfill it, so applied state is knowable rather than probed.
3. Add a startup assertion in `app/main.py`'s production boot check — alongside the existing
   placeholder-secret guards — that `inbound_messages`, `scheduler_locks`, and `appointments.refund_id`
   exist, and **fail the boot** if they do not. A silent fallback and a fail-closed lock are both correct
   behaviors in isolation; together with unapplied migrations they hide an outage.
4. Only then treat P1-6, P2-5 and P0-1 as closed in production. Until step 1 is done, they are closed in
   the repository and open in the running system.
