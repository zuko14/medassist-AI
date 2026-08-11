"""Bi-directional HMIS API Bridge for MediAssist AI.

Enables two-way data sync between MediAssist and external Hospital Management
Information Systems (HMIS), eliminating double data entry for receptionists.

Sync direction:
  MediAssist → HMIS:  push_appointment_to_hmis()  (new bookings, cancellations)
  HMIS → MediAssist:  pull_roster_from_hmis()      (doctor availability, slots)
  Both directions:    sync_patient_registration()   (patient demographics)

Configuration (per clinic in clinics.config JSONB):
  {
    "hmis_webhook_url":    "https://hospital-hmis.com/api/webhooks/appointments",
    "hmis_api_key":        "your-hmis-api-key",
    "hmis_sync_enabled":   true,
    "hmis_system":         "HIS-Pro | eHospital | Insta HMS | custom"
  }
"""

import logging

import httpx

logger = logging.getLogger(__name__)

# HMIS sync timeout — must be short to not block WhatsApp response
_HMIS_TIMEOUT_SECONDS = 5


class HMISBridge:
    """Service for bi-directional HMIS data synchronization."""

    def is_enabled(self, clinic: dict) -> bool:
        """Check if HMIS sync is configured and enabled for a clinic."""
        config = clinic.get("config", {}) or {}
        return bool(config.get("hmis_sync_enabled") and config.get("hmis_webhook_url"))

    async def push_appointment_to_hmis(self, clinic: dict, appointment: dict) -> bool:
        """Push a new or updated appointment to the clinic's external HMIS.

        Fires-and-forgets after MediAssist books the appointment. The
        HMIS receives the booking in FHIR-compatible format so receptionists
        see the appointment in their existing system immediately.

        Args:
            clinic: Clinic config dict (from clinics table).
            appointment: Appointment row from Supabase.

        Returns:
            True if HMIS accepted, False on error (non-blocking — we don't
            fail the booking if HMIS is down).
        """
        if not self.is_enabled(clinic):
            return True  # Not configured — silently skip

        config = clinic.get("config", {}) or {}
        webhook_url = config.get("hmis_webhook_url")
        api_key = config.get("hmis_api_key", "")

        # Build the payload in a common FHIR-like format most HMIS accept
        payload = {
            "event": "appointment.created",
            "source": "Kriya-AI",
            "clinic_id": str(clinic.get("id", "")),
            "appointment": {
                "id": str(appointment.get("id", "")),
                "booking_ref": appointment.get("booking_ref"),
                "patient_name": appointment.get("patient_name"),
                "patient_phone": appointment.get("patient_phone"),
                "department": appointment.get("department"),
                "doctor_name": appointment.get("doctor_name"),
                "date": str(appointment.get("appointment_date", "")),
                "time": str(appointment.get("appointment_time", ""))[:5],
                "status": appointment.get("status", "confirmed"),
                "symptoms": appointment.get("symptoms"),
                "booked_via": "WhatsApp",
            },
        }

        try:
            async with httpx.AsyncClient(timeout=_HMIS_TIMEOUT_SECONDS) as client:
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

                response = await client.post(
                    webhook_url,
                    json=payload,
                    headers=headers,
                )

            if response.status_code in (200, 201, 202):
                logger.info(
                    f"HMIS sync: appointment {appointment.get('booking_ref')} "
                    f"pushed to {clinic.get('name')} HMIS"
                )
                return True
            else:
                logger.warning(
                    f"HMIS push failed for {clinic.get('name')}: "
                    f"HTTP {response.status_code}"
                )
                return False

        except httpx.TimeoutException:
            logger.warning(f"HMIS push timeout for {clinic.get('name')} — skipping")
            return False
        except Exception as e:
            logger.error(f"HMIS push error for {clinic.get('name')}: {e}")
            return False

    async def pull_roster_from_hmis(self, clinic: dict) -> list[dict]:
        """Pull doctor roster and availability from the clinic's external HMIS.

        Called by the scheduler to keep MediAssist's doctors table in sync
        with what the HMIS shows.

        Args:
            clinic: Clinic config dict.

        Returns:
            List of doctor dicts (empty if HMIS sync not enabled or failed).
        """
        if not self.is_enabled(clinic):
            return []

        config = clinic.get("config", {}) or {}
        base_url = config.get("hmis_webhook_url", "").rstrip("/")
        roster_path = config.get("hmis_roster_path", "/roster/doctors")
        api_key = config.get("hmis_api_key", "")

        roster_url = base_url.replace("/webhooks/appointments", "") + roster_path

        try:
            async with httpx.AsyncClient(timeout=_HMIS_TIMEOUT_SECONDS) as client:
                headers = {}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

                response = await client.get(roster_url, headers=headers)

            if response.status_code == 200:
                data = response.json()
                # Normalize: some HMIS return {doctors: [...]} others return [...]
                doctors = data if isinstance(data, list) else data.get("doctors", [])
                logger.info(
                    f"HMIS roster: pulled {len(doctors)} doctors for {clinic.get('name')}"
                )
                return doctors
            else:
                logger.warning(
                    f"HMIS roster pull failed for {clinic.get('name')}: "
                    f"HTTP {response.status_code}"
                )
                return []

        except Exception as e:
            logger.error(f"HMIS roster pull error for {clinic.get('name')}: {e}")
            return []

    async def sync_patient_registration(self, clinic: dict, patient: dict) -> dict:
        """Sync patient registration data with external HMIS.

        Pushes MediAssist patient data to HMIS when a new patient registers
        (gives consent), so they appear in the hospital's central registry.

        Args:
            clinic: Clinic config dict.
            patient: Patient row from Supabase.

        Returns:
            Dict with:
              - success (bool)
              - hmis_patient_id (str | None): External HMIS patient ID if returned.
              - error (str | None)
        """
        if not self.is_enabled(clinic):
            return {"success": True, "hmis_patient_id": None, "error": None}

        config = clinic.get("config", {}) or {}
        base_url = config.get("hmis_webhook_url", "").rstrip("/")
        patient_path = config.get("hmis_patient_path", "/patients")
        api_key = config.get("hmis_api_key", "")

        patient_url = base_url.replace("/webhooks/appointments", "") + patient_path

        payload = {
            "source": "Kriya-AI",
            "clinic_id": str(clinic.get("id", "")),
            "patient": {
                "id": str(patient.get("id", "")),
                "name": patient.get("name"),
                "phone": patient.get("phone"),
                "language": patient.get("language"),
                "registered_via": "WhatsApp",
                "consent_given": patient.get("data_consent", False),
            },
        }

        try:
            async with httpx.AsyncClient(timeout=_HMIS_TIMEOUT_SECONDS) as client:
                headers = {"Content-Type": "application/json"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"

                response = await client.post(
                    patient_url,
                    json=payload,
                    headers=headers,
                )

            if response.status_code in (200, 201, 202):
                data = response.json()
                hmis_id = data.get("patient_id") or data.get("id")
                logger.info(
                    f"HMIS patient sync: {patient.get('phone')} → {clinic.get('name')} HMIS"
                )
                return {"success": True, "hmis_patient_id": hmis_id, "error": None}
            else:
                return {
                    "success": False,
                    "hmis_patient_id": None,
                    "error": f"HMIS HTTP {response.status_code}",
                }

        except Exception as e:
            logger.error(f"HMIS patient sync error: {e}")
            return {"success": False, "hmis_patient_id": None, "error": str(e)[:100]}


# Global service instance
hmis_bridge = HMISBridge()
