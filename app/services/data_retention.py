"""Data Retention Service for MediAssist AI.

Implements a dual-tier retention strategy compliant with:
  - India DPDP Act 2023 (data minimization, right to erasure)
  - National Medical Commission (NMC) clinical record regulations (7 years)

Tier 1 — Clinical Records (appointments, lab_reports, prescriptions):
  Retained for 7 years per NMC mandate.
  When a patient requests data deletion, PII fields are ANONYMIZED
  (replaced with [REDACTED]) but the clinical record structure is preserved.
  This satisfies both DPDP erasure rights and NMC audit requirements.

Tier 2 — Conversation Logs (conversations table):
  Purged after 30 days of inactivity.
  These are transient session/chat data with no clinical significance.

Usage:
  - Scheduled job calls purge_expired_conversations() daily at 2 AM.
  - delete_patient_data() in database.py calls anonymize_clinical_records().
"""

import logging
from datetime import datetime, timedelta, timezone

from app.database import supabase
from app.config import settings

logger = logging.getLogger(__name__)

# ── Retention Configuration ────────────────────────────────────────────────────
# Can be overridden in .env when available in settings
CLINICAL_RETENTION_YEARS: int = getattr(settings, "clinical_retention_years", 7)
CONVERSATION_PURGE_DAYS: int = getattr(settings, "conversation_purge_days", 30)
ANALYTICS_PURGE_MONTHS: int = 12  # Analytics events purged after 12 months


class DataRetentionService:
    """Manages dual-tier data retention per NMC + DPDP requirements."""

    # ── Tier 2: Conversation / Session Purge ──────────────────────────────────

    async def purge_expired_conversations(self) -> int:
        """Delete conversation sessions older than CONVERSATION_PURGE_DAYS.

        Conversations contain transient chat state — not clinical data.
        Purging them keeps the DB lean and complies with DPDP minimization.

        Returns:
            Number of conversation records purged.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=CONVERSATION_PURGE_DAYS)
        ).isoformat()

        try:
            result = (
                supabase.table("conversations")
                .delete()
                .lt("updated_at", cutoff)
                .execute()
            )
            count = len(result.data) if result.data else 0
            if count > 0:
                logger.info(
                    f"Data retention: purged {count} expired conversation sessions "
                    f"(older than {CONVERSATION_PURGE_DAYS} days)"
                )
            return count
        except Exception as e:
            logger.error(f"Conversation purge error: {e}")
            return 0

    async def purge_expired_session_data(self) -> int:
        """Delete analytics events older than ANALYTICS_PURGE_MONTHS.

        Analytics are operational metrics — not clinical records.
        12-month rolling window is sufficient for business reporting.

        Returns:
            Number of analytics event records purged.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=ANALYTICS_PURGE_MONTHS * 30)
        ).isoformat()

        try:
            result = (
                supabase.table("analytics_events")
                .delete()
                .lt("created_at", cutoff)
                .execute()
            )
            count = len(result.data) if result.data else 0
            if count > 0:
                logger.info(
                    f"Data retention: purged {count} expired analytics events "
                    f"(older than {ANALYTICS_PURGE_MONTHS} months)"
                )
            return count
        except Exception as e:
            logger.error(f"Analytics purge error: {e}")
            return 0

    # ── Tier 1: Clinical Record Anonymization ─────────────────────────────────

    async def anonymize_clinical_records(self, clinic_id: str, phone: str) -> dict:
        """Anonymize (not delete) clinical records when a patient requests data deletion.

        DPDP Act gives patients the right to erasure. However, NMC regulations
        mandate that clinical records (diagnoses, treatments, prescriptions) are
        retained for 7 years.

        This function resolves the conflict: PII (name, phone) is replaced with
        [REDACTED], while the clinical structure (department visited, appointment
        date, doctor seen) is preserved for audit/regulatory purposes.

        Args:
            clinic_id: Clinic scope for the operation.
            phone: Patient's phone number.

        Returns:
            Dict with counts of records anonymized per table.
        """
        results = {
            "appointments_anonymized": 0,
            "lab_reports_anonymized": 0,
            "prescriptions_anonymized": 0,
            "errors": [],
        }

        # Get patient record first
        try:
            patient_res = (
                supabase.table("patients")
                .select("id, name")
                .eq("clinic_id", clinic_id)
                .eq("phone", phone)
                .execute()
            )
            patient = patient_res.data[0] if patient_res.data else None
        except Exception as e:
            logger.error(f"Anonymization: could not fetch patient: {e}")
            results["errors"].append(str(e))
            return results

        if not patient:
            return results

        patient.get("id")
        datetime.now(timezone.utc).isoformat()

        # 1. Anonymize appointments — preserve department, doctor, date, status
        try:
            appt_res = (
                supabase.table("appointments")
                .update(
                    {
                        "patient_name": "[REDACTED]",
                        "patient_phone": "[REDACTED]",
                        "symptoms": "[REDACTED]",
                        "notes": "[REDACTED]",
                    }
                )
                .eq("clinic_id", clinic_id)
                .eq("patient_phone", phone)
                .execute()
            )
            results["appointments_anonymized"] = len(appt_res.data or [])
        except Exception as e:
            logger.error(f"Appointment anonymization error: {e}")
            results["errors"].append(f"appointments: {e}")

        # 2. Anonymize lab reports — preserve report_type, created_at
        try:
            lr_res = (
                supabase.table("lab_reports")
                .update(
                    {
                        "patient_name": "[REDACTED]",
                        "patient_phone": "[REDACTED]",
                        "file_url": "[REDACTED]",
                    }
                )
                .eq("clinic_id", clinic_id)
                .eq("patient_phone", phone)
                .execute()
            )
            results["lab_reports_anonymized"] = len(lr_res.data or [])
        except Exception as e:
            # Lab reports table might not exist in all deployments
            logger.debug(f"Lab reports anonymization skipped: {e}")

        # 3. Anonymize prescriptions — preserve medicine class, frequency (not name)
        try:
            rx_res = (
                supabase.table("prescriptions")
                .update(
                    {
                        "patient_name": "[REDACTED]",
                        "patient_phone": "[REDACTED]",
                        "notes": "[REDACTED]",
                    }
                )
                .eq("clinic_id", clinic_id)
                .eq("patient_phone", phone)
                .execute()
            )
            results["prescriptions_anonymized"] = len(rx_res.data or [])
        except Exception as e:
            logger.debug(f"Prescriptions anonymization skipped: {e}")

        # 4. Mark the patient row itself as anonymized (but keep the shell for FK integrity)
        try:
            supabase.table("patients").update(
                {
                    "name": "[REDACTED]",
                    "opted_in": False,
                    "data_consent": False,
                }
            ).eq("clinic_id", clinic_id).eq("phone", phone).execute()
        except Exception as e:
            logger.error(f"Patient anonymization error: {e}")
            results["errors"].append(f"patient_row: {e}")

        logger.info(
            f"Data retention: anonymized records for phone={phone[:6]}*** "
            f"in clinic={clinic_id}: {results}"
        )
        return results

    async def get_retention_status(self, clinic_id: str, phone: str) -> dict:
        """Return retention tier status for a patient's data.

        Useful for privacy dashboard and audit logs.
        """
        try:
            appt_res = (
                supabase.table("appointments")
                .select("id, appointment_date, status")
                .eq("clinic_id", clinic_id)
                .eq("patient_phone", phone)
                .execute()
            )
            appointments = appt_res.data or []

            # Find oldest record for retention expiry calculation
            oldest_date = None
            for appt in appointments:
                d = appt.get("appointment_date")
                if d and (oldest_date is None or d < oldest_date):
                    oldest_date = d

            retention_expires = None
            if oldest_date:
                from datetime import date

                try:
                    oldest = date.fromisoformat(str(oldest_date))
                    retention_expires = datetime(
                        oldest.year + CLINICAL_RETENTION_YEARS, oldest.month, oldest.day
                    ).isoformat()
                except Exception:
                    pass

            return {
                "phone": phone[:6] + "***",
                "clinical_records_count": len(appointments),
                "clinical_retention_years": CLINICAL_RETENTION_YEARS,
                "clinical_retention_expires": retention_expires,
                "conversation_purge_days": CONVERSATION_PURGE_DAYS,
            }

        except Exception as e:
            logger.error(f"Retention status error: {e}")
            return {"error": str(e)}


# Global service instance
data_retention_service = DataRetentionService()
