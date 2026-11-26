"""The standalone-pharmacy block in the conversation pipeline (RXR-ADR-5/6, VERIFICATION D14).

A `pharmacy`-plan clinic's every message goes to the pharmacy front door (never a booking flow); a prescription photo
is accepted only in that clinic's "send prescription" state; every other plan never reaches rx_retail and a photo
there still gets the unsupported-media reply.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services import rx_retail
from app.services.conversation import ConversationManager


def _run(plan, state, message="", mtype="text", data=None):
    clinic = {"id": "c1", "name": "Sai Medicals", "config": {}, "plan": plan, "account_type": "tenant",
              "features": {"pharmacy_enabled": True}, "whatsapp_number": "+919000000000"}
    session = {"state": state, "context": {}, "last_processed_message_id": None, "booking_context_expires_at": None}
    handle = AsyncMock(return_value=True)
    m = ConversationManager()
    m.whatsapp = AsyncMock()
    with patch.object(rx_retail, "handle", handle), \
            patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=session)), \
            patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value={"id": "p", "language": "en"})), \
            patch("app.services.conversation.update_conversation", new=AsyncMock()), \
            patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="book_appointment")), \
            patch("app.services.leads.record_label", new=lambda *a, **k: None):
        try:
            asyncio.run(m._handle_message_locked(clinic=clinic, phone="+919800000001", message=message, message_type=mtype,
                                                 message_id="wamid.R1", interactive_data=data))
        except Exception:                                    # non-pharmacy pipelines past the block are not under test
            if plan == "pharmacy":
                raise
    return handle, m


@pytest.mark.parametrize("text", ["book appointment", "show doctors", "hi"])
def test_pharmacy_plan_text_goes_to_the_pharmacy_front_door(text):
    handle, m = _run("pharmacy", "main_menu", text)
    handle.assert_awaited_once()
    m.whatsapp.send_interactive_buttons.assert_not_awaited()      # no "already booking" / booking prompt


def test_photo_only_in_the_send_prescription_state():
    handle, m = _run("pharmacy", "rx_upload_rx", mtype="image", data={"type": "image", "media_id": "M1"})
    handle.assert_awaited_once()
    assert handle.await_args.args[5] == "image"
    handle, m = _run("pharmacy", "main_menu", mtype="image", data={"type": "image", "media_id": "M1"})
    handle.assert_not_awaited()                                    # elsewhere a photo keeps the unsupported reply
    m.whatsapp.send_text.assert_awaited()


@pytest.mark.parametrize("plan", ["polyclinic", "soloclinic", "diagstream"])
def test_other_plans_never_reach_the_pharmacy_block(plan):
    handle, _ = _run(plan, "main_menu", "book appointment")
    handle.assert_not_awaited()
    handle, _ = _run(plan, "rx_upload_rx", mtype="image", data={"type": "image", "media_id": "M1"})
    handle.assert_not_awaited()


def test_webhook_model_carries_the_image_id():
    from app.models.message import WhatsAppMessage
    msg = WhatsAppMessage.model_validate({"from": "919800000001", "id": "wamid.X", "timestamp": "1", "type": "image",
                                          "image": {"id": "MEDIA9", "mime_type": "image/jpeg", "caption": "rx"}})
    assert msg.image["id"] == "MEDIA9"
