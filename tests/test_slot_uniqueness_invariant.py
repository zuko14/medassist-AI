"""Adversarial slot-uniqueness tests against real PostgreSQL (KA-P0-01 / RT-01, RT-02).

The pre-existing invariant test (test_real_postgres_invariants.py::test_02) inserts
both rows with doctor_id AND branch_id omitted, so both collapse onto the migration-060
COALESCE sentinel and the index correctly rejects the second. It therefore passed while
the production row shape — doctor_id populated, branch_id populated on one path and NULL
on the other — was double-bookable.

These tests write the shapes production actually writes.

INVARIANT: one clinic + one doctor + one date + one time
           => at most ONE appointment in an active status.
"""

import concurrent.futures

import psycopg2
import pytest

ACTIVE = ("confirmed", "pending_payment", "pending_review")


def _seed(cur):
    """Create a clinic, a doctor and two branches. Returns (clinic, doctor, b1, b2)."""
    cur.execute("SELECT id FROM clinics LIMIT 1;")
    row = cur.fetchone()
    if row:
        clinic_id = str(row[0])
    else:
        cur.execute(
            "INSERT INTO clinics (name, whatsapp_number, plan, is_active) "
            "VALUES ('Slot Invariant Clinic', '+919999999997', 'essential', true) "
            "RETURNING id;"
        )
        clinic_id = str(cur.fetchone()[0])

    cur.execute(
        "INSERT INTO doctors (clinic_id, name, department, specialization, is_active) "
        "VALUES (%s, 'Dr. Rao', 'Cardiology', 'Cardiologist', true) RETURNING id;",
        (clinic_id,),
    )
    doctor_id = str(cur.fetchone()[0])

    cur.execute(
        "INSERT INTO branches (clinic_id, name, is_active) "
        "VALUES (%s, 'Main', true) RETURNING id;",
        (clinic_id,),
    )
    branch_1 = str(cur.fetchone()[0])

    cur.execute(
        "INSERT INTO branches (clinic_id, name, is_active) "
        "VALUES (%s, 'Annexe', true) RETURNING id;",
        (clinic_id,),
    )
    branch_2 = str(cur.fetchone()[0])

    return clinic_id, doctor_id, branch_1, branch_2


_INSERT = """
INSERT INTO appointments (
    clinic_id, doctor_id, branch_id, patient_phone, patient_name,
    department, doctor_name, appointment_date, appointment_time, status, booking_type
) VALUES (%s, %s, %s, %s, %s, 'Cardiology', 'Dr. Rao', %s, %s, %s, 'consultation');
"""


def _active_count(cur, clinic_id, doctor_id, date, time):
    cur.execute(
        "SELECT COUNT(*) FROM appointments "
        "WHERE clinic_id = %s AND doctor_id = %s AND appointment_date = %s "
        "AND appointment_time = %s AND status IN %s;",
        (clinic_id, doctor_id, date, time, ACTIVE),
    )
    return cur.fetchone()[0]


# ── RT-01 ────────────────────────────────────────────────────────────────────

def test_rt01_branch_null_and_branch_set_cannot_both_hold_one_slot(
    real_pg_conn, clean_db
):
    """The exact shape that made KA-P0-01 reproducible.

    Patient A enters via the department-first menu (branch_id never set).
    Patient B enters via the branch-first menu (branch_id set).
    Same clinic, same doctor_id, same date, same minute.

    Before migration 064 both INSERTs were accepted, because
    COALESCE(branch_id, sentinel) put them on two different index keys.
    """
    cur = real_pg_conn.cursor()
    clinic_id, doctor_id, branch_1, _ = _seed(cur)

    cur.execute(
        _INSERT,
        (clinic_id, doctor_id, None, "+919000000001", "A",
         "2026-09-01", "10:00:00", "confirmed"),
    )

    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            _INSERT,
            (clinic_id, doctor_id, branch_1, "+919000000002", "B",
             "2026-09-01", "10:00:00", "confirmed"),
        )

    real_pg_conn.rollback()
    cur = real_pg_conn.cursor()
    assert _active_count(cur, clinic_id, doctor_id, "2026-09-01", "10:00:00") == 1


def test_rt01b_two_different_branches_cannot_both_hold_one_doctor(
    real_pg_conn, clean_db
):
    """A physician cannot be at two branches in the same minute.

    branch_id is deliberately absent from the migration-064 key. This asserts
    that removing it did not merely move the hole from NULL-vs-set to set-vs-set.
    """
    cur = real_pg_conn.cursor()
    clinic_id, doctor_id, branch_1, branch_2 = _seed(cur)

    cur.execute(
        _INSERT,
        (clinic_id, doctor_id, branch_1, "+919000000001", "A",
         "2026-09-02", "11:00:00", "confirmed"),
    )

    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            _INSERT,
            (clinic_id, doctor_id, branch_2, "+919000000002", "B",
             "2026-09-02", "11:00:00", "pending_payment"),
        )


def test_rt01c_pending_payment_holds_the_slot_against_confirmed(
    real_pg_conn, clean_db
):
    """All three active statuses share one key — a 10-minute hold really holds."""
    cur = real_pg_conn.cursor()
    clinic_id, doctor_id, branch_1, _ = _seed(cur)

    cur.execute(
        _INSERT,
        (clinic_id, doctor_id, None, "+919000000001", "A",
         "2026-09-03", "12:00:00", "pending_payment"),
    )
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            _INSERT,
            (clinic_id, doctor_id, branch_1, "+919000000002", "B",
             "2026-09-03", "12:00:00", "confirmed"),
        )


# ── Behaviour that must NOT change ───────────────────────────────────────────

def test_rt01d_different_doctors_do_not_block_each_other(real_pg_conn, clean_db):
    """Migration 060 collapsed unresolved doctors onto one sentinel, so two
    distinct physicians could falsely reject each other. They must not."""
    cur = real_pg_conn.cursor()
    clinic_id, doctor_id, _, _ = _seed(cur)

    cur.execute(
        "INSERT INTO doctors (clinic_id, name, department, specialization, is_active) "
        "VALUES (%s, 'Dr. Iyer', 'Cardiology', 'Cardiologist', true) RETURNING id;",
        (clinic_id,),
    )
    other_doctor = str(cur.fetchone()[0])

    cur.execute(
        _INSERT,
        (clinic_id, doctor_id, None, "+919000000001", "A",
         "2026-09-04", "09:00:00", "confirmed"),
    )
    # Same clinic, same minute, DIFFERENT physician — must be allowed.
    cur.execute(
        _INSERT,
        (clinic_id, other_doctor, None, "+919000000002", "B",
         "2026-09-04", "09:00:00", "confirmed"),
    )

    cur.execute(
        "SELECT COUNT(*) FROM appointments WHERE clinic_id = %s "
        "AND appointment_date = '2026-09-04' AND status IN %s;",
        (clinic_id, ACTIVE),
    )
    assert cur.fetchone()[0] == 2


def test_rt01e_cancelled_slot_is_released(real_pg_conn, clean_db):
    """A cancelled booking must free the slot for the next patient."""
    cur = real_pg_conn.cursor()
    clinic_id, doctor_id, _, _ = _seed(cur)

    cur.execute(
        _INSERT,
        (clinic_id, doctor_id, None, "+919000000001", "A",
         "2026-09-05", "09:30:00", "cancelled"),
    )
    cur.execute(
        _INSERT,
        (clinic_id, doctor_id, None, "+919000000002", "B",
         "2026-09-05", "09:30:00", "confirmed"),
    )
    assert _active_count(cur, clinic_id, doctor_id, "2026-09-05", "09:30:00") == 1


def test_rt01f_lab_bookings_are_unaffected(real_pg_conn, clean_db):
    """Lab bookings write appointment_time = NULL (conversation.py:4044).

    NULLs are distinct in a unique index, so the migration-060 index was
    ALWAYS a no-op for them. Migration 064 excludes booking_type='lab_test'
    explicitly; this asserts that is a no-op too, not a new restriction.
    """
    cur = real_pg_conn.cursor()
    clinic_id, _, branch_1, _ = _seed(cur)

    for phone in ("+919000000001", "+919000000002", "+919000000003"):
        cur.execute(
            """
            INSERT INTO appointments (
                clinic_id, branch_id, patient_phone, patient_name, department,
                appointment_date, appointment_time, status, booking_type
            ) VALUES (%s, %s, %s, 'Lab Patient', 'Diagnostics',
                      '2026-09-06', NULL, 'confirmed', 'lab_test');
            """,
            (clinic_id, branch_1, phone),
        )

    cur.execute(
        "SELECT COUNT(*) FROM appointments WHERE clinic_id = %s "
        "AND booking_type = 'lab_test' AND status IN %s;",
        (clinic_id, ACTIVE),
    )
    assert cur.fetchone()[0] == 3, "lab-test booking behaviour must be unchanged"


# ── RT-02: concurrency ───────────────────────────────────────────────────────

def test_rt02_hundred_concurrent_bookings_yield_exactly_one(
    real_pg_conn, real_postgres_uri, clean_db
):
    """100 real connections race for one slot. Exactly one may win.

    Half carry a branch_id and half do not — the asymmetry that made the
    original defect reachable. Every loser must fail on the slot index, not
    on some unrelated integrity error.
    """
    cur = real_pg_conn.cursor()
    clinic_id, doctor_id, branch_1, _ = _seed(cur)

    date, time = "2026-09-07", "14:00:00"

    def attempt(n: int) -> str:
        conn = psycopg2.connect(real_postgres_uri)
        try:
            conn.autocommit = True
            c = conn.cursor()
            c.execute(
                _INSERT,
                (clinic_id, doctor_id, branch_1 if n % 2 else None,
                 f"+9190000{n:05d}", f"P{n}", date, time, "confirmed"),
            )
            return "won"
        except psycopg2.errors.UniqueViolation as exc:
            assert "uq_appointment_active_slot" in str(exc), (
                f"loser rejected by the wrong constraint: {exc}"
            )
            return "slot_taken"
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:
        results = list(pool.map(attempt, range(100)))

    assert results.count("won") == 1, (
        f"expected exactly 1 winner, got {results.count('won')}"
    )
    assert results.count("slot_taken") == 99

    cur = real_pg_conn.cursor()
    assert _active_count(cur, clinic_id, doctor_id, date, time) == 1


# ── Index shape ──────────────────────────────────────────────────────────────

def test_slot_index_is_not_keyed_on_branch_id(real_pg_conn):
    """Regression guard on the index definition itself.

    If a future migration reintroduces branch_id or a COALESCE sentinel into
    this key, KA-P0-01 returns. Assert on the shape, not just the behaviour.
    """
    cur = real_pg_conn.cursor()
    cur.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_appointment_active_slot';"
    )
    row = cur.fetchone()
    assert row, "uq_appointment_active_slot is missing — the slot guarantee is gone"

    indexdef = row[0].lower()
    assert "unique" in indexdef
    assert "branch_id" not in indexdef, (
        "branch_id is back in the slot key — a NULL branch and a set branch "
        "would occupy different index keys again (KA-P0-01)"
    )
    assert "coalesce" not in indexdef, (
        "a COALESCE sentinel is back in the slot key — NULL folding creates a "
        "second distinct key rather than merging rows (KA-P0-01)"
    )
    assert "doctor_id" in indexdef
