# Kriya AI / MediAssist AI — Forensic Remediation Execution Plan

**Document type:** Executable remediation plan (agent-consumable)
**Created:** 2026-08-27
**Baseline commit:** `128f777` on branch `staging`, working tree clean
**Baseline test state:** `954 passed, 2 skipped` in ~131s
**Audit score at baseline:** 58 / 100 — `BLOCKED`
**Target score:** >= 90 / 100 — `READY FOR PRODUCTION`

> This plan supersedes nothing. It is a **new** plan from the 2026-08-27 audit and
> a different finding set (KRIYA-001..016) than
> `2026-08-25-production-score-and-95-plan.md` (baseline 75/100) or
> `ANTIGRAVITY-PROMPT-95-plan-completion.md`. Do not merge the two task lists.

---

## HOW TO USE THIS DOCUMENT

Every task is self-contained and carries:

- a **task ID** (`T0.1a`) — use it in the commit message
- an explicit **touch-list** — the *only* files that task may modify
- **exact current code** and **exact replacement code**
- a **binary acceptance gate** — a test that fails before and passes after
- **rollout** and **rollback** instructions

### Execution rules

1. Execute in the order given. Phase 0 tasks may run in parallel with each other;
   every later phase depends on Phase 0 being complete.
2. **One task = one commit.** Never bundle. Every task must be independently
   revertable with `git revert`.
3. Run the baseline check before and after every task. The counts must match
   except where the task explicitly names a test it changes.
4. If the acceptance gate for a task cannot be made to pass, **stop and report**.
   Do not work around it, do not weaken the gate, do not move on.
5. If a task requires modifying a file not in its touch-list, **stop and report**.
   That means the task was mis-scoped.
6. Do not branch off `main`. Work on `staging` or a feature branch off it.
7. Do not commit or push unless the user asks.

### Baseline check (before and after every task)

```bash
cd /path/to/hospital-bot
python -m pytest -q -p no:cacheprovider
# Expect: 954 passed, 2 skipped   (plus tests added by completed tasks)
```

> **Do not pass `--timeout=`.** `pytest-timeout` is NOT installed. pytest rejects
> the argument, runs **nothing**, and the wrapper still reports exit 0 — this
> produced a fake green run during the audit. Installing it is task **T6.9**.

---

## TABLE OF CONTENTS

- [1. Scoring model](#1-scoring-model)
- [2. No-collateral-damage rules](#2-no-collateral-damage-rules)
- [3. Corrections to prior audit documents](#3-corrections-to-prior-audit-documents)
- [PHASE 0 — Launch blockers](#phase-0--launch-blockers)
- [PHASE 1 — Distributed correctness](#phase-1--distributed-correctness)
- [PHASE 2 — Tenant routing and cache coherence](#phase-2--tenant-routing-and-cache-coherence)
- [PHASE 3 — Security hardening](#phase-3--security-hardening)
- [PHASE 4 — Data model](#phase-4--data-model)
- [PHASE 5 — Silent failures and observability](#phase-5--silent-failures-and-observability)
- [PHASE 6 — Test debt](#phase-6--test-debt)
- [PHASE 7 — Contract and dead code](#phase-7--contract-and-dead-code)
- [PHASE 8 — Load and capacity](#phase-8--load-and-capacity)
- [PHASE 9 — Staging verification](#phase-9--staging-verification)
- [PHASE 10 — Release gate](#phase-10--release-gate)
- [Sequencing](#sequencing-and-dependency-graph)
- [What this plan does not promise](#what-this-plan-does-not-promise)
- [Appendix A — Finding index](#appendix-a--finding-id-index)
- [Appendix B — Verified-good controls](#appendix-b--verified-good-controls-do-not-regress)

---

## 1. SCORING MODEL

No plan guarantees a score. The score comes from the acceptance gates actually
passing. Every task below has a falsifiable gate; the sum of those gates is what
moves each domain.

| # | Domain | Now | Target | Moved by |
|---|---|---:|---:|---|
| 1 | Multi-tenancy isolation | 45 | **95** | T0.1, T0.5, T2.1 + T6.1 |
| 2 | WhatsApp lifecycle and idempotency | 62 | **93** | T0.3, T1.4 |
| 3 | Conversation FSM | 70 | **88** | T2.3 decision + T6.7 |
| 4 | Booking and slot integrity | 55 | **95** | T0.2, T3.4 + T6.4 |
| 5 | Payments | 68 | **93** | T0.5, T0.2b, T4.1 |
| 6 | Admin authorization | 45 | **95** | T0.1 + T6.1 |
| 7 | Database and migrations | 72 | **90** | T4.1, T4.2, T7.3 |
| 8 | RLS / DB-level isolation | 30 | **85** | T4.3 (adopt `kriya_app`) |
| 9 | Connectors and integrations | 65 | **88** | T1.2, T5.2 |
| 10 | Scheduler / distributed safety | 58 | **92** | T1.1, T1.2, T1.3 + T6.6 |
| 11 | Observability | 55 | **88** | T3.1, T5.1, T5.3 |
| 12 | Application security | 58 | **92** | T0.6, T3.1, T3.2, T3.3 |
| 13 | AI / LLM safety | 78 | **88** | T5.1 + T6.8 |
| 14 | Healthcare data and privacy | 65 | **90** | T0.1 + T4.4 |
| 15 | Test quality | 70 | **92** | Phase 6 |
| 16 | Deployment / DevOps | 55 | **90** | T0.6, T3.2, T7.4 |
| 17 | Capacity and scalability | 40 | **85** | **Phase 8 only** — not achievable by code |
| 18 | Failure recovery | 45 | **93** | T0.3, T0.4, T1.1 + Phase 9 |

Sum = 1642 -> **91.2 / 100**.

**Ceilings if phases are skipped:**

| Skipped | Realistic ceiling |
|---|---:|
| Nothing | 91 |
| Phase 8 (load execution) | 89 |
| Phase 4 T4.3 (RLS adoption) | 88 |
| Phase 6 (test debt) | 82 |
| Phase 8 **and** Phase 9 | 84 |
| Phase 8, 9 and T4.3 | 80 |

---

## 2. NO-COLLATERAL-DAMAGE RULES

Binding on every task. **The largest risk in this plan is not the bugs — it is the
fixes.** These eight rules are the entire answer to "should not cause any other
errors or bugs while executing it".

### Rule 1 — Green to green

`pytest -q` must report `954 passed, 2 skipped` before every commit and the same
(plus new tests) after. Any test that changes status must be named in the commit
message with a justification.

**Tests expected to change status, and only these:**

- `tests/test_appointment.py::test_unique_references` — inverted by T0.2d
- `tests/test_appointment.py::test_reference_format` — updated by T0.2d

Anything else moving is a regression. Stop and investigate.

### Rule 2 — One task per commit

Never bundle. Commit message format:

```
fix(T0.1a): fail closed in AdminUser.can_access_clinic

<why, in 2-3 lines>

Tests: 954 passed, 2 skipped (unchanged)
```

### Rule 3 — Additive-first migrations

Production runs `numInstances: 2` (`render.yaml:8`), so old and new application
code run **simultaneously** during every rollout. Therefore:

- Every migration is **expand only**: `ADD COLUMN`, `CREATE INDEX CONCURRENTLY`,
  `ADD CONSTRAINT ... NOT VALID`.
- **No `DROP COLUMN`, no `ALTER TYPE`, no `DROP CONSTRAINT`** in the same release
  as the code that stops using it.
- Contract migrations ship one full release later (Phase 7).
- `CREATE INDEX CONCURRENTLY` cannot run inside a transaction. Check how
  `scripts/migrate.py` wraps statements before adding one; if it wraps the file in
  a transaction, the index goes in its own migration file or is applied manually
  with the step recorded in the runbook.

### Rule 4 — Fail-closed changes ship behind a flag for one release

Any change turning a permissive path into a denying path (T0.1, T1.4) ships with
`settings.<name>_enforce: bool = False`. In shadow mode it **logs what it would
have blocked** and allows the request. Flip to `True` only after 48h of clean logs
in both staging and production.

**This rule is what converts a potential outage into a log line.** It is the single
most important rule for avoiding workflow damage.

### Rule 5 — Never widen a query to fix a narrow bug

Several current defects (KRIYA-002, KRIYA-005) exist because someone removed a
filter to make something work. If a fix appears to need more data, resolve the
correct scope. Do not remove the filter.

### Rule 6 — Preserve every public contract

No route path, HTTP method, request field, or response key changes in Phases 0-5.
Adding an optional response field is allowed. API shape changes are Phase 7 only.

### Rule 7 — Touch-list discipline

Each task lists the exact files it may modify. Modifying anything else means the
task was mis-scoped — stop and report.

### Rule 8 — No new bare `except: pass`

The codebase already has 48 (see T5.1). Do not add a 49th. Every exception handler
introduced by this plan must log with context and emit a metric, or re-raise.

---

## 3. CORRECTIONS TO PRIOR AUDIT DOCUMENTS

The executing agent must know these, because prior documents in `docs/audits/`
make claims the source code does not support. **Source code is the only source of
truth.**

### 3.1 — `2026-08-25-production-load-test-report.md` overstates its evidence

| Claim in that report | What the test actually does | Verdict |
|---|---|---|
| "Scenario 2: 20-worker distributed lock contention ... PASSED — COMPLETE MUTUAL EXCLUSION" | `tests/test_phase_f_real_load_and_failure_injection.py::test_02_real_postgres_scheduler_locks_concurrency` (line 91) issues a **raw `INSERT INTO scheduler_locks`** and catches `psycopg2.errors.UniqueViolation`. It never calls `DistributedJobLock.acquire()`. | **Proves the table constraint. Proves nothing about the production lock path.** It can never fail on lease expiry or takeover — which *is* KRIYA-008. |
| "Scenario 1: 50-thread slot booking contention ... ZERO DOUBLE BOOKING" | `test_01_real_postgres_slot_concurrency_50_threads` (line 38) issues a raw `INSERT INTO appointments`. Genuine and valuable — it proves `idx_unique_active_slot` works under real contention. | **Valid for the DB constraint.** Does not exercise `book_appointment()` or `create_booking_with_payment()`, so it cannot catch KRIYA-001. |
| "Scenario 3: HTTP Spike Test (200 concurrent webhook requests)" | `test_03_mocked_in_memory_dispatch_burst_200_requests` (line 126) — the name says `mocked_in_memory_dispatch`. | **Mocked.** Not a load test. |

**Consequence:** T6.4 and T6.6 *strengthen existing tests* to cover the application
path. **Do not duplicate `test_01` or `test_02` — extend them.**

The justification for domain #17 is therefore: *component-level DB-constraint
concurrency evidence exists and is real; application-path concurrency and
multi-tenant capacity evidence does not.*

### 3.2 — An existing test ratifies a bug

`tests/test_appointment.py:96-101`:

```python
refs = [generate_booking_reference() for _ in range(100)]
# With 4-digit random suffix (1000-9999), minor collisions are possible
assert len(set(refs)) >= 90  # At least 90% unique
```

The suite was written to accommodate KRIYA-001. **Invert it (T0.2d), do not delete
it.**

### 3.3 — RLS claims

`migrations/049_force_row_level_security.sql` creates a correct `kriya_app` role
and correct tenant policies. **The application never connects as `kriya_app`** — it
uses `SUPABASE_SERVICE_ROLE_KEY`, which is `BYPASSRLS`, and every table also carries
an explicit `FOR ALL TO service_role USING (true)` bypass policy. Nothing calls
`SET app.clinic_id`. Any document claiming RLS-based multi-tenancy describes
policies that are not in the request path. See T4.3.

### 3.4 — Facts that make the work smaller than it looks

| Fact | Consequence |
|---|---|
| `message_queue.claim_message()` (`app/services/message_queue.py:141-158`) already performs a correct atomic CAS to `status='processing'` with `locked_at`. It is called **only** from `scheduler.py:255`, never on the hot path. | **T0.3 calls an existing correct function**, it does not write a new one. |
| `migrations/047_durable_inbound_messages.sql` already defines `status='processing'`, `locked_at`, `attempt_count`, `retry_at`, and `idx_inbound_messages_locked_at ON inbound_messages(status, locked_at) WHERE status = 'processing'` — an index shaped for a reaper that was never written. | **T0.3 and T0.4 need ZERO migrations.** |
| `ingest()` calls `sanitize_pii(raw_str)` **without** a `patient_name` argument (`message_queue.py:105`), so it redacts phone/Aadhaar/ABHA/email/DOB/age/patient-ID/PIN only. A normal body (`"I have fever"`, `"Cardiology"`, `"10:00 AM"`) passes through untouched. | **The stored payload IS reconstructable.** T0.4 is feasible. |
| `connectors/runner.py:101,144,163` implements `acquire_connector_lock` / `renew_connector_lock` / `release_connector_lock` — a correct DB lock **with an active heartbeat**. | **T1.1 ports an existing correct pattern.** Do not invent a second one. |
| `settings.booking_ref_prefix` (`app/config.py:36`) exists and is never read. | T0.2a finally uses it. |

---

# PHASE 0 — LAUNCH BLOCKERS

**Gate:** all six tasks complete before any customer traffic.
**Dependencies:** none between tasks — all six may run in parallel.
**Exception:** T0.3 and T0.4 **must ship in the same release**.

---

## T0.1 · Close cross-tenant admin access (KRIYA-002) · P0

**Severity:** P0 — mass PHI disclosure across tenants
**Domains moved:** #1 45->95, #6 45->95, #14 65->90
**Touch-list:**

- `app/routers/admin.py`
- `app/config.py`
- `admin/index.html`
- `migrations/051_clinic_admin_scope_constraint.sql` *(new)*

**Estimated:** 1.5 days including tests

### Problem statement

Three independent defects compound into a cross-tenant read:

1. **Schema permits it.** `migrations/011_clinic_admins.sql` declares
   `clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE, -- NULL for platform-level super admins`.
   Verified across all 52 migrations: **no CHECK constraint** ties
   `clinic_id IS NULL` to `role = 'super_admin'`. A `staff` row with
   `clinic_id = NULL` is legal.
2. **Authorization returns `True` on the unconfigured case**
   (`app/routers/admin.py:91-99`).
3. **The frontend always sends the sentinel.** `admin/index.html` hardcodes
   `clinic_id=default` at lines 2931, 4252, 4309, 4401, 4448, 4478 and never
   constructs a real clinic UUID in 5,104 lines. The list endpoints respond to
   `default` with an **unfiltered `select("*")`**.

The docstring of `resolve_clinic_id_for_write` (`admin.py:272+`) documents the
behaviour: *"the admin panel's own list endpoints skip the clinic_id filter
entirely when it is still `default`."*

**Exploit:** create a front-desk `staff` account whose `clinic_id` was not
populated (script bug, manual SQL, partially-completed onboarding). That user logs
into the normal admin panel and `GET /admin/patients` returns **every patient of
every tenant** — names, phone numbers, appointment histories. No exploitation
skill is required; the attacker is a legitimate user clicking a menu item.
`clinic_admins.username` is also globally `UNIQUE`, so usernames are enumerable
platform-wide.

**Route statistics (verified):** 78 routes, 78 with `Depends()`, 58 accepting
`clinic_id: str = "default"`, 61 `enforce_clinic_access` calls, 15
`resolve_clinic_id_for_write`, only **2** `can_access_clinic` references.

---

### T0.1a — Invert the authorization default

**File:** `app/routers/admin.py`, lines 91-99

**Current code:**

```python
    def can_access_clinic(self, target_clinic_id: str) -> bool:
        if self.role == "super_admin":
            return True
        if target_clinic_id == "default":
            return True
        if not self.clinic_id:
            return True
        return str(self.clinic_id) == str(target_clinic_id)
```

**Replacement:**

```python
    def can_access_clinic(self, target_clinic_id: str) -> bool:
        """Tenant boundary check. Fails CLOSED.

        An admin row with no clinic_id that is not a super_admin is a
        misconfigured account, not a platform account. Before migration 051,
        clinic_id was nullable for every role, so an unscoped 'staff' row could
        read the data of every tenant (KRIYA-002).

        Do NOT restore the `if not self.clinic_id: return True` branch. It is
        the exact hole that the chk_admin_scope constraint in 051 now prevents.
        """
        if self.role == "super_admin":
            return True
        if not self.clinic_id:
            if not settings.tenant_scope_enforce:
                logger.error(
                    f"TENANT_SCOPE_WOULD_DENY user='{self.username}' "
                    f"role={self.role} target='{target_clinic_id}' "
                    f"— unscoped non-super-admin account"
                )
                return True          # shadow mode, ONE release only (Rule 4)
            return False
        if target_clinic_id == "default":
            return True              # caller resolves 'default' -> self.clinic_id
        return str(self.clinic_id) == str(target_clinic_id)
```

**File:** `app/config.py` — add adjacent to the other boolean settings (near
`allow_unsigned_webhooks_dev`):

```python
    # T0.1 (KRIYA-002): when False, an unscoped non-super-admin is ALLOWED but
    # logged as TENANT_SCOPE_WOULD_DENY. Flip to True after 48h of clean logs in
    # both staging and production. See
    # docs/audits/2026-08-27-forensic-remediation-execution-plan.md Rule 4.
    tenant_scope_enforce: bool = False
```

Verify `logger` and `settings` are already imported in `admin.py`. They are —
confirm before editing.

---

### T0.1b — Make `enforce_clinic_access` refuse to return the sentinel

**File:** `app/routers/admin.py`, lines ~250-271

**Current tail of the function:**

```python
        if requested_clinic_id == "default" and user.clinic_id:
            return user.clinic_id

    return requested_clinic_id
```

**Replacement:**

```python
        if requested_clinic_id == "default":
            if user.clinic_id:
                return user.clinic_id
            if user.role != "super_admin":
                # Defence in depth. Unreachable once tenant_scope_enforce=True,
                # because can_access_clinic() denies first. Kept in case that
                # guard is ever weakened.
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account is not scoped to a clinic. Contact your administrator.",
                )

    return requested_clinic_id
```

`"default"` is still returned for a genuine `super_admin`. That is intentional and
is what T0.1c gates on.

---

### T0.1c — Remove the implicit unscoped reads

**File:** `app/routers/admin.py`

**Sites (verify each before editing — line numbers shift as you edit):**

| Line | Pattern |
|---:|---|
| 2244 | `if effective_clinic_id == "default":` |
| 2259 | `if effective_clinic_id == "default":` |
| 4279 | `if not effective_clinic_id or effective_clinic_id == "default":` |
| 2713 | `if not target_clinic_id or target_clinic_id == "default":` |
| 2857 | `if not target_clinic_id or target_clinic_id == "default":` |

Locate them all with:

```bash
grep -n 'effective_clinic_id == "default"\|target_clinic_id == "default"' app/routers/admin.py
```

**Current shape (example from `/admin/patients`, ~line 2244):**

```python
        if effective_clinic_id == "default":
            # unscoped: tenant-scoped operation with verified clinic authorization
            patients = supabase.table("patients").select("*").order("phone").execute()
        else:
            patients = (
                # unscoped: tenant-scoped operation with verified clinic authorization
                supabase.table("patients")
                .select("*")
                .eq("clinic_id", effective_clinic_id)
                .order("phone")
                .execute()
            )
```

**Replacement pattern — apply to all five sites:**

```python
        if effective_clinic_id == "default" and user.role == "super_admin":
            # Platform-wide view. Only a super_admin can reach this branch.
            # Before T0.1 any principal whose scope resolved to the "default"
            # sentinel landed here and read the rows of every tenant (KRIYA-002).
            patients = (
                supabase.table("patients")
                .select("*")
                .order("phone")
                .limit(2000)          # T3.4: no admin read may be unbounded
                .execute()
            )
        else:
            patients = (
                supabase.table("patients")
                .select("*")
                .eq("clinic_id", effective_clinic_id)
                .order("phone")
                .execute()
            )
```

Two changes per site:

1. Add `and user.role == "super_admin"` to the condition.
2. Add `.limit(2000)` to the platform-wide branch.

**Also delete** the `# unscoped: tenant-scoped operation with verified clinic
authorization` comments at every site. They assert something untrue and will
mislead the next reader.

> **Blast-radius note.** If `user.role != "super_admin"` and the scope is
> `"default"`, control now falls to the `else` branch and filters on the literal
> string `"default"`, which matches no real clinic UUID and returns zero rows.
> That is correct fail-closed behaviour, and with T0.1b it is unreachable anyway.

Verify `user` is in scope at each of the five sites. All 78 admin routes already
have `user: AdminUser = Depends(...)` in their signature, so this should not
require a signature change.

---

### T0.1d — Frontend sends the real clinic scope

**File:** `admin/index.html`

Defence in depth, **not** the fix. The server must never trust it. Its purpose is
to remove the sentinel from normal traffic so `"default"` becomes a genuine
super-admin-only signal and the T0.1a shadow logs stay clean.

**Step 1.** Confirm `GET /admin/me` returns `clinic_id`:

```bash
grep -n '@router.get("/me")' -A 25 app/routers/admin.py
```

If it does not, add `clinic_id` to that response. Adding a field is
backward-compatible and permitted under Rule 6.

**Step 2.** In `admin/index.html`, near the existing `auth` and `API` globals
(~line 2270, just before `async function api(path)`), add:

```javascript
// T0.1d: real clinic scope, sourced from the server via /admin/me.
// 'default' is the platform-wide sentinel, honoured only for super_admin.
let CLINIC_SCOPE = 'default';
```

**Step 3.** In the existing post-login handler that calls `/admin/me`:

```javascript
CLINIC_SCOPE = me.clinic_id || 'default';
```

**Step 4.** Replace every hardcoded literal:

```bash
grep -n 'clinic_id=default' admin/index.html
```

Change each `clinic_id=default` to `clinic_id=${CLINIC_SCOPE}` **and ensure the
containing string is a template literal (backticks), not single quotes.** This is
the most common way to break this task — a `${}` inside `'...'` is sent literally.

**Step 5.** Syntax-check. Write the extracted JS into the repo directory or the
session scratchpad, **not** `/tmp` — Git Bash on Windows maps `/tmp` such that
`node --check /tmp/x.js` fails with `Cannot find module`.

```
python - <<PYEOF
import re
h = open('admin/index.html', encoding='utf-8').read()
s = re.findall(r'<script[^>]*>(.*?)</script>', h, re.S)
open('adm_check.js', 'w', encoding='utf-8').write('\n'.join(s))
PYEOF
node --check adm_check.js && echo "index.html JS: SYNTAX OK" && rm adm_check.js
```

Baseline: at `128f777` both `admin/index.html` (144,580 chars, 1 script block) and
`admin/platform.html` pass `node --check`.

---

### T0.1e — Migration `051_clinic_admin_scope_constraint.sql`

**File:** `migrations/051_clinic_admin_scope_constraint.sql` *(new)*

```sql
-- Migration 051: Prevent unscoped non-super-admin accounts.
-- Root cause of KRIYA-002. clinic_admins.clinic_id has been nullable for every
-- role since migration 011, so an unscoped 'staff' row could read every tenant
-- via AdminUser.can_access_clinic().
--
-- Safe to run against live data with numInstances: 2. Adds no columns, modifies
-- no rows, and the constraint is NOT VALID so existing rows are untouched.

-- 1. Surface offenders BEFORE constraining. This should return zero in a healthy
--    production database. If it does not, remediate each row manually before
--    running the VALIDATE step in section 3.
DO $$
DECLARE
    offenders INT;
BEGIN
    SELECT count(*) INTO offenders
      FROM clinic_admins
     WHERE clinic_id IS NULL
       AND role IS DISTINCT FROM 'super_admin';

    IF offenders > 0 THEN
        RAISE WARNING
            'MIGRATION 051: % unscoped non-super-admin account(s) found. '
            'Constraint added as NOT VALID, so these rows keep working at the '
            'database layer, but they are DENIED at the application layer once '
            'settings.tenant_scope_enforce = True. Assign a clinic_id or promote '
            'to super_admin, then run: '
            'ALTER TABLE clinic_admins VALIDATE CONSTRAINT chk_admin_scope;',
            offenders;
    END IF;
END $$;

-- 2. NOT VALID: enforced for INSERT and UPDATE immediately; existing rows are
--    not scanned and not rejected. This is what makes it safe mid-rollout.
ALTER TABLE clinic_admins
    ADD CONSTRAINT chk_admin_scope
    CHECK (role = 'super_admin' OR clinic_id IS NOT NULL) NOT VALID;

-- 3. RUN MANUALLY, NOT IN THIS MIGRATION, after remediating any offenders:
--      ALTER TABLE clinic_admins VALIDATE CONSTRAINT chk_admin_scope;
```

---

### T0.1 · Rollout

1. Ship migration 051 and all code with `tenant_scope_enforce = False`.
2. Run 48h in staging and production.
3. Search logs for `TENANT_SCOPE_WOULD_DENY`. Every hit is a real account needing
   a `clinic_id` (or promotion to `super_admin`). Remediate each.
4. Run `ALTER TABLE clinic_admins VALIDATE CONSTRAINT chk_admin_scope;`.
5. Set `tenant_scope_enforce = True`, redeploy.
6. Confirm no `403` spike in admin traffic over the following 24h.

### T0.1 · Rollback

- Code: `git revert` the commits.
- Migration: `ALTER TABLE clinic_admins DROP CONSTRAINT chk_admin_scope;`
- **No data is modified at any step.** Rollback is total.

### T0.1 · Acceptance gate

- [ ] `tests/test_tenant_isolation_matrix.py` passes (written in T6.1): 5 principal
      types x 78 routes, correct status code for each, **zero rows belonging to a
      foreign clinic in any response**.
- [ ] `test_unscoped_staff_is_denied`: a `clinic_admins` row with `role='staff'`
      and `clinic_id=NULL` receives `403` on `GET /admin/patients` when
      `tenant_scope_enforce=True`.
- [ ] `test_unscoped_staff_is_shadow_logged`: the same request with
      `tenant_scope_enforce=False` returns `200` **and** emits
      `TENANT_SCOPE_WOULD_DENY`.
- [ ] `test_super_admin_platform_view_still_works`: a `super_admin` still receives
      rows from all tenants. **Regression guard — this is the workflow most at
      risk from this task.**
- [ ] `test_scoped_admin_sees_only_own_clinic`.
- [ ] Real-Postgres: `INSERT INTO clinic_admins (role, clinic_id) VALUES ('staff', NULL)`
      raises SQLSTATE `23514` after `VALIDATE CONSTRAINT`.
- [ ] `node --check` passes on the extracted `admin/index.html` JS.
- [ ] Baseline suite unchanged: `954 passed, 2 skipped`.

---

## T0.2 · Collision-resistant booking references (KRIYA-001) · P0

**Severity:** P0 — progressive platform-wide booking outage
**Domains moved:** #4 55->95, #5 contributory
**Touch-list:**

- `app/utils/helpers.py`
- `app/database.py`
- `app/services/payment.py`
- `tests/test_appointment.py`
- `migrations/052_booking_ref_per_tenant.sql` *(new)*

**Estimated:** 1 day

### Problem statement

`app/utils/helpers.py:7-14`:

```python
def generate_booking_reference() -> str:
    """Generate a unique booking reference."""
    import random
    from datetime import datetime

    year = datetime.now().year
    number = str(random.randint(1000, 9999)).zfill(4)
    return f"MC-{year}-{number}"
```

`migrations/001_initial_schema.sql:112`:

```sql
ALTER TABLE appointments ADD COLUMN booking_ref VARCHAR(20) UNIQUE;
```

- **9,000 possible values per calendar year**, shared **globally** across all
  tenants (the UNIQUE is on the bare column, not `(clinic_id, booking_ref)`).
- `random` is the non-cryptographic Mersenne Twister.
- **No retry on collision.**
- `settings.booking_ref_prefix` (`config.py:36`) is defined and never read, so
  per-tenant prefixes do not disambiguate either.

Birthday paradox: `P(collision) = 1 - exp(-n^2/18000)`.

| Bookings (platform-wide) | P(at least one collision) |
|---:|---:|
| 112 | ~50% |
| 300 | ~99.3% |
| 1,000 | ~100% |

The `23505` propagates into the handler of `book_appointment()` (`app/database.py`
~line 660), which maps any message containing `"duplicate"`, `"unique"` or
`"23505"` to `{"success": False, "reason": "slot_taken"}`. **The patient is told
the slot was just taken. It was not.** Retrying picks a new random value and often
succeeds, so the failure is intermittent and will be triaged as a
slot-availability bug for months.

**Call sites (both must be updated):**

```
app/database.py:651-653          book_appointment()
app/services/payment.py:152-154  create_booking_with_payment()
```

---

### T0.2a — Replace the generator

**File:** `app/utils/helpers.py`, lines 7-14 — replace entirely:

```python
def generate_booking_reference(prefix: Optional[str] = None) -> str:
    """Collision-resistant, per-tenant booking reference.

    Was MC-{year}-{4 random digits}: 9,000 values/year against a GLOBALLY unique
    column with no retry — ~50% collision probability at 112 platform-wide
    bookings, and the resulting 23505 was reported to the patient as
    "slot_taken" (KRIYA-001).

    32^8 = 1.1e12 values per prefix per year. Ambiguous glyphs (O/0, I/1) are
    excluded so the reference is safe to read aloud over the phone at reception.
    """
    import secrets
    from datetime import datetime

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 chars, no O I 0 1
    p = (prefix or settings.booking_ref_prefix or "MC").strip().upper()[:6]
    body = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{p}-{datetime.now().year}-{body}"
```

Module imports required at the top of `app/utils/helpers.py`:

```python
from typing import Optional
from app.config import settings
```

`from typing import Optional` is already present. **Check for an import cycle
before adding `from app.config import settings`** — if `app.config` imports
anything that imports `app.utils.helpers`, move the settings import inside the
function body instead.

**Length budget:** `6 + 1 + 4 + 1 + 8 = 20` characters maximum, which exactly
fills the current `VARCHAR(20)`. T0.2c widens it to 32 anyway — sitting exactly on
a boundary is how the next truncation bug happens.

---

### T0.2b — Constraint-identity helpers and bounded retry

**File:** `app/utils/helpers.py` — add:

```python
def _pg_error_text(exc: Exception) -> str:
    """Best-effort lowercase error text from a PostgREST or psycopg exception."""
    return str(exc).lower()


def is_booking_ref_conflict(exc: Exception) -> bool:
    """True only for a booking_ref uniqueness violation."""
    s = _pg_error_text(exc)
    return "23505" in s and (
        "booking_ref" in s or "uq_appointment_booking_ref" in s
    )


def is_slot_conflict(exc: Exception) -> bool:
    """True only for the partial slot unique indexes.

    Both index names are matched because migrations 008 and 043 define
    functionally identical indexes and either may fire until T4.2 drops one.

    Do NOT match on the bare word "violates". app/services/payment.py:100
    currently does, which swallows foreign-key, NOT NULL and CHECK failures and
    reports them to the patient as slot conflicts, hiding real data-integrity
    errors from operators.
    """
    s = _pg_error_text(exc)
    return "23505" in s and (
        "idx_unique_active_slot" in s or "uq_appointment_active_slot" in s
    )
```

> ### BLOCKING VERIFICATION — do this before relying on the helpers
>
> Confirm the Supabase/PostgREST client actually surfaces the constraint name in
> the exception string. Force a real duplicate insert against the test Postgres
> and print `str(exc)`. If the constraint name is **not** present, the helpers
> must instead read `exc.details`, `exc.message` or `exc.code` from the PostgREST
> `APIError` object.
>
> **Do not ship helpers that silently never match.** That would turn every
> booking_ref collision into an unhandled exception — worse than today. The
> acceptance gate `test_booking_ref_helpers_match_real_violations` exists
> specifically to catch this.

**File:** `app/database.py`, lines ~650-663

**Current code:**

```python
        # Generate booking reference
        from app.utils.helpers import generate_booking_reference

        ref = generate_booking_reference()
        data["booking_ref"] = ref

        # ...

        result = supabase.table("appointments").insert(data).execute()
```

**Replacement:**

```python
        from app.utils.helpers import (
            generate_booking_reference,
            is_booking_ref_conflict,
            is_slot_conflict,
        )

        # Bounded retry: a booking_ref collision must NOT be reported to the
        # patient as "slot_taken" (KRIYA-001). Only the partial slot unique
        # indexes mean the slot is genuinely gone.
        result = None
        for attempt in range(3):
            data["booking_ref"] = generate_booking_reference(
                clinic.get("booking_ref_prefix") if isinstance(clinic, dict) else None
            )
            try:
                result = supabase.table("appointments").insert(data).execute()
                break
            except Exception as e:
                if is_booking_ref_conflict(e) and attempt < 2:
                    logger.warning(
                        f"booking_ref collision (attempt {attempt + 1}/3), regenerating"
                    )
                    continue
                if is_slot_conflict(e):
                    return {"success": False, "reason": "slot_taken"}
                raise

        if result is None:
            logger.error(
                "booking_ref generation exhausted 3 attempts — entropy source suspect"
            )
            return {"success": False, "reason": "internal_error"}
```

**Check:** does `book_appointment()` have a `clinic` dict in scope, or only
`clinic_id`? Inspect the signature. If only `clinic_id` is available, pass `None`
as the prefix for now and add the per-clinic prefix in a follow-up — **do not add
a database lookup inside the retry loop.**

**Check:** the existing `except` block that maps to `slot_taken` may live further
down the function. Ensure the new inner handler does not leave a now-unreachable
outer handler, and that any outer handler still catching broadly is narrowed to
`is_slot_conflict`.

**File:** `app/services/payment.py`

1. Apply the identical retry pattern at lines ~152-154
   (`create_booking_with_payment`), wrapping the `booking_data` insert.
2. **Replace the over-broad mapping at line ~100:**

```bash
grep -n 'violates' app/services/payment.py
```

The current condition matches `"duplicate"`, `"unique"` or `"violates"`.
Replace it with `is_slot_conflict(e)`. This is the KRIYA-005 tail fix; it belongs
here because it shares the helper.

---

### T0.2c — Migration `052_booking_ref_per_tenant.sql`

**File:** `migrations/052_booking_ref_per_tenant.sql` *(new)*

```sql
-- Migration 052: booking_ref becomes per-tenant unique and wider (KRIYA-001).
-- Migration 001 created it as globally UNIQUE VARCHAR(20). Global uniqueness
-- means one busy tenant exhausts the namespace for every other tenant.
--
-- EXPAND ONLY. The original global UNIQUE constraint is deliberately NOT dropped
-- here — it is dropped in migration 054 (Phase 7), one full release later, so a
-- rollback to the previous application version cannot orphan data.

-- 1. Widen first. The maximum of the new format is exactly 20 characters; do not
--    sit on the boundary.
ALTER TABLE appointments ALTER COLUMN booking_ref TYPE VARCHAR(32);

-- 2. Per-tenant uniqueness. CONCURRENTLY = no table lock, safe with
--    numInstances: 2 serving live traffic.
--    NOTE: CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
--    Verify how scripts/migrate.py wraps statements before applying. If the
--    runner wraps each file in a transaction, this statement must be applied
--    manually and the step recorded in the deployment runbook.
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_appointment_booking_ref
    ON appointments (clinic_id, booking_ref)
    WHERE booking_ref IS NOT NULL;
```

**Historical rows are deliberately left untouched.** Existing `MC-YYYY-NNNN`
references have already been issued to patients who hold them; rewriting them
would break every reception lookup.

**New references cannot collide with the surviving global constraint** — the new
space is ~1.1e12 per prefix per year against at most a few thousand legacy values.

---

### T0.2d — Invert the test that ratified the bug

**File:** `tests/test_appointment.py`

**Current, lines ~88-101:**

```python
    def test_reference_format(self):
        """Test booking reference format."""
        from app.utils.helpers import generate_booking_reference

        ref = generate_booking_reference()
        assert ref.startswith("MC-")
        assert len(ref) == 12  # MC-YYYY-NNNN format

    def test_unique_references(self):
        """Test that references are likely unique."""
        from app.utils.helpers import generate_booking_reference

        refs = [generate_booking_reference() for _ in range(100)]
        # With 4-digit random suffix (1000-9999), minor collisions are possible
        assert len(set(refs)) >= 90  # At least 90% unique
```

**Replacement:**

```python
    def test_reference_format(self):
        """Booking reference format: PREFIX-YYYY-XXXXXXXX, unambiguous alphabet."""
        import re
        from app.utils.helpers import generate_booking_reference

        ref = generate_booking_reference()
        assert re.fullmatch(r"[A-Z]{2,6}-\d{4}-[A-HJ-NP-Z2-9]{8}", ref), ref
        assert 15 <= len(ref) <= 20

    def test_custom_prefix_is_honoured(self):
        """settings.booking_ref_prefix was dead config before T0.2 (KRIYA-001)."""
        from app.utils.helpers import generate_booking_reference

        assert generate_booking_reference("ABCD").startswith("ABCD-")

    def test_unique_references(self):
        """Booking references must be collision-free at volume.

        The previous assertion (>= 90 unique out of 100) ratified KRIYA-001:
        a 9,000-value namespace against a globally UNIQUE column, where every
        collision surfaced to the patient as "slot_taken".
        """
        from app.utils.helpers import generate_booking_reference

        refs = [generate_booking_reference() for _ in range(100_000)]
        assert len(set(refs)) == 100_000
```

These two status changes are the **only** permitted deviations from the baseline
under Rule 1. Name them in the commit message.

---

### T0.2 · Rollout

1. Apply migration 052 (widen + concurrent index).
2. Deploy the code. New bookings immediately use the new format.
3. Monitor for `booking_ref collision (attempt` — expect **zero**. Any occurrence
   at the new entropy level means the entropy source is broken.
4. Monitor the `slot_taken` rate — it should **drop**, because false `slot_taken`
   responses caused by reference collisions disappear.

### T0.2 · Rollback

- Code: `git revert`. Old code writes old-format references; the new per-tenant
  index accepts them, and the surviving global constraint still applies.
- Migration: `DROP INDEX CONCURRENTLY uq_appointment_booking_ref;` The
  `VARCHAR(32)` widening does not need reverting and should not be reverted
  (narrowing a column with data in it can fail).

### T0.2 · Acceptance gate

- [ ] `test_unique_references`: 100,000 references, zero duplicates.
- [ ] `test_reference_format` and `test_custom_prefix_is_honoured` pass.
- [ ] `test_booking_ref_helpers_match_real_violations` (real Postgres): force a
      real `booking_ref` duplicate and a real slot duplicate; assert
      `is_booking_ref_conflict` and `is_slot_conflict` each return `True` for the
      right one and `False` for the other. **This gates the whole task.**
- [ ] `test_booking_ref_collision_regenerates_and_does_not_report_slot_taken`:
      monkeypatch the generator to a constant, insert twice, assert the second
      call retries and the result is **not** `slot_taken`.
- [ ] `test_slot_conflict_still_reports_slot_taken` (real Postgres): two inserts
      on the same slot -> `{"success": False, "reason": "slot_taken"}`.
      **Regression guard for the primary workflow.**
- [ ] `test_fk_violation_is_not_slot_taken`: insert with a bogus `clinic_id`,
      assert it raises rather than returning `slot_taken`.
- [ ] Real Postgres: the same `booking_ref` string under two different
      `clinic_id` values both insert successfully.
- [ ] Baseline suite unchanged except the two named tests.

---

## T0.3 · Leased message claim and reaper (KRIYA-003) · P0 · NO MIGRATION

**Severity:** P0 — silent permanent loss of patient messages
**Domains moved:** #2 62->93, #18 45->93 (with T0.4)
**Touch-list:**

- `app/routers/webhook.py`
- `app/services/message_queue.py`
- `app/services/scheduler.py`

**Estimated:** 1.5 days

### Problem statement

The hot path is:

1. `ingest()` persists to `inbound_messages` with `status='received'`
2. HTTP 200 returned to Meta
3. `BackgroundTasks` -> `process_message_safe` -> `process_message`
4. `webhook.py:208` -> `message_queue.acquire(message_id)` — atomic INSERT into
   `processed_messages`, the **dedup claim**
5. `conversation_manager.handle_message()` — the actual work
6. `mark_completed()`

**The claim at step 4 is written before the work at step 5, and the status of the
durable row is never moved to `'processing'`.**

If the process dies between steps 4 and 5 (Render deploy, scale-down, OOM kill):

- Meta retries the webhook -> `acquire()` finds the existing `processed_messages`
  row -> returns `False` -> **dropped as a duplicate**
- The recovery sweep is independently broken (KRIYA-004 / T0.4)
- Whichever path eventually runs calls `mark_completed()`, writing
  `status='completed'` — **a false record.** Operations sees a healthy queue.

Production runs `numInstances: 2` (`render.yaml:8`) with `WEB_CONCURRENCY: 2`, so
**every deploy rolls all four processes** and is an opportunity to silently drop
in-flight patient messages.

### Why this fix is small

`message_queue.claim_message()` (`app/services/message_queue.py:141-158`) already
does the right thing:

```python
    async def claim_message(self, message_id: str) -> bool:
        """Atomically claim a message for processing."""
        ...
        res = (
            supabase.table("inbound_messages")
            .update({"status": "processing", "locked_at": now_iso, "updated_at": now_iso})
            .eq("message_id", message_id)
            .in_("status", ["received", "failed_retryable"])
            .execute()
        )
        return bool(res.data)
```

It is called **only** from `scheduler.py:255` (the recovery sweep), never on the
hot path. And `migrations/047` already defines the index the reaper needs:

```sql
CREATE INDEX IF NOT EXISTS idx_inbound_messages_locked_at
    ON inbound_messages(status, locked_at)
    WHERE status = 'processing';
```

**No migration is required for this task.**

---

### T0.3a — Claim the durable row on the hot path

**File:** `app/routers/webhook.py`, lines 207-214

**Current code:**

```python
        # -- Primary Idempotency: Atomic Supabase INSERT (closes the race window) --
        acquired = await message_queue.acquire(message_id, clinic_id=clinic_id)
        if not acquired:
            logger.info(
                f"Webhook: duplicate message {message_id} dropped by atomic queue"
            )
            return
        # -- End Idempotency Gate --
```

**Replacement:**

```python
        # -- Primary Idempotency: Atomic Supabase INSERT (closes the race window) --
        acquired = await message_queue.acquire(message_id, clinic_id=clinic_id)
        if not acquired:
            logger.info(
                f"Webhook: duplicate message {message_id} dropped by atomic queue"
            )
            return

        # Move the durable row to 'processing' so that a crash between here and
        # mark_completed() is DETECTABLE. Without this the row stays 'received'
        # forever, the processed_messages claim blocks the retry from Meta, and
        # the message of the patient is lost silently while later being marked
        # 'completed' (KRIYA-003).
        #
        # claim_message() is an atomic CAS on status IN ('received',
        # 'failed_retryable') and is already indexed for the reaper by
        # idx_inbound_messages_locked_at (migration 047).
        #
        # The return value is deliberately NOT used as a gate: acquire() above is
        # the authoritative anti-duplicate check. Treating a claim_message() miss
        # as a drop would introduce a NEW message-loss path.
        await message_queue.claim_message(message_id)
        # -- End Idempotency Gate --
```

---

### T0.3b — The reaper

**File:** `app/services/message_queue.py` — add immediately after `release()`
(which ends around line 459):

```python
    async def reap_abandoned_claims(
        self, lease_seconds: int = 120, limit: int = 50
    ) -> int:
        """Release claims whose worker died mid-processing.

        A row stuck in 'processing' past the lease means the process that claimed
        it is gone. Deleting the processed_messages row makes the message
        eligible for replay; setting failed_retryable + retry_at hands it to the
        existing drain_pending_retry_messages job, which knows how to reconstruct
        and replay from `payload` (after T0.4).

        Uses idx_inbound_messages_locked_at (migration 047). No migration needed.
        """
        from app.database import supabase
        from datetime import datetime, timezone, timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
        ).isoformat()

        try:
            stale = (
                supabase.table("inbound_messages")
                .select("message_id, attempt_count")
                .eq("status", "processing")
                .lt("locked_at", cutoff)
                .limit(limit)
                .execute()
            )
        except Exception as e:
            logger.error(f"Reaper: failed to query abandoned claims: {e}")
            return 0

        reaped = 0
        now_iso = datetime.now(timezone.utc).isoformat()

        for row in (stale.data or []):
            mid = row["message_id"]
            try:
                # 1. Drop the processed_messages claim so a replay can proceed.
                await self.release(mid)

                # 2. Hand to the retry drain. The .eq("status", "processing")
                #    predicate is a CAS: if another process reaped this row
                #    first, the update matches nothing and no double-handling
                #    occurs.
                supabase.table("inbound_messages").update(
                    {
                        "status": "failed_retryable",
                        "retry_at": now_iso,
                        "last_error": "abandoned: worker died mid-processing",
                        "updated_at": now_iso,
                    }
                ).eq("message_id", mid).eq("status", "processing").execute()

                reaped += 1
                logger.warning(f"Reaper: released abandoned claim for {mid}")
            except Exception as e:
                logger.error(f"Reaper: failed to release {mid}: {e}")

        if reaped:
            logger.warning(f"Reaper: released {reaped} abandoned claim(s)")

        return reaped
```

Emit a metric alongside the log using whatever convention
`app/services/metrics.py` already establishes (see T5.3).

---

### T0.3c — Schedule the reaper

**File:** `app/services/scheduler.py`

Add a 20th job alongside the existing 19. Follow the exact `add_job` style used by
its neighbours (read lines 39-200 before editing):

```python
        self.scheduler.add_job(
            self.reap_abandoned_message_claims,
            "interval",
            seconds=60,
            id="reap_abandoned_message_claims",
            max_instances=1,
            coalesce=True,
        )
```

Job body, following the same `distributed_job_lock` pattern as the other 15 locked
jobs:

```python
    async def reap_abandoned_message_claims(self):
        """Release message claims abandoned by a worker that died mid-processing.

        Lease 45s < interval 60s so a stuck reaper cannot block the next tick.
        Reap threshold 120s > the 15s phone-lock timeout plus typical handler
        time, so a merely slow handler is never reaped out from under itself.
        """
        from app.services.distributed_lock import distributed_job_lock

        async with distributed_job_lock(
            "reap_abandoned_claims", lease_seconds=45
        ) as acquired:
            if not acquired:
                return
            await message_queue.reap_abandoned_claims(lease_seconds=120)
```

---

### T0.3 · Rollout

**T0.3 and T0.4 MUST ship in the same release.**

Before T0.4 lands, the output of the reaper flows into
`drain_pending_retry_messages`, which replays via the same broken empty-message
path. Shipping T0.3 alone would mean the reaper correctly resurrects messages only
to blank them — turning a silent loss into a silent corruption, which is worse.

Otherwise this task is purely additive and safe to deploy directly.

### T0.3 · Rollback

`git revert`. No schema change, no data change.

### T0.3 · Acceptance gate

- [ ] `test_crash_between_claim_and_handler_is_recovered` (real Postgres): ingest
      -> `acquire` -> `claim_message` -> simulate death (no `mark_completed`) ->
      backdate `locked_at` past the lease -> run reaper -> assert the
      `processed_messages` row is gone **and** `inbound_messages.status ==
      'failed_retryable'`.
- [ ] `test_reaper_does_not_touch_in_flight`: identical setup with
      `locked_at = now()`; assert the reaper returns `0` and the row is unchanged.
      **Regression guard preventing the reaper from eating live traffic.**
- [ ] `test_reaper_is_idempotent`: run the reaper twice against the same stale
      row; assert the second run is a no-op and produces no duplicate replay.
- [ ] `test_hot_path_sets_processing_status`: after `process_message` claims,
      assert `inbound_messages.status == 'processing'` and `locked_at IS NOT NULL`.
- [ ] `test_claim_message_failure_does_not_drop_message`: make `claim_message`
      raise; assert `handle_message` is still invoked (the return value must not
      gate).
- [ ] Baseline suite unchanged.

---

## T0.4 · Recovery replays the real message (KRIYA-004) · P0 · NO MIGRATION

**Severity:** P0 — the recovery subsystem destroys the data it exists to recover
**Domains moved:** #18 (with T0.3)
**Touch-list:** `app/services/scheduler.py`
**Estimated:** 1 day

### Problem statement

`app/services/scheduler.py:212-278` (`recover_pending_inbound_messages`) replays
messages using:

```python
class SimpleMessage:
    def __init__(self, mid, phone):
        self.id = mid
        self.from_ = phone
        self.type = "text"
        self.text = type("obj", (object,), {"body": ""})   # <- EMPTY BODY
        self.button = None
        self.interactive = None
```

Selection predicate: `status IN ('received', 'failed_retryable', 'processing')
LIMIT 20` — **with no age filter.**

Two defects:

**(a) The replayed message has no content.** `inbound_messages.payload` holds the
stored payload, but recovery never reads it. Feeding `""` into the 25-state FSM
does not resume the booking of the patient — it produces a re-prompt or fallback,
and the actual message (symptom, slot choice, confirmation) is discarded.

**(b) It races live traffic.** `'received'` is the status of messages *currently
being processed on the hot path* (because the hot path never advanced the status
before T0.3a). With no age filter, this job — running every 60 seconds across up
to 4 processes — selects messages a live request is handling right now.

### Why the fix is feasible

`ingest()` at `app/services/message_queue.py:104-105`:

```python
raw_str = json.dumps(payload) if isinstance(payload, dict) else str(payload)
sanitized_payload = json.loads(sanitize_pii(raw_str)) if isinstance(payload, dict) else {...}
```

`sanitize_pii` is called **without** a `patient_name` argument, so per
`app/utils/pii_sanitizer.py` it redacts only: Indian phone numbers, Aadhaar, ABHA,
email, labelled DOB, age patterns, labelled patient IDs, labelled PIN codes.

A normal inbound body — `"I have fever and headache"`, `"Cardiology"`,
`"10:00 AM"` — **passes through untouched.** The payload is reconstructable for
the overwhelming majority of messages. Where redaction did occur, redacting was
the correct behaviour anyway.

---

### T0.4a — Reconstruct instead of synthesizing

**File:** `app/services/scheduler.py`

**FIRST — determine the stored envelope shape. Do not guess.**

```bash
grep -n "message_queue.ingest(" -B 15 app/routers/webhook.py
grep -n "class .*Message" app/models/message.py
```

Establish whether `ingest()` receives the **full Meta webhook envelope**
(`{"entry": [{"changes": [{"value": {"messages": [...]}}]}]}`) or an
**already-unwrapped message dict**, then write the reconstruction to match. Add a
test asserting the shape so a future change to `ingest()` breaks loudly.

Delete the `SimpleMessage` class and replace with:

```python
def _reconstruct_message(row: dict):
    """Rebuild the original inbound message from the durable payload.

    The previous implementation synthesized a text message with body="" —
    recovery discarded the very content it existed to preserve (KRIYA-004).

    Returns None if the payload cannot yield a message. The caller MUST
    dead-letter and alert rather than replay a blank.
    """
    from app.models.message import WhatsAppMessage

    payload = row.get("payload") or {}

    # Full Meta envelope
    try:
        entry = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    except (KeyError, IndexError, TypeError):
        # Already-unwrapped message dict
        entry = payload if payload.get("type") else None

    if not entry:
        return None

    try:
        return WhatsAppMessage(**entry)
    except Exception as e:
        logger.warning(
            f"Reconstruction failed for {row.get('message_id')}: {e}"
        )
        return None
```

---

### T0.4b — Age filter and dead-letter on unreconstructable

**File:** `app/services/scheduler.py`, the selection query in
`recover_pending_inbound_messages`

**Current:** `status IN ('received', 'failed_retryable', 'processing') LIMIT 20`,
no age filter.

**Replacement:**

```python
        from datetime import datetime, timezone, timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=lease_timeout_seconds)
        ).isoformat()

        rows = (
            supabase.table("inbound_messages")
            .select(
                "message_id, phone, display_phone, phone_number_id, "
                "payload, attempt_count"
            )
            # 'processing' is deliberately EXCLUDED — that is the job of the
            # reaper (T0.3b). Two subsystems competing for the same rows is how
            # double replies happen.
            .in_("status", ["received", "failed_retryable"])
            # Never race the live hot path. Before T0.4 there was no age filter
            # and 'received' is the status of in-flight messages (KRIYA-004).
            .lt("created_at", cutoff)
            .order("created_at")
            .limit(20)
            .execute()
        )
```

Default `lease_timeout_seconds` is already `300` in the signature — verify and
keep it.

**Then, per row:**

```python
            msg = _reconstruct_message(row)
            if msg is None:
                logger.error(
                    f"RECOVERY_UNRECONSTRUCTABLE message_id={row['message_id']} "
                    f"— dead-lettering rather than replaying a blank message"
                )
                await message_queue.mark_failed(
                    row["message_id"],
                    "payload unreconstructable",
                    max_retries=0,
                )
                continue
```

**Never replay a blank.** A dead letter with an alert is recoverable by a human; a
blank replay silently corrupts the FSM state of the patient.

Verify that `mark_failed(..., max_retries=0)` routes straight to `dead_letter`
rather than scheduling another retry. Read `message_queue.py:179` and confirm the
branch. If `max_retries=0` does not dead-letter, use whatever value does.

---

### T0.4 · Rollout

Ship with T0.3 in the same release.

### T0.4 · Rollback

`git revert`. No schema change, no data change.

### T0.4 · Acceptance gate

- [ ] `test_recovery_replays_original_body`: ingest a message whose body is
      `"I have fever and headache"`, abandon it, backdate `created_at`, run the
      sweep, assert `handle_message` receives **that exact string**.
- [ ] `test_recovery_replays_button_reply`: same for an interactive/button
      message — assert the `interactive` data is reconstructed, not just text.
- [ ] `test_recovery_ignores_recent_messages`: row with `created_at = now()`,
      assert not swept. **Regression guard against racing live traffic.**
- [ ] `test_recovery_deadletters_unreconstructable`: row with `payload = {}`,
      assert dead-letter path taken, `RECOVERY_UNRECONSTRUCTABLE` logged, and
      `handle_message` **not** called.
- [ ] `test_recovery_and_reaper_status_sets_are_disjoint`: assert the recovery
      sweep never selects `'processing'` and the reaper only selects
      `'processing'`.
- [ ] Baseline suite unchanged.

---

## T0.5 · Remove the unscoped payment fallback (KRIYA-005) · P0

**Severity:** P0 — cross-tenant booking confirmation without payment
**Domains moved:** #5 68->93, #1 contributory
**Touch-list:** `app/services/payment.py`
**Estimated:** 0.5 day

### Problem statement

`process_payment_webhook` (`app/services/payment.py:293`) correctly verifies the
signature first, enforces an event-type allowlist, dedups on `payment_id`, and
performs a clinic-scoped booking lookup. **Then, if that lookup fails, it retries
with an explicitly unscoped global query** — `_scoped_query(False)` — matching on
`payment_link_id`, then `notes.booking_id`, then `booking_ref`.

**Exploit.** Per-clinic Razorpay webhook secrets are correctly implemented, so
Tenant A holds a valid secret. A malicious or compromised Tenant A signs a payload
**with its own valid secret** containing `notes.booking_id = <booking UUID of
Tenant B>` or a `booking_ref` from the (pre-T0.2) 9,000-value guessable space.
Signature verification passes — it is the secret of A — and the handler resolves
the booking **globally**. The `pending_payment` booking of Tenant B is confirmed
without payment: the unique-index slot is consumed, a confirmation WhatsApp goes
to the patient of B, and the reconciliation of B shows a confirmed appointment
with no matching settlement.

---

### T0.5a — Delete the fallback

**File:** `app/services/payment.py`

Locate it:

```bash
grep -n "_scoped_query(False)" app/services/payment.py
```

Delete the entire unscoped chain and replace with:

```python
        if not booking:
            # Do NOT widen the search (Rule 5). A tenant holding its own valid
            # webhook secret could previously confirm the booking of ANOTHER
            # tenant by supplying notes.booking_id or a guessable booking_ref
            # (KRIYA-005, amplified by the pre-T0.2 9,000-value reference space).
            #
            # Every candidate lookup must carry the clinic_id resolved from THIS
            # webhook's own routing / secret.
            logger.error(
                f"UNMATCHED_PAYMENT clinic_id={clinic_id} payment_id={payment_id} "
                f"link_id={payment_link_id} — no clinic-scoped booking found"
            )
            try:
                await send_admin_alert(
                    clinic_id,
                    f"Payment received with no matching booking\n\n"
                    f"Payment ID: {payment_id}\n"
                    f"Requires manual reconciliation.",
                )
            except Exception as alert_err:
                logger.error(f"Failed to alert on unmatched payment: {alert_err}")

            # Return 200. Razorpay retries non-2xx, and retrying will never make
            # an unmatched payment match. The alert is the recovery path.
            return {"status": "unmatched", "http_status": 200}
```

Verify `send_admin_alert` is importable here; if not, import it from wherever
`connectors/runner.py` gets it.

**Keep every clinic-scoped lookup.** Each must carry `.eq("clinic_id", clinic_id)`
where `clinic_id` comes from the per-clinic webhook secret that already
authenticated the request.

**Also in this file (KRIYA-005 tail):** if T0.2b has not already done it, replace
the `"duplicate" or "unique" or "violates"` mapping at line ~100 with
`is_slot_conflict(e)`.

### T0.5 · Rollout

Deploy directly. **Monitor `UNMATCHED_PAYMENT` closely for 72h.** If it fires for
legitimate payments, the clinic resolution upstream of the lookup is wrong — fix
*that*, do not restore the fallback.

### T0.5 · Rollback

`git revert`. No schema change.

### T0.5 · Acceptance gate

- [ ] `test_cross_tenant_booking_id_forgery_rejected`: the valid signature of
      Tenant A carrying the `notes.booking_id` of Tenant B -> HTTP 200,
      `UNMATCHED_PAYMENT` logged, **the booking `status` of B unchanged**.
- [ ] `test_cross_tenant_booking_ref_forgery_rejected`: same via `booking_ref`.
- [ ] `test_legitimate_payment_still_confirms`: the happy path is untouched and
      the booking moves `pending_payment` -> `confirmed`.
      **This is the most important regression guard in the entire plan** —
      breaking it breaks revenue.
- [ ] `test_duplicate_payment_webhook_is_idempotent`: existing behaviour preserved.
- [ ] `test_unmatched_payment_returns_200`: assert 2xx so Razorpay does not
      retry-storm.
- [ ] Baseline suite unchanged.

---

## T0.6 · Staging inherits production guards (KRIYA-006) · P0

**Severity:** P0 — unauthenticated webhook injection plus default credentials on
an auto-deployed environment
**Domains moved:** #12 58->92, #16 55->90
**Touch-list:**

- `app/main.py`
- `app/routers/webhook.py`
- `app/config.py`

**Estimated:** 0.5 day

### Problem statement

`render.yaml:22-30` gives `mediassist-ai-staging` `autoDeploy: true` on branch
`staging`. **Every hardening control in `app/main.py:94-132` is inside
`if settings.app_env == "production":`**, so staging skips all of it:

- placeholder-secret boot refusal (`META_APP_SECRET`, `ADMIN_PASSWORD` in
  `("admin","admin123","password","")`, `OWNER_PASSWORD`, `INTEGRATION_SECRET`,
  `CALLMEDEX_BEARER_TOKEN`)
- the 046/047/048 schema pre-flight
- `/docs` and `/redoc` suppression

And `/webhook/test`, which **bypasses HMAC verification**, is blocked only when
`app_env == "production"` — so it is live on staging.

`app/config.py` ships `admin_username: str = "admin"` and
`admin_password: str = "admin"` as defaults. `admin_username` is never validated,
even in production.

---

### T0.6 — Changes

**1. `app/main.py:94`** — widen the guard:

```python
# Before
if settings.app_env == "production":
# After
if settings.app_env != "development":
```

Read the whole block first and confirm nothing inside it is genuinely
production-only (for example a production-only external integration). If something
is, split it into a separate `== "production"` block rather than weakening the
widened guard.

**2. `app/main.py`** — add `admin_username` to the placeholder check, inside the
same block:

```python
        if settings.admin_username in ("admin", "administrator", "root", ""):
            raise RuntimeError(
                "ADMIN_USERNAME is a placeholder value — refusing to start"
            )
```

**3. `app/routers/webhook.py`** — delete `/webhook/test` from the application. Its
HMAC-bypass behaviour belongs in a pytest fixture, not a deployed route.

```bash
grep -rn "webhook/test" app/routers/webhook.py tests/
```

If a test depends on it, replace that dependency with a direct call to the handler
plus a correctly-signed body. If the route absolutely must survive, gate it on
`app_env == "development"` **and** require a header matching a non-defaultable
secret — but deletion is strongly preferred.

**4. `app/main.py:206`** — `@app.get("/metrics")` has no auth dependency. That is
T3.1; **pull it forward into this commit** since it is the same file and the same
review.

---

### T0.6 · BLOCKING MANUAL VERIFICATION

**Confirm that `mediassist-ai-staging` uses a separate Supabase project from
production.**

`render.yaml` does not express this and the repository cannot answer it. **This is
the single highest-uncertainty item in the entire audit.**

- Staging has **separate** credentials -> T0.6 is a hardening task. Proceed.
- Staging **shares** the production database -> T0.6 is not a hardening task, it
  is an **active incident**. Stop, take staging offline, escalate before
  continuing with anything else in this plan.

Record the evidence (Render environment variable group names and the Supabase
project refs, redacted) in the deployment runbook.

### T0.6 · Rollback

`git revert`. If staging then fails to boot, that is the guard working — supply
real secrets rather than reverting.

### T0.6 · Acceptance gate

- [ ] `APP_ENV=staging ADMIN_PASSWORD=admin` -> application refuses to start.
- [ ] `APP_ENV=staging ADMIN_USERNAME=admin` -> application refuses to start.
- [ ] `APP_ENV=development` with placeholders -> still starts. **Regression guard
      — local dev must not break.**
- [ ] `GET /docs` on staging -> 404.
- [ ] `POST /webhook/test` on staging -> 404.
- [ ] `GET /metrics` unauthenticated -> 401.
- [ ] Documented evidence that staging and production use separate Supabase
      projects.
- [ ] Baseline suite unchanged.

---

# PHASE 1 — DISTRIBUTED CORRECTNESS

**Depends on:** Phase 0 complete. **Target:** domain #10 58->92.

---

## T1.1 · Real distributed lock (KRIYA-008) · P1

**Touch-list:** `app/services/distributed_lock.py`, `app/services/scheduler.py`

### Problem statement

`migrations/048_scheduler_locks.sql` defines a **correct** atomic acquire:

```sql
INSERT INTO public.scheduler_locks (job_name, locked_by, locked_at, expires_at)
VALUES (...)
ON CONFLICT (job_name) DO UPDATE
SET locked_by = p_locked_by, locked_at = NOW(), expires_at = NOW() + (...)
WHERE public.scheduler_locks.expires_at < NOW();
```

`acquire_scheduler_lock` and `release_scheduler_lock` are **never called from
Python**. Verify:

```bash
grep -rn "acquire_scheduler_lock\|release_scheduler_lock" app/ connectors/
```

What actually runs (`app/services/distributed_lock.py`, 119 lines) is a Python
read-modify-write: insert -> catch conflict -> select -> **compare expiry as an
ISO string** (`if exp and exp < now.isoformat()`) -> CAS on
`.eq("locked_by", row["locked_by"])`. The CAS narrows the race but the string
comparison is fragile across timezone-offset and precision variation, and — the
real problem — **there is no lease renewal.** A job that outlives its lease is
taken over and runs concurrently with itself.

Exposure at 4 processes: `recover_pending_inbound_messages` and
`expire_stale_bookings` hold 60s leases; reminder jobs hold 300s leases and
iterate every clinic. At scale these overrun, and the consequence is **patients
receiving duplicate reminder messages** — a Meta quality-rating risk on top of the
annoyance.

> **`test_02` does not cover this.** It issues a raw `INSERT INTO scheduler_locks`
> and never calls `DistributedJobLock.acquire()`. It can never fail on lease
> expiry or takeover. See section 3.1.

### Changes

**1.** Replace `DistributedJobLock.acquire()` with a call to the existing RPC:

```python
    async def acquire(self, job_name: str, lease_seconds: int = 300) -> bool:
        """Atomic acquire via the RPC defined in migration 048.

        The previous Python read-modify-write compared expiry as an ISO STRING
        and had no lease renewal, so a job outliving its lease could be taken
        over and run concurrently across the 4 production processes (KRIYA-008).
        """
        from app.database import supabase

        try:
            res = supabase.rpc(
                "acquire_scheduler_lock",
                {
                    "p_job_name": job_name,
                    "p_locked_by": self.worker_id,
                    "p_lease_seconds": lease_seconds,
                },
            ).execute()
            return bool(res.data)
        except Exception as e:
            # Fail CLOSED: not acquiring means the job is skipped this tick,
            # which is safe. Acquiring on error would allow concurrent runs.
            logger.error(f"Lock acquire failed for {job_name}: {e}")
            return False
```

Verify the exact parameter names and return type of the RPC:

```bash
sed -n '1,80p' migrations/048_scheduler_locks.sql
```

**2.** Add `renew()`:

```python
    async def renew(self, job_name: str, lease_seconds: int = 300) -> bool:
        """Extend the lease. Returns False if the lock was stolen.

        The locked_by predicate is what makes theft detectable.
        """
        from app.database import supabase
        from datetime import datetime, timezone, timedelta

        new_expiry = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat()
        try:
            res = (
                supabase.table("scheduler_locks")
                .update({"expires_at": new_expiry})
                .eq("job_name", job_name)
                .eq("locked_by", self.worker_id)
                .execute()
            )
            return bool(res.data)
        except Exception as e:
            logger.error(f"Lock renew failed for {job_name}: {e}")
            return False
```

**3.** Add the heartbeat to `distributed_job_lock`. **Port the pattern that
already works** in `connectors/runner.py:144` (`renew_connector_lock`) — do not
invent a second one.

```python
@asynccontextmanager
async def distributed_job_lock(job_name: str, lease_seconds: int = 300):
    acquired = await lock_manager.acquire(job_name, lease_seconds)
    if not acquired:
        yield False
        return

    stop = asyncio.Event()

    async def _heartbeat():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=lease_seconds / 3)
                return
            except asyncio.TimeoutError:
                if not await lock_manager.renew(job_name, lease_seconds):
                    logger.error(
                        f"LOCK_STOLEN job={job_name} — another process took the "
                        f"lease; aborting job body"
                    )
                    stop.set()
                    return

    hb = asyncio.create_task(_heartbeat())
    try:
        yield True
    finally:
        stop.set()
        await hb
        await lock_manager.release(job_name)
```

> **Design note.** `stop.set()` on theft signals the heartbeat to exit but does
> not itself interrupt the job body — Python cannot preempt it. If a job must
> abort on theft, it has to poll a shared flag. For the current job set, logging
> `LOCK_STOLEN` plus alerting (T5.3) is sufficient; genuine abort-on-theft is only
> needed if the side effects of a job are non-idempotent. **Assess each of the 19
> jobs and record which ones are. Check reminder-sending first.**

### T1.1 · Acceptance gate

- [ ] `test_scheduler_lock_single_winner_via_application_path`: 8 concurrent real
      calls to **`DistributedJobLock.acquire()`** (not raw SQL — `test_02` already
      does that); exactly 1 winner.
- [ ] `test_lock_renewal_prevents_steal`: a job body runs 3x its lease with the
      heartbeat active; assert no second acquirer succeeds.
- [ ] `test_lock_expires_without_renewal`: no heartbeat, wait past the lease,
      assert a second acquirer **does** succeed. **Liveness guard — the lock must
      not become permanent on crash.**
- [ ] `test_lock_steal_is_detected`: forcibly change `locked_by`, assert `renew()`
      returns `False` and `LOCK_STOLEN` is logged.
- [ ] `test_acquire_fails_closed_on_db_error`: make the RPC raise, assert
      `acquire` returns `False`.
- [ ] Baseline suite unchanged (including `test_02`, which is unaffected).

---

## T1.2 · Lock and bound `cleanup_expired_storage` (KRIYA-013) · P2

**Touch-list:** `connectors/runner.py`

`connectors/runner.py:814-860` has **no distributed lock**, does an **unbounded**
`select` of all 90-day-old reports with a `file_path`, and mass-deletes
`connector_audit_log` — running on all 4 processes concurrently.

1. Wrap the body in `distributed_job_lock("cleanup_expired_storage", lease_seconds=600)`.
2. Add `.limit(500)` to the `lab_reports` select; loop until exhausted or a
   wall-clock budget (for example 5 minutes) expires.
3. Batch the `connector_audit_log` delete the same way.

**Acceptance:** `test_storage_cleanup_is_bounded` (asserts `.limit` present and
the loop terminates); `test_storage_cleanup_holds_lock` (a second concurrent call
is a no-op).

---

## T1.3 · Bound `expire_stale_bookings` (KRIYA-014) · P2

**Touch-list:** `app/services/payment.py`, a Phase 4 migration

`app/services/payment.py:679` does an unscoped `select("*")` with **no `.limit()`,
every 60 seconds**.

```python
        rows = (
            supabase.table("appointments")
            .select("id, clinic_id, booking_ref, patient_phone, hold_expires_at")
            .eq("status", "pending_payment")
            .lt("hold_expires_at", now_iso)
            .limit(200)
            .execute()
        )
```

Supporting index:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_appointments_stale_holds
    ON appointments (hold_expires_at)
    WHERE status = 'pending_payment';
```

**Acceptance:** `test_expire_stale_bookings_is_bounded`; existing expiry behaviour
tests unchanged.

---

## T1.4 · Consistent fail-closed in the message queue (KRIYA-016) · P2

**Touch-list:** `app/services/message_queue.py`, `app/config.py`

Three functions, three different failure postures:

| Function | Line | On DB error | Correct? |
|---|---:|---|:---:|
| `acquire()` | 440 | returns `False` (fail **closed**) | yes |
| `claim_message()` | 158 | returns `True` (fail **open**) | no |
| `is_processed()` | ~480 | fails open | no |

A Supabase blip during a Meta retry storm produces duplicate processing —
duplicate replies, duplicate booking attempts.

Apply **Rule 4**: add `settings.queue_fail_closed_enforce: bool = False`, log
`MESSAGE_QUEUE_FAIL_OPEN` with a metric in shadow mode so the real rate of DB
blips is measured before the behaviour flips.

> **Interaction with T0.3a:** the return value of `claim_message` is deliberately
> not used as a gate on the hot path, so making it fail closed does not drop
> messages. Confirm this before flipping the flag.

**Acceptance:** `test_claim_message_fails_closed_when_enforced`;
`test_claim_message_shadow_logs_when_not_enforced`;
`test_hot_path_unaffected_by_claim_message_failure`.

---

# PHASE 2 — TENANT ROUTING AND CACHE COHERENCE

## T2.1 · Restrict the sandbox catch-all (KRIYA-012) · P2

**Touch-list:** `app/services/tenant.py`

`app/services/tenant.py:148-169` — strategy 3 routes **any** unrecognized number
to the first `is_sandbox=True` clinic, and runs **before** the multi-tenant safety
guard (strategy 4).

Consequence: a newly-onboarded clinic whose `phone_number_id` mapping is wrong has
the conversations, symptoms and bookings of its patients land in the sandbox
tenant, visible to sandbox admins. Cross-tenant PHI leak via misconfiguration.

```python
        # Sandbox fallback: dev/staging convenience ONLY. In production this
        # routed the patients of a misconfigured tenant — names, symptoms,
        # bookings — into the sandbox clinic where sandbox admins could read
        # them (KRIYA-012).
        if settings.app_env != "production" and clinic_count == 1:
            ...
```

In production, an unresolvable number must **raise and alert**, not silently land
somewhere.

**Acceptance:** `test_sandbox_fallback_disabled_in_production`;
`test_unresolvable_number_alerts_not_routes`;
`test_sandbox_fallback_still_works_in_development` (**regression guard for local
dev**).

---

## T2.2 · Cross-process cache invalidation (KRIYA-011) · P2

**Touch-list:** `app/services/tenant.py`, a Phase 4 migration

Per-process caches with 300s TTL and no cross-process invalidation.
`invalidate_tenant_cache()` clears only the local copy of one worker — 1 of 4. A
suspended, deleted, or reconfigured clinic keeps being served by the other 3 for
up to 5 minutes.

**Option A (recommended).** `ALTER TABLE clinics ADD COLUMN config_version BIGINT
NOT NULL DEFAULT 1;` bumped on every clinic write. The cache stores the version it
was built from and revalidates with one cheap `select config_version` per request.

**Option B (cheaper, weaker).** TTL 30s for `is_active` and `plan`; 300s for
immutable fields.

Either way the requirement is: **clinic deactivation propagates in seconds, not 5
minutes.** That is contractual, not cosmetic.

**Acceptance:** `test_clinic_deactivation_propagates_within_30s`;
`test_cache_still_serves_hot_path_without_extra_query_per_field`.

---

## T2.3 · Cross-process phone serialization (KRIYA-015) · DEFER — DECISION RECORDED

**Recommendation: document the limitation and defer. Do not implement now.**

`message_queue.py:507-560` per-phone locks are `asyncio` locks, therefore
process-local, therefore useless across the 4 production processes.

However `acquire()` already prevents duplicate *processing*. The residual risk is
FSM **interleaving** when one patient sends two messages within milliseconds that
land on different workers.

Adding a Postgres advisory lock to the hottest path in the system, on speculation,
is more likely to cause an outage than the interleaving it prevents.

**Action:** add a comment at the lock site naming the ceiling and the upgrade
path, add an `fsm_interleave_suspected` metric, and **measure in Phase 9**.
Implement only if the metric shows it happening.

---

# PHASE 3 — SECURITY HARDENING

| Task | File | Change | Acceptance gate |
|---|---|---|---|
| **T3.1** `/metrics` auth (KRIYA-009) | `app/main.py:206` | Add `Depends(verify_metrics_token)` — bearer token from a new `settings.metrics_token`, or an IP allowlist. The Prometheus export currently leaks tenant counts, booking volumes, error rates and queue depth to anyone. *(Pulled forward into T0.6.)* | Unauthenticated `GET /metrics` -> 401; authenticated -> 200 with the same body |
| **T3.2** Trust boundary (KRIYA-010) | `Dockerfile`, `app/utils/security.py`, `app/routers/admin.py:161-236` | (a) `--forwarded-allow-ips` pinned to the Render proxy CIDR instead of `'*'` — currently any client can spoof `X-Forwarded-For`, which feeds the rate-limit key. (b) Rate limiter counts **failures only**, not every authenticated request (5/60s on every request means parallel dashboard loads self-429). (c) `PersistentRateLimiter._use_fallback` becomes time-boxed (60s) with a `RATE_LIMITER_DEGRADED` alert instead of permanently sticky. | A spoofed `X-Forwarded-For` does not reset the counter; 10 parallel dashboard loads by one admin do not 429; fallback recovers automatically |
| **T3.3** CSP | `app/utils/security.py`, `admin/index.html` | Remove `'unsafe-inline'` from `script-src`. Move the 144,580-char inline script of `admin/index.html` to a served `.js` file with a nonce or SRI hash. | CSP header contains no `unsafe-inline`; `node --check` passes; admin panel functions end to end |
| **T3.4** Unbounded admin reads | `app/routers/admin.py` | `.limit(2000)` plus pagination on every platform-wide branch. *(The `.limit` part lands in T0.1c; pagination is here.)* | No admin endpoint can return an unbounded result set |

---

# PHASE 4 — DATA MODEL

## T4.1 · `payment_events` tenant column and event idempotency · P3

> **HIGHEST-RISK MIGRATION IN THIS PLAN. Rehearse against a restored production
> snapshot before applying.**

`migrations/008_payments.sql` creates `payment_events` with **no `clinic_id`** and
**no unique event-id key**, protected by a `prevent_payment_event_mutation()`
trigger that blocks `UPDATE` and `DELETE`.

```sql
ALTER TABLE payment_events ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id);
ALTER TABLE payment_events ADD COLUMN IF NOT EXISTS provider_event_id TEXT;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_payment_event_provider_id
    ON payment_events (provider_event_id)
    WHERE provider_event_id IS NOT NULL;
```

**The backfill of `clinic_id` from `appointments` is an `UPDATE`, which the
append-only trigger blocks.** Before writing the backfill:

1. Read `prevent_payment_event_mutation()` in full.
2. Determine whether it blocks all `UPDATE`s or only those touching specific
   columns.
3. If it blocks all: the backfill must `ALTER TABLE payment_events DISABLE TRIGGER
   <name>;` ... backfill in batches ... `ENABLE TRIGGER` — **inside one
   transaction**, in its own migration, batched to avoid a long lock.
4. Rehearse on a restored snapshot. Verify row counts and a checksum before and
   after.

Without `provider_event_id`, a `payment.captured` and a `payment_link.paid` for
the same payment write two ledger rows today.

---

## T4.2 · Drop the duplicate slot index · P3

`idx_unique_active_slot` (migration 008) and `uq_appointment_active_slot`
(migration 043:56-58) have **identical definitions** — two index maintenance costs
on every appointment write.

1. Check `pg_stat_user_indexes` to see which the planner actually uses.
2. Drop the other `CONCURRENTLY`.
3. **`is_slot_conflict()` (T0.2b) matches both names** — leave it matching both,
   which is safer than coupling it to whichever survives.

---

## T4.3 · RLS decision (KRIYA-007) · P1 · LARGEST SINGLE SCORE MOVE (#8 30->85)

Migration `049` already creates the `kriya_app` role and correct policies. Adopt
them:

1. Provision a second Supabase/Postgres connection authenticating as `kriya_app`.
2. Add a per-request `SET LOCAL app.clinic_id = '<uuid>'` in the tenant-scoped DB
   dependency.
3. Route patient-facing traffic (webhook -> conversation -> booking) through it.
   Keep `service_role` **only** for genuinely cross-tenant work: the platform
   router, scheduler sweeps, migrations.
4. **Measure:** any query returning fewer rows under `kriya_app` than under
   `service_role` was relying on a missing filter. That is a bug the RLS layer
   just found for you — fix it, do not add a bypass.

Roughly two weeks. May ship after limited pilot.

**If deferred, domain #8 stays ~40 and the overall ceiling is ~88.** Deferring is
a legitimate business decision. Claiming RLS protection to hospital customers
while deferring is not.

---

## T4.4 · Retention proof · P3

`app/services/data_retention.py` — add tests asserting:

- the DPDP deletion path actually deletes across all 18 `TENANT_OWNED_TABLES`
- NMC 7-year clinical retention is preserved where required
- a deletion request for clinic A does not touch clinic B

Currently unverified.

---

# PHASE 5 — SILENT FAILURES AND OBSERVABILITY

## T5.1 · Triage all 48 `except: pass` blocks

**Not a bulk edit.** Produce a table — file:line, what is swallowed, correct
disposition from {log+metric, propagate, genuinely-ignorable-with-comment} — then
fix in priority order.

Enumerate:

```bash
grep -rn -A1 "except.*:" --include=*.py app/ connectors/ | grep -B1 "pass$"
```

Priority order:

1. **`app/services/message_queue.py:239`** — the DLQ write on the retryable path.
   **The dead-letter queue can silently fail to record a dead letter.** Highest
   priority silent failure in the repository.
2. `app/services/payment.py:255, 1543, 1632` — financial paths.
3. `app/services/conversation.py:3434`, `app/services/lab_reports.py:112` —
   patient-facing.
4. `app/services/ai_engine.py:184`, `app/services/tenant.py:279`,
   `app/services/whatsapp.py:256`, `app/services/data_retention.py:352`.

Every remaining `pass` gets a one-line comment stating why swallowing is correct.
After this task, a bare `pass` with no justification fails review (Rule 8).

## T5.2 · Fix connector delivery accounting

`connectors/runner.py:617-620` — in the `download_report` failure branch:

```python
                    if meta.external_report_id in getattr(connector, "_processed_ids", set()):
                        summary["reports_delivered"] += 1     # <- counts a FAILED download as delivered
                        continue
```

Introduce `reports_skipped_already_processed` and stop inflating
`reports_delivered`. Operators are currently making decisions on a metric that
lies.

## T5.3 · Alert on the new failure modes

A fix whose failure mode is invisible is half a fix.

| Signal | Source task | Severity |
|---|---|---|
| `TENANT_SCOPE_WOULD_DENY` | T0.1a | Warning -> must reach zero before flag flip |
| `UNMATCHED_PAYMENT` | T0.5 | **Page** |
| `message_queue_reaped` | T0.3b | Warning; sustained > 0 means crashes |
| `RECOVERY_UNRECONSTRUCTABLE` | T0.4b | **Page** — a patient message was lost |
| `LOCK_STOLEN` | T1.1 | **Page** |
| `MESSAGE_QUEUE_FAIL_OPEN` | T1.4 | Warning |
| `RATE_LIMITER_DEGRADED` | T3.2 | Warning |
| `booking_ref collision` | T0.2b | **Page** — should be impossible at 32^8 |

Follow the existing convention in `app/services/metrics.py`.

---

# PHASE 6 — TEST DEBT

Target: domain #15 70->92. **Gates domains 1, 4, 6, 10.**

| ID | Test file / name | Note |
|---|---|---|
| **T6.1** | `tests/test_tenant_isolation_matrix.py` — 5 principals x 78 routes, assert status **and zero foreign-clinic rows** | **Highest-value test in the plan.** Nothing today asserts a negative authorization outcome. Gates T0.1. |
| **T6.2** | `test_booking_ref_volume` — 100k references, zero collisions | Replaces the test that ratified the bug. Gates T0.2. |
| **T6.3** | `test_crash_recovery_real_pg` — kill mid-handler, assert exactly-once replay with the original body | No test kills a process today. Gates T0.3 and T0.4. |
| **T6.4** | **Extend** `test_phase_f_real_load_and_failure_injection.py::test_01` to drive `book_appointment()` / `create_booking_with_payment()` rather than raw `INSERT` | The raw-INSERT version proves the index; it cannot catch KRIYA-001. **Do not duplicate `test_01` — extend it.** |
| **T6.5** | `test_cross_tenant_payment_forgery` | Gates T0.5. |
| **T6.6** | **Extend** `test_phase_f...::test_02` to call `DistributedJobLock.acquire()` and to cover lease expiry and takeover | The raw-INSERT version can never fail on the actual defect. Gates T1.1. |
| **T6.7** | `test_fsm_transition_table` — enumerate all 25 states x input classes; assert no state is unreachable and no transition undefined | 25 states verified at `conversation.py:66-91`; transitions not. |
| **T6.8** | `test_prompt_injection_changes_behaviour` — assert the `is_suspicious` flag of `sanitize_user_input` actually alters behaviour, not just logs | Existing tests only assert detection. |
| **T6.9** | Add `pytest-timeout` to `requirements.txt` | Missing. Passing `--timeout=` makes pytest reject the argument and run **nothing** while exiting 0 — this silently produced a fake green run during the audit. |

---

# PHASE 7 — CONTRACT AND DEAD CODE

Breaking changes permitted here, nowhere earlier.

| ID | Change |
|---|---|
| **T7.1** | Delete `app/services/appointment.py` (130 lines, zero call sites in `app/`, old single-tenant signatures). Verify with `grep -rn "appointment_service\." app/` -> no hits. |
| **T7.2** | Delete the `get_available_slots` 2-arg shim that defaults `clinic_id="default"` (`app/database.py:418`). It returns zero booked slots and is a latent double-booking path if any caller regresses to it. |
| **T7.3** | Migration `054`: `ALTER TABLE appointments DROP CONSTRAINT appointments_booking_ref_key;` — the global UNIQUE from migration 001, now that T0.2 has been stable for one full release. **Verify the exact constraint name first** with `\d appointments`. |
| **T7.4** | Audit the ~49 admin routes with no frontend caller. The admin panel calls ~29 distinct paths; `admin.py` defines 78. Each unreferenced route is authenticated attack surface with no user-facing justification. Wire or delete. |

---

# PHASE 8 — LOAD AND CAPACITY

**Domain #17 40->85 only via execution. This cannot be achieved by code changes.**

Existing assets: `loadtest/locustfile.py`, `loadtest/run_load_test.py`,
`tests/test_phase_f_real_load_and_failure_injection.py`. Per section 3.1, what
exists is DB-constraint concurrency evidence, not capacity evidence.

Against a production-shaped dataset at 10 / 100 / 1,000 tenants, measure and
record:

- [ ] `run_all_connectors` wall-time vs poll interval. The `await asyncio.sleep(2)`
      per clinic alone is 200s at 100 connectors, before any work.
- [ ] Reminder job duration vs its 300s lease. **This is what produces duplicate
      patient reminders** (KRIYA-008).
- [ ] `expire_stale_bookings` query time at 100k+ appointments.
- [ ] Tenant/branch cache RSS x 4 processes.
- [ ] p50 / p95 / p99 webhook ack latency vs the 20s deadline from Meta.
- [ ] Sustained inbound message rate at which the `inbound_messages` backlog grows
      monotonically.
- [ ] Soak: 24h at expected peak; watch for memory growth and lock starvation.

**No capacity claim may be made to any customer before this exists.**

---

# PHASE 9 — STAGING VERIFICATION

Human-required. Blocking.

1. [ ] **Staging uses a separate Supabase project from production.** Highest
       uncertainty item in the audit. See T0.6.
2. [ ] Meta WABA account state healthy. Run `whatsapp_doctor` **first** — a
       generic "unknown error, code 1" from Meta means an inactive WABA or
       unverified business, not a code bug.
3. [ ] `lab_report_delivery` template status is **APPROVED**. A healthy WABA still
       delivers nothing without it.
4. [ ] Razorpay **live-mode** smoke test. Previously blocked on missing
       credentials; still outstanding.
5. [ ] Rollback rehearsal: deploy N -> deploy N+1 -> revert to N with migrations
       051 and 052 applied -> confirm zero data loss and a working booking flow.
6. [ ] 48h shadow-mode log review for `TENANT_SCOPE_WOULD_DENY` (T0.1) and
       `MESSAGE_QUEUE_FAIL_OPEN` (T1.4) before flipping either enforcement flag.
7. [ ] Observe `fsm_interleave_suspected` (T2.3) to decide whether cross-process
       phone locking is actually needed.

---

# PHASE 10 — RELEASE GATE

| Gate | Evidence source |
|---|---|
| All 6 Phase 0 acceptance criteria green | CI |
| `test_tenant_isolation_matrix` green | CI |
| Baseline 954 plus ~40 new tests green, zero regressions | CI |
| Phase 8 load results recorded | Document in `docs/audits/` |
| Phase 9 items 1-7 signed off | Human sign-off |
| `tenant_scope_enforce = True` and `queue_fail_closed_enforce = True`, 48h clean | Logs |
| Re-run this forensic audit, score >= 90 | Repeat audit |

---

## SEQUENCING AND DEPENDENCY GRAPH

```
Week 1   PHASE 0   T0.1  T0.2  T0.3+T0.4 (same release)  T0.5  T0.6
                   parallel-safe: 6 engineers, or 1 engineer serial

Week 2   PHASE 1   T1.1 --> T1.2                         PHASE 3 (independent)
                   T1.3  T1.4

Week 3   PHASE 2   T2.1  T2.2  (T2.3 deferred)           PHASE 5

Week 4   PHASE 6   all                                    PHASE 4  T4.1 T4.2 T4.4

Week 5   PHASE 8 (load execution)  +  PHASE 9 (staging verification)

Week 6   PHASE 7 (contract) --> PHASE 10 (release gate)

Later    PHASE 4 T4.3 (RLS adoption, ~2 weeks)  <- ceiling stays 88 until done
```

### Hard ordering constraints

| Constraint | Reason |
|---|---|
| **T0.3 and T0.4 ship in the same release** | The reaper feeds the retry drain, which replays blanks until T0.4 lands |
| **T1.1 before T1.2** | T1.2 uses the fixed lock |
| **T0.2 before T7.3, by one full release** | Rule 3 expand/contract |
| **T4.1 backfill rehearsed on a restored snapshot before applying** | Append-only trigger interaction |
| **T0.6 manual verification before anything else in Phase 0** | If staging shares the production DB, this is an incident, not a plan |
| **T6.1 before flipping `tenant_scope_enforce`** | The matrix is the proof that T0.1 is safe |

---

## WHAT THIS PLAN DOES NOT PROMISE

- **Phases 8 and 9 are execution, not code.** Skipping them caps the honest score
  at ~84 regardless of how much of Phases 0-7 lands.
- **Deferring T4.3 (RLS adoption) caps the score at ~88.**
- Even at 91, the system is ~9 points short of anything anyone should call "100%
  secure", "zero downtime", or "supports millions of users". Those claims remain
  unsupportable until production telemetry exists. Use calibrated language:
  *Verified / Partially verified / Not verified / Requires load testing / Requires
  production telemetry / Architecture appears capable, but capacity is unproven.*
- **T2.3 is deliberately deferred rather than solved.** Adding a distributed lock
  to the hottest path in the system on speculation is more likely to cause an
  outage than the interleaving it would prevent. Measure first (Phase 9 item 7).
- This plan covers the subsystems inspected in the 2026-08-27 audit. **Not
  inspected in depth, and requiring a second pass before launch:**
  `app/integrations/callmedex/**`, `app/services/abdm.py`,
  `app/services/hmis_bridge.py`, `app/services/vector_search.py`,
  `app/services/report_summarizer.py`, `app/services/broadcast.py`,
  `app/routers/fhir.py`, the internals of `connectors/mocdoc/worker.py`, and
  `.github/` CI workflows.

---

## APPENDIX A — FINDING ID INDEX

| ID | Severity | Title | Task |
|---|---|---|---|
| KRIYA-001 | P0 | Booking reference space is 9,000 values/year, globally shared, no retry | T0.2 |
| KRIYA-002 | P0 | Cross-tenant admin access for any row with `clinic_id = NULL` | T0.1 |
| KRIYA-003 | P0 | Messages permanently lost on crash between claim and handling | T0.3 |
| KRIYA-004 | P0 | Recovery sweep replays an empty message and races live traffic | T0.4 |
| KRIYA-005 | P1 | Payment webhook falls back to an unscoped global booking lookup | T0.5 |
| KRIYA-006 | P1 | Staging runs production code paths with every guard disabled | T0.6 |
| KRIYA-007 | P1 | RLS policies exist but are not in the request path | T4.3 |
| KRIYA-008 | P1 | Distributed lock has no lease renewal; the correct RPC is dead code | T1.1 |
| KRIYA-009 | P2 | `/metrics` is unauthenticated | T3.1 |
| KRIYA-010 | P2 | Rate limiter counts successes, spoofable key, sticky fail-open | T3.2 |
| KRIYA-011 | P2 | Per-process caches, no cross-process invalidation | T2.2 |
| KRIYA-012 | P2 | Sandbox catch-all routes unrecognized numbers cross-tenant | T2.1 |
| KRIYA-013 | P2 | `cleanup_expired_storage` unlocked, unbounded, 4x concurrent | T1.2 |
| KRIYA-014 | P2 | `expire_stale_bookings` unscoped `select("*")`, no limit, every 60s | T1.3 |
| KRIYA-015 | P2 | Per-phone asyncio locks are process-local | T2.3 (deferred) |
| KRIYA-016 | P2 | `claim_message` and `is_processed` fail open | T1.4 |

## APPENDIX B — VERIFIED-GOOD CONTROLS (DO NOT REGRESS)

Confirmed by reading implementation. Every task above must leave them intact;
several acceptance gates exist specifically to protect them.

- Webhook HMAC verification fails closed (`app/utils/security.py`), raw body read
  before parse, `hmac.compare_digest`
- `idx_unique_active_slot` partial unique index — DB-enforced anti-double-booking,
  proven under real 50-thread contention
- Payment webhook: signature first, `payment_id` dedup, event-type allowlist,
  atomic CAS confirm `.eq("id", booking_id).eq("status", "pending_payment")`
- `prevent_payment_event_mutation()` trigger — immutable payment ledger
- `scoped_query()` raising `TenantIsolationError` — fail-closed app-layer guard
- `patient_match.py` fail-closed `NEEDS_REVIEW` gate (threshold 0.75), invoked at
  all three lab-report ingestion points (`admin.py:2138`, `integrations.py:161`,
  `connectors/runner.py:571`)
- Clinical firewall on input (`conversation.py:319`) and output
  (`ai_engine.py:919`), unconditional, present in every plan tier
- `sanitize_user_input` at all three Groq entry points
  (`ai_engine.py:679, 779, 875`)
- Connector per-connector DB lock **with active renewal**
  (`connectors/runner.py:101/144/163`)
- `UNIQUE (clinic_id, external_report_id) WHERE external_report_id IS NOT NULL`
  (migration 026) — connector report dedup
- All 78 admin routes carry an auth dependency
- Both admin HTML bundles pass `node --check`
- `scripts/migrate.py` — versioned, lexicographic, idempotent, skip-if-applied
- Real embedded-Postgres test fixtures (`tests/conftest_db.py` via `pgserver`),
  applying all 52 migrations
