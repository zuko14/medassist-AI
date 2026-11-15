"""Voice receptionist × Diagnostic OS (P9, DX-56; KRIYA-KETU §2): status only, never values.

Dialog: a DX status is spoken before the lab_reports flow; held for dues -> one payment link;
recollection -> staff callback; "sent" falls through to the existing resend offer; a clinic
without the module (tools without dx_visit_status) keeps the old behaviour.
"""

from tests.voice.test_dialog import Conv, FakeTools


class DxTools(FakeTools):
    def __init__(self, dx=None, link=None, **kw):
        super().__init__(**kw)
        self.dx, self.link = dx, link or {"status": "sent"}
        self.callbacks = []

    async def dx_visit_status(self):
        self.calls.append(("dx_visit_status",))
        return self.dx

    async def dx_send_payment_link(self, status):
        self.calls.append(("dx_send_payment_link", status["accession_id"]))
        return self.link

    async def create_callback(self, reason):
        self.callbacks.append(reason)
        return {"ok": True}


def _dx(state, text):
    return {"state": state, "text": text, "accession_id": "v1", "accession_number": "LAB-2026-000001"}


def test_processing_status_is_spoken():
    t = DxTools(dx=_dx("processing", "Your sample was collected at 10:30 AM; results are expected by 6:00 PM."))
    r = Conv(t, lang="en-IN").say("What is the status of my blood test report?")
    assert "collected at 10:30 AM" in r and "6:00 PM" in r
    assert not any(c[0] == "resend_report" for c in t.calls)


def test_held_for_dues_sends_one_payment_link():
    t = DxTools(dx=_dx("held_dues", "Your test report is ready, but a payment of ₹350 is pending."))
    r = Conv(t, lang="en-IN").say("Is my lab report ready?")
    assert "₹350 is pending" in r and "sent the payment link" in r
    assert ("dx_send_payment_link", "v1") in t.calls
    t2 = DxTools(dx=_dx("held_dues", "pending"), link={"status": "failed"})
    r = Conv(t2, lang="en-IN").say("Is my lab report ready?")
    assert "couldn't send the payment link" in r and t2.callbacks == ["dx_payment_link_failed"]


def test_recollection_creates_a_callback():
    t = DxTools(dx=_dx("recollect", "Your sample could not be tested (hemolysed) and must be collected again."))
    r = Conv(t, lang="en-IN").say("What about my report?")
    assert "collected again" in r and "arrange a time" in r and t.callbacks == ["dx_recollection"]


def test_sent_falls_through_to_the_resend_offer_and_no_dx_keeps_old_flow():
    t = DxTools(dx=_dx("sent", "sent"), reports=[{"id": "r1", "test_name": "CBC", "status": "ready"}])
    r = Conv(t, lang="en-IN").say("send my report on WhatsApp")
    assert "sent the report" in r and ("resend_report", "r1") in t.calls
    t = DxTools(dx=None, reports=[])
    assert "don't see any recent reports" in Conv(t, lang="en-IN").say("send my report on WhatsApp")


def test_kriya_tools_dx_status_is_off_without_the_module():
    import asyncio
    from app.voice.tools import CallContext, KriyaTools
    ctx = CallContext(call_id="c", call_ref="r", clinic={"id": "x", "dx_state": None}, branch_id=None,
                      caller_phone="+919999999999")
    assert asyncio.run(KriyaTools(ctx).dx_visit_status()) is None
