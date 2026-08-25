# Antigravity Agent Prompt — Complete the Kriya AI 95+ Production Plan

> Copy everything below the line into the Antigravity agent. It is self-contained.

---

You are working on **Kriya AI / MediAssist AI v2.0.0** — a multi-tenant healthcare WhatsApp assistant
(Python 3.11, FastAPI, Supabase PostgreSQL, Meta WhatsApp Cloud API, Razorpay, APScheduler, Playwright,
deployed on Render). Repo root contains `app/`, `connectors/`, `migrations/`, `tests/`, `docs/`.

Your job is to finish the remaining work in `docs/audits/2026-08-25-production-score-and-95-plan.md`.
Current independently verified score: **75/100**. Target: **95+**.

## CRITICAL CONTEXT — READ BEFORE WRITING ANY CODE

Three prior remediation rounds reported "98/100", "99.5/100", and "APPROVED FOR PRODUCTION". Independent
verification found each of those claims unsupported. The specific failure patterns that occurred, which
you must not repeat:

1. **Dead code counted as done.** Round 1 shipped `scoped_query()` with **zero** call sites plus a green
   test file that exercised the helper in isolation. Round 3 shipped `TenantScopedClient` the same way —
   it still has **zero** usages today.
2. **Mocked tests reporting real-world numbers.** `tests/test_phase_k_load_and_stress.py` patches
   `supabase.table` with `MagicMock` and reports "p95 < 1.0ms". It measures coroutine dispatch.
3. **Fabricated measurements.** `docs/audits/capacity-model.md` is headed *"Measured Performance
   Baselines (Locust / Staging Benchmark)"* with a full throughput/latency table. There is no staging
   service in `render.yaml`, no Locust run artifact anywhere in the repo, and no host, date, or duration
   cited. Those numbers were never measured.
4. **A green suite over a dead product.** 899 tests passed while **100% of inbound WhatsApp messages were
   silently dropped in production** — `message_queue.ingest()` pre-wrote the `processed_messages` row that
   `message_queue.acquire()` uses to claim a message, so `acquire()` rejected every message as its own
   duplicate, `process_message()` returned at the guard, and the row was still marked `completed` with
   `attempt_count=0` and `last_error=None`. Two separate tests covered `ingest()` and `acquire()` — each
   with its own mock — so neither could observe the interaction. That bug is now fixed
   (`tests/test_regression_ingest_acquire_deadlock.py`).

**Therefore: a task is done when its acceptance command produces the required output, not when you
believe it is done. Run the command. Paste the real output. If it fails, the task is not done.**

## ABSOLUTE PROHIBITIONS

- **Never** add a module, class, or helper without wiring it into a real call path in the same change.
  If `grep -rn "<YourNewThing>" app/ | grep -v "<its own file>"` returns 0, the task is incomplete.
- **Never** let a test that patches `supabase`, `httpx`, or any I/O boundary report a latency, throughput,
  or memory figure. Such a test may assert correctness only.
- **Never** write "measured", "benchmark", or "verified" about a number unless a committed run artifact
  (CSV/HTML/log) with a timestamp and target host exists in the repo.
- **Never** report a summary score, "production ready", or "all blockers closed". Report per-task
  acceptance output only. A human makes the readiness call.
- **Never** delete or weaken an existing test to make a suite pass. If a test blocks you, explain why.
- Do not touch `docs/audits/2026-08-25-production-readiness-forensic-audit.md` or any existing audit
  report — they are the historical record.

## ALREADY IMPLEMENTED — DO NOT REDO (independently verified)

These are confirmed present and working. Leave them alone except where a task below explicitly extends them.

| Area | Evidence |
|---|---|
| P0-1 refund status/columns | `payment.py:592-596` writes `refunded` + `refund_reason` + `refunded_at`; `migrations/046` applied to live DB |
| P0-2 lab-report resend IDOR | `lab_reports.py:495-514` clinic-scoped; `admin.py:2098-2112` passes `user.clinic_id`, maps `ValueError`→404 |
| P0-3 refund IDOR + idempotency | `admin.py` uses `enforce_clinic_access` + per-clinic Razorpay creds; `payment.py:871` key is `ref_{booking_id}_{payment_id}` |
| P0-4 patient-match fail-closed | `patient_match.py:143-155` returns `needs_review` / `is_safe_to_send=False` on DB error |
| P1-1 real PDF validation | `app/utils/pdf_reader.py:validate_pdf_report()`; `pdfplumber==0.10.3` in requirements |
| P1-4 processing order | `conversation.py:222-230` persists `last_processed_message_id` only after success |
| P1-6 durable inbound queue | `migrations/047`, `inbound_messages` table, `webhook.py` persists before dispatch, `scheduler.recover_pending_inbound_messages` reclaims expired leases |
| P2-5 scheduler distributed locks | `migrations/048`, `app/services/distributed_lock.py`, 13 wiring sites in `scheduler.py`, fails closed on DB error |
| Ingest/acquire deadlock | fixed; `tests/test_regression_ingest_acquire_deadlock.py` passes |
| W1.4 intake gating | `patient_match_service` reached from all 3 intake paths incl. admin manual upload |
| W6.1 startup schema assertion | `app/main.py` fails boot if `inbound_messages`, `scheduler_locks`, `appointments.refund_id` are absent |
| W6.3 / W6.4 | `render.yaml` has `preDeployCommand: python scripts/migrate.py` and `autoDeploy: false` |
| W7.1 / W7.2 | `ingest()` table-missing fallback removed; `PYTEST_CURRENT_TEST` bypass removed from `distributed_lock.py` |
| W8.1 | plaintext fallback removed from `check_password_hash` |
| W5.1–W5.3 | `app/utils/correlation.py`, `app/services/metrics.py`, `/metrics` wired in `main.py` |
| Real-PostgreSQL invariants | `tests/test_real_postgres_invariants.py`, 16 invariants incl. 50-thread slot race and scheduler lock takeover, via `pgserver` + `psycopg2` |

Baseline: `pytest -q` → **902 passed, 1 skipped**. `pytest app/integrations/callmedex/tests/ -q` → **71
passed, 1 skipped**. Do not let either regress.

## PENDING WORK

Execute in this order. Each task lists its acceptance command. **Run it and paste real output.**

---

### TASK 1 — W1.1: Scope every raw query in the routers

**Current:** `app/routers/admin.py` has **112** raw `supabase.table(` calls; only **8** carry an
`# unscoped:` annotation. Multi-tenant isolation currently has no database backstop (see Task 3), so every
unscoped call is the only thing standing between tenants.

**Do:** For each raw `supabase.table(...)` call in `app/routers/**`, either
- route it through `scoped_query(table_name, clinic_id, select_fields)` from `app/database.py`, or
- if it legitimately must be unscoped (platform-owner operations, tenant-resolution bootstrap), add a
  comment on the line above: `# unscoped: <specific reason>`.

Do not blanket-annotate to satisfy the count. An annotation that says `# unscoped: needed` is a failure.

**Acceptance:**
```bash
python - <<'PY'
import pathlib
bad = []
for f in pathlib.Path("app/routers").rglob("*.py"):
    lines = f.read_text(encoding="utf-8").splitlines()
    for i, l in enumerate(lines):
        if "supabase.table(" in l:
            prev = lines[i-1] if i else ""
            if "# unscoped:" not in prev and "# unscoped:" not in l:
                bad.append(f"{f}:{i+1}: {l.strip()[:90]}")
print(f"UNANNOTATED RAW CALLS: {len(bad)}")
for b in bad[:20]: print("  ", b)
PY
```
Required output: `UNANNOTATED RAW CALLS: 0`

---

### TASK 2 — W1.2: CI lint so Task 1 cannot regress

**Do:** Add `tests/test_lint_unscoped_queries.py` implementing the check above as a pytest that fails on
any unannotated raw call in `app/routers/**`.

**Acceptance:** Add a raw `supabase.table("patients").select("*")` line to any router, run
`pytest tests/test_lint_unscoped_queries.py -q`, confirm it **FAILS**, then remove the line and confirm it
passes. Paste both outputs.

---

### TASK 3 — W2: Database-level tenant backstop (highest point value: 25 → 95)

**Current:** `FORCE ROW LEVEL SECURITY` appears **0** times across 48 migrations. The app connects with
the Supabase `service_role` key, which holds `BYPASSRLS`, so all 38 existing RLS policies are inert for
every query the application makes. `app/services/tenant_scoped_client.py` exists but has **0 usages**.

Choose **Option A or Option B and complete it.** Do not partially do both.

**Option A — make Python scoping structurally unbypassable (~1 week, ceiling ≈90):**
Wire `TenantScopedClient` into every tenant-owned table access so a missing `clinic_id` raises at call
time rather than silently returning cross-tenant rows. Delete the class if you do not wire it — an unused
wrapper is worse than none, because it reads as protection that does not exist.

**Option B — real defense in depth (~3 weeks, ceiling 95, RECOMMENDED):**
1. New migration: create an application role **without** `BYPASSRLS`.
2. `ALTER TABLE <t> FORCE ROW LEVEL SECURITY` on all tenant-owned tables.
3. Policies keyed on a per-request setting; set `SET LOCAL app.clinic_id = '<uuid>'` at request scope.
4. Move application traffic to that role; reserve `service_role` for migrations and platform-owner ops.

**Acceptance (Option B) — must run against real PostgreSQL:**
Add a test in `tests/test_real_postgres_invariants.py` that, connected **as the application role**, runs a
query deliberately omitting `clinic_id` and asserts **zero** rows from another tenant are returned — i.e.
the database refuses even when Python forgets.
```bash
pytest tests/test_real_postgres_invariants.py -q
grep -rl "FORCE ROW LEVEL SECURITY" migrations/ | wc -l   # must be > 0
```

**Acceptance (Option A):**
```bash
grep -rn "TenantScopedClient\|tenant_scoped_client" app/ --include=*.py \
  | grep -v "app/services/tenant_scoped_client.py" | wc -l    # must be > 20
```
plus a test proving a call omitting `clinic_id` raises.

---

### TASK 4 — W4.1: Enable and prove multi-instance operation

**Current:** `Dockerfile` has no `--workers`; `render.yaml` has no `numInstances`. The distributed locks
built in a previous round are therefore never exercised — the system runs as a single process, and the
locks are dead weight until this changes.

**Do:**
1. `Dockerfile`: add `--workers 2` to the uvicorn CMD (keep `--proxy-headers --forwarded-allow-ips='*'`).
2. `render.yaml`: set `numInstances: 2` on the web service.
3. Audit every process-local cache for correctness under N processes: `_tenant_cache`, `_branch_cache`,
   `_holiday_cache` in `app/services/tenant.py`, and the per-phone `asyncio.Lock` dict in
   `app/services/message_queue.py`. Either move to a shared store or document the staleness bound in
   `docs/architecture/multi-instance-cache-semantics.md` and make the TTL match it.
4. Note `app/services/tenant.py` declares `_branch_cache` **twice** (lines ~17 and ~424) — the second
   shadows the first. Fix.

**Acceptance:** with 2 workers running, the real-PostgreSQL invariants still hold:
```bash
pytest tests/test_real_postgres_invariants.py -q
grep -c "workers" Dockerfile        # must be >= 1
grep -c "numInstances" render.yaml  # must be >= 1
```
Plus: kill one worker mid-processing under load and show `recover_pending_inbound_messages` reclaims the
lease with zero message loss. Paste the `inbound_messages` status transitions.

---

### TASK 5 — W3.5: Remove tests that report fake performance numbers

**Do:** Delete `tests/test_phase_k_load_and_stress.py`, and delete or rename the mocked spike/soak tests
inside `tests/test_phase_f_real_load_and_failure_injection.py`
(`test_03_http_spike_test_200_requests`, `test_04_soak_test_10_consecutive_cycles`). They patch
`app.database.supabase.table` with `MagicMock` and report latency percentiles.

**Keep** the genuine tests in that file: `test_01_real_postgres_slot_concurrency_50_threads`,
`test_02_real_postgres_scheduler_locks_concurrency`, `test_05_failure_injection_malformed_webhook_payload`,
`test_06_failure_injection_database_outage_fail_closed`.

If you want to keep a dispatch-overhead microbenchmark, rename it to `*_dispatch_overhead` and add a
docstring stating it measures framework overhead against mocks and says nothing about system capacity.

**Acceptance:**
```bash
grep -rln "p95\|p99\|percentile\|latency" tests/ | xargs grep -ln "MagicMock\|patch("
# expected: no output
pytest -q   # must still be >= 900 passed
```

---

### TASK 6 — W6.5: Staging environment (blocks Tasks 7 and 9)

**Do:** Add a staging service to `render.yaml` (or a separate Render blueprint) pointing at a **separate
Supabase project**. Apply all 48 migrations via `scripts/migrate.py`. Seed 2+ test clinics so
cross-tenant work has real targets. Staging must never share a database with production.

**Acceptance:** paste the staging URL and the output of a `GET /health` and `GET /ready` against it, plus
`scripts/migrate.py` output showing the applied migration count.

---

### TASK 7 — W3.1–W3.4: Real load test (highest point value after Task 3: 55 → 95)

**Current:** `loadtest/locustfile.py` and `loadtest/run_load_test.py` exist but there is no evidence they
were ever run. `docs/audits/capacity-model.md` presents an unmeasured table as "Measured".

**Do FIRST, before any load work:** edit `docs/audits/capacity-model.md` and change the heading
`## 1. Measured Performance Baselines (Locust / Staging Benchmark)` to
`## 1. PROJECTED — NOT MEASURED (superseded once a real run exists)`. Leaving that label in place is a
false evidence claim. This edit is required and takes one minute.

**Then:** run the Locust suite against the **staging URL from Task 6** with a real Supabase project.
Scenarios: webhook ingest ramp; N concurrent bookings on one slot; connector burst; admin dashboard
queries under load. Then a 4-hour soak at 60% of measured p95 capacity.

**Do NOT** run load tests against production.

**Acceptance:**
- Commit the raw run output to `loadtest/results/` (Locust CSV or HTML, with timestamps).
- Rewrite `docs/audits/capacity-model.md` with **measured** p50/p95/p99, throughput, and error rate for
  10 / 100 / 1,000 clinics, each citing the target host, run date, duration, and the artifact filename.
- Soak: RSS flat within 10%, zero unhandled exceptions, DLQ depth returns to 0.

```bash
ls loadtest/results/          # must contain timestamped artifacts
```

---

### TASK 8 — W5.4: Prove alerts fire

**Current:** `metrics.py` and `/metrics` exist, but no alert has been demonstrated firing. A 100% inbound
outage ran undetected in production while every metric reported success — this is the task that would have
caught it.

**Do:** Configure alerting on: refund DB-write failure, DLQ/`dead_letter` depth > 0, NEEDS_REVIEW report
created, scheduler job skipped or overrunning its lease, `fail_closed_count` increasing, and — specifically
— **an `inbound_messages` row reaching `completed` without a corresponding
`conversations.last_processed_message_id` update**, which is the invariant the outage violated.

**Acceptance:** deliberately trigger **each** of the six conditions in staging and paste the resulting
alert notification with its timestamp. An alert that has not been observed firing does not count.

---

### TASK 9 — Remaining items

| ID | Task | Acceptance |
|---|---|---|
| W1.3 | Extend `tests/test_phase2_route_adversarial_matrix.py` from 80 security-sensitive routes to the **full** surface (~103 admin + 26 platform) | test count == route count enumerated from the FastAPI app object; any exemption carries a written reason |
| W6.6 | Rehearse the rollback in `docs/operations/rollback-procedure.md` | paste timed output of one real rollback performed in staging |
| W8.2 | Session tokens with expiry replacing per-request HTTP Basic | test proving a stale token is rejected |
| W8.3 | Per-account lockout + failed-attempt audit | brute-force test locks the account, not the whole clinic |
| W8.4 | MFA for `clinic_admin` and `super_admin` | enforced for any role that can refund or read PHI |
| W9.3 | Browser smoke test of critical admin journeys against staging | runs in CI; **specifically assert no UI action reports success on a failed API call** |
| W10.2 | Adversarial summarization tests: dropped negation, unit errors, inverted abnormal flags | suite fails on a summary that inverts clinical meaning |
| W10.3 | Live MocDoc portal run in staging | one real report delivered to a test patient with the match gate active |
| W10.4 | Razorpay reconciliation against real sandbox data | previously blocked on credentials — unblock or record as an accepted gap with an owner |
| — | `clinics.whatsapp_number` for TestHospital is set to a patient's number (`+917981945956`), so `display_phone` (`15551649189`) matches no clinic and only `phone_number_id` resolution saves it | fix the row; assert `whatsapp_number` is a business number for every clinic |

---

## REPORTING FORMAT

For each task, report exactly:

```
TASK <n> — <name>
STATUS: DONE | PARTIAL | BLOCKED | NOT STARTED
FILES CHANGED: <paths>
ACCEPTANCE COMMAND: <the command you ran>
ACCEPTANCE OUTPUT:
<paste the real terminal output>
NOTES: <anything that did not work, any assumption made>
```

If a task is BLOCKED, say what blocks it and stop — do not substitute a different task and mark the
original done. If you cannot meet an acceptance criterion, report PARTIAL with the real output. **A
truthful PARTIAL is a success. A DONE that verification contradicts is the failure mode this whole
document exists to prevent.**

Do not produce a final score. Do not write "production ready". Report task outcomes; a human decides.
