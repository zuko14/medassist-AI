"""Migration 095 against real PostgreSQL: tenant-consistent FK, duplicate
guards, cascade on company delete, viewer logins losing access."""

import json
import uuid

import psycopg2
import pytest

SHA = "a" * 64


def _clinic(cur, name):
    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, 'diagstream', true) RETURNING id",
        (name, "+9197" + uuid.uuid4().hex[:8]),
    )
    return str(cur.fetchone()[0])


def _company(cur, clinic, name):
    cur.execute("INSERT INTO corporate_clients (clinic_id, name) VALUES (%s, %s) RETURNING id", (clinic, name))
    return str(cur.fetchone()[0])


def _report(cur, clinic, company, bill="B1", sha=SHA, sex="M"):
    cur.execute(
        "INSERT INTO corporate_health_reports (clinic_id, corporate_client_id, sex, age_years, bill_id, "
        "results, file_sha256, parser_version) VALUES (%s, %s, %s, 40, %s, %s, %s, 'test') RETURNING id",
        (clinic, company, sex, bill, json.dumps({"hemoglobin": {"v": 14, "s": "normal"}}), sha),
    )
    return str(cur.fetchone()[0])


@pytest.fixture
def world(real_pg_conn):
    cur = real_pg_conn.cursor()
    a, b = _clinic(cur, "Lab A 095"), _clinic(cur, "Lab B 095")
    return cur, a, b, _company(cur, a, "Acme " + uuid.uuid4().hex[:6]), _company(cur, b, "Globex " + uuid.uuid4().hex[:6])


def test_report_cannot_point_at_another_clinics_company(world):
    cur, a, b, acme, globex = world
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _report(cur, a, globex)


def test_same_report_twice_in_one_company_is_refused(world):
    cur, a, b, acme, _ = world
    _report(cur, a, acme, bill="B1", sha="1" * 64)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _report(cur, a, acme, bill="B1", sha="2" * 64)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _report(cur, a, acme, bill="B2", sha="1" * 64)  # same file bytes
    acme2 = _company(cur, a, "Acme Two " + uuid.uuid4().hex[:6])
    _report(cur, a, acme2, bill="B1", sha="1" * 64)  # another company may hold the same bill


def test_company_names_unique_per_clinic_case_insensitive(world):
    cur, a, b, _, _ = world
    name = "Initech " + uuid.uuid4().hex[:6]
    _company(cur, a, name)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _company(cur, a, "  " + name.upper() + " ")
    _company(cur, b, name)


@pytest.mark.parametrize("field, value", [("sex", "X"), ("sha", "not-a-hash")])
def test_check_constraints(world, field, value):
    cur, a, _, acme, _ = world
    kw = {"sex": "M", "sha": SHA, "bill": "C" + uuid.uuid4().hex[:6], field: value}
    with pytest.raises(psycopg2.errors.CheckViolation):
        _report(cur, a, acme, **kw)


def test_deleting_a_company_removes_its_reports_and_unlinks_its_logins(world):
    cur, a, _, acme, _ = world
    _report(cur, a, acme, bill="D1", sha="3" * 64)
    user = "viewer_" + uuid.uuid4().hex[:8]
    cur.execute(
        "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, is_active, corporate_client_id) "
        "VALUES (%s, %s, '$2b$12$x', 'staff', 'CORPORATE_VIEWER', true, %s)", (a, user, acme))
    cur.execute("DELETE FROM corporate_clients WHERE id = %s", (acme,))
    cur.execute("SELECT count(*) FROM corporate_health_reports WHERE corporate_client_id = %s", (acme,))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT corporate_client_id, staff_role FROM clinic_admins WHERE username = %s", (user,))
    assert cur.fetchone() == (None, "CORPORATE_VIEWER")  # login kept, company gone: the app refuses it


def test_rls_is_forced(world):
    cur = world[0]
    cur.execute("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname IN ('corporate_clients', 'corporate_health_reports') ORDER BY relname")
    assert cur.fetchall() == [("corporate_clients", True, True), ("corporate_health_reports", True, True)]
