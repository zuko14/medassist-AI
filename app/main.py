"""MediAssist AI - Hospital WhatsApp Assistant — Security Hardened."""

import logging
import httpx
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware


from app.config import settings
from app.routers import webhook, health, admin, clinics, platform
from app.routers.integrations import router as integrations_router
from app.integrations.callmedex.api.router import (
    router as callmedex_router,
    global_container as callmedex_container,
)
from app.routers.fhir import router as fhir_router
from app.routers.razorpay_webhook import router as razorpay_router
from app.services.scheduler import scheduler_service
from app.utils.logger import setup_logging
from app.utils.security import SECURITY_HEADERS

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)


from app.utils.correlation import set_correlation_id, get_correlation_id
from app.services.metrics import metrics



class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Assign and thread request correlation IDs for end-to-end tracing (W5.1)."""

    async def dispatch(self, request: Request, call_next):
        req_cid = request.headers.get("X-Correlation-ID") or request.headers.get("X-Request-ID")
        if req_cid:
            set_correlation_id(req_cid)
        else:
            req_cid = get_correlation_id()

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = req_cid
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response.

    Protects against:
    - XSS attacks (X-XSS-Protection, CSP)
    - Clickjacking (X-Frame-Options)
    - MIME sniffing (X-Content-Type-Options)
    - Referrer leakage (Referrer-Policy)
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        # Also add ngrok skip header for development convenience
        response.headers["ngrok-skip-browser-warning"] = "1"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting MediAssist AI...")

    # Off-loop database execution (T5.1 / KA-P1-03). app.database._DB_EXECUTOR
    # is a dedicated, module-level pool sized from settings.db_thread_pool_size
    # — deliberately NOT the loop's default executor, which Starlette also uses
    # for sync endpoints, and NOT AnyIO's limiter, which governs a different
    # pool entirely. Logged at boot so the per-process database concurrency
    # ceiling is visible in the startup record.
    from app.database import _DB_EXECUTOR

    logger.info(
        f"DB executor ready: {_DB_EXECUTOR._max_workers} worker threads "
        f"(per-process query concurrency ceiling)"
    )

    # Security audit log on startup
    if not settings.meta_app_secret:
        logger.warning(
            "⚠️  META_APP_SECRET is not set — webhook signature verification is DISABLED. "
            "Set this in .env for production security."
        )
    if settings.admin_password in ("admin", "admin123", "password", ""):
        logger.warning(
            "⚠️  ADMIN_PASSWORD is using a default/weak value — change it immediately!"
        )
    if not settings.owner_username or not settings.owner_password:
        logger.info(
            "ℹ️  OWNER_USERNAME/OWNER_PASSWORD not set — platform owner dashboard "
            "(/platform-panel) is disabled (returns 503)."
        )
    elif settings.owner_password in ("owner", "owner_secret_change_me", "admin", "password", "123456"):
        logger.warning(
            "⚠️  OWNER_PASSWORD is using a default/weak value — change it immediately! "
            "This dashboard exposes cross-hospital revenue and patient data."
        )
    if settings.app_env == "development":
        logger.info(
            "🔓 Running in DEVELOPMENT mode"
        )
    else:
        logger.info(
            f"🔒 Running in {settings.app_env.upper()} mode — enforcing production security controls"
        )
        placeholder_secrets = []
        if settings.admin_username in ("admin", "administrator", "root", ""):
            placeholder_secrets.append("ADMIN_USERNAME")
        if not settings.meta_app_secret or settings.meta_app_secret in ("change_me_in_production", "dev_secret"):
            placeholder_secrets.append("META_APP_SECRET")
        if settings.admin_password in ("admin", "admin123", "password", ""):
            placeholder_secrets.append("ADMIN_PASSWORD")
        if settings.owner_password in ("owner", "owner_secret_change_me", "admin", "password", "123456"):
            placeholder_secrets.append("OWNER_PASSWORD")
        if not settings.integration_secret or "change_in_prod" in settings.integration_secret.lower():
            placeholder_secrets.append("INTEGRATION_SECRET")
        if callmedex_container.settings.bearer_token.get_secret_value() in ("dev_bearer_token", "change_in_prod"):
            placeholder_secrets.append("CALLMEDEX_BEARER_TOKEN")

        if placeholder_secrets:
            error_msg = f"Refusing to boot in {settings.app_env} mode with default/placeholder secrets: {', '.join(placeholder_secrets)}"
            logger.critical(f"FATAL: {error_msg}")
            raise RuntimeError(error_msg)

        # Connector credential key: a pure local check, so a malformed key is
        # knowable at boot instead of being rediscovered on every poll as a
        # bare "ValueError". Deliberately NOT fatal — connectors are optional,
        # and taking down booking and reminders for a clinic that does not use
        # them would be a far bigger outage than the one being reported.
        if settings.connector_encryption_key:
            from app.utils.connector_crypto import fernet_key_problem

            key_problem = fernet_key_problem(settings.connector_encryption_key)
            if key_problem:
                logger.critical(
                    f"CONNECTOR CREDENTIALS UNUSABLE: {key_problem} "
                    f"Report connectors will fail every poll until this is fixed. "
                    f"Stored passwords are NOT lost — restore the original key. "
                    f"Generating a new key makes them permanently undecryptable."
                )

        # Database schema pre-flight: fail-closed schema-drift detection (KA-01)
        # Compares highest applied migration in DB against highest .sql on disk.
        # Refuses to start if migrations are missing — prevents serving on stale schema.
        try:
            import os as _os
            import re as _re
            from app.database import supabase

            # 1. Determine highest migration file on disk
            migrations_dir = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "migrations")
            migration_files = [
                f for f in _os.listdir(migrations_dir)
                if _re.match(r"^\d{3}_", f) and f.endswith(".sql")
            ]
            if not migration_files:
                raise RuntimeError("No migration files found in migrations/ directory")
            highest_file = sorted(migration_files)[-1]
            highest_file_num = int(highest_file[:3])

            # 2. Query highest applied migration from DB
            result = await sb(supabase.table("schema_migrations").select("name").order("name", desc=True).limit(1))
            if not result.data:
                raise RuntimeError("schema_migrations table is empty — no migrations applied")
            highest_applied = result.data[0]["name"]
            match = _re.match(r"^(\d{3})_", highest_applied)
            highest_applied_num = int(match.group(1)) if match else 0

            # 3. Fail-closed: applied must be >= disk
            if highest_applied_num < highest_file_num:
                raise RuntimeError(
                    f"Schema drift detected: DB has migration {highest_applied} "
                    f"(#{highest_applied_num}) but disk has {highest_file} "
                    f"(#{highest_file_num}). Run migrations before starting."
                )

            # 4. Verify critical tables still exist (regression guard)
            # unscoped: migration_backfill
            await sb(supabase.table("inbound_messages").select("id").limit(1))
            await sb(supabase.table("scheduler_locks").select("job_name").limit(1))
            # unscoped: migration_backfill
            await sb(supabase.table("appointments").select("refund_id").limit(1))

            logger.info(
                f"✅ Schema pre-flight passed: DB at {highest_applied} "
                f"(disk highest: {highest_file})"
            )
        except RuntimeError:
            raise
        except (httpx.TransportError, OSError) as e:
            # Transient connectivity issue (Supabase outage, DNS failure,
            # read timeout).  Crashing here would cause infinite restart
            # loops on the deployment platform while the DB is unreachable.
            # TransportError is the whole family, not just ConnectError and
            # TimeoutException: behind an egress proxy — the norm on hospital
            # networks — a hiccup surfaces as ProxyError, and a dropped
            # connection as ReadError or RemoteProtocolError. Those fell
            # through to the fatal branch below and crash-looped the app on
            # exactly the outage this handler exists to ride out.
            # Log critically so alerts fire, but let the app start — it
            # will serve 503s on DB-dependent routes until connectivity
            # recovers, which is strictly better than being fully down.
            logger.critical(
                f"⚠️ Schema pre-flight SKIPPED due to database connectivity "
                f"error ({type(e).__name__}: {e}). The app will start but "
                f"database-dependent features may be degraded until "
                f"connectivity is restored."
            )
        except Exception as e:
            error_msg = f"Database schema validation failed on {settings.app_env} boot: {e}"
            logger.critical(f"FATAL: {error_msg}")
            raise RuntimeError(error_msg) from e

    # Storage orphan cleanup and pre-flight directory check
    import os
    if hasattr(callmedex_container.storage_provider, "cleanup_stale_temp_files"):
        purged = callmedex_container.storage_provider.cleanup_stale_temp_files(max_age_seconds=3600.0)
        if purged > 0:
            logger.info(f"Startup pre-flight: Purged {purged} stale temporary report files")

    download_dir = getattr(callmedex_container.storage_provider, "download_dir", None)
    if download_dir and not os.access(download_dir, os.W_OK):
        logger.warning(f"Storage download directory '{download_dir}' is not writable")

    from app.integrations.callmedex.api.router import global_runner
    await callmedex_container.queue_engine.register_handler(
        "process_report", global_runner.execute_report_job
    )
    scheduler_service.start()
    await callmedex_container.queue_engine.start()
    yield
    # Shutdown
    logger.info("Shutting down MediAssist AI...")

    # Drain in-flight background work before the worker exits (KA-P1-04).
    # FastAPI BackgroundTasks and spawn_background_task() coroutines run on
    # this event loop AFTER the response is sent. A rolling deploy that tears
    # the loop down mid-flight abandons them; the durable-queue sweep then has
    # to recover the message, which is the slow path and — before T0.2 — the
    # lossy one. Bounded so a wedged task cannot block the deploy indefinitely.
    try:
        import asyncio as _asyncio
        from app.utils.async_tasks import _BACKGROUND_TASKS

        running_loop = _asyncio.get_running_loop()
        # Only tasks belonging to THIS loop can be awaited here. A task bound to
        # a different (or already closed) loop cannot be waited on — asyncio.wait
        # would raise or block until the timeout, delaying every shutdown.
        # _BACKGROUND_TASKS is a module-level set, so a foreign task is not
        # hypothetical: anything spawned under a previous loop stays in it.
        pending = [
            t
            for t in list(_BACKGROUND_TASKS)
            if not t.done() and t.get_loop() is running_loop
        ]
        if pending:
            logger.info(f"Draining {len(pending)} in-flight background task(s)...")
            done, still_running = await _asyncio.wait(pending, timeout=10.0)
            if still_running:
                logger.warning(
                    f"{len(still_running)} background task(s) did not finish within "
                    f"10s — the durable inbound queue will recover any dropped work"
                )
            else:
                logger.info("All in-flight background tasks drained cleanly")
    except Exception as drain_err:
        logger.warning(f"Background task drain failed: {drain_err}")

    await callmedex_container.queue_engine.shutdown()
    scheduler_service.shutdown()
    from connectors.runner import release_all_locks_held
    await release_all_locks_held()


# Create FastAPI app
# In non-development environments, disable interactive API docs to reduce attack surface
is_production = settings.app_env != "development"
app = FastAPI(
    title="Kriya AI",
    description="Hospital WhatsApp Assistant for appointment scheduling",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None if is_production else "/docs",
    redoc_url=None if is_production else "/redoc",
    openapi_url=None if is_production else "/openapi.json",
)

# CORS middleware — restricted to same-origin for security
# The admin panel is served from the same domain, so no cross-origin needed.
# If you deploy the admin panel separately, add that domain here.
allowed_origins = [
    f"http://localhost:{settings.app_port}",  # Local development
    "http://localhost:8000",  # Default dev port
    "http://127.0.0.1:8000",  # Alt local
]

# In production, add your actual deployed domain
# Example: "https://medassist-ai.onrender.com"
if settings.app_env == "production":
    # In production, only allow same-origin (no CORS needed since admin panel
    # is served from the same FastAPI server)
    allowed_origins = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Admin-Secret",
        "X-Integration-Secret",
    ],
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CorrelationIdMiddleware)


async def verify_metrics_auth(request: Request):
    """Authenticate /metrics scraping endpoint (T3.1 / KRIYA-009).

    Unauthenticated GET /metrics -> 401; authenticated -> 200.
    In development mode without metrics_token set, permits access for local diagnostics.
    In staging/production or when metrics_token is set, requires Bearer token or X-Metrics-Token header.
    """
    token = settings.metrics_token or settings.admin_secret or settings.meta_app_secret
    if settings.app_env == "development" and not settings.metrics_token:
        return True

    auth_header = request.headers.get("Authorization", "")
    x_token = request.headers.get("X-Metrics-Token", "")

    provided = ""
    if auth_header.startswith("Bearer "):
        provided = auth_header[7:].strip()
    elif x_token:
        provided = x_token.strip()

    import secrets
    if not provided or not token or not secrets.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="Unauthorized metrics access")
    return True


# Prometheus metrics endpoint (W5.2 / T3.1)
@app.get("/metrics", include_in_schema=False, dependencies=[Depends(verify_metrics_auth)])
async def metrics_endpoint():
    """Prometheus text format metrics export for scraping."""
    return PlainTextResponse(metrics.export_prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")

# Include routers
app.include_router(webhook.router)
app.include_router(health.router)
app.include_router(admin.router)
app.include_router(clinics.router)
app.include_router(platform.router)
# FHIR R4 interoperability API (HMIS / ABDM integration)
app.include_router(fhir_router)
# Razorpay payment webhook (/webhooks/razorpay)
app.include_router(razorpay_router)
# Internal integration API (connector → MedAssist)
app.include_router(integrations_router)
# CallMedex internal integration API (/internal/integrations/callmedex)
app.include_router(callmedex_router)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "Kriya AI",
        "version": "2.0.0",
        "hospital": settings.hospital_name,
        "status": "running",
    }


@app.get("/admin", include_in_schema=False)
async def admin_shortcut_redirect():
    """Redirect /admin to /admin-panel for user convenience."""
    return RedirectResponse(url="/admin-panel", status_code=307)


@app.get("/admin-panel")
async def admin_panel():

    """Serve admin panel HTML."""
    return FileResponse(
        "admin/index.html",
        media_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# NOTE: /admin-panel/admin.js is gone. admin/admin.js was a second copy of the
# panel that admin/index.html never loaded, and the two drifted apart more than
# once — session handling was fixed in one and not the other, and it still held
# the unscoped api() helpers that leaked across tenants. Serving a stale second
# frontend is a maintenance trap, not a feature. The panel is admin/index.html,
# which carries its own inline script.


@app.get("/platform-panel")
async def platform_panel():
    """Serve platform super-admin owner panel HTML."""
    return FileResponse(
        "admin/platform.html",
        media_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )



@app.get("/platform-panel/vendor/chart.umd.min.js")
async def platform_panel_chartjs():
    """Serve self-hosted Chart.js — CSP script-src is 'self' only, so this
    can't be loaded from a third-party CDN."""
    return FileResponse("admin/vendor/chart.umd.min.js", media_type="application/javascript")


@app.get("/ready", include_in_schema=False)
async def ready_probe():
    """Root-level readiness probe alias."""
    return await health.readiness_check()


@app.get("/live", include_in_schema=False)
async def live_probe():
    """Root-level liveness probe alias."""
    return await health.liveness_check()


from fastapi.responses import HTMLResponse as HTMLResp
from app.database import sb  # T5.1: off-loop query execution


@app.get("/privacy", response_class=HTMLResp, include_in_schema=False)
async def privacy_page():
    return HTMLResp("""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Privacy Policy - Kriya AI</title>
<style>
body{font-family:Arial,sans-serif;max-width:800px;margin:40px auto;padding:20px;color:#1e293b;line-height:1.7}
h1{color:#0d9488;border-bottom:2px solid #0d9488;padding-bottom:10px}
h2{color:#0f172a;margin-top:30px}
p{color:#475569}
</style>
</head>
<body>
<h1>Privacy Policy - Kriya AI</h1>
<p>Last updated: March 2026</p>
<h2>1. Information We Collect</h2>
<p>We collect your name, phone number, and appointment details 
when you interact with our WhatsApp bot for hospital appointment 
scheduling.</p>
<h2>2. WhatsApp Messaging</h2>
<p>We use Meta WhatsApp Cloud API to send appointment 
confirmations, reminders, and health information. All 
conversations are initiated by the patient messaging the 
hospital WhatsApp number first. You can opt out at any 
time by replying STOP.</p>
<h2>3. How We Use Your Data</h2>
<p>Your data is used only for appointment scheduling and 
hospital communication. We never sell or share your 
personal data with third parties.</p>
<h2>4. Data Retention</h2>
<p><strong>Clinical Records</strong> (appointments, lab reports, prescriptions):
Retained for <strong>7 years</strong> per National Medical Commission (NMC)
regulations. Upon deletion request, personal identifiers are anonymized
(replaced with [REDACTED]) while the clinical record is preserved for
regulatory audit purposes.</p>
<p><strong>Conversation History</strong> (WhatsApp chat sessions):
Automatically purged after <strong>30 days</strong> of inactivity to minimize
data retention per DPDP data minimization principles.</p>
<h2>5. Data Deletion</h2>
<p>Type DELETE MY DATA in WhatsApp to permanently delete 
all your data within 24 hours.</p>
<h2>6. India DPDP Act Compliance</h2>
<p>We comply with India Digital Personal Data Protection 
Act 2023. Explicit consent is collected before storing 
any personal information.</p>
<h2>7. Security</h2>
<p>All data is stored securely in encrypted databases 
using industry-standard security practices.</p>
<h2>8. Contact</h2>
<p>For privacy concerns message us on WhatsApp or 
contact the hospital directly.</p>
<p style="margin-top:40px;color:#94a3b8;font-size:13px">
Kriya AI - Hospital WhatsApp Assistant - 2026</p>
</body>
</html>""")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        reload=settings.app_env == "development",
    )
