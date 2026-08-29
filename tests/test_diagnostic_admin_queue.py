"""Tests for Diagnostic Center Admin Queue, Triage Resolution, and Stats Endpoints."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException

from app.routers.admin import (
    AdminUser,
    ResolveMatchRequest,
    ResendReportRequest,
    get_diagnostic_reports_queue,
    resolve_report_match,
    resend_lab_report,
    get_diagnostic_stats,
)


@pytest.mark.asyncio
async def test_get_diagnostic_reports_queue():
    """Verify queue endpoint returns needs_review, failed reports, and connector failures."""
    admin = AdminUser("diag_staff", role="clinic_admin", clinic_id="clinic-diag-1", user_id="user-diag-1")

    mock_sb = MagicMock()
    mock_lab_table = MagicMock()
    mock_conn_table = MagicMock()

    def table_side_effect(name):
        if name == "lab_reports":
            return mock_lab_table
        elif name == "connector_failed_reports":
            return mock_conn_table
        return MagicMock()

    mock_sb.table.side_effect = table_side_effect

    # Mock lab_reports data
    mock_lab_table.select.return_value.eq.return_value.in_.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "lr-1",
                "clinic_id": "clinic-diag-1",
                "patient_phone": "+919876543210",
                "patient_name": "Ravi Kumar",
                "status": "needs_review",
                "error_message": "Patient name mismatch with registered patient",
            },
            {
                "id": "lr-2",
                "clinic_id": "clinic-diag-1",
                "patient_phone": "+919876543211",
                "patient_name": "Sita Devi",
                "status": "failed",
                "error_message": "WhatsApp rejected message",
            },
        ]
    )

    # Mock connector_failed_reports data
    mock_conn_table.select.return_value.is_.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "cfr-1",
                "clinic_id": "clinic-diag-1",
                "vam_id": "VAM-101",
                "external_report_id": "DOC-99",
                "last_error": "MocDoc download timeout",
            }
        ]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        res = await get_diagnostic_reports_queue(clinic_id="clinic-diag-1", user=admin)

    assert len(res["needs_review"]) == 1
    assert res["needs_review"][0]["id"] == "lr-1"
    assert len(res["failed_reports"]) == 1
    assert res["failed_reports"][0]["id"] == "lr-2"
    assert len(res["connector_failures"]) == 1
    assert res["connector_failures"][0]["id"] == "cfr-1"
    assert res["total_queued"] == 3


@pytest.mark.asyncio
async def test_resolve_report_match_valid():
    """Verify resolving a match updates phone, match_source, and records audit log."""
    admin = AdminUser("diag_staff", role="clinic_admin", clinic_id="clinic-diag-1", user_id="user-diag-1")
    req = MagicMock()
    req.client.host = "127.0.0.1"

    body = ResolveMatchRequest(
        patient_phone="9876543210",
        patient_name="Ravi Kumar Corrected",
        send_now=False,
    )

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table

    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "lr-1",
                "clinic_id": "clinic-diag-1",
                "patient_phone": "+910000000000",
                "patient_name": "Ravi Kumar",
                "status": "needs_review",
            }
        ]
    )
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{}])
    mock_table.insert.return_value.execute.return_value = MagicMock(data=[{}])

    with patch("app.routers.admin.supabase", mock_sb), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock) as mock_audit:
        res = await resolve_report_match(
            report_id="lr-1",
            body=body,
            clinic_id="clinic-diag-1",
            request=req,
            user=admin,
        )

    assert res["success"] is True
    assert res["patient_phone"] == "+919876543210"
    assert res["status"] == "matched"
    mock_audit.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_report_match_invalid_phone():
    """Verify resolving with an invalid phone fails with HTTP 400."""
    admin = AdminUser("diag_staff", role="clinic_admin", clinic_id="clinic-diag-1", user_id="user-diag-1")

    body = ResolveMatchRequest(
        patient_phone="123",  # invalid
        patient_name="Ravi Kumar",
        send_now=False,
    )

    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "lr-1", "clinic_id": "clinic-diag-1"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc_info:
            await resolve_report_match(
                report_id="lr-1",
                body=body,
                clinic_id="clinic-diag-1",
                user=admin,
            )
        assert exc_info.value.status_code == 400


class _FakeQuery:
    """Chainable stand-in for a PostgREST query builder.

    get_diagnostic_stats now filters "today" in SQL and counts the open
    triage queues server-side with count="exact", so the test has to model
    both a row-returning query and a counting one.
    """

    def __init__(self, rows=None, count=None):
        self._rows = rows or []
        self._count = count

    def select(self, *a, **k):
        if k.get("count") == "exact":
            return _FakeQuery(count=self._count)
        return self

    @property
    def not_(self):
        # postgrest exposes `not_` as a property, not a call.
        return self

    def __getattr__(self, _name):
        # eq / gte / lte / is_ / order / limit all just chain.
        def chain(*a, **k):
            return self
        return chain

    def execute(self):
        return SimpleNamespace(data=self._rows, count=self._count)


@pytest.mark.asyncio
async def test_get_diagnostic_stats():
    """Verify diagnostic stats aggregation endpoint."""
    admin = AdminUser("diag_staff", role="clinic_admin", clinic_id="clinic-diag-1", user_id="user-diag-1")

    # Today's rows: one delivered WITH its summary, one delivered WITHOUT.
    today_rows = [
        {"id": "1", "status": "sent", "uploaded_at": "2099-01-01T10:00:00Z",
         "ai_summary": "All normal.", "ai_summary_sent": True},
        {"id": "2", "status": "sent", "uploaded_at": "2099-01-01T11:00:00Z",
         "ai_summary": "All normal.", "ai_summary_sent": False},
        {"id": "3", "status": "failed", "uploaded_at": "2099-01-01T12:00:00Z",
         "ai_summary": None, "ai_summary_sent": False},
    ]

    mock_sb = MagicMock()
    mock_conn_table = MagicMock()

    def table_side_effect(name):
        if name == "lab_reports":
            # Row query returns today's rows; count queries return 1.
            return _FakeQuery(rows=today_rows, count=1)
        if name == "connector_failed_reports":
            # The Failed Deliveries Queue the tile must now agree with.
            return _FakeQuery(rows=[], count=51)
        if name == "integration_connectors":
            return mock_conn_table
        return _FakeQuery()

    mock_sb.table.side_effect = table_side_effect

    mock_conn_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "conn-1",
                "connector_type": "mocdoc",
                "is_enabled": True,
                "last_run_at": "2099-01-01T12:00:00Z",
                "last_success_at": "2099-01-01T12:00:00Z",
                "last_error": None,
                "config": {"poll_interval_minutes": 10},
            }
        ]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        stats = await get_diagnostic_stats(clinic_id="clinic-diag-1", user=admin)

    rep = stats["reports_today"]
    assert rep["total"] == 3
    assert rep["sent"] == 2
    assert rep["needs_review"] == 1
    # "Delivery Failures" must include connector-stage failures, otherwise the
    # tile reads 0 directly above a queue listing 51 of them.
    assert rep["connector_failures_open"] == 51
    assert rep["failed"] == 52
    assert rep["failed_today"] == 1
    # Only one of the two delivered reports actually carried its summary.
    assert rep["ai_summary_delivered"] == 1
    assert rep["ai_summary_missing"] == 1
    assert stats["connector"]["health"] == "healthy"
    assert stats["connector"]["poll_interval_minutes"] == 10
    assert stats["connector"]["next_run_at"] is not None


@pytest.mark.asyncio
async def test_get_diagnostic_stats_picks_most_recently_updated_connector():
    """When no branch_id is given and multiple connector rows exist (one
    per branch), the dashboard must surface the most recently active one
    instead of an arbitrary/stale row — regression guard for the dashboard
    showing a dead connector's old Chromium error after a working one was
    configured for a different branch."""
    admin = AdminUser("diag_staff", role="clinic_admin", clinic_id="clinic-diag-1", user_id="user-diag-1")

    mock_sb = MagicMock()
    mock_lab_table = MagicMock()
    mock_conn_table = MagicMock()

    def table_side_effect(name):
        if name == "lab_reports":
            return mock_lab_table
        elif name == "integration_connectors":
            return mock_conn_table
        return MagicMock()

    mock_sb.table.side_effect = table_side_effect
    mock_lab_table.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    mock_conn_table.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[
            {
                "id": "conn-old",
                "branch_id": "branch-old",
                "connector_type": "mocdoc",
                "is_enabled": False,
                "last_run_at": "2026-01-01T00:00:00Z",
                "last_success_at": None,
                "last_error": "RuntimeError: Chromium browser is not installed",
                "config": {},
                "updated_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "conn-new",
                "branch_id": "branch-new",
                "connector_type": "mocdoc",
                "is_enabled": True,
                "last_run_at": "2099-01-01T12:00:00Z",
                "last_success_at": "2099-01-01T12:00:00Z",
                "last_error": None,
                "config": {"poll_interval_minutes": 5},
                "updated_at": "2099-01-01T12:00:00Z",
            },
        ]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        stats = await get_diagnostic_stats(clinic_id="clinic-diag-1", user=admin)

    assert stats["connector"]["id"] == "conn-new"
    assert stats["connector"]["health"] == "healthy"
    assert stats["connector"]["branch_id"] == "branch-new"

