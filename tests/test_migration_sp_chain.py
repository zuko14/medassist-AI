"""Migrations 151–156 (lane SP, Specialty OS) against real PostgreSQL.

Every new table is clinic-scoped, registered in TENANT_OWNED_TABLES, RLS enabled + forced; references into tables of
earlier migrations go through kriya_guard_refs (never a FOREIGN KEY), so rollbacks of 103+ keep working; no trigger or
ALTER lands on an existing table; each file has a verify block and a rollback; the rollbacks refuse while data exists,
and the whole chain drops and re-applies (twice, idempotent) in one transaction. Guards: a form freezes on sign, a
package session beyond its count is refused, a cross-tenant patient is refused, the witness chain is append-only and
re-verifies, sp_purge_clinic removes every SP row of one clinic only.
"""

import json
import re
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_ipd_api_e2e import _clinic, _doctor, _patient

MIG = Path(__file__).resolve().parents[1] / "migrations"
FILES = ("151_sp_media_forms", "152_sp_packages_estimates", "153_sp_eye_dental", "154_sp_derma_ivf", "155_sp_womenchild_purge",
         "156_sp_wc_optical")
UPS = [MIG / f"{f}.sql" for f in FILES]
DOWNS = [MIG / "rollback" / f"{f.replace('_sp_', '_rollback_sp_', 1)}.sql" for f in FILES]
SP_TABLES = ("sp_media_consents", "sp_media", "opd_encounter_forms", "sp_packages", "sp_package_sales",
             "sp_package_consumptions", "sp_instalments", "sp_estimates", "sp_imaging_links", "sp_camps",
             "sp_camp_registrations", "sp_optical_jobs", "sp_dental_lab_orders", "sp_compliance_items", "ivf_couples",
             "ivf_cycles", "ivf_cycle_days", "ivf_embryos", "ivf_witness_events", "obs_pregnancies", "obs_anc_visits",
             "imm_doses", "obs_deliveries", "obs_newborns", "child_growth_measurements", "wc_gov_ids", "nicu_flowsheet_entries",
             "sp_optical_items", "sp_optical_stock_moves")


def _q(c, sql, *args):
    cur = c.cursor()
    cur.execute(sql, args)
    return cur.fetchone()[0] if cur.description else None


def test_files_exist_with_verify_block_and_rollback():
    for n, up, down in zip(range(151, 157), UPS, DOWNS):
        assert f"{n} verify" in up.read_text(encoding="utf-8"), up.name
        assert "DROP" in down.read_text(encoding="utf-8") and "RAISE EXCEPTION" in down.read_text(encoding="utf-8"), down.name


def test_no_trigger_alter_or_foreign_key_on_existing_tables():
    sql = "\n".join(p.read_text(encoding="utf-8") for p in UPS)
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
    assert created == set(SP_TABLES)
    assert set(re.findall(r"CREATE TRIGGER\s+\w+\s+BEFORE[A-Z ]+?ON\s+(\w+)", sql)) <= created
    assert set(re.findall(r"ALTER TABLE\s+(\w+)", sql)) <= created
    targets = set(re.findall(r"REFERENCES\s+(\w+)", sql))
    assert targets <= created | {"clinics"}, targets - created                       # only SP tables and the tenant root


def test_tables_are_registered_and_rls_forced(real_pg_conn):
    from app.tenancy import TENANT_OWNED_TABLES
    for t in SP_TABLES:
        assert t in TENANT_OWNED_TABLES, t
        assert _q(real_pg_conn, "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", t), t
        assert _q(real_pg_conn, "SELECT count(*) FROM information_schema.columns WHERE table_name = %s "
                                "AND column_name = 'clinic_id' AND is_nullable = 'NO'", t) == 1, t


def _encounter(c, clinic, patient, doctor):
    appt = _q(c, "INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id, doctor_name, "
                 "department, appointment_date, appointment_time, status, is_walk_in, booking_type) VALUES (%s, %s, "
                 "'+919800000001', 'P', %s, 'Dr', 'Med', CURRENT_DATE, %s, 'confirmed', false, 'consultation') RETURNING id",
              clinic, patient, doctor, f"{uuid.uuid4().int % 24:02d}:{uuid.uuid4().int % 60:02d}")
    return str(_q(c, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id) VALUES (%s, %s, %s, %s) "
                     "RETURNING id", clinic, appt, patient, doctor))


def test_guards_freeze_tenant_and_witness_chain(real_pg_conn):
    c = real_pg_conn
    clinic, other = _clinic(c), _clinic(c)
    pat, doctor = _patient(c, clinic), _doctor(c, clinic)
    enc = _encounter(c, clinic, pat, doctor)
    form = ("INSERT INTO opd_encounter_forms (clinic_id, encounter_id, patient_id, form_code, form_version, data, "
            "updated_by_name) VALUES (%s, %s, %s, 'eye_exam_v1', 1, '{}'::jsonb, 'Dr X') RETURNING id")
    with pytest.raises(psycopg2.Error, match="kriya_cross_tenant_ref"):
        _q(c, form, other, enc, pat)                                                    # another clinic's encounter
    _q(c, form, clinic, enc, pat)
    admin = _q(c, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, doctor_id, is_active) "
                  "VALUES (%s, %s, 'x', 'staff', 'DOCTOR', %s, true) RETURNING id", clinic, "d" + uuid.uuid4().hex[:8], doctor)
    _q(c, "SELECT id FROM opd_sign_encounter(%s, %s, %s, %s, %s::jsonb)", clinic, enc, admin, doctor,
       json.dumps({"name": "Dr", "registration_number": "R1"}))
    with pytest.raises(psycopg2.Error, match="sp_form_frozen"):
        _q(c, "UPDATE opd_encounter_forms SET data = '{\"x\": 1}'::jsonb WHERE encounter_id = %s", enc)
    with pytest.raises(psycopg2.Error, match="sp_form_frozen"):
        _q(c, "DELETE FROM opd_encounter_forms WHERE encounter_id = %s", enc)
    # package ledger: never beyond the count
    pkg = _q(c, "INSERT INTO sp_packages (clinic_id, code, name, specialty, service_code, sessions_total) VALUES "
                "(%s, 'P2', 'Peel x2', 'derma', 'PEEL', 2) RETURNING id", clinic)
    inv = _q(c, "INSERT INTO opd_invoices (clinic_id, patient_id, status) VALUES (%s, %s, 'draft') RETURNING id", clinic, pat)
    sale = _q(c, "INSERT INTO sp_package_sales (clinic_id, package_id, patient_id, invoice_id, sessions_total, price_paise, "
                 "expires_on, sold_by_name) VALUES (%s, %s, %s, %s, 2, 500000, CURRENT_DATE + 30, 'Desk') RETURNING id",
              clinic, pkg, pat, inv)
    use = "INSERT INTO sp_package_consumptions (clinic_id, sale_id, session_no, consumed_by_name) VALUES (%s, %s, %s, 'Nurse')"
    _q(c, use, clinic, sale, 1)
    with pytest.raises(psycopg2.Error, match="uq_sp_package_consumption_live"):
        _q(c, use, clinic, sale, 1)
    with pytest.raises(psycopg2.Error, match="sp_package_sessions_exhausted"):
        _q(c, use, clinic, sale, 3)
    # witness chain
    couple = _q(c, "INSERT INTO ivf_couples (clinic_id, female_patient_id, donor_code, female_id_type, female_id_last4, "
                   "marital_status_declared, identity_verified_by_name) VALUES (%s, %s, 'D-9', 'pan', 'AB12', 'single_woman', "
                   "'Desk') RETURNING id", clinic, pat)
    cyc = _q(c, "INSERT INTO ivf_cycles (clinic_id, couple_id, cycle_type, start_date, created_by_name) VALUES "
                "(%s, %s, 'ivf', CURRENT_DATE, 'Dr') RETURNING id", clinic, couple)
    wit = ("INSERT INTO ivf_witness_events (clinic_id, seq, cycle_id, step, performed_by, performed_by_name, witnessed_by, "
           "witnessed_by_name, performed_at, witnessed_at, row_hash) VALUES (%s, 0, %s, %s, %s, 'Asha', %s, 'Ravi', now(), "
           "now() + interval '2 minutes', '\\x00') RETURNING seq")
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert [_q(c, wit, clinic, cyc, s, a, b) for s in ("oocyte_collection", "insemination")] == [1, 2]
    with pytest.raises(psycopg2.Error, match="ivf_witness_events_two_people"):
        _q(c, wit, clinic, cyc, "sperm_prep", a, a)
    with pytest.raises(psycopg2.Error, match="ivf_witness_append_only"):
        _q(c, "DELETE FROM ivf_witness_events WHERE clinic_id = %s", clinic)
    assert _q(c, "SELECT ivf_witness_verify(%s)", clinic) is None
    # optical ledger: a write-off without a reason is refused by the database itself (NULL note included)
    item = _q(c, "INSERT INTO sp_optical_items (clinic_id, sku, kind, name, qty_on_hand) VALUES (%s, 'LN-X', 'lens', 'SV', 2) "
                 "RETURNING id", clinic)
    with pytest.raises(psycopg2.Error, match="sp_optical_stock_moves_note"):
        _q(c, "SELECT sp_optical_move(%s, %s, -1, 'write_off', NULL, NULL, 'Admin')", clinic, item)
    with pytest.raises(psycopg2.Error, match="sp_optical_out_of_stock"):
        _q(c, "SELECT sp_optical_move(%s, %s, -3, 'adjust', NULL, 'count corrected', 'Admin')", clinic, item)
    assert _q(c, "SELECT sp_optical_move(%s, %s, 1, 'receive', NULL, NULL, 'Desk')", clinic, item)["qty_on_hand"] == 3
    # the purge removes this clinic's SP rows only (and passes the freeze / append-only guards for it)
    keep = _q(c, "INSERT INTO sp_compliance_items (clinic_id, kind, label, valid_until, created_by_name) VALUES "
                 "(%s, 'fire_noc', 'Fire NOC', CURRENT_DATE + 90, 'Admin') RETURNING id", other)
    out = _q(c, "SELECT sp_purge_clinic(%s)", clinic)
    assert out["opd_encounter_forms"] == 1 and out["ivf_witness_events"] == 2 and out["sp_package_consumptions"] == 1
    assert _q(c, "SELECT count(*) FROM sp_compliance_items WHERE id = %s", keep) == 1
    for t in SP_TABLES:
        assert _q(c, f"SELECT count(*) FROM {t} WHERE clinic_id = %s", clinic) == 0, t


def test_rollback_refuses_then_drops_and_reapplies(real_pg_conn):
    c = real_pg_conn
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute("SAVEPOINT s")
        clinic = str(uuid.uuid4())
        cur.execute("INSERT INTO clinics (id, name, whatsapp_number, plan, is_active) VALUES (%s, 'R', %s, 'polyclinic', true)",
                    (clinic, "+9180" + str(uuid.uuid4().int)[:8]))
        cur.execute("INSERT INTO sp_compliance_items (clinic_id, kind, label, valid_until, created_by_name) VALUES "
                    "(%s, 'other', 'Something', CURRENT_DATE, 'Admin')", (clinic,))
        cur.execute("INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'X', '+919811100000') RETURNING id", (clinic,))
        pid = cur.fetchone()[0]
        cur.execute("INSERT INTO obs_pregnancies (clinic_id, patient_id, edd, edd_source, gravida, created_by_name) VALUES "
                    "(%s, %s, CURRENT_DATE + 100, 'dating_scan', 1, 'Dr')", (clinic, pid))
        with pytest.raises(psycopg2.Error, match="155_down"):
            cur.execute(DOWNS[4].read_text(encoding="utf-8"))
        cur.execute("ROLLBACK TO SAVEPOINT s")
        cur.execute("SAVEPOINT s")
        cur.execute("INSERT INTO clinics (id, name, whatsapp_number, plan, is_active) VALUES (%s, 'R', %s, 'eye', true)",
                    (clinic, "+9181" + str(uuid.uuid4().int)[:8]))
        cur.execute("INSERT INTO sp_optical_items (clinic_id, sku, kind, name) VALUES (%s, 'FR-1', 'frame', 'Titan')", (clinic,))
        with pytest.raises(psycopg2.Error, match=r"156_down: \w+ has rows"):   # whichever new table has rows first
            cur.execute(DOWNS[5].read_text(encoding="utf-8"))
        cur.execute("ROLLBACK TO SAVEPOINT s")
        with pytest.raises(psycopg2.Error, match="151_down: roll back 152 first"):
            cur.execute(DOWNS[0].read_text(encoding="utf-8"))
        cur.execute("ROLLBACK TO SAVEPOINT s")
        cur.execute("SELECT DISTINCT clinic_id FROM (" + " UNION ".join(f"SELECT clinic_id FROM {t}" for t in SP_TABLES) + ") x")
        for (cid,) in cur.fetchall():
            cur.execute("SELECT sp_purge_clinic(%s)", (cid,))
        for down in reversed(DOWNS):
            cur.execute(down.read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM pg_class WHERE relname = ANY(%s) AND relkind = 'r'", (list(SP_TABLES),))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT to_regprocedure('kriya_guard_refs()') IS NOT NULL")
        assert cur.fetchone()[0]                                                         # 144 owns it: never dropped
        for _ in range(2):
            for up in UPS:
                cur.execute(up.read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM pg_class WHERE relname = ANY(%s) AND relkind = 'r'", (list(SP_TABLES),))
        assert cur.fetchone()[0] == len(SP_TABLES)
    finally:
        cur.execute("ROLLBACK")
