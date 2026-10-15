"""D-23 / D-24 / D-25 (bug-remediation-register): read endpoints found by the KK-P4-01 hardening audit.

D-23: unbounded days / limit / offset are refused with 422 before any query runs
      (a huge `days` used to overflow timedelta -> 500).
D-24: /admin/stats no longer returns raw exception text in its 200 body.
D-25: /admin/payments/reconciliation validates date_str (422) and defaults to the IST date.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import admin
from app.routers.admin import AdminUser
from app.services import analytics, payment

CID = "6f1c2c56-0000-4000-8000-000000000001"
client = TestClient(app)


@pytest.fixture
def as_admin():
    user = AdminUser("boss", role="clinic_admin", clinic_id=CID, user_id="u-1")
    app.dependency_overrides[admin.verify_credentials] = lambda: user
    yield user
    app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.mark.parametrize("path, params", [
    ("/admin/stats", {"days": 100000}), ("/admin/stats", {"days": 0}),
    ("/admin/payments/stats", {"days": 100000}), ("/admin/payments/stats", {"days": -5}),
    ("/admin/reports/deliveries", {"days": 10**9}), ("/admin/lab-reports/deliveries", {"days": 0}),
    ("/admin/audit-logs", {"limit": 100000}), ("/admin/audit-logs", {"limit": 0}),
    ("/admin/notifications", {"limit": 100000}), ("/admin/notifications", {"offset": -1}),
    ("/admin/notifications", {"offset": 10**7}),
    ("/admin/payments/reconciliation", {"date_str": "not-a-date"}),
    ("/admin/payments/reconciliation", {"date_str": "2026-13-40"}),
])
def test_out_of_range_parameters_are_422_before_any_query(as_admin, path, params):
    with patch.object(admin, "supabase", MagicMock(side_effect=AssertionError("queried"))):
        r = client.get(path, params={**params, "clinic_id": CID})
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_dashboard_stats_error_never_returns_the_exception_text():
    with patch("app.services.analytics.supabase") as mock_sb:
        mock_sb.table.side_effect = Exception("DB connection lost: password=hunter2 host=10.0.0.5")
        stats = await analytics.AnalyticsService().get_dashboard_stats(CID)
    assert stats["total_appointments"] == 0 and stats["error"] == "stats_unavailable"
    assert "hunter2" not in str(stats) and "10.0.0.5" not in str(stats)


@pytest.mark.asyncio
async def test_reconciliation_defaults_to_the_ist_date():
    seen = []

    class _Q:
        def __getattr__(self, name):
            if name == "not_":  # an attribute in postgrest (.not_.is_(...)), not a method
                return self

            def chain(*a, **k):
                seen.append((name, a))
                return self
            return chain

    sb_client = MagicMock()
    sb_client.table.return_value = _Q()

    async def _sb(q):
        return MagicMock(data=[], count=0)

    with patch.object(payment, "supabase", sb_client), patch.object(payment, "sb", _sb), \
         patch.object(payment, "today_ist", lambda: date(2026, 1, 2)):
        out = await payment.payment_service.get_daily_reconciliation(clinic_id=CID)
    assert out["date"] == "2026-01-02"
    assert ("eq", ("appointment_date", "2026-01-02")) in seen


# D-27 (endpoint half): hospital_holidays.name is VARCHAR(100); an over-long or blank name used to 500.
@pytest.mark.parametrize("name", ["x" * 101, "", "   "])
def test_holiday_name_must_be_1_to_100_characters(as_admin, name):
    with patch.object(admin, "supabase", MagicMock(side_effect=AssertionError("queried"))):
        r = client.post("/admin/holidays", params={"holiday_date": "2035-01-26", "name": name, "clinic_id": CID})
    assert r.status_code == 422, r.text


# D-28: connector run-history limit is bounded (the panel asks for 20).
@pytest.mark.parametrize("limit", [0, 101, 100000])
def test_connector_audit_log_limit_is_bounded(as_admin, limit):
    with patch.object(admin, "supabase", MagicMock(side_effect=AssertionError("queried"))):
        r = client.get("/admin/connectors/c-1/audit-log", params={"limit": limit, "clinic_id": CID})
    assert r.status_code == 422, r.text
