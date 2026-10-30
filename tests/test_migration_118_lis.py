"""Migration 118 (in-house LIS) against real PostgreSQL.

Schema shape, tenant-safe composite FKs, entry -> sign-off -> amendment through
the SQL functions, signed-report immutability, ten concurrent first entries,
and a guarded rollback + idempotent re-apply. HTTP behaviour:
tests/test_lis_api_e2e.py.
"""

import json
from decimal import Decimal
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_115_orders import _place, _q, _rows, world as _orders_world  # noqa: F401  (fixture)

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP = MIG / "118_lab_information_system.sql"
DOWN = MIG / "rollback" / "118_down.sql"
SIGNER = {"name": "Dr. Path", "registration_number": "TSMC/2010/123", "registration_council": "TSMC"}


@pytest.fixture
def lab(_orders_world):  # noqa: F811
    w = _orders_world
    c = w["c"]
    w["hb"] = str(_q(c, """INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, unit, normal_low, normal_high)
                           VALUES (%s, %s, 'Hemoglobin', 'g/dL', 13, 17) RETURNING id""", w["clinic"], w["cbc"]))
    order = _place(c, w, [{"order_type": "investigation", "catalog_type": "lab_test",
                           "catalog_reference_id": w["cbc"]}])["orders"][0]
    w["order"] = order["id"]
    return w


def _collect(w):
    _q(w["c"], """UPDATE opd_orders SET status = 'sample_collected', specimen_barcode = %s, sample_collected_at = now()
                  WHERE id = %s""", "S-" + uuid.uuid4().hex[:8], w["order"])


def _record(conn, w, value="14.2", reason=None, flag="normal"):
    vals = [{"parameter_id": w["hb"], "parameter_name": "Hemoglobin", "unit": "g/dL", "numeric_value": value,
             "flag": flag, "reference_range_display": "13 - 17 g/dL"}]
    return _q(conn, "SELECT lis_record_results(%s, %s, %s::jsonb, false, %s, NULL, 'tech')",
              w["clinic"], w["order"], json.dumps(vals), reason)


def _sign(w, result, signer=SIGNER):
    actor = str(uuid.uuid4())
    return _q(w["c"], "SELECT lis_validate_result(%s, %s, %s, %s, %s::jsonb, '{}'::jsonb, '{}'::jsonb)",
              w["clinic"], result["id"], result["updated_at"], actor, json.dumps(signer))


def test_schema_constraints_and_rls(real_pg_conn):
    names = {r[0] for r in _rows(real_pg_conn, """SELECT conname FROM pg_constraint WHERE conrelid IN
             ('lab_results'::regclass, 'lab_result_values'::regclass, 'lab_test_parameters'::regclass)""")}
    assert {"lab_results_clinic_id_id_key", "lab_results_order_fk", "lab_results_validated_complete",
            "lab_results_amendment_shape", "lab_result_values_result_param_key", "lab_test_parameters_test_fk",
            "lab_test_parameters_ranges"} <= names
    for t in ("lab_results", "lab_result_values", "lab_test_parameters"):
        assert _q(real_pg_conn, "SELECT relforcerowsecurity FROM pg_class WHERE relname = %s", t) is True


def test_entry_needs_a_collected_sample_then_moves_the_order(lab):
    with pytest.raises(psycopg2.Error) as e:
        _record(lab["c"], lab)
    assert "lis_order_not_ready" in str(e.value)
    _collect(lab)
    r = _record(lab["c"], lab)
    assert (r["status"], r["version"], r["created"], r["test_name"]) == ("entered", 1, True, "CBC")
    assert _q(lab["c"], "SELECT status FROM opd_orders WHERE id = %s", lab["order"]) == "in_progress"
    again = _record(lab["c"], lab, value="15.0")       # re-entry replaces values, same row
    assert again["id"] == r["id"] and again["created"] is False
    assert _rows(lab["c"], "SELECT numeric_value FROM lab_result_values WHERE result_id = %s", r["id"]) == [(15,)]


def test_sign_off_is_atomic_and_the_signed_report_is_immutable(lab):
    _collect(lab)
    r = _record(lab["c"], lab)
    with pytest.raises(psycopg2.Error) as e:     # the pathologist reviewed an older version
        _q(lab["c"], "SELECT lis_validate_result(%s, %s, now() - interval '1 day', NULL, %s::jsonb, '{}', '{}')",
           lab["clinic"], r["id"], json.dumps(SIGNER))
    assert "lis_result_changed" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:
        _sign(lab, r, signer={"name": "No Reg"})
    assert "lis_signer_registration_missing" in str(e.value)

    signed = _sign(lab, r)
    assert signed["status"] == "validated" and signed["replayed"] is False
    assert _rows(lab["c"], "SELECT status, result_summary FROM opd_orders WHERE id = %s", lab["order"]) == \
        [("completed", "LIS report v1 validated")]
    c = lab["c"]
    for sql, code in (("UPDATE lab_result_values SET numeric_value = 9 WHERE result_id = %s", "lis_report_locked"),
                      ("DELETE FROM lab_result_values WHERE result_id = %s", "lis_report_locked"),
                      ("UPDATE lab_results SET has_critical_value = true WHERE id = %s", "lis_report_locked"),
                      ("UPDATE lab_results SET status = 'amended' WHERE id = %s", "lis_report_locked"),
                      ("DELETE FROM lab_results WHERE id = %s", "lis_append_only")):
        with pytest.raises(psycopg2.Error) as e:
            _q(c, sql, r["id"])
        assert code in str(e.value)
    with pytest.raises(psycopg2.Error):          # no new value rows on a signed report either
        _q(c, """INSERT INTO lab_result_values (clinic_id, result_id, parameter_id, parameter_name, numeric_value)
                 VALUES (%s, %s, %s, 'X', 1)""", lab["clinic"], r["id"], str(uuid.uuid4()))
    _q(c, "UPDATE lab_results SET pdf_storage_path = 'a/b.pdf' WHERE id = %s", r["id"])   # recorded once
    with pytest.raises(psycopg2.Error):
        _q(c, "UPDATE lab_results SET pdf_storage_path = 'c/d.pdf' WHERE id = %s", r["id"])


def test_a_correction_is_a_versioned_amendment(lab):
    _collect(lab)
    v1 = _record(lab["c"], lab)
    _sign(lab, v1)
    with pytest.raises(psycopg2.Error) as e:
        _record(lab["c"], lab, value="11.0")
    assert "lis_amendment_reason_required" in str(e.value)
    v2 = _record(lab["c"], lab, value="11.0", reason="Transcription error", flag="low")
    assert (v2["version"], v2["supersedes_id"], v2["status"]) == (2, v1["id"], "entered")
    assert _q(lab["c"], "SELECT status FROM lab_results WHERE id = %s", v1["id"]) == "validated"   # until v2 is signed
    _sign(lab, v2)
    assert _rows(lab["c"], "SELECT version, status FROM lab_results WHERE order_id = %s ORDER BY version",
                 lab["order"]) == [(1, "amended"), (2, "validated")]
    assert _q(lab["c"], "SELECT numeric_value FROM lab_result_values WHERE result_id = %s", v1["id"]) == Decimal("14.200")


def test_cross_tenant_parameter_and_second_open_result_are_rejected(lab):
    c = lab["c"]
    other = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('Other', %s, 'polyclinic', true)
                         RETURNING id""", "+9189" + uuid.uuid4().hex[:8]))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _q(c, "INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name) VALUES (%s, %s, 'X')", other, lab["cbc"])
    with pytest.raises(psycopg2.errors.CheckViolation):
        _q(c, """INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, normal_low, normal_high)
                 VALUES (%s, %s, 'Bad', 10, 5)""", lab["clinic"], lab["cbc"])
    _collect(lab)
    r = _record(c, lab)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _q(c, """INSERT INTO lab_results (clinic_id, order_id, patient_id, lab_test_id, test_name, version, status,
                 entered_at, technician_name, supersedes_id, amendment_reason)
                 VALUES (%s, %s, %s, %s, 'CBC', 2, 'entered', now(), 't', %s, 'second copy')""",
           lab["clinic"], lab["order"], lab["patient"], lab["cbc"], r["id"])


def test_ten_concurrent_first_entries_create_one_result(lab, real_postgres_uri):
    _collect(lab)
    errors, barrier = [], threading.Barrier(10)

    def worker():
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            barrier.wait()
            _record(conn, lab)
        except Exception as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert _q(lab["c"], "SELECT count(*) FROM lab_results WHERE order_id = %s", lab["order"]) == 1
    assert _q(lab["c"], """SELECT count(*) FROM lab_result_values v JOIN lab_results r ON r.id = v.result_id
                           WHERE r.order_id = %s""", lab["order"]) == 1


def test_owner_purge_removes_signed_results(lab):
    _collect(lab)
    _sign(lab, _record(lab["c"], lab))
    _q(lab["c"], "SELECT ipd_purge_clinic(%s)", lab["clinic"])
    assert _q(lab["c"], "SELECT count(*) FROM lab_results WHERE clinic_id = %s", lab["clinic"]) == 0


def test_rollback_refuses_results_then_reapplies(lab):
    _collect(lab)
    _record(lab["c"], lab)
    cur = lab["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "118_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        # the shared test DB holds other suites' results: the owner purge is the only path that removes them
        cur.execute("SELECT ipd_purge_clinic(clinic_id) FROM (SELECT DISTINCT clinic_id FROM lab_results) x")
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('public.lab_results'), to_regprocedure("
                    "'lis_record_results(uuid,uuid,jsonb,boolean,text,uuid,text)')")
        assert cur.fetchone() == (None, None)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE proname LIKE 'lis\\_%' AND NOT EXISTS
                       (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) c WHERE c LIKE 'search_path=%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
