"""HTTP contract for the owner lifecycle endpoints and the clinic-facing banner."""

import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)
CLINIC = "11111111-1111-1111-1111-111111111111"


def owner_auth():
    creds = f"{settings.owner_username}:{settings.owner_password}"
    return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}


def clinic_row(**over):
    now = datetime.now(timezone.utc)
    row = {
        "id": CLINIC,
        "name": "Apex Diagnostics",
        "plan": "diagstream",
        "is_active": True,
        "whatsapp_number": "+919000000000",
        "phone_number_id": "12345",
        "daily_report_limit": 100,
        "subscription_start_date": (now - timedelta(days=33)).isoformat(),
        "subscription_end_date": (now - timedelta(days=3)).isoformat(),
        "grace_period_days": 5,
        "subscription_status": "active",
        "last_renewed_at": None,
    }
    row.update(over)
    return row


def _routed_supabase(mock_supabase, clinic, updated=None, usage=None):
    """Point clinics/clinic_daily_usage at fixed rows through one mock chain."""
    def table(name):
        obj = MagicMock()
        obj.select.return_value = obj
        obj.update.return_value = obj
        for m in ("eq", "neq", "gte", "lt", "in_", "order", "limit"):
            getattr(obj, m).return_value = obj
        if name == "clinics":
            obj._rows = [clinic]
            obj._update_rows = [updated if updated is not None else clinic]
        elif name == "clinic_daily_usage":
            obj._rows = usage or []
        else:
            obj._rows = []
        obj._name = name
        return obj

    mock_supabase.table.side_effect = table


# -- Owner: renewal ----------------------------------------------------------


def test_renew_backdates_to_the_previous_expiry_and_clears_suspension():
    now = datetime.now(timezone.utc)
    previous_end = now - timedelta(days=3)
    clinic = clinic_row(subscription_status="suspended",
                        subscription_end_date=previous_end.isoformat())
    written = {}

    def table(name):
        obj = MagicMock()
        obj.select.return_value = obj
        for m in ("eq", "neq", "gte", "lt", "in_", "order", "limit"):
            getattr(obj, m).return_value = obj
        obj._rows = [clinic] if name == "clinics" else []

        def _update(payload):
            written.update(payload)
            up = MagicMock()
            up.eq.return_value = up
            up._rows = [{**clinic, **payload}]
            return up

        obj.update.side_effect = _update
        return obj

    async def fake_sb(builder):
        return MagicMock(data=getattr(builder, "_rows", []))

    with patch("app.routers.platform.supabase") as sb_mod, \
         patch("app.routers.platform.sb", side_effect=fake_sb), \
         patch("app.database.sb", side_effect=fake_sb), \
         patch("app.routers.platform.log_admin_action", new_callable=AsyncMock):
        sb_mod.table.side_effect = table
        res = client.post(f"/platform/clinics/{CLINIC}/renew", headers=owner_auth())

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["previous_status"] == "suspended"
    assert written["subscription_status"] == "active"
    # Backdated: the new window starts where the old one ended.
    assert written["subscription_start_date"][:19] == previous_end.isoformat()[:19]
    new_end = datetime.fromisoformat(written["subscription_end_date"])
    assert (new_end - previous_end).days == 30
    assert written["last_renewed_at"]


def test_renew_requires_owner_auth():
    assert client.post(f"/platform/clinics/{CLINIC}/renew").status_code == 401


# -- Owner: fixed-tier enforcement ------------------------------------------


def _patch_update_ok(clinic, written):
    def table(name):
        obj = MagicMock()
        obj.select.return_value = obj
        for m in ("eq", "neq", "gte", "lt", "in_", "order", "limit"):
            getattr(obj, m).return_value = obj
        obj._rows = [clinic] if name == "clinics" else []

        def _update(payload):
            written.update(payload)
            up = MagicMock()
            up.eq.return_value = up
            up._rows = [{**clinic, **payload}]
            return up

        obj.update.side_effect = _update
        return obj
    return table


async def _fake_sb(builder):
    return MagicMock(data=getattr(builder, "_rows", []))


@pytest.mark.parametrize("tier", [0, 50, 100, 200, 300, 500])
def test_every_allowed_tier_is_accepted(tier):
    clinic, written = clinic_row(), {}
    with patch("app.routers.platform.supabase") as sb_mod, \
         patch("app.routers.platform.sb", side_effect=_fake_sb), \
         patch("app.database.sb", side_effect=_fake_sb), \
         patch("app.routers.platform.log_admin_action", new_callable=AsyncMock):
        sb_mod.table.side_effect = _patch_update_ok(clinic, written)
        res = client.patch(f"/platform/clinics/{CLINIC}/subscription",
                           headers=owner_auth(), json={"daily_report_limit": tier})
    assert res.status_code == 200, res.text
    assert written["daily_report_limit"] == tier


@pytest.mark.parametrize("tier", [1, 25, 75, 150, 1000, -50])
def test_an_off_tier_limit_is_rejected_before_it_reaches_the_database(tier):
    """The CHECK constraint would 500. Pydantic must 422 first."""
    res = client.patch(f"/platform/clinics/{CLINIC}/subscription",
                       headers=owner_auth(), json={"daily_report_limit": tier})
    assert res.status_code == 422


def test_changing_the_start_date_recomputes_the_end_date():
    clinic, written = clinic_row(), {}
    start = "2026-10-01T00:00:00+00:00"
    with patch("app.routers.platform.supabase") as sb_mod, \
         patch("app.routers.platform.sb", side_effect=_fake_sb), \
         patch("app.database.sb", side_effect=_fake_sb), \
         patch("app.routers.platform.log_admin_action", new_callable=AsyncMock):
        sb_mod.table.side_effect = _patch_update_ok(clinic, written)
        res = client.patch(f"/platform/clinics/{CLINIC}/subscription",
                           headers=owner_auth(), json={"subscription_start_date": start})
    assert res.status_code == 200, res.text
    assert written["subscription_end_date"][:10] == "2026-10-31"


def test_a_malformed_start_date_is_a_422_not_a_500():
    clinic, written = clinic_row(), {}
    with patch("app.routers.platform.supabase") as sb_mod, \
         patch("app.routers.platform.sb", side_effect=_fake_sb), \
         patch("app.database.sb", side_effect=_fake_sb), \
         patch("app.routers.platform.log_admin_action", new_callable=AsyncMock):
        sb_mod.table.side_effect = _patch_update_ok(clinic, written)
        res = client.patch(f"/platform/clinics/{CLINIC}/subscription",
                           headers=owner_auth(), json={"subscription_start_date": "yesterday"})
    assert res.status_code == 422


def test_an_empty_patch_is_rejected():
    clinic, written = clinic_row(), {}
    with patch("app.routers.platform.supabase") as sb_mod, \
         patch("app.routers.platform.sb", side_effect=_fake_sb), \
         patch("app.database.sb", side_effect=_fake_sb), \
         patch("app.routers.platform.log_admin_action", new_callable=AsyncMock):
        sb_mod.table.side_effect = _patch_update_ok(clinic, written)
        res = client.patch(f"/platform/clinics/{CLINIC}/subscription",
                           headers=owner_auth(), json={})
    assert res.status_code == 400


def test_subscription_endpoints_require_owner_auth():
    assert client.get(f"/platform/clinics/{CLINIC}/subscription").status_code == 401
    assert client.get("/platform/subscriptions").status_code == 401
    assert client.get("/platform/outbound-audit").status_code == 401


def test_audit_feed_rejects_an_unknown_class():
    res = client.get("/platform/outbound-audit?source_class=NOPE", headers=owner_auth())
    assert res.status_code == 422


# -- Clinic-facing banner endpoint -------------------------------------------


def admin_auth():
    creds = f"{settings.admin_username}:{settings.admin_password}"
    return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}


def test_clinic_subscription_endpoint_returns_the_banner_and_limit_state():
    now = datetime.now(timezone.utc)
    clinic = clinic_row(
        subscription_end_date=(now - timedelta(days=2)).isoformat(),  # in grace
        daily_report_limit=50,
    )
    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=clinic)), \
         patch("app.services.subscription.get_daily_usage",
               new=AsyncMock(return_value={"reports_delivered_count": 45})), \
         patch("app.routers.admin.enforce_clinic_access", return_value=CLINIC):
        res = client.get("/admin/subscription", headers=admin_auth())

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["subscription"]["status"] == "grace_period"
    assert "grace period (Day 3 of 5)" in body["subscription"]["banner"]
    assert body["daily_reports"]["level"] == "warning"
    assert body["daily_reports"]["percent"] == 90
    assert body["resets_at"]


def test_clinic_subscription_endpoint_leaks_no_financial_field():
    """P0: this is a customer-facing route, same boundary as messaging-usage."""
    forbidden = ("cost", "price", "paise", "inr", "markup", "margin", "revenue", "profit")
    with patch("app.routers.admin.get_clinic_by_id", new=AsyncMock(return_value=clinic_row())), \
         patch("app.services.subscription.get_daily_usage",
               new=AsyncMock(return_value={"reports_delivered_count": 1})), \
         patch("app.routers.admin.enforce_clinic_access", return_value=CLINIC):
        body = client.get("/admin/subscription", headers=admin_auth()).json()

    violations = []

    def scan(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                full = f"{path}.{k}" if path else k
                if any(f in k.lower() for f in forbidden):
                    violations.append(full)
                scan(v, full)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                scan(item, f"{path}[{i}]")

    scan(body)
    assert not violations, f"financial fields leaked to a clinic admin: {violations}"


def test_clinic_subscription_endpoint_requires_auth():
    assert client.get("/admin/subscription").status_code == 401
