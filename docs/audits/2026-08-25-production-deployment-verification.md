# Kriya AI — Production Deployment & Release Verification Report

**Audit Date:** 2026-08-25  
**Evaluated Systems:** Infrastructure as Code (`render.yaml`), Database Migrations (`scripts/migrate.py`), Startup Invariants (`app/main.py`), and Rollback SOP.

---

## 1. Startup Schema Invariant Protection (W6.1)
- **Implementation:** [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/hospital-bot/app/main.py) lifespan hook.
- **Enforced Tables & Columns:**
  - `inbound_messages` (Durable ingestion queue)
  - `scheduler_locks` (Distributed scheduler advisory locks)
  - `appointments.refund_id` (Idempotent payment refund column)
- **Behavior:** Fails closed loudly and halts server boot if any table or column is missing from PostgreSQL, preventing silent runtime downgrades.

---

## 2. Cryptographic Migration Verification (W6.2, W6.3)
- **Migration Runner:** [`scripts/migrate.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/hospital-bot/scripts/migrate.py).
- **Tracking Table:** `schema_migrations` tracking `version`, `name`, `checksum` (SHA-256), and `applied_at`.
- **Integrity Assertion:**
  - Detects if an already-applied migration file is modified or tampered with.
  - Prevents partial execution of migration batches.
- **Pre-Deploy Execution:** [`render.yaml`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/hospital-bot/render.yaml) specifies `preDeployCommand: python scripts/migrate.py`.

---

## 3. CI/CD Release Promotion & Gating (W6.4, W6.5)
- **Auto-Deploy Policy:** Configured to `autoDeploy: false` in `render.yaml` to ensure unverified commits cannot deploy to production without passing full CI gates.
- **Worker Configuration:** Dedicated `mediassist-connector-worker` declared alongside `mediassist-ai` web service for scheduled connector polling.

---

## 4. Production Rollback Procedure (W6.6)
- **SOP Document:** [`docs/operations/rollback-procedure.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/hospital-bot/docs/operations/rollback-procedure.md).
- **Guarantees:**
  - Forward-only additive migrations guarantee that reverting Docker container images never causes database schema crashes.
  - In-flight webhooks remain in `inbound_messages` and are picked up by `recover_pending_inbound_messages()`.
  - Target SLA for container rollback is < 180 seconds.
