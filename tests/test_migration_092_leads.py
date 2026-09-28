"""Migration 092 against real PostgreSQL: admin_whatsapp_leads() stages,
interests, filters, paging, tenant isolation and API-role lockdown."""

import json
import uuid

import pytest

SIG = "public.admin_whatsapp_leads(uuid, text, text, text, integer, integer, integer)"


def _clinic(cur, name):
    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, 'dental', true) RETURNING id",
        (name, "+9198" + uuid.uuid4().hex[:8]),
    )
    return str(cur.fetchone()[0])


def _contact(cur, clinic, phone, name=None, active_hours_ago=None, created_days_ago=30,
             opted_in=True, consent=None, state="main_menu", declined=False):
    cur.execute(
        "INSERT INTO patients (clinic_id, phone, name, opted_in, data_consent, created_at, data_consent_declined_at) "
        "VALUES (%s, %s, %s, %s, %s, now() - make_interval(days => %s), CASE WHEN %s THEN now() END)",
        (clinic, phone, name, opted_in, consent, created_days_ago, declined),
    )
    if active_hours_ago is not None:
        # handle_message sets session_expires_at = last message + 24h.
        cur.execute(
            "INSERT INTO conversations (clinic_id, phone, state, session_expires_at) "
            "VALUES (%s, %s, %s, now() - make_interval(hours => %s) + interval '24 hours')",
            (clinic, phone, state, active_hours_ago),
        )


def _interest(cur, clinic, phone, intent=None, department=None, days_ago=0):
    cur.execute(
        "INSERT INTO analytics_events (clinic_id, phone, event_type, intent, department, created_at) "
        "VALUES (%s, %s, 'lead_interest', %s, %s, now() - make_interval(days => %s))",
        (clinic, phone, intent, department, days_ago),
    )


def _erased(cur, clinic, phone, hours_ago):
    # What _handle_data_deletion leaves: patients row kept, conversation and
    # analytics purged, then a 'data_deleted' event logged.
    cur.execute("DELETE FROM conversations WHERE clinic_id = %s AND phone = %s", (clinic, phone))
    cur.execute("DELETE FROM analytics_events WHERE clinic_id = %s AND phone = %s", (clinic, phone))
    cur.execute(
        "INSERT INTO analytics_events (clinic_id, phone, event_type, created_at) "
        "VALUES (%s, %s, 'data_deleted', now() - make_interval(hours => %s))",
        (clinic, phone, hours_ago),
    )


def _appt(cur, clinic, phone, status):
    cur.execute(
        "INSERT INTO appointments (clinic_id, patient_phone, department, appointment_date, appointment_time, status) "
        "VALUES (%s, %s, 'General', current_date, (now() + make_interval(mins => (random() * 600)::int))::time, %s)",
        (clinic, phone, status),
    )


def _leads(cur, clinic, segment="all", interest=None, search=None, days=30, limit=50, offset=0):
    cur.execute("SELECT public.admin_whatsapp_leads(%s, %s, %s, %s, %s, %s, %s)",
                (clinic, segment, interest, search, days, limit, offset))
    out = cur.fetchone()[0]
    return out if isinstance(out, dict) else json.loads(out)


@pytest.fixture
def world(real_pg_conn):
    cur = real_pg_conn.cursor()
    a = _clinic(cur, "Leads A 092")
    b = _clinic(cur, "Leads B 092")
    # Clinic A
    _contact(cur, a, "+919800000001", "Ravi", active_hours_ago=2, created_days_ago=1, consent=False)  # hot, new
    _contact(cur, a, "+919800000002", "Sita", active_hours_ago=72)                  # warm
    _contact(cur, a, "+919800000003", None, active_hours_ago=24 * 20)              # cold, 20 days
    _contact(cur, a, "+919800000004", "Booked Bala", active_hours_ago=5)            # booked
    _contact(cur, a, "+919800000005", "Stop Sam", active_hours_ago=1, opted_in=False)   # dnc
    _contact(cur, a, "+919800000006", "No Consent", active_hours_ago=1, consent=False,
             declined=True)                                                          # dnc: said No
    _contact(cur, a, "+919800000007", "Unpaid 100%_x", active_hours_ago=30,
             state="awaiting_payment")                                                    # warm, unpaid
    _contact(cur, a, "+919800000008", "Erased", active_hours_ago=3)
    _interest(cur, a, "+919800000008", "book_appointment")
    _erased(cur, a, "+919800000008", hours_ago=2)
    _contact(cur, a, "+919800000009", "Old Contact", created_days_ago=400)           # no conversation row
    _appt(cur, a, "+919800000004", "confirmed")
    _appt(cur, a, "+919800000007", "pending_payment")
    _appt(cur, a, "+919800000002", "cancelled")
    _interest(cur, a, "+919800000001", "book_appointment")
    _interest(cur, a, "+919800000001", "book_appointment")
    _interest(cur, a, "+919800000001", "lab_tests")
    _interest(cur, a, "+919800000001", department="Cardiology")
    _interest(cur, a, "+919800000002", "lab_tests")
    _interest(cur, a, "+919800000002", "doctors", days_ago=60)   # outside a 30-day window
    # Clinic B: SAME phone as Ravi, and interests that must never show in A.
    _contact(cur, b, "+919800000001", "Ravi at B", active_hours_ago=1)
    _interest(cur, b, "+919800000001", "emergency")
    _interest(cur, b, "+919800000001", "emergency")
    _appt(cur, b, "+919800000001", "confirmed")
    yield cur, a, b
    cur.execute("DELETE FROM analytics_events WHERE clinic_id IN (%s, %s)", (a, b))
    cur.execute("DELETE FROM appointments WHERE clinic_id IN (%s, %s)", (a, b))
    cur.execute("DELETE FROM conversations WHERE clinic_id IN (%s, %s)", (a, b))
    cur.execute("DELETE FROM patients WHERE clinic_id IN (%s, %s)", (a, b))
    cur.execute("DELETE FROM clinics WHERE id IN (%s, %s)", (a, b))


def _by_phone(out):
    return {r["phone"]: r for r in out["rows"]}


def test_092_every_sender_is_a_contact_with_the_right_stage(world):
    cur, a, _ = world
    rows = _by_phone(_leads(cur, a))
    assert rows["+919800000001"]["stage"] == "hot"
    assert rows["+919800000001"]["window_open"] is True
    assert rows["+919800000001"]["window_closes_at"] is not None
    assert rows["+919800000002"]["stage"] == "warm"       # a cancelled booking is not a booking
    assert rows["+919800000002"]["window_open"] is False
    assert rows["+919800000003"]["stage"] == "cold"
    assert rows["+919800000003"]["name"] is None          # a bare "hi" with no name still counts
    assert rows["+919800000004"]["stage"] == "booked"
    assert rows["+919800000007"]["stage"] == "warm"
    assert rows["+919800000007"]["unpaid"] == 1
    # Raw state; the API maps it to mid_booking with MID_BOOKING_STATES.
    assert rows["+919800000007"]["state"] == "awaiting_payment"
    assert rows["+919800000001"]["state"] == "main_menu"


def test_092_activity_window(world):
    cur, a, _ = world
    assert "+919800000009" not in _by_phone(_leads(cur, a, days=30))   # last seen 400 days ago
    assert "+919800000009" in _by_phone(_leads(cur, a, days=0))        # all time
    assert "+919800000003" not in _by_phone(_leads(cur, a, days=7))


def test_093_unanswered_consent_is_a_lead_with_its_number(world):
    cur, a, _ = world
    ravi = _by_phone(_leads(cur, a))["+919800000001"]      # data_consent=false, never answered
    assert ravi["stage"] == "hot" and ravi["phone"] == "+919800000001"


def test_092_do_not_contact_numbers_are_masked(world):
    cur, a, _ = world
    out = _leads(cur, a, segment="dnc")
    assert out["total"] == 2
    for r in out["rows"]:
        assert r["stage"] == "dnc"
        assert r["phone"].startswith("+91") and "•" in r["phone"] and "98000000" not in r["phone"]


def test_092_erased_patients_never_appear(world):
    cur, a, _ = world
    assert _leads(cur, a, days=0, search="Erased")["total"] == 0
    interests = {i["label"]: i for i in _leads(cur, a)["interests"]}
    assert interests["book_appointment"]["patients"] == 1   # Ravi only: the erased contact's rows were purged


def test_092_erased_patient_who_messages_again_is_a_contact_again(world):
    cur, a, _ = world
    cur.execute(
        "INSERT INTO conversations (clinic_id, phone, state, session_expires_at) "
        "VALUES (%s, '+919800000008', 'main_menu', now() + interval '23 hours')", (a,))
    row = _by_phone(_leads(cur, a))["+919800000008"]
    assert row["stage"] == "hot"


def test_092_interests_are_counted_per_clinic_and_window(world):
    cur, a, _ = world
    out = _leads(cur, a)
    interests = {i["label"]: i for i in out["interests"]}
    assert interests["book_appointment"] == {"label": "book_appointment", "patients": 1, "requests": 2}
    assert interests["lab_tests"]["patients"] == 2
    assert "doctors" not in interests                     # 60 days old
    assert "emergency" not in interests                   # clinic B's
    assert out["departments"] == [{"label": "Cardiology", "patients": 1}]
    ravi = _by_phone(out)["+919800000001"]
    assert ravi["interests"] == ["book_appointment", "lab_tests"]
    assert ravi["departments"] == ["Cardiology"]
    assert ravi["interactions"] == 4


def test_092_same_phone_in_two_clinics_never_leaks(world):
    cur, a, b = world
    ravi_a = _by_phone(_leads(cur, a))["+919800000001"]
    assert ravi_a["name"] == "Ravi" and ravi_a["bookings"] == 0 and ravi_a["stage"] == "hot"
    out_b = _leads(cur, b)
    assert out_b["total"] == 1
    ravi_b = out_b["rows"][0]
    assert ravi_b["name"] == "Ravi at B" and ravi_b["stage"] == "booked"
    assert ravi_b["interests"] == ["emergency"]


def test_092_summary_counts(world):
    cur, a, _ = world
    s = _leads(cur, a)["summary"]
    # In the 30-day window: 1..7 (8 was erased, 9 was last seen 400 days ago).
    assert s["active"] == 7
    assert s["contacts_all_time"] == 8
    assert (s["hot"], s["booked"], s["dnc"]) == (1, 1, 2)
    assert s["open"] == 4                                 # hot + warm x2 + cold
    assert s["unpaid"] == 1
    assert s["new"] == 1                                  # only Ravi first messaged inside the window


def test_092_filters_search_and_paging(world):
    cur, a, _ = world
    assert _leads(cur, a, segment="hot")["total"] == 1
    assert _leads(cur, a, segment="open")["total"] == 4
    assert _leads(cur, a, segment="booked")["total"] == 1
    assert {r["phone"] for r in _leads(cur, a, interest="lab_tests")["rows"]} == {"+919800000001", "+919800000002"}
    assert _leads(cur, a, search="ravi")["total"] == 1
    assert _leads(cur, a, search="0000004")["total"] == 1          # phone fragment
    assert _leads(cur, a, search="98000 00007")["total"] == 1      # spaces ignored for phones
    assert _leads(cur, a, search="100%_")["total"] == 1            # wildcards are literal
    assert _leads(cur, a, search="%")["total"] == 1                # not "match everything"

    first = _leads(cur, a, limit=3, offset=0)
    second = _leads(cur, a, limit=3, offset=3)
    assert first["total"] == second["total"] == 7
    assert len(first["rows"]) == 3
    assert not {r["phone"] for r in first["rows"]} & {r["phone"] for r in second["rows"]}
    actives = [r["last_active_at"] for r in first["rows"]]
    assert actives == sorted(actives, reverse=True)


def test_092_bad_parameters_are_clamped_not_fatal(world):
    cur, a, _ = world
    out = _leads(cur, a, segment=None, days=-5, limit=100000, offset=-1)
    assert out["total"] == 8                              # days<=0 means all time; the erased contact excluded
    assert len(out["rows"]) == 8


def test_092_unknown_clinic_is_empty_not_an_error(real_pg_conn):
    cur = real_pg_conn.cursor()
    out = _leads(cur, str(uuid.uuid4()))
    assert out["total"] == 0 and out["rows"] == [] and out["interests"] == []


def test_092_not_callable_by_public(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", (SIG,))
    assert cur.fetchone()[0] is False
    for role in ("anon", "authenticated"):
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone():
            cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, SIG))
            assert cur.fetchone()[0] is False


def test_092_partial_index_exists(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_analytics_lead_interest'")
    (definition,) = cur.fetchone()
    assert "lead_interest" in definition
