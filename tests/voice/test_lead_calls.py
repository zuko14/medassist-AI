"""Lead follow-up calls: the clinic's services pitch, the WhatsApp booking line,
bulk import with attested consent, and one-call-at-a-time dispatch."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.voice import outbound
from app.voice import responses as R
from app.voice.dates import IST

from .test_dialog import TODAY, Conv, FakeTools

PITCH = {"te": "మా దగ్గర 24 గంటల ఫార్మసీ ఉంది.", "en": "We have a 24-hour pharmacy and free home sample collection."}


def _lead_conv(lang, whatsapp="+919876543210"):
    c = Conv(FakeTools(), lang=lang)
    c.engine.clinic.update({"pitch": PITCH, "whatsapp": whatsapp})
    return c, c.text(c.engine.greeting(c.state, outbound=True, interest="cardiology"))


def test_greeting_speaks_the_pitch_in_the_callers_language_only():
    _, en = _lead_conv("en-IN")
    assert "You recently asked us about cardiology. We have a 24-hour pharmacy" in en
    assert en.endswith("Can I help you book an appointment?")
    _, te = _lead_conv("te-IN")
    assert PITCH["te"] in te and "pharmacy" not in te
    _, hi = _lead_conv("hi-IN")               # no Hindi pitch written: nothing English read out
    assert "pharmacy" not in hi and "{" not in hi


def test_lead_who_declines_hears_the_whatsapp_number_then_anything_else():
    c, _ = _lead_conv("en-IN")
    reply = c.say("No")
    assert "No problem. Whenever you need us, you can also book easily on our WhatsApp number, 9 8 7 6 5, 4 3 2 1 0." in reply
    assert "anything else" in reply
    assert c.state["expect"] == "anything_else"


def test_decline_without_a_whatsapp_number_skips_that_sentence():
    c, _ = _lead_conv("en-IN", whatsapp=None)
    reply = c.say("No")
    assert reply.startswith("No problem.") and "WhatsApp" not in reply


def test_inbound_decline_is_unchanged():
    c = Conv(FakeTools(), lang="en-IN")
    c.engine.clinic.update({"whatsapp": "+919876543210"})
    c.state.update({"expect": "offer", "offer": {"wf": "BOOKING", "slots": {}}})
    reply = c.say("No")
    assert "WhatsApp" not in reply and "anything else" in reply


def test_speak_phone_groups_digits():
    assert R.speak_phone("+91 98765-43210") == "9 8 7 6 5, 4 3 2 1 0"
    assert R.speak_phone("108") == "1 0 8"


def test_new_templates_render_without_placeholders():
    for key in ("lead_declined", "wa_book_sentence", "greeting_outbound"):
        for lang in ("te-IN", "hi-IN", "en-IN"):
            out = R.realize(key, lang, TODAY, {}, hospital="H", assistant="K", wa_number="+919876543210", pitch=PITCH)
            assert out and "{" not in out, (key, lang, out)


# ---- eligibility ----

def test_attestation_allows_strangers_but_never_overrides_an_opt_out():
    assert outbound._blocked(None, attested=False) == "not_a_contact_of_this_clinic"
    assert outbound._blocked(None, attested=True) is None
    assert outbound._blocked({"opted_in": False}, attested=True) == "do_not_contact"
    assert outbound._blocked({"data_consent_declined_at": "2026-10-01"}, attested=True) == "do_not_contact"
    assert outbound._blocked({"opted_in": True}, attested=False) is None


# ---- bulk import ----

CLINIC = {"id": "c1", "features": {"ai_receptionist": True}}


def _res(data):
    return SimpleNamespace(data=data)


@pytest.mark.asyncio
async def test_bulk_import_skips_with_reasons_and_records_consent():
    week_old = (datetime.now(IST) - timedelta(days=3)).isoformat()
    calls = [
        _res([{"phone": "+919000000002", "opted_in": False}]),                         # patients
        _res([{"patient_phone": "+919000000003", "status": "queued", "created_at": week_old},
              {"patient_phone": "+919000000004", "status": "completed", "created_at": week_old}]),  # jobs
        _res([{"id": "j1"}]),                                                           # insert
    ]
    sb = AsyncMock(side_effect=calls)
    contacts = [{"phone": "9000000001", "interest": "knee pain"}, {"phone": "+91 90000 00001"},
                {"phone": "9000000002"}, {"phone": "9000000003"}, {"phone": "9000000004"}, {"phone": "12"}]
    with patch.object(outbound, "ai_receptionist_enabled", return_value=True), \
         patch.object(outbound, "sb", new=sb), patch.object(outbound, "supabase", new=MagicMock()) as fake:
        r = await outbound.enqueue_bulk(CLINIC, contacts, {"source": "Health camp"}, "admin1")
    assert r["queued"] == 1
    reasons = {s["phone"]: s["reason"] for s in r["skipped"]}
    assert reasons == {"+919000000001": "Listed twice in this import", "+919000000002": "Opted out — never called",
                       "+919000000003": "A call is already queued", "+919000000004": "Called in the last 7 days",
                       "12": "Not a valid phone number"}
    rows = fake.table.return_value.insert.call_args[0][0]
    assert [r_["patient_phone"] for r_ in rows] == ["+919000000001"]
    assert rows[0]["clinic_id"] == "c1" and rows[0]["context"]["interest"] == "knee pain"
    assert rows[0]["context"]["consent"]["source"] == "Health camp"
    assert rows[0]["context"]["consent"]["attested_by"] == "admin1"


@pytest.mark.asyncio
async def test_bulk_import_without_consent_queues_only_existing_contacts():
    sb = AsyncMock(side_effect=[_res([{"phone": "+919000000001", "opted_in": True}]), _res([]), _res([{"id": "j"}])])
    with patch.object(outbound, "ai_receptionist_enabled", return_value=True), \
         patch.object(outbound, "sb", new=sb), patch.object(outbound, "supabase", new=MagicMock()):
        r = await outbound.enqueue_bulk(CLINIC, [{"phone": "9000000001"}, {"phone": "9000000009"}], None, "admin1")
    assert r["queued"] == 1
    assert r["skipped"] == [{"phone": "+919000000009", "reason": "Never contacted the clinic"}]


# ---- dispatch: one by one ----

@pytest.mark.asyncio
async def test_dispatch_waits_while_the_clinic_has_a_call_in_flight():
    job = {"id": "j1", "clinic_id": "c1", "patient_phone": "+919000000001", "attempts": 0, "context": {}}
    sb = AsyncMock(side_effect=[_res([job]), _res([])])
    dial = AsyncMock()
    with patch.object(outbound, "get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
         patch.object(outbound, "ai_receptionist_enabled", return_value=True), \
         patch.object(outbound, "next_window_start", return_value=None), \
         patch.object(outbound, "_today_count", new=AsyncMock(return_value=0)), \
         patch.object(outbound, "_in_flight", new=AsyncMock(return_value=1)), \
         patch.object(outbound, "_dial", new=dial), \
         patch.object(outbound, "sb", new=sb), patch.object(outbound, "supabase", new=MagicMock()) as fake:
        placed = await outbound.dispatch_due()
    assert placed == 0
    dial.assert_not_called()
    update = fake.table.return_value.update.call_args[0][0]
    assert set(update) == {"next_attempt_at"}           # pushed back, still queued, attempts untouched


# ---- routes ----

from tests.voice.test_voice_routes import ADMIN, CID, VIEWER, client  # noqa: E402


def test_bulk_route_needs_a_source_with_consent_and_manage_permission():
    c, p = client(ADMIN)
    try:
        r = c.post(f"/admin/voice/outbound/bulk?clinic_id={CID}",
                   json={"contacts": [{"phone": "9000000001"}], "consent_attested": True, "source": " "})
        assert r.status_code == 422
        too_many = c.post(f"/admin/voice/outbound/bulk?clinic_id={CID}",
                          json={"contacts": [{"phone": "9000000001"}] * 501})
        assert too_many.status_code == 422
    finally:
        p.stop()
    c, p = client(VIEWER)
    try:
        assert c.post(f"/admin/voice/outbound/bulk?clinic_id={CID}",
                      json={"contacts": [{"phone": "9000000001"}]}).status_code == 403
    finally:
        p.stop()


def test_bulk_route_passes_consent_and_audits():
    c, p = client(ADMIN)
    try:
        with patch("app.routers.voice_admin.outbound.enqueue_bulk",
                   new=AsyncMock(return_value={"queued": 1, "skipped": [], "truncated": 0})) as eb, \
             patch("app.routers.voice_admin.log_admin_action", new=AsyncMock()) as audit:
            r = c.post(f"/admin/voice/outbound/bulk?clinic_id={CID}",
                       json={"contacts": [{"phone": "9000000001", "interest": "ECG"}],
                             "consent_attested": True, "source": "Health  camp"})
        assert r.status_code == 200 and r.json()["queued"] == 1
        assert eb.await_args.args[2] == {"source": "Health camp"}
        assert audit.await_args.args[1] == "voice_outbound_bulk_enqueue"
    finally:
        p.stop()


def test_settings_accept_pitch_and_concurrency_and_bound_them():
    c, p = client(ADMIN)
    base = {"primary_language": "te-IN", "assistant_name": "Kriya", "pace": 1.0,
            "reception_hours": {"start": "09:00", "end": "20:00", "days": "Mon,Tue"}}
    try:
        bad = c.put(f"/admin/voice/settings?clinic_id={CID}", json={**base, "outbound": {"concurrent": 9}})
        long = c.put(f"/admin/voice/settings?clinic_id={CID}", json={**base, "outbound": {"pitch": {"en": "x" * 301}}})
        assert bad.status_code == 422 and long.status_code == 422
        with patch("app.routers.voice_admin.sb", new=AsyncMock(return_value=MagicMock(data=[{"config": {}}]))) as sb, \
             patch("app.routers.voice_admin.invalidate_tenant_cache"), \
             patch("app.routers.voice_admin.log_admin_action", new=AsyncMock()):
            ok = c.put(f"/admin/voice/settings?clinic_id={CID}",
                       json={**base, "outbound": {"concurrent": 2, "pitch": {"te": "మా దగ్గర ఫార్మసీ ఉంది."}}})
        assert ok.status_code == 200
    finally:
        p.stop()


@pytest.mark.asyncio
async def test_exotel_rejection_records_the_status_and_masks_the_number_in_logs(caplog):
    import httpx
    job = {"id": "j1", "clinic_id": "c1", "patient_phone": "+919000000001", "attempts": 0, "context": {}}
    resp = httpx.Response(403, text='{"RestException":{"Message":"Call to +919000000001 not allowed"}}',
                          request=httpx.Request("POST", "https://api.exotel.com/x"))
    sb = AsyncMock(side_effect=[_res([job]), _res([{"exophone": "+914000000000"}]), _res([{"id": "j1"}])])
    finished = AsyncMock()
    with patch.object(outbound, "get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
         patch.object(outbound, "ai_receptionist_enabled", return_value=True), \
         patch.object(outbound, "next_window_start", return_value=None), \
         patch.object(outbound, "_today_count", new=AsyncMock(return_value=0)), \
         patch.object(outbound, "_in_flight", new=AsyncMock(return_value=0)), \
         patch.object(outbound, "_eligible", new=AsyncMock(return_value=None)), \
         patch.object(outbound.store, "create_call", new=AsyncMock(return_value={"id": "call1", "clinic_id": "c1"})), \
         patch.object(outbound.store, "update_call", new=AsyncMock()), \
         patch.object(outbound, "_dial", new=AsyncMock(side_effect=httpx.HTTPStatusError("403", request=resp.request, response=resp))), \
         patch.object(outbound, "job_finished", new=finished), \
         patch.object(outbound, "sb", new=sb), patch.object(outbound, "supabase", new=MagicMock()):
        assert await outbound.dispatch_due() == 0
    assert finished.await_args.kwargs["error"] == "exotel_http_403"
    assert "+919000000001" not in caplog.text and "XXXXXX0001" in caplog.text
