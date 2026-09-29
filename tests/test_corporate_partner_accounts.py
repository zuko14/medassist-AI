"""Migration 096 — dashboard-only corporate partner labs.

A partner (e.g. Taiyo Labs) is a clinics row with account_type='corporate_partner':
it hosts Corporate Health companies and company logins, but must never be
routed WhatsApp traffic, billed, or listed as a hospital."""

import ast
import pathlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import platform
from app.routers.admin import AdminUser
from app.services.tenant import corporate_health_enabled

REPO = pathlib.Path(__file__).resolve().parents[1]
PARTNER = "66666666-6666-6666-6666-666666666666"
TENANT = "77777777-7777-7777-7777-777777777777"
client = TestClient(app)


# ═══════ migration, real PostgreSQL ═══════


def _insert_clinic(cur, account_type=None, phone_number_id=None):
    row = {"name": "P " + uuid.uuid4().hex[:6], "whatsapp_number": "x-" + uuid.uuid4().hex,
           "plan": "diagstream", "is_active": True}
    if account_type:
        row["account_type"] = account_type
    if phone_number_id:
        row["phone_number_id"] = phone_number_id
    cur.execute(f"INSERT INTO clinics ({', '.join(row)}) VALUES ({', '.join(['%s'] * len(row))}) "
                "RETURNING id, account_type", list(row.values()))
    return cur.fetchone()


def test_new_clinics_default_to_tenant_and_none_are_unset(real_pg_conn):
    cur = real_pg_conn.cursor()
    cid, kind = _insert_clinic(cur)
    assert kind == "tenant"
    cur.execute("SELECT count(*) FROM clinics WHERE account_type IS NULL")
    assert cur.fetchone()[0] == 0
    cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))


def test_unknown_account_type_is_refused(real_pg_conn):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_clinic(real_pg_conn.cursor(), account_type="hospital")


def test_a_partner_can_never_carry_a_meta_phone_number_id(real_pg_conn):
    cur = real_pg_conn.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_clinic(cur, account_type="corporate_partner", phone_number_id="1234567890")
    cid, _ = _insert_clinic(cur, account_type="corporate_partner")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("UPDATE clinics SET phone_number_id = '999' WHERE id = %s", (cid,))
    cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))


def test_a_partner_hosts_companies_and_company_logins(real_pg_conn):
    cur = real_pg_conn.cursor()
    cid, _ = _insert_clinic(cur, account_type="corporate_partner")
    cur.execute("INSERT INTO corporate_clients (clinic_id, name) VALUES (%s, 'Acme') RETURNING id", (cid,))
    company = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, permissions, "
        "corporate_client_id, is_active) VALUES (%s, %s, 'x', 'staff', 'CORPORATE_VIEWER', '{}', %s, true)",
        (cid, "hr_" + uuid.uuid4().hex[:8], company))
    cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))  # the 096 rollback path: everything cascades
    cur.execute("SELECT count(*) FROM corporate_clients WHERE id = %s", (company,))
    assert cur.fetchone()[0] == 0


# ═══════ owner endpoints ═══════


@pytest.fixture
def owner():
    app.dependency_overrides[platform.verify_owner_credentials] = lambda: AdminUser(
        "owner", role="platform_owner", user_id="platform_owner_env")
    yield
    app.dependency_overrides.pop(platform.verify_owner_credentials, None)


def test_partner_endpoints_need_owner_auth():
    assert client.post("/platform/corporate-health/partners", json={"name": "Taiyo"}).status_code in (401, 503)
    assert client.patch(f"/platform/corporate-health/partners/{PARTNER}", json={"name": "Taiyo"}).status_code in (401, 503)


def test_create_partner_is_dashboard_only(owner):
    sb_mock = AsyncMock(side_effect=[MagicMock(data=[]), MagicMock(data=[{"id": PARTNER, "name": "Taiyo Labs"}])])
    with patch.object(platform, "sb", sb_mock), patch.object(platform, "supabase") as fake, \
            patch.object(platform, "log_admin_action", AsyncMock()) as audit, \
            patch.object(platform, "invalidate_tenant_cache") as inval:
        r = client.post("/platform/corporate-health/partners", json={"name": "  Taiyo   Labs "})
    assert r.status_code == 200 and r.json()["partner"] == {"id": PARTNER, "name": "Taiyo Labs"}
    row = fake.table.return_value.insert.call_args.args[0]
    assert row["name"] == "Taiyo Labs" and row["account_type"] == "corporate_partner"
    assert row["plan"] == "diagstream" and row["features"] == {"corporate_health": True}
    assert row["whatsapp_number"].startswith("corporate-partner:") and "phone_number_id" not in row
    assert corporate_health_enabled(row)  # the existing 095 gate admits it unchanged
    assert audit.call_args.kwargs["action"] == "corporate_partner_create"
    inval.assert_called_once()


def test_duplicate_partner_name_is_409(owner):
    with patch.object(platform, "sb", AsyncMock(return_value=MagicMock(data=[{"id": PARTNER, "name": "Taiyo Labs"}]))), \
            patch.object(platform, "supabase") as fake:
        r = client.post("/platform/corporate-health/partners", json={"name": "taiyo  labs"})
    assert r.status_code == 409
    fake.table.return_value.insert.assert_not_called()


@pytest.mark.parametrize("name", ["", "x", "y" * 121])
def test_partner_name_is_validated(owner, name):
    assert client.post("/platform/corporate-health/partners", json={"name": name}).status_code == 422


def test_rename_touches_only_partner_rows(owner):
    partners = MagicMock(data=[{"id": PARTNER, "name": "Taiyo", "config": {"clinic_name": "Taiyo", "k": 1}}])
    with patch.object(platform, "sb", AsyncMock(return_value=partners)), patch.object(platform, "supabase") as fake:
        assert client.patch(f"/platform/corporate-health/partners/{TENANT}", json={"name": "Hijack"}).status_code == 404
        fake.table.return_value.update.assert_not_called()
    with patch.object(platform, "sb", AsyncMock(return_value=partners)), \
            patch.object(platform, "supabase") as fake, patch.object(platform, "log_admin_action", AsyncMock()):
        r = client.patch(f"/platform/corporate-health/partners/{PARTNER}", json={"name": "Taiyo Labs"})
    assert r.status_code == 200
    upd = fake.table.return_value.update
    assert upd.call_args.args[0] == {"name": "Taiyo Labs", "config": {"clinic_name": "Taiyo Labs", "k": 1}}
    assert upd.return_value.eq.return_value.eq.call_args.args == ("account_type", "corporate_partner")


def test_hosts_flags_partners(owner):
    rows = [{"id": PARTNER, "name": "Taiyo", "plan": "diagstream", "features": {"corporate_health": True},
             "account_type": "corporate_partner"},
            {"id": TENANT, "name": "Lab", "plan": "diagbooking", "features": {}, "account_type": "tenant"}]
    with patch.object(platform, "sb", AsyncMock(return_value=MagicMock(data=rows))), patch.object(platform, "supabase"):
        hosts = client.get("/platform/corporate-health/hosts").json()["hosts"]
    assert [(h["id"], h["partner"], h["enabled"]) for h in hosts] == [(PARTNER, True, True), (TENANT, False, False)]


# ═══════ every owner tenant listing skips partners ═══════

# Functions that read ALL clinics to show, bill or message them as hospitals.
# A partner row in any of these would be invoiced a plan fee, flagged dormant,
# or sent announcements. Name lookups (id -> name) are deliberately not listed.
MUST_SKIP_PARTNERS = {
    "app/routers/platform.py": {
        "get_platform_overview", "get_platform_branch_changes", "get_platform_clinics_leaderboard",
        "get_platform_data_storage", "get_plan_tiers", "get_platform_subscriptions",
        "get_finance_rates", "generate_finance_invoices",
    },
    "app/services/platform_finance.py": {"finance_summary"},
    "app/services/message_accounting.py": {"get_platform_usage"},
    "app/services/broadcast.py": {"_dispatch_notifications"},
}


def _functions_querying_clinics(path):
    src = (REPO / path).read_text(encoding="utf-8")
    return {n.name: ast.get_source_segment(src, n) or "" for n in ast.walk(ast.parse(src))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and 'table("clinics")' in (ast.get_source_segment(src, n) or "")}


@pytest.mark.parametrize("path", sorted(MUST_SKIP_PARTNERS))
def test_tenant_listings_filter_out_partners(path):
    funcs = _functions_querying_clinics(path)
    missing = MUST_SKIP_PARTNERS[path] - funcs.keys()
    assert not missing, f"{path}: {missing} renamed or moved — update MUST_SKIP_PARTNERS"
    for name in MUST_SKIP_PARTNERS[path]:
        assert '.eq("account_type", "tenant")' in funcs[name], f"{path}:{name} would list or bill partner labs"
