# KRIYA AI / MediAssist AI v2.0.0 — Independent Forensic Production-Readiness Audit

**Date:** 2026-08-28
**Auditor stance:** Independent. No conclusion inherited from prior reports.
**Repository state:** branch `main` @ `d019b48`
**Method:** Implementation evidence only. Documentation claims were treated as hypotheses and tested against code, migrations, Dockerfile, `render.yaml`, and a full local test execution.

---

## A. EXECUTIVE VERDICT

**OVERALL SCORE: 48 / 100**
**PRODUCTION STATUS: `BLOCKED`**

Kriya AI is **not** ready for real-world commercial launch to clinics, hospitals, or diagnostic centres.

This is not a verdict about code quality. Substantial parts of this system are genuinely well engineered — the durable inbound-message queue, the database-authoritative booking uniqueness, the payment-confirmation compare-and-set, the fail-closed webhook signature verification, and the fail-closed `scoped_query()` tenant guard are all real, correct, and better than typical for this class of product.

The verdict is driven by a small number of facts that are each independently disqualifying:

1. **Database migrations cannot execute in the production container at all.** Two independent failures. The system as deployed cannot reach the schema it depends on.
2. **Migration `043` aborts on any database that contains the duplicate rows it exists to clean up** — and `043` gates every migration up to `055`, including the *only surviving* anti-double-booking index.
3. **The multi-tenant boundary is running in deliberate shadow mode.** `tenant_scope_enforce` defaults to `False`, and in that mode an unscoped non-super-admin account is granted access to **every** tenant.
4. **The claimed test evidence is false.** The claim of "439 tests / 438 passed / 1 skipped / 0 failed" does not reproduce. Actual result: **877 passed, 1 failed, 2 skipped, 21 errors.** Critically, **all 21 errors are the real-PostgreSQL invariant tests** — the exact tests that would prove double-booking prevention, RLS isolation, scheduler mutual exclusion, and payment CAS. They have never run.
5. **Row-Level Security provides zero tenant isolation for the running application.** The app connects as `service_role`, which holds `BYPASSRLS`. Migration `049`'s tenant policies target a `kriya_app` role that appears nowhere in the application code.

The architecture is capable. The controls largely exist. But **the distributed behaviour of those controls is unproven, the deployment path is broken, and the evidence offered for correctness does not exist.**

---

## B. WHAT IS ACTUALLY PROVEN

These were verified by reading the implementation, not the docs.

| Control | Status | Evidence |
|---|---|---|
| Meta webhook HMAC-SHA256 signature verification, fails closed | **VERIFIED** | `app/utils/security.py:27`; `app/routers/webhook.py:57` verifies raw body before parse |
| Razorpay webhook signature verification, `hmac.compare_digest`, fails closed on missing sig/secret | **VERIFIED** | `app/services/payment.py:281-305` |
| Durable inbound persistence **before** HTTP 200 | **VERIFIED** | `app/routers/webhook.py:101-108`, migration `047` |
| Message-level idempotency via atomic UNIQUE insert (distributed-safe) | **VERIFIED** | `message_queue.acquire()`, `processed_messages` unique constraint |
| Booking uniqueness enforced by the database, not application logic | **VERIFIED** | `uq_appointment_active_slot` (migration `043`); `app/database.py:618-692` correctly discriminates booking-ref conflict from slot conflict |
| Payment confirmation is a correct compare-and-set | **VERIFIED** | `payment.py` Step 8: `.eq("id", booking_id).eq("status", "pending_payment")` |
| `payment_events` append-only enforced by DB trigger | **VERIFIED** | `prevent_payment_event_mutation()`, migration `008` |
| Fail-closed tenant guard on unscoped reads of tenant tables | **VERIFIED** | `scoped_query()` raises `TenantIsolationError`; `TENANT_OWNED_TABLES` frozenset |
| 18 of 20 scheduler jobs hold a PostgreSQL-backed distributed lock; lock acquisition fails **closed** | **VERIFIED** | `app/services/scheduler.py`; `distributed_lock.py` RPC `acquire_scheduler_lock` |
| Login rate limiter is Supabase-backed, not process-local | **VERIFIED** | `PersistentRateLimiter`, `security.py:166` |
| Prompt-injection sanitisation is wired into the live AI path | **VERIFIED** | `sanitize_user_input` / `strip_injection_markers` called from `ai_engine.py`; `clinical_firewall.screen_message` called from `conversation.py:319` |
| Authorization dependency present on admin/platform/clinics/FHIR routes | **VERIFIED** | 113 of 117 routes carry `Depends()`; the 4 without are health checks |
| Stack traces never returned from the webhook endpoint | **VERIFIED** | `webhook.py:122-125` |
| Phone masking in logs | **VERIFIED** | `mask_phone()` used consistently in lab-report and conversation paths |

**These are real strengths and should not be discarded during remediation.**

---

## C. WHAT IS PARTIALLY VERIFIED

| Area | What is true | What is not established |
|---|---|---|
| Multi-tenancy | The application-layer guard exists and is fail-closed for *unscoped* queries | The `"default"` sentinel path bypasses the guard entirely (KA-14); shadow mode disables the user-level check (KA-03) |
| Anti-double-booking | The partial unique index is the right design | Never executed against a real PostgreSQL server; keyed on `doctor_name` TEXT, not `doctor_id`, and omits `branch_id` (KA-11) |
| Payment idempotency | Webhook signature + confirmation CAS are correct | Ledger-level dedup index is **inert** — `provider_event_id` is never populated (KA-04) |
| Scheduler distribution | 18/20 jobs correctly locked | `prescription_reminders` is unlocked and additionally has an idempotency and timezone defect (KA-09) |
| Connector delivery | `external_report_id` dedup check exists | The connector lock is a TOCTOU read-then-write **and fails open** (KA-10) |

---

## D. WHAT IS NOT VERIFIED

Marked per the audit mandate. Absence of evidence, stated as such.

- **Capacity, throughput, latency under load** — `NOT VERIFIED`. No executed load test. No benchmark artefacts. Architecture appears capable; capacity is entirely unproven.
- **Behaviour under 2 / 10 / 100 concurrent booking attempts against real PostgreSQL** — `NOT VERIFIED`. The tests exist (`test_10_concurrent_booking_race_condition`, `test_01_real_postgres_slot_concurrency_50_threads`) and **error at setup**.
- **RLS tenant isolation** — `NOT VERIFIED` as a test, and **FALSE** as a control for the running application (KA-08).
- **Scheduler lock mutual exclusion across processes** — `NOT VERIFIED`. `test_16_scheduler_locks_mutual_exclusion_and_takeover` errors.
- **Migration forward-replay on a database with real data** — `NOT VERIFIED`, and analytically **BROKEN** (KA-02).
- **End-to-end Razorpay live-mode payment** — `NOT VERIFIED`. Blocked on absent test credentials.
- **25-state conversation FSM full transition coverage** — `NOT VERIFIED`. Not exhaustively traced in this audit; flagged as remaining work.
- **Rollback path for any migration** — `NOT VERIFIED`. No down-migrations exist in `migrations/`.
- **Soak / memory-leak behaviour over days** — `NOT VERIFIED`.
- **FHIR / ABDM conformance** — `NOT VERIFIED`. Routers exist; conformance untested.

---

## E. WHAT IS BROKEN OR FALSE

| Claim | Verdict |
|---|---|
| "439 automated tests / 438 passed / 1 skipped / 0 failed" | **FALSE.** Actual: 877 passed, **1 failed**, 2 skipped, **21 errors** |
| "Migrations are applied automatically on deploy" | **FALSE.** `preDeployCommand` cannot run — see KA-01 |
| "RLS enforces multi-tenancy" | **FALSE** for the running application — see KA-08 |
| "Payment webhooks are idempotent at the ledger" | **FALSE.** The dedup index is inert — see KA-04 |
| "Tenant isolation is enforced" | **CONDITIONALLY FALSE.** Shadow mode is the shipped default — see KA-03 |
| Migration `043` is replayable | **FALSE** — see KA-02 |

---

## F. FINDINGS REGISTER

Severity: **P0** = launch blocker · **P1** = must fix before general availability · **P2** = fix in first month · **P3/P4** = backlog.

---

### KA-01 · P0 · Database migrations cannot execute in the production container

- **Component:** Deployment / release engineering
- **Files:** `Dockerfile`, `render.yaml`, `scripts/migrate.py`, `requirements.txt`
- **Location:** `render.yaml` → `preDeployCommand: python scripts/migrate.py` (present on both production and staging services)

**Problem.** Two independent, each-sufficient failures:

1. The `Dockerfile` copies `app/`, `migrations/`, `admin/`, `connectors/`, and `tests/`. It **does not copy `scripts/`**. `python scripts/migrate.py` inside the image fails with `can't open file '.../scripts/migrate.py'`.
2. `scripts/migrate.py` imports `psycopg2`. **`psycopg2` is not in `requirements.txt`** (21 entries, verified). Even if the file were present, the import fails.

**Failure scenario.** A deploy carrying a new migration succeeds at the container level, the pre-deploy step fails or is silently skipped, and the application starts against an *older* schema. `app/main.py`'s pre-flight only checks migrations `046`/`047`/`048` — a missing `049`–`055` passes pre-flight and the app serves traffic against a schema it was not written for.

**Why existing tests miss it.** No test executes the Docker image's pre-deploy command. `test_01_migration_completeness_and_tracking` would catch schema drift — and it errors at setup (KA-07).

**Reproduction.**
```bash
docker build -t kriya:audit .
docker run --rm kriya:audit ls scripts/                    # -> No such file or directory
docker run --rm kriya:audit python -c "import psycopg2"    # -> ModuleNotFoundError
```

**Fix.** Add `COPY scripts/ ./scripts/` to the `Dockerfile`; add `psycopg2-binary>=2.9,<3` to `requirements.txt`. Add a startup assertion that the highest migration in `migrations/` equals the highest row in `schema_migrations`, and **fail closed** if not.

**Regression test required.** A CI job that builds the image and runs `python scripts/migrate.py --dry-run` inside it.

---

### KA-02 · P0 · Migration `043` is not replayable and gates the entire `043`→`055` chain

- **Component:** Database schema integrity
- **File:** `migrations/043_tenant_routing_upgrade.sql:47-49`

```sql
UPDATE appointments
SET status = 'cancelled_dedup'
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);
```

**Problem.** `'cancelled_dedup'` is not a member of `appointments_status_check` (migration `008`: `confirmed, cancelled, rescheduled, completed, no_show, pending_payment, expired, refunded, pending_review`). No later migration adds it. The `UPDATE` therefore raises a check-constraint violation.

**Failure scenario.** On an empty or duplicate-free database the `UPDATE` matches zero rows and `043` succeeds — which is why this has not been noticed. On **any database that actually contains duplicate active appointments** — precisely the database this migration exists to repair — `043` aborts. `scripts/migrate.py` uses one transaction per file and `break`s on first failure, so `044`–`055` never apply.

**Impact — this is the compounding part.** Migration `054` executes `DROP INDEX IF EXISTS idx_unique_active_slot`, removing the migration-`008`-era index, on the assumption that `043`'s `uq_appointment_active_slot` is present. If `043` failed, the database ends up with **no anti-double-booking index at all** while the application believes it is protected. Two patients can be confirmed into the same doctor/date/time slot.

**Why existing tests miss it.** Migrations are never replayed against a database seeded with duplicates. `test_02_idx_unique_active_slot_prevents_double_booking` errors at setup.

**Reproduction.** Seed a PostgreSQL instance at schema `042`, insert two `confirmed` appointments with identical `(clinic_id, doctor_name, appointment_date, appointment_time)`, run `scripts/migrate.py`. `043` aborts.

**Fix.** Add `'cancelled_dedup'` to the CHECK constraint in a new migration that runs *before* the repair, **or** change `043` to use the already-valid `'cancelled'`. Because `043` may already be recorded as applied in some environments, ship the fix as a new idempotent migration rather than editing `043` in place.

**Regression test required.** A real-PostgreSQL test that seeds duplicates, runs the full migration chain from `001`, and asserts both that it completes and that `uq_appointment_active_slot` exists afterwards.

---

### KA-03 · P0 · Tenant isolation ships in shadow mode; unscoped accounts reach every tenant

- **Component:** Multi-tenant authorization
- **Files:** `app/config.py:63`, `app/routers/admin.py:102-115`

```python
# app/config.py:63
tenant_scope_enforce: bool = False
```
```python
# app/routers/admin.py:104-112
if not self.clinic_id:
    if not settings.tenant_scope_enforce:
        logger.error("TENANT_SCOPE_WOULD_DENY ...")
        return True          # shadow mode, ONE release only (Rule 4)
    return False
```

**Problem.** With the shipped default, any authenticated account whose `clinic_id` is `NULL` and whose role is not `super_admin` — a `staff` or `clinic_admin` row created before migration `051`, or created by a code path that does not set `clinic_id` — is granted access to **every** tenant. `can_access_clinic()` returns `True` unconditionally.

**Exploit scenario.** A diagnostic-centre receptionist account with a `NULL` `clinic_id` calls `GET /admin/appointments?clinic_id=<competitor_clinic_uuid>` and receives another hospital's patient list: names, phone numbers, doctors, appointment times. Under the India DPDP Act this is a reportable personal-data breach.

**Mitigating factor (does not clear the finding).** Migration `051` adds `chk_admin_scope`, preventing *new* unscoped non-super-admin rows. The shadow-mode branch exists to avoid locking out pre-existing rows. The comment says "ONE release only" — that release has evidently passed.

**Why existing tests miss it.** `test_phase2_route_adversarial_matrix.py` seeds a *scoped* clinic-A user. It never seeds a `clinic_id=NULL` staff user, which is the vulnerable shape.

**Fix.**
1. Query production: `SELECT id, username, role FROM clinic_admins WHERE clinic_id IS NULL AND role <> 'super_admin';`
2. Assign each a `clinic_id`, or promote/delete.
3. Flip `tenant_scope_enforce` to `True` **as the new default in `config.py`**, not merely as an environment override.
4. Delete the shadow branch.

**Regression test required.** An adversarial test that constructs an `AdminUser` with `clinic_id=None, role="staff"` and asserts `403` on a cross-tenant read.

---

### KA-04 · P0 · Razorpay webhook has no ledger-level idempotency — the dedup index is inert

- **Component:** Payment integrity
- **Files:** `app/services/payment.py:1476-1499` (`_log_payment_event`), `:1527-1568` (`_log_payment_event_raw`), `migrations/054_payment_events_and_slot_index.sql`

**Problem.** Migration `054` creates:
```sql
CREATE UNIQUE INDEX uq_payment_event_provider_id
  ON payment_events(provider_event_id) WHERE provider_event_id IS NOT NULL;
```
and adds `payment_events.clinic_id`. Both `_log_payment_event` and `_log_payment_event_raw` accept `provider_event_id` and `clinic_id` parameters. **No call site inside `process_payment_webhook` passes either one.** Every ledger row is written with `provider_event_id = NULL` and `clinic_id = NULL`.

**Consequences, both real:**
1. Because the index is `WHERE provider_event_id IS NOT NULL`, every row is exempt. Razorpay retries — which it does on any non-2xx or timeout — write duplicate ledger entries. The financial audit trail double-counts.
2. `payment_events.clinic_id` remains NULL, so the ledger cannot be queried per tenant. Reconciliation, per-clinic revenue reporting, and tenant-scoped export are all impossible from the ledger.

**Why existing tests miss it.** Payment tests mock the Supabase client and assert that `_log_payment_event` was *called*. They do not assert on the arguments, and no test inserts two rows with the same `provider_event_id` against real PostgreSQL. `test_05_payment_events_immutability_trigger` errors at setup.

**Reproduction.** Replay an identical Razorpay `payment.captured` webhook twice with a valid signature. Observe two `payment_events` rows with identical `payment_id` and `NULL` `provider_event_id`.

**Fix.** Extract Razorpay's event id (`payload["id"]` / the `x-razorpay-event-id` header) at the top of `process_payment_webhook` and thread it plus `resolved_clinic_id` through every `_log_payment_event*` call. Catch the unique-violation and treat it as "already processed → return 200".

**Regression test required.** Real-PostgreSQL: two identical webhook deliveries → exactly one `payment_events` row, non-NULL `clinic_id`.

---

### KA-05 · P0 · Pathless Razorpay webhook route performs an unscoped cross-tenant booking lookup

- **Component:** Payment / multi-tenancy
- **Files:** `app/routers/razorpay_webhook.py`, `app/services/payment.py:445-449`

**Problem.** The router registers **both** `@router.post("/razorpay")` (with `clinic_id` defaulting to `"default"`) and `@router.post("/razorpay/{clinic_id}")`. On the pathless route:
```python
resolved_clinic_id = clinic.get("id") if (clinic and clinic.get("id") != "default") else None
```
yields `None`, and `_build_booking_query()` **omits the `clinic_id` predicate entirely** when `use_clinic_scope` is `None`. The booking is then resolved by `booking_id` across all tenants.

**Exploit scenario.** With migration `055` having dropped global booking-ref uniqueness in favour of per-tenant uniqueness (`052`), booking references are only unique *within* a clinic. A crafted or mis-routed webhook can therefore resolve to a different tenant's appointment and confirm it. An in-code comment (`KRIYA-005`) asserts this path is scoped; **it is not.**

**Why existing tests miss it.** Payment webhook tests exercise the `/{clinic_id}` route.

**Fix.** Delete the pathless route, or make it hard-fail with `400` when `clinic_id` cannot be resolved. Make `_build_booking_query()` raise rather than silently drop the predicate when scope is `None`.

**Regression test required.** Assert `POST /webhooks/razorpay` (no path segment) returns `4xx`, and that `_build_booking_query(None)` raises.

---

### KA-06 · P0 · Payment webhook silently captures money on the cancel-vs-payment race and can clobber terminal states

- **Component:** Payment ↔ booking invariants
- **File:** `app/services/payment.py`, Step 7

**Problem.** When the appointment's status is not `pending_payment`, the handler writes `status='pending_review'` and `payment_id=<id>` with **no status precondition on the update** and **no admin alert**.

**Failure scenarios.**
1. *Cancel racing payment.* Patient cancels; a beat later Razorpay confirms the capture. The appointment moves `cancelled → pending_review`. Money is held. Nothing notifies staff. The patient believes they cancelled; the clinic never learns they were paid. Silent financial loss and a consumer-protection exposure.
2. *Terminal-state clobber.* An out-of-order or replayed webhook against a `completed` or `refunded` appointment overwrites that terminal status with `pending_review`, corrupting the record after the fact.

**Why existing tests miss it.** The unexpected-status branch is tested for the *return value*, not for the state transition's legality or for alerting.

**Fix.** Add a status precondition (`.in_("status", ["pending_payment", "expired"])`) to the update. Add an admin alert + `payment_events` entry on every entry into this branch. Refuse to transition out of `completed` / `refunded` / `cancelled` — route to a manual reconciliation queue instead.

**Regression test required.** Concurrency test: cancel and payment-webhook issued simultaneously → assert exactly one of {refund initiated, booking confirmed} and that an alert row exists.

---

### KA-07 · P0 · The test suite is red, and the claimed evidence does not exist

- **Component:** Test quality / evidence integrity
- **Evidence:** full local execution, 141.46s

```
1 failed, 877 passed, 2 skipped, 2 warnings, 21 errors
```

**Against the claim of "439 tests / 438 passed / 1 skipped / 0 failed" this is unreproducible in every dimension.**

**The 21 errors are the finding.** Every one is:
```
ModuleNotFoundError: No module named 'pgserver'
```
at fixture setup, and they comprise the entire real-PostgreSQL invariant suite:

| Test | What it would have proven |
|---|---|
| `test_02_idx_unique_active_slot_prevents_double_booking` | Double-booking prevention |
| `test_10_concurrent_booking_race_condition` | The actual race |
| `test_01_real_postgres_slot_concurrency_50_threads` | 50-way concurrency |
| `test_11_compare_and_set_payment_confirmation` | Payment CAS |
| `test_12_processed_messages_idempotency` | Message idempotency |
| `test_16_scheduler_locks_mutual_exclusion_and_takeover` | Distributed scheduler safety |
| `test_17_force_row_level_security_tenant_isolation` | RLS isolation |
| `test_05_payment_events_immutability_trigger` | Ledger immutability |
| `test_01_migration_completeness_and_tracking` | Migration chain integrity |
| (+12 more) | |

Neither `pgserver` nor `psycopg2` is in `requirements.txt`. **From a clean checkout following the documented setup, none of these tests can run.** Every concurrency and isolation guarantee in this system rests on tests that have never executed here.

**The 1 failure** is `test_adversarial_cross_tenant_rejection_per_route[GET-/admin]`: `assert 200 != 200`. On inspection this is a **test-harness false positive** — `/admin` is a static `RedirectResponse` to `/admin-panel`, which serves an unauthenticated HTML shell (data is fetched by authenticated XHR). It is not a data leak. But it means **CI is red**, and a red suite that the team has learned to ignore is how the other findings in this report survived three prior audit rounds.

**Mock density:** 96 of 126 test files (76%) import `Mock` or `patch`. 528 test functions defined, 877 collected after parametrisation. Per the audit mandate — *a test that passes while mocking the race condition is not evidence the race condition is solved* — and here the un-mocked tests are exactly the ones that do not run.

**Fix.** Add `pgserver` and `psycopg2-binary` to a `requirements-dev.txt` (and `psycopg2-binary` to `requirements.txt` for KA-01). Make CI fail on any error or failure. Exclude static/redirect routes from the adversarial matrix by explicit allowlist, not by loosening the assertion.

---

### KA-08 · P0 · Row-Level Security provides no isolation for the running application

- **Component:** Database security posture
- **Files:** `migrations/049_force_row_level_security.sql`, `app/database.py:22-23`

**Problem.** The application connects with the Supabase **`service_role`** key:
```python
_sb_key = settings.supabase_service_role_key or "placeholder-key"
supabase: Client = create_client(_sb_url, _sb_key)
```
`service_role` holds `BYPASSRLS`, and every policy across `002`–`054` is of the form `FOR ALL TO service_role USING (true)`.

Migration `049` does add genuine tenant policies — but scoped `TO kriya_app, authenticated, anon`, gated on `current_setting('app.clinic_id')`. **`kriya_app` and `app.clinic_id` appear nowhere in `app/`** (verified by repository-wide grep). The role is created, granted, and never used.

**Consequence.** `049`'s own header comment — *"Even if an application query omits `clinic_id`, PostgreSQL rejects cross-tenant data access"* — is **false for the running system**. The sole tenant boundary is the Python-layer `scoped_query()` guard. That guard is good, but it is one layer, not defence in depth, and KA-14 documents a path around it.

**To the codebase's credit,** `app/database.py:32` and `:56` state this honestly in comments. The documentation and prior audit reports do not.

**Fix (choose one, do not ship neither):**
- *(Preferred, larger)* Migrate the application to connect as `kriya_app` and set `app.clinic_id` per request/transaction, making `049` live.
- *(Minimum)* Correct all documentation to state plainly that RLS is a defence-in-depth backstop for non-application roles only, and that application tenant isolation is enforced solely in Python — then treat `scoped_query()` coverage as a P0-grade invariant with 100% route coverage testing.

---

### KA-09 · P1 · `send_due_reminders` — four independent defects in one function

- **Component:** Scheduler / patient safety
- **File:** `app/services/prescriptions.py:103-157`; registered in `app/services/scheduler.py` as `id="prescription_reminders"`, interval 5 min

This is the **only 1 of 20** scheduler jobs not wrapped in `async with distributed_job_lock(...)`.

```python
now = datetime.now(timezone.utc)
current_time = now.strftime("%H:%M")
today_str = str(date.today())
result = (supabase.table("prescriptions").select("*")
    .eq("is_active", True).lte("start_date", today_str).gte("end_date", today_str).execute())
for rx in result.data or []:
    for rt in rx.get("reminder_times", []):
        if self._time_within_window(current_time, rt, 5):
            ... await whatsapp_service.send_text(...)
            break
```

1. **No distributed lock.** Production runs `numInstances: 2` × `--workers 2` = **4 processes** (`render.yaml` + `Dockerfile` CMD). All four fire this job.
2. **No sent-tracking.** A ±5-minute window against a 5-minute tick matches on 2–3 consecutive ticks.
3. **Combined effect: up to 12 duplicate medication reminders per dose.** For a patient on a psychiatric or cardiac regimen, repeated "take your medication now" messages are a clinical-safety issue, not a UX annoyance.
4. **Timezone defect.** `current_time` is **UTC**; `reminder_times` are entered by clinic staff in **IST**. These are 5h30m apart. `date.today()` on a container with `TZ=Asia/Kolkata` is IST, so the query window and the comparison clock are in *different* timezones. A reminder set for 09:00 IST fires at 09:00 UTC = 14:30 IST.
5. **Unbounded cross-tenant scan.** `select("*")` with no `clinic_id` filter and no pagination across all prescriptions of all clinics, every 5 minutes.

**Why existing tests miss it.** `_time_within_window` is unit-tested in isolation with injected times. No test runs the job concurrently, and no test asserts the timezone of `current_time`.

**Fix.** Wrap in `distributed_job_lock("prescription_reminders")`. Add a `prescription_reminder_sends(prescription_id, reminder_time, sent_date)` table with a UNIQUE constraint and insert-before-send. Convert `now` to `ZoneInfo("Asia/Kolkata")`. Paginate and scope the query per clinic.

**Regression test required.** Four concurrent invocations → exactly one send per prescription per reminder time; explicit assertion that the comparison clock is IST.

---

### KA-10 · P1 · Connector lock is TOCTOU **and** fails open — duplicate lab-report delivery

- **Component:** Lab report pipeline / connectors
- **File:** `connectors/runner.py:102-142`

```python
res = supabase.table("integration_connectors").select("id, locked_at").eq("id", connector_id).execute()
...                                    # <-- window
supabase.table("integration_connectors").update({"locked_at": now_str, "locked_by": worker_id}).eq("id", connector_id).execute()
_locks_held_by_this_process.add(connector_id)
return True, 0
except Exception as e:
    logger.warning(f"Could not acquire lock for connector {connector_id} (proceeding): {e}")
    return True, 0
```

Two defects:
1. **Read-then-write with no CAS predicate.** Two workers can both read a stale `locked_at`, both write, both believe they hold the lock.
2. **Fails OPEN.** Any exception — a transient Supabase timeout, exactly when contention is highest — returns `True` and proceeds.

This is the direct inverse of `app/services/distributed_lock.py`, which uses an atomic RPC and **fails closed**. The correct primitive already exists in this repository and is imported at `connectors/runner.py:49` — but is not used for the connector lock.

**Impact.** Two Playwright sessions poll the same LIS concurrently; a patient receives their lab report twice. Under partial failure the `external_report_id` dedup check at `:287` can race with itself, and the connector's report-claim logic is the last barrier before a report is sent to a phone number.

**Fix.** Replace with `distributed_job_lock(f"connector_{connector_id}")`, or at minimum make the UPDATE a CAS and change the exception handler to `return False, 0`.

**Regression test required.** Two concurrent `run_connector()` invocations against real PostgreSQL → exactly one executes.

---

### KA-11 · P1 · Slot uniqueness is keyed on a free-text doctor name and ignores branch

- **Component:** Booking business rules
- **File:** `migrations/043_tenant_routing_upgrade.sql`

```sql
CREATE UNIQUE INDEX uq_appointment_active_slot
  ON appointments(clinic_id, doctor_name, appointment_date, appointment_time)
  WHERE status IN ('confirmed','pending_payment');
```

**Two symmetric defects:**
- **False negative (double-booking).** `doctor_name` is `TEXT`. `"Dr. Rao"`, `"Dr Rao"`, `"dr. rao"`, and `"Dr. Rao "` are four distinct index keys. A doctor renamed mid-day, or a slot list rendered with differing whitespace, silently permits two patients into one slot. The `doctors` table has an `id`; it is not used here.
- **False positive (lost bookings).** `branch_id` is absent. A multi-branch hospital with two physicians of the same name — common in India — cannot book them at the same time in different branches. The second patient receives a spurious "slot taken".

**Fix.** New migration: add `doctor_id UUID` to `appointments`, backfill from `doctors` by name, then rebuild the index on `(clinic_id, branch_id, doctor_id, appointment_date, appointment_time)`. Keep `doctor_name` as a denormalised display field only. Sequence this **after** KA-02 is resolved.

**Regression test required.** Real-PostgreSQL: same doctor with differing whitespace/case → second insert rejected. Same-named doctors at different branches → both succeed.

---

### KA-12 · P1 · Per-phone conversation serialization is process-local

- **Component:** Concurrency
- **Files:** `app/services/message_queue.py:38-40`, `:574`, `:602`, `:619`; `app/services/conversation.py:150-238`

`_phone_locks: dict[str, asyncio.Lock]` is module-level Python state. With 4 production processes there are 4 independent lock tables. Two messages from the same patient landing on different workers execute `handle_message()` concurrently.

**Message-level idempotency is unaffected** — `processed_messages` UNIQUE insert is genuinely distributed and correct. What is unprotected is **conversation-state serialization**: a patient who sends "2" and "confirm" in rapid succession can have both processed against the same pre-state, producing two bookings or a corrupted FSM state.

The `book_appointment` unique index catches the double-booking case. It does **not** catch FSM corruption, duplicate outbound messages, or duplicate payment-link generation.

**Why existing tests miss it.** Tests run in one process; `asyncio.Lock` works perfectly there. This is exactly the distributed-execution failure mode: *a local lock that passes every single-process test.*

**Fix.** Move to a PostgreSQL advisory lock keyed on `hashtext(phone)`, or reuse `distributed_lock.py` with `job_name=f"phone_{phone}"`. Keep the local lock as a cheap first-level filter.

**Regression test required.** Two processes, same phone, simultaneous messages → serialized execution proven by a state assertion, not a mock.

---

### KA-13 · P1 · Amount-mismatch handling releases the slot while the patient has paid

- **Component:** Payment ↔ booking invariants
- **File:** `app/services/payment.py`, Step 6

On an amount mismatch the appointment moves to `pending_review`. Because `uq_appointment_active_slot` is partial on `status IN ('confirmed','pending_payment')`, this **drops the slot hold**. Another patient can now confirm that slot while the first patient's money is captured.

**Fix.** Introduce a `payment_held` status inside the index predicate, or add `pending_review` to the predicate. Alert staff synchronously.

---

### KA-14 · P1 · The `"default"` sentinel skips the tenant predicate on admin reads

- **Component:** Multi-tenant authorization
- **File:** `app/routers/admin.py` (pervasive; ~7 read routes)

Pattern:
```python
clinic_id: str = "default"                                   # query param default
effective_clinic_id = enforce_clinic_access(user, clinic_id)
if effective_clinic_id != "default":
    query = query.eq("clinic_id", effective_clinic_id)       # <-- skipped otherwise
```

`enforce_clinic_access` returns `"default"` unchanged when `user.role == "super_admin"` and `user.clinic_id is None` — the shape produced by the **environment-variable super-admin fallback** (`verify_credentials`, `admin.py:177-254`, which sets `role="super_admin", clinic_id=None`). The `.eq()` is skipped and the query returns rows from **all tenants**.

For the platform owner this may be intentional. It is nonetheless unbounded and unpaginated, and `resolve_clinic_id_for_write()`'s own docstring warns about precisely this hazard for writes while leaving reads unguarded. Sentinel-string-as-authorization-state is fragile: one route that forgets the `if` leaks silently.

**Fix.** Replace `"default"` with `Optional[str] = None` and make "all tenants" an **explicit, separately-authorized** `?all_clinics=true` parameter that requires `super_admin` and is logged to `admin_audit_logs`.

---

### KA-15 · P1 · Admin authentication weaknesses

- **File:** `app/routers/admin.py:177-254`

- **Global username lookup.** `clinic_admins` is queried by `username` with **no tenant scope and no `ORDER BY`**. Two clinics that each create a user named `admin` produce a non-deterministic login — a user may authenticate into the wrong tenant depending on row order. No UNIQUE constraint on `username` was found.
- **Environment-variable master key.** `ADMIN_USERNAME`/`ADMIN_PASSWORD` grants `role="super_admin", clinic_id=None` — a static, non-rotatable, non-revocable credential to every tenant's data. It cannot be disabled per-environment and does not appear in `clinic_admins` for audit purposes.
- **HTTP Basic with no session layer.** No logout, no session expiry, no MFA, no credential rotation. Browser-cached credentials are replayed on every request indefinitely.
- **Rate limiting keyed on IP only.** Behind Render's proxy, `X-Forwarded-For` handling determines whether this is per-client or per-proxy. `PersistentRateLimiter` is correctly DB-backed (verified), but the key choice permits credential-stuffing across rotating source IPs against one username.

**Fix.** `UNIQUE(clinic_id, username)` + scope the lookup. Gate the env fallback behind an explicit `ALLOW_ENV_SUPERADMIN` flag, default off in production. Rate-limit on `(username, ip)`. Plan a session/JWT migration with MFA for super-admin.

---

### KA-16 · P2 · Lab report retrieval uses substring matching on phone number

- **File:** `app/services/lab_reports.py:492-510`

```python
clean_phone = phone.lstrip("+")
query = supabase.table("lab_reports").select("*").ilike("patient_phone", f"%{clean_phone}%")
```

Identity matching for **medical report retrieval** by SQL substring. The only caller (`conversation.py:3416`) correctly passes `clinic["id"]`, so cross-tenant leakage is closed *at that call site* — but the function's own default is `clinic_id="default"`, which skips the tenant filter, so any future caller inherits a cross-tenant read.

Within a tenant, `%1234567890%` matches a stored `911234567890` (correct — same person), but the pattern is unanchored and depends entirely on stored-format consistency. Given `resend_report` permits `patient_phone` to be overwritten with a `new_phone`, format drift is reachable.

**The invariant "a patient's report must never be delivered to another patient" is therefore enforced by data-format convention, not by a constraint.**

**Fix.** Normalise `patient_phone` to E.164 on write (add a CHECK constraint), and use `.eq()` here. Make `clinic_id` a required parameter with no default.

---

### KA-17 · P2 · Tenant-resolution failure is silently swallowed in the webhook

- **File:** `app/routers/webhook.py:93-98`

```python
try:
    clinic = await resolve_tenant(display_phone, phone_number_id=phone_number_id)
    if clinic: clinic_id = clinic.get("id")
except Exception:
    pass
```

The message is then ingested with `clinic_id=None`. No log, no metric, no alert. `process_message` later re-resolves and raises properly — but the durable queue row is already written unattributed, so the DLQ cannot be triaged per tenant and a tenant-resolution outage is invisible on the dashboard.

**Fix.** Log at `error` with the correlation id and increment a `kriya_tenant_resolution_failures_total` counter.

---

### KA-18 · P2 · `record_delivery_status` performs an unscoped write to `lab_reports`

- **File:** `app/routers/webhook.py:131-166`

The Meta delivery receipt updates `lab_reports` by `whatsapp_message_id` with **no `clinic_id` predicate** (annotated `# unscoped:`). Correctness depends on `wamid` global uniqueness, which is a Meta property this system does not control. No UNIQUE constraint on `lab_reports.whatsapp_message_id` was found; a collision or replay updates an arbitrary matching row across tenants. The monotonic `_DELIVERY_RANK` guard is a good design and should be kept.

**Fix.** Add `UNIQUE(whatsapp_message_id)` to `lab_reports` and assert single-row affected.

---

### KA-19 · P2 · Tenant cache invalidation is process-local

- **File:** `app/services/tenant.py:13-40`, `:274-281`

`_tenant_cache` is in-process with a 30s TTL. `invalidate_tenant_cache()` clears only the calling process's copy. After an admin deactivates a clinic, the other 3 workers continue serving it for up to 30 seconds. Bounded and low-severity, but "clinic suspended" is not immediate — relevant for non-payment suspension and for DPDP deletion requests.

**Fix.** Document the 30s propagation window explicitly, or move to a shared cache. Do not lower the TTL to zero — that shifts load to the database on every message.

---

### KA-20 · P2 · DLQ rows store `clinic_id` inside a JSON string rather than a column

- **File:** `app/services/conversation.py:150-238`

On phone-lock timeout the failure is written to `failed_messages` with the clinic embedded in a serialized JSON payload. `failed_messages` is in `TENANT_OWNED_TABLES`, so `scoped_query()` will refuse to read it per tenant — the DLQ is effectively un-triageable by clinic.

**Fix.** Promote `clinic_id` to a column and backfill.

---

### KA-21 · P2 · `cancel_appointment` has no status precondition and no payment coupling

- **File:** `app/database.py:711-724`

Sets `status='cancelled'` unconditionally. A `completed` appointment can be cancelled. A `confirmed`-and-paid appointment is cancelled with no refund initiated and no linkage to `payment_events`. Combined with KA-06 this is the other half of the cancel/payment race.

**Fix.** Add an `.in_("status", [...])` precondition; require an explicit refund decision when `payment_id IS NOT NULL`.

---

### KA-22 · P2 · Broad silent-failure surface

Repository-wide counts across `app/`:
- **379** `except Exception` handlers
- **30** handlers whose body is a bare `pass`
- **46** handlers returning `None`/`False`/`[]`/`{}` on failure

Many are legitimate (fire-and-forget logging, metrics). But `return None` on failure is indistinguishable from `return None` on "not found" at every call site, and a `pass` in a delivery or persistence path converts an outage into silence. This is the mechanism by which several findings above stayed hidden.

**Fix.** Triage the 30 bare-`pass` sites; each must either log with context or re-raise. Add `flake8-bugbear` B110 (`try-except-pass`) to CI.

---

### KA-23 · P3 · No rollback path for any migration

`migrations/` contains 55 forward migrations and **zero** down-migrations. `scripts/migrate.py` has no `--rollback`. A bad migration on a production healthcare database has no scripted recovery other than restore-from-backup, and no documented RTO.

---

## G. DOMAIN SCORECARD

Scores are prioritisation aids, not measurements.

| # | Domain | Score | Basis |
|---|---|---|---|
| 1 | Deployment & release engineering | **20** | Migrations cannot execute in the image (KA-01); no rollback (KA-23) |
| 2 | Database schema & migration integrity | **30** | `043` blocks the chain and can remove the double-booking index (KA-02) |
| 3 | Multi-tenant isolation | **45** | Good app-layer guard; shadow-mode default (KA-03), sentinel bypass (KA-14), RLS inert (KA-08) |
| 4 | Payment correctness & security | **45** | Signature + CAS correct; dedup inert (KA-04), unscoped route (KA-05), silent loss (KA-06) |
| 5 | Concurrency & distributed correctness | **40** | 18/20 jobs locked; phone lock local (KA-12); connector lock fails open (KA-10); **untested against real PG** |
| 6 | WhatsApp / Meta integration | **70** | Durable queue + distributed idempotency are genuinely strong |
| 7 | Conversation FSM | **60** | `NOT VERIFIED` in depth; no exhaustive transition table exists |
| 8 | Booking business rules | **55** | DB-authoritative, but keyed on free text and branch-blind (KA-11) |
| 9 | Admin panel authorization | **45** | RBAC model is well designed; auth layer is weak (KA-15) |
| 10 | API security | **65** | 113/117 routes authorized; headers, CORS, injection defences present |
| 11 | AI / LLM safety | **70** | Three-layer injection defence verified live; keyword fallback present |
| 12 | Healthcare privacy — *technical controls* | **55** | Masking, sanitiser, retention service exist; deletion not end-to-end verified |
| 13 | Lab report pipeline | **50** | Dedup exists; lock fails open (KA-10); identity by substring (KA-16) |
| 14 | Scheduler reliability | **60** | 18/20 correctly locked, fail-closed; the 20th is a patient-safety job (KA-09) |
| 15 | Observability | **60** | Prometheus, correlation IDs, PII sanitiser present; silent-failure surface undermines them (KA-22) |
| 16 | Test quality & evidence integrity | **25** | Suite red; all concurrency proofs error; claimed counts false (KA-07) |
| 17 | Capacity & performance | **NOT VERIFIED** | No executed load test. Excluded from the mean rather than guessed |
| 18 | Incident recovery & rollback | **30** | No down-migrations, no documented RTO/RPO, no runbook found |

**OVERALL: 48 / 100 — `BLOCKED`**

The score is capped regardless of the mean because: authorization is bypassable on sensitive data (KA-03), payment webhooks are not idempotent at the ledger (KA-04), migrations cannot be run safely (KA-01, KA-02), and there is no rollback path (KA-23).

---

## H. REMEDIATION PLAN

**Do not reorder Phase 0.** KA-02 must precede KA-11 because both touch the slot index.

### PHASE 0 — LAUNCH BLOCKERS (all green before any clinic onboards)

| ID | Finding | Objective | Files | Acceptance criteria |
|---|---|---|---|---|
| **T0.1** | KA-01 | Make migrations executable in the image | `Dockerfile` (+`COPY scripts/`), `requirements.txt` (+`psycopg2-binary`) | CI builds the image and runs `python scripts/migrate.py --dry-run` inside it, exit 0 |
| **T0.2** | KA-02 | Unblock the migration chain | new `migrations/056_fix_dedup_status.sql` | Chain `001`→`056` applies cleanly on a PG seeded with duplicate confirmed appointments; `uq_appointment_active_slot` exists afterwards |
| **T0.3** | KA-01 | Fail-closed schema pre-flight | `app/main.py` lifespan | App refuses to start when `max(migrations/)` ≠ `max(schema_migrations)` |
| **T0.4** | KA-07 | Restore executable evidence | new `requirements-dev.txt` (+`pgserver`, `psycopg2-binary`), CI workflow | All 21 real-PG tests execute; suite exits 0; CI fails on any error |
| **T0.5** | KA-03 | Close the tenant shadow mode | `app/config.py:63`, `app/routers/admin.py:104-112` | Production query returns zero `clinic_id IS NULL AND role<>'super_admin'` rows; default `True`; shadow branch deleted; adversarial test with an unscoped staff user asserts `403` |
| **T0.6** | KA-04 | Activate payment ledger idempotency | `app/services/payment.py` (all `_log_payment_event*` call sites) | Duplicate webhook → exactly one `payment_events` row with non-NULL `provider_event_id` and `clinic_id`, proven against real PG |
| **T0.7** | KA-05 | Remove the unscoped payment route | `app/routers/razorpay_webhook.py`, `payment.py:445-449` | `POST /webhooks/razorpay` returns `4xx`; `_build_booking_query(None)` raises |
| **T0.8** | KA-06 | Stop silent payment capture | `app/services/payment.py` Step 7 | Cancel-vs-payment concurrency test yields exactly one outcome + an alert row; terminal states cannot be clobbered |
| **T0.9** | KA-08 | Resolve the RLS claim | `docs/**` at minimum; ideally `app/database.py` + `049` | Either the app connects as `kriya_app` with `app.clinic_id` set and `test_17` passes, **or** every document stating "RLS enforces multi-tenancy" is corrected |

**Rollout:** staging first, full migration replay from `001` against a production-shaped snapshot, then production with `autoDeploy: false` retained and a manual gate.
**Rollback:** T0.5 is the only behaviour-changing flag — keep it env-overridable for 48h before hard-coding. T0.2 is additive and idempotent. T0.6–T0.8 must ship together, with a payment-reconciliation report run before and after.

### PHASE 1 — PRE-GENERAL-AVAILABILITY (P1)

| ID | Finding | Objective | Acceptance |
|---|---|---|---|
| T1.1 | KA-09 | Lock, de-duplicate, and fix the timezone of `send_due_reminders` | 4 concurrent invocations → 1 send per dose; comparison clock asserted IST |
| T1.2 | KA-10 | Replace the connector lock with `distributed_job_lock`, fail closed | 2 concurrent `run_connector()` → exactly 1 executes |
| T1.3 | KA-11 | Re-key the slot index on `(clinic_id, branch_id, doctor_id, date, time)` | Whitespace/case variants rejected; same-named doctors at different branches both succeed |
| T1.4 | KA-12 | Distributed per-phone lock | 2-process same-phone test proves serialization without mocks |
| T1.5 | KA-13 | Keep the slot held through `pending_review` | Amount-mismatch test: slot not re-bookable |
| T1.6 | KA-14 | Replace the `"default"` sentinel with explicit `all_clinics` authorization | Adversarial matrix extended to super-admin-with-null-clinic; all reads scoped |
| T1.7 | KA-15 | `UNIQUE(clinic_id, username)`; gate the env super-admin; rate-limit on `(username, ip)` | Duplicate-username login deterministic; env fallback off by default in production |

### PHASE 2 — FIRST MONTH (P2)

T2.1 KA-16 E.164 normalisation + CHECK + `.eq()` lookup · T2.2 KA-17 log/metric tenant-resolution failures · T2.3 KA-18 `UNIQUE(whatsapp_message_id)` · T2.4 KA-19 document the 30s cache window · T2.5 KA-20 promote `failed_messages.clinic_id` to a column · T2.6 KA-21 status precondition + refund coupling on cancel · T2.7 KA-22 triage the 30 bare-`pass` sites, add B110 to CI.

### PHASE 3 — VERIFICATION (cannot be automated; must be observed)

| Activity | Why it cannot be inferred from code |
|---|---|
| Load test: 100 concurrent bookings on one slot, real PG | The only way to falsify KA-11 and prove the index |
| Load test: sustained inbound webhook rate to saturation | No capacity figure exists today |
| 72-hour soak with 4 processes | Memory growth in `_tenant_cache`, `_phone_locks`, `_locks_held_by_this_process` |
| Failure injection: kill a worker mid-`process_message` | Proves the `claim_message` reaper actually recovers |
| Failure injection: Supabase unavailable for 60s | Proves fail-closed paths are genuinely closed |
| Razorpay **live-mode** end-to-end + refund + reconciliation | Still blocked on credentials; test-mode passing is not evidence |
| DPDP "delete my data" end-to-end across all 18 tenant tables | `data_retention.py` exists; completeness unverified |
| Restore-from-backup drill with measured RTO/RPO | No rollback path exists (KA-23) |

---

## I. HEALTHCARE & REGULATORY POSITION

**Implemented technical controls (verified):** phone masking in logs, PII sanitiser before external API calls, append-only payment ledger with a DB-enforced immutability trigger, admin action audit logging to `admin_audit_logs`, encrypted connector credentials (`connector_crypto`), consent service, data-retention service, RBAC with 15 granular permissions.

**Not established:** that these controls constitute compliance with the India DPDP Act, NABH, or any other regime. **No legal or regulatory compliance claim is made or supported by this audit.** Specifically:

- The KA-03 shadow-mode path is a **reportable personal-data-breach vector** under DPDP if an unscoped account exists in production. This must be checked against live data, not assumed.
- Erasure completeness (DPDP §12) is `NOT VERIFIED` — `data_retention.py` was not traced across all 18 tenant-owned tables.
- Retention periods are implemented as configuration, not as a DB-enforced policy.

---

## J. FINAL LAUNCH GATE

| Gate | Status |
|---|---|
| Migrations execute in the production image | ❌ **BLOCKED** (KA-01) |
| Migration chain replays on a production-shaped database | ❌ **BLOCKED** (KA-02) |
| Anti-double-booking index provably present post-migration | ❌ **NOT VERIFIED** (KA-02, KA-07) |
| Double-booking prevented under real concurrency | ❌ **NOT VERIFIED** — proving test never ran |
| Tenant isolation enforced for every account shape | ❌ **BLOCKED** (KA-03, KA-14) |
| RLS isolation | ❌ **FALSE for the running application** (KA-08) |
| Payment webhook idempotent end-to-end | ❌ **BLOCKED** (KA-04) |
| Payment never silently captured | ❌ **BLOCKED** (KA-06) |
| Cross-tenant payment resolution impossible | ❌ **BLOCKED** (KA-05) |
| Test suite green | ❌ **RED** — 1 failed, 21 errors (KA-07) |
| Scheduler jobs all distributed-safe | ❌ 19/20 (KA-09) |
| Lab report cannot reach the wrong patient | ⚠️ **PARTIALLY VERIFIED** — enforced by convention, not constraint (KA-16) |
| Message idempotency distributed | ✅ **VERIFIED** |
| Webhook signature verification fails closed | ✅ **VERIFIED** |
| Payment confirmation is an atomic CAS | ✅ **VERIFIED** |
| Prompt-injection defence live in the AI path | ✅ **VERIFIED** |
| Capacity known | ❌ **NOT VERIFIED** — no load test executed |
| Rollback path exists | ❌ **ABSENT** (KA-23) |

**GATE RESULT: DO NOT LAUNCH.**

Phase 0 (9 tasks) clears the blockers. Phase 1 (7 tasks) is required before general availability. Phase 3 verification cannot be skipped or inferred — **the single most important corrective action is T0.4**, because until the 21 real-PostgreSQL tests actually execute, every concurrency and isolation guarantee in this system remains an assertion rather than a fact.

---

## K. CLOSING NOTE ON EVIDENCE

This audit found serious defects. That is the intended outcome, not a failure of the engineering team. Several of the strongest controls here — the durable queue, the DB-authoritative booking, the fail-closed distributed lock, the honest `# unscoped:` annotations in `database.py` — are the work of engineers who were thinking about exactly the right failure modes.

The gap is not competence. It is that **the evidence layer collapsed**: a red test suite with 21 silently-erroring tests let three prior audit rounds report green while the guarantees underneath were never exercised. Fix the evidence layer first, and the rest of this list becomes tractable.
