"""2026-08-25 outage: every lab report failed against a PENDING template.

Three defects turned one fixable Meta state into a silent, permanent data loss:
  1. send_template probed a hardcoded alias list, so the reported error came
     from a template nobody had registered — and an appointment reminder could
     fall through to the lab report template.
  2. The real Meta error was dropped; the failure queue showed a guess.
  3. "template may not be approved" was classified permanent, so reports were
     burnt as failed and never re-delivered once approval landed.
"""

import httpx
import pytest

from app.services.whatsapp import WhatsAppService, _describe_meta_error

# What Meta actually returns for a template that exists but is not APPROVED.
TEMPLATE_NOT_APPROVED = {
    "error": {
        "message": "(#132001) Template name does not exist in the translation",
        "type": "OAuthException",
        "code": 132001,
        "error_data": {
            "messaging_product": "whatsapp",
            "details": "template name (lab_report_delivery) does not exist in en",
        },
        "fbtrace_id": "AbCdEfGhIjK",
    }
}

CLINIC = {"id": "c1", "config": {"meta_access_token": "t", "meta_phone_number_id": "p"}}


async def _noop(*a, **k):
    return None


async def _true(*a, **k):
    return True


def _fake_client(monkeypatch, sent, status=400, body=TEMPLATE_NOT_APPROVED):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            sent.append(kw.get("json"))
            return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())


def test_describe_meta_error_names_code_and_fbtrace():
    err = httpx.HTTPStatusError(
        "400",
        request=httpx.Request("POST", "https://x"),
        response=httpx.Response(400, json=TEMPLATE_NOT_APPROVED),
    )
    detail = _describe_meta_error(err)
    assert "132001" in detail
    assert "does not exist in en" in detail, "Meta's own details must survive"
    assert "AbCdEfGhIjK" in detail, "fbtrace_id is what Meta support asks for"


@pytest.mark.asyncio
async def test_send_template_never_substitutes_another_template(monkeypatch):
    """A rejected appointment reminder must not go out as a lab report."""
    sent = []
    _fake_client(monkeypatch, sent)
    monkeypatch.setattr(WhatsAppService, "_log_to_ledger", _noop)

    ok = await WhatsAppService().send_template(
        CLINIC, "919999999999", template_name="appointment_reminder"
    )

    assert ok is False
    names = {p["template"]["name"] for p in sent}
    assert names == {"appointment_reminder"}, f"sent other templates: {names}"


@pytest.mark.asyncio
async def test_send_template_reports_metas_real_reason(monkeypatch):
    sent = []
    _fake_client(monkeypatch, sent)
    monkeypatch.setattr(WhatsAppService, "_log_to_ledger", _noop)
    capture = {}

    ok = await WhatsAppService().send_template(
        CLINIC, "919999999999", template_name="lab_report_delivery", _capture=capture
    )

    assert ok is False
    assert "132001" in capture["error"]
    assert "AbCdEfGhIjK" in capture["error"]


@pytest.mark.asyncio
async def test_missing_credentials_never_reads_as_a_successful_send(monkeypatch):
    """_make_request returning {} made send_text log 'Sent' for nothing sent."""
    monkeypatch.setattr(WhatsAppService, "_can_send_freeform", _true)
    monkeypatch.setattr(WhatsAppService, "_log_to_ledger", _noop)

    ok = await WhatsAppService().send_text({"id": "c1", "config": {}}, "919999999999", "hi")

    assert ok is False


def test_unapproved_template_is_retryable_not_permanent():
    """Approval flips to APPROVED on its own; a burnt report never re-delivers."""
    import inspect

    from app.services.lab_reports import LabReportService

    source = inspect.getsource(LabReportService.upload_and_send)
    start = source.index("permanent_indicators = [")
    permanent = source[start:source.index("]", start)].lower()

    for phrase in (
        "template may not be approved",
        "template does not exist",
        "template name is invalid",
    ):
        assert phrase not in permanent, (
            f"{phrase!r} must stay retryable — Meta returns the same code for "
            f"'awaiting approval' as for 'no such template', and approval flips "
            f"on its own"
        )
    # The one template case that IS permanent: none configured at all.
    assert "lab_report_template_name unset" in permanent


def test_template_name_is_per_clinic():
    """Templates live on a WABA — a clinic override must beat the global default."""
    from app.services.lab_reports import template_name_for
    from app.config import settings

    assert template_name_for(None) == settings.lab_report_template_name
    assert template_name_for({"config": {}}) == settings.lab_report_template_name
    assert (
        template_name_for({"config": {"lab_report_template_name": "lab_report_delivery_v2"}})
        == "lab_report_delivery_v2"
    )
