"""Configurable cancellation window, dynamic booking policy notes, refund receipts.

The invariant these protect: the deadline quoted to the patient at booking time
and the deadline the refund gate enforces are resolved by ONE helper, so they
can never disagree. A patient told "4 hours" must not be refused at 4h01m.
"""

import sys

# app.database is faked into sys.modules by some sibling test modules; undo that
# before importing anything that binds app.database.sb at import time.
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.tenant import (
    CANCELLATION_WINDOW_CHOICES,
    cancellation_window_hours,
)
from app.templates.whatsapp_templates import (
    cancellation_cutoff,
    cancellation_policy_line,
    get_message,
)

IST = timezone(timedelta(hours=5, minutes=30))


def clinic(window=None, **extra):
    cfg = dict(extra)
    if window is not None:
        cfg["cancellation_window_hours"] = window
    return {"id": "11111111-1111-1111-1111-111111111111", "name": "City Care", "config": cfg}


# ── A. The configurable window ──────────────────────────────────────────────


def test_the_offered_tiers_are_exactly_the_six():
    assert CANCELLATION_WINDOW_CHOICES == (0, 2, 4, 6, 12, 24)


def test_default_is_four_hours_when_a_clinic_has_not_chosen():
    assert cancellation_window_hours(clinic()) == 4
    assert cancellation_window_hours({}) == 4
    assert cancellation_window_hours(None) == 4


@pytest.mark.parametrize("hours", [0, 2, 4, 6, 12, 24])
def test_every_tier_is_honoured(hours):
    assert cancellation_window_hours(clinic(hours)) == hours


def test_zero_means_anytime_not_missing():
    """0 is a real choice. Treating it as falsy would silently restore 4h."""
    assert cancellation_window_hours(clinic(0)) == 0


def test_a_numeric_string_from_jsonb_is_accepted():
    assert cancellation_window_hours(clinic("12")) == 12


@pytest.mark.parametrize("bad", [-1, 3, 999, "soon", None])
def test_an_off_tier_or_junk_value_falls_back_to_the_default(bad):
    """A hand-edited config must not end up refunding nobody."""
    assert cancellation_window_hours(clinic(bad)) == 4


# ── B. The policy note in the booking confirmation ──────────────────────────


def test_cutoff_is_the_slot_minus_the_window_in_ist():
    cutoff = cancellation_cutoff("2026-09-05", "10:00", 4)
    assert cutoff == datetime(2026, 9, 5, 6, 0, tzinfo=IST)


def test_a_window_larger_than_the_time_of_day_rolls_to_the_previous_day():
    cutoff = cancellation_cutoff("2026-09-05", "10:00", 24)
    assert cutoff == datetime(2026, 9, 4, 10, 0, tzinfo=IST)


def test_zero_window_cutoff_is_the_slot_itself():
    assert cancellation_cutoff("2026-09-05", "10:00", 0) == datetime(2026, 9, 5, 10, 0, tzinfo=IST)


def test_cutoff_tolerates_seconds_in_the_stored_time():
    assert cancellation_cutoff("2026-09-05", "10:00:00", 4) == datetime(2026, 9, 5, 6, 0, tzinfo=IST)


@pytest.mark.parametrize("date_str,time_str", [("garbage", "10:00"), ("2026-09-05", ""), ("2026-09-05", None)])
def test_an_unparseable_slot_yields_no_line_rather_than_a_wrong_deadline(date_str, time_str):
    assert cancellation_cutoff(date_str, time_str, 4) is None
    assert cancellation_policy_line("en", 4, date_str, time_str) == ""


@pytest.mark.parametrize("lang", ["en", "hi", "te"])
def test_the_note_renders_in_every_supported_language(lang):
    line = cancellation_policy_line(lang, 4, "2026-09-05", "10:00")
    assert line
    assert "4" in line
    assert "05 Sep 2026, 06:00 AM" in line


def test_english_note_matches_the_specified_wording():
    line = cancellation_policy_line("en", 4, "2026-09-05", "10:00")
    assert line == (
        "\u2139\ufe0f Cancellation Policy: Free cancellation with full refund is "
        "available up to 4 hours before your slot (before 05 Sep 2026, 06:00 AM)."
    )


def test_the_note_tracks_the_clinics_own_window():
    for hours in (2, 6, 12, 24):
        line = cancellation_policy_line("en", hours, "2026-09-05", "10:00")
        assert f"up to {hours} hours" in line


def test_a_zero_window_never_says_up_to_0_hours():
    """'up to 0 hours before your slot' is nonsense; say 'any time' instead."""
    line = cancellation_policy_line("en", 0, "2026-09-05", "10:00")
    assert "0 hours" not in line
    assert "any time before your appointment starts" in line


def test_an_unpaid_booking_is_never_promised_a_refund():
    """The direct-booking flow takes no money. Promising one is a lie."""
    line = cancellation_policy_line("en", 4, "2026-09-05", "10:00", refundable=False)
    assert "refund" not in line.lower()
    assert "05 Sep 2026, 06:00 AM" in line


@pytest.mark.parametrize("lang", ["hi", "te"])
def test_unpaid_note_also_avoids_refund_wording_in_translation(lang):
    line = cancellation_policy_line(lang, 4, "2026-09-05", "10:00", refundable=False)
    assert line
    assert "05 Sep 2026, 06:00 AM" in line


# ── C. The refund gate reads the same window ────────────────────────────────


def _booking(hours_from_now, **over):
    slot = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    row = {
        "id": "booking-1",
        "status": "confirmed",
        "payment_id": "pay_1",
        "amount_paise": 50000,
        "doctor_name": "Dr. Rao",
        "patient_phone": "+919876543210",
        "clinic_id": "11111111-1111-1111-1111-111111111111",
        "appointment_date": slot.astimezone(IST).strftime("%Y-%m-%d"),
        "appointment_time": slot.astimezone(IST).strftime("%H:%M"),
    }
    row.update(over)
    return row


async def _refund(booking, clinic_dict):
    from app.services.payment import PaymentService

    with patch("app.services.payment.supabase") as mock_sb:
        table = MagicMock()
        mock_sb.table.return_value = table
        table.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[booking])
        table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[booking])
        with patch.object(
            PaymentService, "_create_razorpay_refund", new=AsyncMock(return_value={"id": "rfnd_abc123"})
        ):
            return await PaymentService().initiate_refund("booking-1", "patient_cancelled", clinic=clinic_dict)


@pytest.mark.asyncio
async def test_a_clinic_with_a_wider_window_refuses_what_the_default_would_allow():
    """8h before the slot: fine under the 4h default, too late under 12h."""
    booking = _booking(8)
    assert (await _refund(booking, clinic(4)))["success"] is True
    late = await _refund(booking, clinic(12))
    assert late["success"] is False
    assert late["is_late"] is True
    assert "refund_window_closed" in late["reason"]
    assert late["window_hours"] == 12


@pytest.mark.asyncio
async def test_a_clinic_with_a_narrower_window_allows_what_the_default_would_refuse():
    booking = _booking(3)
    assert (await _refund(booking, clinic(4)))["success"] is False
    assert (await _refund(booking, clinic(2)))["success"] is True


@pytest.mark.asyncio
async def test_anytime_window_refunds_right_up_to_the_slot():
    assert (await _refund(_booking(0.25), clinic(0)))["success"] is True


@pytest.mark.asyncio
async def test_anytime_window_still_refuses_once_the_slot_has_passed():
    result = await _refund(_booking(-1), clinic(0))
    assert result["success"] is False
    assert result["is_late"] is True


@pytest.mark.asyncio
async def test_a_successful_refund_reports_the_amount_and_reference():
    result = await _refund(_booking(48), clinic(4))
    assert result["success"] is True
    assert result["refund_id"] == "rfnd_abc123"
    assert result["amount_inr"] == 500.0
    assert result["is_late"] is False


@pytest.mark.asyncio
async def test_every_refund_outcome_carries_the_same_keys():
    """Callers branch on is_late; a missing key would be a KeyError in prod."""
    outcomes = [
        await _refund(_booking(48), clinic(4)),                       # success
        await _refund(_booking(1), clinic(4)),                        # late
        await _refund(_booking(48, payment_id=None), clinic(4)),      # unpaid
        await _refund(_booking(48, status="cancelled"), clinic(4)),   # wrong state
    ]
    for out in outcomes:
        for key in ("success", "refund_id", "amount_inr", "is_late", "reason"):
            assert key in out, (key, out)


@pytest.mark.asyncio
async def test_only_a_closed_window_is_flagged_late():
    """A gateway failure must not be worded as 'you cancelled too late'."""
    assert (await _refund(_booking(48, payment_id=None), clinic(4)))["is_late"] is False
    assert (await _refund(_booking(48, status="cancelled"), clinic(4)))["is_late"] is False


# ── D. The WhatsApp refund receipt ──────────────────────────────────────────


async def _notify(refund, lang="en", booking=None):
    from app.services.payment import PaymentService

    svc = PaymentService()
    sent = {}

    async def fake_send(clinic_arg, phone, message, **kw):
        sent["phone"] = phone
        sent["message"] = message
        sent["source"] = kw.get("_source")
        return True

    with patch("app.services.whatsapp.whatsapp_service.send_text", new=fake_send), \
         patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value=clinic(4))), \
         patch.object(PaymentService, "resolve_patient_language", new=AsyncMock(return_value=lang)):
        await svc.notify_cancellation_outcome(booking or _booking(48), refund, clinic=clinic(4))
    return sent


@pytest.mark.asyncio
async def test_a_successful_refund_sends_an_itemised_receipt():
    sent = await _notify({"success": True, "refund_id": "rfnd_abc123", "amount_inr": 500.0, "is_late": False})
    body = sent["message"]
    assert "Appointment Cancelled & Refund Initiated" in body
    assert "Dr. Rao" in body
    assert "Refund Amount: \u20b9500" in body
    assert "Refund Reference: rfnd_abc123" in body
    assert "Payment Gateway: Razorpay" in body
    assert "2 to 5 business days" in body


@pytest.mark.asyncio
async def test_the_receipt_goes_to_the_patient_on_the_booking():
    sent = await _notify({"success": True, "refund_id": "r1", "amount_inr": 500.0})
    assert sent["phone"] == "+919876543210"


@pytest.mark.asyncio
async def test_a_late_cancellation_gets_the_non_refundable_notice_not_a_receipt():
    sent = await _notify(
        {"success": False, "is_late": True, "reason": "refund_window_closed_need_4h_before_slot",
         "window_hours": 4, "refund_id": "", "amount_inr": 500.0}
    )
    body = sent["message"]
    assert "non-refundable" in body
    assert "within 4 hours" in body
    assert "Refund Reference" not in body


@pytest.mark.asyncio
async def test_the_late_notice_quotes_the_clinics_own_window():
    sent = await _notify(
        {"success": False, "is_late": True, "reason": "refund_window_closed_need_12h_before_slot",
         "window_hours": 12, "refund_id": "", "amount_inr": 500.0}
    )
    assert "within 12 hours" in sent["message"]


@pytest.mark.asyncio
async def test_a_gateway_failure_does_not_promise_money_is_on_its_way():
    """The worst outcome is telling a patient a refund is coming when it isn't."""
    sent = await _notify(
        {"success": False, "is_late": False, "reason": "razorpay_error: boom",
         "refund_id": "", "amount_inr": 500.0}
    )
    body = sent["message"]
    assert "manually" in body
    assert "Refund Reference" not in body
    assert "2 to 5 business days" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["hi", "te"])
async def test_the_receipt_is_translated(lang):
    sent = await _notify({"success": True, "refund_id": "rfnd_x", "amount_inr": 750.0}, lang=lang)
    body = sent["message"]
    assert "rfnd_x" in body
    assert "Razorpay" in body
    assert "Appointment Cancelled" not in body  # i.e. it is not the English copy


@pytest.mark.asyncio
async def test_an_unpaid_cancellation_sends_no_money_message_at_all():
    sent = await _notify(None)
    assert sent == {}


@pytest.mark.asyncio
async def test_a_booking_with_no_phone_is_skipped_rather_than_crashing():
    sent = await _notify(
        {"success": True, "refund_id": "r1", "amount_inr": 500.0},
        booking=_booking(48, patient_phone=None),
    )
    assert sent == {}


# ── E. Patient cancels in the bot: refund runs BEFORE the status flips ──────


async def _patient_cancel(booking, refund_result):
    """Drive ConversationManager._cancel_with_refund with everything stubbed."""
    from app.services.conversation import ConversationManager

    calls = {"order": [], "notified": None}

    async def fake_db_cancel(clinic_id, appointment_id):
        calls["order"].append("db_cancel")
        return True

    async def fake_refund(booking_id, reason="", clinic=None, **kw):
        calls["order"].append("refund")
        calls["reason"] = reason
        return refund_result

    async def fake_notify(bk, rf, clinic=None):
        calls["notified"] = rf

    async def fake_sb(builder):
        return MagicMock(data=[booking] if booking else [])

    with patch("app.database.cancel_appointment", new=fake_db_cancel), \
         patch("app.services.payment.payment_service.initiate_refund", new=fake_refund), \
         patch("app.services.payment.payment_service.notify_cancellation_outcome", new=fake_notify), \
         patch("app.services.conversation.sb", new=fake_sb), \
         patch("app.services.conversation.supabase", MagicMock(), create=True):
        mgr = ConversationManager()
        cancelled, refund = await mgr._cancel_with_refund(clinic(4), "+919876543210", "booking-1")
    return cancelled, refund, calls


@pytest.mark.asyncio
async def test_refund_is_attempted_before_the_booking_is_marked_cancelled():
    """initiate_refund only accepts 'confirmed'. Cancel first and every refund
    would fail with cannot_refund_status_cancelled — keeping the money."""
    ok = {"success": True, "refund_id": "rfnd_1", "amount_inr": 500.0, "is_late": False}
    cancelled, refund, calls = await _patient_cancel(_booking(48), ok)
    assert cancelled is True
    assert calls["order"][0] == "refund"
    assert "db_cancel" not in calls["order"]  # initiate_refund already set 'refunded'
    assert calls["reason"] == "patient_cancelled"
    assert calls["notified"] == ok


@pytest.mark.asyncio
async def test_a_late_cancellation_still_cancels_the_appointment():
    """Refusing the refund must not leave the slot blocked."""
    late = {"success": False, "is_late": True, "window_hours": 4,
            "reason": "refund_window_closed_need_4h_before_slot", "refund_id": "", "amount_inr": 500.0}
    cancelled, refund, calls = await _patient_cancel(_booking(1), late)
    assert cancelled is True
    assert calls["order"] == ["refund", "db_cancel"]
    assert calls["notified"]["is_late"] is True


@pytest.mark.asyncio
async def test_a_gateway_failure_still_cancels_and_still_notifies():
    fail = {"success": False, "is_late": False, "reason": "razorpay_error: boom",
            "refund_id": "", "amount_inr": 500.0}
    cancelled, refund, calls = await _patient_cancel(_booking(48), fail)
    assert cancelled is True
    assert calls["order"] == ["refund", "db_cancel"]
    assert calls["notified"] is fail


@pytest.mark.asyncio
async def test_an_unpaid_booking_never_touches_the_refund_api():
    cancelled, refund, calls = await _patient_cancel(_booking(48, payment_id=None), None)
    assert cancelled is True
    assert refund is None
    assert calls["order"] == ["db_cancel"]
    assert calls["notified"] is None


@pytest.mark.asyncio
async def test_an_unreadable_booking_falls_back_to_a_plain_cancel():
    cancelled, refund, calls = await _patient_cancel(None, None)
    assert refund is None
    assert calls["order"] == ["db_cancel"]


# ── F. Admin settings API ───────────────────────────────────────────────────

import base64

from fastapi.testclient import TestClient

from app.config import settings as app_settings
from app.main import app

client = TestClient(app)
CLINIC_ID = "11111111-1111-1111-1111-111111111111"


def admin_auth():
    creds = f"{app_settings.admin_username}:{app_settings.admin_password}"
    return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}


def _settings_mocks(existing_cfg, written):
    """Route the clinics table through one mock and capture the written config."""
    row = {"id": CLINIC_ID, "name": "City Care", "plan": "essential",
           "whatsapp_number": "+919000000000", "config": existing_cfg}

    def table(name):
        obj = MagicMock()
        obj.select.return_value = obj
        for m in ("eq", "neq", "gte", "lt", "in_", "order", "limit"):
            getattr(obj, m).return_value = obj
        obj._rows = [row]

        def _update(payload):
            written.update(payload)
            up = MagicMock()
            up.eq.return_value = up
            up._rows = [{**row, **payload}]
            return up

        obj.update.side_effect = _update
        return obj

    async def fake_sb(builder):
        return MagicMock(data=getattr(builder, "_rows", []))

    return row, table, fake_sb


@pytest.mark.parametrize("hours", [0, 2, 4, 6, 12, 24])
def test_every_tier_can_be_saved_through_the_settings_api(hours):
    written = {}
    row, table, fake_sb = _settings_mocks({"razorpay_key_id": "rzp_x"}, written)
    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=row)), \
         patch("app.routers.admin.enforce_clinic_access", return_value=CLINIC_ID), \
         patch("app.routers.admin.supabase") as sb_mod, \
         patch("app.routers.admin.sb", side_effect=fake_sb), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock), \
         patch("app.routers.admin.invalidate_tenant_cache"):
        sb_mod.table.side_effect = table
        res = client.put("/admin/settings/payment", headers=admin_auth(),
                         json={"cancellation_window_hours": hours})
    assert res.status_code == 200, res.text
    assert written["config"]["cancellation_window_hours"] == hours


@pytest.mark.parametrize("bad", [1, 3, 5, 8, 48, -4])
def test_an_off_tier_window_is_rejected_by_the_api(bad):
    res = client.put("/admin/settings/payment", headers=admin_auth(),
                     json={"cancellation_window_hours": bad})
    assert res.status_code == 422


def test_saving_the_window_preserves_the_existing_razorpay_config():
    """A partial update must not wipe keys the clinic already saved."""
    written = {}
    existing = {"razorpay_key_id": "rzp_live_x", "razorpay_key_secret": "s3cr3t",
                "payment_mode": "full"}
    row, table, fake_sb = _settings_mocks(existing, written)
    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=row)), \
         patch("app.routers.admin.enforce_clinic_access", return_value=CLINIC_ID), \
         patch("app.routers.admin.supabase") as sb_mod, \
         patch("app.routers.admin.sb", side_effect=fake_sb), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock), \
         patch("app.routers.admin.invalidate_tenant_cache"):
        sb_mod.table.side_effect = table
        res = client.put("/admin/settings/payment", headers=admin_auth(),
                         json={"cancellation_window_hours": 12})
    assert res.status_code == 200, res.text
    cfg = written["config"]
    assert cfg["razorpay_key_id"] == "rzp_live_x"
    assert cfg["razorpay_key_secret"] == "s3cr3t"
    assert cfg["payment_mode"] == "full"
    assert cfg["cancellation_window_hours"] == 12


def test_the_settings_api_reports_the_window_actually_enforced():
    row = {"id": CLINIC_ID, "name": "City Care", "config": {"cancellation_window_hours": 24}}
    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=row)), \
         patch("app.routers.admin.enforce_clinic_access", return_value=CLINIC_ID):
        body = client.get("/admin/settings/payment", headers=admin_auth()).json()
    assert body["cancellation_window_hours"] == 24
    assert body["cancellation_window_choices"] == [0, 2, 4, 6, 12, 24]


def test_a_clinic_that_never_chose_reads_back_the_platform_default():
    row = {"id": CLINIC_ID, "name": "City Care", "config": {}}
    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=row)), \
         patch("app.routers.admin.enforce_clinic_access", return_value=CLINIC_ID):
        body = client.get("/admin/settings/payment", headers=admin_auth()).json()
    assert body["cancellation_window_hours"] == 4


def test_settings_endpoints_require_auth():
    assert client.get("/admin/settings/payment").status_code == 401
    assert client.put("/admin/settings/payment", json={"cancellation_window_hours": 4}).status_code == 401


# ── G. Admin cancels a paid booking ─────────────────────────────────────────


async def _admin_cancel(booking, refund_result):
    from app.services.payment import PaymentService

    svc = PaymentService()
    calls = {"updates": [], "notified": "none"}

    def table(name):
        obj = MagicMock()
        obj.select.return_value = obj
        obj.eq.return_value = obj
        obj._rows = [booking]

        def _update(payload):
            calls["updates"].append(payload)
            up = MagicMock()
            up.eq.return_value = up
            up._rows = [{**booking, **payload}]
            return up

        obj.update.side_effect = _update
        return obj

    async def fake_sb(builder):
        return MagicMock(data=getattr(builder, "_rows", []))

    # Patched onto the class, so these arrive as bound methods and take `self`.
    async def fake_notify_outcome(self_, bk, rf, clinic=None):
        calls["notified"] = ("outcome", rf)

    async def fake_notify_generic(self_, bk, refunded):
        calls["notified"] = ("generic", refunded)

    with patch("app.services.payment.supabase") as sb_mod, \
         patch("app.services.payment.sb", side_effect=fake_sb), \
         patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value=clinic(4))), \
         patch.object(PaymentService, "initiate_refund", new=AsyncMock(return_value=refund_result)), \
         patch.object(PaymentService, "notify_cancellation_outcome", new=fake_notify_outcome), \
         patch.object(PaymentService, "_notify_booking_cancelled", new=fake_notify_generic), \
         patch.object(PaymentService, "_log_payment_event", new=AsyncMock()):
        sb_mod.table.side_effect = table
        result = await svc.admin_cancel_confirmed_booking(
            "booking-1", clinic_id=CLINIC_ID, admin_notes="Cancelled by admin"
        )
    return result, calls


@pytest.mark.asyncio
async def test_admin_cancel_of_a_paid_booking_refunds_and_sends_the_receipt():
    ok = {"success": True, "refund_id": "rfnd_9", "amount_inr": 500.0, "is_late": False}
    result, calls = await _admin_cancel(_booking(48), ok)
    assert result["success"] is True
    assert result["refunded"] is True
    assert calls["notified"] == ("outcome", ok)


@pytest.mark.asyncio
async def test_admin_can_still_cancel_after_the_refund_window_has_closed():
    """This used to return a failure and leave the booking confirmed: the slot
    stayed blocked, and the patient was never told anything."""
    late = {"success": False, "is_late": True, "window_hours": 4,
            "reason": "refund_window_closed_need_4h_before_slot", "refund_id": "", "amount_inr": 500.0}
    result, calls = await _admin_cancel(_booking(1), late)
    assert result["success"] is True
    assert result["cancelled"] is True
    assert result["refunded"] is False
    assert {"status": "cancelled"} in calls["updates"]
    assert calls["notified"] == ("outcome", late)


@pytest.mark.asyncio
async def test_admin_cancel_after_a_gateway_failure_also_cancels_and_notifies():
    fail = {"success": False, "is_late": False, "reason": "razorpay_error: boom",
            "refund_id": "", "amount_inr": 500.0}
    result, calls = await _admin_cancel(_booking(48), fail)
    assert result["success"] is True
    assert result["refunded"] is False
    assert {"status": "cancelled"} in calls["updates"]
    assert calls["notified"][0] == "outcome"


@pytest.mark.asyncio
async def test_admin_cancel_of_an_unpaid_booking_is_unchanged():
    result, calls = await _admin_cancel(_booking(48, payment_id=None), None)
    assert result["success"] is True
    assert result["refunded"] is False
    assert {"status": "cancelled"} in calls["updates"]
    assert calls["notified"] == ("generic", False)


@pytest.mark.asyncio
async def test_an_anytime_window_never_says_within_0_hours():
    """With a 0-hour window, 'late' can only mean the slot already started."""
    sent = await _notify(
        {"success": False, "is_late": True, "window_hours": 0,
         "reason": "refund_window_closed_need_0h_before_slot", "refund_id": "", "amount_inr": 500.0}
    )
    body = sent["message"]
    assert "0 hours" not in body
    assert "after the appointment start time" in body
