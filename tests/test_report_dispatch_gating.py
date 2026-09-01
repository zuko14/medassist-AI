"""The daily report limit and suspension gate inside the lab-report pipeline.

The contract these pin down: when a clinic is over its limit or suspended, the
report is QUEUED, never lost and never silently dropped. The PDF is already in
storage, the row is left in pending_retry, and next_retry_at points at the
Asia/Kolkata reset — which is exactly what the existing retry worker consumes.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.lab_reports as lab_reports_module
from app.services.lab_reports import LabReportService
from app.services.subscription import next_ist_midnight

CLINIC_ID = "11111111-2222-3333-4444-555555555555"
PHONE = "9876543210"


def _clinic(limit=50, status="active"):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {
        "id": CLINIC_ID,
        "name": "Apex Diagnostics",
        "daily_report_limit": limit,
        "subscription_start_date": (now - timedelta(days=1)).isoformat(),
        "subscription_end_date": (now + timedelta(days=29)).isoformat(),
        "grace_period_days": 5,
        "subscription_status": status,
    }


async def _run_upload(clinic, reports_today):
    """Drive upload_and_send with every external edge stubbed, and return the
    persisted row plus the WhatsApp send mocks so a test can assert on both."""
    send_template = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
    send_text = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
    send_document = AsyncMock(return_value=True)

    captured = {}

    mock_bucket = MagicMock()
    mock_bucket.upload.return_value = {"Key": "uploaded"}
    mock_bucket.create_signed_url.return_value = {"signedURL": "https://example.test/f.pdf"}

    mock_table = MagicMock()

    inserts = []

    def _insert(row):
        # supabase.table() is patched to one mock, so analytics_events inserts
        # land here too. Keep only the lab_reports row.
        inserts.append(row)
        if isinstance(row, dict) and "report_name" in row:
            captured["row"] = row
        return MagicMock(execute=MagicMock(return_value=MagicMock(data=[{**row, "id": "rep-1"}])))

    mock_table.insert.side_effect = _insert
    mock_table.select.return_value = MagicMock(
        eq=MagicMock(return_value=MagicMock(
            execute=MagicMock(return_value=MagicMock(data=[]))))
    )

    with patch("app.services.lab_reports.extract_text_from_pdf", return_value="text"), \
         patch("app.services.lab_reports.ReportSummarizer.summarize",
               new=AsyncMock(return_value={"patient_message": "Normal", "has_abnormal": False,
                                           "fallback": False})), \
         patch("app.services.lab_reports.get_clinic_by_id", new=AsyncMock(return_value=clinic)), \
         patch("app.services.lab_reports.whatsapp_service.send_template", new=send_template), \
         patch("app.services.lab_reports.whatsapp_service.send_text", new=send_text), \
         patch("app.services.lab_reports.whatsapp_service.send_document", new=send_document), \
         patch("app.services.lab_reports.whatsapp_service._can_send_freeform",
               new=AsyncMock(return_value=True)), \
         patch("app.services.subscription.get_daily_usage", new=AsyncMock(
               return_value={"reports_delivered_count": reports_today})), \
         patch.object(lab_reports_module.supabase.storage, "from_", return_value=mock_bucket), \
         patch.object(lab_reports_module.supabase, "table", return_value=mock_table):

        result = await LabReportService().upload_and_send(
            clinic_id=CLINIC_ID,
            patient_phone=PHONE,
            patient_name="Alice",
            filename="blood_test.pdf",
            file_bytes=b"%PDF-1.4 dummy",
            content_type="application/pdf",
            report_name="Blood Test",
            report_type="blood_test",
        )

    return result, captured.get("row", {}), (send_template, send_text, send_document)


@pytest.mark.asyncio
async def test_under_the_limit_the_report_is_delivered_normally():
    result, row, (send_template, send_text, send_document) = await _run_upload(
        _clinic(limit=50), reports_today=10
    )
    assert row["status"] == "sent"
    assert send_document.await_count == 1


@pytest.mark.asyncio
async def test_at_the_limit_no_whatsapp_call_is_made_at_all():
    """The whole point of the gate: Meta is never billed past the tier."""
    result, row, (send_template, send_text, send_document) = await _run_upload(
        _clinic(limit=50), reports_today=50
    )
    assert send_template.await_count == 0
    assert send_text.await_count == 0
    assert send_document.await_count == 0


@pytest.mark.asyncio
async def test_at_the_limit_the_report_is_queued_not_failed():
    result, row, _ = await _run_upload(_clinic(limit=50), reports_today=50)
    assert row["status"] == "pending_retry"
    assert row["delivery_status"] == "pending_retry"
    assert "Daily report limit reached" in row["error_message"]


@pytest.mark.asyncio
async def test_a_queued_report_retries_at_the_asia_kolkata_reset():
    result, row, _ = await _run_upload(_clinic(limit=50), reports_today=50)
    expected = next_ist_midnight()
    # Same reset instant to the minute; the two calls are moments apart.
    assert row["next_retry_at"][:16] == expected.isoformat()[:16]


@pytest.mark.asyncio
async def test_a_policy_hold_does_not_burn_a_meta_retry_attempt():
    """retry_count is the Meta-outage budget. A limit hold must not spend it."""
    result, row, _ = await _run_upload(_clinic(limit=50), reports_today=50)
    assert "retry_count" not in row


@pytest.mark.asyncio
async def test_the_pdf_is_still_stored_so_the_queued_report_is_recoverable():
    result, row, _ = await _run_upload(_clinic(limit=50), reports_today=50)
    assert row["file_path"], "a queued report with no file_path can never be redelivered"


@pytest.mark.asyncio
async def test_a_suspended_clinic_queues_rather_than_delivering():
    result, row, (send_template, send_text, send_document) = await _run_upload(
        _clinic(limit=500, status="suspended"), reports_today=0
    )
    assert send_document.await_count == 0
    assert row["status"] == "pending_retry"
    assert "Subscription suspended" in row["error_message"]


@pytest.mark.asyncio
async def test_an_unlimited_clinic_is_never_gated():
    result, row, (_, _, send_document) = await _run_upload(
        _clinic(limit=0), reports_today=99_999
    )
    assert row["status"] == "sent"
    assert send_document.await_count == 1
