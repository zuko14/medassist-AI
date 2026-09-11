"""Migration 076 against a real PostgreSQL, not a mock.

The catalogue's name uniqueness is enforced by an index expression, and index
expressions are exactly the thing a mocked query builder cannot check. Two
parts of it are easy to get wrong and impossible to see in Python:

  * branch_id is NULLABLE, and Postgres treats every NULL as distinct. A plain
    UNIQUE (clinic_id, branch_id, name) would therefore constrain nothing for
    all-branches tests -- which is most of the catalogue. The COALESCE to a
    sentinel is what closes that, the same way migration 073 does for lab
    queue tokens.

  * The application compares names with .strip().lower(). An index on raw
    `name` would let "CBC" and " cbc " coexist in the database and then
    collide in Python, where one silently shadows the other.

The de-duplication step gets the same treatment: it is run against real rows
with a real foreign key, because its whole purpose is to not lose booking
history.
"""

import uuid
from pathlib import Path

import psycopg2
import pytest

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "076_lab_tests_unique_name_per_branch.sql"
)

SENTINEL = "00000000-0000-0000-0000-000000000000"
INDEX = "idx_unique_lab_test_name_per_branch"


@pytest.fixture
def catalogue(real_pg_conn):
    """A clinic with two branches, and a clean catalogue around each test."""
    cur = real_pg_conn.cursor()
    tag = f"m076-{uuid.uuid4().hex[:8]}"

    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) "
        "VALUES (%s, %s, 'diagstream', true) RETURNING id;",
        (f"{tag} Diagnostics", "+9190000" + uuid.uuid4().hex[:5]),
    )
    clinic_id = str(cur.fetchone()[0])

    branch_ids = []
    for label in ("Kukatpally", "Madhapur"):
        cur.execute(
            "INSERT INTO branches (clinic_id, name, is_active) "
            "VALUES (%s, %s, true) RETURNING id;",
            (clinic_id, f"{tag} {label}"),
        )
        branch_ids.append(str(cur.fetchone()[0]))

    yield {"conn": real_pg_conn, "clinic_id": clinic_id, "branches": branch_ids}

    cur.execute("DELETE FROM appointments WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM lab_tests WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM branches WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM clinics WHERE id = %s;", (clinic_id,))
    cur.close()


def _add_test(cat, name, branch_id=None, price=50000):
    cur = cat["conn"].cursor()
    cur.execute(
        "INSERT INTO lab_tests (clinic_id, branch_id, name, price_paise, is_active) "
        "VALUES (%s, %s, %s, %s, true) RETURNING id;",
        (cat["clinic_id"], branch_id, name, price),
    )
    return str(cur.fetchone()[0])


def test_index_exists_after_migrations(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s;", (INDEX,))
    row = cur.fetchone()
    assert row, f"{INDEX} was not created by migration 076"
    definition = row[0]
    assert "UNIQUE" in definition
    assert SENTINEL in definition, "nullable branch_id is not COALESCEd"
    assert "lower(btrim(name))" in definition, "name key does not match the app"


def test_same_name_twice_in_one_branch_is_rejected(catalogue):
    _add_test(catalogue, "Complete Blood Count", catalogue["branches"][0])
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _add_test(catalogue, "Complete Blood Count", catalogue["branches"][0])


def test_same_name_at_two_branches_is_allowed(catalogue):
    """Per-branch catalogues are the entire point; this must stay legal."""
    a = _add_test(catalogue, "Lipid Profile", catalogue["branches"][0], price=45000)
    b = _add_test(catalogue, "Lipid Profile", catalogue["branches"][1], price=60000)
    assert a != b


def test_a_branch_row_may_override_an_all_branches_row(catalogue):
    shared = _add_test(catalogue, "Thyroid Profile", None)
    override = _add_test(catalogue, "Thyroid Profile", catalogue["branches"][0])
    assert shared != override


def test_same_name_twice_as_all_branches_is_rejected(catalogue):
    """The NULL-sentinel case a plain UNIQUE constraint would let through."""
    _add_test(catalogue, "Urine Routine", None)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _add_test(catalogue, "Urine Routine", None)


def test_case_and_padding_collide(catalogue):
    """Matches .strip().lower() in the importer and the override rule."""
    _add_test(catalogue, "Vitamin D", None)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _add_test(catalogue, "  vitamin d  ", None)


def test_dedup_retires_losers_and_keeps_booking_history(catalogue):
    """The re-import duplicate bug, cleaned up without losing anything."""
    conn = catalogue["conn"]
    cur = conn.cursor()

    # Reproduce the pre-076 state: the index cannot exist while duplicates do.
    cur.execute(f"DROP INDEX IF EXISTS {INDEX};")

    older = _add_test(catalogue, "Widal Test", None, price=30000)
    cur.execute(
        "UPDATE lab_tests SET updated_at = now() - interval '10 days' WHERE id = %s;",
        (older,),
    )
    newer = _add_test(catalogue, "Widal Test", None, price=35000)

    # A real booking pointing at the row that is about to lose.
    cur.execute(
        "INSERT INTO appointments (clinic_id, patient_phone, department, "
        "appointment_date, booking_type, lab_test_id, lab_test_name, amount_paise) "
        "VALUES (%s, '+919000000001', 'Lab Test', CURRENT_DATE, 'lab_test', %s, "
        "'Widal Test', 30000) RETURNING id;",
        (catalogue["clinic_id"], older),
    )
    appointment_id = str(cur.fetchone()[0])

    cur.execute(MIGRATION.read_text(encoding="utf-8"))

    # The most recently updated row survives, untouched and bookable.
    cur.execute("SELECT name, is_active FROM lab_tests WHERE id = %s;", (newer,))
    name, is_active = cur.fetchone()
    assert name == "Widal Test"
    assert is_active is True

    # The loser is retired, NOT deleted: its price is still there to inspect.
    cur.execute(
        "SELECT name, is_active, price_paise FROM lab_tests WHERE id = %s;", (older,)
    )
    row = cur.fetchone()
    assert row is not None, "migration deleted a row instead of retiring it"
    assert row[0].startswith("Widal Test [duplicate ")
    assert row[1] is False
    assert row[2] == 30000

    # Booking history follows the surviving test rather than being orphaned.
    cur.execute("SELECT lab_test_id FROM appointments WHERE id = %s;", (appointment_id,))
    assert str(cur.fetchone()[0]) == newer

    # ...and the constraint is in place afterwards.
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s;", (INDEX,))
    assert cur.fetchone(), "migration did not recreate the unique index"

    # Re-runnable: a second pass finds nothing to do and still succeeds.
    cur.execute(MIGRATION.read_text(encoding="utf-8"))
    cur.execute(
        "SELECT count(*) FROM lab_tests WHERE clinic_id = %s AND is_active = false;",
        (catalogue["clinic_id"],),
    )
    assert cur.fetchone()[0] == 1, "re-running the migration retired extra rows"
