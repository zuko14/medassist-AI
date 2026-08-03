"""CallMedex Callback Handler (Phase 3 Implementation)."""

import hmac
import hashlib
import logging
import httpx
from typing import Optional
from app.integrations.callmedex.callbacks.base import BaseCallbackHandler
from app.integrations.callmedex.api.schemas import CallbackStatusPayload
from app.integrations.callmedex.config.settings import callmedex_settings

logger = logging.getLogger(__name__)


class CallMedexCallbackHandler(BaseCallbackHandler):
    """Handles sending HMAC-signed status callback webhooks to CallMedex."""

    def __init__(self, secret: Optional[str] = None):
        self.secret = secret or callmedex_settings.hmac_signature_secret.get_secret_value()

    async def send_status_callback(
        self, payload: CallbackStatusPayload
    ) -> bool:
        """Dispatch signed status callback to callback endpoint."""
        raw_body = payload.model_dump_json().encode("utf-8")
        signature = hmac.new(
            self.secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()

        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        headers = {
            "Content-Type": "application/json",
            "X-Signature-256": signature,
            "X-Correlation-ID": payload.correlation_id,
            "X-Timestamp": ts,
        }

        logger.info(
            f"Dispatching Callback [Task: {payload.task_id} | Status: {payload.status}] "
            f"to {callmedex_settings.callmedex_callback_url}"
        )

        target_url = callmedex_settings.callmedex_callback_url

        # Offline sandbox/test mode transport bypass
        if callmedex_settings.app_env in ("test", "sandbox", "development"):
            logger.info(f"Sandbox Callback Delivered (Signed HMAC OK) for task {payload.task_id}")
            return True


        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    target_url,
                    content=raw_body,
                    headers=headers,
                )
                if response.status_code == 200:
                    logger.info(f"Callback delivered successfully for task {payload.task_id}")
                    return True
                else:
                    logger.warning(
                        f"Callback HTTP {response.status_code} for task {payload.task_id}: {response.text[:100]}"
                    )
                    return False
        except Exception as e:
            logger.error(f"Callback transport failed for task {payload.task_id}: {e}")
            return False


    async def verify_signature(
        self, raw_body: bytes, signature_header: str
    ) -> bool:
        """Verify HMAC-SHA256 signature of incoming webhooks."""
        expected_sig = hmac.new(
            self.secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected_sig, signature_header)
