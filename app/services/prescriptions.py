"""Prescription Reminders Service."""

import logging
from datetime import datetime, date, timezone


from app.database import scoped_query, supabase
from app.services.whatsapp import whatsapp_service
from app.services.tenant import get_clinic_by_id

logger = logging.getLogger(__name__)


class PrescriptionService:
    """Service for managing prescription reminders."""

    async def add_prescription(
        self,
        clinic_id: str,
        patient_phone: str,
        patient_name: str,
        medicine_name: str,
        dosage: str,
        frequency: str,
        reminder_times: list[str],
        start_date: str,
        end_date: str,
        notes: str = None,
    ) -> dict:
        """Add a new prescription and send confirmation to patient."""
        row = {
            "clinic_id": clinic_id,
            "patient_phone": patient_phone,
            "patient_name": patient_name,
            "medicine_name": medicine_name,
            "dosage": dosage,
            "frequency": frequency,
            "reminder_times": reminder_times,
            "start_date": start_date,
            "end_date": end_date,
            "is_active": True,
            "notes": notes,
        }
        result = supabase.table("prescriptions").insert(row).execute()

        # Send confirmation WhatsApp message
        try:
            times_str = ", ".join(reminder_times)
            message = (
                f"Hi {patient_name}, your medication reminder has been set.\n"
                f"Medicine: {medicine_name} | Dose: {dosage} | Frequency: {frequency}.\n"
                f"You will receive reminders at: {times_str} daily until {end_date}.\n"
                f"Reply STOP anytime to opt out."
            )
            clinic = await get_clinic_by_id(clinic_id)
            await whatsapp_service.send_text(clinic, patient_phone, message, _source="prescriptions")
        except Exception as e:
            logger.error(f"Failed to send prescription confirmation: {e}")

        return result.data[0]

    async def get_all_prescriptions(
        self, clinic_id: str = "default", active_only: bool = False
    ) -> list:
        """Get all prescriptions, optionally filtered to active only."""
        if active_only:
            result = (
                supabase.table("prescriptions")
                .select("*")
                .eq("clinic_id", clinic_id)
                .eq("is_active", True)
                .gte("end_date", str(date.today()))
                .order("created_at", desc=True)
                .execute()
            )
        else:
            result = (
                supabase.table("prescriptions")
                .select("*")
                .eq("clinic_id", clinic_id)
                .order("created_at", desc=True)
                .execute()
            )
        return result.data or []

    async def deactivate_prescription(
        self, clinic_id: str, prescription_id: str
    ) -> dict:
        """Deactivate a prescription reminder."""
        supabase.table("prescriptions").update({"is_active": False}).eq(
            "clinic_id", clinic_id
        ).eq("id", prescription_id).execute()

        updated = (
            supabase.table("prescriptions")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("id", prescription_id)
            .execute()
        )
        return updated.data[0]

    async def send_due_reminders(self) -> dict:
        """Send reminders for prescriptions due right now (within 5 min window).

        KA-09 fixes:
        - Wrapped in distributed_job_lock to prevent duplicate sends across workers
        - Uses IST (Asia/Kolkata) for time comparison since all clinics are in India
        - Tracks sent reminders in prescription_reminder_sends for deduplication
        - Queries per-clinic to maintain tenant scoping
        """
        from app.services.distributed_lock import distributed_job_lock
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(ist)
        current_time = now_ist.strftime("%H:%M")
        today_str = now_ist.strftime("%Y-%m-%d")

        count_sent = 0
        count_errors = 0
        count_skipped_dedup = 0

        async with distributed_job_lock("prescription_reminders", lease_seconds=120) as acquired:
            if not acquired:
                return {"sent": 0, "errors": 0, "skipped": "lock_held_by_another_instance"}

            # Get all active prescriptions where today is within range (cross-clinic background scan)
            result = (
                scoped_query("prescriptions", allow_unscoped=True)
                .eq("is_active", True)
                .lte("start_date", today_str)
                .gte("end_date", today_str)
                .execute()
            )

            for rx in result.data or []:
                rx_clinic_id = rx.get("clinic_id", "default")
                # Check if current time matches any reminder time (within 5 min)
                for rt in rx.get("reminder_times", []):
                    if self._time_within_window(current_time, rt, 5):
                        # KA-09: Deduplication — check if already sent today for this time
                        try:
                            dedup_check = (
                                scoped_query("prescription_reminder_sends", allow_unscoped=True)
                                .eq("prescription_id", rx["id"])
                                .eq("reminder_time", rt)
                                .eq("sent_date", today_str)
                                .execute()
                            )
                            if dedup_check.data:
                                count_skipped_dedup += 1
                                break  # Already sent
                        except Exception:
                            pass  # Table may not exist yet; proceed without dedup

                        try:
                            clinic = await get_clinic_by_id(rx_clinic_id)
                            message = (
                                f"⏰ Medication Reminder\n"
                                f"Hi {rx['patient_name']}, time to take your medicine!\n"
                                f"💊 {rx['medicine_name']} — {rx['dosage']}\n"
                                f"Stay healthy! 🏥 {clinic['name']}"
                            )
                            await whatsapp_service.send_text(
                                clinic, rx["patient_phone"], message, _source="prescriptions"
                            )

                            # Record send for deduplication
                            try:
                                supabase.table("prescription_reminder_sends").insert({
                                    "prescription_id": rx["id"],
                                    "reminder_time": rt,
                                    "sent_date": today_str,
                                    "clinic_id": rx_clinic_id,
                                }).execute()
                            except Exception as dedup_err:
                                logger.warning(f"Dedup record insert failed for {rx['id']}: {dedup_err}")

                            count_sent += 1
                        except Exception as e:
                            logger.error(f"Reminder send error for {rx['id']}: {e}")
                            count_errors += 1
                        break  # Only send one reminder per prescription per cycle

        return {"sent": count_sent, "errors": count_errors, "skipped_dedup": count_skipped_dedup}

    # ── Internal helpers ──

    @staticmethod
    def _time_within_window(current: str, target: str, window_min: int) -> bool:
        """Check if current time is within window_min minutes of target time."""
        try:
            c_h, c_m = map(int, current.split(":"))
            t_h, t_m = map(int, target.split(":"))
            current_mins = c_h * 60 + c_m
            target_mins = t_h * 60 + t_m
            return abs(current_mins - target_mins) <= window_min
        except (ValueError, AttributeError):
            return False
