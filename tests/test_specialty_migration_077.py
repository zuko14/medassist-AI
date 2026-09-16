"""Migration 077 against a real PostgreSQL (pgserver), never Supabase.

The properties that matter most can only be proven by the database itself:
  * a treatment-tagged consultation is STILL blocked by uq_appointment_active_slot
    (the whole reason booking_type is not widened);
  * deleting a treatment keeps the booking's treatment_name;
  * existing plan values keep working after the CHECK is widened.
"""

import uuid

import psycopg2
import pytest


@pytest.fixture
def clinic(real_pg_conn):
    cur = real_pg_conn.cursor()
    tag = f"m077-{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) "
        "VALUES (%s, %s, 'derma', true) RETURNING id;",
        (f"{tag} Skin Clinic", "+9190001" + uuid.uuid4().hex[:5]),
    )
    clinic_id = str(cur.fetchone()[0])
    cur.execute(
        "INSERT INTO doctors (clinic_id, name, department, specialization) "
        "VALUES (%s, %s, 'Dermatology', 'Dermatologist') RETURNING id;",
        (clinic_id, f"Dr. {tag}"),
    )
    doctor_id = str(cur.fetchone()[0])
    yield {"conn": real_pg_conn, "clinic_id": clinic_id, "doctor_id": doctor_id, "tag": tag}
    cur.execute("DELETE FROM appointments WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM treatment_doctors WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM specialty_treatments WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM doctors WHERE clinic_id = %s;", (clinic_id,))
    cur.execute("DELETE FROM clinics WHERE id = %s;", (clinic_id,))
    cur.close()


def _treatment(c, name="Hair PRP Therapy", **extra):
    cur = c["conn"].cursor()
    cols = {"clinic_id": c["clinic_id"], "name": name, "category": "Hair & Scalp", **extra}
    keys = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    cur.execute(
        f"INSERT INTO specialty_treatments ({keys}) VALUES ({marks}) RETURNING id;",
        list(cols.values()),
    )
    return str(cur.fetchone()[0])


def _appointment(c, treatment_id=None, treatment_name=None, time="10:00:00", status="confirmed"):
    cur = c["conn"].cursor()
    cur.execute(
        "INSERT INTO appointments (clinic_id, patient_phone, department, doctor_name, doctor_id, "
        "appointment_date, appointment_time, status, treatment_id, treatment_name) "
        "VALUES (%s, '+919000000077', 'Dermatology', 'Dr. X', %s, CURRENT_DATE + 3, %s, %s, %s, %s) "
        "RETURNING id;",
        (c["clinic_id"], c["doctor_id"], time, status, treatment_id, treatment_name),
    )
    return str(cur.fetchone()[0])


@pytest.mark.parametrize("plan", ["derma", "eye", "dental", "ivf", "soloclinic", "diagstream",
                                  "diagbooking", "essential", "polyclinic", "enterprise"])
def test_every_plan_value_is_accepted(real_pg_conn, plan):
    cur = real_pg_conn.cursor()
    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, %s, true) RETURNING id;",
        (f"m077 plan {plan}", "+9190002" + uuid.uuid4().hex[:5], plan),
    )
    cid = cur.fetchone()[0]
    cur.execute("DELETE FROM clinics WHERE id = %s;", (cid,))


def test_unknown_plan_is_still_rejected(real_pg_conn):
    cur = real_pg_conn.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('bad', %s, 'dentistry', true);",
            ("+9190003" + uuid.uuid4().hex[:5],),
        )


def test_specialty_plan_tiers_are_seeded(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute(
        "SELECT plan_name, included_messages_month FROM plan_tiers "
        "WHERE plan_name IN ('derma','eye','dental','ivf') ORDER BY plan_name;"
    )
    rows = cur.fetchall()
    assert [r[0] for r in rows] == ["dental", "derma", "eye", "ivf"]
    assert all(r[1] == 2500 for r in rows)


def test_treatment_name_is_unique_per_clinic_ignoring_case_and_padding(clinic):
    _treatment(clinic, "Chemical Peel")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _treatment(clinic, "  chemical peel ")


def test_short_name_longer_than_24_is_rejected(clinic):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _treatment(clinic, "Laser Hair Reduction", short_name="L" * 25)


def test_negative_price_is_rejected(clinic):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _treatment(clinic, "Tooth Filling", price_from_paise=-1)


def test_treatment_doctor_link_is_unique_and_cascades(clinic):
    tid = _treatment(clinic)
    cur = clinic["conn"].cursor()
    cur.execute(
        "INSERT INTO treatment_doctors (clinic_id, treatment_id, doctor_id) VALUES (%s, %s, %s);",
        (clinic["clinic_id"], tid, clinic["doctor_id"]),
    )
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            "INSERT INTO treatment_doctors (clinic_id, treatment_id, doctor_id) VALUES (%s, %s, %s);",
            (clinic["clinic_id"], tid, clinic["doctor_id"]),
        )
    cur.execute("DELETE FROM specialty_treatments WHERE id = %s;", (tid,))
    cur.execute("SELECT COUNT(*) FROM treatment_doctors WHERE treatment_id = %s;", (tid,))
    assert cur.fetchone()[0] == 0


def test_deleting_a_treatment_keeps_the_booking_and_its_name(clinic):
    tid = _treatment(clinic)
    appt = _appointment(clinic, tid, "Hair PRP Therapy")
    cur = clinic["conn"].cursor()
    cur.execute("DELETE FROM specialty_treatments WHERE id = %s;", (tid,))
    cur.execute("SELECT treatment_id, treatment_name, booking_type FROM appointments WHERE id = %s;", (appt,))
    treatment_id, treatment_name, booking_type = cur.fetchone()
    assert treatment_id is None
    assert treatment_name == "Hair PRP Therapy"
    assert booking_type == "consultation"


def test_treatment_tag_does_not_bypass_the_double_booking_index(clinic):
    """THE invariant. Two active bookings for one doctor at one minute must
    collide whether or not they carry a treatment."""
    t1 = _treatment(clinic, "Hair PRP Therapy")
    t2 = _treatment(clinic, "Acne Treatment")
    _appointment(clinic, t1, "Hair PRP Therapy")
    with pytest.raises(psycopg2.errors.UniqueViolation) as exc:
        _appointment(clinic, t2, "Acne Treatment")
    assert "uq_appointment_active_slot" in str(exc.value)


def test_untagged_and_tagged_bookings_also_collide(clinic):
    t1 = _treatment(clinic, "Chemical Peel")
    _appointment(clinic)  # plain consultation, no treatment
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _appointment(clinic, t1, "Chemical Peel")


def test_booking_type_constraint_is_untouched(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'appointments_booking_type_check';"
    )
    definition = cur.fetchone()[0]
    assert "treatment" not in definition
    assert "consultation" in definition and "lab_test" in definition
