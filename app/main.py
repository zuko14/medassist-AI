"""MediAssist AI - Hospital WhatsApp Assistant — Security Hardened."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.routers import webhook, health, admin, clinics
from app.routers.fhir import router as fhir_router
from app.routers.razorpay_webhook import router as razorpay_router
from app.services.scheduler import scheduler_service
from app.utils.logger import setup_logging
from app.utils.security import SECURITY_HEADERS

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)


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
    if settings.app_env != "production":
        logger.info("🔓 Running in DEVELOPMENT mode — /webhook/test endpoint is ENABLED")
    else:
        logger.info("🔒 Running in PRODUCTION mode — /webhook/test endpoint is DISABLED")

    scheduler_service.start()
    yield
    # Shutdown
    logger.info("Shutting down MediAssist AI...")
    scheduler_service.shutdown()


# Create FastAPI app
# In production, disable interactive API docs to reduce attack surface
is_production = settings.app_env == "production"
app = FastAPI(
    title="MediAssist AI",
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
    f"http://localhost:{settings.app_port}",   # Local development
    "http://localhost:8000",                     # Default dev port
    "http://127.0.0.1:8000",                    # Alt local
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
    allow_headers=["Authorization", "Content-Type", "X-Admin-Secret"],
)

# Security headers middleware (replaces old NgrokMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# Include routers
app.include_router(webhook.router)
app.include_router(health.router)
app.include_router(admin.router)
app.include_router(clinics.router)
# FHIR R4 interoperability API (HMIS / ABDM integration)
app.include_router(fhir_router)
# Razorpay payment webhook (/webhooks/razorpay)
app.include_router(razorpay_router)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "MediAssist AI",
        "version": "2.0.0",
        "hospital": settings.hospital_name,
        "status": "running"
    }


@app.get("/admin-panel")
async def admin_panel():
    """Serve admin panel HTML."""
    return FileResponse("admin/index.html", media_type="text/html")


from fastapi.responses import HTMLResponse as HTMLResp

@app.get("/privacy", response_class=HTMLResp, include_in_schema=False)
async def privacy_page():
    return HTMLResp("""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Privacy Policy - MediAssist AI</title>
<style>
body{font-family:Arial,sans-serif;max-width:800px;margin:40px auto;padding:20px;color:#1e293b;line-height:1.7}
h1{color:#0d9488;border-bottom:2px solid #0d9488;padding-bottom:10px}
h2{color:#0f172a;margin-top:30px}
p{color:#475569}
</style>
</head>
<body>
<h1>Privacy Policy - MediAssist AI</h1>
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
MediAssist AI - Hospital WhatsApp Assistant - 2026</p>
</body>
</html>""")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        reload=settings.app_env == "development"
    )
