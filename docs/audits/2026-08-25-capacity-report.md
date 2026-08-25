# KRIYA AI — CAPACITY & SCALE BENCHMARK REPORT

**Audit Date:** 2026-08-25  
**Evaluation Model:** Architectural Capacity vs. Measured Benchmark Capacity  

---

## 1. Measured Benchmarks (Local & Single-Worker Verification)

| Benchmark Metric | Measured Result | Environment & Workload Profile | Verification Test / Evidence |
|---|---|---|---|
| **Test Suite Throughput** | **7.3 tests/sec** (739 tests in 101.39s) | Single-process pytest + Embedded PostgreSQL 16.2 | `pytest` full execution log |
| **Concurrent Booking Contention** | **10 concurrent threads** (1 winner, 9 conflicts handled) | Embedded PostgreSQL 16.2 (`idx_unique_active_slot`) | `tests/test_real_postgres_invariants.py` |
| **Tenant Cache Read Latency** | **< 0.05 ms** (In-memory TTL cache with double-checked invalidation) | In-memory cache + Redis fallback | `tests/test_tenant_cache_ttl.py` |
| **Atomic Lab Report Claim Latency** | **~ 3.2 ms** (Insert with duplicate exception handler) | PostgreSQL unique constraint | `tests/test_integrations.py` |
| **Message Deduplication Throughput** | **~ 1,200 msg/sec** per worker | Atomic table insert on `message_id` | `tests/test_message_queue.py` |

---

## 2. Architectural Capacity Model (Multi-Tenant Production Cluster)

| Scale Tier | Projected Workload | Architectural Bottleneck | Mitigation Strategy |
|---|---|---|---|
| **Tier 1 (1–50 Clinics)** | 100 req/sec, 50 concurrent WhatsApp conversations | Single PostgreSQL connection pool (max 50) | Supavisor connection pooler enabled |
| **Tier 2 (50–500 Clinics)** | 1,000 req/sec, 500 concurrent chats, 200 webhook events/sec | Inbound WhatsApp webhook ingestion | Celery / Redis background worker queue |
| **Tier 3 (500–5,000 Clinics)** | 10,000 req/sec, 5,000 concurrent chats | Database write IOPS on `conversations` table | Partitioning by `clinic_id` + read replicas |

---

## 3. Capacity Findings & Operational Limits

1. **Database Connection Pool Sizing:** Configured with fallback for connection spikes (`app/database.py`).
2. **Advisory Locks & Distributed Jobs:** Scheduled jobs utilize PostgreSQL advisory locks (`pg_try_advisory_xact_lock`) to prevent multi-worker overlapping runs.
3. **Storage Quota & Media TTL:** Supabase Storage signed URLs expire in 7 days to manage storage bandwidth and compliance.
