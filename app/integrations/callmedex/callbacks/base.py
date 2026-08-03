"""Callback Contracts & Interface (Phase 2 Contract)."""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from app.integrations.callmedex.api.schemas import CallbackStatusPayload


class BaseCallbackHandler(ABC):
    """Abstract Base Class for dispatching and processing CallMedex status callbacks.

    Handles sending signed status notifications (completion, failure, retries)
    back to CallMedex or external management endpoints.
    """

    @abstractmethod
    async def send_status_callback(
        self, payload: CallbackStatusPayload
    ) -> bool:
        """Dispatch a signed status callback webhook to the configured callback endpoint.

        Args:
            payload: Typed callback status payload.

        Returns:
            bool: True if callback was successfully delivered and acknowledged.
        """
        pass

    @abstractmethod
    async def verify_signature(
        self, raw_body: bytes, signature_header: str
    ) -> bool:
        """Verify HMAC-SHA256 signature of incoming callback payloads.

        Args:
            raw_body: Raw request body bytes.
            signature_header: Received signature header value.

        Returns:
            bool: True if signature is valid, False otherwise.
        """
        pass
