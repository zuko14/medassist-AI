"""Unit & Integration tests for Platform Owner Broadcast Messaging and Clinic Admin In-App Notifications."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.broadcast import BroadcastService, broadcast_service

client = TestClient(app)


def get_owner_auth_header(username: str = None, password: str = None) -> dict:
    u = username or settings.owner_username or "test_owner"
    p = password or settings.owner_password or "test_owner_password_12345"
    creds = f"{u}:{p}"
    encoded = base64.b64encode(creds.encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {encoded}"}


def get_admin_auth_header(username: str = "admin", password: str = "admin123") -> dict:
    creds = f"{username}:{password}"
    encoded = base64.b64encode(creds.encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {encoded}"}


# ─── 1. Authentication & RBAC Guards ─────────────────────────────────────────


def test_broadcast_unauthorized_without_header():
    res = client.post(
        "/platform/broadcasts",
        json={"title": "Test Title", "message": "Test Message", "target_type": "ALL"},
    )
    assert res.status_code == 401


def test_broadcast_unauthorized_with_wrong_credentials():
    headers = get_owner_auth_header(username="wrong_owner", password="wrong_password")
    res = client.post(
        "/platform/broadcasts",
        json={"title": "Test Title", "message": "Test Message", "target_type": "ALL"},
        headers=headers,
    )
    assert res.status_code == 401


# ─── 2. Broadcast Creation & Dispatch ────────────────────────────────────────


@patch("app.routers.platform.log_admin_action")
@patch("app.services.broadcast.BroadcastService._dispatch_notifications")
@patch("app.services.broadcast.supabase")
def test_create_broadcast_all_clinics(mock_supabase, mock_dispatch, mock_log_action):
    mock_broadcast_row = {
        "id": "bc-1234",
        "sender_id": "test_owner",
        "title": "System Maintenance Notice",
        "message": "Scheduled maintenance tonight at 2 AM IST.",
        "target_type": "ALL",
        "target_clinic_ids": [],
        "recipient_count": 0,
        "created_at": "2026-08-17T10:00:00Z",
    }
    mock_supabase.table.return_value.insert.return_value.execute.return_value.data = [
        mock_broadcast_row
    ]

    headers = get_owner_auth_header()
    res = client.post(
        "/platform/broadcasts",
        json={
            "title": "System Maintenance Notice",
            "message": "Scheduled maintenance tonight at 2 AM IST.",
            "target_type": "ALL",
        },
        headers=headers,
    )

    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["broadcast"]["id"] == "bc-1234"
    assert data["broadcast"]["title"] == "System Maintenance Notice"


@patch("app.routers.platform.log_admin_action")
@patch("app.services.broadcast.BroadcastService._dispatch_notifications")
@patch("app.services.broadcast.supabase")
def test_create_broadcast_selective(mock_supabase, mock_dispatch, mock_log_action):
    mock_broadcast_row = {
        "id": "bc-5678",
        "sender_id": "test_owner",
        "title": "Billing Alert",
        "message": "Please update payment credentials.",
        "target_type": "SELECTIVE",
        "target_clinic_ids": ["clinic-a", "clinic-b"],
        "recipient_count": 0,
        "created_at": "2026-08-17T10:00:00Z",
    }
    mock_supabase.table.return_value.insert.return_value.execute.return_value.data = [
        mock_broadcast_row
    ]

    headers = get_owner_auth_header()
    res = client.post(
        "/platform/broadcasts",
        json={
            "title": "Billing Alert",
            "message": "Please update payment credentials.",
            "target_type": "SELECTIVE",
            "target_clinic_ids": ["clinic-a", "clinic-b"],
        },
        headers=headers,
    )

    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["broadcast"]["target_type"] == "SELECTIVE"
    assert data["broadcast"]["target_clinic_ids"] == ["clinic-a", "clinic-b"]


def test_create_broadcast_validation_failures():
    headers = get_owner_auth_header()

    # Empty title
    res = client.post(
        "/platform/broadcasts",
        json={"title": "", "message": "Test Message", "target_type": "ALL"},
        headers=headers,
    )
    assert res.status_code == 422

    # Selective with empty clinic IDs
    res = client.post(
        "/platform/broadcasts",
        json={"title": "Test", "message": "Test Message", "target_type": "SELECTIVE", "target_clinic_ids": []},
        headers=headers,
    )
    assert res.status_code == 400


# ─── 3. Broadcast History & Delivery Metrics ─────────────────────────────────


@patch("app.routers.platform.log_admin_action")
@patch("app.services.broadcast.supabase")
def test_list_broadcasts(mock_supabase, mock_log_action):
    mock_broadcasts = [
        {
            "id": "bc-1",
            "sender_id": "owner",
            "title": "Alert 1",
            "message": "Msg 1",
            "target_type": "ALL",
            "recipient_count": 5,
            "created_at": "2026-08-17T10:00:00Z",
        }
    ]
    mock_notifs = [
        {"id": "n-1", "is_read": True},
        {"id": "n-2", "is_read": False},
    ]

    mock_b_table = MagicMock()
    mock_b_table.select.return_value.order.return_value.range.return_value.execute.return_value.data = mock_broadcasts

    mock_n_table = MagicMock()
    mock_n_table.select.return_value.eq.return_value.execute.return_value.data = mock_notifs

    def table_router(t):
        if t == "broadcasts":
            return mock_b_table
        if t == "admin_notifications":
            return mock_n_table
        return MagicMock()

    mock_supabase.table.side_effect = table_router

    headers = get_owner_auth_header()
    res = client.get("/platform/broadcasts", headers=headers)

    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert len(data["broadcasts"]) == 1
    assert data["broadcasts"][0]["delivered_count"] == 2
    assert data["broadcasts"][0]["read_count"] == 1


# ─── 4. Clinic Admin Notifications & Read Status ─────────────────────────────


def test_admin_get_notifications():
    mock_notifs = [
        {
            "id": "notif-1",
            "broadcast_id": "bc-1",
            "clinic_id": "clinic-123",
            "admin_id": "admin-1",
            "title": "Maintenance Notice",
            "message": "Downtime at midnight.",
            "is_read": False,
            "created_at": "2026-08-17T10:00:00Z",
        }
    ]

    from app.routers.admin import AdminUser, verify_credentials
    mock_user = AdminUser(username="clinic_admin_user", role="clinic_admin", clinic_id="clinic-123", user_id="admin-1")

    app.dependency_overrides[verify_credentials] = lambda: mock_user

    try:
        with patch.object(broadcast_service, "get_admin_notifications", return_value=mock_notifs):
            res = client.get("/admin/notifications")
            assert res.status_code == 200
            data = res.json()
            assert data["success"] is True
            assert len(data["notifications"]) == 1
            assert data["notifications"][0]["title"] == "Maintenance Notice"
    finally:
        app.dependency_overrides.pop(verify_credentials, None)


def test_admin_get_unread_count():
    from app.routers.admin import AdminUser, verify_credentials
    mock_user = AdminUser(username="clinic_admin_user", role="clinic_admin", clinic_id="clinic-123", user_id="admin-1")

    app.dependency_overrides[verify_credentials] = lambda: mock_user

    try:
        with patch.object(broadcast_service, "get_unread_count", return_value=3):
            res = client.get("/admin/notifications/unread-count")
            assert res.status_code == 200
            data = res.json()
            assert data["success"] is True
            assert data["unread_count"] == 3
    finally:
        app.dependency_overrides.pop(verify_credentials, None)


def test_admin_mark_notification_read():
    from app.routers.admin import AdminUser, verify_credentials
    mock_user = AdminUser(username="clinic_admin_user", role="clinic_admin", clinic_id="clinic-123", user_id="admin-1")

    app.dependency_overrides[verify_credentials] = lambda: mock_user

    try:
        with patch.object(broadcast_service, "mark_notification_read", return_value=True):
            res = client.patch("/admin/notifications/notif-1/read")
            assert res.status_code == 200
            data = res.json()
            assert data["success"] is True
            assert data["message"] == "Notification marked as read"
    finally:
        app.dependency_overrides.pop(verify_credentials, None)


def test_admin_mark_all_notifications_read():
    from app.routers.admin import AdminUser, verify_credentials
    mock_user = AdminUser(username="clinic_admin_user", role="clinic_admin", clinic_id="clinic-123", user_id="admin-1")

    app.dependency_overrides[verify_credentials] = lambda: mock_user

    try:
        with patch.object(broadcast_service, "mark_all_notifications_read", return_value=5):
            res = client.post("/admin/notifications/mark-all-read")
            assert res.status_code == 200
            data = res.json()
            assert data["success"] is True
            assert data["updated_count"] == 5
    finally:
        app.dependency_overrides.pop(verify_credentials, None)

