"""Consent management service for DPDP compliance.

Security fix: All methods now require clinic_id for proper multi-tenant
scoping. Previously, calls to get_patient_by_phone/update_patient without
clinic_id would query across all clinics — a data isolation bug in
multi-tenant mode.
"""

import logging
from datetime import datetime, timezone

from app.database import get_patient_by_phone, update_patient, delete_patient_data

logger = logging.getLogger(__name__)


class ConsentService:
    """Service for managing patient consent (multi-tenant aware)."""

    async def has_consent(self, clinic_id: str, phone: str) -> bool:
        """Check if patient has given data consent.

        Args:
            clinic_id: Tenant clinic ID (required for multi-tenant isolation).
            phone: Patient phone number.
        """
        patient = await get_patient_by_phone(clinic_id, phone)
        if not patient:
            return False
        return patient.get("data_consent", False)

    async def request_consent(self, clinic_id: str, phone: str) -> bool:
        """Request consent from patient (handled in conversation flow)."""
        return True

    async def grant_consent(self, clinic_id: str, phone: str) -> bool:
        """Record consent grant.

        Args:
            clinic_id: Tenant clinic ID.
            phone: Patient phone number.
        """
        return await update_patient(
            clinic_id, phone, {"data_consent": True, "data_consent_at": "now()"}
        )

    async def revoke_consent(self, clinic_id: str, phone: str) -> bool:
        """Revoke consent.

        Args:
            clinic_id: Tenant clinic ID.
            phone: Patient phone number.
        """
        return await update_patient(
            clinic_id, phone, {"data_consent": False, "data_consent_at": None}
        )

    async def delete_data(self, clinic_id: str, phone: str) -> dict:
        """Delete / anonymize all patient data (DPDP right to erasure).

        Uses tiered deletion: clinical records are anonymized (NMC mandate),
        conversation/session data is fully deleted (DPDP compliance).

        Args:
            clinic_id: Tenant clinic ID.
            phone: Patient phone number.
        """
        try:
            # Get patient before deletion for confirmation
            patient = await get_patient_by_phone(clinic_id, phone)
            if not patient:
                return {"success": False, "error": "Patient not found"}

            # Tiered delete / anonymize
            success = await delete_patient_data(clinic_id, phone)

            if success:
                import uuid

                ref = str(uuid.uuid4())[:8].upper()
                return {
                    "success": True,
                    "deletion_ref": ref,
                    "deleted_at": datetime.now(timezone.utc).isoformat(),
                    "note": (
                        "Chat history deleted. Clinical records (appointments, reports) "
                        "have been anonymized and retained per NMC 7-year mandate."
                    ),
                }
            else:
                return {"success": False, "error": "Deletion failed"}

        except Exception as e:
            logger.error(f"Error deleting patient data: {e}")
            return {"success": False, "error": str(e)}

    async def get_consent_status(self, clinic_id: str, phone: str) -> dict:
        """Get full consent status for a patient.

        Args:
            clinic_id: Tenant clinic ID.
            phone: Patient phone number.
        """
        patient = await get_patient_by_phone(clinic_id, phone)
        if not patient:
            return {"exists": False, "opted_in": False, "data_consent": False}

        return {
            "exists": True,
            "opted_in": patient.get("opted_in", False),
            "opted_in_at": patient.get("opted_in_at"),
            "opted_out_at": patient.get("opted_out_at"),
            "data_consent": patient.get("data_consent", False),
            "data_consent_at": patient.get("data_consent_at"),
        }


# Global instance
consent_service = ConsentService()
