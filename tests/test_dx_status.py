"""Patient-facing DX status (P9, DX-56/57; KRIYA-KETU §2–§3, ADR-DX-10) on real Postgres.

States follow the visit (registered -> processing -> held for dues -> sent; recollection), the text never
carries a result value, the payment link is reused for 10 minutes, and another phone sees nothing.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_dx_reports_e2e import _signed_visit, rep  # noqa: F401  (fixture)
from tests.test_dx_results_e2e import _as, _collect_receive, _visit, bench  # noqa: F401
from tests.test_dx_visits_e2e import dxw  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _status(w, phone=None):
    from app.services import dx_status
    phone = phone or _q(w["c"], "SELECT phone FROM patients WHERE id = %s", w["patient"])
    return asyncio.run(dx_status.get_patient_dx_status(w["clinic"], phone))


@pytest.fixture
def quiet(rep, monkeypatch):  # noqa: F811
    from app.services import dx

    async def no_notice(*_a, **_k):
        return True
    monkeypatch.setattr(dx, "notify_recollection", no_notice)
    return rep


def test_states_follow_the_visit_and_carry_no_values(quiet):
    w = quiet
    assert _status(w, "+910000000000") is None
    v = _visit(w, ["cbc"])
    st = _status(w)
    assert st["state"] == "registered" and st["accession_number"] == v["visit"]["accession_number"]
    _collect_receive(w, v)
    st = _status(w)
    assert st["state"] == "processing" and "collected at" in st["text"] and "expected by" in st["text"]
    # a later visit, signed with value 27 and unpaid: held for dues, with the balance, never the value
    v2, _ = _signed_visit(w, ("cbc",), values={"cbc": "27"})
    st = _status(w)
    assert st["state"] == "held_dues" and st["balance_paise"] == 35000 and "₹350" in st["text"]
    assert "27" not in st["text"] and "flag" not in str(st).lower()
    from app.services import dx_status
    assert "₹350" in dx_status.status_text(st, "hi") and "₹350" in dx_status.status_text(st, "te")


def test_recollection_state(quiet):
    w = quiet
    v = _visit(w, ["cbc"])
    _collect_receive(w, v)
    r = _as(w, "tech").call("POST", f"/admin/dx/samples/{v['samples'][0]['id']}/reject", w["clinic"],
                            {"reason_code": "HEMOLYSED", "recollect": True})
    assert r.status_code == 200, r.text
    st = _status(w)
    assert st["state"] == "recollect" and "collected again" in st["text"] and "no extra charge" in st["text"]


def test_payment_link_is_reused_for_ten_minutes(quiet, monkeypatch):
    from app.services import dx_billing, dx_status
    w = quiet
    v, _ = _signed_visit(w, ("cbc",))
    vid = v["visit"]["id"]
    bill = _as(w, "desk").call("POST", f"/admin/dx/visits/{vid}/bill", w["clinic"], {"issue": True})
    assert bill.status_code == 200, bill.text
    made = []

    async def fake_link(clinic_id, clinic, accession_id):
        made.append(accession_id)
        _q(w["c"], "UPDATE opd_invoices SET payment_link_url = 'https://pay.test/x', payment_link_amount_paise = "
                   "35000, payment_link_created_at = now() WHERE accession_id = %s", accession_id)
        return {"url": "https://pay.test/x", "amount_paise": 35000}
    monkeypatch.setattr(dx_billing, "payment_link", fake_link)
    first = asyncio.run(dx_status.dues_payment_link(w["clinic"], {"id": w["clinic"]}, vid))
    again = asyncio.run(dx_status.dues_payment_link(w["clinic"], {"id": w["clinic"]}, vid))
    later = asyncio.run(dx_status.dues_payment_link(w["clinic"], {"id": w["clinic"]}, vid,
                                                    now=datetime.now(timezone.utc) + timedelta(minutes=11)))
    assert (first["fresh"], again["fresh"], later["fresh"]) == (True, False, True) and len(made) == 2
    assert again["url"] == "https://pay.test/x" and again["amount_paise"] == 35000


def test_request_recollection_notifies_the_desk(quiet):
    from app.services import dx_status
    w = quiet
    assert asyncio.run(dx_status.request_recollection(w["clinic"], "+919000000001", "LAB-2026-000009", "home"))
    title = _q(w["c"], "SELECT title FROM admin_notifications WHERE clinic_id = %s ORDER BY created_at DESC LIMIT 1",
               w["clinic"])
    assert title == "Recollection: LAB-2026-000009 (home visit)"


# ── WhatsApp chat (DX-57, KRIYA-KETU §3, O-DX-14) ────────────────────────────

DX_CLINIC = {"id": "c1", "whatsapp_number": "+911111111111", "plan": "diagstream", "features": {"dx_enabled": True}}


def _chat(st, link=None, clinic=DX_CLINIC):
    from unittest.mock import AsyncMock, patch
    from app.services import dx_status
    from app.services.conversation import ConversationManager
    m = ConversationManager()
    with patch.object(m.whatsapp, "send_text", new_callable=AsyncMock) as text, \
            patch.object(m.whatsapp, "send_interactive_buttons", new_callable=AsyncMock) as buttons, \
            patch.object(dx_status, "get_patient_dx_status", AsyncMock(return_value=st)), \
            patch.object(dx_status, "dues_payment_link", AsyncMock(side_effect=link or RuntimeError("no bill"))):
        asyncio.run(m._send_dx_visit_status(clinic, "+919876543210", "en"))
    return text, buttons


def _st(state, **params):
    from app.services import dx_status
    st = {"state": state, "accession_id": "v1", "accession_number": "LAB-2026-000001",
          "params": {"visit": "LAB-2026-000001", **params}}
    st["text"] = dx_status.status_text(st)
    return st


def test_chat_held_report_carries_the_payment_link_and_no_send_button():
    from unittest.mock import AsyncMock
    text, buttons = _chat(_st("held_dues", amount="₹350"),
                          link=AsyncMock(return_value={"url": "https://pay.test/l", "amount_paise": 35000}))
    body = text.call_args.args[2]
    assert "₹350 is pending" in body and "https://pay.test/l" in body and not buttons.called
    # no issued bill / gateway down: the status still goes, without a link
    text, _ = _chat(_st("held_dues", amount="₹350"))
    assert "₹350" in text.call_args.args[2] and "http" not in text.call_args.args[2]


def test_chat_recollection_offers_home_or_centre_buttons():
    _, buttons = _chat(_st("recollect", reason="hemolysed"))
    ids = [b["id"] for b in buttons.call_args.kwargs["buttons"]]
    assert ids == ["dx_recollect_home", "dx_recollect_centre"]
    assert not any("send" in b["title"].lower() for b in buttons.call_args.kwargs["buttons"])


def test_chat_is_silent_without_the_module_or_a_visit():
    text, buttons = _chat(_st("processing", collected="10:30 AM", expected="6:00 PM"),
                          clinic={**DX_CLINIC, "features": {}})
    assert not text.called and not buttons.called
    text, buttons = _chat(None)
    assert not text.called and not buttons.called


def test_recollection_button_rereads_the_phone():
    from unittest.mock import AsyncMock, patch
    from app.services import dx_status
    from app.services.conversation import ConversationManager
    m = ConversationManager()
    for st, asked in ((_st("recollect", reason="clotted"), True), (_st("sent", sent="today"), False)):
        with patch.object(m.whatsapp, "send_text", new_callable=AsyncMock) as text, \
                patch.object(dx_status, "get_patient_dx_status", AsyncMock(return_value=st)), \
                patch.object(dx_status, "request_recollection", AsyncMock(return_value=True)) as req:
            asyncio.run(m._handle_dx_recollection(DX_CLINIC, "+919876543210", "home", "en"))
        assert req.called is asked
        assert ("call you" in text.call_args.args[2]) is asked
