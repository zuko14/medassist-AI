# Kriya IPD OS — Rollout, Rollback and Recovery

## 1. Pre-deployment checks (all must pass; tick in [status.md](status.md))
1. Full suite green on the release commit (`.venv/Scripts/python.exe -m pytest -q`), including
   `tests/test_migration_111_ipd.py` and `tests/test_ipd_api_e2e.py` (real Postgres).
2. Staging (Supabase branch or a restored copy — **never production**): apply `111_ipd_os_core.sql` with
   `scripts/migrate.py`, run `migrations/verify_supabase_schema.sql`, then re-run the E2E suite against it with
   `TEST_DATABASE_URL=<staging url>` (the fixture honours it).
3. Confirm production Postgres ≥ 15 (`SELECT version();` — recorded as 16.2 in the OPD status) and that the
   Supabase PostgREST exposes `rpc/ipd_*` to `service_role` only (`REVOKE` in §15 of the migration).
4. Back up: `pg_dump "$DATABASE_URL" -Fc -f pre_111_$(date +%F).dump`.

## 2. Deployment order
1. **Schema first** (off-peak, before 08:00 IST): `scripts/migrate.py` applies 111 in one transaction. It is
   additive; running pre-111 code against it is safe.
2. **Code second**: deploy the commit containing `app/routers/ipd.py`. Lifespan parity check passes once 111 is applied.
3. **No clinic changes.** Every clinic stays `ipd_state = OFF` until the owner enables one.

## 3. Pilot sequence
| Step | Who | Gate to continue |
|---|---|---|
| P0 | Sandbox clinic on production (no real patients): owner enables IPD; walk W1–W8 of [WORKFLOWS.md](WORKFLOWS.md) with test data; desktop + tablet | all workflows complete; no 5xx in logs |
| P1 | Clinical sign-off: a doctor, a nurse and a billing person from the pilot hospital review the screens and the bed-day rule (O-1), waiver rule (O-2), admin read access (O-3), consent (O-4), no alerts (O-5) | written acceptance of open decisions |
| P2 | One pilot hospital, one ward, ≤ 20 beds, paper backup in parallel for 2 weeks | daily reconciliation: bed board vs ward census; bills vs posted charges |
| P3 | All wards of the pilot hospital | 2 weeks without a data-integrity incident |
| P4 | Next tenants, on request, one at a time | — |

## 4. Monitoring (existing log pipeline)
- `IPD_DB_ERROR type=…` (service: unmapped database error — investigate every one).
- HTTP 5xx on `/admin/ipd/*`; 409 rates on `ipd_bed_unavailable` / `ipd_stale_bed` (contention signal, not an error).
- Daily SQL checks (read-only):
  ```sql
  -- every occupied bed has exactly one matching active stay, and vice versa (expect 0 rows)
  SELECT b.id FROM ipd_beds b LEFT JOIN ipd_admissions a ON a.id = b.current_admission_id
   WHERE b.status = 'occupied' AND (a.id IS NULL OR a.current_bed_id <> b.id OR a.status NOT IN ('admitted','discharge_pending'));
  SELECT a.id FROM ipd_admissions a LEFT JOIN ipd_beds b ON b.id = a.current_bed_id
   WHERE a.status IN ('admitted','discharge_pending') AND (b.id IS NULL OR b.current_admission_id <> a.id);
  -- bills reconcile to posted charges (expect 0 rows)
  SELECT i.id FROM opd_invoices i JOIN (SELECT invoice_id, SUM(total_paise) t FROM ipd_charges WHERE invoice_id IS NOT NULL GROUP BY 1) c
    ON c.invoice_id = i.id WHERE i.subtotal_paise <> c.t;
  ```

## 5. Rollback (choose the lowest level that resolves the incident)
**R1 — per clinic (seconds).** Owner console → clinic → untick *IPD OS*. With patients admitted it enters
WIND_DOWN after confirmation (care continues, no new admissions); otherwise OFF. Nothing is deleted.

**R2 — code revert (minutes).** Redeploy the previous release. Migration 111 is additive; pre-111 code ignores
the IPD tables. IPD final bills remain ordinary OPD invoices and can still be issued/paid in OPD Billing.
In-flight admissions are frozen (no IPD screens) until the code is redeployed — use R1 first where possible.

**R3 — schema rollback (destructive; owner approval in writing).** Only if 111 itself breaks pre-111 behaviour.
Rollbacks run **newest first**: with 111 applied, `103_down.sql` must never run before `111_down.sql`
(111 depends on OPD functions and tables; Postgres refuses, and a `CASCADE` would silently strip IPD constraints).
1. R1 for every clinic; discharge or cancel every active stay (`111_down.sql` refuses otherwise).
2. `pg_dump "$DATABASE_URL" -t 'ipd_*' -Fc -f ipd_backup_$(date +%F).dump`
3. `psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f migrations/rollback/111_down.sql`
4. `DELETE FROM schema_migrations WHERE name = '111_ipd_os_core.sql';` only after the reverted code is live.

## 6. Conditions that block rollout
- Any failing test on the release commit, or E2E not re-run against staging.
- Any non-zero result from the §4 reconciliation queries during P0–P2.
- Pilot hospital has not accepted the open owner decisions O-1…O-5.
- A pilot hospital that needs deposits/advances, TPA claims, packages or pharmacy dispensing on day one (DEFERRED).
