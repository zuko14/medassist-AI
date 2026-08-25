# Production Rollback & Emergency Recovery Procedure (W6.6)

**Target System:** Kriya AI / MediAssist AI  
**Authoritative Runbook:** W6.6 Rollback Rehearsal & Verification  
**Last Updated:** 2026-08-25  

---

## 1. Trigger Conditions for Immediate Rollback
A production rollback must be initiated immediately if any of the following occur within 15 minutes of deployment:
1. **Health / Readiness Probe Failure:** `/health/ready` returns HTTP 500 or `database: disconnected` for >60 seconds.
2. **Elevated Webhook Error Rate:** Meta webhook endpoint (`/webhook`) error rate exceeds 2.0%.
3. **Database Transaction Contention Spike:** PostgreSQL connection pool saturation or deadlocks detected in logs.
4. **Tenant Context Leak:** Any 403 / cross-tenant boundary assertion failure.

---

## 2. Fast Rollback Execution Path (Container Compute)
To rollback to the previously verified stable Git commit:

```bash
# Step 1: Identify the previous known good Git SHA
PREV_SHA="c265691"  # Replace with stable release SHA

# Step 2: Trigger instant rolling rollback via Render CLI or Git
git revert --no-edit HEAD
git push origin main

# Step 3: Trigger automated redeployment with pre-deploy migrations check
render deploys create srv-mediassist-ai --commit $PREV_SHA
```

---

## 3. Database Schema Rollback Strategy (Forward-Compatible & Non-Destructive)
All Kriya AI migrations are engineered to be **forward-compatible** and **non-destructive**:
- **Additive Migrations Only:** New tables, columns, indexes, and RLS policies do not drop or mutate existing operational columns.
- **Rollback Safety:** If a new container version is rolled back to an older image, the older application code ignores the newly added columns/tables without failing.
- **RLS Safety:** `migrations/049_force_row_level_security.sql` preserves `service_role` full permissions (`USING (true)`), ensuring backward-compatible access for legacy workers.

---

## 4. Post-Rollback Verification Checklist
- [ ] Verify `/health` returns `HTTP 200 {"status": "ok"}` across all worker PIDs.
- [ ] Verify `/health/ready` returns `HTTP 200 {"status": "ready", "database": "connected"}`.
- [ ] Run invariant smoke test: `pytest tests/test_real_postgres_invariants.py -q`.
- [ ] Send test WhatsApp message to test hospital number and verify automated reply within <3 seconds.
- [ ] Confirm zero pending dead-letter messages in `failed_messages`.
