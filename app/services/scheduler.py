"""Scheduler service for reminders and follow-ups."""

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.database import supabase
from app.services.whatsapp import whatsapp_service
from app.templates.whatsapp_templates import TEMPLATES
from app.utils.helpers import format_slot_time
from app.services.tenant import get_clinic_by_id

logger = logging.getLogger(__name__)

# The APScheduler cron triggers below already fire on Asia/Kolkata. Date
# arithmetic must use the same clock: `datetime.now()` is the container's
# local time (UTC on Render), which only happened to agree because these jobs
# run mid-morning IST. Pin it so a schedule change cannot silently shift which
# day a follow-up is measured from.
CLINIC_TZ = ZoneInfo("Asia/Kolkata")


def _today_local() -> date:
    return datetime.now(CLINIC_TZ).date()


# How many days back the follow-up job re-scans. Without a window a single
# transient Meta failure lost that patient's follow-up permanently, because the
# query was pinned to exactly yesterday's date.
FOLLOWUP_LOOKBACK_DAYS = 3


def followup_config(clinic: dict) -> dict:
    """Resolve one clinic's follow-up settings.

    Admin panel (Hospital Profile -> Patient Follow-ups) writes these into
    clinics.config; anything unset falls back to the platform default.
    """
    cfg = (clinic or {}).get("config") or {}

    enabled = cfg.get("followup_enabled")
    if not isinstance(enabled, bool):
        enabled = settings.followup_enabled_default

    try:
        days = int(cfg.get("followup_days", settings.followup_days_after_visit))
    except (TypeError, ValueError):
        days = settings.followup_days_after_visit
    days = max(1, min(days, 30))

    return {
        "enabled": enabled,
        "days": days,
        "message": (cfg.get("followup_message") or "").strip(),
        # A business-initiated message must be a template, so the admin's own
        # wording only reaches the patient through a template with a body
        # variable to carry it.
        "message_template": (
            cfg.get("followup_message_template_name")
            or settings.followup_message_template_name
            or ""
        ).strip(),
        "template": (
            cfg.get("followup_template_name")
            or settings.followup_template_name
            or TEMPLATES["followup_message"]["name"]
        ).strip(),
    }


scheduler = AsyncIOScheduler(
    timezone=ZoneInfo("Asia/Kolkata"),
    job_defaults={"misfire_grace_time": 60, "coalesce": True},
)
_last_fail_open_count = 0


async def send_due_reminders_job():
    """Async wrapper for prescription reminder scheduler job."""
    from app.services.prescriptions import PrescriptionService

    await PrescriptionService().send_due_reminders()


def _reconstruct_message(row: dict):
    """Rebuild the original inbound message from the durable payload.

    The previous implementation synthesized a text message with body="" —
    recovery discarded the very content it existed to preserve (KRIYA-004).

    Returns None if the payload cannot yield a message. The caller MUST
    dead-letter and alert rather than replay a blank.
    """
    from app.models.message import WhatsAppMessage

    payload = row.get("payload") or {}

    # Full Meta envelope
    try:
        entry = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    except (KeyError, IndexError, TypeError):
        # Already-unwrapped message dict
        entry = payload if payload.get("type") else None

    if not entry:
        return None

    try:
        return WhatsAppMessage(**entry)
    except Exception as e:
        logger.warning(
            f"Reconstruction failed for {row.get('message_id')}: {e}"
        )
        return None


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

        # Close out yesterday's visits (runs daily at 00:30 IST).
        # Must run BEFORE the follow-up job: nothing else in the system ever
        # set status='completed', so send_followups() matched zero rows every
        # day and no patient has ever received a post-visit follow-up.
        self.scheduler.add_job(
            self.auto_complete_appointments,
            CronTrigger(hour=0, minute=30),
            id="auto_complete_appointments",
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

        # ── Reliability: Replay lock-timeout messages from the DLQ (every 5 min) ──
        self.scheduler.add_job(
            self.drain_pending_retry_messages,
            "interval",
            minutes=5,
            id="dlq_pending_retry_drain",
            replace_existing=True,
        )

        # ── Reliability: Monitor failed lab report deliveries (hourly) ──
        self.scheduler.add_job(
            self.alert_failed_lab_reports,
            "interval",
            hours=1,
            id="failed_lab_reports_alert",
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

        # ── Payment: Fast-poll recently-created pending payments (every 30s) ──
        # Catches cases where Razorpay webhook is delayed or missed.
        # Only checks bookings created in the last 5 minutes to minimize API calls.
        self.scheduler.add_job(
            self.poll_recent_pending_payments,
            "interval",
            seconds=30,
            id="poll_recent_pending_payments",
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

        # ── Durable Inbound Queue Recovery: Recover pending / retryable messages (every minute) ──
        self.scheduler.add_job(
            self.recover_pending_inbound_messages,
            "interval",
            minutes=1,
            id="recover_pending_inbound_messages",
            replace_existing=True,
        )

        # ── Abandoned Message Claims: Reap stuck worker claims (every minute) ──
        self.scheduler.add_job(
            self.reap_abandoned_message_claims,
            "interval",
            seconds=60,
            id="reap_abandoned_message_claims",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        # ── Connector Polling: Poll all enabled integration connectors (every 1 minute) ──
        # Each connector internally respects its own poll_interval_minutes before re-running,
        # so the 1-minute tick is just the evaluation frequency, not the actual poll rate.
        # This replaces the need for a separate Render background worker process.
        from connectors.runner import run_all_connectors, cleanup_expired_storage
        self.scheduler.add_job(
            run_all_connectors,
            "interval",
            minutes=1,
            id="connector_polling",
            replace_existing=True,
        )

        # ── Connector Storage Cleanup: Delete PDFs older than 90 days (daily 2 AM IST) ──
        self.scheduler.add_job(
            cleanup_expired_storage,
            CronTrigger(hour=2, minute=0),
            id="connector_storage_cleanup",
            replace_existing=True,
        )

        self.scheduler.start()
        logger.info("Scheduler started (with integrated connector polling)")

    def shutdown(self):
        """Shutdown the scheduler."""
        self.scheduler.shutdown()
        logger.info("Scheduler shutdown")

    async def reap_abandoned_message_claims(self):
        """Release message claims abandoned by a worker that died mid-processing.

        Lease 45s < interval 60s so a stuck reaper cannot block the next tick.
        Reap threshold 120s > the 15s phone-lock timeout plus typical handler
        time, so a merely slow handler is never reaped out from under itself.
        """
        from app.services.distributed_lock import distributed_job_lock
        from app.services.message_queue import message_queue

        async with distributed_job_lock(
            "reap_abandoned_claims", lease_seconds=45
        ) as acquired:
            if not acquired:
                return
            await message_queue.reap_abandoned_claims(lease_seconds=120)

    async def recover_pending_inbound_messages(self, lease_timeout_seconds: int = 300):
        """Durable Inbound Queue Recovery sweep.

        Reclaims messages in 'received' or 'failed_retryable' older than lease_timeout_seconds.
        'processing' is handled exclusively by the reaper (T0.3b) to prevent dual-reclamation.
        Dispatches safe processing with full payload reconstruction so no message content is lost.
        """
        from app.services.distributed_lock import distributed_job_lock
        from datetime import datetime, timezone, timedelta

        async with distributed_job_lock("recover_pending_inbound_messages", lease_seconds=60) as acquired:
            if not acquired:
                return

            try:
                from app.services.message_queue import message_queue
                from app.routers.webhook import process_message_safe
                from app.database import supabase

                cutoff = (
                    datetime.now(timezone.utc) - timedelta(seconds=lease_timeout_seconds)
                ).isoformat()

                rows = (
                    supabase.table("inbound_messages")
                    .select(
                        "message_id, phone, display_phone, phone_number_id, "
                        "payload, attempt_count"
                    )
                    # 'processing' is deliberately EXCLUDED — that is the job of the
                    # reaper (T0.3b). Two subsystems competing for the same rows is how
                    # double replies happen.
                    .in_("status", ["received", "failed_retryable"])
                    # Never race the live hot path. Before T0.4 there was no age filter
                    # and 'received' is the status of in-flight messages (KRIYA-004).
                    .lt("created_at", cutoff)
                    .order("created_at")
                    .limit(20)
                    .execute()
                )

                if not rows.data:
                    return

                for msg_row in rows.data:
                    msg_id = msg_row.get("message_id")
                    claimed = await message_queue.claim_message(msg_id)
                    if not claimed:
                        continue

                    msg = _reconstruct_message(msg_row)
                    if msg is None:
                        logger.error(
                            f"RECOVERY_UNRECONSTRUCTABLE message_id={msg_id} "
                            f"— dead-lettering rather than replaying a blank message"
                        )
                        await message_queue.mark_failed(
                            msg_id,
                            "payload unreconstructable",
                            max_retries=0,
                        )
                        continue

                    logger.info(f"Durable queue recovery: processing recovered message {msg_id}")

                    await process_message_safe(
                        msg,
                        msg_row.get("display_phone") or msg_row.get("phone"),
                        msg_row.get("payload", {}),
                        msg_row.get("phone_number_id"),
                    )
            except Exception as e:
                logger.warning(f"Error in recover_pending_inbound_messages: {e}")

    async def send_24h_reminders(self):
        """Send 24-hour appointment reminders."""
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("24h_reminders", lease_seconds=300) as acquired:
            if not acquired:
                return
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
                        if not has_feature(clinic, "reminders"):
                            continue

                        components = TEMPLATES["reminder_24h"]["components_builder"](
                            appt["doctor_name"],
                            format_slot_time(appt["appointment_time"]),
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
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("2h_reminders", lease_seconds=300) as acquired:
            if not acquired:
                return
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
                            if not has_feature(clinic, "reminders"):
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

    async def auto_complete_appointments(self):
        """Mark past confirmed appointments as completed.

        Nothing else in the codebase writes status='completed', which is the
        state send_followups() filters on. Without this the entire post-visit
        follow-up feature is inert.

        Deliberately counts a no-show as completed: the alternative is that
        follow-ups only fire for visits a staff member remembered to close,
        which in practice means almost never.
        """
        from app.services.distributed_lock import distributed_job_lock

        async with distributed_job_lock(
            "auto_complete_appointments", lease_seconds=300
        ) as acquired:
            if not acquired:
                return
            try:
                today = _today_local().isoformat()
                now_iso = datetime.now(timezone.utc).isoformat()

                due = (
                    supabase.table("appointments")
                    .select("id")
                    .eq("status", "confirmed")
                    .lt("appointment_date", today)
                    .limit(2000)
                    .execute()
                )
                rows = due.data or []
                if not rows:
                    return

                completed = 0
                for appt in rows:
                    try:
                        # Re-assert status in the WHERE clause so a cancellation
                        # landing between the scan and this write is not clobbered.
                        supabase.table("appointments").update(
                            {"status": "completed", "completed_at": now_iso}
                        ).eq("id", appt["id"]).eq("status", "confirmed").execute()
                        completed += 1
                    except Exception as e:
                        logger.error(
                            f"Failed to auto-complete appointment {appt['id']}: {e}"
                        )

                logger.info(f"Auto-completed {completed} past appointment(s)")
            except Exception as e:
                logger.error(f"Error in auto-complete appointments job: {e}")

    async def send_followups(self):
        """Send post-appointment follow-up messages.

        Scans a lookback window rather than exactly yesterday so a transient
        Meta failure retries on following days instead of losing that patient's
        follow-up forever, and only marks followup_sent once the send succeeded.
        """
        from app.services.distributed_lock import distributed_job_lock

        async with distributed_job_lock("followups", lease_seconds=300) as acquired:
            if not acquired:
                return
            try:
                from app.services.tenant import has_feature

                today = _today_local()
                # Widest window any clinic could ask for; narrowed per clinic below.
                window_start = (
                    today - timedelta(days=30 + FOLLOWUP_LOOKBACK_DAYS)
                ).isoformat()
                window_end = (today - timedelta(days=1)).isoformat()

                appointments = (
                    supabase.table("appointments")
                    .select("*")
                    .eq("status", "completed")
                    .eq("followup_sent", False)
                    .gte("appointment_date", window_start)
                    .lte("appointment_date", window_end)
                    .limit(2000)
                    .execute()
                )

                for appt in appointments.data or []:
                    try:
                        clinic = await get_clinic_by_id(appt.get("clinic_id", "default"))

                        # "reminders" is the flag that actually exists in
                        # PLAN_FEATURES. This used to check "reminders_post_visit",
                        # which is in no plan at all, so every non-enterprise
                        # clinic fell through here and had the appointment
                        # permanently marked as followed up.
                        if not has_feature(clinic, "reminders"):
                            self._burn_followup(appt["id"])
                            continue

                        cfg = followup_config(clinic)
                        if not cfg["enabled"]:
                            self._burn_followup(appt["id"])
                            continue

                        try:
                            visit_date = datetime.strptime(
                                appt["appointment_date"], "%Y-%m-%d"
                            ).date()
                        except (KeyError, TypeError, ValueError):
                            continue

                        age_days = (today - visit_date).days
                        if age_days < cfg["days"]:
                            continue  # not due yet for this clinic's offset
                        if age_days > cfg["days"] + FOLLOWUP_LOOKBACK_DAYS:
                            # Past the retry window — stop rescanning it forever.
                            self._burn_followup(appt["id"])
                            continue

                        # Opt-out suppresses engagement. Burn the flag so an
                        # opted-out patient is not rescanned every day until
                        # the lookback window closes. A DB error here raises
                        # instead, and the per-appointment handler retries
                        # tomorrow with the flag left unset.
                        from app.services.consent import consent_service

                        if not await consent_service.accepts_engagement(
                            clinic["id"], appt["patient_phone"]
                        ):
                            logger.info(
                                f"Skipping follow-up for appointment {appt['id']} — "
                                f"patient has opted out of engagement messages"
                            )
                            self._burn_followup(appt["id"])
                            continue

                        first_name = (appt.get("patient_name") or "there").split()[0]
                        template, components = self._followup_template_and_components(
                            cfg, first_name
                        )

                        sent = await whatsapp_service.send_template(
                            clinic,
                            appt["patient_phone"],
                            template,
                            components=components,
                            _source="scheduler",
                        )

                        if not sent:
                            # Leave followup_sent False so the lookback window
                            # retries tomorrow. Burning it here is how the previous
                            # version lost follow-ups to one transient Meta error.
                            logger.warning(
                                f"Follow-up send refused for appointment {appt['id']} "
                                f"(template '{template}') — retrying for up to "
                                f"{FOLLOWUP_LOOKBACK_DAYS} more day(s)"
                            )
                            continue

                        supabase.table("appointments").update(
                            {"followup_sent": True}
                        ).eq("id", appt["id"]).execute()

                        logger.info(f"Sent followup for appointment {appt['id']}")
                    except Exception as e:
                        logger.error(f"Error sending followup: {e}")

            except Exception as e:
                logger.error(f"Error in followup job: {e}")

    @staticmethod
    def _burn_followup(appointment_id: str) -> None:
        """Mark an appointment as followed up without sending.

        Only for deliberate suppression (feature off, clinic disabled it, or
        past the retry window) — never for a failed send.
        """
        try:
            supabase.table("appointments").update({"followup_sent": True}).eq(
                "id", appointment_id
            ).execute()
        except Exception as e:
            logger.error(f"Failed to mark followup_sent for {appointment_id}: {e}")

    @staticmethod
    def _followup_template_and_components(cfg: dict, first_name: str):
        """Pick the follow-up template and its body parameters.

        A follow-up always lands outside WhatsApp's 24h customer-service window,
        so it must be a template — the clinic's own wording can only reach the
        patient through a template with a body variable to carry it. Without one
        configured we fall back to the built-in template and the custom message
        is not delivered (logged, never silently claimed).
        """
        from app.services.lab_reports import flatten_for_template_param

        if cfg["message_template"] and cfg["message"]:
            return cfg["message_template"], [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": first_name},
                        {
                            "type": "text",
                            "text": flatten_for_template_param(cfg["message"]),
                        },
                    ],
                }
            ]

        if cfg["message"]:
            logger.info(
                "Clinic has a custom follow-up message but no message-carrying "
                "template configured — sending the built-in follow-up instead. "
                "Set followup_message_template_name to an approved 2-variable "
                "template to deliver it."
            )

        return cfg["template"], TEMPLATES["followup_message"]["components_builder"](
            first_name, settings.hospital_phone
        )

    async def send_health_checkins(self):
        """Send day+3 and day+7 post-discharge health check-ins."""
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("health_checkins", lease_seconds=300) as acquired:
            if not acquired:
                return
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
                            lang = "en"

                            from app.services.consent import consent_service

                            if not await consent_service.accepts_engagement(
                                clinic["id"], appt["patient_phone"]
                            ):
                                logger.info(
                                    f"Skipping day+{offset_days} health check-in for "
                                    f"appointment {appt['id']} — patient has opted out "
                                    f"of engagement messages"
                                )
                                supabase.table("appointments").update(
                                    {flag_field: True}
                                ).eq("id", appt["id"]).execute()
                                continue

                            from app.templates.whatsapp_templates import get_message

                            first_name = (appt.get("patient_name") or "there").split()[0]
                            text = get_message(
                                "health_checkin",
                                lang,
                                name=first_name,
                                doctor=appt.get("doctor_name", ""),
                            )

                            sent = await whatsapp_service.send_interactive_buttons(
                                clinic,
                                appt["patient_phone"],
                                body=text,
                                buttons=[
                                    {"id": "checkin_ok", "title": "Feeling fine"},
                                    {"id": "checkin_concern", "title": "Still have symptoms"},
                                ],
                                _source="scheduler",
                            )

                            if not sent:
                                # An interactive message is freeform, so Meta
                                # refuses it outside the 24h customer-service
                                # window — which, day+3 and day+7 after a visit,
                                # is nearly always. Marking the flag anyway (the
                                # previous behaviour) burned the check-in on a
                                # send that never happened. Leave it unset so it
                                # lands if the patient writes in, and say plainly
                                # in the log why nothing went out.
                                logger.warning(
                                    f"Day+{offset_days} health check-in NOT delivered for "
                                    f"appointment {appt['id']} — patient is outside the 24h "
                                    f"window and this check-in has no approved template"
                                )
                                continue

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
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("doctor_leaves", lease_seconds=300) as acquired:
            if not acquired:
                return
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
                            supabase.table("appointments").update(
                                {"status": "cancelled"}
                            ).eq("id", appt["id"]).execute()

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

    async def drain_pending_retry_messages(self):
        """Replay patient messages that were deferred by a phone-lock timeout.

        conversation.handle_message() parks these with status='pending_retry'
        and a comment promising "automatic retry" — but nothing ever read them
        back, so every timed-out patient message was silently lost until the
        30-day purge removed the evidence. This is that missing reader.

        Only 'pending_retry' is drained: it is written by exactly one call site
        with a known compact payload. The 'retryable' rows from message_queue
        are an audit copy of a queue that already retries itself, and the older
        'pending' rows are crash reports with no replayable shape.

        Bounded by age rather than an attempt counter, so it needs no new
        column: each cycle retries, and anything still stuck past the window is
        marked 'failed' and logged.

        ponytail: a replay that times out again lands as a fresh row with a new
        window, so pathological lock contention can churn. Bounded per-row, not
        globally — add a retry_count column if that ever shows up in the data.
        """
        import json

        from app.services.distributed_lock import distributed_job_lock

        GIVE_UP_AFTER_MINUTES = 30

        async with distributed_job_lock("dlq_pending_retry_drain", lease_seconds=240) as acquired:
            if not acquired:
                return
            try:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(minutes=GIVE_UP_AFTER_MINUTES)
                ).isoformat()
                rows = (
                    # unscoped: platform-wide DLQ sweep; clinic comes from each payload
                    supabase.table("failed_messages")
                    .select("id,phone,payload,created_at")
                    .eq("status", "pending_retry")
                    .execute()
                ).data or []
                if not rows:
                    return

                from app.services.conversation import conversation_manager
                from app.services.tenant import get_clinic_by_id
                from app.utils.validators import mask_phone

                for row in rows:
                    row_id = row["id"]
                    if str(row.get("created_at") or "") < cutoff:
                        supabase.table("failed_messages").update(
                            {"status": "failed", "resolved_at": datetime.now(timezone.utc).isoformat()}
                        ).eq("id", row_id).execute()
                        logger.error(
                            f"ALERT dlq_drain: giving up on message {row_id} from "
                            f"{mask_phone(row.get('phone') or '')} after "
                            f"{GIVE_UP_AFTER_MINUTES}m of lock contention — patient got no reply"
                        )
                        continue

                    try:
                        payload = row.get("payload")
                        payload = json.loads(payload) if isinstance(payload, str) else (payload or {})
                        clinic = await get_clinic_by_id(payload.get("clinic_id"))
                        if not clinic:
                            raise ValueError(f"clinic {payload.get('clinic_id')} not found")

                        await conversation_manager.handle_message(
                            clinic=clinic,
                            phone=row.get("phone"),
                            message=payload.get("message") or "",
                            message_type=payload.get("message_type") or "text",
                            message_id=payload.get("message_id"),
                            interactive_data=payload.get("interactive_data"),
                        )
                        supabase.table("failed_messages").update(
                            {"status": "resolved", "resolved_at": datetime.now(timezone.utc).isoformat()}
                        ).eq("id", row_id).execute()
                        logger.info(f"dlq_drain: replayed message {row_id}")
                    except Exception as replay_err:
                        # Left as pending_retry so the next cycle tries again
                        # until the age window closes it out.
                        logger.warning(f"dlq_drain: replay of {row_id} failed: {replay_err}")

            except Exception as e:
                logger.error(f"dlq_pending_retry_drain job errored: {e}")

    async def alert_failed_lab_reports(self):
        """Alert on lab reports that gave up delivering.

        The 2026-08-25 outage sat undetected because nothing watched this table:
        50 reports reached status='failed' and no human was told. The log line
        is emitted unconditionally and *before* any WhatsApp attempt on purpose
        — the incidents most worth alerting on are the ones where WhatsApp is
        itself the broken channel, so an alert that only rides WhatsApp goes
        silent exactly when it matters.
        """
        from app.services.distributed_lock import distributed_job_lock

        async with distributed_job_lock("failed_lab_reports_alert", lease_seconds=300) as acquired:
            if not acquired:
                return
            try:
                since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                result = (
                    # unscoped: platform-wide reliability sweep across all tenants
                    supabase.table("lab_reports")
                    .select("clinic_id,report_name,error_message,delivery_updated_at")
                    .eq("status", "failed")
                    .gte("delivery_updated_at", since)
                    .execute()
                )
                rows = result.data or []
                if not rows:
                    return

                by_clinic: dict = {}
                for row in rows:
                    by_clinic.setdefault(row.get("clinic_id"), []).append(row)

                # Always on the log stream, whatever WhatsApp does next.
                logger.error(
                    f"ALERT failed_lab_reports: {len(rows)} report(s) permanently failed "
                    f"in the last hour across {len(by_clinic)} clinic(s). "
                    f"Sample error: {(rows[0].get('error_message') or '')[:200]}"
                )

                for clinic_id, clinic_rows in by_clinic.items():
                    try:
                        from app.services.tenant import get_clinic_by_id, get_clinic_contact

                        clinic = await get_clinic_by_id(clinic_id) if clinic_id else None
                        if not clinic:
                            continue
                        admin_phone = get_clinic_contact(clinic, "admin_phone", "") or get_clinic_contact(
                            clinic, "phone", settings.hospital_phone
                        )
                        if not admin_phone:
                            continue
                        alert = "\n".join([
                            "⚠️ Kriya AI delivery alert",
                            "",
                            f"{len(clinic_rows)} lab report(s) could not be delivered and have stopped retrying.",
                            "",
                            f"Reason: {(clinic_rows[0].get('error_message') or 'unknown')[:300]}",
                            "",
                            "Open Failed Deliveries in the admin panel to review and requeue.",
                        ])
                        await whatsapp_service.send_text(
                            clinic, admin_phone, alert, _source="scheduler",
                        )
                    except Exception as send_err:
                        logger.warning(
                            f"failed_lab_reports alert: could not notify clinic {clinic_id}: {send_err}"
                        )

            except Exception as e:
                logger.error(f"failed_lab_reports alert job errored: {e}")

    async def alert_failed_messages(self):
        """Check for unprocessed failed messages and alert admin."""
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("alert_failed_messages", lease_seconds=300) as acquired:
            if not acquired:
                return
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

                    try:
                        from app.services.tenant import resolve_tenant

                        clinic = await resolve_tenant(admin_phone)
                        await whatsapp_service.send_text(clinic, admin_phone, alert_msg, _source="scheduler")
                        logger.info(f"Sent failed messages alert: {pending_count} pending")
                    except Exception:
                        logger.warning(
                            f"ALERT: {pending_count} failed messages pending in dead-letter queue. "
                            f"Could not send WhatsApp alert — check Supabase manually."
                        )

            except Exception as e:
                logger.debug(f"Failed messages check skipped: {e}")

    async def alert_message_queue_fail_closed(self):
        """Alert admin if the message queue fail-closed rate is elevated."""
        global _last_fail_open_count
        try:
            from app.services.message_queue import get_fail_closed_count

            current = get_fail_closed_count()
            delta = current - _last_fail_open_count
            _last_fail_open_count = current

            if delta > 5:
                admin_phone = settings.hospital_phone
                alert_msg = (
                    f"⚠️ Message queue fail-closed / fail-open rate elevated: "
                    f"{delta} messages could not acquire idempotency lock due to database error since last check."
                )
                try:
                    from app.services.tenant import resolve_tenant

                    clinic = await resolve_tenant(admin_phone)
                    await whatsapp_service.send_text(clinic, admin_phone, alert_msg, _source="scheduler")
                    logger.warning(
                        f"Sent message queue fail-closed alert: {delta} fail-closed events"
                    )
                except Exception:
                    logger.warning(
                        f"ALERT: {delta} message queue fail-closed events occurred. "
                        f"Could not send WhatsApp alert."
                    )
        except Exception as e:
            logger.debug(f"Message queue fail-closed check skipped: {e}")

    # Backward-compatible alias
    alert_message_queue_fail_open = alert_message_queue_fail_closed

    async def cleanup_rate_limits(self):
        """Delete stale rate limit entries older than 1 hour."""
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("cleanup_rate_limits", lease_seconds=300) as acquired:
            if not acquired:
                return
            try:
                cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
                supabase.table("rate_limits").delete().lt("window_start", cutoff).execute()
                logger.info("Cleaned up stale rate limit entries")
            except Exception as e:
                logger.debug(f"Rate limits cleanup skipped: {e}")

    async def purge_expired_conversations(self):
        """Purge conversation sessions older than the configured purge window."""
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("purge_expired_conversations", lease_seconds=300) as acquired:
            if not acquired:
                return
            try:
                from app.services.data_retention import data_retention_service

                count = await data_retention_service.purge_expired_conversations()
                if count > 0:
                    logger.info(f"Scheduler: purged {count} expired conversation sessions")
            except Exception as e:
                logger.error(f"Conversation purge job failed: {e}")

    async def purge_expired_session_data(self):
        """Purge analytics events older than 12 months."""
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("purge_expired_session_data", lease_seconds=300) as acquired:
            if not acquired:
                return
            try:
                from app.services.data_retention import data_retention_service

                count = await data_retention_service.purge_expired_session_data()
                if count > 0:
                    logger.info(f"Scheduler: purged {count} expired analytics events")
            except Exception as e:
                logger.error(f"Analytics purge job failed: {e}")

    async def expire_stale_bookings(self):
        """Expire pending_payment bookings past their hold window."""
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("expire_stale_bookings", lease_seconds=60) as acquired:
            if not acquired:
                return
            try:
                from app.services.payment import payment_service

                count = await payment_service.expire_stale_bookings()
                if count > 0:
                    logger.info(f"Scheduler: processed {count} stale bookings")
            except Exception as e:
                logger.error(f"Stale bookings job failed: {e}")

    async def poll_recent_pending_payments(self):
        """Fast-poll Razorpay for recently-created pending_payment bookings."""
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("poll_recent_pending_payments", lease_seconds=25) as acquired:
            if not acquired:
                return
            try:
                from app.services.payment import payment_service

                count = await payment_service.poll_recent_pending_payments()
                if count > 0:
                    logger.info(f"Scheduler: fast-poll confirmed {count} booking(s)")
            except Exception as e:
                logger.error(f"Fast payment poll job failed: {e}")

    async def daily_payment_reconciliation(self):
        """Compare confirmed bookings against Razorpay settlements."""
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("daily_payment_reconciliation", lease_seconds=600) as acquired:
            if not acquired:
                return
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
        """Re-attempt delivery of lab reports stuck in 'pending_retry' status."""
        from app.services.distributed_lock import distributed_job_lock
        async with distributed_job_lock("lab_report_retry", lease_seconds=300) as acquired:
            if not acquired:
                return
            try:
                from app.services.lab_reports import LabReportService

                service = LabReportService()
                count = await service.retry_pending_deliveries()
                if count > 0:
                    logger.info(f"Scheduler: processed {count} pending lab report retries")
            except Exception as e:
                logger.debug(f"Lab report retry job skipped: {e}")


# Global instance
scheduler_service = SchedulerService()
