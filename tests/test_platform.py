"""Unit & Integration tests for Platform Owner / Super-Admin router and endpoints."""

import base64
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)


def get_owner_auth_header(username: str = None, password: str = None) -> dict:
    u = username or settings.owner_username
    p = password or settings.owner_password
    creds = f"{u}:{p}"
    encoded = base64.b64encode(creds.encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {encoded}"}


def test_platform_auth_failure_missing_header():
    response = client.get("/platform/overview")
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


def test_platform_auth_failure_invalid_credentials():
    headers = get_owner_auth_header("invalid_user", "invalid_pass")
    response = client.get("/platform/overview", headers=headers)
    assert response.status_code == 401


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_platform_overview_success(mock_supabase, mock_log_action):
    # Mock supabase tables
    mock_clinics = MagicMock()
    mock_clinics.execute.return_value.data = [
        {"id": "c1", "name": "Hospital A", "whatsapp_number": "+911", "plan": "essential", "is_active": True, "created_at": "2026-01-01T00:00:00Z"},
        {"id": "c2", "name": "Hospital B", "whatsapp_number": "+912", "plan": "enterprise", "is_active": False, "created_at": "2026-01-02T00:00:00Z"},
    ]

    mock_patients = MagicMock()
    mock_patients.execute.return_value.count = 42
    mock_patients.execute.return_value.data = []

    mock_appts = MagicMock()
    mock_appts.execute.return_value.count = 105
    mock_appts.execute.return_value.data = []

    def table_router(table_name):
        mock_obj = MagicMock()
        if table_name == "clinics":
            mock_obj.select.return_value = mock_clinics
        elif table_name == "patients":
            mock_obj.select.return_value = mock_patients
        elif table_name == "appointments":
            mock_obj.select.return_value = mock_appts
        return mock_obj

    mock_supabase.table.side_effect = table_router

    headers = get_owner_auth_header()
    response = client.get("/platform/overview", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["total_clinics"] == 2
    assert data["active_clinics"] == 1
    assert data["inactive_clinics"] == 1
    assert data["clinics_by_plan"]["essential"] == 1
    assert data["clinics_by_plan"]["enterprise"] == 1


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_platform_clinics_leaderboard(mock_supabase, mock_log_action):
    mock_clinics = MagicMock()
    mock_clinics.execute.return_value.data = [
        {"id": "c1", "name": "Hospital Alpha", "whatsapp_number": "+919999911111", "plan": "essential", "is_active": True, "created_at": "2026-01-01T00:00:00Z"}
    ]

    mock_appts = MagicMock()
    mock_appts.execute.return_value.data = [
        {"id": "a1", "status": "confirmed", "amount_paise": 50000, "payment_id": "pay_123", "created_at": "2026-08-01T10:00:00Z"}
    ]

    mock_patients = MagicMock()
    mock_patients.execute.return_value.count = 10
    mock_patients.execute.return_value.data = []

    def table_router(table_name):
        mock_obj = MagicMock()
        if table_name == "clinics":
            mock_obj.select.return_value = mock_clinics
        elif table_name == "appointments":
            mock_obj.select.return_value.eq.return_value.gte.return_value = mock_appts
        elif table_name == "patients":
            mock_obj.select.return_value.eq.return_value = mock_patients
        return mock_obj

    mock_supabase.table.side_effect = table_router

    headers = get_owner_auth_header()
    response = client.get("/platform/clinics", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert len(data["clinics"]) == 1
    c0 = data["clinics"][0]
    assert c0["name"] == "Hospital Alpha"
    assert c0["revenue_inr_30d"] == 500.0
    assert c0["appointments_count_30d"] == 1


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_platform_department_analytics(mock_supabase, mock_log_action):
    mock_clinics = MagicMock()
    mock_clinics.execute.return_value.data = [
        {"id": "c1", "name": "Hospital Alpha"},
        {"id": "c2", "name": "Hospital Beta"},
    ]

    mock_appts = MagicMock()
    mock_appts.execute.return_value.data = [
        {"clinic_id": "c1", "department": "Cardiology", "created_at": "2026-08-01T09:00:00+00:00"},
        {"clinic_id": "c1", "department": "Cardiology", "created_at": "2026-08-01T09:30:00+00:00"},
        {"clinic_id": "c2", "department": "Orthopedics", "created_at": "2026-08-02T14:00:00+00:00"},
    ]

    def table_router(table_name):
        mock_obj = MagicMock()
        if table_name == "clinics":
            mock_obj.select.return_value = mock_clinics
        elif table_name == "appointments":
            mock_obj.select.return_value.gte.return_value = mock_appts
        return mock_obj

    mock_supabase.table.side_effect = table_router

    headers = get_owner_auth_header()
    response = client.get("/platform/departments", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["department_leaderboard"][0]["department"] == "Cardiology"
    assert data["department_leaderboard"][0]["count"] == 2
    hospital_c1 = next(h for h in data["hospital_departments"] if h["clinic_id"] == "c1")
    assert hospital_c1["clinic_name"] == "Hospital Alpha"
    assert hospital_c1["departments"][0]["department"] == "Cardiology"
    peak_hour_9 = next(h for h in data["peak_hours"] if h["hour"] == 9)
    assert peak_hour_9["count"] == 2
    assert len(data["peak_hours"]) == 24


def test_platform_department_analytics_requires_auth():
    response = client.get("/platform/departments")
    assert response.status_code == 401


def test_platform_create_clinic_requires_auth():
    response = client.post("/platform/clinics", json={
        "name": "Apex Diagnostic Labs",
        "whatsapp_number": "+919876543211",
        "meta_phone_number_id": "pid",
        "meta_access_token": "token",
    })
    assert response.status_code == 401


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.provision_clinic")
def test_platform_create_clinic_success(mock_provision, mock_log_action):
    mock_provision.return_value = {
        "success": True,
        "clinic": {"id": "clinic-new", "name": "Apex Diagnostic Labs"},
        "branches": None,
        "clinic_admin": {"username": "apexdiagabc123", "password": "generated-pw"},
    }

    headers = get_owner_auth_header()
    response = client.post(
        "/platform/clinics",
        headers=headers,
        json={
            "name": "Apex Diagnostic Labs",
            "whatsapp_number": "+919876543211",
            "plan": "diagstream",
            "meta_phone_number_id": "pid",
            "meta_access_token": "token",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["clinic"]["id"] == "clinic-new"
    assert data["clinic_admin"]["username"] == "apexdiagabc123"
    mock_provision.assert_called_once()
    mock_log_action.assert_called_once()


def test_platform_panel_static_route():
    response = client.get("/platform-panel")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Platform Owner Dashboard" in response.text
