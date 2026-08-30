"""Health check router."""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse, RedirectResponse

from app.database import supabase
from app.database import sb  # T5.1: off-loop query execution

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
@router.head("")
async def health_check():
    """Basic health check endpoint."""
    return JSONResponse(
        status_code=200,
        headers={"X-Process-Id": str(os.getpid())},
        content={
            "status": "ok",
            "service": "Kriya AI",
            "pid": os.getpid(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@router.get("/ready")
async def readiness_check():
    """Readiness check with database connectivity."""
    try:
        # unscoped: database liveness probe connectivity test
        result = await sb(supabase.table("patients").select("count", count="exact").limit(1))
        return {
            "status": "ready",
            "database": "connected",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        return {
            "status": "not_ready",
            "database": "disconnected",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


@router.get("/live")
async def liveness_check():
    """Liveness check - basic service health."""
    return {"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("/privacy")
async def privacy_policy():
    """Redirect to the canonical privacy policy at /privacy.

    This route used to serve its own copy of the policy text, which had
    drifted out of sync with the one at /privacy (wrong retention period).
    Redirecting keeps a single source of truth.
    """
    return RedirectResponse(url="/privacy")
