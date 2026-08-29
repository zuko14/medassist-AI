"""Owner-dashboard roster counts and plan feature matrix.

Covers the two things the platform owner prices plans off:
  1. GET /platform/clinics — active doctors + distinct departments per hospital,
     including the >1000-row pagination path where a truncated scan would
     silently understate the largest hospitals.
  2. GET /platform/plan-tiers — bundled features per tier, read from the same
     PLAN_FEATURES registry has_feature() gates the bot on.
"""

import base64
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.tenant import ALL_FEATURES, FEATURE_LABELS, PLAN_FEATURES

client = TestClient(app)


def _auth() -> dict:
    creds = f"{settings.owner_username}:{settings.owner_password}"
    return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}


def _paged(rows):
    """Serve rows through .range(start, end) the way PostgREST does."""

    def _range(start, end):
        res = MagicMock()
        res.execute.return_value.data = rows[start : end + 1]
        return res

    return _range


# -- 1. Roster counts on the fleet leaderboard --------------------------------


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_leaderboard_reports_doctor_and_department_counts(mock_supabase, _log):
    doctors = [
        {"clinic_id": "c1", "department": "Cardiology", "is_active": True},
        {"clinic_id": "c1", "department": "Cardiology", "is_active": True},
        {"clinic_id": "c1", "department": "Orthopaedics", "is_active": True},
        {"clinic_id": "c1", "department": "Neurology", "is_active": None},  # NULL = active
        {"clinic_id": "c1", "department": "Dermatology", "is_active": False},  # excluded
        {"clinic_id": "c2", "department": "  ", "is_active": True},  # blank dept, still a doctor
        {"clinic_id": None, "department": "Orphan", "is_active": True},  # no tenant, skipped
    ]

    def table_router(name):
        m = MagicMock()
        if name == "clinics":
            m.select.return_value.execute.return_value.data = [
                {
                    "id": "c1", "name": "Alpha", "whatsapp_number": "+91999",
                    "plan": "polyclinic", "is_active": True,
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "id": "c2", "name": "Beta", "whatsapp_number": "+91888",
                    "plan": "soloclinic", "is_active": True,
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ]
        elif name == "doctors":
            m.select.return_value.range.side_effect = _paged(doctors)
        elif name == "appointments":
            m.select.return_value.eq.return_value.gte.return_value.execute.return_value.data = []
        elif name == "patients":
            r = m.select.return_value.eq.return_value.execute.return_value
            r.count = 0
            r.data = []
        return m

    mock_supabase.table.side_effect = table_router

    res = client.get("/platform/clinics", headers=_auth())
    assert res.status_code == 200
    by_id = {c["id"]: c for c in res.json()["clinics"]}

    # 4 active doctors: the is_active=False row is excluded, the NULL row counted.
    assert by_id["c1"]["doctors_count"] == 4
    assert by_id["c1"]["departments_count"] == 3
    assert by_id["c1"]["departments"] == ["Cardiology", "Neurology", "Orthopaedics"]

    # A doctor with a blank department still counts as a doctor, not a department.
    assert by_id["c2"]["doctors_count"] == 1
    assert by_id["c2"]["departments_count"] == 0
    assert by_id["c2"]["departments"] == []


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_roster_scan_pages_past_the_1000_row_cap(mock_supabase, _log):
    """A single unbounded select stops at 1000 and understates big hospitals."""
    doctors = [
        {"clinic_id": "c1", "department": f"Dept{i % 40}", "is_active": True}
        for i in range(2350)
    ]

    def table_router(name):
        m = MagicMock()
        if name == "clinics":
            m.select.return_value.execute.return_value.data = [
                {
                    "id": "c1", "name": "Mega", "whatsapp_number": "+91777",
                    "plan": "enterprise", "is_active": True,
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ]
        elif name == "doctors":
            m.select.return_value.range.side_effect = _paged(doctors)
        elif name == "appointments":
            m.select.return_value.eq.return_value.gte.return_value.execute.return_value.data = []
        elif name == "patients":
            r = m.select.return_value.eq.return_value.execute.return_value
            r.count = 0
            r.data = []
        return m

    mock_supabase.table.side_effect = table_router

    c0 = client.get("/platform/clinics", headers=_auth()).json()["clinics"][0]
    assert c0["doctors_count"] == 2350
    assert c0["departments_count"] == 40


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_leaderboard_survives_roster_query_failure(mock_supabase, _log):
    """Roster counts are a sizing aid — losing them must not 500 the fleet view."""

    def table_router(name):
        m = MagicMock()
        if name == "clinics":
            m.select.return_value.execute.return_value.data = [
                {
                    "id": "c1", "name": "Alpha", "whatsapp_number": "+91999",
                    "plan": "essential", "is_active": True,
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ]
        elif name == "doctors":
            m.select.return_value.range.side_effect = RuntimeError("doctors table unreachable")
        elif name == "appointments":
            m.select.return_value.eq.return_value.gte.return_value.execute.return_value.data = []
        elif name == "patients":
            r = m.select.return_value.eq.return_value.execute.return_value
            r.count = 3
            r.data = []
        return m

    mock_supabase.table.side_effect = table_router

    res = client.get("/platform/clinics", headers=_auth())
    assert res.status_code == 200
    c0 = res.json()["clinics"][0]
    assert c0["doctors_count"] == 0
    assert c0["departments_count"] == 0
    assert c0["patients_count"] == 3  # the rest of the row is intact


# -- 2. Plan feature matrix ---------------------------------------------------


def test_every_plan_feature_has_a_display_label():
    """A feature added to PLAN_FEATURES without a label renders as a raw slug."""
    missing = [f for f in ALL_FEATURES if f not in FEATURE_LABELS]
    assert not missing, f"FEATURE_LABELS missing: {missing}"
    stale = [f for f in FEATURE_LABELS if f not in ALL_FEATURES]
    assert not stale, f"FEATURE_LABELS has unknown features: {stale}"


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_plan_tiers_expose_bundled_features_and_adoption(mock_supabase, _log):
    tiers = [
        {
            "plan_name": "soloclinic", "display_name": "Solo Clinic",
            "monthly_price_paise": 0, "included_messages_month": 500, "is_active": True,
        },
        {
            "plan_name": "polyclinic", "display_name": "PolyClinic",
            "monthly_price_paise": 500000, "included_messages_month": 5000, "is_active": True,
        },
        {
            "plan_name": "enterprise", "display_name": "Enterprise",
            "monthly_price_paise": 0, "included_messages_month": 0, "is_active": True,
        },
    ]

    def table_router(name):
        m = MagicMock()
        if name == "plan_tiers":
            m.select.return_value.order.return_value.execute.return_value.data = tiers
        elif name == "clinics":
            m.select.return_value.execute.return_value.data = [
                {"plan": "polyclinic", "is_active": True},
                {"plan": "polyclinic", "is_active": True},
                {"plan": "soloclinic", "is_active": True},
                {"plan": "soloclinic", "is_active": False},  # inactive not counted
            ]
        return m

    mock_supabase.table.side_effect = table_router

    res = client.get("/platform/plan-tiers", headers=_auth())
    assert res.status_code == 200
    data = res.json()

    assert data["all_features"] == ALL_FEATURES
    assert data["feature_labels"] == FEATURE_LABELS

    by_plan = {t["plan_name"]: t for t in data["plan_tiers"]}

    # Features mirror the registry the bot itself gates on.
    assert set(by_plan["polyclinic"]["features"]) == PLAN_FEATURES["polyclinic"]
    assert by_plan["polyclinic"]["includes_all_features"] is False

    # Enterprise's "*" wildcard expands to the full list, never leaks as "*".
    assert by_plan["enterprise"]["includes_all_features"] is True
    assert by_plan["enterprise"]["features"] == ALL_FEATURES
    assert "*" not in by_plan["enterprise"]["features"]

    # soloclinic has booking but not lab_reports — the pricing distinction.
    assert "booking" in by_plan["soloclinic"]["features"]
    assert "lab_reports" not in by_plan["soloclinic"]["features"]

    # Live adoption, active hospitals only.
    assert by_plan["polyclinic"]["clinics_count"] == 2
    assert by_plan["soloclinic"]["clinics_count"] == 1
    assert by_plan["enterprise"]["clinics_count"] == 0
