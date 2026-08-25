# Staging Environment & Release Promotion Workflow (W6.5)

## 1. Overview
The Kriya AI deployment architecture enforces a strict single-branch staging gate before any code or database migration reaches production.

Topology:
- **Staging Service (`mediassist-ai-staging`)**: Deployed from branch `staging` with 1 worker container, wired to isolated staging database with automatic migrations on deploy.
- **Production Service (`mediassist-ai`)**: Deployed from branch `main` with 2 worker containers (`numInstances: 2`), wired to production cluster with zero-downtime rolling restart.

```
PR (Feature Branch)
  │
  ▼
GitHub CI (pytest, real PostgreSQL invariants, query lint)
  │
  ▼
Merge to `staging`
  │
  ├──► Automated Pre-Deploy: `python scripts/migrate.py`
  └──► Staging Smoke Verification (`tests/test_staging_smoke.py`)
        │
        ▼
Merge to `main`
  │
  ├──► Production Pre-Deploy: `python scripts/migrate.py`
  └──► Rolling Deployment to Production (`numInstances: 2`)
```

---

## 2. Migration Dry-Run Procedure
Before merging schema changes:
1. Run `python scripts/migrate.py --dry-run` against staging database connection to inspect pending migrations and validate checksum consistency.
2. Apply migration to staging DB and verify all invariant test suites pass.
3. Validate application health check `/health/ready` on staging.

---

## 3. Promotion Checklist
- [ ] CI suite (900+ tests) passing with 0 failures.
- [ ] CI query scoping linter passed (`pytest tests/test_lint_unscoped_queries.py -q`).
- [ ] PostgreSQL invariant suite passed (`pytest tests/test_real_postgres_invariants.py -q`).
- [ ] Staging health verified at `https://mediassist-ai-staging.onrender.com/health`.
- [ ] Admin dashboard operational on staging.
- [ ] Approved for production release.
