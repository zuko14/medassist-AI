"""ABDM (Ayushman Bharat Digital Mission) Integration Service.

Provides ABHA (Ayushman Bharat Health Account) ID verification and QR code
parsing for MediAssist AI WhatsApp workflows.

Capabilities:
  - verify_abha_id()   — Validate 14-digit ABHA IDs against ABDM gateway
  - parse_abha_qr()    — Parse ABHA QR code payloads (base64 encoded JSON)
  - fetch_abha_profile() — Pull patient registration data from ABDM

Configuration (via .env):
  ABDM_CLIENT_ID       — Your ABDM integration client ID
  ABDM_CLIENT_SECRET   — Your ABDM integration client secret
  ABDM_BASE_URL        — Defaults to sandbox: https://abhasbx.abdm.gov.in/abha/api

ABDM Sandbox API Docs: https://sandbox.abdm.gov.in/docs/
"""

import base64
import json
import logging
import re
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ABHA ID: exactly 14 digits
_ABHA_FORMAT_PATTERN = re.compile(r"^\d{14}$")

# ABDM auth token cache (in-memory, expires in ~10 min)
_token_cache: dict = {"token": None, "expires_at": 0}


class ABDMService:
    """Service for ABDM / ABHA integration."""

    def __init__(self):
        self.base_url = getattr(settings, "abdm_base_url", "https://abhasbx.abdm.gov.in/abha/api")
        self.client_id = getattr(settings, "abdm_client_id", "")
        self.client_secret = getattr(settings, "abdm_client_secret", "")
        self._configured = bool(self.client_id and self.client_secret)

    def is_configured(self) -> bool:
        """Return True if ABDM credentials are configured."""
        return self._configured

    def validate_abha_format(self, abha_id: str) -> bool:
        """Check if a string matches the 14-digit ABHA ID format."""
        cleaned = abha_id.strip().replace("-", "").replace(" ", "")
        return bool(_ABHA_FORMAT_PATTERN.match(cleaned))

    async def verify_abha_id(self, abha_id: str) -> dict:
        """Validate a 14-digit ABHA ID against the ABDM gateway.

        Args:
            abha_id: 14-digit ABHA health account ID.

        Returns:
            Dict with keys:
              - valid (bool): Whether the ABHA ID is active and valid.
              - name (str | None): Patient's name from ABDM if valid.
              - error (str | None): Error message if invalid or unavailable.

        Note:
            If ABDM credentials are not configured, returns a graceful
            fallback indicating ABDM is not enabled.
        """
        cleaned = abha_id.strip().replace("-", "").replace(" ", "")

        # Format validation first
        if not self.validate_abha_format(cleaned):
            return {
                "valid": False,
                "name": None,
                "error": "ABHA ID must be exactly 14 digits.",
            }

        # If not configured, return graceful fallback
        if not self._configured:
            logger.warning("ABDM credentials not configured. Skipping live verification.")
            return {
                "valid": True,  # Optimistic — format is correct
                "name": None,
                "error": None,
                "note": "ABDM live verification not enabled. Format validated only.",
            }

        try:
            token = await self._get_auth_token()
            if not token:
                return {"valid": False, "name": None, "error": "ABDM auth failed."}

            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.base_url}/v3/profile/account",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-HIP-ID": self.client_id,
                        "ABHA-Address": cleaned,
                        "Accept": "application/json",
                    },
                )

            if response.status_code == 200:
                data = response.json()
                return {
                    "valid": True,
                    "name": data.get("name") or data.get("firstName", ""),
                    "abha_address": data.get("abhaAddress"),
                    "gender": data.get("gender"),
                    "raw": data,
                    "error": None,
                }
            elif response.status_code == 404:
                return {"valid": False, "name": None, "error": "ABHA ID not found."}
            else:
                logger.error(f"ABDM verify error: {response.status_code} {response.text[:200]}")
                return {"valid": False, "name": None, "error": "ABDM verification unavailable."}

        except httpx.TimeoutException:
            logger.warning("ABDM verify timeout — failing open")
            return {"valid": True, "name": None, "error": None, "note": "ABDM timeout — format valid only."}
        except Exception as e:
            logger.error(f"ABDM verify exception: {e}")
            return {"valid": False, "name": None, "error": "ABDM service error."}

    def parse_abha_qr(self, qr_data: str) -> dict:
        """Parse an ABHA QR code payload.

        ABHA QR codes contain a base64-encoded JSON payload with patient
        registration data.

        Args:
            qr_data: Raw QR code string scanned from the ABHA card.

        Returns:
            Dict with parsed fields:
              - abha_id, name, gender, dob, mobile (when available)
              - raw: full decoded payload
              - error: error message if parsing failed
        """
        try:
            # Try base64 decode first
            try:
                decoded_bytes = base64.b64decode(qr_data + "==")  # pad for safety
                payload = json.loads(decoded_bytes.decode("utf-8"))
            except Exception:
                # If base64 fails, try raw JSON
                payload = json.loads(qr_data)

            return {
                "abha_id": payload.get("hidn") or payload.get("abhaNumber"),
                "name": payload.get("name") or payload.get("patientName"),
                "gender": payload.get("gender"),
                "dob": payload.get("dob") or payload.get("dateOfBirth"),
                "mobile": payload.get("mobile"),
                "address": payload.get("address"),
                "raw": payload,
                "error": None,
            }

        except Exception as e:
            logger.warning(f"ABHA QR parse failed: {e}")
            return {
                "abha_id": None,
                "name": None,
                "error": f"Could not parse ABHA QR code: {str(e)[:100]}",
            }

    async def _get_auth_token(self) -> Optional[str]:
        """Get ABDM auth token, using cache to avoid re-auth on every call."""
        import time

        if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["token"]

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # ABDM uses client_credentials flow
                response = await client.post(
                    "https://dev.abdm.gov.in/gateway/v0.5/sessions",
                    json={
                        "clientId": self.client_id,
                        "clientSecret": self.client_secret,
                    },
                    headers={"Content-Type": "application/json"},
                )

            if response.status_code == 200:
                data = response.json()
                token = data.get("accessToken")
                expires_in = data.get("expiresIn", 600)  # default 10 min
                _token_cache["token"] = token
                _token_cache["expires_at"] = time.time() + expires_in - 30  # 30s buffer
                return token
            else:
                logger.error(f"ABDM auth failed: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"ABDM auth error: {e}")
            return None


# Global service instance
abdm_service = ABDMService()
