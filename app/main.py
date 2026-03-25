"""MediAssist AI - Hospital WhatsApp Assistant."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.routers import webhook, health, admin
from app.services.scheduler import scheduler_service
from app.utils.logger import setup_logging

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)


class NgrokMiddleware(BaseHTTPMiddleware):
    """Middleware to skip ngrok browser warning page."""

    async def dispatch(self, request, call_next):
        """Add ngrok-skip-browser-warning header to every response."""
        response = await call_next(request)
        response.headers["ngrok-skip-browser-warning"] = "1"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting MediAssist AI...")
    scheduler_service.start()
    yield
    # Shutdown
    logger.info("Shutting down MediAssist AI...")
    scheduler_service.shutdown()


# Create FastAPI app
app = FastAPI(
    title="MediAssist AI",
    description="Hospital WhatsApp Assistant for appointment scheduling",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ngrok skip browser warning middleware
app.add_middleware(NgrokMiddleware)

# Include routers
app.include_router(webhook.router)
app.include_router(health.router)
app.include_router(admin.router)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "MediAssist AI",
        "version": "1.0.0",
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
<p>Your data is retained for 12 months after your last 
interaction. You may request deletion at any time.</p>
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
