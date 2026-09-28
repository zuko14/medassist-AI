"""WhatsApp Leads (migration 092): interest capture on the live message path,
the panel list, and staff follow-up inside Meta's 24-hour window."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import AdminUser, verify_credentials
from app.services import leads
from app.services.conversation import ConversationManager

CLINIC_ID = "11111111-1111-1111-1111-111111111111"
OTHER_CLINIC = "99999999-9999-9999-9999-999999999999"
PHONE = "+919876543210"
CLINIC = {"id": CLINIC_ID, "name": "Apollo Clinic", "whatsapp_number": "+919999999999", "config": {}}

ADMIN = AdminUser(username="admin", role="clinic_admin", clinic_id=CLINIC_ID, user_id="u1")
STAFF = AdminUser(username="desk", role="staff", clinic_id=CLINIC_ID, user_id="u2")
STAFF_OK = AdminUser(username="desk2", role="staff", clinic_id=CLINIC_ID, user_id="u3",
                     permissions=["LEADS_MANAGE"])

client = TestClient(app)


@pytest.fixture
def as_user():
    def _set(user):
        app.dependency_overrides[verify_credentials] = lambda: user
    yield _set
    app.dependency_overrides.pop(verify_credentials, None)


# ═══════ what counts as interest ═══════


@pytest.mark.parametrize("message_type, intent, data, expected", [
    ("text", "book_appointment", None, ("book_appointment", None)),
    ("text", "find_tests", None, ("lab_tests", None)),
    ("text", "human_escalation", None, ("talk_to_staff", None)),
    ("text", "greeting", None, None),            # a bare "hi" is a contact, not an interest
    ("text", "unknown", None, None),
    ("text", "opt_out", None, None),
    ("text", "data_deletion_request", None, None),
    ("interactive", "button_click", {"id": "menu_book"}, ("book_appointment", None)),
    ("interactive", "button_click", {"id": "menu_human"}, ("talk_to_staff", None)),
    ("interactive", "button_click", {"id": "trtcall_abc"}, ("callback_request", None)),
    ("interactive", "button_click", {"id": "labsvc_radiology"}, ("lab_tests", None)),
    ("interactive", "button_click", {"id": "dept_general_medicine"}, (None, "General Medicine")),
    ("interactive", "button_click", {"id": "slot_10:00"}, None),   # mid-flow tap: not re-counted
    ("interactive", "button_click", {"id": "confirm_yes"}, None),
    # Template quick reply: mapped when known, else falls back to the text intent.
    ("button", "view_services", {"id": "book_lab_test"}, ("lab_tests", None)),
    ("button", "view_services", {"id": "checkin_ok"}, ("services", None)),
    ("image", None, None, None),
])
def test_classify_interest(message_type, intent, data, expected):
    assert leads.classify_interest(message_type, intent, data) == expected


def test_every_label_is_short_enough_for_the_intent_column():
    assert all(len(label) <= 50 for label in leads.INTEREST_LABELS)


@pytest.mark.asyncio
async def test_record_interest_writes_one_label_event():
    log = AsyncMock(return_value=True)
    with patch("app.services.leads.log_analytics_event", log):
        leads.record_interest(CLINIC_ID, PHONE, "interactive", "button_click", {"id": "dept_cardiology"})
        await asyncio.sleep(0)
    log.assert_awaited_once_with(CLINIC_ID, PHONE, "lead_interest", intent=None, department="Cardiology")


@pytest.mark.asyncio
async def test_record_interest_ignores_greeting():
    log = AsyncMock()
    with patch("app.services.leads.log_analytics_event", log):
        leads.record_interest(CLINIC_ID, PHONE, "text", "greeting", None)
        await asyncio.sleep(0)
    log.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_interest_never_raises_into_the_message_path():
    with patch("app.services.leads.log_analytics_event", AsyncMock(side_effect=RuntimeError("db down"))):
        leads.record_interest(CLINIC_ID, PHONE, "text", "book_appointment", None)  # must not raise
        await asyncio.sleep(0)
    with patch("app.services.leads.spawn_background_task", side_effect=RuntimeError("no loop")):
        leads.record_interest(CLINIC_ID, PHONE, "text", "book_appointment", None)  # must not raise


# ═══════ the hook on the live message path ═══════


def _hook_patches(intent):
    session = {"state": "main_menu", "context": {}, "last_processed_message_id": None,
               "booking_context_expires_at": None}
    patient = {"id": "p1", "phone": PHONE, "language": "en", "data_consent": True}
    return [
        patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=session)),
        patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value=patient)),
        patch("app.services.conversation.update_conversation", new=AsyncMock()),
        patch("app.services.conversation.detect_intent", new=AsyncMock(return_value=intent)),
        patch("app.services.conversation.get_lang", new=AsyncMock(return_value="en")),
    ]


@pytest.mark.asyncio
async def test_text_message_records_interest_then_replies_normally():
    manager = ConversationManager()
    manager.whatsapp = MagicMock(send_text=AsyncMock(return_value=True))
    record = MagicMock()
    patches = _hook_patches("book_appointment")
    for p in patches:
        p.start()
    try:
        with patch("app.services.conversation.leads.record_interest", record), \
             patch.object(manager, "_process_state", new=AsyncMock()) as process:
            await manager._handle_message_locked(CLINIC, PHONE, "i want to see a doctor", "text", "wamid.1")
    finally:
        for p in patches:
            p.stop()
    record.assert_called_once_with(CLINIC_ID, PHONE, "text", "book_appointment", None)
    process.assert_awaited_once()   # the reply flow is unchanged


@pytest.mark.asyncio
async def test_menu_tap_is_recorded_before_the_menu_early_return():
    manager = ConversationManager()
    manager.whatsapp = MagicMock(send_text=AsyncMock(return_value=True))
    record = MagicMock()
    patches = _hook_patches("button_click")
    for p in patches:
        p.start()
    try:
        with patch("app.services.conversation.leads.record_interest", record), \
             patch.object(manager, "_handle_human_escalation", new=AsyncMock()) as escalate:
            await manager._handle_message_locked(
                CLINIC, PHONE, "Talk to Staff", "interactive", "wamid.2", {"id": "menu_human"})
    finally:
        for p in patches:
            p.stop()
    record.assert_called_once_with(CLINIC_ID, PHONE, "interactive", "button_click", {"id": "menu_human"})
    escalate.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_broken_recorder_cannot_stop_the_reply():
    manager = ConversationManager()
    manager.whatsapp = MagicMock(send_text=AsyncMock(return_value=True))
    patches = _hook_patches("book_appointment")
    for p in patches:
        p.start()
    try:
        with patch("app.services.leads.classify_interest", side_effect=KeyError("boom")), \
             patch.object(manager, "_process_state", new=AsyncMock()) as process:
            await manager._handle_message_locked(CLINIC, PHONE, "book", "text", "wamid.3")
    finally:
        for p in patches:
            p.stop()
    process.assert_awaited_once()


# ═══════ list ═══════


@pytest.mark.asyncio
async def test_list_leads_rejects_bad_filters_before_the_database():
    rpc = MagicMock()
    with patch("app.services.leads.supabase", MagicMock(rpc=rpc)):
        for kwargs in ({"segment": "everyone"}, {"interest": "'; drop table"}):
            with pytest.raises(leads.LeadError) as e:
                await leads.list_leads(CLINIC_ID, **kwargs)
            assert e.value.status_code == 400
        with pytest.raises(leads.LeadError):
            await leads.list_leads("default")
    rpc.assert_not_called()


@pytest.mark.asyncio
async def test_list_leads_passes_clamped_params_and_validates_the_shape():
    rpc = MagicMock()
    with patch("app.services.leads.supabase", MagicMock(rpc=rpc)), \
         patch("app.services.leads.sb", new=AsyncMock(return_value=MagicMock(data={"rows": [], "total": 0}))):
        out = await leads.list_leads(CLINIC_ID, search="  ravi  ", days=99999, limit=999, offset=-5)
    assert out == {"rows": [], "total": 0}
    name, params = rpc.call_args.args
    assert name == "admin_whatsapp_leads"
    assert params == {"p_clinic_id": CLINIC_ID, "p_segment": "all", "p_interest": None, "p_search": "ravi",
                      "p_days": 3650, "p_limit": 100, "p_offset": 0}

    with patch("app.services.leads.supabase", MagicMock()), \
         patch("app.services.leads.sb", new=AsyncMock(return_value=MagicMock(data=None))):
        with pytest.raises(RuntimeError):
            await leads.list_leads(CLINIC_ID)


@pytest.mark.asyncio
async def test_mid_booking_uses_the_state_machines_own_definition():
    rows = [{"phone": "1", "state": "selecting_slot"}, {"phone": "2", "state": "main_menu"},
            {"phone": "3", "state": "awaiting_payment"}, {"phone": "4", "state": None}, {"phone": "5"}]
    with patch("app.services.leads.supabase", MagicMock()),          patch("app.services.leads.sb", new=AsyncMock(return_value=MagicMock(data={"rows": rows, "total": 5}))):
        out = await leads.list_leads(CLINIC_ID)
    assert [r["mid_booking"] for r in out["rows"]] == [True, False, False, False, False]
    assert all("state" not in r for r in out["rows"])


# ═══════ staff follow-up ═══════


def _open(hours=5):
    return {"session_expires_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()}


async def _send(patient, conversation, sent=True, text="Hello from Apollo"):
    send = AsyncMock(return_value=sent)
    with patch("app.services.leads.get_patient_by_phone", new=AsyncMock(return_value=patient)), \
         patch("app.services.leads.get_conversation", new=AsyncMock(return_value=conversation)), \
         patch("app.services.whatsapp.whatsapp_service.send_text", send):
        await leads.send_lead_message(CLINIC, PHONE, text)
    return send


@pytest.mark.asyncio
async def test_sends_inside_the_window_as_admin_lead():
    send = await _send({"phone": PHONE, "opted_in": True, "data_consent": None}, _open())
    send.assert_awaited_once_with(CLINIC, PHONE, "Hello from Apollo", _source="admin_lead")


@pytest.mark.asyncio
@pytest.mark.parametrize("patient, conversation, sent, text, status", [
    (None, _open(), True, "hi", 404),                                                     # never messaged THIS clinic
    ({"opted_in": True}, None, True, "hi", 409),                                          # erased: conversation purged
    ({"opted_in": False}, _open(), True, "hi", 409),                                      # sent STOP
    ({"opted_in": True, "data_consent": False}, _open(), True, "hi", 409),                # declined consent
    ({"opted_in": True}, _open(hours=-1), True, "hi", 409),                               # window closed
    ({"opted_in": True}, None, True, "hi", 409),                                          # no conversation row
    ({"opted_in": True}, _open(), False, "hi", 502),                                      # Meta refused
    ({"opted_in": True}, _open(), True, "   ", 400),
    ({"opted_in": True}, _open(), True, "x" * 1001, 400),
])
async def test_send_refusals(patient, conversation, sent, text, status):
    with pytest.raises(leads.LeadError) as e:
        await _send(patient, conversation, sent, text)
    assert e.value.status_code == status


# ═══════ routes: permission + tenant ═══════


def test_staff_without_permission_cannot_see_leads(as_user):
    as_user(STAFF)
    assert client.get("/admin/leads").status_code == 403
    assert client.post("/admin/leads/message", json={"phone": PHONE, "message": "hi"}).status_code == 403


def test_cannot_read_another_clinics_leads(as_user):
    as_user(ADMIN)
    with patch("app.services.leads.list_leads", new=AsyncMock()) as listing:
        res = client.get(f"/admin/leads?clinic_id={OTHER_CLINIC}")
    assert res.status_code == 403
    listing.assert_not_awaited()


def test_list_route_scopes_to_the_admins_clinic(as_user):
    as_user(STAFF_OK)
    payload = {"rows": [], "total": 0, "summary": {}, "interests": [], "departments": []}
    with patch("app.services.leads.list_leads", new=AsyncMock(return_value=payload)) as listing:
        res = client.get("/admin/leads?segment=hot&days=7&q=ravi")
    assert res.status_code == 200
    assert listing.await_args.args[0] == CLINIC_ID
    assert listing.await_args.kwargs["segment"] == "hot"
    assert "book_appointment" in res.json()["interest_labels"]


def test_list_route_hides_internal_errors(as_user):
    as_user(ADMIN)
    with patch("app.services.leads.list_leads", new=AsyncMock(side_effect=RuntimeError("secret dsn"))):
        res = client.get("/admin/leads")
    assert res.status_code == 500
    assert "secret" not in res.text


def test_message_route_sends_and_audits_without_the_body(as_user):
    as_user(ADMIN)
    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
         patch("app.services.leads.send_lead_message", new=AsyncMock()) as send, \
         patch("app.routers.admin.log_admin_action", new=AsyncMock()) as audit:
        res = client.post("/admin/leads/message", json={"phone": "98765 43210", "message": "Private note"})
    assert res.status_code == 200
    send.assert_awaited_once_with(CLINIC, PHONE, "Private note")
    details = audit.await_args.kwargs["details"]
    assert details == {"clinic_id": CLINIC_ID, "chars": 12}
    assert "Private note" not in str(audit.await_args)


def test_message_route_maps_refusals(as_user):
    as_user(ADMIN)
    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
         patch("app.services.leads.send_lead_message",
               new=AsyncMock(side_effect=leads.LeadError(409, "window closed"))):
        res = client.post("/admin/leads/message", json={"phone": PHONE, "message": "hi"})
    assert res.status_code == 409
    assert res.json()["detail"] == "window closed"
    # A masked do-not-contact number is not a phone number.
    res = client.post("/admin/leads/message", json={"phone": "+91•••••3210", "message": "hi"})
    assert res.status_code == 400
