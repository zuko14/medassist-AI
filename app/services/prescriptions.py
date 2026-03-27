"""Prescription Reminders Service."""

import logging
from datetime import datetime, date, timezone

import httpx

from app.config import settings
from app.database import supabase

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com/v18.0"


class PrescriptionService:
    """Service for managing prescription reminders."""

    def __init__(self):
        self.phone_number_id = settings.whatsapp_phone_number_id
        self.token = settings.whatsapp_token
        self.base_url = f"{WHATSAPP_API_BASE}/{self.phone_number_id}"

    async def add_prescription(
        self,
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
            await self._send_text(patient_phone, message)
        except Exception as e:
            logger.error(f"Failed to send prescription confirmation: {e}")

        return result.data[0]

    async def get_all_prescriptions(self, active_only: bool = False) -> list:
        """Get all prescriptions, optionally filtered to active only."""
        if active_only:
            result = (
                supabase.table("prescriptions")
                .select("*")
                .eq("is_active", True)
                .gte("end_date", str(date.today()))
                .order("created_at", desc=True)
                .execute()
            )
        else:
            result = (
                supabase.table("prescriptions")
                .select("*")
                .order("created_at", desc=True)
                .execute()
            )
        return result.data or []

    async def deactivate_prescription(self, prescription_id: str) -> dict:
        """Deactivate a prescription reminder."""
        supabase.table("prescriptions").update(
            {"is_active": False}
        ).eq("id", prescription_id).execute()

        updated = (
            supabase.table("prescriptions")
            .select("*")
            .eq("id", prescription_id)
            .execute()
        )
        return updated.data[0]

    async def send_due_reminders(self) -> dict:
        """Send reminders for prescriptions due right now (within 5 min window)."""
        now = datetime.now(timezone.utc)
        current_time = now.strftime("%H:%M")
        today_str = str(date.today())

        # Get all active prescriptions where today is within range
        result = (
            supabase.table("prescriptions")
            .select("*")
            .eq("is_active", True)
            .lte("start_date", today_str)
            .gte("end_date", today_str)
            .execute()
        )

        count_sent = 0
        count_errors = 0

        for rx in result.data or []:
            # Check if current time matches any reminder time (within 5 min)
            for rt in rx.get("reminder_times", []):
                if self._time_within_window(current_time, rt, 5):
                    try:
                        message = (
                            f"⏰ Medication Reminder\n"
                            f"Hi {rx['patient_name']}, time to take your medicine!\n"
                            f"💊 {rx['medicine_name']} — {rx['dosage']}\n"
                            f"Stay healthy! 🏥 TestHospital"
                        )
                        await self._send_text(rx["patient_phone"], message)
                        count_sent += 1
                    except Exception as e:
                        logger.error(f"Reminder send error for {rx['id']}: {e}")
                        count_errors += 1
                    break  # Only send one reminder per prescription per cycle

        return {"sent": count_sent, "errors": count_errors}

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

    async def _send_text(self, phone: str, message: str):
        """Send a text message via WhatsApp."""
        url = f"{self.base_url}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": message},
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
