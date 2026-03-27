"""Scheduler service for reminders and follow-ups."""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import supabase
from app.services.whatsapp import whatsapp_service
from app.templates.whatsapp_templates import TEMPLATES

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()



async def send_due_reminders_job():
    """Async wrapper for prescription reminder scheduler job."""
    from app.services.prescriptions import PrescriptionService
    await PrescriptionService().send_due_reminders()


class SchedulerService:
    """Service for scheduled tasks."""

    def __init__(self):
        self.scheduler = scheduler

    def start(self):
        """Start the scheduler."""
        # 24-hour reminder (runs daily at 9 AM)
        self.scheduler.add_job(
            self.send_24h_reminders,
            CronTrigger(hour=9, minute=0),
            id="24h_reminders",
            replace_existing=True
        )

        # 2-hour reminder (runs every hour)
        self.scheduler.add_job(
            self.send_2h_reminders,
            CronTrigger(hour="*"),
            id="2h_reminders",
            replace_existing=True
        )

        # Follow-up messages (runs daily at 10 AM)
        self.scheduler.add_job(
            self.send_followups,
            CronTrigger(hour=10, minute=0),
            id="followups",
            replace_existing=True
        )

        # Check doctor leaves (runs daily at 8 AM)
        self.scheduler.add_job(
            self.check_doctor_leaves,
            CronTrigger(hour=8, minute=0),
            id="doctor_leaves",
            replace_existing=True
        )

        # Prescription reminders (every 5 minutes)
        self.scheduler.add_job(
            send_due_reminders_job,
            'interval',
            minutes=5,
            id='prescription_reminders',
            replace_existing=True
        )

        self.scheduler.start()
        logger.info("Scheduler started")

    def shutdown(self):
        """Shutdown the scheduler."""
        self.scheduler.shutdown()
        logger.info("Scheduler shutdown")

    async def send_24h_reminders(self):
        """Send 24-hour appointment reminders."""
        try:
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

            appointments = supabase.table("appointments").select("*").eq("appointment_date", tomorrow).eq("status", "confirmed").eq("reminder_24h_sent", False).execute()

            for appt in appointments.data:
                try:
                    components = TEMPLATES["reminder_24h"]["components_builder"](
                        appt["doctor_name"],
                        appt["appointment_time"]
                    )

                    await whatsapp_service.send_template(
                        appt["patient_phone"],
                        "appointment_reminder_24h",
                        components=components
                    )

                    # Mark as sent
                    supabase.table("appointments").update({"reminder_24h_sent": True}).eq("id", appt["id"]).execute()

                    logger.info(f"Sent 24h reminder for appointment {appt['id']}")
                except Exception as e:
                    logger.error(f"Error sending 24h reminder: {e}")

        except Exception as e:
            logger.error(f"Error in 24h reminders job: {e}")

    async def send_2h_reminders(self):
        """Send 2-hour appointment reminders."""
        try:
            now = datetime.now()
            in_2h = (now + timedelta(hours=2)).strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")

            appointments = supabase.table("appointments").select("*").eq("appointment_date", today).eq("status", "confirmed").eq("reminder_2h_sent", False).execute()

            for appt in appointments.data:
                appt_time = appt["appointment_time"]
                # Check if appointment is in ~2 hours
                if appt_time[:5] <= in_2h[:5]:
                    try:
                        components = TEMPLATES["reminder_2h"]["components_builder"](
                            settings.hospital_name,
                            appt["doctor_name"]
                        )

                        await whatsapp_service.send_template(
                            appt["patient_phone"],
                            "appointment_reminder_2h",
                            components=components
                        )

                        # Mark as sent
                        supabase.table("appointments").update({"reminder_2h_sent": True}).eq("id", appt["id"]).execute()

                        logger.info(f"Sent 2h reminder for appointment {appt['id']}")
                    except Exception as e:
                        logger.error(f"Error sending 2h reminder: {e}")

        except Exception as e:
            logger.error(f"Error in 2h reminders job: {e}")

    async def send_followups(self):
        """Send post-appointment follow-up messages."""
        try:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

            appointments = supabase.table("appointments").select("*").eq("appointment_date", yesterday).eq("status", "completed").eq("followup_sent", False).execute()

            for appt in appointments.data:
                try:
                    components = TEMPLATES["followup_message"]["components_builder"](
                        appt["patient_name"].split()[0],
                        settings.hospital_phone
                    )

                    await whatsapp_service.send_template(
                        appt["patient_phone"],
                        "post_appointment_followup",
                        components=components
                    )

                    # Mark as sent
                    supabase.table("appointments").update({"followup_sent": True}).eq("id", appt["id"]).execute()

                    logger.info(f"Sent followup for appointment {appt['id']}")
                except Exception as e:
                    logger.error(f"Error sending followup: {e}")

        except Exception as e:
            logger.error(f"Error in followup job: {e}")

    async def check_doctor_leaves(self):
        """Check for doctor leaves and notify affected patients."""
        try:
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            next_week = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

            leaves = supabase.table("doctor_leaves").select("*").gte("leave_date", tomorrow).lte("leave_date", next_week).eq("leave_type", "full").execute()

            for leave in leaves.data:
                # Find affected appointments
                affected = supabase.table("appointments").select("*").eq("doctor_name", leave["doctor_name"]).eq("appointment_date", leave["leave_date"]).eq("status", "confirmed").execute()

                for appt in affected.data:
                    try:
                        # Cancel appointment
                        supabase.table("appointments").update({"status": "cancelled"}).eq("id", appt["id"]).execute()

                        # Send notification
                        components = TEMPLATES["appointment_cancelled_doctor_leave"]["components_builder"](
                            appt["doctor_name"],
                            appt["appointment_date"]
                        )

                        await whatsapp_service.send_template(
                            appt["patient_phone"],
                            "appointment_cancelled_doctor_leave",
                            components=components
                        )

                        logger.info(f"Cancelled appointment {appt['id']} due to doctor leave")
                    except Exception as e:
                        logger.error(f"Error handling doctor leave: {e}")

        except Exception as e:
            logger.error(f"Error in doctor leaves job: {e}")


# Global instance
scheduler_service = SchedulerService()
