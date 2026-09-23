# 01 — System Architecture: Kriya AI

## 1. System Topology & Architectural Diagram

```text
                                       INTERNET
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  ▼                                               ▼
      [Meta WhatsApp Cloud API]                       [Clinic Staff / Admin]
     (Inbound Webhooks / Receipts)                    (Browser / HTTPS Web)
                  │                                               │
                  │ POST /webhook                                 │ GET /admin-panel, /platform-panel
                  │                                               │ REST API: /admin/*, /platform/*
                  ▼                                               ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │                         RENDER.COM WEB CONTAINER                            │
   │                                                                             │
   │  FastAPI (Uvicorn 2 workers) · Port 8000 · Asia/Kolkata (IST)              │
   │                                                                             │
   │  ┌───────────────────────────────────────────────────────────────────────┐  │
   │  │ Middleware Pipeline                                                   │  │
   │  │  1. CorrelationIdMiddleware (X-Correlation-ID tracing)                 │  │
   │  │  2. SecurityHeadersMiddleware (CSP, HSTS, X-Frame-Options: DENY)      │  │
   │  │  3. CORSMiddleware (Restricted same-origin in production)             │  │
   │  └───────────────────────────────────────────────────────────────────────┘  │
   │                                                                             │
   │  ┌─────────────────────────────┐  ┌──────────────────────────────────────┐  │
   │  │ Ingress Routers             │  │ Admin & Internal Routers             │  │
   │  │  • /webhook (Meta WhatsApp) │  │  • /admin/* (Staff RBAC)             │  │
   │  │  • /webhooks/razorpay       │  │  • /platform/* (Platform Owner)      │  │
   │  │  • /health, /ready, /live   │  │  • /fhir/* (HL7 FHIR R4)             │  │
   │  │  • /metrics (Prometheus)    │  │  • /internal/integrations/*          │  │
   │  └──────────────┬──────────────┘  └──────────────────┬───────────────────┘  │
   │                 │                                    │                      │
   │                 ▼                                    │                      │
   │  ┌─────────────────────────────┐                     │                      │
   │  │ Durable Ingestion Boundary  │                     │                      │
   │  │ (INSERT inbound_messages    │                     │                      │
   │  │  BEFORE HTTP 200 response)  │                     │                      │
   │  └──────────────┬──────────────┘                     │                      │
   │                 │ BackgroundTask                     │                      │
   │                 ▼                                    │                      │
   │  ┌─────────────────────────────┐                     │                      │
   │  │ Per-Phone Concurrency Lock  │                     │                      │
   │  │ (asyncio.Lock + CAS Lease)  │                     │                      │
   │  └──────────────┬──────────────┘                     │                      │
   │                 │                                    │                      │
   │                 ▼                                    ▼                      │
   │  ┌───────────────────────────────────────────────────────────────────────┐  │
   │  │ Core Domain Logic Services                                            │  │
   │  │  • conversation_manager (27-state WhatsApp FSM)                       │  │
   │  │  • clinical_firewall (Deterministic NMC Drug/Advice Interceptor)      │  │
   │  │  • ai_gateway (OpenRouter Llama/DeepSeek + Gemini fallback)           │  │
   │  │  • hybrid_search (Zero-LLM Multilingual Catalogue Matcher)            │  │
   │  │  • payment_service (Razorpay Payment Links, Slot Holds, Refunds)      │  │
   │  │  • lab_report_service (Upload, Storage, WhatsApp PDF Delivery)        │  │
   │  │  • specialty_flow (Derma, Eye, Dental, IVF, Women & Child)            │  │
   │  │  • permissions (Branch-scoped RBAC Enforcement)                       │  │
   │  └───────────────────────────────────┬───────────────────────────────────┘  │
   │                                      │                                      │
   │  ┌───────────────────────────────────┴───────────────────────────────────┐  │
   │  │ Database Execution Engine: app.database.sb()                          │  │
   │  │  • Off-Loop ThreadPoolExecutor (_DB_EXECUTOR, max_workers=16)         │  │
   │  │  • Forces HTTP/1.1 transport (prevents H2 connection teardown cascade)│  │
   │  │  • 15-second bounded query ceiling                                    │  │
   │  │  • Automatic retry on stale pooled TCP keep-alive connections         │  │
   │  │  • scoped_query() enforcement against TENANT_OWNED_TABLES             │  │
   │  └───────────────────────────────────┬───────────────────────────────────┘  │
   │                                      │                                      │
   │  ┌───────────────────────────────────┴───────────────────────────────────┐  │
   │  │ Embedded Scheduler: APScheduler (AsyncIOScheduler)                    │  │
   │  │  • 24 background cron & interval jobs                                 │  │
   │  │  • CAS lease-based distributed locks on scheduler_locks table         │  │
   │  └───────────────────────────────────────────────────────────────────────┘  │
   └──────────────────────────────────────┬──────────────────────────────────────┘
                                          │
                                          ▼
   ┌─────────────────────────────────────────────────────────────────────────────┐
   │                         SUPABASE POSTGRESQL 15+                             │
   │                                                                             │
   │  • 50 Tables (30 strictly tenant-scoped via clinic_id)                      │
   │  • Service Role Connection (Holds BYPASSRLS — App enforces isolation)       │
   │  • Supabase Storage Bucket ('lab-reports' for encrypted diagnostic PDFs)   │
   └─────────────────────────────────────────────────────────────────────────────┘
                                          ▲
                                          │
   ┌──────────────────────────────────────┴──────────────────────────────────────┐
   │                  RENDER.COM DEDICATED WORKER CONTAINER                      │
   │                                                                             │
   │  Command: python -m connectors.runner --all                                 │
   │  • Headless Chromium (Playwright) isolated from Web Container               │
   │  • Polls MocDoc / EHR systems for newly signed diagnostic reports           │
   │  • CAS advisory lock (acquire_connector_lock) with 5-minute lease           │
   │  • Downloads PDF, matches patient, POSTs to /internal/integrations          │
   └─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Process Model & Concurrency Architecture

### Process Separation
The platform operates as two distinct processes in production (`render.yaml`):
1. **Web Service (`mediassist-ai`)**:
   - Runs `uvicorn app.main:app --workers 2` across 2 Render instances (total 4 worker processes).
   - Serves customer traffic, Meta webhooks, admin UI, and REST APIs.
   - Sets `RUN_CONNECTORS_IN_WEB=false` to prevent memory-heavy Chromium processes from launching in the web container.
2. **Connector Worker (`mediassist-connector-worker`)**:
   - Runs `python -m connectors.runner --all`.
   - Runs Playwright headless Chromium for web scraping MocDoc/EHR portals.
   - An OOM crash in Chromium does not disrupt patient WhatsApp chat or admin UI.

### Inbound Concurrency & Idempotency
To meet Meta's strict 20-second webhook ACK window and prevent race conditions:
1. **Durable Ingestion**: The webhook immediately writes incoming payloads to `inbound_messages` and returns HTTP 200.
2. **Atomic Queue Claim**: The message is dispatched to a FastAPI `BackgroundTask`. It performs an atomic `INSERT ... ON CONFLICT DO NOTHING` into `processed_messages`.
3. **Per-Phone Lock**: Within each process, an `asyncio.Lock` protects the patient's phone number (`message_queue._phone_locks`). A cross-process lease is held in `scheduler_locks` (`phone_{last10digits}`) with a background heartbeat (`_renew_phone_lease`).

---

## 3. Data Access & Off-Loop Execution Layer (`sb()`)

### The PostgREST Asynchronous Bottleneck
Supabase's Python SDK (`supabase-py`) v2.x wraps the synchronous `postgrest-py` client. Calling `.execute()` inside an `async def` route directly blocks Python's asyncio event loop for the duration of the HTTP round-trip (~30–150ms). A single booking turn with 15 DB calls freezes the event loop for 1–2 seconds.

### The Solution: `app.database.sb()`
[`app/database.py:152`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py#L152) defines the `sb(builder)` wrapper:
- **Dedicated Thread Pool**: Executes `.execute()` on `_DB_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="kriya-db")`.
- **Forced HTTP/1.1**: Patches `supabase.postgrest._session` to force HTTP/1.1 instead of HTTP/2. In HTTP/2, all requests multiplex over a single TCP connection; a single query timeout triggers a connection reset that terminates all multiplexed queries simultaneously (the "cascading timeout" failure mode). HTTP/1.1 isolates failures to individual connections.
- **Bounded Query Timeout**: Configured at 15 seconds (`settings.db_query_timeout_seconds`).
- **Connection Retry**: Stale keep-alive connections that disconnect prematurely are retried once on idempotent methods (`GET`, `HEAD`, `PATCH`, `DELETE`). `POST` is never retried to prevent duplicate insertions.

---

## 4. Multi-Tenant Enforcement & Scoping Rules

### The Service Role Reality
The backend connects to Supabase using `SUPABASE_SERVICE_ROLE_KEY`. In PostgreSQL, `service_role` has `BYPASSRLS`.
> **CRITICAL SECURITY FACT**: PostgreSQL Row Level Security (RLS) policies defined in migrations are **completely bypassed** by the backend API. Application code is the sole tenant isolation enforcement boundary.

### `scoped_query()` Guard
[`app/database.py:254`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py#L254) provides `scoped_query(table_name, clinic_id)`:
- Queries against any table in `TENANT_OWNED_TABLES` (defined in [`app/tenancy.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/tenancy.py)) **MUST** provide a valid `clinic_id`.
- Rejects sentinel values (`default`, `none`, `null`, `""`, `*`, `all`) via `is_valid_clinic_scope()`.
- If `clinic_id` is invalid and `allow_unscoped=True` is not explicitly set, it raises `TenantIsolationError`.

---

## 5. Failure & Recovery Modes

| Subsystem | Failure Scenario | System Reaction | Recovery Mechanism |
|---|---|---|---|
| **Database** | Supabase transient disconnect | `sb()` retries idempotent queries once | Re-connects; raises 503 if unreachable |
| **Meta Webhook** | Webhook delivery delayed or retried | Signature check -> `inbound_messages` deduplication via `processed_messages` UNIQUE constraint | Duplicate dropped silently; original proceeds |
| **OpenRouter / LLM** | OpenRouter 429 rate-limit or 5xx | Falls back to Groq Llama 3.3; if Groq fails, falls back to regex/keyword rules | Zero-LLM fallback completes booking |
| **Worker Process** | Worker killed during report scraping | Database advisory lock lease expires in 5 minutes | Next scheduled poll claims lease and resumes |
| **Message Lock** | Phone lock timeout (>15s) | Defers message to `failed_messages` DLQ | Scheduler re-plays pending messages every 5 min |
| **Razorpay** | Webhook missed or failed | Fast-poll scheduler runs every 30s for bookings created in last 5 min | Syncs payment status and confirms booking |
