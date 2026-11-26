"""Permanent clinic erase (migration 157) on a REAL Postgres through the real owner route.

A clinic with signed OPD records, an issued invoice, a Continuous Care enrolment and an append-only payment event
(no purge flag, ON DELETE RESTRICT) is erased: no row of it remains in any clinic_id table, another
clinic is untouched, and every append-only guard is enabled again afterwards."""

import pytest

from tests.cc_world import care, patient, signed_rx, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _clinic, _doctor, _q, api  # noqa: F401  (fixture)
from tests.test_opd_billing_real_pg import _issued_invoice


def _rows_left(conn, clinic):
    cur = conn.cursor()
    cur.execute("""SELECT c.table_name FROM information_schema.columns c JOIN information_schema.tables t
                   ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'
                   WHERE c.table_schema = 'public' AND c.column_name = 'clinic_id'""")
    left = {}
    for (t,) in cur.fetchall():
        cur.execute(f'SELECT count(*) FROM "{t}" WHERE clinic_id = %s', (clinic,))
        n = cur.fetchone()[0]
        if n:
            left[t] = n
    cur.close()
    return left


@pytest.fixture
def no_storage(monkeypatch):
    from app.routers import platform

    async def files(clinic_id):
        return {"files_removed": 0, "storage_ok": True}
    monkeypatch.setattr(platform, "_erase_clinic_files", files)


def test_erase_removes_every_row_and_only_that_clinic(care, no_storage):  # noqa: F811
    api, clinic, doctor = care.api, care.clinic, care.doctor
    care.enroll()
    p = patient(api.conn, clinic)
    signed_rx(api.conn, clinic, p, doctor, [{"drug_name": "Metformin", "formulation": "tablet", "dosage": "500 mg",
                                            "frequency": "BD"}])
    _issued_invoice(api, clinic)
    appt = _q(api.conn, "SELECT id FROM appointments WHERE clinic_id = %s LIMIT 1", clinic)
    _q(api.conn, "INSERT INTO payment_events (booking_id, event_type, clinic_id) VALUES (%s, 'confirmed', %s)", appt, clinic)

    # production has tables created outside migrations whose clinic_id is TEXT (e.g. callmedex_connector_configs)
    _q(api.conn, "CREATE TABLE IF NOT EXISTS erase_text_probe (clinic_id TEXT NOT NULL)")
    _q(api.conn, "INSERT INTO erase_text_probe VALUES (%s)", clinic)

    other = _clinic(api.conn)
    od = _doctor(api.conn, other)
    signed_rx(api.conn, other, patient(api.conn, other), od, [{"drug_name": "Amlodipine", "formulation": "tablet",
                                                               "dosage": "5 mg", "frequency": "OD"}])
    before_other = _rows_left(api.conn, other)
    assert _rows_left(api.conn, clinic)

    name = _q(api.conn, "SELECT name FROM clinics WHERE id = %s", clinic)
    bad = api.client.delete(f"/platform/clinics/{clinic}", params={"erase": "true", "confirm_name": "wrong"})
    assert bad.status_code == 400 and _q(api.conn, "SELECT count(*) FROM clinics WHERE id = %s", clinic) == 1

    r = api.client.delete(f"/platform/clinics/{clinic}", params={"erase": "true", "confirm_name": name.lower()})
    assert r.status_code == 200, r.text
    assert r.json()["erased"] is True and r.json()["rows_removed"] > 0
    assert _q(api.conn, "SELECT count(*) FROM clinics WHERE id = %s", clinic) == 0
    assert _rows_left(api.conn, clinic) == {}
    assert _q(api.conn, "SELECT count(*) FROM payment_events WHERE booking_id = %s", appt) == 0
    assert _rows_left(api.conn, other) == before_other

    # guards are enabled again: the other clinic's signed encounter still refuses deletion
    assert _q(api.conn, """SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgenabled = 'D'""") == 0
    with pytest.raises(Exception):
        _q(api.conn, "DELETE FROM opd_encounters WHERE clinic_id = %s", other)

    assert api.client.delete(f"/platform/clinics/{clinic}",
                             params={"erase": "true", "confirm_name": name}).status_code == 404
    _q(api.conn, "DROP TABLE erase_text_probe")


def test_erase_is_owner_only_rpc(api):  # noqa: F811
    for role in ("anon", "authenticated"):
        if _q(api.conn, "SELECT count(*) FROM pg_roles WHERE rolname = %s", role):
            assert _q(api.conn, "SELECT has_function_privilege(%s, 'kriya_erase_clinic(uuid)', 'EXECUTE')",
                      role) is False
