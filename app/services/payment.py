"""Payment service for Razorpay-gated appointment booking.

SECURITY INVARIANTS — if any of these are violated, treat it as a P0 bug:
  1. Never store card/UPI/bank details — Razorpay handles all PCI-scoped data.
  2. Never confirm a booking without a verified webhook or verified server-side poll.
  3. Every webhook is HMAC-SHA256 signature-verified before any field is trusted.
  4. All money operations are idempotent — duplicate webhooks are harmless.
  5. Amounts are ALWAYS in integer paise — never floats.
  6. Every state transition is logged to the append-only payment_events table.
  7. Fail closed: ambiguous states → pending_review, never auto-confirmed.

MULTI-TENANT RAZORPAY:
  Each clinic can store its own Razorpay credentials inside the clinics.config JSONB:
    {
      "razorpay_key_id":       "rzp_live_xxxxxx",
      "razorpay_key_secret":   "<secret>",
      "razorpay_webhook_secret": "<webhook_secret>"
    }
  If a clinic has no per-clinic keys, the global settings (env vars) are used as a
  transparent fallback — so single-clinic deployments need zero changes.
"""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.utils.security import PersistentRateLimiter

import httpx

from app.config import settings
from app.database import supabase

logger = logging.getLogger(__name__)


def get_razorpay_creds(clinic: dict) -> tuple[str, str, str]:
    """Extract Razorpay credentials from a clinic config with global settings fallback.

    Resolution order (per credential):
      1. clinic["config"]["razorpay_key_id"]       — per-clinic override
      2. settings.razorpay_key_id                   — global env-var fallback

    Returns:
        (key_id, key_secret, webhook_secret)
    """
    cfg: dict = clinic.get("config") or {}
    key_id = cfg.get("razorpay_key_id") or settings.razorpay_key_id
    key_secret = cfg.get("razorpay_key_secret") or settings.razorpay_key_secret
    webhook_secret = (
        cfg.get("razorpay_webhook_secret") or settings.razorpay_webhook_secret
    )
    return key_id, key_secret, webhook_secret


def resolve_payment_mode(clinic: dict) -> tuple[str, int]:
    """Resolve a clinic's payment mode with a fail-safe default.

    Returns:
        (mode, percent) where mode is "full" | "partial" | "none" and percent
        is 100 unless mode == "partial" (then it's the configured deposit %).

    Back-compat default: if config.payment_mode is unset, every existing
    clinic behaves exactly as it did before this feature existed — "full"
    if Razorpay keys are configured, "none" if they aren't.

    Fail-safe: "full"/"partial" is never returned without working keys, so a
    clinic that saves a payment-gated mode and later loses its Razorpay keys
    falls back to free direct booking instead of silently blocking bookings.
    """
    cfg = clinic.get("config") or {}
    key_id, key_secret, _ = get_razorpay_creds(clinic)
    configured = bool(key_id and key_secret)

    mode = cfg.get("payment_mode") or ("full" if configured else "none")
    if mode in ("full", "partial") and not configured:
        mode = "none"

    percent = cfg.get("payment_deposit_percent", 100) if mode == "partial" else 100
    return mode, percent


class PaymentService:
    """Razorpay payment integration for appointment booking."""

    def __init__(self):
        self._razorpay_base = "https://api.razorpay.com/v1"

    # ─────────────────────────────────────────────────────────────────────
    # 1. CREATE BOOKING + RAZORPAY ORDER
    # ─────────────────────────────────────────────────────────────────────

    async def create_booking_with_payment(
        self,
        clinic_id: str,
        patient_phone: str,
        patient_name: str,
        department: str,
        doctor_name: Optional[str],
        appointment_date: str,
        appointment_time: Optional[str],
        symptoms: str = "",
        patient_id: Optional[str] = None,
        clinic: Optional[dict] = None,
        branch_id: Optional[str] = None,
        branch_name: Optional[str] = None,
        deposit_percent: int = 100,
        booking_type: str = "consultation",
        lab_test_id: Optional[str] = None,
        lab_test_name: Optional[str] = None,
    ) -> dict:
        """Create a pending_payment booking and a Razorpay order.

        Args:
            clinic: Optional full clinic dict. If provided, per-clinic Razorpay
                    credentials are used. Falls back to global settings if None.
            branch_id: Optional branch UUID for multi-branch clinics.
            branch_name: Optional branch display name for multi-branch clinics.
            deposit_percent: 1-100. When < 100, only this fraction of the
                    doctor's full consultation_fee is charged now (the rest is
                    collected at the clinic). Defaults to 100 (full fee).

        Returns:
            dict with keys: success, booking_id, razorpay_order_id,
            payment_link, amount_paise, hold_expires_at, reason
        """
        # ── Resolve per-clinic Razorpay credentials ──
        key_id, key_secret, _ = get_razorpay_creds(clinic or {})

        # ── Determine fee based on booking type ──
        if booking_type == "lab_test":
            amount_paise = await self._get_lab_test_fee_paise(clinic_id, lab_test_id)
        else:
            amount_paise = await self._get_doctor_fee_paise(clinic_id, doctor_name)
        if deposit_percent < 100:
            amount_paise = round(amount_paise * deposit_percent / 100)

        hold_expires_at = (
            datetime.now(timezone.utc)
            + timedelta(minutes=settings.booking_hold_minutes)
        ).isoformat()

        # Generate booking ref
        from app.utils.helpers import generate_booking_reference

        booking_ref = generate_booking_reference()

        # ── INSERT with status='pending_payment' ──
        # If the partial UNIQUE constraint (clinic_id, doctor_name, appointment_date,
        # appointment_time) WHERE status IN ('pending_payment','confirmed') rejects
        # this, the slot is already taken.
        booking_data = {
            "clinic_id": clinic_id,
            "patient_id": patient_id,
            "patient_phone": patient_phone,
            "patient_name": patient_name,
            "department": department,
            "doctor_name": doctor_name,
            "appointment_date": appointment_date,
            "appointment_time": appointment_time,
            "symptoms": symptoms,
            "status": "pending_payment",
            "amount_paise": amount_paise,
            "hold_expires_at": hold_expires_at,
            "booking_ref": booking_ref,
            "booking_type": booking_type,
        }
        if booking_type == "lab_test":
            booking_data["lab_test_id"] = lab_test_id
            booking_data["lab_test_name"] = lab_test_name

        # Include branch info when booking at a specific branch
        if branch_id:
            booking_data["branch_id"] = branch_id
            booking_data["branch_name"] = branch_name or ""

        try:
            result = supabase.table("appointments").insert(booking_data).execute()
            if not result.data:
                return {"success": False, "reason": "insert_failed"}
            booking = result.data[0]
            booking_id = booking["id"]
        except Exception as e:
            error_msg = str(e).lower()
            if (
                "duplicate" in error_msg
                or "unique" in error_msg
                or "violates" in error_msg
            ):
                logger.info(
                    f"Slot taken (DB constraint): {doctor_name} {appointment_date} {appointment_time}"
                )
                return {"success": False, "reason": "slot_taken"}
            logger.error(f"Booking insert failed: {e}")
            return {"success": False, "reason": "error"}

        # ── Create Razorpay Payment Link ──
        # (Payment Links attach captured payments to a payment_link_id, not
        # an order_id — no separate Order object is needed for this flow.)
        try:
            link = await self._create_payment_link(
                amount_paise=amount_paise,
                booking_id=booking_id,
                booking_ref=booking_ref,
                patient_phone=patient_phone,
                patient_name=patient_name,
                key_id=key_id,
                key_secret=key_secret,
            )

            payment_link_id = link["id"]
            payment_link = link["short_url"]

            supabase.table("appointments").update(
                {"razorpay_payment_link_id": payment_link_id}
            ).eq("id", booking_id).execute()

            self._log_payment_event(
                booking_id,
                "payment_link_created",
                {
                    "razorpay_payment_link_id": payment_link_id,
                    "amount_paise": amount_paise,
                    "booking_ref": booking_ref,
                },
            )

            return {
                "success": True,
                "booking_id": booking_id,
                "booking_ref": booking_ref,
                "razorpay_payment_link_id": payment_link_id,
                "payment_link": payment_link,
                "amount_paise": amount_paise,
                "hold_expires_at": hold_expires_at,
            }

        except Exception as e:
            logger.error(f"Razorpay payment link creation failed: {e}")
            try:
                supabase.table("appointments").update({"status": "cancelled"}).eq(
                    "id", booking_id
                ).execute()
                self._log_payment_event(
                    booking_id, "payment_link_creation_failed", {"error": str(e)[:500]}
                )
            except Exception:
                pass
            return {"success": False, "reason": "razorpay_error"}

    # ─────────────────────────────────────────────────────────────────────
    # 2. WEBHOOK SIGNATURE VERIFICATION
    # ─────────────────────────────────────────────────────────────────────

    def verify_webhook_signature(
        self,
        raw_body: bytes,
        signature: str,
        webhook_secret: Optional[str] = None,
    ) -> bool:
        """Verify Razorpay webhook HMAC-SHA256 signature.

        MUST be called BEFORE parsing or trusting any field in the payload.

        Args:
            webhook_secret: Per-clinic webhook secret. Falls back to
                            settings.razorpay_webhook_secret if None/empty.
        """
        secret = webhook_secret or settings.razorpay_webhook_secret
        if not signature or not secret:
            return False

        expected = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    # ─────────────────────────────────────────────────────────────────────
    # 3. PROCESS PAYMENT WEBHOOK
    # ─────────────────────────────────────────────────────────────────────

    async def process_payment_webhook(
        self,
        raw_body: bytes,
        signature: str,
        webhook_secret: Optional[str] = None,
        alert_limiter: Optional["PersistentRateLimiter"] = None,
        alert_key: Optional[str] = None,
    ) -> dict:
        """Process a Razorpay webhook event.

        Args:
            webhook_secret: Per-clinic webhook secret resolved by the router.
                            Falls back to settings if None.
            alert_limiter: Optional PersistentRateLimiter used to throttle the
                            _alert_admin() call on repeated signature failures
                            (an unauthenticated attacker can otherwise flood
                            the hospital's own WhatsApp admin number). Callers
                            that omit this keep the old unthrottled behavior.
            alert_key: Key to rate-limit on (e.g. "{clinic_id}:{client_ip}").

        Returns: {"status": "ok"|"error"|"ignored", "code": 200|400}
        """
        # ── Step 1: Verify signature FIRST ──
        if not self.verify_webhook_signature(
            raw_body, signature, webhook_secret=webhook_secret
        ):
            self._log_payment_event_raw(
                None,
                "signature_failed",
                {
                    "signature_provided": (
                        signature[:20] + "..." if signature else "none"
                    ),
                    "body_length": len(raw_body),
                },
            )
            logger.warning(
                "⚠️ Razorpay webhook SIGNATURE FAILED — possible spoofing attempt"
            )
            should_alert = True
            if alert_limiter is not None:
                key = alert_key or "global"
                should_alert = not alert_limiter.check_and_record(key)
            if should_alert:
                await self._alert_admin(
                    "🚨 Payment webhook signature verification FAILED. Possible spoofing attempt."
                )
            return {"status": "error", "code": 400, "reason": "signature_failed"}

        # ── Step 2: Parse payload ──
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            logger.error("Razorpay webhook: invalid JSON body")
            return {"status": "error", "code": 400, "reason": "invalid_json"}

        event_type = payload.get("event", "")

        # We care about payment.captured and payment_link.paid events
        if event_type not in ("payment.captured", "payment_link.paid"):
            logger.info(f"Razorpay webhook: ignoring event type '{event_type}'")
            return {"status": "ignored", "code": 200}

        # ── Step 3: Extract payment details ──
        payment_entity = (
            payload.get("payload", {}).get("payment", {}).get("entity", {})
        )
        payment_link_entity = (
            payload.get("payload", {}).get("payment_link", {}).get("entity", {})
        )

        payment_id = (
            payment_entity.get("id")
            or payment_link_entity.get("payment_id")
            or payment_link_entity.get("id")
        )
        amount_paid = (
            payment_entity.get("amount")
            or payment_link_entity.get("amount_paid")
            or payment_link_entity.get("amount")
        )
        notes = (
            payment_entity.get("notes")
            or payment_link_entity.get("notes")
            or {}
        )
        payment_link_id = (
            payment_link_entity.get("id")
            or payment_entity.get("invoice_id")
            or payment_entity.get("payment_link_id")
        )

        if not payment_id:
            logger.error("Razorpay webhook: missing payment_id")
            return {"status": "error", "code": 400, "reason": "missing_fields"}

        # ── Step 4: Idempotency check ──
        existing_confirmed = (
            supabase.table("appointments")
            .select("id")
            .eq("payment_id", payment_id)
            .eq("status", "confirmed")
            .execute()
        )

        if existing_confirmed.data:
            logger.info(
                f"Razorpay webhook: payment_id {payment_id} already processed (idempotent)"
            )
            return {"status": "ok", "code": 200, "reason": "already_processed"}

        # ── Step 5: Look up booking ──
        # Match on payment_link_id first, fallback to notes.booking_id and booking_ref
        booking_result = None
        if payment_link_id:
            booking_result = (
                supabase.table("appointments")
                .select("*")
                .eq("razorpay_payment_link_id", payment_link_id)
                .execute()
            )

        if not booking_result or not booking_result.data:
            booking_id_from_notes = notes.get("booking_id")
            if booking_id_from_notes:
                booking_result = (
                    supabase.table("appointments")
                    .select("*")
                    .eq("id", booking_id_from_notes)
                    .execute()
                )

        if not booking_result or not booking_result.data:
            booking_ref = (
                notes.get("booking_ref")
                or payment_link_entity.get("reference_id")
                or (
                    payment_entity.get("description", "")
                    .replace("Appointment booking ", "")
                    .strip()
                )
            )
            if booking_ref:
                booking_result = (
                    supabase.table("appointments")
                    .select("*")
                    .eq("booking_ref", booking_ref)
                    .execute()
                )

        if not booking_result or not booking_result.data:
            logger.error(
                f"Razorpay webhook: no booking found for payment_link {payment_link_id}"
            )
            self._log_payment_event_raw(
                None,
                "webhook_received",
                {
                    "payment_id": payment_id,
                    "payment_link_id": payment_link_id,
                    "error": "no_booking_found",
                    "raw": payload,
                },
            )
            return {"status": "error", "code": 200, "reason": "booking_not_found"}

        booking = booking_result.data[0]
        booking_id = booking["id"]

        # Log webhook received
        self._log_payment_event(
            booking_id,
            "webhook_received",
            {
                "payment_id": payment_id,
                "payment_link_id": payment_link_id,
                "amount_paid": amount_paid,
            },
        )

        self._log_payment_event(
            booking_id,
            "signature_verified",
            {
                "payment_id": payment_id,
            },
        )

        # ── Step 6: Amount mismatch check ──
        expected_amount = booking.get("amount_paise", 0)
        if amount_paid != expected_amount:
            logger.warning(
                f"⚠️ Amount mismatch: expected {expected_amount}, got {amount_paid} "
                f"for booking {booking_id}"
            )
            self._log_payment_event(
                booking_id,
                "mismatch_flagged",
                {
                    "expected_paise": expected_amount,
                    "received_paise": amount_paid,
                    "payment_id": payment_id,
                },
            )
            # Route to pending_review — NEVER auto-confirm on mismatch
            supabase.table("appointments").update(
                {
                    "status": "pending_review",
                    "payment_id": payment_id,
                }
            ).eq("id", booking_id).execute()

            await self._alert_admin(
                f"🚨 Payment AMOUNT MISMATCH for booking {booking.get('booking_ref', booking_id)}\n"
                f"Expected: ₹{expected_amount/100:.2f}\n"
                f"Received: ₹{amount_paid/100:.2f}\n"
                f"Patient: {booking.get('patient_phone')}\n"
                f"Status: Moved to pending_review — needs manual resolution."
            )
            return {"status": "ok", "code": 200, "reason": "amount_mismatch"}

        # ── Step 7: Check booking is in a valid state to confirm ──
        current_status = booking.get("status")
        if current_status == "confirmed":
            # Already confirmed — idempotent
            logger.info(f"Booking {booking_id} already confirmed (idempotent)")
            return {"status": "ok", "code": 200, "reason": "already_confirmed"}

        if current_status not in ("pending_payment", "expired"):
            # Booking is in an unexpected state
            logger.warning(
                f"Booking {booking_id} in unexpected state '{current_status}' during webhook"
            )
            self._log_payment_event(
                booking_id,
                "mismatch_flagged",
                {
                    "reason": f"unexpected_status_{current_status}",
                    "payment_id": payment_id,
                },
            )
            supabase.table("appointments").update(
                {
                    "status": "pending_review",
                    "payment_id": payment_id,
                }
            ).eq("id", booking_id).execute()
            return {"status": "ok", "code": 200, "reason": "unexpected_state"}

        # ── Step 8: CONFIRM the booking (Atomic Update) ──
        update_result = (
            supabase.table("appointments")
            .update(
                {
                    "status": "confirmed",
                    "payment_id": payment_id,
                }
            )
            .eq("id", booking_id)
            .in_("status", ["pending_payment", "expired"])
            .execute()
        )

        if not update_result.data:
            # Atomic update modified 0 rows because a concurrent process already confirmed it!
            logger.info(
                f"Razorpay webhook atomic check: booking {booking_id} already confirmed by concurrent process (idempotent)"
            )
            return {"status": "ok", "code": 200, "reason": "already_confirmed"}

        self._log_payment_event(
            booking_id,
            "confirmed",
            {
                "payment_id": payment_id,
                "amount_paise": amount_paid,
                "payment_link_id": payment_link_id,
            },
        )

        logger.info(f"✅ Booking {booking_id} CONFIRMED via payment {payment_id}")

        # ── Step 9: Notify patient + admin ──
        await self._increment_patient_visit_count(
            booking.get("clinic_id"), booking.get("patient_phone")
        )
        await self._notify_payment_confirmed(booking)

        return {"status": "ok", "code": 200}

    # ─────────────────────────────────────────────────────────────────────
    # 4. EXPIRE STALE BOOKINGS
    # ─────────────────────────────────────────────────────────────────────

    async def expire_stale_bookings(self) -> int:
        """Expire pending_payment bookings past their hold window.

        IMPORTANT: Before expiring, check Razorpay order status.
        If Razorpay shows 'paid' but we missed the webhook, CONFIRM instead.
        Never silently drop a paid booking.

        Each stale booking's clinic is fetched individually so the correct
        per-clinic Razorpay credentials are used for the order status check.

        Returns: number of bookings processed
        """
        now = datetime.now(timezone.utc).isoformat()

        stale = (
            supabase.table("appointments")
            .select("*")
            .eq("status", "pending_payment")
            .lt("hold_expires_at", now)
            .execute()
        )

        if not stale.data:
            return 0

        count = 0
        for booking in stale.data:
            booking_id = booking["id"]
            order_id = booking.get("razorpay_order_id")

            try:
                # ── Resolve per-clinic Razorpay creds for this booking ──
                clinic_for_booking: dict = {}
                clinic_id_for_booking = booking.get("clinic_id")
                if clinic_id_for_booking:
                    try:
                        from app.services.tenant import get_clinic_by_id

                        clinic_for_booking = await get_clinic_by_id(
                            clinic_id_for_booking
                        )
                    except Exception as ce:
                        logger.warning(
                            f"Could not fetch clinic {clinic_id_for_booking} for expiry: {ce}"
                        )

                key_id, key_secret, _ = get_razorpay_creds(clinic_for_booking)

                # ── Recovery path: check Razorpay before expiring ──
                if order_id:
                    rz_status = await self._check_razorpay_order_status(
                        order_id, key_id=key_id, key_secret=key_secret
                    )

                    if rz_status == "paid":
                        # Webhook was missed — recover by confirming
                        logger.info(
                            f"Recovery: booking {booking_id} was paid on Razorpay but webhook missed. Confirming."
                        )

                        # Fetch payment details from Razorpay
                        payment_info = await self._get_razorpay_order_payments(
                            order_id, key_id=key_id, key_secret=key_secret
                        )
                        payment_id = payment_info.get(
                            "payment_id", f"recovery_{order_id}"
                        )

                        recovery_update = (
                            supabase.table("appointments")
                            .update(
                                {
                                    "status": "confirmed",
                                    "payment_id": payment_id,
                                }
                            )
                            .eq("id", booking_id)
                            .eq("status", "pending_payment")
                            .execute()
                        )

                        if not recovery_update.data:
                            logger.info(
                                f"Recovery skipped for booking {booking_id}: already updated concurrently"
                            )
                            continue

                        self._log_payment_event(
                            booking_id,
                            "recovery_confirmed",
                            {
                                "razorpay_order_status": "paid",
                                "payment_id": payment_id,
                                "recovery_reason": "webhook_missed",
                            },
                        )

                        await self._increment_patient_visit_count(
                            booking.get("clinic_id"), booking.get("patient_phone")
                        )
                        await self._notify_payment_confirmed(booking)
                        count += 1
                        continue

                # ── Normal expiry path ──
                supabase.table("appointments").update({"status": "expired"}).eq(
                    "id", booking_id
                ).eq("status", "pending_payment").execute()

                self._log_payment_event(
                    booking_id,
                    "hold_expired",
                    {
                        "hold_expires_at": booking.get("hold_expires_at"),
                        "expired_at": now,
                    },
                )

                logger.info(f"Expired stale booking {booking_id}")
                count += 1

            except Exception as e:
                logger.error(f"Error processing stale booking {booking_id}: {e}")

        return count

    # ─────────────────────────────────────────────────────────────────────
    # 5. REFUNDS
    # ─────────────────────────────────────────────────────────────────────

    async def initiate_refund(
        self, booking_id: str, reason: str = "", clinic: Optional[dict] = None
    ) -> dict:
        """Initiate a refund for a confirmed booking.

        Checks refund eligibility (4+ hours before slot), calls Razorpay
        Refund API with an idempotency key, and logs all transitions.

        Args:
            clinic: Optional clinic dict. Used to resolve per-clinic Razorpay
                    credentials. Falls back to global settings if None.

        Returns: {"success": bool, "refund_id": str, "reason": str}
        """
        key_id, key_secret, _ = get_razorpay_creds(clinic or {})
        # ── Look up booking ──
        booking_result = (
            supabase.table("appointments").select("*").eq("id", booking_id).execute()
        )

        if not booking_result.data:
            return {"success": False, "reason": "booking_not_found"}

        booking = booking_result.data[0]

        if booking["status"] not in ("confirmed", "pending_review"):
            return {
                "success": False,
                "reason": f"cannot_refund_status_{booking['status']}",
            }

        if not booking.get("payment_id"):
            return {"success": False, "reason": "no_payment_to_refund"}

        # ── Refund eligibility check ──
        slot_datetime = self._parse_slot_datetime(
            booking["appointment_date"], booking["appointment_time"]
        )
        if slot_datetime:
            hours_until_slot = (
                slot_datetime - datetime.now(timezone.utc)
            ).total_seconds() / 3600
            if hours_until_slot < settings.refund_window_hours:
                return {
                    "success": False,
                    "reason": f"refund_window_closed_need_{settings.refund_window_hours}h_before_slot",
                }

        # ── Generate idempotency key ──
        idempotency_key = f"refund_{booking_id}_{uuid.uuid4().hex[:8]}"

        # ── Log refund_initiated IMMEDIATELY (before gateway call) ──
        self._log_payment_event(
            booking_id,
            "refund_initiated",
            {
                "payment_id": booking["payment_id"],
                "amount_paise": booking["amount_paise"],
                "reason": reason,
                "idempotency_key": idempotency_key,
            },
        )

        # ── Call Razorpay Refund API ──
        try:
            refund_result = await self._create_razorpay_refund(
                payment_id=booking["payment_id"],
                amount_paise=booking["amount_paise"],
                reason=reason,
                idempotency_key=idempotency_key,
                key_id=key_id,
                key_secret=key_secret,
            )

            refund_id = refund_result.get("id", "")

            # Update booking status
            supabase.table("appointments").update({"status": "refunded"}).eq(
                "id", booking_id
            ).execute()

            # Log refund_completed only after gateway confirms
            self._log_payment_event(
                booking_id,
                "refund_completed",
                {
                    "refund_id": refund_id,
                    "amount_paise": booking["amount_paise"],
                    "razorpay_response": refund_result,
                },
            )

            logger.info(f"✅ Refund completed for booking {booking_id}: {refund_id}")
            return {"success": True, "refund_id": refund_id, "status": "completed"}

        except Exception as e:
            logger.error(f"Refund failed for booking {booking_id}: {e}")
            self._log_payment_event(
                booking_id,
                "refund_failed",
                {
                    "error": str(e)[:500],
                    "payment_id": booking["payment_id"],
                },
            )
            return {"success": False, "reason": f"razorpay_error: {str(e)[:200]}"}

    # ─────────────────────────────────────────────────────────────────────
    # 6. MANUAL ADMIN ACTIONS
    # ─────────────────────────────────────────────────────────────────────

    async def admin_confirm_booking(
        self, booking_id: str, clinic_id: str = "default", admin_notes: str = ""
    ) -> dict:
        """Manually confirm a pending_review booking (admin override), scoped to clinic_id."""
        query = supabase.table("appointments").select("*").eq("id", booking_id)
        if clinic_id and clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        booking_result = query.execute()

        if not booking_result.data:
            return {"success": False, "reason": "booking_not_found"}

        booking = booking_result.data[0]

        if booking["status"] != "pending_review":
            return {
                "success": False,
                "reason": f"can_only_confirm_pending_review_not_{booking['status']}",
            }

        supabase.table("appointments").update({"status": "confirmed"}).eq(
            "id", booking_id
        ).execute()

        self._log_payment_event(
            booking_id,
            "manual_confirm",
            {
                "admin_notes": admin_notes,
                "previous_status": booking["status"],
            },
        )

        logger.info(f"Admin manually confirmed booking {booking_id}")
        await self._increment_patient_visit_count(
            booking.get("clinic_id"), booking.get("patient_phone")
        )
        await self._notify_payment_confirmed(booking)
        return {"success": True}

    async def admin_reject_booking(
        self, booking_id: str, clinic_id: str = "default", admin_notes: str = ""
    ) -> dict:
        """Manually reject a pending_review booking + initiate refund, scoped to clinic_id."""
        query = supabase.table("appointments").select("*").eq("id", booking_id)
        if clinic_id and clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        booking_result = query.execute()

        if not booking_result.data:
            return {"success": False, "reason": "booking_not_found"}

        booking = booking_result.data[0]

        if booking["status"] != "pending_review":
            return {
                "success": False,
                "reason": f"can_only_reject_pending_review_not_{booking['status']}",
            }

        # Cancel + refund
        supabase.table("appointments").update({"status": "cancelled"}).eq(
            "id", booking_id
        ).execute()

        self._log_payment_event(
            booking_id,
            "manual_reject",
            {
                "admin_notes": admin_notes,
            },
        )

        # Initiate refund if payment was captured
        if booking.get("payment_id"):
            await self.initiate_refund(
                booking_id, reason=f"Admin rejected: {admin_notes}"
            )

        return {"success": True}

    async def admin_cancel_confirmed_booking(
        self, booking_id: str, clinic_id: str = "default", admin_notes: str = ""
    ) -> dict:
        """Cancel a CONFIRMED appointment from the admin panel.

        The admin panel's Appointments page "Cancel" button (DELETE
        /admin/appointments/{id}) used to set status='cancelled' directly via
        raw Supabase update — for a Razorpay-paid booking that left the
        patient's payment captured with no refund and no notification, even
        though it's shown right next to bookings that DID go through the
        proper reject/refund path (admin_reject_booking). This routes
        paid bookings through initiate_refund() instead, and always notifies
        the patient over WhatsApp so the bot conversation stays in sync with
        what the admin just did.
        """
        query = supabase.table("appointments").select("*").eq("id", booking_id)
        if clinic_id and clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        booking_result = query.execute()

        if not booking_result.data:
            return {"success": False, "reason": "booking_not_found"}

        booking = booking_result.data[0]

        if booking["status"] != "confirmed":
            return {
                "success": False,
                "reason": f"can_only_cancel_confirmed_not_{booking['status']}",
            }

        if booking.get("payment_id"):
            from app.services.tenant import get_clinic_by_id

            clinic = await get_clinic_by_id(booking.get("clinic_id", "default"))
            refund_result = await self.initiate_refund(
                booking_id, reason=admin_notes or "Cancelled by admin", clinic=clinic
            )
            if not refund_result["success"]:
                return refund_result
        else:
            supabase.table("appointments").update({"status": "cancelled"}).eq(
                "id", booking_id
            ).execute()
            self._log_payment_event(
                booking_id, "admin_cancel", {"admin_notes": admin_notes}
            )

        await self._notify_booking_cancelled(
            booking, refunded=bool(booking.get("payment_id"))
        )
        return {"success": True}

    # ─────────────────────────────────────────────────────────────────────
    # 7. RECONCILIATION
    # ─────────────────────────────────────────────────────────────────────

    async def get_daily_reconciliation(self, date_str: str = None) -> dict:
        """Compare confirmed bookings against Razorpay for a given date.

        Returns a reconciliation summary. Discrepancies must be reviewed by a human.
        """
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Get confirmed bookings for the date
        bookings = (
            supabase.table("appointments")
            .select("id, amount_paise, payment_id, booking_ref, patient_phone")
            .eq("status", "confirmed")
            .eq("appointment_date", date_str)
            .not_.is_("payment_id", "null")
            .execute()
        )

        total_bookings = len(bookings.data) if bookings.data else 0
        total_amount = sum(b.get("amount_paise", 0) for b in (bookings.data or []))

        # Get pending_review bookings
        pending_review = (
            supabase.table("appointments")
            .select("id")
            .eq("status", "pending_review")
            .eq("appointment_date", date_str)
            .execute()
        )

        return {
            "date": date_str,
            "confirmed_count": total_bookings,
            "confirmed_total_paise": total_amount,
            "confirmed_total_rupees": total_amount / 100,
            "pending_review_count": (
                len(pending_review.data) if pending_review.data else 0
            ),
            "note": "Compare confirmed_total_rupees against Razorpay dashboard settlements for this date.",
        }

    # ─────────────────────────────────────────────────────────────────────
    # PRIVATE HELPERS
    # ─────────────────────────────────────────────────────────────────────

    async def _get_doctor_fee_paise(self, clinic_id: str, doctor_name: str) -> int:
        """Get the doctor's consultation fee in paise. Falls back to config default."""
        try:
            result = (
                supabase.table("doctors")
                .select("consultation_fee")
                .eq("clinic_id", clinic_id)
                .eq("name", doctor_name)
                .execute()
            )

            if result.data and result.data[0].get("consultation_fee"):
                # consultation_fee is stored in rupees, convert to paise
                return int(result.data[0]["consultation_fee"]) * 100

        except Exception as e:
            logger.error(f"Error fetching doctor fee: {e}")

        return settings.booking_fee_paise

    async def _get_lab_test_fee_paise(self, clinic_id: str, lab_test_id: str) -> int:
        """Get a lab test's price in paise directly from the catalog."""
        try:
            result = (
                supabase.table("lab_tests")
                .select("price_paise")
                .eq("clinic_id", clinic_id)
                .eq("id", lab_test_id)
                .execute()
            )
            if result.data and result.data[0].get("price_paise"):
                return int(result.data[0]["price_paise"])
        except Exception as e:
            logger.error(f"Error fetching lab test fee: {e}")

        return settings.booking_fee_paise

    async def _create_razorpay_order(
        self,
        amount_paise: int,
        booking_id: str,
        booking_ref: str,
        patient_phone: str,
        key_id: str = "",
        key_secret: str = "",
    ) -> dict:
        """Create a Razorpay Order via their API.

        Args:
            key_id:     Per-clinic key ID (or global fallback).
            key_secret: Per-clinic key secret (or global fallback).
        """
        effective_key_id = key_id or settings.razorpay_key_id
        effective_key_secret = key_secret or settings.razorpay_key_secret

        if not effective_key_id or not effective_key_secret:
            raise ValueError("Razorpay API credentials not configured")

        order_data = {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": booking_ref,
            "notes": {
                "booking_id": booking_id,
                "booking_ref": booking_ref,
                "patient_phone": patient_phone,
            },
            "payment_capture": 1,  # Auto-capture payments
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._razorpay_base}/orders",
                json=order_data,
                auth=(effective_key_id, effective_key_secret),
                timeout=15.0,
            )
            response.raise_for_status()
            return response.json()

    async def _create_payment_link(
        self,
        amount_paise: int,
        booking_id: str,
        booking_ref: str,
        patient_phone: str,
        patient_name: str,
        key_id: str = "",
        key_secret: str = "",
    ) -> dict:
        """Create a Razorpay Payment Link — a real hosted checkout page.

        Unlike the old `checkout/embedded` API endpoint (which requires
        Razorpay's checkout.js to render and is NOT a standalone browsable
        page), this returns a short_url (rzp.io/i/xxxxx) that works when
        tapped directly from a WhatsApp message on a mobile browser.
        """
        effective_key_id = key_id or settings.razorpay_key_id
        effective_key_secret = key_secret or settings.razorpay_key_secret

        link_data = {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "description": f"Appointment booking {booking_ref}",
            "customer": {
                "name": patient_name,
                "contact": patient_phone,
            },
            "notify": {"sms": False, "email": False},
            "reference_id": booking_ref,
            "notes": {"booking_id": booking_id, "booking_ref": booking_ref},
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._razorpay_base}/payment_links",
                json=link_data,
                auth=(effective_key_id, effective_key_secret),
                timeout=15.0,
            )
            response.raise_for_status()
            return response.json()

    async def _check_razorpay_order_status(
        self,
        order_id: str,
        key_id: str = "",
        key_secret: str = "",
    ) -> str:
        """Check order status from Razorpay (for recovery/expiry path)."""
        effective_key_id = key_id or settings.razorpay_key_id
        effective_key_secret = key_secret or settings.razorpay_key_secret
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._razorpay_base}/orders/{order_id}",
                    auth=(effective_key_id, effective_key_secret),
                    timeout=10.0,
                )
                response.raise_for_status()
                return response.json().get("status", "unknown")
        except Exception as e:
            logger.error(f"Error checking Razorpay order status for {order_id}: {e}")
            return "unknown"

    async def _get_razorpay_order_payments(
        self,
        order_id: str,
        key_id: str = "",
        key_secret: str = "",
    ) -> dict:
        """Get payment details for a Razorpay order."""
        effective_key_id = key_id or settings.razorpay_key_id
        effective_key_secret = key_secret or settings.razorpay_key_secret
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._razorpay_base}/orders/{order_id}/payments",
                    auth=(effective_key_id, effective_key_secret),
                    timeout=10.0,
                )
                response.raise_for_status()
                data = response.json()
                items = data.get("items", [])
                if items:
                    return {"payment_id": items[0].get("id", "")}
        except Exception as e:
            logger.error(f"Error fetching Razorpay order payments for {order_id}: {e}")
        return {}

    async def _create_razorpay_refund(
        self,
        payment_id: str,
        amount_paise: int,
        reason: str,
        idempotency_key: str,
        key_id: str = "",
        key_secret: str = "",
    ) -> dict:
        """Call Razorpay Refund API."""
        effective_key_id = key_id or settings.razorpay_key_id
        effective_key_secret = key_secret or settings.razorpay_key_secret
        refund_data = {
            "amount": amount_paise,
            "speed": "normal",
            "notes": {
                "reason": reason[:200],
            },
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._razorpay_base}/payments/{payment_id}/refund",
                json=refund_data,
                auth=(effective_key_id, effective_key_secret),
                headers={"X-Razorpay-Idempotency-Key": idempotency_key},
                timeout=15.0,
            )
            response.raise_for_status()
            return response.json()

    def _log_payment_event(
        self, booking_id: str, event_type: str, payload: dict
    ) -> None:
        """Log to payment_events audit table. NEVER skip this."""
        try:
            supabase.table("payment_events").insert(
                {
                    "booking_id": booking_id,
                    "event_type": event_type,
                    "raw_payload": json.dumps(payload, default=str),
                }
            ).execute()
        except Exception as e:
            # If audit logging fails, that is itself a bug — log loudly
            logger.error(f"CRITICAL: Failed to write payment_event ({event_type}): {e}")

    async def _increment_patient_visit_count(
        self, clinic_id: Optional[str], patient_phone: Optional[str]
    ) -> None:
        """Increment patients.visit_count when a Razorpay-gated booking is confirmed.

        The direct (non-Razorpay) booking path increments this in
        app.database.book_appointment(). This path inserts appointments
        directly via Supabase, bypassing that helper, so visit_count must be
        kept in sync here too — otherwise the admin Patients page "Visits"
        column and the bot's own returning-patient detection
        (conversation.py checks patient.get("visit_count", 0) > 0) go stale
        for every clinic that has Razorpay configured.
        """
        if not clinic_id or not patient_phone:
            return
        try:
            from app.database import get_patient_by_phone, update_patient

            patient = await get_patient_by_phone(clinic_id, patient_phone)
            if patient:
                new_count = (patient.get("visit_count") or 0) + 1
                await update_patient(
                    clinic_id, patient_phone, {"visit_count": new_count}
                )
        except Exception as e:
            logger.error(f"Failed to increment patient visit_count: {e}")

    def _log_payment_event_raw(
        self, booking_id: Optional[str], event_type: str, payload: dict
    ) -> None:
        """Log payment event even when booking_id might be None (e.g. signature failures).

        Orphan events (no booking_id) go to webhook_security_events instead
        of payment_events, since payment_events.booking_id is a required FK —
        this keeps signature-failure/spoofing-attempt events queryable in the
        DB for forensic replay instead of only living in rotated app logs.
        """
        try:
            if booking_id:
                supabase.table("payment_events").insert(
                    {
                        "booking_id": booking_id,
                        "event_type": event_type,
                        "raw_payload": json.dumps(payload, default=str),
                    }
                ).execute()
            else:
                supabase.table("webhook_security_events").insert(
                    {
                        "event_type": event_type,
                        "raw_payload": json.dumps(payload, default=str),
                    }
                ).execute()
        except Exception as e:
            logger.error(
                f"CRITICAL: Failed to write raw payment_event ({event_type}): {e}"
            )
            logger.warning(
                f"Payment event without persisted record: {event_type} — "
                f"{json.dumps(payload, default=str)}"
            )

    async def _notify_payment_confirmed(self, booking: dict) -> None:
        """Send WhatsApp confirmation message to the patient after payment is verified."""
        try:
            from app.services.whatsapp import whatsapp_service
            from app.services.tenant import get_clinic_by_id

            clinic = await get_clinic_by_id(booking.get("clinic_id", "default"))

            date_display = booking.get("appointment_date", "")
            try:
                from datetime import datetime as dt

                date_display = dt.strptime(date_display, "%Y-%m-%d").strftime(
                    "%d %b %Y"
                )
            except Exception:
                pass

            amount_rupees = booking.get("amount_paise", 0) / 100

            if booking.get("booking_type") == "lab_test":
                msg = (
                    f"✅ *Payment Confirmed — Test Booked!*\n\n"
                    f"📋 *Booking Ref:* {booking.get('booking_ref', 'N/A')}\n"
                    f"🧪 *Test:* {booking.get('lab_test_name', 'N/A')}\n"
                    f"📅 *Collection Date:* {date_display}\n"
                    f"💰 *Paid:* ₹{amount_rupees:.0f}\n\n"
                    f"📌 Please arrive during our sample collection hours with a valid ID.\n\n"
                    f"_Cancellation with full refund available up to {settings.refund_window_hours} hours before your collection date._"
                )
            else:
                msg = (
                    f"✅ *Payment Confirmed — Appointment Booked!*\n\n"
                    f"📋 *Booking Ref:* {booking.get('booking_ref', 'N/A')}\n"
                    f"👨‍⚕️ *Doctor:* {booking.get('doctor_name', 'N/A')}\n"
                    f"🏥 *Department:* {booking.get('department', 'N/A')}\n"
                    f"📅 *Date:* {date_display}\n"
                    f"🕐 *Time:* {booking.get('appointment_time', 'N/A')}\n"
                    f"💰 *Paid:* ₹{amount_rupees:.0f}\n\n"
                    f"📌 Please arrive 15 minutes early with any relevant medical records.\n\n"
                    f"_Cancellation with full refund available up to {settings.refund_window_hours} hours before your appointment. "
                    f"No-show bookings are non-refundable._"
                )

            # Append clinic location — skip for branch bookings, which already
            # have their own branch-specific address shown during booking
            if not booking.get("branch_id"):
                from app.services.tenant import get_clinic_contact

                clinic_address = get_clinic_contact(
                    clinic, "address", settings.hospital_address
                )
                clinic_maps_link = get_clinic_contact(
                    clinic, "maps_link", settings.hospital_maps_link
                )
                if clinic_address or clinic_maps_link:
                    location_line = f"\n\n📍 Location: {clinic.get('name', settings.hospital_name)}"
                    if clinic_address:
                        location_line += f", {clinic_address}"
                    if clinic_maps_link:
                        location_line += f"\nGoogle Maps: {clinic_maps_link}"
                    msg += location_line

            await whatsapp_service.send_text(clinic, booking["patient_phone"], msg, _source="payment")
            logger.info(
                f"Sent payment confirmation to {booking['patient_phone'][:6]}***"
            )

            follow_up_msg = {
                "en": "What would you like to do next?",
            }.get("en", "What would you like to do next?")
            await whatsapp_service.send_interactive_buttons(
                clinic,
                booking["patient_phone"],
                body=follow_up_msg,
                buttons=[{"id": "main_menu", "title": "Main Menu"}],
                _source="payment",
            )

            from app.services.conversation import conversation_manager

            await conversation_manager.update_state(
                clinic, booking["patient_phone"], "main_menu"
            )

        except Exception as e:
            logger.error(f"Failed to send payment confirmation notification: {e}")

    async def _notify_booking_cancelled(self, booking: dict, refunded: bool) -> None:
        """Send WhatsApp notice to the patient after an admin cancels their booking."""
        try:
            from app.services.whatsapp import whatsapp_service
            from app.services.tenant import get_clinic_by_id

            clinic = await get_clinic_by_id(booking.get("clinic_id", "default"))

            date_display = booking.get("appointment_date", "")
            try:
                from datetime import datetime as dt

                date_display = dt.strptime(date_display, "%Y-%m-%d").strftime(
                    "%d %b %Y"
                )
            except Exception:
                pass

            refund_line = ""
            if refunded:
                amount_rupees = booking.get("amount_paise", 0) / 100
                refund_line = (
                    f"💰 A full refund of ₹{amount_rupees:.0f} has been initiated and "
                    f"will reflect in your account within 5-7 business days.\n\n"
                )

            msg = (
                f"❌ *Appointment Cancelled*\n\n"
                f"📋 *Booking Ref:* {booking.get('booking_ref', 'N/A')}\n"
                f"👨‍⚕️ *Doctor:* {booking.get('doctor_name', 'N/A')}\n"
                f"📅 *Date:* {date_display}\n"
                f"🕐 *Time:* {booking.get('appointment_time', 'N/A')}\n\n"
                f"{refund_line}"
                f"Please contact us at {clinic.get('whatsapp_number', '')} if you have "
                f"any questions or would like to book a new appointment."
            )

            await whatsapp_service.send_text(clinic, booking["patient_phone"], msg, _source="payment")
            logger.info(
                f"Sent cancellation notice to {booking['patient_phone'][:6]}***"
            )

        except Exception as e:
            logger.error(f"Failed to send cancellation notification: {e}")

    async def _alert_admin(self, message: str) -> None:
        """Send alert to admin phone."""
        try:
            from app.services.whatsapp import whatsapp_service
            from app.services.tenant import resolve_tenant

            admin_phone = settings.hospital_phone
            clinic = await resolve_tenant(admin_phone)
            await whatsapp_service.send_text(clinic, admin_phone, message, _source="payment")
        except Exception as e:
            logger.error(f"Failed to send admin alert: {e}")
            # At minimum, log the alert content
            logger.warning(f"ADMIN ALERT (undelivered): {message}")

    def _parse_slot_datetime(self, date_str: str, time_str: str) -> Optional[datetime]:
        """Parse appointment date+time into a timezone-aware datetime."""
        try:
            # IST offset (UTC+5:30)
            from datetime import timezone as tz

            ist = tz(timedelta(hours=5, minutes=30))
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            return dt.replace(tzinfo=ist)
        except Exception:
            return None


# Global instance
payment_service = PaymentService()
