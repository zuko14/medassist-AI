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

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from app.utils.security import PersistentRateLimiter

import httpx

from app.config import settings
from app.database import supabase, is_valid_clinic_scope
from app.utils.helpers import format_slot_time
from app.database import sb  # T5.1: off-loop query execution

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
        doctor_id: Optional[str] = None,
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
            doctor_id: Optional doctor UUID. If omitted, resolved from doctor_name.

        Returns:
            dict with keys: success, booking_id, booking_ref,
            razorpay_payment_link_id, payment_link, amount_paise,
            hold_expires_at, reason
        """
        # ── Resolve per-clinic Razorpay credentials ──
        key_id, key_secret, _ = get_razorpay_creds(clinic or {})

        # ── Determine fee based on booking type ──
        if booking_type == "lab_test":
            amount_paise = await self._get_lab_test_fee_paise(clinic_id, lab_test_id)
            if not amount_paise or amount_paise <= 0:
                return {"success": False, "reason": "lab_test_price_unavailable"}
        else:
            amount_paise = await self._get_doctor_fee_paise(clinic_id, doctor_name)
        if deposit_percent < 100:
            amount_paise = round(amount_paise * deposit_percent / 100)

        hold_expires_at = (
            datetime.now(timezone.utc)
            + timedelta(minutes=settings.booking_hold_minutes)
        ).isoformat()

        # Resolve doctor_id if missing to guarantee index compatibility with migration 060
        if not doctor_id and doctor_name and booking_type != "lab_test":
            try:
                from app.database import get_doctor_by_name
                doc_res = get_doctor_by_name(clinic_id, doctor_name)
                if hasattr(doc_res, "__await__"):
                    doc_res = await doc_res
                if doc_res and isinstance(doc_res, dict) and doc_res.get("id"):
                    doctor_id = doc_res["id"]
            except Exception as doc_err:
                logger.warning(f"Could not resolve doctor_id for {doctor_name}: {doc_err}")

        # A consultation with no resolvable doctor_id must NOT be written.
        # Migration 064 keys uq_appointment_active_slot on doctor_id directly
        # (branch_id removed, COALESCE sentinel removed), so a NULL doctor_id
        # is an unguarded slot that any number of patients could book.
        # Mirrors the identical guard in app/database.py:book_appointment()
        # so both writers into `appointments` behave the same (KA-P0-01).
        if booking_type != "lab_test" and not doctor_id:
            logger.error(
                f"Refusing consultation booking with unresolved doctor: "
                f"clinic={clinic_id} doctor_name={doctor_name!r} — "
                f"the slot uniqueness index cannot guard a NULL doctor_id"
            )
            return {"success": False, "reason": "doctor_unavailable"}

        # Generate booking ref
        from app.utils.helpers import (
            generate_booking_reference,
            is_booking_ref_conflict,
            is_slot_conflict,
        )

        booking_ref = generate_booking_reference(
            clinic.get("booking_ref_prefix") if isinstance(clinic, dict) else None
        )

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
        if doctor_id:
            booking_data["doctor_id"] = doctor_id
        if booking_type == "lab_test":
            booking_data["lab_test_id"] = lab_test_id
            booking_data["lab_test_name"] = lab_test_name

        # Include branch info when booking at a specific branch
        if branch_id:
            booking_data["branch_id"] = branch_id
            booking_data["branch_name"] = branch_name or ""

        booking = None
        for attempt in range(3):
            booking_ref = generate_booking_reference(
                clinic.get("booking_ref_prefix") if isinstance(clinic, dict) else None
            )
            booking_data["booking_ref"] = booking_ref
            try:
                # unscoped: insert_scoped_by_payload
                result = await sb(supabase.table("appointments").insert(booking_data))
                if result.data:
                    booking = result.data[0]
                    booking_id = booking["id"]
                    break
            except Exception as e:
                if is_booking_ref_conflict(e) and attempt < 2:
                    logger.warning(
                        f"booking_ref collision (attempt {attempt + 1}/3), regenerating"
                    )
                    continue
                if is_slot_conflict(e):
                    logger.info(
                        f"Slot taken (DB constraint): {doctor_name} {appointment_date} {appointment_time}"
                    )
                    return {"success": False, "reason": "slot_taken"}
                logger.error(f"Booking insert failed: {e}")
                return {"success": False, "reason": "error"}

        if not booking:
            logger.error(
                "booking_ref generation exhausted 3 attempts or insert failed"
            )
            return {"success": False, "reason": "insert_failed"}

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

            # unscoped: unique_row_key
            await sb(supabase.table("appointments").update(
                {"razorpay_payment_link_id": payment_link_id}
            ).eq("id", booking_id))

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
                # unscoped: unique_row_key
                await sb(supabase.table("appointments").update({"status": "cancelled"}).eq(
                    "id", booking_id
                ))
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
        clinic_id: Optional[str] = None,
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
            clinic_id: Clinic ID from the webhook endpoint path (authoritative).

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
                    "clinic_id": clinic_id,
                },
            )
            logger.warning(
                "⚠️ Razorpay webhook SIGNATURE FAILED — possible spoofing attempt"
            )
            should_alert = True
            if alert_limiter is not None:
                key = alert_key or "global"
                # T5.1: synchronous limiter, hits the rate_limits table.
                should_alert = not await asyncio.to_thread(
                    alert_limiter.check_and_record, key
                )
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

        # KA-04: Extract Razorpay's own event ID for ledger idempotency.
        # This is distinct from payment_id — it identifies the webhook delivery itself.
        rz_event_id = payload.get("event_id") or payload.get("id")

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

        # ── Step 4: Idempotency check (scoped) ──
        use_clinic_scope = clinic_id if (clinic_id and str(clinic_id).strip().lower() not in ("default", "none", "null", "")) else None

        # KA-P2-08: when the webhook arrives on the unsuffixed endpoint
        # (POST /webhooks/razorpay, no {clinic_id}), use_clinic_scope is None
        # and every booking lookup below runs WITHOUT a clinic predicate —
        # the exact scoping that KRIYA-005 added to the per-clinic route.
        # Because get_razorpay_creds() falls back to the global secret when a
        # clinic has none configured, any clinic sharing that fallback could
        # reach another tenant's booking via notes.booking_id or a guessed
        # booking_ref.
        #
        # Mirrors resolve_tenant()'s "refuse to guess the tenant" rule
        # (app/services/tenant.py:186-192). Single-tenant deployments are
        # unaffected: the guard only fires when >1 active clinic exists.
        if use_clinic_scope is None:
            try:
                active = (
                    await sb(supabase.table("clinics")
                    .select("id")
                    .eq("is_active", True)
                    .neq("status", "DELETED")
                    .limit(2))
                )
                if active.data and len(active.data) > 1:
                    logger.error(
                        "UNSCOPED_WEBHOOK_REFUSED: payment webhook arrived on the "
                        "unsuffixed /webhooks/razorpay endpoint in a multi-tenant "
                        "deployment. Refusing to match a booking without a clinic "
                        "predicate. Configure the per-clinic endpoint "
                        "/webhooks/razorpay/{clinic_id} in the Razorpay dashboard."
                    )
                    await self._alert_admin(
                        "Payment webhook refused: it was delivered to the shared "
                        "/webhooks/razorpay endpoint, but this deployment serves "
                        "multiple clinics. Set the per-clinic webhook URL in "
                        "Razorpay. The payment was NOT applied to any booking."
                    )
                    return {
                        "status": "refused",
                        "code": 200,
                        "reason": "unscoped_webhook_multi_tenant",
                    }
            except Exception as scope_err:
                # Fail closed on the security check itself.
                logger.error(
                    f"UNSCOPED_WEBHOOK_CHECK_FAILED: could not determine tenant "
                    f"count: {scope_err} — refusing unscoped webhook"
                )
                return {
                    "status": "refused",
                    "code": 200,
                    "reason": "unscoped_webhook_check_failed",
                }

        try:
            idemp_query = (
                # unscoped: meta_callback_by_unique_id
                supabase.table("appointments")
                .select("id")
                .eq("payment_id", payment_id)
                .eq("status", "confirmed")
            )
            if use_clinic_scope:
                idemp_query = idemp_query.eq("clinic_id", use_clinic_scope)
            existing_confirmed = await sb(idemp_query)
        except Exception as idemp_err:
            logger.warning(f"Scoped idempotency check failed ({idemp_err}) — retrying global check")
            existing_confirmed = (
                # unscoped: meta_callback_by_unique_id
                await sb(supabase.table("appointments")
                .select("id")
                .eq("payment_id", payment_id)
                .eq("status", "confirmed"))
            )

        if existing_confirmed.data:
            logger.info(
                f"Razorpay webhook: payment_id {payment_id} already processed (idempotent)"
            )
            return {"status": "ok", "code": 200, "reason": "already_processed"}

        # ── Step 5: Look up booking (CLINIC SCOPED) ──
        # Match on payment_link_id first, fallback to notes.booking_id and booking_ref
        # Every candidate query is scoped to use_clinic_scope when available.
        # Never fall back to an unscoped global query (KRIYA-005).
        booking_result = None

        def _build_booking_query():
            # unscoped: meta_callback_by_unique_id
            q = supabase.table("appointments").select("*")
            if use_clinic_scope:
                q = q.eq("clinic_id", use_clinic_scope)
            return q

        if payment_link_id:
            try:
                booking_result = (
                    await sb(_build_booking_query()
                    .eq("razorpay_payment_link_id", payment_link_id))
                )
            except Exception as e:
                logger.warning(f"Lookup by payment_link_id failed: {e}")

        if not booking_result or not booking_result.data:
            booking_id_from_notes = notes.get("booking_id")
            if booking_id_from_notes:
                try:
                    booking_result = (
                        await sb(_build_booking_query()
                        .eq("id", booking_id_from_notes))
                    )
                except Exception as e:
                    logger.warning(f"Lookup by booking_id failed: {e}")

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
                try:
                    booking_result = (
                        await sb(_build_booking_query()
                        .eq("booking_ref", booking_ref))
                    )
                except Exception as e:
                    logger.warning(f"Lookup by booking_ref failed: {e}")

        if not booking_result or not booking_result.data:
            # Do NOT widen the search (Rule 5). A tenant holding its own valid
            # webhook secret could previously confirm the booking of ANOTHER
            # tenant by supplying notes.booking_id or a guessable booking_ref
            # (KRIYA-005, amplified by the pre-T0.2 9,000-value reference space).
            #
            # Every candidate lookup must carry the clinic_id resolved from THIS
            # webhook's own routing / secret.
            logger.error(
                f"UNMATCHED_PAYMENT clinic_id={clinic_id} payment_id={payment_id} "
                f"link_id={payment_link_id} — no clinic-scoped booking found"
            )
            self._log_payment_event_raw(
                None,
                "webhook_received",
                {
                    "clinic_id": clinic_id,
                    "payment_id": payment_id,
                    "payment_link_id": payment_link_id,
                    "error": "no_booking_found_in_clinic",
                    "raw": payload,
                },
                clinic_id=use_clinic_scope,
                provider_event_id=rz_event_id,
            )
            try:
                from connectors.runner import send_admin_alert

                await send_admin_alert(
                    clinic_id,
                    f"Payment received with no matching booking\n\n"
                    f"Payment ID: {payment_id}\n"
                    f"Requires manual reconciliation.",
                )
            except Exception as alert_err:
                logger.error(f"Failed to alert on unmatched payment: {alert_err}")

            # Return 200. Razorpay retries non-2xx, and retrying will never make
            # an unmatched payment match. The alert is the recovery path.
            return {"status": "unmatched", "code": 200, "reason": "booking_not_found"}

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
            clinic_id=use_clinic_scope,
            provider_event_id=rz_event_id,
        )

        self._log_payment_event(
            booking_id,
            "signature_verified",
            {
                "payment_id": payment_id,
            },
            clinic_id=use_clinic_scope,
            provider_event_id=rz_event_id,
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
                clinic_id=use_clinic_scope,
                provider_event_id=rz_event_id,
            )
            # Route to pending_review — NEVER auto-confirm on mismatch.
            #
            # KA-P2-07: this was the one status write in this file without a
            # CAS predicate. The step-4 idempotency check only suppresses a
            # REPEAT of the same payment_id, so a second, different payment
            # against an already-confirmed booking reached here and demoted a
            # confirmed appointment to pending_review while overwriting
            # payment_id — orphaning the payment that actually confirmed it.
            mismatch_from_status = booking.get("status")
            protected_states = ("confirmed", "completed", "refunded", "cancelled")
            if mismatch_from_status in protected_states:
                logger.error(
                    f"MISMATCH_ON_SETTLED_BOOKING booking={booking_id} "
                    f"status={mismatch_from_status} payment_id={payment_id} "
                    f"expected={expected_amount} received={amount_paid} — refusing "
                    f"to overwrite a settled booking; manual reconciliation required"
                )
                await self._alert_admin(
                    f"Payment AMOUNT MISMATCH on an already-{mismatch_from_status} booking "
                    f"{booking.get('booking_ref', booking_id)}\n"
                    f"Expected: Rs.{expected_amount / 100:.2f}\n"
                    f"Received: Rs.{amount_paid / 100:.2f}\n"
                    f"Payment ID: {payment_id}\n"
                    f"The booking was NOT modified. This payment is unmatched and "
                    f"likely needs a refund."
                )
                return {"status": "ok", "code": 200, "reason": "mismatch_on_settled_booking"}

            mismatch_update = (
                # unscoped: unique_row_key
                await sb(supabase.table("appointments")
                .update(
                    {
                        "status": "pending_review",
                        "payment_id": payment_id,
                    }
                )
                .eq("id", booking_id)
                .eq("status", mismatch_from_status))
            )
            if not mismatch_update.data:
                # A concurrent process moved this booking between our read and
                # this write. Do not retry — a human must look at it.
                logger.error(
                    f"MISMATCH_CAS_LOST booking={booking_id} "
                    f"expected_status={mismatch_from_status} payment_id={payment_id} "
                    f"— booking changed concurrently, not modified"
                )
                await self._alert_admin(
                    f"Payment amount mismatch could not be recorded for booking "
                    f"{booking.get('booking_ref', booking_id)} — its status changed "
                    f"concurrently. Payment ID: {payment_id}. Needs manual review."
                )
                return {"status": "ok", "code": 200, "reason": "mismatch_cas_lost"}

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

        if current_status == "expired":
            logger.warning(
                f"LATE_PAYMENT booking={booking_id} ref={booking.get('booking_ref')} "
                f"payment_id={payment_id} — hold already expired, auto-refunding"
            )
            self._log_payment_event(
                booking_id,
                "late_payment_after_expiry",
                {"payment_id": payment_id, "amount_paise": amount_paid},
                clinic_id=use_clinic_scope,
                provider_event_id=rz_event_id,
            )
            from app.services.tenant import get_clinic_by_id
            clinic = None
            try:
                clinic = await get_clinic_by_id(booking.get("clinic_id") or "default")
            except Exception as e:
                logger.warning(f"Could not load clinic for late payment refund: {e}")

            refund = await self._refund_payment_id(
                payment_id=payment_id,
                amount_paise=amount_paid,
                booking_id=booking_id,
                reason="Payment received after slot hold expired",
                clinic=clinic,
            )
            # unscoped: unique_row_key
            await sb(supabase.table("appointments").update({
                "status": "refunded",
                "refund_reason": "late_payment",
                "payment_id": payment_id,
                "refund_id": refund.get("refund_id"),
                "refunded_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", booking_id).eq("status", "expired"))

            await self._notify_late_payment_refunded(booking, refund)
            await self._alert_admin(
                clinic,
                f"Late payment auto-refunded: {booking.get('booking_ref')} "
                f"({amount_paid / 100:.0f} INR). Slot was already released.",
            )
            return {
                "status": "ok",
                "code": 200,
                "action": "late_payment_refunded",
                "reason": "expired_hold_refunded",
            }

        if current_status != "pending_payment":
            # KA-06: NEVER overwrite terminal states with pending_review.
            # Terminal states (completed, refunded, cancelled) represent
            # concluded business transactions — clobbering them is data loss.
            terminal_states = ("completed", "refunded", "cancelled")
            if current_status in terminal_states:
                logger.warning(
                    f"TERMINAL_STATE_GUARD booking={booking_id} status={current_status} "
                    f"payment_id={payment_id} — refusing to overwrite terminal state"
                )
                self._log_payment_event(
                    booking_id,
                    "terminal_state_blocked",
                    {
                        "current_status": current_status,
                        "payment_id": payment_id,
                        "action": "blocked_overwrite",
                    },
                    clinic_id=use_clinic_scope,
                    provider_event_id=rz_event_id,
                )
                return {"status": "ok", "code": 200, "reason": f"terminal_state_{current_status}"}

            # Non-terminal, non-pending_payment state — route to review
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
                clinic_id=use_clinic_scope,
                provider_event_id=rz_event_id,
            )
            # Use CAS: only update if status hasn't changed since we read it
            # unscoped: unique_row_key
            await sb(supabase.table("appointments").update(
                {
                    "status": "pending_review",
                    "payment_id": payment_id,
                }
            ).eq("id", booking_id).eq("status", current_status))
            return {"status": "ok", "code": 200, "reason": "unexpected_state"}

        # ── Step 8: CONFIRM the booking (Atomic Update) ──
        update_result = (
            # unscoped: unique_row_key
            await sb(supabase.table("appointments")
            .update(
                {
                    "status": "confirmed",
                    "payment_id": payment_id,
                }
            )
            .eq("id", booking_id)
            .eq("status", "pending_payment"))
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
            clinic_id=use_clinic_scope,
            provider_event_id=rz_event_id,
        )

        logger.info(f"✅ Booking {booking_id} CONFIRMED via payment {payment_id}")

        # Update in-memory booking dictionary before notifications
        booking["status"] = "confirmed"
        booking["payment_id"] = payment_id

        # ── Step 9: Notify patient + admin ──
        try:
            await self._increment_patient_visit_count(
                booking.get("clinic_id"), booking.get("patient_phone")
            )
        except Exception as visit_err:
            logger.error(f"Failed to increment visit count for booking {booking_id}: {visit_err}")

        try:
            await self._notify_payment_confirmed(booking)
        except Exception as notif_err:
            logger.error(f"Failed to notify payment confirmed for booking {booking_id}: {notif_err}")

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
            # unscoped: platform_sweep
            await sb(supabase.table("appointments")
            .select("id, clinic_id, booking_ref, patient_phone, hold_expires_at, razorpay_payment_link_id, amount_paise, doctor_name, department, appointment_date, appointment_time")
            .eq("status", "pending_payment")
            .lt("hold_expires_at", now)
            .limit(200))
        )

        if not stale.data:
            return 0

        count = 0
        for booking in stale.data:
            booking_id = booking["id"]
            payment_link_id = booking.get("razorpay_payment_link_id") or booking.get("payment_link_id")

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
                if payment_link_id:
                    link_status = await self._check_payment_link_status(
                        payment_link_id, key_id=key_id, key_secret=key_secret
                    )

                    if link_status["status"] == "paid":
                        # Webhook was missed — recover by confirming
                        logger.info(
                            f"Recovery: booking {booking_id} was paid on Razorpay but webhook missed. Confirming."
                        )

                        payment_id = link_status["payment_id"] or f"recovery_{payment_link_id}"

                        recovery_update = (
                            # unscoped: unique_row_key
                            await sb(supabase.table("appointments")
                            .update(
                                {
                                    "status": "confirmed",
                                    "payment_id": payment_id,
                                }
                            )
                            .eq("id", booking_id)
                            .eq("status", "pending_payment"))
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

                        booking["status"] = "confirmed"
                        booking["payment_id"] = payment_id

                        try:
                            await self._increment_patient_visit_count(
                                booking.get("clinic_id"), booking.get("patient_phone")
                            )
                        except Exception as visit_err:
                            logger.error(f"Failed to increment visit count in recovery for {booking_id}: {visit_err}")

                        try:
                            await self._notify_payment_confirmed(booking)
                        except Exception as notif_err:
                            logger.error(f"Failed to notify payment confirmed in recovery for {booking_id}: {notif_err}")

                        count += 1
                        continue

                    if link_status["status"] == "unknown":
                        # P1-7: If link status cannot be verified due to Razorpay API error/timeout,
                        # do not expire the booking. Allow next run to retry to prevent false expiration.
                        logger.warning(
                            f"Razorpay status check for booking {booking_id} returned unknown/error. "
                            f"Skipping expiry to prevent false expiration of potentially paid booking."
                        )
                        continue

                # ── Normal expiry path ──
                # unscoped: unique_row_key
                await sb(supabase.table("appointments").update({"status": "expired"}).eq(
                    "id", booking_id
                ).eq("status", "pending_payment"))

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
    # 4b. FAST-POLL RECENTLY-CREATED PENDING PAYMENTS
    # ─────────────────────────────────────────────────────────────────────

    async def poll_recent_pending_payments(self) -> int:
        """Fast-poll Razorpay for recently-created pending_payment bookings.

        Checks bookings created within the last 5 minutes that have not yet
        been confirmed via webhook. If Razorpay shows 'paid', confirms the
        booking immediately and sends the patient WhatsApp confirmation.

        This ensures near-instant confirmation (~30-60 seconds) even when
        the webhook is delayed, rate-limited, or missed entirely.

        Constraints:
          - Only queries bookings from the last 5 minutes → minimal API load.
          - Atomic CAS update prevents duplicate confirmations.
          - Max 20 bookings per cycle to bound Razorpay API calls.

        Returns: number of bookings confirmed via polling.
        """
        five_min_ago = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()

        try:
            recent_pending = (
                # unscoped: platform_sweep
                await sb(supabase.table("appointments")
                .select(
                    "id, clinic_id, booking_ref, patient_phone, "
                    "razorpay_payment_link_id, amount_paise, doctor_name, "
                    "department, appointment_date, appointment_time, "
                    "patient_name, branch_id"
                )
                .eq("status", "pending_payment")
                .gte("created_at", five_min_ago)
                .order("created_at", desc=True)
                .limit(20))
            )
        except Exception as e:
            logger.error(f"poll_recent_pending_payments: query failed: {e}")
            return 0

        if not recent_pending.data:
            return 0

        confirmed = 0
        for booking in recent_pending.data:
            payment_link_id = booking.get("razorpay_payment_link_id")
            if not payment_link_id:
                continue

            booking_id = booking["id"]

            try:
                # Resolve per-clinic Razorpay creds
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
                            f"poll_recent: could not fetch clinic {clinic_id_for_booking}: {ce}"
                        )

                key_id, key_secret, _ = get_razorpay_creds(clinic_for_booking)

                link_status = await self._check_payment_link_status(
                    payment_link_id, key_id=key_id, key_secret=key_secret
                )

                if link_status["status"] != "paid":
                    continue

                # ── Confirm immediately via atomic CAS update ──
                payment_id = (
                    link_status["payment_id"]
                    or f"poll_{payment_link_id}"
                )

                update_result = (
                    # unscoped: unique_row_key
                    await sb(supabase.table("appointments")
                    .update(
                        {
                            "status": "confirmed",
                            "payment_id": payment_id,
                        }
                    )
                    .eq("id", booking_id)
                    .eq("status", "pending_payment"))
                )

                if not update_result.data:
                    # Another process (webhook / expire recovery) already confirmed
                    logger.info(
                        f"poll_recent: booking {booking_id} already confirmed "
                        f"by concurrent process (idempotent)"
                    )
                    continue

                # Update in-memory copy for notification
                booking["status"] = "confirmed"
                booking["payment_id"] = payment_id

                logger.info(
                    f"⚡ FAST_POLL_CONFIRMED booking={booking_id} "
                    f"ref={booking.get('booking_ref')} via payment_link polling"
                )

                self._log_payment_event(
                    booking_id,
                    "confirmed",
                    {
                        "payment_id": payment_id,
                        "amount_paise": booking.get("amount_paise"),
                        "confirmation_source": "fast_poll",
                    },
                    clinic_id=clinic_id_for_booking,
                )

                # Increment visit count
                try:
                    await self._increment_patient_visit_count(
                        clinic_id_for_booking, booking.get("patient_phone")
                    )
                except Exception as visit_err:
                    logger.error(
                        f"poll_recent: visit count increment failed for {booking_id}: {visit_err}"
                    )

                # Send patient & admin notifications immediately
                try:
                    await self._notify_payment_confirmed(booking)
                except Exception as notif_err:
                    logger.error(
                        f"poll_recent: notification failed for {booking_id}: {notif_err}"
                    )

                confirmed += 1

            except Exception as e:
                logger.error(
                    f"poll_recent: error processing booking {booking_id}: {e}"
                )

        if confirmed > 0:
            logger.info(
                f"poll_recent_pending_payments: confirmed {confirmed} booking(s) via fast polling"
            )

        return confirmed

    # ─────────────────────────────────────────────────────────────────────
    # 5. REFUNDS
    # ─────────────────────────────────────────────────────────────────────

    async def initiate_refund(
        self,
        booking_id: str,
        reason: str = "",
        clinic: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Initiate a refund for a confirmed booking.

        Checks refund eligibility (4+ hours before slot), calls Razorpay
        Refund API with a deterministic idempotency key, and logs all transitions.

        Args:
            clinic: Optional clinic dict. Used to resolve per-clinic Razorpay
                    credentials. Falls back to global settings if None.
            idempotency_key: Optional explicit idempotency key. If omitted,
                             a canonical key derived from booking_id and payment_id
                             is used to ensure retries do not trigger duplicate refunds.

        Returns:
            {"success": bool, "refund_id": str, "amount_inr": float,
             "is_late": bool, "reason": str}

            `is_late` is True ONLY when the refund was refused because the
            cancellation cutoff had already passed. Callers use it to tell a
            "you cancelled too late" message apart from a gateway failure —
            those need very different words to the patient.
        """
        from app.services.tenant import cancellation_window_hours

        key_id, key_secret, _ = get_razorpay_creds(clinic or {})
        # ── Look up booking ──
        booking_result = (
            # unscoped: unique_row_key
            await sb(supabase.table("appointments").select("*").eq("id", booking_id))
        )

        if not booking_result.data:
            return {
                "success": False,
                "reason": "booking_not_found",
                "refund_id": "",
                "amount_inr": 0.0,
                "is_late": False,
            }

        booking = booking_result.data[0]
        amount_inr = round((booking.get("amount_paise") or 0) / 100, 2)

        if booking["status"] not in ("confirmed", "pending_review"):
            return {
                "success": False,
                "reason": f"cannot_refund_status_{booking['status']}",
                "refund_id": "",
                "amount_inr": amount_inr,
                "is_late": False,
            }

        if not booking.get("payment_id"):
            return {
                "success": False,
                "reason": "no_payment_to_refund",
                "refund_id": "",
                "amount_inr": amount_inr,
                "is_late": False,
            }

        # ── Refund eligibility check ──
        slot_datetime = self._parse_slot_datetime(
            booking["appointment_date"], booking["appointment_time"]
        )
        # The cutoff is per-clinic (clinics.config.cancellation_window_hours),
        # falling back to the platform default. Resolved through the same
        # helper the booking confirmation quotes to the patient, so what they
        # were promised and what is enforced here cannot drift apart.
        window_hours = cancellation_window_hours(clinic)
        if slot_datetime:
            hours_until_slot = (
                slot_datetime - datetime.now(timezone.utc)
            ).total_seconds() / 3600
            if hours_until_slot < window_hours:
                return {
                    "success": False,
                    "reason": f"refund_window_closed_need_{window_hours}h_before_slot",
                    "refund_id": "",
                    "amount_inr": amount_inr,
                    "is_late": True,
                    "window_hours": window_hours,
                }

        # ── Deterministic idempotency key (stable across all retries) ──
        # Canonical identity: ref_<booking_id>_<payment_id>
        # Guarantees that any retry of the same business refund passes the exact same
        # key to Razorpay, preventing duplicate refund charges at the payment gateway.
        payment_id = booking.get("payment_id", "")
        effective_idempotency_key = (
            idempotency_key or f"ref_{booking_id}_{payment_id}"
        )

        # ── Log refund_initiated IMMEDIATELY (before gateway call) ──
        self._log_payment_event(
            booking_id,
            "refund_initiated",
            {
                "payment_id": booking["payment_id"],
                "amount_paise": booking["amount_paise"],
                "reason": reason,
                "idempotency_key": effective_idempotency_key,
            },
        )

        # ── Call Razorpay Refund API ──
        try:
            refund_result = await self._create_razorpay_refund(
                payment_id=booking["payment_id"],
                amount_paise=booking["amount_paise"],
                reason=reason,
                idempotency_key=effective_idempotency_key,
                key_id=key_id,
                key_secret=key_secret,
            )

            refund_id = refund_result.get("id", "")

            # Update booking status
            # unscoped: unique_row_key
            await sb(supabase.table("appointments").update({
                "status": "refunded",
                "refund_id": refund_id,
                "refund_reason": reason,
                "refunded_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", booking_id))

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
            return {
                "success": True,
                "refund_id": refund_id,
                "status": "completed",
                "amount_inr": amount_inr,
                "is_late": False,
                "reason": "",
                "window_hours": window_hours,
            }

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
            return {
                "success": False,
                "reason": f"razorpay_error: {str(e)[:200]}",
                "refund_id": "",
                "amount_inr": amount_inr,
                "is_late": False,
                "window_hours": window_hours,
            }

    async def _refund_payment_id(
        self,
        payment_id: str,
        amount_paise: int,
        booking_id: str,
        reason: str,
        clinic: Optional[dict] = None,
    ) -> dict:
        """Issue an immediate refund by Razorpay payment_id directly.

        Useful for late payments where internal booking is expired or cancelled,
        bypassing booking status checks.
        """
        key_id, key_secret, _ = get_razorpay_creds(clinic or {})
        effective_key_id = key_id or settings.razorpay_key_id
        effective_key_secret = key_secret or settings.razorpay_key_secret
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._razorpay_base}/payments/{payment_id}/refund",
                    json={"amount": amount_paise, "notes": {"reason": reason[:255]}},
                    headers={"X-Razorpay-Idempotency-Key": f"late-{payment_id}"},
                    auth=(effective_key_id, effective_key_secret),
                    timeout=15.0,
                )
                resp.raise_for_status()
                data = resp.json()
            self._log_payment_event(
                booking_id,
                "auto_refund_issued",
                {"payment_id": payment_id, "refund_id": data.get("id"), "reason": reason},
            )
            return {"success": True, "refund_id": data.get("id")}
        except Exception as e:
            logger.error(f"AUTO_REFUND_FAILED payment={payment_id}: {e}")
            self._log_payment_event(
                booking_id,
                "auto_refund_failed",
                {"payment_id": payment_id, "error": str(e)[:500], "reason": reason},
            )
            return {"success": False, "error": str(e)}

    # ─────────────────────────────────────────────────────────────────────
    # 6. MANUAL ADMIN ACTIONS
    # ─────────────────────────────────────────────────────────────────────

    async def admin_confirm_booking(
        self, booking_id: str, clinic_id: str = "", admin_notes: str = ""
    ) -> dict:
        """Manually confirm a pending_review booking (admin override), scoped to clinic_id."""
        if not is_valid_clinic_scope(clinic_id):
            # Warning-and-continue meant an unscoped booking id resolved
            # against every tenant's appointments. A booking id alone is
            # not authorization to confirm, reject or refund.
            raise ValueError("clinic_id is required for admin_confirm_booking")
        query = (
            supabase.table("appointments")
            .select("*")
            .eq("id", booking_id)
            .eq("clinic_id", clinic_id)
        )
        booking_result = await sb(query)

        if not booking_result.data:
            return {"success": False, "reason": "booking_not_found"}

        booking = booking_result.data[0]

        if booking["status"] != "pending_review":
            return {
                "success": False,
                "reason": f"can_only_confirm_pending_review_not_{booking['status']}",
            }

        # unscoped: unique_row_key
        await sb(supabase.table("appointments").update({"status": "confirmed"}).eq(
            "id", booking_id
        ))

        self._log_payment_event(
            booking_id,
            "manual_confirm",
            {
                "admin_notes": admin_notes,
                "previous_status": booking["status"],
            },
        )

        logger.info(f"Admin manually confirmed booking {booking_id}")
        booking["status"] = "confirmed"

        try:
            await self._increment_patient_visit_count(
                booking.get("clinic_id"), booking.get("patient_phone")
            )
        except Exception as visit_err:
            logger.error(f"Failed to increment visit count in manual confirm for {booking_id}: {visit_err}")

        try:
            await self._notify_payment_confirmed(booking)
        except Exception as notif_err:
            logger.error(f"Failed to notify payment confirmed in manual confirm for {booking_id}: {notif_err}")

        return {"success": True}

    async def admin_reject_booking(
        self, booking_id: str, clinic_id: str = "", admin_notes: str = ""
    ) -> dict:
        """Manually reject a pending_review booking + initiate refund, scoped to clinic_id.

        initiate_refund() checks status in ('confirmed', 'pending_review'), so refunding
        BEFORE cancelling ensures the refund is processed rather than blocked.
        """
        if not is_valid_clinic_scope(clinic_id):
            # Warning-and-continue meant an unscoped booking id resolved
            # against every tenant's appointments. A booking id alone is
            # not authorization to confirm, reject or refund.
            raise ValueError("clinic_id is required for admin_reject_booking")
        query = (
            supabase.table("appointments")
            .select("*")
            .eq("id", booking_id)
            .eq("clinic_id", clinic_id)
        )
        booking_result = await sb(query)

        if not booking_result.data:
            return {"success": False, "reason": "booking_not_found"}

        booking = booking_result.data[0]

        if booking["status"] not in ("pending_review", "confirmed"):
            return {
                "success": False,
                "reason": f"can_only_reject_pending_review_not_{booking['status']}",
            }

        resolved_clinic_id = booking.get("clinic_id") or clinic_id or "default"
        from app.services.tenant import get_clinic_by_id
        clinic = None
        try:
            clinic = await get_clinic_by_id(resolved_clinic_id)
        except Exception as e:
            logger.warning(f"Could not load clinic {resolved_clinic_id} for reject: {e}")

        # ── Step 1: refund while the row is still refundable ──
        refund_result = {"success": True, "reason": "no_payment_to_refund"}
        if booking.get("payment_id"):
            refund_result = await self.initiate_refund(
                booking_id,
                reason=f"Admin rejected: {admin_notes}"[:255],
                clinic=clinic,
            )
            if not refund_result.get("success"):
                logger.error(
                    f"REFUND_FAILED_ON_REJECT booking={booking_id} "
                    f"reason={refund_result.get('reason')}"
                )
                self._log_payment_event(
                    booking_id, "reject_aborted_refund_failed", refund_result
                )
                await self._alert_admin(
                    clinic,
                    f"Reject aborted for {booking.get('booking_ref')}: refund failed "
                    f"({refund_result.get('reason')}). Booking left in current status.",
                )
                return {
                    "success": False,
                    "reason": "refund_failed",
                    "detail": refund_result,
                }

        # ── Step 2: only now cancel ──
        update_data = {
            "status": "cancelled",
            "admin_notes": admin_notes,
        }
        if refund_result.get("refund_id"):
            update_data["refund_id"] = refund_result.get("refund_id")

        if not is_valid_clinic_scope(clinic_id):
            raise ValueError("clinic_id is required to cancel/refund a booking")
        update_query = (
            supabase.table("appointments")
            .update(update_data)
            .eq("id", booking_id)
            .eq("clinic_id", clinic_id)
        )
        await sb(update_query)

        self._log_payment_event(
            booking_id,
            "manual_reject",
            {
                "admin_notes": admin_notes,
                "refund": refund_result,
            },
        )
        await self._notify_booking_cancelled(booking, refunded=bool(booking.get("payment_id")))
        return {"success": True, "refund": refund_result}

    async def admin_cancel_confirmed_booking(
        self, booking_id: str, clinic_id: str = "", admin_notes: str = ""
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
        if not is_valid_clinic_scope(clinic_id):
            # Warning-and-continue meant an unscoped booking id resolved
            # against every tenant's appointments. A booking id alone is
            # not authorization to confirm, reject or refund.
            raise ValueError("clinic_id is required for admin_cancel_confirmed_booking")
        query = (
            supabase.table("appointments")
            .select("*")
            .eq("id", booking_id)
            .eq("clinic_id", clinic_id)
        )
        booking_result = await sb(query)

        if not booking_result.data:
            return {"success": False, "reason": "booking_not_found"}

        booking = booking_result.data[0]

        if booking["status"] != "confirmed":
            return {
                "success": False,
                "reason": f"can_only_cancel_confirmed_not_{booking['status']}",
            }

        clinic = None
        refund_result = None

        if booking.get("payment_id"):
            from app.services.tenant import get_clinic_by_id

            clinic = await get_clinic_by_id(booking.get("clinic_id") or "default")
            refund_result = await self.initiate_refund(
                booking_id, reason=admin_notes or "Cancelled by admin", clinic=clinic
            )
            if not refund_result["success"]:
                # A refund that cannot be issued is not a reason to refuse the
                # cancellation. Returning early here left the admin unable to
                # cancel a paid booking at all once its refund window had
                # closed — the slot stayed blocked and the patient was never
                # told. Cancel it, then tell the patient the truth about the
                # money: too late for a refund, or refund needs a human.
                # unscoped: unique_row_key
                await sb(supabase.table("appointments").update(
                    {"status": "cancelled"}
                ).eq("id", booking_id))
                self._log_payment_event(
                    booking_id,
                    "admin_cancel_without_refund",
                    {"admin_notes": admin_notes, "refund_reason": refund_result.get("reason")},
                )
                await self.notify_cancellation_outcome(booking, refund_result, clinic=clinic)
                return {
                    "success": True,
                    "cancelled": True,
                    "refunded": False,
                    "refund": refund_result,
                    "reason": refund_result.get("reason", ""),
                }
        else:
            # unscoped: unique_row_key
            await sb(supabase.table("appointments").update({"status": "cancelled"}).eq(
                "id", booking_id
            ))
            self._log_payment_event(
                booking_id, "admin_cancel", {"admin_notes": admin_notes}
            )

        if refund_result and refund_result.get("success"):
            # Itemised refund receipt (amount + Razorpay reference) instead of
            # the generic "a refund has been initiated" line.
            await self.notify_cancellation_outcome(booking, refund_result, clinic=clinic)
        else:
            await self._notify_booking_cancelled(booking, refunded=False)

        return {
            "success": True,
            "cancelled": True,
            "refunded": bool(refund_result and refund_result.get("success")),
            "refund": refund_result,
        }

    # ─────────────────────────────────────────────────────────────────────
    # 7. RECONCILIATION
    # ─────────────────────────────────────────────────────────────────────

    async def get_daily_reconciliation(
        self, date_str: Optional[str] = None, clinic_id: Optional[str] = None
    ) -> dict:
        """Compare confirmed bookings against Razorpay for a given date.

        When clinic_id is provided, scopes the reconciliation summary strictly to that clinic.
        """
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Get confirmed bookings for the date
        query = (
            # unscoped: platform_sweep
            supabase.table("appointments")
            .select("id, amount_paise, payment_id, booking_ref, patient_phone")
            .eq("status", "confirmed")
            .eq("appointment_date", date_str)
            .not_.is_("payment_id", "null")
        )
        if clinic_id:
            query = query.eq("clinic_id", clinic_id)
        bookings = await sb(query)

        total_bookings = len(bookings.data) if bookings.data else 0
        total_amount = sum(b.get("amount_paise", 0) for b in (bookings.data or []))

        # Get pending_review bookings
        pr_query = (
            # unscoped: platform_sweep
            supabase.table("appointments")
            .select("id")
            .eq("status", "pending_review")
            .eq("appointment_date", date_str)
        )
        if clinic_id:
            pr_query = pr_query.eq("clinic_id", clinic_id)
        pending_review = await sb(pr_query)

        return {
            "date": date_str,
            "clinic_id": clinic_id,
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
            # Doctor names are not unique across tenants: unscoped, this could
            # price a booking from another clinic's fee schedule.
            query = (
                supabase.table("doctors")
                .select("consultation_fee")
                .eq("clinic_id", clinic_id)
            )
            result = await sb(query.eq("name", doctor_name))

            if result.data and result.data[0].get("consultation_fee"):
                # consultation_fee is stored in rupees, convert to paise
                return int(result.data[0]["consultation_fee"]) * 100

        except Exception as e:
            logger.error(f"Error fetching doctor fee: {e}")

        return settings.booking_fee_paise

    async def _get_lab_test_fee_paise(self, clinic_id: str, lab_test_id: str) -> Optional[int]:
        """Get a lab test's price in paise directly from the catalog."""
        try:
            query = (
                supabase.table("lab_tests")
                .select("price_paise")
                .eq("clinic_id", clinic_id)
            )
            result = await sb(query.eq("id", lab_test_id))
            if result.data and result.data[0].get("price_paise"):
                return int(result.data[0]["price_paise"])
        except Exception as e:
            logger.error(f"Error fetching lab test fee: {e}")
            return None

        # No usable price. Unlike a consultation -- where booking_fee_paise is a
        # legitimate clinic-wide default -- a lab test has no sensible fallback
        # price, so charging the generic fee would bill the patient an amount
        # that appears nowhere in the catalog. Both intake paths (the API and
        # the CSV import) reject a price <= 0, so reaching here means the row
        # was deleted mid-booking or written outside those paths.
        logger.error(
            f"Lab test {lab_test_id} for clinic {clinic_id} has no usable "
            f"price_paise -- refusing to bill a fallback amount"
        )
        return None

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

        # Razorpay requires expire_by >= now + 15 min. Our internal hold is
        # settings.booking_hold_minutes, so use max(hold, 16 min).
        expire_by = int(time.time()) + max(settings.booking_hold_minutes * 60, 16 * 60)

        link_data = {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "expire_by": expire_by,
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

    async def _check_payment_link_status(
        self,
        payment_link_id: str,
        key_id: str = "",
        key_secret: str = "",
    ) -> dict:
        """Check a Razorpay Payment Link's status (for recovery/expiry path).

        Returns {"status": <razorpay status>, "payment_id": <str or "">}.
        Payment Links (not Orders) are what create_booking_with_payment
        actually creates, so this is the correct API for the hold-expiry
        recovery check — _check_razorpay_order_status/_get_razorpay_order_payments
        below operate on Orders and never match a real booking.
        """
        effective_key_id = key_id or settings.razorpay_key_id
        effective_key_secret = key_secret or settings.razorpay_key_secret
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self._razorpay_base}/payment_links/{payment_link_id}",
                    auth=(effective_key_id, effective_key_secret),
                    timeout=10.0,
                )
                response.raise_for_status()
                data = response.json()
                payments = data.get("payments", [])
                payment_id = payments[0].get("payment_id", "") if payments else ""
                return {"status": data.get("status", "unknown"), "payment_id": payment_id}
        except Exception as e:
            logger.error(f"Error checking Razorpay payment link status for {payment_link_id}: {e}")
            return {"status": "unknown", "payment_id": ""}

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
        self,
        booking_id: str,
        event_type: str,
        payload: dict,
        clinic_id: Optional[str] = None,
        provider_event_id: Optional[str] = None,
    ) -> None:
        """Log to payment_events audit table. NEVER skip this (T4.1)."""
        try:
            event_row = {
                "booking_id": booking_id,
                "event_type": event_type,
                "raw_payload": json.dumps(payload, default=str),
            }
            if clinic_id:
                event_row["clinic_id"] = clinic_id
            if provider_event_id:
                event_row["provider_event_id"] = provider_event_id
            # unscoped: insert_scoped_by_payload
            supabase.table("payment_events").insert(event_row).execute()
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

            patient = get_patient_by_phone(clinic_id, patient_phone)
            if hasattr(patient, "__await__"):
                patient = await patient
            if patient and isinstance(patient, dict):
                new_count = (patient.get("visit_count") or 0) + 1
                res = update_patient(
                    clinic_id, patient_phone, {"visit_count": new_count}
                )
                if hasattr(res, "__await__"):
                    await res
        except Exception as e:
            logger.error(f"Failed to increment patient visit_count: {e}")

    def _log_payment_event_raw(
        self,
        booking_id: Optional[str],
        event_type: str,
        payload: dict,
        clinic_id: Optional[str] = None,
        provider_event_id: Optional[str] = None,
    ) -> None:
        """Log payment event even when booking_id might be None (e.g. signature failures).

        Orphan events (no booking_id) go to webhook_security_events instead
        of payment_events, since payment_events.booking_id is a required FK —
        this keeps signature-failure/spoofing-attempt events queryable in the
        DB for forensic replay instead of only living in rotated app logs.
        """
        try:
            if booking_id:
                event_row = {
                    "booking_id": booking_id,
                    "event_type": event_type,
                    "raw_payload": json.dumps(payload, default=str),
                }
                if clinic_id:
                    event_row["clinic_id"] = clinic_id
                if provider_event_id:
                    event_row["provider_event_id"] = provider_event_id
                # unscoped: insert_scoped_by_payload
                supabase.table("payment_events").insert(event_row).execute()
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
                f"payload={json.dumps(payload, default=str)}"
            )

    async def _notify_payment_confirmed(self, booking: dict) -> None:
        """Send WhatsApp confirmation message to the patient and admin alerts after payment is verified."""
        clinic_id_val = booking.get("clinic_id") or "default"
        try:
            from app.services.whatsapp import whatsapp_service
            from app.services.tenant import (
                cancellation_window_hours,
                get_branch_by_id,
                get_clinic_by_id,
                get_clinic_contact,
            )
            from app.templates.whatsapp_templates import cancellation_policy_line

            clinic = await get_clinic_by_id(clinic_id_val)

            # Resolve patient language
            lang = "en"
            try:
                from app.database import get_patient_by_phone
                patient_phone = booking.get("patient_phone")
                if patient_phone:
                    patient_res = get_patient_by_phone(clinic_id_val, patient_phone)
                    if hasattr(patient_res, "__await__"):
                        patient_res = await patient_res
                    if patient_res and isinstance(patient_res, dict) and patient_res.get("language"):
                        lang = patient_res["language"]
            except Exception as lang_err:
                logger.warning(f"Could not resolve patient language for confirmation: {lang_err}")

            date_display = booking.get("appointment_date", "")
            try:
                from datetime import datetime as dt

                date_display = dt.strptime(date_display, "%Y-%m-%d").strftime(
                    "%d %b %Y"
                )
            except Exception:
                pass

            amount_rupees = (booking.get("amount_paise") or 0) / 100
            slot_time_display = format_slot_time(booking.get("appointment_time")) or "N/A"
            ref_code = booking.get("booking_ref", "N/A")
            # Per-clinic cutoff, and the exact deadline computed from it. The
            # same helper the refund gate uses, so the promise made here is
            # the promise enforced there.
            window_hours = cancellation_window_hours(clinic)
            policy_line = cancellation_policy_line(
                lang,
                window_hours,
                booking.get("appointment_date", ""),
                booking.get("appointment_time", ""),
            )

            if booking.get("booking_type") == "lab_test":
                test_name = booking.get("lab_test_name", "N/A")
                if lang == "hi":
                    msg = (
                        f"✅ *भुगतान सफल — टेस्ट बुक हो गया!*\n\n"
                        f"📋 *बुकिंग संदर्भ:* {ref_code}\n"
                        f"🧪 *टेस्ट:* {test_name}\n"
                        f"📅 *सैंपल संग्रह तिथि:* {date_display}\n"
                        f"💰 *भुगतान राशि:* ₹{amount_rupees:.0f}\n\n"
                        f"📌 कृपया वैध पहचान पत्र के साथ हमारे सैंपल संग्रह समय के दौरान पहुंचें।\n\n"
                    )
                elif lang == "te":
                    msg = (
                        f"✅ *చెల్లింపు నిర్ధారించబడింది — టెస్ట్ బుక్ చేయబడింది!*\n\n"
                        f"📋 *బుకింగ్ రిఫరెన్స్:* {ref_code}\n"
                        f"🧪 *టెస్ట్:* {test_name}\n"
                        f"📅 *సేకరణ తేదీ:* {date_display}\n"
                        f"💰 *చెల్లించిన మొత్తం:* ₹{amount_rupees:.0f}\n\n"
                        f"📌 దయచేసి చెల్లుబాటు అయ్యే ఐడీతో మా శాంపిల్ సేకరణ వేళల్లో రండి.\n\n"
                    )
                else:
                    msg = (
                        f"✅ *Payment Confirmed — Test Booked!*\n\n"
                        f"📋 *Booking Ref:* {ref_code}\n"
                        f"🧪 *Test:* {test_name}\n"
                        f"📅 *Collection Date:* {date_display}\n"
                        f"💰 *Paid:* ₹{amount_rupees:.0f}\n\n"
                        f"📌 Please arrive during our sample collection hours with a valid ID.\n\n"
                    )
            else:
                doc_name = booking.get("doctor_name", "N/A")
                dept_name = booking.get("department", "N/A")
                if lang == "hi":
                    msg = (
                        f"✅ *भुगतान सफल — अपॉइंटमेंट बुक हो गई!*\n\n"
                        f"📋 *बुकिंग संदर्भ:* {ref_code}\n"
                        f"👨‍⚕️ *डॉक्टर:* {doc_name}\n"
                        f"🏥 *विभाग:* {dept_name}\n"
                        f"📅 *दिनांक:* {date_display}\n"
                        f"🕐 *समय:* {slot_time_display}\n"
                        f"💰 *भुगतान राशि:* ₹{amount_rupees:.0f}\n\n"
                        f"📌 कृपया प्रासंगिक मेडिकल रिकॉर्ड के साथ 15 मिनट पहले पहुंचें।\n\n"
                        f"_नो-शो बुकिंग गैर-वापसी योग्य हैं।_"
                    )
                elif lang == "te":
                    msg = (
                        f"✅ *చెల్లింపు నిర్ధారించబడింది — అపాయింట్‌మెంట్ బుక్ అయింది!*\n\n"
                        f"📋 *బుకింగ్ రిఫరెన్స్:* {ref_code}\n"
                        f"👨‍⚕️ *డాక్టర్:* {doc_name}\n"
                        f"🏥 *విభాగం:* {dept_name}\n"
                        f"📅 *తేదీ:* {date_display}\n"
                        f"🕐 *సమయం:* {slot_time_display}\n"
                        f"💰 *చెల్లించిన మొత్తం:* ₹{amount_rupees:.0f}\n\n"
                        f"📌 దయచేసి సంబంధిత మెడికల్ రికార్డులతో 15 నిమిషాల ముందుగా రండి.\n\n"
                        f"_నో-షో బుకింగ్‌లకు రీఫండ్ ఉండదు._"
                    )
                else:
                    msg = (
                        f"✅ *Payment Confirmed — Appointment Booked!*\n\n"
                        f"📋 *Booking Ref:* {ref_code}\n"
                        f"👨‍⚕️ *Doctor:* {doc_name}\n"
                        f"🏥 *Department:* {dept_name}\n"
                        f"📅 *Date:* {date_display}\n"
                        f"🕐 *Time:* {slot_time_display}\n"
                        f"💰 *Paid:* ₹{amount_rupees:.0f}\n\n"
                        f"📌 Please arrive 15 minutes early with any relevant medical records.\n\n"
                        f"_No-show bookings are non-refundable._"
                    )

            if policy_line:
                msg += "\n\n" + policy_line

            # Append location details
            if booking.get("branch_id"):
                try:
                    branch = await get_branch_by_id(booking["branch_id"])
                    if branch:
                        branch_name = branch.get("name") or booking.get("branch_name", "")
                        branch_address = branch.get("address", "")
                        branch_landmark = branch.get("landmark", "")
                        branch_maps = branch.get("maps_link", "")
                        location_line = f"\n\n📍 Location: {branch_name}"
                        if branch_address:
                            location_line += f", {branch_address}"
                        if branch_landmark:
                            location_line += f"\nNear {branch_landmark}"
                        if branch_maps:
                            location_line += f"\n🗺️ Google Maps: {branch_maps}"
                        msg += location_line
                except Exception as branch_err:
                    logger.warning(f"Failed to append branch location: {branch_err}")
            else:
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

            # Deliver WhatsApp text to patient with 2-attempt retry loop
            patient_phone = booking.get("patient_phone")
            patient_notified = False
            for attempt in range(2):
                try:
                    await whatsapp_service.send_text(
                        clinic, patient_phone, msg, _source="booking_confirmation"
                    )
                    patient_notified = True
                    logger.info(
                        f"Sent payment confirmation to {patient_phone[:6]}*** (attempt {attempt + 1})"
                    )
                    break
                except Exception as send_err:
                    logger.warning(
                        f"Failed to send payment confirmation to patient {patient_phone[:6]}*** (attempt {attempt + 1}/2): {send_err}"
                    )
                    if attempt == 0:
                        import asyncio
                        await asyncio.sleep(2)

            if not patient_notified:
                logger.error(
                    f"CRITICAL: Failed to deliver payment confirmation WhatsApp message to patient {patient_phone} for booking {ref_code}"
                )
                # Escalate to clinic admin via alert
                try:
                    alert_msg = (
                        f"⚠️ *Payment Confirmation WhatsApp Delivery Failed*\n\n"
                        f"Patient: {booking.get('patient_name', 'Patient')} ({patient_phone})\n"
                        f"Booking Ref: {ref_code}\n"
                        f"Doctor: {booking.get('doctor_name', 'N/A')}\n"
                        f"Date: {date_display} at {slot_time_display}\n"
                        f"Paid: ₹{amount_rupees:.0f}\n\n"
                        f"Please contact the patient manually to confirm their appointment."
                    )
                    await self._alert_admin(clinic, alert_msg)
                except Exception as alert_err:
                    logger.error(f"Failed to send admin alert on delivery failure: {alert_err}")

            # Send interactive buttons for Main Menu
            follow_up_msg = {
                "en": "What would you like to do next?",
                "hi": "आप आगे क्या करना चाहेंगे?",
                "te": "మీరు ఇంకా ఏమి చేయాలనుకుంటున్నారు?",
            }.get(lang, "What would you like to do next?")

            btn_title = {
                "en": "Main Menu",
                "hi": "मुख्य मेनू",
                "te": "ప్రధాన మెనూ",
            }.get(lang, "Main Menu")

            try:
                await whatsapp_service.send_interactive_buttons(
                    clinic,
                    patient_phone,
                    body=follow_up_msg,
                    buttons=[{"id": "main_menu", "title": btn_title}],
                    _source="payment",
                )
            except Exception as btn_err:
                logger.warning(f"Failed to send interactive buttons after payment confirmation: {btn_err}")
                try:
                    await whatsapp_service.send_text(
                        clinic, patient_phone, follow_up_msg, _source="payment"
                    )
                except Exception:
                    pass

            # Update conversation state to main_menu
            try:
                from app.services.conversation import conversation_manager
                await conversation_manager.update_state(
                    clinic, patient_phone, "main_menu"
                )
            except Exception as state_err:
                logger.warning(f"Failed to update conversation state: {state_err}")

            # Send real-time WhatsApp alert to Clinic Admin Phone
            try:
                if booking.get("booking_type") == "lab_test":
                    admin_notif_msg = (
                        f"✅ *New Lab Test Booking & Payment Confirmed!*\n\n"
                        f"📋 *Booking Ref:* {ref_code}\n"
                        f"👤 *Patient:* {booking.get('patient_name', 'Patient')} ({patient_phone})\n"
                        f"🧪 *Test:* {booking.get('lab_test_name', 'N/A')}\n"
                        f"📅 *Collection Date:* {date_display}\n"
                        f"💰 *Paid:* ₹{amount_rupees:.0f}\n"
                        f"🆔 *Payment ID:* {booking.get('payment_id', 'N/A')}"
                    )
                else:
                    admin_notif_msg = (
                        f"✅ *New Payment & Booking Confirmed!*\n\n"
                        f"📋 *Booking Ref:* {ref_code}\n"
                        f"👤 *Patient:* {booking.get('patient_name', 'Patient')} ({patient_phone})\n"
                        f"👨‍⚕️ *Doctor:* {booking.get('doctor_name', 'N/A')}\n"
                        f"🏥 *Department:* {booking.get('department', 'N/A')}\n"
                        f"📅 *Date:* {date_display}\n"
                        f"🕐 *Time:* {slot_time_display}\n"
                        f"💰 *Paid:* ₹{amount_rupees:.0f}\n"
                        f"🆔 *Payment ID:* {booking.get('payment_id', 'N/A')}"
                    )
                await self._alert_admin(clinic, admin_notif_msg)
            except Exception as admin_alert_err:
                logger.warning(f"Failed to send admin WhatsApp alert: {admin_alert_err}")

            # In-App Admin Notification creation (for staff & admin web dashboard)
            try:
                if clinic_id_val and str(clinic_id_val).strip().lower() not in ("default", "none", "null", ""):
                    if booking.get("booking_type") == "lab_test":
                        in_app_msg = (
                            f"Patient {booking.get('patient_name', 'Patient')} ({patient_phone}) booked lab test "
                            f"{booking.get('lab_test_name', 'N/A')} on "
                            f"{date_display}. Paid ₹{amount_rupees:.0f} (Payment ID: {booking.get('payment_id', 'N/A')})."
                        )
                    else:
                        in_app_msg = (
                            f"Patient {booking.get('patient_name', 'Patient')} ({patient_phone}) booked "
                            f"{booking.get('doctor_name', 'N/A')} ({booking.get('department', 'N/A')}) on "
                            f"{date_display} at {slot_time_display}. Paid ₹{amount_rupees:.0f} (Payment ID: {booking.get('payment_id', 'N/A')})."
                        )
                    notif_row = {
                        "clinic_id": clinic_id_val,
                        "admin_id": None,
                        "title": (
                            f"New Lab Test Booking Confirmed ({ref_code})"
                            if booking.get("booking_type") == "lab_test"
                            else f"New Booking Confirmed ({ref_code})"
                        ),
                        "message": in_app_msg,
                        "is_read": False,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    # unscoped: insert_scoped_by_payload
                    await sb(supabase.table("admin_notifications").insert(notif_row))
            except Exception as in_app_err:
                logger.warning(f"Could not create in-app admin notification: {in_app_err}")

            # Log analytics event for clinic dashboard
            try:
                from app.database import log_analytics_event
                analytics_res = log_analytics_event(
                    clinic_id_val,
                    patient_phone,
                    "appointment_booked",
                    department=booking.get("department"),
                )
                if hasattr(analytics_res, "__await__"):
                    await analytics_res
            except Exception as analytics_err:
                logger.warning(f"Could not log analytics event: {analytics_err}")

            # Log confirmation dispatched event
            if booking.get("id"):
                try:
                    self._log_payment_event(
                        booking["id"],
                        "confirmation_dispatched",
                        {
                            "patient_phone": patient_phone,
                            "patient_notified": patient_notified,
                            "payment_id": booking.get("payment_id"),
                        },
                        clinic_id=clinic_id_val,
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Failed in _notify_payment_confirmed: {e}")

    async def resolve_patient_language(self, clinic_id: str, phone: str) -> str:
        """Patient's chosen language, defaulting to English on any failure."""
        try:
            from app.database import get_patient_by_phone

            if not phone:
                return "en"
            patient = get_patient_by_phone(clinic_id, phone)
            if hasattr(patient, "__await__"):
                patient = await patient
            if isinstance(patient, dict):
                lang = patient.get("language")
                if lang in ("en", "hi", "te"):
                    return lang
        except Exception as e:
            logger.warning(f"Could not resolve patient language: {e}")
        return "en"

    async def notify_cancellation_outcome(
        self,
        booking: dict,
        refund: Optional[dict],
        clinic: Optional[dict] = None,
    ) -> None:
        """Tell the patient what actually happened to their money.

        THE single place a cancellation outcome is worded, used by both the
        patient's own cancel in the bot and an admin cancel in the panel — so
        the two can never tell the same patient different stories.

        Three outcomes, three different messages:
          refunded  -> itemised refund receipt with the Razorpay reference
          too late  -> the clinic's non-refundable policy, quoting its window
          failed    -> cancelled, refund needs a human; says so plainly rather
                       than implying money is on its way

        Never raises: a notification failure must not roll back a cancellation
        the patient already asked for.
        """
        try:
            from app.services.whatsapp import whatsapp_service
            from app.services.tenant import get_clinic_by_id, cancellation_window_hours
            from app.templates.whatsapp_templates import get_message

            phone = booking.get("patient_phone")
            if not phone:
                return

            clinic_id_val = booking.get("clinic_id") or "default"
            if clinic is None:
                clinic = await get_clinic_by_id(clinic_id_val)
            lang = await self.resolve_patient_language(clinic_id_val, phone)

            date_display = booking.get("appointment_date", "")
            try:
                date_display = datetime.strptime(date_display, "%Y-%m-%d").strftime("%d %b %Y")
            except (ValueError, TypeError):
                pass

            if refund and refund.get("success"):
                amount = refund.get("amount_inr")
                if amount is None:
                    amount = (booking.get("amount_paise") or 0) / 100
                msg = get_message(
                    "refund_receipt",
                    lang,
                    doctor=booking.get("doctor_name") or "N/A",
                    date=date_display or "N/A",
                    amount=f"{float(amount):.0f}",
                    refund_id=refund.get("refund_id") or "pending",
                )
            elif refund and refund.get("is_late"):
                hours = refund.get("window_hours")
                if hours is None:
                    hours = cancellation_window_hours(clinic)
                # A 0-hour ("anytime") window can only be late once the slot has
                # already started; "within 0 hours of the slot" reads as a bug.
                msg = (
                    get_message("refund_late_slot_started", lang)
                    if not hours
                    else get_message("refund_late_no_refund", lang, hours=hours)
                )
            elif refund:
                msg = get_message("refund_failed_manual_review", lang)
            else:
                return

            await whatsapp_service.send_text(clinic, phone, msg, _source="payment")
            logger.info(
                f"Sent cancellation outcome ({'refunded' if refund.get('success') else refund.get('reason')}) "
                f"to {phone[:6]}*** for booking {booking.get('id')}"
            )
        except Exception as e:
            logger.error(f"Failed to send cancellation outcome notification: {e}")

    async def _notify_booking_cancelled(self, booking: dict, refunded: bool) -> None:
        """Send WhatsApp notice to the patient after an admin cancels their booking."""
        try:
            from app.services.whatsapp import whatsapp_service
            from app.services.tenant import get_clinic_by_id

            clinic_id_val = booking.get("clinic_id") or "default"
            clinic = await get_clinic_by_id(clinic_id_val)

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
                f"🕐 *Time:* {format_slot_time(booking.get('appointment_time')) or 'N/A'}\n\n"
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

    async def _notify_late_payment_refunded(self, booking: dict, refund: dict) -> None:
        """Send WhatsApp notice when payment arrived after slot hold expired and was auto-refunded."""
        try:
            from app.services.whatsapp import whatsapp_service
            from app.services.tenant import get_clinic_by_id

            clinic_id_val = booking.get("clinic_id") or "default"
            clinic = await get_clinic_by_id(clinic_id_val)
            amount_rupees = (booking.get("amount_paise") or 0) / 100
            msg = (
                f"⚠️ *Payment Received After Slot Hold Expired*\n\n"
                f"📋 *Booking Ref:* {booking.get('booking_ref', 'N/A')}\n"
                f"💰 We received your payment of ₹{amount_rupees:.0f}, but the slot hold had already expired.\n\n"
                f"A full refund of ₹{amount_rupees:.0f} has been automatically initiated (Refund ID: {refund.get('refund_id', 'pending')}) "
                f"and will reflect in your account within 5-7 business days.\n\n"
                f"Please visit {clinic.get('whatsapp_number', '')} to select a new appointment slot."
            )
            await whatsapp_service.send_text(clinic, booking["patient_phone"], msg, _source="payment")
        except Exception as e:
            logger.error(f"Failed to send late payment refund notification: {e}")

    async def _alert_admin(self, clinic_or_message: Union[dict, str, None], message: Optional[str] = None) -> None:
        """Send alert to clinic admin phone with platform operator fallback."""
        if isinstance(clinic_or_message, str):
            msg = clinic_or_message
            clinic = {}
        else:
            clinic = clinic_or_message or {}
            msg = message or ""

        try:
            from app.services.whatsapp import whatsapp_service
            from app.services.tenant import get_clinic_contact, resolve_tenant

            admin_phone = ""
            if clinic:
                admin_phone = get_clinic_contact(clinic, "admin_phone", "") or get_clinic_contact(clinic, "phone", "")
            if not admin_phone:
                admin_phone = settings.hospital_phone

            target_clinic = clinic
            if not target_clinic:
                try:
                    target_clinic = await resolve_tenant(admin_phone)
                except Exception:
                    target_clinic = {}

            if admin_phone:
                await whatsapp_service.send_text(target_clinic, admin_phone, msg, _source="payment")
        except Exception as e:
            logger.error(f"Failed to send admin alert: {e}")
            logger.warning(f"ADMIN ALERT (undelivered): {msg}")

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
