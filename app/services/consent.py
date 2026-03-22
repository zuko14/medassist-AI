"""Consent management service for DPDP compliance."""

import logging
from datetime import datetime, timezone
from typing import Optional

from app.database import get_patient_by_phone, update_patient, delete_patient_data

logger = logging.getLogger(__name__)


class ConsentService:
    """Service for managing patient consent."""

    async def has_consent(self, phone: str) -> bool:
        """Check if patient has given data consent."""
        patient = await get_patient_by_phone(phone)
        if not patient:
            return False
        return patient.get("data_consent", False)

    async def request_consent(self, phone: str) -> bool:
        """Request consent from patient."""
        # This is handled in the conversation flow
        return True

    async def grant_consent(self, phone: str) -> bool:
        """Record consent grant."""
        return await update_patient(phone, {
            "data_consent": True,
            "data_consent_at": "now()"
        })

    async def revoke_consent(self, phone: str) -> bool:
        """Revoke consent."""
        return await update_patient(phone, {
            "data_consent": False,
            "data_consent_at": None
        })

    async def delete_data(self, phone: str) -> dict:
        """Delete all patient data (DPDP right to erasure)."""
        try:
            # Get patient before deletion for confirmation
            patient = await get_patient_by_phone(phone)
            if not patient:
                return {"success": False, "error": "Patient not found"}

            # Delete data
            success = await delete_patient_data(phone)

            if success:
                # Generate deletion reference
                import uuid
                ref = str(uuid.uuid4())[:8].upper()

                return {
                    "success": True,
                    "deletion_ref": ref,
                    "deleted_at": datetime.now(timezone.utc).isoformat()
                }
            else:
                return {"success": False, "error": "Deletion failed"}

        except Exception as e:
            logger.error(f"Error deleting patient data: {e}")
            return {"success": False, "error": str(e)}

    async def get_consent_status(self, phone: str) -> dict:
        """Get full consent status for a patient."""
        patient = await get_patient_by_phone(phone)
        if not patient:
            return {
                "exists": False,
                "opted_in": False,
                "data_consent": False
            }

        return {
            "exists": True,
            "opted_in": patient.get("opted_in", False),
            "opted_in_at": patient.get("opted_in_at"),
            "opted_out_at": patient.get("opted_out_at"),
            "data_consent": patient.get("data_consent", False),
            "data_consent_at": patient.get("data_consent_at")
        }


# Global instance
consent_service = ConsentService()
