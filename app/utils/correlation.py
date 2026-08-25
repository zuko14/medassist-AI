"""Request Correlation ID Context & Logging Helper (W5.1).

Enables tracing a single transaction end-to-end from webhook ingress
through AI conversation, slot reservation, payment, and WhatsApp delivery.
"""

import contextvars
from typing import Optional
from uuid import uuid4

# Thread/Asyncio-safe correlation ID holder
correlation_id_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "correlation_id_ctx", default=None
)


def get_correlation_id() -> str:
    """Retrieve the current request's correlation ID or generate a fallback."""
    cid = correlation_id_ctx.get()
    if not cid:
        cid = f"cid_{uuid4().hex[:12]}"
        correlation_id_ctx.set(cid)
    return cid


def set_correlation_id(cid: str) -> None:
    """Set the correlation ID for the current async task context."""
    correlation_id_ctx.set(cid)
