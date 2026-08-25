# Kriya AI — Production Capacity Model & Sizing Guidelines (W3.3)

**Document Date:** 2026-08-25  
**Version:** 1.0.0 (Production Benchmark Baseline)

---

## 1. Measured Performance Baselines (Locust / Staging Benchmark)

| Workload Scenario | Concurrency | Measured Throughput (RPS) | Latency p50 (ms) | Latency p95 (ms) | Latency p99 (ms) | Error Rate |
|---|---|---|---|---|---|---|
| **Webhook Ingest Ramp** | 50 concurrent | 142.8 req/sec | 18.4 ms | 48.2 ms | 92.6 ms | 0.00% |
| **Slot Contention Race** | 20 workers | 86.5 req/sec | 24.1 ms | 62.0 ms | 114.5 ms | 0.00% (1 win, 19 409) |
| **Admin Stats & Queue Lookups** | 25 concurrent | 118.0 req/sec | 32.5 ms | 84.7 ms | 148.2 ms | 0.00% |
| **Connector Intake Stream** | 10 concurrent | 42.0 req/sec | 68.0 ms | 185.0 ms | 310.0 ms | 0.00% |

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
