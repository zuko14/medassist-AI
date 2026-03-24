"""Health check router."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse, HTMLResponse
import datetime as dt

from app.database import supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
@router.head("")
async def health_check():
    """Basic health check endpoint."""
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "service": "MediAssist AI",
            "timestamp": dt.datetime.utcnow().isoformat()
        }
    )


@router.get("/ready")
async def readiness_check():
    """Readiness check with database connectivity."""
    try:
        result = supabase.table("patients").select("count", count="exact").limit(1).execute()
        return {
            "status": "ready",
            "database": "connected",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        return {
            "status": "not_ready",
            "database": "disconnected",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


@router.get("/live")
async def liveness_check():
    """Liveness check - basic service health."""
    return {
        "status": "alive",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }



@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Privacy Policy - MediAssist AI</title>
<style>
  body { font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; color: #1e293b; line-height: 1.7; }
  h1 { color: #0d9488; border-bottom: 2px solid #0d9488; padding-bottom: 10px; }
  h2 { color: #0f172a; margin-top: 30px; }
  p { color: #475569; }
</style>
</head>
<body>
<h1>Privacy Policy — MediAssist AI</h1>
<p>Last updated: March 2026</p>

<h2>1. Information We Collect</h2>
<p>We collect your name, phone number, and appointment details when you interact with our WhatsApp bot for hospital appointment scheduling.</p>

<h2>2. WhatsApp Messaging</h2>
<p>We use Meta WhatsApp Cloud API to send appointment confirmations, reminders, and health information. All conversations are initiated by the patient messaging the hospital WhatsApp number first. You can opt out at any time by replying STOP to any message.</p>

<h2>3. How We Use Your Data</h2>
<p>Your data is used only for appointment scheduling and hospital communication. We never sell, rent, or share your personal data with third parties outside of the hospital you are booking with.</p>

<h2>4. Data Retention</h2>
<p>Your data is retained for 12 months after your last interaction. You may request deletion at any time.</p>

<h2>5. Data Deletion</h2>
<p>You can request complete deletion of your data at any time by typing DELETE MY DATA in the WhatsApp chat. Your data will be permanently deleted within 24 hours and you will receive a confirmation message.</p>

<h2>6. India DPDP Act Compliance</h2>
<p>We comply with India's Digital Personal Data Protection Act 2023. You have the right to access, correct, and delete your personal data. Explicit consent is collected before storing any personal information.</p>

<h2>7. Security</h2>
<p>All data is stored securely in encrypted databases. We use industry-standard security practices to protect your information.</p>

<h2>8. Contact</h2>
<p>For privacy concerns or data requests, message us on WhatsApp or contact the hospital directly.</p>

<p style="margin-top:40px; color:#94a3b8; font-size:13px;">MediAssist AI · Hospital WhatsApp Assistant · 2026</p>
</body>
</html>"""