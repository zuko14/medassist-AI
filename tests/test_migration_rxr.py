"""Migration 150 (standalone pharmacy plan, lane RXR) against real PostgreSQL.

clinics.plan / plan_tiers.plan_name accept 'pharmacy' and every older value; the plan tier is seeded; pharmacy_order_requests:
one open request per (phone, kind), a photo request carries its media id, a collected request names its sale, a
cross-tenant patient / store is refused by the same-clinic guard; rx_purge_clinic v10 removes the requests; no trigger
on an existing table; the rollback refuses while a pharmacy clinic exists, and drop → re-apply works in one transaction.
"""

import json
import re
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_ipd_api_e2e import _clinic, _patient

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "150_retail_pharmacy.sql", MIG / "rollback" / "150_rollback_retail_pharmacy.sql"
OLD_PLANS = ("soloclinic", "diagstream", "diagbooking", "essential", "polyclinic", "enterprise", "derma", "eye", "dental",
             "ivf", "multispecialty", "womenchild")


def _q(c, sql, *args):
    cur = c.cursor()
    cur.execute(sql, args)
    return cur.fetchone()[0] if cur.description else None


def _err(c, sql, *args) -> str:
    with pytest.raises(psycopg2.Error) as e:
        _q(c, sql, *args)
    return str(e.value)


def _pharmacy(c):
    return str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('Sai Medicals', %s, "
                     "'pharmacy', true) RETURNING id", "+9187" + str(uuid.uuid4().int)[:8]))


def test_plan_widened_and_tier_seeded(real_pg_conn):
    c = real_pg_conn
    assert _pharmacy(c)
    allowed = set(re.findall(r"'([a-z]+)'", _q(c, "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                                   "WHERE conname = 'clinics_plan_check'")))
    assert allowed == set(OLD_PLANS) | {"pharmacy"}
    assert "clinics_plan_check" in _err(c, "INSERT INTO clinics (name, whatsapp_number, plan) VALUES ('X', '+919000000001', "
                                           "'shop')")
    assert _q(c, "SELECT display_name || ':' || included_messages_month FROM plan_tiers WHERE plan_name = 'pharmacy'") == \
        "Pharmacy / Medical Store:1000"
    from app.tenancy import TENANT_OWNED_TABLES
    assert "pharmacy_order_requests" in TENANT_OWNED_TABLES
    assert _q(c, "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'pharmacy_order_requests'::regclass")


def test_order_request_rules(real_pg_conn):
    c = real_pg_conn
    clinic = _pharmacy(c)
    ins = ("INSERT INTO pharmacy_order_requests (clinic_id, phone, phone_masked, kind, source, media_ref) VALUES "
           "(%s, '+919811112222', '+91XXXXXX2222', %s, %s, %s) RETURNING id")
    r = str(_q(c, ins, clinic, "prescription", "whatsapp_photo", "wamid.MEDIA1"))
    assert "uq_pharmacy_order_requests_open" in _err(c, ins, clinic, "prescription", "whatsapp_photo", "wamid.MEDIA2")
    assert _q(c, ins, clinic, "refill", "whatsapp_text", None)                # another kind is its own queue line
    assert "pharmacy_order_requests_photo" in _err(c, ins, clinic, "callback", "whatsapp_photo", None)
    assert "pharmacy_order_requests_collected_sale" in _err(c, "UPDATE pharmacy_order_requests SET status = 'collected' WHERE id = %s", r)
    assert "pharmacy_order_requests_reject_reason" in _err(c, "UPDATE pharmacy_order_requests SET status = 'rejected' WHERE id = %s", r)
    _q(c, "UPDATE pharmacy_order_requests SET status = 'rejected', rejected_reason = 'Unreadable photo' WHERE id = %s", r)
    assert _q(c, ins, clinic, "prescription", "whatsapp_photo", "wamid.MEDIA3")   # a closed request frees the slot
    other = _clinic(c)
    stranger = _patient(c, other)
    assert "kriya_cross_tenant_ref" in _err(c, "INSERT INTO pharmacy_order_requests (clinic_id, patient_id, phone, phone_masked, "
                                               "kind, source) VALUES (%s, %s, '+919811113333', 'x3333', 'refill', "
                                               "'counter')", clinic, stranger)
    out = _q(c, "SELECT rx_purge_clinic(%s)", clinic)
    assert out["order_requests"] == 3
    assert _q(c, "SELECT count(*) FROM pharmacy_order_requests WHERE clinic_id = %s", clinic) == 0


def test_no_trigger_on_existing_tables():
    sql = UP.read_text(encoding="utf-8")
    assert set(re.findall(r"CREATE TRIGGER\s+\w+\s+BEFORE[A-Z ]+?ON\s+([a-z_]+)", sql)) == {"pharmacy_order_requests"}
    assert set(re.findall(r"ALTER TABLE\s+([a-z_]+)", sql)) <= {"clinics", "plan_tiers", "pharmacy_order_requests"}
    assert "FOREIGN KEY" not in sql.split("rx_purge_clinic v10")[0]


def test_rollback_refuses_then_drops_and_reapplies(real_pg_conn):
    c = real_pg_conn
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute("SAVEPOINT s")
        cur.execute("INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('P', '+918700000001', "
                    "'pharmacy', true)")
        with pytest.raises(psycopg2.Error, match="150_down: clinics on plan pharmacy"):
            cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("ROLLBACK TO SAVEPOINT s")
        cur.execute("UPDATE clinics SET plan = 'soloclinic' WHERE plan = 'pharmacy'")
        cur.execute("SELECT id FROM clinics WHERE id IN (SELECT DISTINCT clinic_id FROM pharmacy_order_requests)")
        for (cid,) in cur.fetchall():
            cur.execute("DELETE FROM pharmacy_order_requests WHERE clinic_id = %s", (cid,))
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('public.pharmacy_order_requests'), (SELECT count(*) FROM plan_tiers "
                    "WHERE plan_name = 'pharmacy'), pg_get_constraintdef(oid) LIKE '%%pharmacy%%' FROM pg_constraint "
                    "WHERE conname = 'clinics_plan_check'")
        assert cur.fetchone() == (None, 0, False)
        cur.execute("SELECT prosrc LIKE '%%pharmacy_order_requests%%' FROM pg_proc WHERE proname = 'rx_purge_clinic'")
        assert cur.fetchone()[0] is False                                   # 142's purge body is back
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('public.pharmacy_order_requests') IS NOT NULL")
        assert cur.fetchone()[0]
        assert json  # noqa: B018
    finally:
        cur.execute("ROLLBACK")
