# Kriya AI — 95+ Execution Baseline Snapshot

**Date:** 2026-08-25  
**Author:** Principal Implementation Engineer (Antigravity Agent)  
**Document Type:** Pre-Execution Forensic & Operational Baseline  
**Authority:** `docs/audits/ANTIGRAVITY-PROMPT-95-plan-completion.md`

---

## 1. System & Git Metadata

| Parameter | Value |
|---|---|
| **Git Commit SHA** | `13b93d83034ab9a9beffff71f7c9dec86a5d2ebf` |
| **Git Branch** | `main` (in sync with `origin/main`) |
| **Working Tree State** | Clean (0 modified tracked files; prompt doc untracked) |
| **Python Runtime** | Python 3.12.3 / Windows 11 (Container target: Python 3.11-slim) |
| **Live Database Target** | Supabase PostgreSQL `fvibyvfnjtztxetnemyd` |
| **Local Verification DB** | Real PostgreSQL via `pgserver` + `psycopg2` |

---

## 2. Test Suite Baseline Measurements

| Test Target | Command | Result | Timing |
|---|---|---|---|
| **Core & Invariant Suite** | `pytest -q` | **902 passed, 1 skipped** | 128.58s |
| **CallMedex Integration Suite** | `python -m pytest app/integrations/callmedex/tests/ -q` | **71 passed, 1 skipped** | 39.91s |
| **Combined Test Total** | Total suite execution | **973 passing, 2 skipped, 0 failing** | 168.49s |

---

## 3. Database Schema & Migration Baseline

- **Total SQL Migration Files:** 48 migrations (`001_initial_schema.sql` through `048_scheduler_locks.sql`)
- **`FORCE ROW LEVEL SECURITY` Occurrences:** `0` across all migrations.
- **Migration Tracking Table:** `schema_migrations` present with 3 placeholder entries (`046_hash`, `047_hash`, `048_hash`).

---

## 4. Codebase Vulnerability & Defect Inventory

| ID | Issue Description | Location | Measured Baseline Value |
|---|---|---|---|
| **W1.1** | Raw unannotated `supabase.table(` calls | `app/routers/**` | **170 unannotated call sites** |
| **W1.2** | CI query lint preventing unscoped regressions | `tests/` | **Missing** |
| **W2** | Database-level tenant RLS enforcement | `migrations/` & `app/services/` | **0 `FORCE ROW LEVEL SECURITY`**, `TenantScopedClient` has **0 usages** |
| **W4.1** | Multi-instance concurrency configuration | `Dockerfile`, `render.yaml`, `app/services/tenant.py` | `Dockerfile` missing `--workers 2`; `render.yaml` missing `numInstances: 2`; `tenant.py` has duplicate `_branch_cache` declaration (lines 17 & 447) |
| **W3.5** | Mock-backed tests reporting fake latency percentiles | `tests/test_phase_k_load_and_stress.py`, `tests/test_phase_f_real_load_and_failure_injection.py` | 4 tests in Phase K + 2 tests in Phase F measure `MagicMock` dispatch and report p50/p95/p99 |
| **W6.5 / W3.1** | False claim of measured baseline in capacity model | `docs/audits/capacity-model.md` | Labeled "Measured Performance Baselines" without run artifacts |
| **W5.4** | Proactive failure alerting | `app/services/metrics.py`, `app/services/scheduler.py` | Alerts not verified firing on 6 critical production failure modes (including ingest-acquire deadlock invariant) |
| **W1.3** | Adversarial cross-tenant route matrix | `tests/test_phase2_route_adversarial_matrix.py` | Covers only admin subset; must cover full route surface |
| **W8.2–W8.5** | Auth hardening & account lockout | `app/routers/admin.py`, `app/services/permissions.py` | HTTP Basic only, no lockout mechanism, no session tokens |
| **W10.2** | Clinical AI adversarial summarization tests | `tests/` | Lacks adversarial negation dropping and abnormal inversion test suite |
| **Data Fix** | TestHospital whatsapp_number misconfiguration | `clinics` table | Needs assertion and fix |

---

## 5. Domain Baseline Score Breakdown (Starting Score: 71 / 100)

1. Multi-tenant isolation: **72/100**
2. AuthN / AuthZ: **70/100**
3. Payment integrity: **88/100**
4. Booking concurrency: **90/100**
5. WhatsApp reliability: **85/100**
6. Connector reliability: **82/100**
7. Wrong-patient prevention: **82/100**
8. Database design: **78/100**
9. RLS / DB-level security: **25/100**
10. Silent-failure resistance: **70/100**
11. Observability: **40/100**
12. AI safety: **70/100**
13. Privacy & data lifecycle: **75/100**
14. Frontend ↔ backend wiring: **65/100**
15. Scalability: **55/100**
16. Deployment & release: **62/100**
17. Test quality: **80/100**
18. Failure recovery: **82/100**

**Starting Overall Score: 71.0 / 100**
