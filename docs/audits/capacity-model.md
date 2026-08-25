# Kriya AI — Production Capacity Model & Sizing Guidelines (W3.1–W3.4)

**Document Date:** 2026-08-25  
**Version:** 1.0.0 (Capacity Model & Theoretical Sizing Framework)  
**CAPACITY STATUS:** Modeled based on single-worker benchmarks; live staging load test pending execution.  
**STATUS CLASSIFICATION:** UNVERIFIED / MODEL ONLY (W3.1–W3.4 pending staging run).  
**Capacity Domain Score Cap:** 3/5 (Strictly bounded per authoritative audit completion plan).  

---

## 1. Capacity Modeling Methodology & Mathematical Framework

### 1.1 Methodology & Assumptions
The projections in this model are derived from single-node in-process asynchronous dispatch benchmarks and real PostgreSQL transaction isolation measurements:
- **FastAPI/Uvicorn Async Concurrency:** Non-blocking I/O loop handling concurrent HTTP socket connections with Python asyncio.
- **PostgreSQL Transaction Baseline:** Measured row-level conflict resolution under 50-thread concurrent slot contention resolving in ~25 ms.
- **Meta Webhook 20-Second SLA:** Inbound webhook payloads write a durable row to `inbound_messages` / `processed_messages` and immediately return HTTP 200 within <50 ms, preventing Meta retry cascades.
- **Assumed Deployment Topology:** 2 worker processes per container (`--workers 2`), 2 container instances (`numInstances: 2`) behind standard load balancing.

### 1.2 Invalidation Conditions
This model is invalidated and must be recalculated if:
1. Database connection latency exceeds 80 ms (e.g. non-pooled direct SSL connections to cross-region PostgreSQL).
2. Groq LLM API response time exceeds 4,000 ms during high-concurrency conversational turns.
3. Node memory consumption exceeds 512 MB per worker process under prolonged high-concurrency PDF OCR processing.

---

## 2. Multi-Tenant Capacity Projections

### Tier A: 10 Active Clinics (Pilot / Launch)
- **Daily Inbound Volume:** ~2,500 messages / day
- **Peak Traffic:** ~5 req/sec
- **Recommended Compute:** 1 container instance (1 vCPU, 1 GB RAM)
- **Database Connection Pool:** 10 connections (Supabase Small / Free tier compatible)
- **Resource Utilization:** CPU < 8%, Memory < 180 MB RSS

### Tier B: 100 Active Clinics (Growth Scale)
- **Daily Inbound Volume:** ~35,000 messages / day
- **Peak Traffic:** ~40–60 req/sec
- **Recommended Compute:** 2 container instances (2 vCPU, 2 GB RAM each) with `--workers 2`
- **Database Connection Pool:** 25 connections with Supabase Transaction Pooler (Port 6543)
- **Resource Utilization:** CPU ~25–35%, Memory ~320 MB RSS per instance

### Tier C: 1,000 Active Clinics (Enterprise Fleet)
- **Daily Inbound Volume:** ~400,000 messages / day
- **Peak Traffic:** ~350–500 req/sec
- **Recommended Compute:** 4–6 container instances (2 vCPU, 4 GB RAM each) behind Cloud Load Balancer
- **Database Connection Pool:** 60 connections with Supabase Direct Pooler + Read Replica for Analytics
- **Cache Architecture:** Local 300s TTL tenant cache + distributed PgBouncer pooler

---

## 3. Bottleneck Analysis & Guardrails
1. **Meta Webhook 20-Second Window:** Inbound messages are accepted and written to `inbound_messages` asynchronously within <50 ms, returning HTTP 200 to Meta immediately.
2. **Groq LLM Rate Limits:** Per-phone serialization prevents duplicate LLM invocations for rapid repeated patient messages.
3. **PostgreSQL Row Locks:** Appointment slot reservation and queue token allocation use atomic conditional statements (`WHERE status = 'available'` / `UPDATE token_counter`) preventing table-wide lock contention.
