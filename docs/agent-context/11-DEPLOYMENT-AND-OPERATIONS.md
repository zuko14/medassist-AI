# 11 - DEPLOYMENT & PRODUCTION OPERATIONS

This document details the production container topology, orchestration rules, migration pipeline, environment configuration, and operational lifecycle procedures for KriyaAI.

---

## 1. PRODUCTION DEPLOYMENT TOPOLOGY (RENDER ARCHITECTURE)

KriyaAI is architected for deployment on [Render](https://render.com) using containerized services declared in [`render.yaml`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/render.yaml):

```text
               Public Internet (Meta Webhooks, Razorpay, Admin Traffic)
                                        │
                                        ▼
                   ┌────────────────────────────────────────┐
                   │    Render Reverse Proxy & Edge SSL     │
                   └────────────────────────────────────────┘
                                        │
                 ┌──────────────────────┴──────────────────────┐
                 ▼                                             ▼
  ┌──────────────────────────────┐              ┌──────────────────────────────┐
  │   Web Instance 1 (Docker)    │              │   Web Instance 2 (Docker)    │
  │  Uvicorn (2 workers)         │              │  Uvicorn (2 workers)         │
  │  FastAPI + APScheduler       │              │  FastAPI + APScheduler       │
  │  RUN_CONNECTORS_IN_WEB=false │              │  RUN_CONNECTORS_IN_WEB=false │
  └──────────────────────────────┘              └──────────────────────────────┘
                 │                                             │
                 └──────────────────────┬──────────────────────┘
                                        │
                  ┌─────────────────────┼─────────────────────┐
                  ▼                     ▼                     ▼
     ┌───────────────────────┐   ┌──────────────┐   ┌───────────────────────┐
     │ Supabase (PostgreSQL) │   │ Meta Cloud   │   │ Connector Worker      │
     │ 50 tables, migrations │   │ WhatsApp API │   │ (Dedicated Docker)    │
     │ off-loop sb() pool    │   └──────────────┘   │ Playwright / Chromium │
     └───────────────────────┘                      │ python -m connectors  │
                                                    └───────────────────────┘
```

### Services Summary
1. **`mediassist-ai` (Web Service)**:
   - 2 container instances (`numInstances: 2`).
   - Runs `uvicorn app.main:app` with 2 worker processes per instance (`WEB_CONCURRENCY: 2`), totaling 4 active web worker processes.
   - `preDeployCommand: python scripts/migrate.py` runs before rolling containers update.
   - `healthCheckPath: /health`.
   - `RUN_CONNECTORS_IN_WEB: "false"` guarantees headless Chromium is not launched in the web containers, eliminating OOM spikes that could breach Meta's 20-second webhook SLA.
2. **`mediassist-connector-worker` (Background Worker)**:
   - Dedicated Docker container executing `python -m connectors.runner --all`.
   - Hosts Playwright headless Chromium for scraping hospital LIS/MocDoc portals without impacting web responsiveness.
3. **`mediassist-ai-staging` (Staging Environment)**:
   - Single container instance tracking git branch `staging`.

---

## 2. CONTAINER SPECIFICATION & DOCKERFILE

- **File**: [`Dockerfile`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/Dockerfile)
- **Base Image**: `python:3.11-slim`
- **System Dependencies**: `gcc`, `tzdata`, `tesseract-ocr`, `poppler-utils`, `tini`.
- **Timezone Invariant**: `ENV TZ=Asia/Kolkata`
  - Indian clinics and patients operate on IST. Without `TZ=Asia/Kolkata`, naive `datetime.now()` calls in scheduler crons and slot cutoffs default to UTC, causing a 5.5-hour timing offset.
- **Playwright Path**: `ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright`
  - Browser binaries are pre-installed at build time (`playwright install --with-deps chromium`) to eliminate per-boot downloads.
- **Process Supervision (`tini`)**:
  - `ENTRYPOINT ["/usr/bin/tini", "--"]` ensures that zombie/orphaned Chromium processes spawned by Playwright are cleanly reaped as PID 1.
- **Unprivileged User**: Runs as `appuser` (UID 1000).

---

## 3. LIFECYCLE & STARTUP/SHUTDOWN HOOKS

Implemented in `lifespan(app)` in [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/main.py):

### Startup Sequence
1. **DB Thread Pool Initialization**: `app.database._DB_EXECUTOR` initializes with 16 worker threads for off-loop PostgREST execution.
2. **Production Security Pre-flight**: If not running in development mode, inspects secrets. Boot halts if default/placeholder passwords or secrets are detected (`ADMIN_PASSWORD`, `META_APP_SECRET`, `INTEGRATION_SECRET`).
3. **Connector Key Verification**: Validates `CONNECTOR_ENCRYPTION_KEY` using `fernet_key_problem()`. Logs critical alert if key is corrupted.
4. **Database Schema Drift Pre-flight (KA-01)**:
   - Compares highest applied migration recorded in `schema_migrations` against the highest `XXX_*.sql` on disk.
   - Refuses to boot if database migration number is less than disk migration number (fail-closed against stale schemas).
5. **Storage Cleanup & Directory Check**: Purges stale temporary report files older than 3,600s.
6. **Subsystem Boot**: Starts APScheduler (`scheduler_service.start()`) and CallMedex queue engine.

### Shutdown Sequence (Graceful Drain)
1. **Background Task Drain (KA-P1-04)**: Waits up to 10 seconds for pending `spawn_background_task` and `BackgroundTasks` coroutines to finish before terminating the event loop.
2. **Queue Shutdown**: Stops CallMedex queue engine.
3. **Scheduler Shutdown**: Gracefully pauses APScheduler jobs.
4. **Lock Release**: Releases any held distributed locks via `release_all_locks_held()`.

---

## 4. DATABASE MIGRATION PIPELINE

- **Script**: [`scripts/migrate.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/scripts/migrate.py)
- **Directory**: `migrations/` (85 sequential `.sql` migration files)
- **Tracking Table**: `schema_migrations(name, applied_at)`
- **Execution**: Triggered automatically during Render deployment via `preDeployCommand: python scripts/migrate.py`. Parses SQL statements, executes against Supabase via service role, and logs each applied file.

---

## 5. ENVIRONMENT CONFIGURATION REFERENCE

| Variable | Required? | Default / Example | Purpose |
| :--- | :--- | :--- | :--- |
| `APP_ENV` | Required | `production` / `development` | Toggles strict security controls and documentation endpoints. |
| `SUPABASE_URL` | Required | `https://xyz.supabase.co` | Supabase project API URL. |
| `SUPABASE_SERVICE_ROLE_KEY`| Required | `eyJ...` (Secret) | Service role secret key carrying `BYPASSRLS`. |
| `META_APP_SECRET` | Required (Prod) | `sec_...` (Secret) | Used for verifying inbound `X-Hub-Signature-256`. |
| `META_VERIFY_TOKEN` | Required | `token_...` | Meta hub webhook handshake verification challenge. |
| `META_ACCESS_TOKEN` | Optional (Global)| `EAA...` (Secret) | Global default WhatsApp token for single-tenant mode. |
| `META_PHONE_NUMBER_ID` | Optional (Global)| `1092837465...` | Global default WhatsApp phone number ID. |
| `RAZORPAY_KEY_ID` | Optional | `rzp_live_...` | Razorpay gateway identifier. |
| `RAZORPAY_KEY_SECRET` | Optional | `sec_...` (Secret) | Razorpay gateway secret. |
| `RAZORPAY_WEBHOOK_SECRET` | Optional | `whsec_...` (Secret)| Razorpay webhook signature verification secret. |
| `OPENROUTER_API_KEY` | Required | `sk-or-v1-...` (Secret) | LLM inference API key for OpenRouter. |
| `OPENROUTER_MODEL` | Optional | `deepseek/deepseek-chat`| Primary LLM completion model. |
| `OPENROUTER_FALLBACK_MODEL`| Optional | `google/gemini-2.0-flash-001`| Fallback LLM completion model. |
| `INTEGRATION_SECRET` | Required | `sec_...` (Secret) | Shared secret for internal connector ingestion API. |
| `CONNECTOR_ENCRYPTION_KEY` | Required | `fernet_base64...` | AES key for encrypting stored LIS passwords. |
| `OWNER_USERNAME` / `_PASSWORD`| Required (Prod)| `owner_kriya` / Secret | Platform owner console authentication credentials. |
| `ADMIN_USERNAME` / `_PASSWORD`| Required | `admin` / Secret | Initial clinic administrator seed account. |
| `RUN_CONNECTORS_IN_WEB` | Optional | `false` (in Render web)| Flags whether Playwright runs inside the web container. |
| `METRICS_TOKEN` | Optional | Secret | Bearer token for scraping `/metrics`. |
