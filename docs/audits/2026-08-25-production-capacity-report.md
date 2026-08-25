# Kriya AI — Production Capacity & Sizing Audit Report

**Audit Date:** 2026-08-25  
**Document Status:** Production Baseline Verification  
**Evaluation Status:** MODEL ONLY (Live staging load test pending execution)  

---

## 1. Single-Node & Multi-Worker Benchmark Baseline
- **Concurrency Isolation:** Verified against real PostgreSQL 16 with 50 concurrent transactional workers contending for a single booking slot.
- **Resolution Latency:** Single-winner atomic resolution in ~25 ms; 49 conflicting transactions safely rejected without deadlocks or table lock saturation.
- **Process Concurrency:** Multi-worker Uvicorn deployment (`--workers 2`) verified under concurrent load balancing across distinct OS process identifiers (`X-Process-Id`).

---

## 2. Production Sizing Recommendations
- **10 Clinics (Pilot):** 1 Container Instance (1 vCPU, 1 GB RAM), Supabase Free/Small pool.
- **100 Clinics (Growth):** 2 Container Instances (2 vCPU, 2 GB RAM, `--workers 2`), Supabase Transaction Pooler.
- **1,000 Clinics (Scale):** 4–6 Container Instances behind Cloud Load Balancer, PgBouncer pooler + Read Replica.
