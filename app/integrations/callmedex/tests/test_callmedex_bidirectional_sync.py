"""CallMedex <-> Kriya: signed callbacks, WhatsApp booking flow, center resolution.

The CallMedex models below mirror callmedex/backend/app/routers/mediassist_inbound.py
and the verifier mirrors app/middleware/mediassist_auth.py — if either side
drifts, these tests are where it shows.
"""

import hashlib
import hmac
import time
from datetime import datetime
from typing import List, Literal, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import BaseModel, Field

from app.integrations.callmedex.api import client as cmx_client
from app.integrations.callmedex.api.schemas import (
    ConnectorType, PatientIdentity, ProcessReportRequest, ReportType,
)
from app.integrations.callmedex.callbacks.handler import CallMedexCallbackHandler, build_analysis_payload
from app.integrations.callmedex.config.settings import callmedex_settings
from app.integrations.callmedex.whatsapp import booking


# ── Mirrors of CallMedex's receiving side ────────────────────────────────────

class _AbnormalFlag(BaseModel):
    marker: Optional[str] = None
    value: Optional[str] = None
    status: Optional[Literal["normal", "high", "low", "critical"]] = None
    reference_range: Optional[str] = None


class _Analysis(BaseModel):
    plain_language_summary: str
    doctor_clinical_summary: str
    health_score: Optional[int] = None
    abnormal_flags: List[_AbnormalFlag] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)


class _Delivered(BaseModel):
    report_job_id: str
    occurred_at: datetime
    delivered_channel: Literal["whatsapp"]
    message_id: Optional[str] = None
    analysis: _Analysis


class _Failed(BaseModel):
    report_job_id: str
    occurred_at: datetime
    failure_reason: Literal[
        "ocr_failed", "interpretation_failed", "delivery_failed", "invalid_source_document",
        "report_not_ready_timeout", "bill_payment_pending", "download_automation_failed",
    ]
    details: Optional[str] = None


class _Booking(BaseModel):
    patient_id: Optional[str] = None
    phone: str
    service_type: Literal["home_blood_collection", "home_nursing_visit", "pharmacy_order", "video_consultation"]
    requested_time_window: dict
    address: dict
    source: Literal["whatsapp"]
    source_conversation_id: str


def _callmedex_verifies(req: httpx.Request, secret: str, token: str) -> bool:
    """CallMedex verify_mediassist_signature, re-implemented."""
    if req.headers.get("Authorization") != f"Bearer {token}":
        return False
    ts = req.headers["X-Timestamp"]
    if abs(time.time() - int(ts)) > 300:
        return False
    sig = req.headers.get("X-Signature", "")
    if not sig.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + req.url.query + req.content, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig[len("sha256="):], expected)


@pytest.fixture
def cmx_configured(monkeypatch):
    monkeypatch.setattr(callmedex_settings, "callmedex_base_url", "https://cmx.example")
    monkeypatch.setattr(callmedex_settings.hmac_signature_secret, "_secret_value", "shared_secret")
    monkeypatch.setattr(callmedex_settings.bearer_token, "_secret_value", "bearer_x")
    monkeypatch.setattr(callmedex_settings.outbound_bearer_token, "_secret_value", "")


@pytest.fixture
def captured(monkeypatch):
    """Route cmx_client's httpx traffic to an in-memory transport."""
    sent: list[httpx.Request] = []
    responses: list[httpx.Response] = []
    real = httpx.AsyncClient

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return responses.pop(0) if responses else httpx.Response(200, json={"received": True})

    monkeypatch.setattr(cmx_client.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(cmx_client.asyncio, "sleep", AsyncMock())
    return sent, responses


# ── Task 1: signed per-event callbacks ───────────────────────────────────────

@pytest.mark.asyncio
async def test_callback_is_signed_exactly_as_callmedex_verifies(cmx_configured, captured):
    sent, _ = captured
    ok = await CallMedexCallbackHandler(secret="unused").send_report_delivered(
        "job-1", "2026-09-24T10:00:00+00:00", "wamid.1", build_analysis_payload(), "corr-1"
    )
    assert ok is True
    req = sent[0]
    assert str(req.url) == "https://cmx.example/api/v1/integrations/mediassist/callbacks/report-delivered"
    assert _callmedex_verifies(req, "shared_secret", "bearer_x")
    assert req.headers["X-Correlation-Id"] == "corr-1"
    _Delivered.model_validate_json(req.content)  # CallMedex would accept the body


@pytest.mark.asyncio
async def test_failed_callback_body_matches_callmedex_model(cmx_configured, captured):
    sent, _ = captured
    await CallMedexCallbackHandler(secret="unused").send_report_failed(
        "job-2", "2026-09-24T10:00:00+00:00", "report_not_ready_timeout", "x" * 900, "corr-2"
    )
    body = _Failed.model_validate_json(sent[0].content)
    assert body.failure_reason == "report_not_ready_timeout" and len(body.details) == 500


@pytest.mark.asyncio
async def test_signed_get_covers_the_query_string(cmx_configured, captured):
    sent, _ = captured
    await cmx_client.request("GET", "/patients/lookup", params={"phone": "+919876543210"}, attempts=1)
    req = sent[0]
    assert req.url.query == b"phone=%2B919876543210"
    assert _callmedex_verifies(req, "shared_secret", "bearer_x")


@pytest.mark.asyncio
async def test_outbound_bearer_override(cmx_configured, captured, monkeypatch):
    sent, _ = captured
    monkeypatch.setattr(callmedex_settings.outbound_bearer_token, "_secret_value", "inbound_token_on_cmx")
    await cmx_client.request("POST", "/callbacks/report-accepted", json_body={"report_job_id": "j"}, attempts=1)
    assert _callmedex_verifies(sent[0], "shared_secret", "inbound_token_on_cmx")


@pytest.mark.asyncio
async def test_callback_idempotency_key_is_stable_per_job_and_event(cmx_configured, captured):
    sent, _ = captured
    h = CallMedexCallbackHandler(secret="unused")
    await h.send_report_failed("job-9", "t", "ocr_failed", "x", "c1")
    await h.send_report_failed("job-9", "t", "ocr_failed", "x", "c2")
    await h.send_report_processing("job-9", "t", "c3")
    keys = [r.headers["X-Idempotency-Key"] for r in sent]
    assert keys[0] == keys[1] != keys[2]


@pytest.mark.asyncio
async def test_no_base_url_means_no_http_call(monkeypatch, captured):
    sent, _ = captured
    monkeypatch.setattr(callmedex_settings, "callmedex_base_url", "")
    assert await CallMedexCallbackHandler(secret="s").send_report_processing("j", "t", "c") is False
    assert sent == []


@pytest.mark.asyncio
async def test_callback_retries_5xx_then_reports_failure(cmx_configured, captured):
    sent, responses = captured
    responses.extend([httpx.Response(503), httpx.Response(503), httpx.Response(503)])
    assert await CallMedexCallbackHandler(secret="s").send_report_accepted("j", "t", "c") is False
    assert len(sent) == 3


def test_analysis_payload_from_real_summary():
    from app.integrations.callmedex.ai.schemas import MultiAudienceSummaryReport, StatementProvenance, SummaryStatus
    from app.integrations.callmedex.ocr.schemas import (
        CanonicalLabReport, CanonicalReportMetadata, ExtractedLabTest, ExtractionSource, LabFlag,
    )

    summary = MultiAudienceSummaryReport(
        patient_summary=[StatementProvenance(statement="Sugar is high.", supported_by=["HBA1C"])],
        clinician_summary=[StatementProvenance(statement="HbA1c 7.2% [<5.7].", supported_by=["HBA1C"])],
        medical_disclaimer="d", status=SummaryStatus.FLAGGED_FOR_REVIEW, overall_confidence=0.9,
    )
    canonical = CanonicalLabReport(
        report_metadata=CanonicalReportMetadata(report_id="r", patient_id="p", barcode="b", generated_at="t"),
        tests=[
            ExtractedLabTest(code="HBA1C", display_name="HbA1c", value=7.2, unit="%", reference_range="< 5.7",
                             flag=LabFlag.HIGH, confidence=0.9, source=ExtractionSource.PDF_TEXT),
            ExtractedLabTest(code="HB", display_name="Hemoglobin", value=14.0, unit="g/dL", reference_range="13-17",
                             flag=LabFlag.NORMAL, confidence=0.9, source=ExtractionSource.PDF_TEXT),
        ],
    )
    a = _Analysis.model_validate(build_analysis_payload(summary, canonical))
    assert a.plain_language_summary == "Sugar is high."
    assert a.doctor_clinical_summary == "HbA1c 7.2% [<5.7]."
    assert [f.marker for f in a.abnormal_flags] == ["HbA1c"]
    assert a.abnormal_flags[0].value == "7.2 %" and a.abnormal_flags[0].status == "high"
    assert a.health_score is None  # never invented


# ── Task 1: runner wiring ───────────────────────────────────────────────────

def _runner_with_mocks(monkeypatch):
    from app.integrations.callmedex.workers import runner as runner_mod

    monkeypatch.setattr(callmedex_settings, "app_env", "development")
    r = runner_mod.CallMedexWorkerRunner()
    c = r.container.mocdoc_connector
    for name in ("open_login_page", "login", "logout", "validate_report"):
        setattr(c, name, AsyncMock(return_value=True))
    c.health_check = AsyncMock(return_value={"status": "healthy"})
    c.search_by_barcode = AsyncMock(return_value=MagicMock())
    c.wait_until_report_available = AsyncMock(return_value=True)
    c.download_report = AsyncMock(return_value=b"%PDF-1.4 x")
    monkeypatch.setattr(runner_mod, "resolve_processing_center", AsyncMock(side_effect=ValueError("none")))
    h = r.container.callback_handler
    for name in ("send_report_accepted", "send_report_processing", "send_report_delivered", "send_report_failed"):
        monkeypatch.setattr(h, name, AsyncMock(return_value=True))
    return r, c, h


def _request(job_id=None):
    return ProcessReportRequest(
        clinic_id="clinic-x", connector_type=ConnectorType.MOCDOC, external_report_id="BC-1",
        patient=PatientIdentity(patient_phone="+919876543210", patient_name="P"),
        report_name="CBC", report_type=ReportType.LABORATORY, report_job_id=job_id,
    )


def _fake_supabase():
    fake = MagicMock()
    fake.storage.from_.return_value.create_signed_url.return_value = {"signedURL": "https://s/x.pdf"}
    return fake


def _primary(status_name: str, message_id: str = "wamid.P"):
    from app.integrations.callmedex.workers import runner as runner_mod
    from app.integrations.callmedex.whatsapp.schemas import WhatsAppDeliveryStatus

    result = MagicMock(status=getattr(WhatsAppDeliveryStatus, status_name), message_id=message_id)
    return patch.object(runner_mod.WhatsAppDeliveryService, "deliver_report_and_summary", AsyncMock(return_value=result))


@pytest.mark.asyncio
async def test_runner_delivered_callback_after_primary_delivery(monkeypatch):
    r, _, h = _runner_with_mocks(monkeypatch)
    upload = AsyncMock()
    with patch("app.database.supabase", _fake_supabase()), _primary("DELIVERED"), \
         patch("app.services.lab_reports.LabReportService.upload_and_send", upload):
        resp = await r.execute_report_job(_request("cmx-job-1"))
    assert resp.success and resp.callback_delivered is True
    upload.assert_not_awaited()
    args = h.send_report_delivered.await_args.args
    assert args[0] == "cmx-job-1" and args[2] == "wamid.P"
    _Analysis.model_validate(args[3])
    h.send_report_failed.assert_not_awaited()
    h.send_report_accepted.assert_called_once()  # fire-and-forget task
    h.send_report_processing.assert_called_once()


@pytest.mark.asyncio
async def test_runner_falls_back_when_primary_send_fails(monkeypatch):
    from app.integrations.callmedex.workers import runner as runner_mod
    from app.integrations.callmedex.whatsapp.schemas import WhatsAppDeliveryStatus

    r, _, h = _runner_with_mocks(monkeypatch)
    monkeypatch.setattr(r.container.ocr_pipeline, "process_pdf", MagicMock(return_value=MagicMock(tests=[])))
    monkeypatch.setattr(runner_mod, "ClinicalReasoningEngine", MagicMock())
    summary = MagicMock()
    summary.status.value = "success"
    summary.patient_summary = []
    summary.clinician_summary = []
    monkeypatch.setattr(
        runner_mod, "MultiAudienceSummaryGenerator",
        MagicMock(return_value=MagicMock(generate_summary=MagicMock(return_value=summary))),
    )
    failed = MagicMock(status=WhatsAppDeliveryStatus.FAILED, message_id="x")
    upload = AsyncMock(return_value={"status": "sent", "whatsapp_message_id": "wamid.F"})
    with patch("app.database.supabase", _fake_supabase()), \
         patch.object(runner_mod.WhatsAppDeliveryService, "deliver_report_and_summary", AsyncMock(return_value=failed)), \
         patch("app.services.lab_reports.LabReportService.upload_and_send", upload):
        await r.execute_report_job(_request("cmx-job-2"))
    upload.assert_awaited_once()  # patient still gets the report from the clinic number
    h.send_report_delivered.assert_awaited_once()


@pytest.mark.asyncio
async def test_runner_undeliverable_sends_delivery_failed(monkeypatch):
    r, _, h = _runner_with_mocks(monkeypatch)
    upload = AsyncMock(side_effect=ValueError("Missing WhatsApp credentials"))
    with patch("app.database.supabase", _fake_supabase()), _primary("FAILED"), \
         patch("app.services.lab_reports.LabReportService.upload_and_send", upload):
        await r.execute_report_job(_request("cmx-job-4"))
    upload.assert_awaited_once()
    assert h.send_report_failed.await_args.args[2] == "delivery_failed"
    h.send_report_delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_login_failure_sends_mapped_failed_callback(monkeypatch):
    from app.integrations.callmedex.api.exceptions import AuthenticationError

    r, c, h = _runner_with_mocks(monkeypatch)
    c.login = AsyncMock(side_effect=AuthenticationError("bad creds"))
    with pytest.raises(AuthenticationError):
        await r.execute_report_job(_request("cmx-job-3"))
    args = h.send_report_failed.await_args.args
    assert args[0] == "cmx-job-3" and args[2] == "download_automation_failed"


@pytest.mark.asyncio
async def test_runner_sends_no_callbacks_without_callmedex_job_id(monkeypatch):
    r, c, h = _runner_with_mocks(monkeypatch)
    c.login = AsyncMock(side_effect=RuntimeError("x"))
    with pytest.raises(RuntimeError):
        await r.execute_report_job(_request(None))
    for name in ("send_report_accepted", "send_report_failed", "send_report_delivered"):
        getattr(h, name).assert_not_awaited()


def test_failure_reason_mapping():
    from app.integrations.callmedex.api.exceptions import ReportDownloadError, ValidationError
    from app.integrations.callmedex.connectors.base.connector import JobCheckpoint
    from app.integrations.callmedex.workers.runner import _failure_reason

    assert _failure_reason(ValidationError("x"), JobCheckpoint.PDF_DOWNLOADED) == "invalid_source_document"
    assert _failure_reason(
        ReportDownloadError("Barcode 'X' did not appear in MocDoc Pending Print within 60s"),
        JobCheckpoint.AUTHENTICATED,
    ) == "report_not_ready_timeout"
    assert _failure_reason(RuntimeError("boom"), JobCheckpoint.CREATED) == "download_automation_failed"


@pytest.mark.asyncio
async def test_simulation_mode_is_a_failure_in_production(monkeypatch):
    from app.config import settings as app_settings
    from app.integrations.callmedex.whatsapp.schemas import WhatsAppDeliveryStatus, WhatsAppTemplatePayload
    from app.integrations.callmedex.whatsapp.service import WhatsAppDeliveryService

    svc = WhatsAppDeliveryService()
    monkeypatch.setattr(svc, "_get_effective_whatsapp_credentials", AsyncMock(return_value=("dev_whatsapp_token", "1")))
    payload = WhatsAppTemplatePayload(
        to="+91", language_code="en", header_pdf_url="u", body_text_summary="s", disclaimer_text="d",
    )
    monkeypatch.setattr(app_settings, "app_env", "production")
    assert (await svc._send_meta_whatsapp_cloud_api("+91", payload))[0] == WhatsAppDeliveryStatus.FAILED
    monkeypatch.setattr(app_settings, "app_env", "development")
    assert (await svc._send_meta_whatsapp_cloud_api("+91", payload))[0] == WhatsAppDeliveryStatus.DELIVERED


# ── Processing center resolution (owner-panel enrollment) ────────────────────

@pytest.mark.asyncio
async def test_resolver_prefers_clinic_wide_row_and_decrypts_password(monkeypatch):
    from cryptography.fernet import Fernet
    from app.config import settings as app_settings
    from app.integrations.callmedex.config import processing_centers as pc
    from app.utils.connector_crypto import encrypt_password

    key = Fernet.generate_key().decode()
    monkeypatch.setattr(app_settings, "connector_encryption_key", key)
    rows = [
        {"branch_id": "b-1", "config": {"base_url": "branch.example", "clinic_slug": "s", "username": "u2"}},
        {"branch_id": None, "config": {"base_url": "https://mocdoc.com/", "clinic_slug": "vmc",
                                        "username": "u1", "password_encrypted": encrypt_password("pw1", key)}},
    ]
    monkeypatch.setattr(pc, "sb", AsyncMock(return_value=MagicMock(data=rows)))
    cfg = await pc.resolve_processing_center("clinic-a")
    assert (cfg.base_url, cfg.clinic_slug, cfg.username, cfg.password) == ("https://mocdoc.com", "vmc", "u1", "pw1")


# ── Task 2: WhatsApp booking on the CallMedex number ────────────────────────

@pytest.mark.asyncio
async def test_callmedex_number_never_shadows_a_clinic(monkeypatch):
    monkeypatch.setattr(callmedex_settings, "whatsapp_phone_number_id", "CMX-1")
    rows = {
        "callmedex_whatsapp_settings": [{"phone_number_id": "CMX-2"}],
        "clinics": [{"phone_number_id": "CLINIC-1", "config": {"meta_phone_number_id": "CMX-2"}}],
    }
    fake_supabase = MagicMock()
    fake_supabase.table.side_effect = lambda t: rows[t]  # the "query" is just its rows

    async def fake_sb(q):
        # .select(...) / .eq(...) chains on a list are not needed: return rows directly
        return MagicMock(data=q)

    class _Q(list):
        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

    fake_supabase.table.side_effect = lambda t: _Q(rows[t])
    monkeypatch.setattr(booking, "supabase", fake_supabase)
    monkeypatch.setattr(booking, "sb", fake_sb)
    monkeypatch.setitem(booking._ids_cache, "at", float("-inf"))
    monkeypatch.setitem(booking._ids_cache, "ids", frozenset())
    assert await booking.is_callmedex_number("CMX-1") is True
    assert await booking.is_callmedex_number("CMX-2") is False  # a clinic owns it
    assert await booking.is_callmedex_number("CLINIC-1") is False
    assert booking.cached_callmedex_number("CMX-1") is True
    assert await booking.is_callmedex_number(None) is False


@pytest.fixture
def flow(monkeypatch, cmx_configured):
    """In-memory session store + captured replies + scripted CallMedex API."""
    store: dict = {}
    replies: list[dict] = []
    api_calls: list[dict] = []
    api_responses: dict = {}

    async def load(phone):
        return store.get(phone)

    async def save(phone, state, data):
        store[phone] = {"state": state, "data": data}

    async def clear(phone):
        store.pop(phone, None)

    async def send(phone, payload, pnid):
        replies.append(payload)
        return True

    async def api(method, path, **kw):
        api_calls.append({"method": method, "path": path, **kw})
        status, body = api_responses.get(path, (404, {}))
        return httpx.Response(status, json=body)

    monkeypatch.setattr(booking, "_load", load)
    monkeypatch.setattr(booking, "_save", save)
    monkeypatch.setattr(booking, "_clear", clear)
    monkeypatch.setattr(booking, "_purge_stale", AsyncMock())
    monkeypatch.setattr(booking, "_send", send)
    monkeypatch.setattr(booking.callmedex_client, "request", api)
    return store, replies, api_calls, api_responses


def _ids(payload):
    inter = payload.get("interactive", {})
    if inter.get("type") == "button":
        return [b["reply"]["id"] for b in inter["action"]["buttons"]]
    if inter.get("type") == "list":
        return [r["id"] for r in inter["action"]["sections"][0]["rows"]]
    return []


@pytest.mark.asyncio
async def test_full_booking_with_saved_address(flow):
    store, replies, calls, responses = flow
    phone = "+919876543210"
    responses["/patients/lookup"] = (200, {
        "patient_id": "pat_1", "preferred_language": "te",
        "default_address": {"line1": "Flat 302, Sai Residency", "city": "Visakhapatnam", "pincode": "530017"},
    })
    responses["/whatsapp-bookings"] = (201, {"booking_id": "bk_42", "status": "confirmed"})

    await booking.handle_turn(phone, "hi", None, "CMX")
    assert _ids(replies[-1]) == ["cmx_book"]
    await booking.handle_turn(phone, "Book home collection", "cmx_book", "CMX")
    date_id = _ids(replies[-1])[-1]  # the last offered day always has every window
    await booking.handle_turn(phone, "", date_id, "CMX")
    assert "cmx_win_7" in _ids(replies[-1])
    await booking.handle_turn(phone, "", "cmx_win_7", "CMX")
    assert _ids(replies[-1]) == ["cmx_addr_saved", "cmx_addr_new"]
    await booking.handle_turn(phone, "", "cmx_addr_saved", "CMX")
    assert _ids(replies[-1]) == ["cmx_confirm", "cmx_cancel"]
    await booking.handle_turn(phone, "Confirm", "cmx_confirm", "CMX")

    post = [c for c in calls if c["path"] == "/whatsapp-bookings"][0]
    b = _Booking.model_validate(post["json_body"])
    day = date_id.removeprefix("cmx_date_")
    assert b.patient_id == "pat_1" and b.phone == phone and b.service_type == "home_blood_collection"
    assert b.requested_time_window == {"earliest": f"{day}T07:00:00+05:30", "latest": f"{day}T09:00:00+05:30"}
    assert b.address == {"line1": "Flat 302, Sai Residency", "city": "Visakhapatnam", "pincode": "530017",
                         "lat": None, "lng": None}
    assert post["attempts"] == 1 and post["idem_key"]
    assert "bk_42" in replies[-1]["text"]["body"] and "నిర్ధారించబడింది" in replies[-1]["text"]["body"]
    assert phone not in store  # session cleared after booking


@pytest.mark.asyncio
async def test_typed_address_and_retry_reuses_idempotency_key(flow):
    store, replies, calls, responses = flow
    phone = "+919000000001"
    await booking.handle_turn(phone, "", "cmx_book", "CMX")  # stale menu tap starts a session
    date_id = _ids(replies[-1])[-1]
    await booking.handle_turn(phone, "", date_id, "CMX")
    await booking.handle_turn(phone, "", "cmx_win_9", "CMX")
    await booking.handle_turn(phone, "somewhere without pin", None, "CMX")
    assert store[phone]["state"] == "address_line"
    await booking.handle_turn(phone, "Flat 9, MVP Colony, 530017", None, "CMX")
    await booking.handle_turn(phone, "Visakhapatnam", None, "CMX")
    assert store[phone]["data"]["address"] == {"line1": "Flat 9, MVP Colony", "pincode": "530017", "city": "Visakhapatnam"}

    responses["/whatsapp-bookings"] = (503, {})
    await booking.handle_turn(phone, "Confirm", "cmx_confirm", "CMX")
    assert store[phone]["state"] == "confirm"  # can retry
    responses["/whatsapp-bookings"] = (201, {"booking_id": "bk_7", "status": "confirmed"})
    await booking.handle_turn(phone, "Confirm", "cmx_confirm", "CMX")
    posts = [c for c in calls if c["path"] == "/whatsapp-bookings"]
    assert posts[0]["idem_key"] == posts[1]["idem_key"]
    assert posts[0]["json_body"]["patient_id"] is None  # lookup 404 -> CallMedex resolves by phone
    assert "Your home sample collection is confirmed" in replies[-1]["text"]["body"]


@pytest.mark.asyncio
async def test_emergency_and_cancel_and_unconfigured(flow, monkeypatch):
    store, replies, calls, _ = flow
    await booking.handle_turn("+911", "my father had a heart attack", None, "CMX")
    assert "108" in replies[-1]["text"]["body"] and calls == []
    store["+911"] = {"state": "confirm", "data": {}}
    await booking.handle_turn("+911", "cancel", None, "CMX")
    assert "+911" not in store
    monkeypatch.setattr(callmedex_settings, "callmedex_base_url", "")
    await booking.handle_turn("+911", "hi", None, "CMX")
    assert "isn't available" in replies[-1]["text"]["body"] and calls == []


@pytest.mark.asyncio
async def test_booking_4xx_clears_session(flow):
    store, replies, _, responses = flow
    phone = "+919000000002"
    store[phone] = {"state": "confirm", "data": {
        "session_id": "s", "date": booking.date_options(datetime.now(booking.IST))[-1][0], "window": 11,
        "address": {"line1": "Flat 1, Road", "city": "Vizag", "pincode": "530001"},
    }}
    responses["/whatsapp-bookings"] = (400, {"error": {"code": "bad", "message": "bad"}})
    await booking.handle_turn(phone, "Confirm", "cmx_confirm", "CMX")
    assert phone not in store and "HI" in replies[-1]["text"]["body"]


def test_same_day_slots_respect_lead_time_and_cutoff():
    ist = booking.IST
    assert booking.window_options("2026-09-24", datetime(2026, 9, 24, 14, 30, tzinfo=ist)) == [16]
    assert booking.date_options(datetime(2026, 9, 24, 15, 1, tzinfo=ist))[0][0] == "2026-09-25"
    assert booking.date_options(datetime(2026, 9, 24, 8, 0, tzinfo=ist))[0] == ("2026-09-24", "Today, 24 Sep")
