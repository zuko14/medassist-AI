# tests/test_admin_connectors.py
"""Tests for diagnostic-center connector (MocDoc) self-service admin endpoints:
GET/PUT /admin/connectors, POST /connectors/{id}/toggle,
GET /connectors/{id}/audit-log, GET/POST /connectors/failed-reports*."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException, Request
from app.services.permissions import require_permission

from app.routers.admin import (
    AdminUser,
    ConnectorCredentialsUpdate,
    ConnectorToggle,
    get_connectors,
    upsert_connector_credentials,
    toggle_connector,
    get_connector_audit_log,
    get_connector_failed_reports,
    resolve_connector_failed_report,
)


def _mock_request() -> Request:
    req = MagicMock()
    req.client.host = "127.0.0.1"
    return req


@pytest.mark.asyncio
async def test_get_connectors_masks_credentials():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "conn-1",
                "clinic_id": "clinic-2",
                "connector_type": "mocdoc",
                "config": {"username": "labadmin", "password_encrypted": "gAAAA...", "base_url": "https://x"},
            }
        ]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        result = await get_connectors(clinic_id="default", user=admin)

    conn = result["connectors"][0]
    assert conn["config"]["username_masked"] == "la••••••"
    assert conn["config"]["password_set"] is True
    assert "password" not in conn["config"]
    assert "password_encrypted" not in conn["config"]


@pytest.mark.asyncio
async def test_upsert_connector_rejects_non_lab_reports_plan():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {"id": "clinic-1", "plan": "soloclinic", "whatsapp_number": "+911111111111"}
    body = ConnectorCredentialsUpdate(username="labadmin", password="secret123")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        with pytest.raises(HTTPException) as exc:
            await upsert_connector_credentials(
                body=body, request=_mock_request(), clinic_id="default", user=admin
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_upsert_connector_creates_new_encrypts_password_never_returns_raw():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    fake_clinic = {"id": "clinic-2", "plan": "diagstream", "whatsapp_number": "+912222222222"}

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    # No existing connector row
    mock_table.select.return_value.eq.return_value.eq.return_value.is_.return_value.execute.return_value = MagicMock(
        data=[]
    )
    mock_table.insert.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "conn-new",
                "clinic_id": "clinic-2",
                "branch_id": None,
                "connector_type": "mocdoc",
                "config": {"username": "labadmin", "password_encrypted": "gAAAA-encrypted-blob"},
                "is_enabled": False,
            }
        ]
    )

    body = ConnectorCredentialsUpdate(username="labadmin", password="supersecretpassword")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ), patch("app.routers.admin.supabase", mock_sb), patch(
        "app.routers.admin.settings"
    ) as mock_settings, patch(
        "app.routers.admin.log_admin_action", new_callable=AsyncMock
    ), patch(
        "app.utils.connector_crypto.encrypt_password", return_value="gAAAA-encrypted-blob"
    ):
        mock_settings.connector_encryption_key = "test-fernet-key"
        result = await upsert_connector_credentials(
            body=body, request=_mock_request(), clinic_id="default", user=admin
        )

    assert result["success"] is True
    inserted = mock_table.insert.call_args[0][0]
    assert inserted["config"]["password_encrypted"] == "gAAAA-encrypted-blob"
    assert "password" not in inserted["config"]
    assert "supersecretpassword" not in str(result)


@pytest.mark.asyncio
async def test_upsert_connector_missing_encryption_key_returns_500():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    fake_clinic = {"id": "clinic-2", "plan": "diagstream", "whatsapp_number": "+912222222222"}

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.eq.return_value.is_.return_value.execute.return_value = MagicMock(
        data=[]
    )

    body = ConnectorCredentialsUpdate(username="labadmin", password="supersecretpassword")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ), patch("app.routers.admin.supabase", mock_sb), patch(
        "app.routers.admin.settings"
    ) as mock_settings:
        mock_settings.connector_encryption_key = None
        with pytest.raises(HTTPException) as exc:
            await upsert_connector_credentials(
                body=body, request=_mock_request(), clinic_id="default", user=admin
            )
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_upsert_connector_empty_password_does_not_clobber_stored():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    fake_clinic = {"id": "clinic-2", "plan": "diagstream", "whatsapp_number": "+912222222222"}

    existing_row = {
        "id": "conn-1",
        "clinic_id": "clinic-2",
        "branch_id": None,
        "connector_type": "mocdoc",
        "config": {"username": "labadmin", "password_encrypted": "existing-encrypted-blob"},
        "is_enabled": True,
    }

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.eq.return_value.is_.return_value.execute.return_value = MagicMock(
        data=[existing_row]
    )
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[existing_row]
    )

    body = ConnectorCredentialsUpdate(password="", base_url="https://new-url.example.com")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ), patch("app.routers.admin.supabase", mock_sb), patch(
        "app.routers.admin.log_admin_action", new_callable=AsyncMock
    ):
        await upsert_connector_credentials(
            body=body, request=_mock_request(), clinic_id="default", user=admin
        )

    sent_config = mock_table.update.call_args[0][0]["config"]
    assert sent_config["password_encrypted"] == "existing-encrypted-blob"
    assert sent_config["base_url"] == "https://new-url.example.com"


@pytest.mark.asyncio
async def test_upsert_connector_rejects_branch_not_owned_by_clinic():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    fake_clinic = {"id": "clinic-2", "plan": "diagstream", "whatsapp_number": "+912222222222"}

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[]
    )

    body = ConnectorCredentialsUpdate(branch_id="branch-not-mine", username="labadmin")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ), patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await upsert_connector_credentials(
                body=body, request=_mock_request(), clinic_id="default", user=admin
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_toggle_connector_cross_tenant_forbidden():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"clinic_id": "clinic-OTHER"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await toggle_connector(
                connector_id="conn-1", body=ConnectorToggle(is_enabled=True), user=admin
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_toggle_connector_own_clinic_succeeds():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"clinic_id": "clinic-2"}]
    )
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "conn-1", "clinic_id": "clinic-2", "connector_type": "mocdoc", "config": {}, "is_enabled": True}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        result = await toggle_connector(
            connector_id="conn-1", body=ConnectorToggle(is_enabled=True), user=admin
        )

    assert result["message"] == "Connector enabled"


@pytest.mark.asyncio
async def test_audit_log_cross_tenant_forbidden():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
        data={"clinic_id": "clinic-OTHER", "connector_type": "mocdoc", "branch_id": None}
    )

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await get_connector_audit_log(connector_id="conn-1", limit=20, user=admin)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_failed_reports_scoped_to_own_clinic():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.is_.return_value.order.return_value.execute.return_value = MagicMock(
        data=[{"id": "fail-1", "clinic_id": "clinic-2", "patient_name": "Alice", "last_error": "timeout"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        result = await get_connector_failed_reports(clinic_id="default", user=admin)

    assert result["failed_reports"][0]["patient_name"] == "Alice"


@pytest.mark.asyncio
async def test_resolve_failed_report_cross_tenant_forbidden():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")

    with pytest.raises(HTTPException) as exc:
        await resolve_connector_failed_report(
            failed_report_id="fail-1", clinic_id="clinic-999", user=admin
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_resolve_failed_report_own_clinic_succeeds():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "fail-1", "clinic_id": "clinic-2", "resolved_at": "2026-08-10T00:00:00"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        result = await resolve_connector_failed_report(
            failed_report_id="fail-1", clinic_id="default", user=admin
        )

    assert result["success"] is True


@pytest.mark.asyncio
async def test_get_connectors_allows_diagnostic_operator_staff():
    """A staff account with CONNECTOR_MANAGE (e.g. DIAGNOSTIC_OPERATOR role)
    must be able to list connectors — require_admin previously 403'd every
    staff account unconditionally, making the CONNECTOR_MANAGE grant dead."""
    staff = AdminUser(
        "diag_op", role="staff", clinic_id="clinic-3", user_id="user-3",
        permissions=["REPORTS_VIEW", "REPORTS_RESOLVE", "CONNECTOR_MANAGE"],
    )
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.order.return_value.execute.return_value = MagicMock(data=[])

    with patch("app.routers.admin.supabase", mock_sb):
        result = await get_connectors(clinic_id="default", user=staff)

    assert result == {"connectors": []}


@pytest.mark.asyncio
async def test_get_connectors_rejects_staff_without_connector_manage():
    """A staff account without CONNECTOR_MANAGE must still be rejected."""
    staff = AdminUser(
        "front_desk", role="staff", clinic_id="clinic-3", user_id="user-4",
        permissions=["REPORTS_VIEW"],
    )
    # When evaluated through require_permission or direct gate
    dep = require_permission("CONNECTOR_MANAGE")
    with pytest.raises(HTTPException) as exc:
        await dep(user=staff)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_connector_manage_dependency_rejects_missing_permission():
    dep = require_permission("CONNECTOR_MANAGE")
    staff = AdminUser("x", role="staff", clinic_id="clinic-3", user_id="user-5", permissions=[])
    with pytest.raises(HTTPException) as exc:
        await dep(user=staff)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_test_connector_calls_dry_run():
    from app.routers.admin import test_connector, _connector_tasks

    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "conn-1", "clinic_id": "clinic-2", "branch_id": None, "connector_type": "mocdoc"}]
    )

    fake_summary = {"run_status": "dry_run", "reports_found": 3, "error_message": None}

    with patch("app.routers.admin.supabase", mock_sb), \
         patch("connectors.runner.run_connector", new_callable=AsyncMock, return_value=fake_summary) as mock_run:
        result = await test_connector(connector_id="conn-1", clinic_id="default", user=admin)
        # Endpoint returns immediately with "running" status
        assert result["status"] == "running"

        # Let the background task complete
        await asyncio.sleep(0.1)

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["dry_run"] is True
    # Background task should have stored the result
    task = _connector_tasks.get("conn-1", {})
    assert task.get("status") == "done"
    assert task["result"]["reports_found"] == 3


@pytest.mark.asyncio
async def test_run_connector_now_calls_run_connector_not_dry_run():
    from app.routers.admin import run_connector_now, _connector_tasks

    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "conn-1", "clinic_id": "clinic-2", "branch_id": None, "connector_type": "mocdoc"}]
    )

    fake_summary = {"run_status": "success", "reports_uploaded": 2, "error_message": None}

    with patch("app.routers.admin.supabase", mock_sb), \
         patch("connectors.runner.run_connector", new_callable=AsyncMock, return_value=fake_summary) as mock_run:
        result = await run_connector_now(connector_id="conn-1", clinic_id="default", user=admin)
        # Endpoint returns immediately with "running" status
        assert result["status"] == "running"

        # Let the background task complete
        await asyncio.sleep(0.1)

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["dry_run"] is False
    # Background task should have stored the result
    task = _connector_tasks.get("conn-1", {})
    assert task.get("status") == "done"
    assert task["result"]["reports_uploaded"] == 2


@pytest.mark.asyncio
async def test_upsert_connector_normalizes_base_url():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    fake_clinic = {"id": "clinic-2", "plan": "diagstream", "whatsapp_number": "+911111111111"}

    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "conn-1", "config": {}, "is_enabled": True}]
    )
    mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "conn-1", "clinic_id": "clinic-2", "connector_type": "mocdoc", "config": {"base_url": "https://www.mocdoc.com"}}]
    )

    body = ConnectorCredentialsUpdate(base_url="www.mocdoc.com/", username="admin")

    with patch("app.routers.admin.supabase", mock_sb), \
         patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock):
        result = await upsert_connector_credentials(body=body, request=_mock_request(), clinic_id="default", user=admin)

    update_call = mock_sb.table.return_value.update.call_args[0][0]
    assert update_call["config"]["base_url"] == "https://www.mocdoc.com"


def test_mocdoc_worker_normalizes_base_url():
    from connectors.mocdoc.worker import MocDocConnector

    worker = MocDocConnector(
        clinic_id="c-1",
        config={"base_url": "www.mocdoc.com/"},
        medassist_url="http://localhost:8000",
        integration_secret="secret",
        session_dir="/tmp",
    )
    assert worker.base_url == "https://www.mocdoc.com"

    worker_with_path = MocDocConnector(
        clinic_id="c-1",
        config={"base_url": "https://mocdoc.com/user/loginform"},
        medassist_url="http://localhost:8000",
        integration_secret="secret",
        session_dir="/tmp",
    )
    assert worker_with_path.base_url == "https://mocdoc.com"


def test_audit_log_uses_connector_manage_permission_not_require_admin():
    """Regression guard: the audit-log route's dependency must match every
    other /admin/connectors/* endpoint (require_permission), not
    require_admin, which 403s every staff account unconditionally."""
    import inspect
    from app.routers.admin import get_connector_audit_log

    source = inspect.getsource(get_connector_audit_log)
    assert "require_admin" not in source
    assert 'require_permission("CONNECTOR_MANAGE")' in source


@pytest.mark.asyncio
async def test_audit_log_allows_diagnostic_operator_staff():
    """A staff account with CONNECTOR_MANAGE (e.g. DIAGNOSTIC_OPERATOR role)
    must be able to load run history — this was 403ing before the fix."""
    from app.routers.admin import get_connector_audit_log

    staff = AdminUser(
        "diag_op", role="staff", clinic_id="clinic-3", user_id="user-3",
        permissions=["REPORTS_VIEW", "REPORTS_RESOLVE", "CONNECTOR_MANAGE"],
    )
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
        data={"clinic_id": "clinic-3", "connector_type": "mocdoc", "branch_id": None}
    )
    mock_table.select.return_value.eq.return_value.eq.return_value.is_.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[{"id": "log-1", "run_status": "success", "reports_found": 2}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        result = await get_connector_audit_log(connector_id="conn-1", limit=20, user=staff)

    assert result["audit_log"][0]["id"] == "log-1"


@pytest.mark.asyncio
async def test_get_connector_types_returns_mocdoc_schema():
    from app.routers.admin import get_connector_types

    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    result = await get_connector_types(user=admin)

    types_by_key = {t["type"]: t for t in result["types"]}
    assert "mocdoc" in types_by_key
    assert types_by_key["mocdoc"]["display_name"] == "MocDoc"
    schema_keys = [f["key"] for f in types_by_key["mocdoc"]["schema"]]
    assert schema_keys == ["username", "password", "clinic_slug", "base_url"]




