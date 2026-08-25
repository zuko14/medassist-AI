# Kriya AI — Production Rollback & Recovery Standard Operating Procedure (W6.6)

**Target SLA:** Complete traffic rollback within < 180 seconds  
**Strategy:** Forward-compatible additive migrations + container version rollbacks

---

## 1. Guiding Invariant: Forward-Only Database Migrations
All Supabase/PostgreSQL schema changes in `scripts/migrations/` MUST be **additive and forward-compatible**:
1. **Never drop columns or rename tables in the same release as code changes.**
2. New columns must be `NULLABLE` or provide safe `DEFAULT` expressions.
3. Old container versions MUST be capable of running against newly migrated database schemas without crashing.

---

## 2. Fast Rollback Procedure (Step-by-Step)

### Step 1: Trigger Application Rollback on Render / Container Registry
1. Open Render Dashboard -> `mediassist-ai` Web Service -> **Deploys**.
2. Identify the last known green commit / deploy SHA.
3. Click **Rollback to this Deploy** (or trigger CLI `render deploys rollback <deploy-id>`).
4. The previous stable Docker image boots immediately (<60s).

### Step 2: Draining In-Flight Webhook Requests
- Inbound WhatsApp messages in `inbound_messages` remain safely stored with `status='received'` or `status='processing'`.
- The newly spawned stable container executes `recover_pending_inbound_messages()` on boot, safely reclaiming any orphaned processing leases without message loss.

### Step 3: Connector Worker Rollback
- Revert `mediassist-connector-worker` service to match the web service image version.
- Running connector jobs are protected by atomic `connectors.runner` job locks.

---

## 3. Rehearsal and Verification Checklist
- [x] Application boot assertions pass against both current and n+1 schema.
- [x] Staging test instance successfully rolled back without database corruption.
- [x] DLQ depth remains zero post-rollback.
