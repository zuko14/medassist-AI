"""Session 20: the data-shape claims behind two fixes, proven on real PostgreSQL.

Both bugs survived because tests mocked the database with shapes production
never returns. These run against the embedded server with every migration
applied, so the schema itself is the judge.
"""

import json
from unittest.mock import MagicMock, patch

import psycopg2
import pytest

from app.services.payment import PaymentService
from tests.conftest_db import get_default_clinic_id


class _Q:
    def __init__(self, data):
        self.data = data

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return MagicMock(data=self.data)


async def _captured_erasure_audit_row(clinic_id: str) -> dict:
    """The admin_audit_logs row anonymize_clinical_records() actually builds."""
    from app.services import data_retention as dr

    inserted = {}

    class _T:
        def __init__(self, name):
            self.name = name

        def select(self, *_):
            return _Q([{"id": "p-1", "name": "X"}] if self.name == "patients" else [])

        def update(self, _payload):
            return _Q([])

        def delete(self):
            return _Q([])

        def insert(self, payload):
            inserted[self.name] = payload
            return _Q([payload])

    sb_mock = MagicMock()
    sb_mock.table.side_effect = _T
    with patch.object(dr, "supabase", sb_mock):
        await dr.DataRetentionService().anonymize_clinical_records(clinic_id, "+919876543210")
    return inserted["admin_audit_logs"]


def _insert_audit(cur, row: dict) -> None:
    cols = list(row)
    cur.execute(
        f"INSERT INTO admin_audit_logs ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
        [json.dumps(row[c]) if isinstance(row[c], dict) else row[c] for c in cols],
    )


@pytest.mark.asyncio
async def test_dpdp_erasure_audit_row_is_accepted_by_the_real_schema(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)
    row = await _captured_erasure_audit_row(clinic_id)

    _insert_audit(cur, row)  # raised on every erasure before the fix
    cur.execute(
        "SELECT action, role, user_id FROM admin_audit_logs WHERE clinic_id = %s", (clinic_id,)
    )
    assert cur.fetchone() == ("DATA_ERASURE_REQUEST", "system", None)


def test_the_pre_fix_audit_row_is_what_the_schema_rejected(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)
    old_row = {
        "clinic_id": clinic_id, "user_id": "dpdp_erasure", "username": "patient_erasure",
        "action": "DATA_ERASURE_REQUEST", "resource_type": "patient",
    }
    with pytest.raises(psycopg2.Error):
        _insert_audit(cur, old_row)


def test_refund_cutoff_parses_the_time_postgres_actually_returns(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)
    cur.execute(
        """
        INSERT INTO appointments (clinic_id, patient_phone, department, doctor_name,
            appointment_date, appointment_time, status)
        VALUES (%s, '+919876543210', 'General', 'Dr. A', '2035-05-15', '10:30', 'confirmed')
        RETURNING appointment_date::text, appointment_time::text
        """,
        (clinic_id,),
    )
    date_str, time_str = cur.fetchone()
    assert time_str == "10:30:00"  # the shape the old "%H:%M" parser rejected
    parsed = PaymentService()._parse_slot_datetime(date_str, time_str)
    assert parsed is not None and (parsed.hour, parsed.minute) == (10, 30)
