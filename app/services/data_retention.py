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
from app.database import sb  # T5.1: off-loop query execution

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
                await sb(supabase.table("conversations")
                .delete()
                .lt("updated_at", cutoff))
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
                await sb(supabase.table("analytics_events")
                .delete()
                .lt("created_at", cutoff))
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

    async def purge_inbound_messages(self, days: int = 30) -> int:
        """Purge completed inbound_messages older than `days` (KA-P2-10).

        inbound_messages grows one row per patient message, forever. There was
        no purge job for it at all — at 10k messages/day that is ~3.6M rows a
        year on the hot deduplication path, holding a raw phone number each.

        Only 'completed' rows are removed. Anything in received / processing /
        failed_retryable / dead_letter is still live work or an operator's
        triage queue.

        30 days is deliberately generous: it must exceed both Meta's webhook
        retry window and the 30-minute DLQ give-up window, or purging would
        reopen the duplicate-delivery hole that message_id closes.

        Returns:
            Number of inbound queue records purged.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        try:
            result = (
                # unscoped: platform_sweep
                await sb(supabase.table("inbound_messages")
                .delete()
                .eq("status", "completed")
                .lt("completed_at", cutoff))
            )
            count = len(result.data) if result.data else 0
            if count > 0:
                logger.info(
                    f"Data retention: purged {count} completed inbound_messages "
                    f"(older than {days} days)"
                )
            return count
        except Exception as e:
            logger.error(f"Inbound messages purge error: {e}")
            return 0

    async def purge_failed_messages_dlq(self, days: int = 30) -> int:
        """Purge dead-letter failed_messages records older than `days` (default 30 days).

        Returns:
            Number of DLQ records purged.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()

        try:
            result = (
                await sb(supabase.table("failed_messages")
                .delete()
                .lt("created_at", cutoff))
            )
            count = len(result.data) if result.data else 0
            if count > 0:
                logger.info(
                    f"Data retention: purged {count} failed_messages DLQ records "
                    f"(older than {days} days)"
                )
            return count
        except Exception as e:
            logger.error(f"Failed messages DLQ purge error: {e}")
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
                await sb(supabase.table("patients")
                .select("id, name")
                .eq("clinic_id", clinic_id)
                .eq("phone", phone))
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
                await sb(supabase.table("appointments")
                .update(
                    {
                        "patient_name": "[REDACTED]",
                        "patient_phone": "[REDACTED]",
                        "symptoms": "[REDACTED]",
                        "notes": "[REDACTED]",
                    }
                )
                .eq("clinic_id", clinic_id)
                .eq("patient_phone", phone))
            )
            results["appointments_anonymized"] = len(appt_res.data or [])
        except Exception as e:
            logger.error(f"Appointment anonymization error: {e}")
            results["errors"].append(f"appointments: {e}")

        # 2. Anonymize lab reports — delete PDF from Storage & redact DB row
        try:
            # Fetch lab report file_path(s) before redacting DB row
            reports_res = (
                await sb(supabase.table("lab_reports")
                .select("id, file_path")
                .eq("clinic_id", clinic_id)
                .eq("patient_phone", phone))
            )
            file_paths_to_delete = [
                r["file_path"]
                for r in (reports_res.data or [])
                if r.get("file_path") and r["file_path"] != "[REDACTED]"
            ]

            # Remove objects from Supabase Storage
            if file_paths_to_delete:
                try:
                    supabase.storage.from_("lab-reports").remove(file_paths_to_delete)
                    logger.info(
                        f"Data retention: deleted {len(file_paths_to_delete)} lab report storage object(s) for patient"
                    )
                except Exception as storage_err:
                    logger.error(
                        f"Data retention: failed to remove lab report PDF(s) from storage: {storage_err}"
                    )
                    results["errors"].append(f"lab_reports_storage: {storage_err}")

            # Update database row (use correct column name: file_path)
            lr_res = (
                await sb(supabase.table("lab_reports")
                .update(
                    {
                        "patient_name": "[REDACTED]",
                        "patient_phone": "[REDACTED]",
                        "file_path": "[REDACTED]",
                    }
                )
                .eq("clinic_id", clinic_id)
                .eq("patient_phone", phone))
            )
            results["lab_reports_anonymized"] = len(lr_res.data or [])
        except Exception as e:
            logger.error(f"Lab reports anonymization error: {e}")
            results["errors"].append(f"lab_reports: {e}")

        # 3. Anonymize prescriptions — preserve medicine class, frequency (not name)
        try:
            rx_res = (
                await sb(supabase.table("prescriptions")
                .update(
                    {
                        "patient_name": "[REDACTED]",
                        "patient_phone": "[REDACTED]",
                        "notes": "[REDACTED]",
                    }
                )
                .eq("clinic_id", clinic_id)
                .eq("patient_phone", phone))
            )
            results["prescriptions_anonymized"] = len(rx_res.data or [])
        except Exception as e:
            logger.error(f"Prescriptions anonymization error: {e}")
            results["errors"].append(f"prescriptions: {e}")

        # 4. Anonymize family members linked to patient
        try:
            await sb(supabase.table("family_members").update(
                {
                    "name": "[REDACTED]",
                    "relationship": "[REDACTED]",
                }
            ).eq("clinic_id", clinic_id).eq("primary_patient_phone", phone))
        except Exception as e:
            logger.debug(f"Family members anonymization note: {e}")

        # 5. Mark the patient row itself as anonymized (but keep the shell for FK integrity)
        try:
            await sb(supabase.table("patients").update(
                {
                    "name": "[REDACTED]",
                    "opted_in": False,
                    "data_consent": False,
                }
            ).eq("clinic_id", clinic_id).eq("phone", phone))
        except Exception as e:
            logger.error(f"Patient anonymization error: {e}")
            results["errors"].append(f"patient_row: {e}")

        # 5b. Pseudonymize the durable inbound queue (KA-P2-10).
        #
        # inbound_messages stores `phone` as a NOT NULL column plus the full
        # webhook body as JSONB. It was not touched by erasure and has no purge
        # job, so after a patient exercised deletion their phone number and the
        # body of every message they ever sent remained indefinitely.
        #
        # Pseudonymize rather than DELETE: message_id carries a UNIQUE
        # constraint and is the anti-duplicate claim for Meta redelivery.
        # Removing the row would let an old wamid be reprocessed as new. The
        # row is kept as a tombstone; the identifying content is not.
        #
        # processed_messages is deliberately NOT touched — it holds only
        # (message_id, clinic_id) and carries no patient identifier.
        try:
            inbound_res = (
                await sb(supabase.table("inbound_messages")
                .update({"phone": "[REDACTED]", "payload": {}})
                .eq("clinic_id", clinic_id)
                .eq("phone", phone))
            )
            results["inbound_messages_pseudonymized"] = len(inbound_res.data or [])
        except Exception as e:
            logger.error(f"Inbound queue pseudonymization error: {e}")
            results["errors"].append(f"inbound_messages: {e}")

        # 6. Record DPDP compliance audit log
        try:
            await sb(supabase.table("admin_audit_logs").insert(
                {
                    "clinic_id": clinic_id,
                    "user_id": "dpdp_erasure",
                    "username": "patient_erasure",
                    "action": "DATA_ERASURE_REQUEST",
                    "resource_type": "patient",
                    "resource_id": phone[:6] + "***",
                    "details": {
                        "erasure_type": "DPDP_TIER_ERASURE",
                        "tier1_clinical": "ANONYMIZED_NMC_COMPLIANT",
                        "tier2_conversations": "DELETED",
                        "results": results,
                    },
                    "ip_address": "system",
                }
            ))
        except Exception as audit_err:
            logger.debug(f"Audit log write note: {audit_err}")

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
                await sb(supabase.table("appointments")
                .select("id, appointment_date, status")
                .eq("clinic_id", clinic_id)
                .eq("patient_phone", phone))
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
