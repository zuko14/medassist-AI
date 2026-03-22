"""Health check router."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
import datetime as dt

from app.database import supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
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
        # Test database connection
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
