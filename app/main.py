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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        reload=settings.app_env == "development"
    )
