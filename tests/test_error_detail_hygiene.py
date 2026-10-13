"""Server errors must not echo exception text; resend and cancel are audited.

Kriya Ketu bug register:
  D-14  19 routes put the raw exception (str(e) / f"...{e}") into a 5xx response
        body: database errors, file paths and provider messages reached the browser.
  D-03  POST /admin/lab-reports/{id}/resend returned str(e) on a 500 and wrote no
        audit row; /admin/reports/{id}/resend turned every failure, including the
        user-facing "re-upload the report" messages, into a 500 with str(e).
  D-04  DELETE /admin/appointments/{id} (staff cancellation) wrote no audit row.
"""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers.admin import AdminUser

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"


def _status(call: ast.Call):
    for kw in call.keywords:
        if kw.arg == "status_code" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _leaks(handler: ast.ExceptHandler):
    """HTTPException(status_code>=500, detail=<uses the caught exception>) inside this handler."""
    found = []
    for node in ast.walk(handler):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HTTPException"):
            continue
        status = _status(node)
        detail = next((kw.value for kw in node.keywords if kw.arg == "detail"), None)
        if status is None or status < 500 or detail is None:
            continue
        # admin._friendly_db_error() inspects the exception but returns only fixed sentences.
        if isinstance(detail, ast.Call) and getattr(detail.func, "id", None) == "_friendly_db_error":
            continue
        if any(isinstance(n, ast.Name) and n.id == handler.name for n in ast.walk(detail)):
            found.append(node.lineno)
    return found


def test_no_router_returns_exception_text_in_a_server_error():
    offenders = []
    for path in sorted(ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.name:
                offenders += [f"{path.name}:{line}" for line in _leaks(node)]
    assert offenders == [], f"5xx responses that echo the exception: {offenders}"


def _admin():
    return AdminUser("admin", role="clinic_admin", clinic_id="c-1", user_id="u-1")


@pytest.mark.asyncio
async def test_lab_reports_resend_hides_internal_error_text():
    from app.routers import admin

    with patch.object(admin.LabReportService, "resend_report",
                      new_callable=AsyncMock, side_effect=RuntimeError("db password=hunter2")), \
         patch.object(admin, "log_admin_action", new_callable=AsyncMock):
        with pytest.raises(HTTPException) as exc:
            await admin.resend_lab_report("r-1", clinic_id="c-1", user=_admin(), request=None)
    assert exc.value.status_code == 500
    assert "hunter2" not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_lab_reports_resend_keeps_user_facing_messages_and_is_audited():
    from app.routers import admin

    with patch.object(admin.LabReportService, "resend_report",
                      new_callable=AsyncMock, side_effect=ValueError("Report not found")), \
         patch.object(admin, "log_admin_action", new_callable=AsyncMock):
        with pytest.raises(HTTPException) as exc:
            await admin.resend_lab_report("r-1", clinic_id="c-1", user=_admin(), request=None)
    assert exc.value.status_code == 404 and exc.value.detail == "Report not found"

    with patch.object(admin.LabReportService, "resend_report", new_callable=AsyncMock), \
         patch.object(admin, "log_admin_action", new_callable=AsyncMock) as audit:
        await admin.resend_lab_report("r-1", clinic_id="c-1", user=_admin(), request=None)
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "resend_lab_report"


def _report_lookup(found=True):
    db = MagicMock()
    db.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = (
        MagicMock(data=[{"id": "r-1", "clinic_id": "c-1"}] if found else [])
    )
    return db


@pytest.mark.asyncio
async def test_reports_resend_maps_retention_message_to_a_400():
    from app.routers import admin

    msg = "PDF has been removed after the 90-day retention period. Please re-upload the report."
    with patch.object(admin, "supabase", _report_lookup()), \
         patch.object(admin.LabReportService, "resend_report", new_callable=AsyncMock, side_effect=ValueError(msg)), \
         patch.object(admin, "log_admin_action", new_callable=AsyncMock):
        with pytest.raises(HTTPException) as exc:
            await admin.resend_lab_report_from_queue("r-1", body=None, clinic_id="c-1", request=None, user=_admin())
    assert exc.value.status_code == 400 and exc.value.detail == msg


@pytest.mark.asyncio
async def test_reports_resend_hides_unexpected_errors():
    from app.routers import admin

    with patch.object(admin, "supabase", _report_lookup()), \
         patch.object(admin.LabReportService, "resend_report",
                      new_callable=AsyncMock, side_effect=RuntimeError("storage key abc123")), \
         patch.object(admin, "log_admin_action", new_callable=AsyncMock):
        with pytest.raises(HTTPException) as exc:
            await admin.resend_lab_report_from_queue("r-1", body=None, clinic_id="c-1", request=None, user=_admin())
    assert exc.value.status_code == 500 and "abc123" not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_staff_cancellation_is_audited():
    from app.routers import admin
    from app.services.payment import payment_service

    with patch.object(admin, "_enforce_booking_branch", new_callable=AsyncMock), \
         patch.object(payment_service, "admin_cancel_confirmed_booking",
                      new_callable=AsyncMock, return_value={"success": True}), \
         patch.object(admin, "log_admin_action", new_callable=AsyncMock) as audit:
        res = await admin.cancel_appointment_by_admin("a-1", clinic_id="c-1", user=_admin(), request=None)
    assert res == {"success": True}
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "cancel_appointment"
    assert audit.await_args.kwargs["resource_id"] == "a-1"


@pytest.mark.asyncio
async def test_failed_cancellation_is_not_audited_as_a_cancellation():
    from app.routers import admin
    from app.services.payment import payment_service

    with patch.object(admin, "_enforce_booking_branch", new_callable=AsyncMock), \
         patch.object(payment_service, "admin_cancel_confirmed_booking",
                      new_callable=AsyncMock, return_value={"success": False, "reason": "not_confirmed"}), \
         patch.object(admin, "log_admin_action", new_callable=AsyncMock) as audit:
        await admin.cancel_appointment_by_admin("a-1", clinic_id="c-1", user=_admin(), request=None)
    audit.assert_not_called()
