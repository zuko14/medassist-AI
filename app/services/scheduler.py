"""Scheduler service for reminders and follow-ups."""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import supabase
from app.services.whatsapp import whatsapp_service
from app.templates.whatsapp_templates import TEMPLATES
from app.services.tenant import get_clinic_by_id

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Kolkata"))
_last_fail_open_count = 0


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
            replace_existing=True,
        )

        # 2-hour reminder (runs every hour)
        self.scheduler.add_job(
            self.send_2h_reminders,
            CronTrigger(hour="*"),
            id="2h_reminders",
            replace_existing=True,
        )

        # Follow-up messages (runs daily at 10 AM)
        self.scheduler.add_job(
            self.send_followups,
            CronTrigger(hour=10, minute=0),
            id="followups",
            replace_existing=True,
        )

        # Post-discharge health check-ins (day+3, day+7 — runs daily at 10:30 AM)
        self.scheduler.add_job(
            self.send_health_checkins,
            CronTrigger(hour=10, minute=30),
            id="health_checkins",
            replace_existing=True,
        )

        # Check doctor leaves (runs daily at 8 AM)
        self.scheduler.add_job(
            self.check_doctor_leaves,
            CronTrigger(hour=8, minute=0),
            id="doctor_leaves",
            replace_existing=True,
        )

        # Prescription reminders (every 5 minutes)
        self.scheduler.add_job(
            send_due_reminders_job,
            "interval",
            minutes=5,
            id="prescription_reminders",
            replace_existing=True,
        )

        # ── Security: Monitor dead-letter queue (Monday 9 AM) ──
        self.scheduler.add_job(
            self.alert_failed_messages,
            CronTrigger(day_of_week="mon", hour=9, minute=0),
            id="failed_messages_alert",
            replace_existing=True,
        )

        # ── Reliability: Monitor message queue fail-open rate (every 10 minutes) ──
        self.scheduler.add_job(
            self.alert_message_queue_fail_open,
            "interval",
            minutes=10,
            id="message_queue_fail_open_alert",
            replace_existing=True,
        )

        # ── Security: Cleanup stale rate limits (daily midnight) ──
        self.scheduler.add_job(
            self.cleanup_rate_limits,
            CronTrigger(hour=0, minute=0),
            id="rate_limits_cleanup",
            replace_existing=True,
        )

        # ── DPDP/NMC Compliance: Purge expired conversation sessions (daily 2 AM) ──
        self.scheduler.add_job(
            self.purge_expired_conversations,
            CronTrigger(hour=2, minute=0),
            id="conversation_purge",
            replace_existing=True,
        )

        # ── DPDP/NMC Compliance: Purge expired analytics events (daily 3 AM) ──
        self.scheduler.add_job(
            self.purge_expired_session_data,
            CronTrigger(hour=3, minute=0),
            id="analytics_purge",
            replace_existing=True,
        )

        # ── Payment: Expire stale pending_payment bookings (every minute) ──
        # Also recovers bookings where Razorpay shows paid but webhook was missed
        self.scheduler.add_job(
            self.expire_stale_bookings,
            "interval",
            minutes=1,
            id="expire_stale_bookings",
            replace_existing=True,
        )

        # ── Payment: Daily reconciliation (11 PM) ──
        # Compares confirmed bookings against Razorpay — discrepancies → alert
        self.scheduler.add_job(
            self.daily_payment_reconciliation,
            CronTrigger(hour=23, minute=0),
            id="payment_reconciliation",
            replace_existing=True,
        )

        # ── Lab Report Retry: Re-attempt pending_retry deliveries (every 5 minutes) ──
        self.scheduler.add_job(
            self._retry_pending_lab_reports,
            "interval",
            minutes=5,
            id="lab_report_retry",
            replace_existing=True,
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
            from app.services.tenant import has_feature

            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

            appointments = (
                supabase.table("appointments")
                .select("*")
                .eq("appointment_date", tomorrow)
                .eq("status", "confirmed")
                .eq("reminder_24h_sent", False)
                .execute()
            )

            for appt in appointments.data:
                try:
                    clinic = await get_clinic_by_id(appt.get("clinic_id", "default"))
                    if not has_feature(clinic, "reminders_basic"):
                        continue

                    components = TEMPLATES["reminder_24h"]["components_builder"](
                        appt["doctor_name"], appt["appointment_time"]
                    )

                    await whatsapp_service.send_template(
                        clinic,
                        appt["patient_phone"],
                        "appointment_reminder_24h",
                        components=components,
                        _source="scheduler",
                    )

                    # Mark as sent
                    supabase.table("appointments").update(
                        {"reminder_24h_sent": True}
                    ).eq("id", appt["id"]).execute()

                    logger.info(f"Sent 24h reminder for appointment {appt['id']}")
                except Exception as e:
                    logger.error(f"Error sending 24h reminder: {e}")

        except Exception as e:
            logger.error(f"Error in 24h reminders job: {e}")

    async def send_2h_reminders(self):
        """Send 2-hour appointment reminders."""
        try:
            from app.services.tenant import has_feature

            now = datetime.now()
            in_2h = (now + timedelta(hours=2)).strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")

            appointments = (
                supabase.table("appointments")
                .select("*")
                .eq("appointment_date", today)
                .eq("status", "confirmed")
                .eq("reminder_2h_sent", False)
                .execute()
            )

            for appt in appointments.data:
                appt_time = appt["appointment_time"]
                # Check if appointment is in ~2 hours
                if appt_time[:5] <= in_2h[:5]:
                    try:
                        clinic = await get_clinic_by_id(
                            appt.get("clinic_id", "default")
                        )
                        if not has_feature(clinic, "reminders_basic"):
                            continue

                        # Use branch name if available (multi-branch), else clinic name
                        location_name = appt.get("branch_name") or clinic["name"]

                        components = TEMPLATES["reminder_2h"]["components_builder"](
                            location_name, appt["doctor_name"]
                        )

                        await whatsapp_service.send_template(
                            clinic,
                            appt["patient_phone"],
                            "appointment_reminder_2h",
                            components=components,
                            _source="scheduler",
                        )

                        # Mark as sent
                        supabase.table("appointments").update(
                            {"reminder_2h_sent": True}
                        ).eq("id", appt["id"]).execute()

                        logger.info(f"Sent 2h reminder for appointment {appt['id']}")
                    except Exception as e:
                        logger.error(f"Error sending 2h reminder: {e}")

        except Exception as e:
            logger.error(f"Error in 2h reminders job: {e}")

    async def send_followups(self):
        """Send post-appointment follow-up messages."""
        try:
            from app.services.tenant import has_feature

            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

            appointments = (
                supabase.table("appointments")
                .select("*")
                .eq("appointment_date", yesterday)
                .eq("status", "completed")
                .eq("followup_sent", False)
                .execute()
            )

            for appt in appointments.data:
                try:
                    clinic = await get_clinic_by_id(appt.get("clinic_id", "default"))
                    if not has_feature(
                        clinic, "reminders_post_visit"
                    ) or not has_feature(clinic, "feedback"):
                        # Mark as sent so we don't keep polling
                        supabase.table("appointments").update(
                            {"followup_sent": True}
                        ).eq("id", appt["id"]).execute()
                        continue

                    components = TEMPLATES["followup_message"]["components_builder"](
                        appt["patient_name"].split()[0], settings.hospital_phone
                    )

                    await whatsapp_service.send_template(
                        clinic,
                        appt["patient_phone"],
                        "post_appointment_followup",
                        components=components,
                        _source="scheduler",
                    )

                    # Mark as sent
                    supabase.table("appointments").update({"followup_sent": True}).eq(
                        "id", appt["id"]
                    ).execute()

                    logger.info(f"Sent followup for appointment {appt['id']}")
                except Exception as e:
                    logger.error(f"Error sending followup: {e}")

        except Exception as e:
            logger.error(f"Error in followup job: {e}")

    async def send_health_checkins(self):
        """Send day+3 and day+7 post-discharge health check-ins.

        Distinct from send_followups (same-day satisfaction survey) —
        this is a clinical safety check, tracked via separate flags
        (health_checkin_3d_sent / health_checkin_7d_sent). Uses interactive
        buttons ("Feeling fine" / "Still have symptoms") so replies route
        through the intent system rather than free text.
        """
        for offset_days, flag_field in [(3, "health_checkin_3d_sent"), (7, "health_checkin_7d_sent")]:
            try:
                target_date = (datetime.now() - timedelta(days=offset_days)).strftime("%Y-%m-%d")

                appointments = (
                    supabase.table("appointments")
                    .select("*")
                    .eq("appointment_date", target_date)
                    .eq("status", "confirmed")
                    .eq(flag_field, False)
                    .execute()
                )

                for appt in appointments.data:
                    try:
                        clinic = await get_clinic_by_id(appt.get("clinic_id", "default"))
                        lang = "en"  # patient language isn't stored on appointments; default to English

                        from app.templates.whatsapp_templates import get_message

                        first_name = (appt.get("patient_name") or "there").split()[0]
                        text = get_message(
                            "health_checkin",
                            lang,
                            name=first_name,
                            doctor=appt.get("doctor_name", ""),
                        )

                        await whatsapp_service.send_interactive_buttons(
                            clinic,
                            appt["patient_phone"],
                            body=text,
                            buttons=[
                                {"id": "checkin_ok", "title": "Feeling fine"},
                                {"id": "checkin_concern", "title": "Still have symptoms"},
                            ],
                            _source="scheduler",
                        )

                        supabase.table("appointments").update({flag_field: True}).eq(
                            "id", appt["id"]
                        ).execute()

                        logger.info(
                            f"Sent day+{offset_days} health check-in for appointment {appt['id']}"
                        )
                    except Exception as e:
                        logger.error(f"Error sending day+{offset_days} health check-in: {e}")

            except Exception as e:
                logger.error(f"Error in health check-in job (day+{offset_days}): {e}")

    async def check_doctor_leaves(self):
        """Check for doctor leaves and notify affected patients."""
        try:
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            next_week = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

            leaves = (
                supabase.table("doctor_leaves")
                .select("*")
                .gte("leave_date", tomorrow)
                .lte("leave_date", next_week)
                .eq("leave_type", "full")
                .execute()
            )

            for leave in leaves.data:
                # Find affected appointments
                affected = (
                    supabase.table("appointments")
                    .select("*")
                    .eq("clinic_id", leave.get("clinic_id", "default"))
                    .eq("doctor_name", leave["doctor_name"])
                    .eq("appointment_date", leave["leave_date"])
                    .eq("status", "confirmed")
                    .execute()
                )

                for appt in affected.data:
                    try:
                        clinic = await get_clinic_by_id(
                            appt.get("clinic_id", "default")
                        )
                        # Cancel appointment
                        supabase.table("appointments").update(
                            {"status": "cancelled"}
                        ).eq("id", appt["id"]).execute()

                        # Send notification
                        components = TEMPLATES["appointment_cancelled_doctor_leave"][
                            "components_builder"
                        ](appt["doctor_name"], appt["appointment_date"])

                        await whatsapp_service.send_template(
                            clinic,
                            appt["patient_phone"],
                            "appointment_cancelled_doctor_leave",
                            components=components,
                            _source="scheduler",
                        )

                        logger.info(
                            f"Cancelled appointment {appt['id']} due to doctor leave"
                        )
                    except Exception as e:
                        logger.error(f"Error handling doctor leave: {e}")

        except Exception as e:
            logger.error(f"Error in doctor leaves job: {e}")

    async def alert_failed_messages(self):
        """Check for unprocessed failed messages and alert admin.

        Runs every Monday at 9 AM. If any messages are sitting in the
        dead-letter queue with status='pending', sends a WhatsApp alert
        to the admin so they know to investigate.
        """
        try:
            result = (
                supabase.table("failed_messages")
                .select("id", count="exact")
                .eq("status", "pending")
                .execute()
            )

            pending_count = len(result.data) if result.data else 0

            if pending_count > 0:
                admin_phone = settings.hospital_phone
                alert_msg = (
                    f"⚠️ Kriya AI Security Alert\n\n"
                    f"{pending_count} failed message(s) pending review.\n"
                    f"These are patient messages that failed to process.\n\n"
                    f"Check the failed_messages table in Supabase."
                )

                # Try to send via the default clinic, or log if we can't
                try:
                    from app.services.tenant import resolve_tenant

                    clinic = await resolve_tenant(admin_phone)
                    await whatsapp_service.send_text(clinic, admin_phone, alert_msg, _source="scheduler")
                    logger.info(f"Sent failed messages alert: {pending_count} pending")
                except Exception:
                    # If we can't resolve a clinic for the admin phone, just log it
                    logger.warning(
                        f"ALERT: {pending_count} failed messages pending in dead-letter queue. "
                        f"Could not send WhatsApp alert — check Supabase manually."
                    )

        except Exception as e:
            # Table might not exist yet — don't crash the scheduler
            logger.debug(f"Failed messages check skipped: {e}")

    async def alert_message_queue_fail_open(self):
        """Alert admin if the message queue fail-open rate is elevated.

        A sustained fail-open rate means messages are being processed without
        the atomic uniqueness guarantee (e.g. Supabase connection issues),
        which risks double-processing patient messages.
        """
        global _last_fail_open_count
        try:
            from app.services.message_queue import get_fail_open_count

            current = get_fail_open_count()
            delta = current - _last_fail_open_count
            _last_fail_open_count = current

            if delta > 5:
                admin_phone = settings.hospital_phone
                alert_msg = (
                    f"⚠️ Message queue fail-open rate elevated: "
                    f"{delta} messages processed without idempotency guarantee since last check."
                )
                try:
                    from app.services.tenant import resolve_tenant

                    clinic = await resolve_tenant(admin_phone)
                    await whatsapp_service.send_text(clinic, admin_phone, alert_msg, _source="scheduler")
                    logger.warning(
                        f"Sent message queue fail-open alert: {delta} fail-open events"
                    )
                except Exception:
                    logger.warning(
                        f"ALERT: {delta} message queue fail-open events occurred. "
                        f"Could not send WhatsApp alert."
                    )
        except Exception as e:
            logger.debug(f"Message queue fail-open check skipped: {e}")

    async def cleanup_rate_limits(self):
        """Delete stale rate limit entries older than 1 hour.

        Runs daily at midnight. Prevents the rate_limits table from
        growing indefinitely with old IP entries.
        """
        try:
            cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
            supabase.table("rate_limits").delete().lt("window_start", cutoff).execute()
            logger.info("Cleaned up stale rate limit entries")
        except Exception as e:
            # Table might not exist yet — don't crash the scheduler
            logger.debug(f"Rate limits cleanup skipped: {e}")

    async def purge_expired_conversations(self):
        """Purge conversation sessions older than the configured purge window.

        Runs daily at 2 AM. Deletes Tier 2 session data only.
        Clinical records are NOT touched by this job.
        """
        try:
            from app.services.data_retention import data_retention_service

            count = await data_retention_service.purge_expired_conversations()
            if count > 0:
                logger.info(f"Scheduler: purged {count} expired conversation sessions")
        except Exception as e:
            logger.error(f"Conversation purge job failed: {e}")

    async def purge_expired_session_data(self):
        """Purge analytics events older than 12 months.

        Runs daily at 3 AM. Removes operational analytics data only.
        """
        try:
            from app.services.data_retention import data_retention_service

            count = await data_retention_service.purge_expired_session_data()
            if count > 0:
                logger.info(f"Scheduler: purged {count} expired analytics events")
        except Exception as e:
            logger.error(f"Analytics purge job failed: {e}")

    async def expire_stale_bookings(self):
        """Expire pending_payment bookings past their hold window.

        Runs every minute. Before expiring, checks Razorpay order status.
        If Razorpay shows paid but the webhook never arrived, confirms
        the booking instead (recovery path).
        """
        try:
            from app.services.payment import payment_service

            count = await payment_service.expire_stale_bookings()
            if count > 0:
                logger.info(f"Scheduler: processed {count} stale bookings")
        except Exception as e:
            logger.error(f"Stale bookings job failed: {e}")

    async def daily_payment_reconciliation(self):
        """Compare confirmed bookings against Razorpay settlements.

        Runs daily at 11 PM. Logs a reconciliation summary.
        Any discrepancy → alert admin, never auto-correct.
        """
        try:
            from app.services.payment import payment_service

            summary = await payment_service.get_daily_reconciliation()
            logger.info(
                f"Payment reconciliation: {summary['confirmed_count']} confirmed, "
                f"₹{summary['confirmed_total_rupees']:.2f} total, "
                f"{summary['pending_review_count']} pending review"
            )
            if summary["pending_review_count"] > 0:
                await payment_service._alert_admin(
                    f"⚠️ Daily Reconciliation Alert\n\n"
                    f"Date: {summary['date']}\n"
                    f"Confirmed bookings: {summary['confirmed_count']}\n"
                    f"Total: ₹{summary['confirmed_total_rupees']:.2f}\n"
                    f"⚠️ Pending review: {summary['pending_review_count']}\n\n"
                    f"Please check the admin panel and compare against Razorpay dashboard."
                )
        except Exception as e:
            logger.error(f"Payment reconciliation job failed: {e}")

    async def _retry_pending_lab_reports(self):
        """Re-attempt delivery of lab reports stuck in 'pending_retry' status.

        Runs every 5 minutes. Delegates to LabReportService.retry_pending_deliveries
        which handles download, re-send, backoff, and permanent failure marking.
        """
        try:
            from app.services.lab_reports import LabReportService

            service = LabReportService()
            count = await service.retry_pending_deliveries()
            if count > 0:
                logger.info(f"Scheduler: processed {count} pending lab report retries")
        except Exception as e:
            # Table might not have retry columns yet — don't crash the scheduler
            logger.debug(f"Lab report retry job skipped: {e}")


# Global instance
scheduler_service = SchedulerService()
