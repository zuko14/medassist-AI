"""Signed HTTP client for the API CallMedex exposes to Kriya (MediAssist).

Wire format is taken from CallMedex's verifier, not its draft OpenAPI file
(backend/app/middleware/mediassist_auth.py::verify_mediassist_signature):

    Authorization: Bearer <token>
    X-Timestamp:   unix epoch seconds (integer string)
    X-Signature:   sha256=<hex HMAC-SHA256(secret, f"{ts}." + raw_query + raw_body)>
    X-Correlation-Id / X-Idempotency-Key on every call

The secret is the one shared secret both directions use
(CALLMEDEX_HMAC_SIGNATURE_SECRET here, MEDIASSIST_HMAC_SECRET there).

Never raises: callers get the final httpx.Response, or None when CallMedex
is not configured or unreachable. Nothing here may break a patient flow.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.integrations.callmedex.config.settings import callmedex_settings

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/integrations/mediassist"

# Fixed namespace so the same logical event always yields the same key —
# CallMedex replays its cached response for a repeated key instead of
# re-applying the side effect.
_IDEMPOTENCY_NS = uuid.UUID("6f1c5d0e-2b7a-4e4f-9a57-cb1d0c2f7a11")


def idempotency_key(*parts: str) -> str:
    return str(uuid.uuid5(_IDEMPOTENCY_NS, ":".join(parts)))


def sign(secret: str, timestamp: str, query: str, body: bytes) -> str:
    message = f"{timestamp}.".encode() + query.encode() + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def is_configured() -> bool:
    return bool(callmedex_settings.callmedex_base_url.strip())


async def request(
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    idem_key: Optional[str] = None,
    correlation_id: Optional[str] = None,
    attempts: int = 3,
    timeout: float = 10.0,
) -> Optional[httpx.Response]:
    """Signed call to CallMedex. Retries only network errors and 5xx.

    Pass attempts=1 for non-idempotent-in-flight writes (booking creation):
    CallMedex caches the response only after the handler finishes, so a
    retry racing a slow first attempt could create a second booking.
    """
    base = callmedex_settings.callmedex_base_url.strip().rstrip("/")
    if not base:
        logger.info(f"CallMedex base URL not configured — skipping {method} {path}")
        return None

    body = b"" if json_body is None else json.dumps(json_body, separators=(",", ":")).encode()
    query = urlencode(params) if params else ""
    url = f"{base}{API_PREFIX}{path}" + (f"?{query}" if query else "")
    token = (
        callmedex_settings.outbound_bearer_token.get_secret_value()
        or callmedex_settings.bearer_token.get_secret_value()
    )
    secret = callmedex_settings.hmac_signature_secret.get_secret_value()
    corr = correlation_id or str(uuid.uuid4())

    resp: Optional[httpx.Response] = None
    for attempt in range(1, attempts + 1):
        ts = str(int(time.time()))  # re-stamped per attempt: 300s freshness window
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Timestamp": ts,
            "X-Signature": sign(secret, ts, query, body),
            "X-Correlation-Id": corr,
            "X-Idempotency-Key": idem_key or str(uuid.uuid4()),
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, content=body or None, headers=headers)
            if resp.status_code < 500:
                if resp.status_code >= 400:
                    logger.warning(
                        f"CallMedex {method} {path} -> HTTP {resp.status_code} "
                        f"[corr={corr}]: {resp.text[:200]}"
                    )
                return resp
            logger.warning(f"CallMedex {method} {path} -> HTTP {resp.status_code} (attempt {attempt}/{attempts})")
        except httpx.HTTPError as e:
            resp = None
            logger.warning(f"CallMedex {method} {path} transport error (attempt {attempt}/{attempts}): {e}")
        if attempt < attempts:
            await asyncio.sleep(2 ** (attempt - 1))
    return resp
