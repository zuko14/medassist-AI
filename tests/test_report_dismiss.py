"""Tests for triage queue dismiss endpoints, UI integration, and Razorpay error handling."""

import inspect
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
import httpx

from app.routers.admin import (
    AdminUser,
    DismissReportsBatchRequest,
    dismiss_lab_report,
    dismiss_lab_reports_batch,
)
from app.services.payment import PaymentService

ADMIN_HTML = pathlib.Path("admin/index.html")


def _html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


# ── 1. Single Report Dismiss ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dismiss_single_report_success():
    admin = AdminUser("staff_user", role="clinic_admin", clinic_id="c-1", user_id="u-1")
    existing_mock = MagicMock(data=[{
        "id": "r-123",
        "status": "needs_review",
        "patient_name": "Test Patient",
        "patient_phone": "+919876543210",
        "report_name": "CBC",
    }])
    update_mock = MagicMock(data=[{"id": "r-123", "status": "dismissed"}])

    sb_mock = AsyncMock(side_effect=[existing_mock, update_mock])

    with patch("app.routers.admin.sb", sb_mock), \
         patch("app.routers.admin.log_admin_action", AsyncMock()) as mock_audit:
        res = await dismiss_lab_report(
            report_id="r-123",
            clinic_id="c-1",
            request=None,
            user=admin,
        )

    assert res["success"] is True
    assert res["report_id"] == "r-123"
    assert res["status"] == "dismissed"
    mock_audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_dismiss_single_report_not_found_or_cross_tenant():
    admin = AdminUser("staff_user", role="clinic_admin", clinic_id="c-1", user_id="u-1")
    # Returns empty data because report belongs to another tenant or doesn't exist
    empty_mock = MagicMock(data=[])

    with patch("app.routers.admin.sb", AsyncMock(return_value=empty_mock)):
        with pytest.raises(HTTPException) as exc:
            await dismiss_lab_report(
                report_id="r-other-tenant",
                clinic_id="c-1",
                request=None,
                user=admin,
            )
    assert exc.value.status_code == 404


# ── 2. Batch Dismiss ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dismiss_batch_by_report_ids():
    admin = AdminUser("staff_user", role="clinic_admin", clinic_id="c-1", user_id="u-1")
    res_mock = MagicMock(data=[{"id": "r-1"}, {"id": "r-2"}, {"id": "r-3"}])

    with patch("app.routers.admin.sb", AsyncMock(return_value=res_mock)), \
         patch("app.routers.admin.log_admin_action", AsyncMock()) as mock_audit:
        body = DismissReportsBatchRequest(report_ids=["r-1", "r-2", "r-3"])
        res = await dismiss_lab_reports_batch(
            body=body,
            clinic_id="c-1",
            request=None,
            user=admin,
        )

    assert res["success"] is True
    assert res["dismissed_count"] == 3
    mock_audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_dismiss_all_needs_review():
    admin = AdminUser("staff_user", role="clinic_admin", clinic_id="c-1", user_id="u-1")
    res_mock = MagicMock(data=[{"id": f"r-{i}"} for i in range(52)])

    with patch("app.routers.admin.sb", AsyncMock(return_value=res_mock)), \
         patch("app.routers.admin.log_admin_action", AsyncMock()) as mock_audit:
        body = DismissReportsBatchRequest(dismiss_all_needs_review=True)
        res = await dismiss_lab_reports_batch(
            body=body,
            clinic_id="c-1",
            request=None,
            user=admin,
        )

    assert res["success"] is True
    assert res["dismissed_count"] == 52
    mock_audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_dismiss_batch_requires_ids_or_flag():
    admin = AdminUser("staff_user", role="clinic_admin", clinic_id="c-1", user_id="u-1")
    body = DismissReportsBatchRequest(report_ids=None, dismiss_all_needs_review=False)

    with pytest.raises(HTTPException) as exc:
        await dismiss_lab_reports_batch(
            body=body,
            clinic_id="c-1",
            request=None,
            user=admin,
        )
    assert exc.value.status_code == 400


# ── 3. Panel UI Verification ─────────────────────────────────────────────────

def test_panel_html_has_dismiss_all_button():
    html = _html()
    assert 'id="dismissAllReviewBtn"' in html
    assert 'onclick="dismissAllReviewReports()"' in html
    assert "Dismiss All" in html


def test_panel_html_has_dismiss_per_row_buttons():
    html = _html()
    assert "dismissReviewReport(" in html
    assert "async function dismissReviewReport(" in html
    assert "async function dismissAllReviewReports(" in html


# ── 4. Razorpay Detailed Error Diagnostics ───────────────────────────────────

@pytest.mark.asyncio
async def test_create_payment_link_surfaces_razorpay_error_description():
    svc = PaymentService()

    fake_request = httpx.Request("POST", "https://api.razorpay.com/v1/payment_links")
    fake_response = httpx.Response(
        status_code=429,
        request=fake_request,
        json={"error": {"code": "RATE_LIMIT_EXCEEDED", "description": "test mode limit of 30 reached for payment_link"}},
    )

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=fake_response)):
        with pytest.raises(RuntimeError) as exc:
            await svc._create_payment_link(
                amount_paise=50000,
                booking_id="b-1",
                booking_ref="REF-1",
                patient_phone="+917981945956",
                patient_name="Test Patient",
                key_id="rzp_test_123",
                key_secret="secret",
            )
    assert "test mode limit of 30 reached for payment_link" in str(exc.value)
    assert "429" in str(exc.value)
