# Multi-Instance Cache Staleness Bound & Invalidation Semantics (W4.2)

## Overview
In horizontally scaled multi-instance deployments (e.g. 2+ worker processes / containers), each instance maintains local in-memory caches for high-frequency tenant data:
- `_tenant_cache`: Resolved clinic configurations, WhatsApp number associations, plan tiers, and features.
- `_branch_cache`: Hospital branch locations and active doctor-branch links.
- `_holiday_cache`: Hospital operational holiday lists.

---

## 1. Staleness Bound Specification
- **Time-to-Live (TTL):** `CACHE_TTL_SECONDS = 300` (5 minutes maximum staleness).
- **Upper Bound:** Any clinic setting update or feature flag flip made on Instance A will propagate to all other instances at latest upon their next local TTL expiration (maximum 300 seconds).
- **Guaranteed Consistency Invariant:** All transactional operations (appointments, payments, refunds, queue token check-ins, lab report claims) bypass cache and execute directly on PostgreSQL using ACID row locks and unique constraints.

---

## 2. Invalidation Protocol
1. **Local Mutator Invalidation:** When an admin modifies clinic profile or settings via `/admin/profile` or `/admin/settings/*`, `invalidate_tenant_cache(clinic_id)` immediately purges the local instance's cache.
2. **Database Persistence:** Changes are committed to Supabase/PostgreSQL.
3. **Cross-Worker Eviction:** Remote worker instances evict expired records via automatic timestamp verification (`cached_at + CACHE_TTL_SECONDS < time.time()`).
4. **Immediate Sync for Sensitive Fields:** Authentication credentials, RBAC permissions, and Razorpay webhook signing secrets query the database directly or use short 60-second rate-limit windows.

---

## 3. Multi-Instance Invariant Table
| Domain | Layer | Guarantee |
|---|---|---|
| Inbound WhatsApp Messages | `inbound_messages` (PostgreSQL `UNIQUE`) | Exactly-once durable ingestion |
| Appointment Slot Booking | `appointments` (`idx_unique_active_doctor_slot`) | Zero double-booking under concurrency |
| Queue Token Assignment | Atomic PostgreSQL retry loop | Sequential unique daily tokens per doctor |
| Scheduler Distributed Jobs | `scheduler_locks` (`pg_try_advisory_lock` / RPC) | Exactly-once job execution per interval |
| Inbound Crash Recovery | `recover_pending_inbound_messages` | Abandoned worker leases reclaimed without message loss |
