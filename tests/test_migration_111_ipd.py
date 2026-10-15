"""Real PostgreSQL tests for migration 111 (Kriya IPD OS core).

Every IPD invariant lives in the database (functions + triggers + partial
unique indexes), so it is proven here against real Postgres, including races:
one bed never holds two stays, a failed transfer changes nothing, records are
immutable, charges and bills are idempotent, discharge prerequisites hold.
"""

import concurrent.futures
import json
import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"


# ── helpers ──────────────────────────────────────────────────────────────────

def _q(cur, sql, *args):
    cur.execute(sql, args)
    return cur.fetchone()[0] if cur.description else None


def _clinic(cur, tariffs=None):
    settings = {"bed_tariffs": tariffs or {"general": 150000, "icu": 600000}}
    return str(_q(cur, """
        INSERT INTO clinics (name, whatsapp_number, plan, is_active, features, opd_state, ipd_state, ipd_settings)
        VALUES ('IPD Hospital', %s, 'multispecialty', true, '{"opd_enabled": true, "ipd_enabled": true}'::jsonb,
                'READY', 'ACTIVE', %s::jsonb) RETURNING id""",
        "+9197" + uuid.uuid4().hex[:8], json.dumps(settings)))


def _doctor(cur, clinic, reg="KMC/2015/1234"):
    return str(_q(cur, """INSERT INTO doctors (clinic_id, name, department, specialization, registration_number,
                          registration_council) VALUES (%s, 'Dr. Rao', 'Medicine', 'Physician', %s, 'KMC') RETURNING id""",
                  clinic, reg))


def _patient(cur, clinic, gender="male"):
    return str(_q(cur, """INSERT INTO patients (clinic_id, name, phone, gender, age_years, age_recorded_on)
                          VALUES (%s, 'Ravi Kumar', %s, %s, 40, CURRENT_DATE) RETURNING id""",
                  clinic, "+9190" + uuid.uuid4().hex[:8], gender))


def _ward(cur, clinic, code=None, gender="any", ward_type="general"):
    return str(_q(cur, """INSERT INTO ipd_wards (clinic_id, code, name, ward_type, gender_restriction)
                          VALUES (%s, %s, 'General Ward', %s, %s) RETURNING id""",
                  clinic, code or "W" + uuid.uuid4().hex[:5], ward_type, gender))


def _bed(cur, clinic, ward, category="general"):
    return str(_q(cur, """INSERT INTO ipd_beds (clinic_id, ward_id, code, bed_category) VALUES (%s, %s, %s, %s)
                          RETURNING id""", clinic, ward, "B" + uuid.uuid4().hex[:6], category))


def _admit(cur, clinic, patient, doctor, bed=None, **extra):
    payload = {"patient_id": patient, "admitting_doctor_id": doctor, "reason": "Fever for 5 days", **extra}
    cur.execute("SELECT ipd_admit(%s, %s::jsonb, %s, NULL, 'desk')", (clinic, json.dumps(payload), bed))
    return cur.fetchone()[0]


def _bed_row(cur, bed):
    cur.execute("SELECT status, current_admission_id::text, reserved_admission_id::text FROM ipd_beds WHERE id = %s",
                (bed,))
    return cur.fetchone()


def _err(fn):
    with pytest.raises(psycopg2.Error) as e:
        fn()
    return e.value


@pytest.fixture
def world(real_pg_conn):
    cur = real_pg_conn.cursor()
    c = _clinic(cur)
    w = _ward(cur, c)
    return {"cur": cur, "clinic": c, "doctor": _doctor(cur, c), "ward": w,
            "bed": _bed(cur, c, w), "bed2": _bed(cur, c, w)}


def _parallel(uri, n, work):
    """Run work(cursor, i) on n separate connections at once; 'ok' or the first error line."""
    def run(i):
        conn = psycopg2.connect(uri)
        conn.autocommit = True
        try:
            work(conn.cursor(), i)
            return "ok"
        except psycopg2.Error as e:
            return (e.pgerror or str(e)).splitlines()[0]
        finally:
            conn.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(run, range(n)))


# ── migration mechanics ──────────────────────────────────────────────────────

def test_reapply_is_idempotent(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("BEGIN")
    cur.execute((MIG / "111_ipd_os_core.sql").read_text(encoding="utf-8"))
    cur.execute("ROLLBACK")


def test_existing_clinics_default_off(real_pg_conn):
    cur = real_pg_conn.cursor()
    cid = _q(cur, "INSERT INTO clinics (name, whatsapp_number, plan) VALUES ('Plain', %s, 'essential') RETURNING id",
             "+9196" + uuid.uuid4().hex[:8])
    cur.execute("SELECT ipd_state, ipd_settings FROM clinics WHERE id = %s", (cid,))
    assert cur.fetchone() == ("OFF", {})


def test_every_ipd_function_has_pinned_search_path(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute(r"""SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public' AND p.proname LIKE 'ipd\_%%'
                      AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(p.proconfig, '{}')) c WHERE c LIKE 'search_path=%%')""")
    assert cur.fetchall() == []


# ── admission + bed allocation ───────────────────────────────────────────────

def test_admit_into_bed_occupies_it_and_records_history(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    assert a["status"] == "admitted" and a["current_bed_id"] == world["bed"]
    assert a["admission_number"].startswith("IPD-")
    assert _bed_row(cur, world["bed"])[:2] == ("occupied", a["id"])
    cur.execute("SELECT count(*) FROM ipd_bed_assignments WHERE admission_id = %s AND ended_at IS NULL", (a["id"],))
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT array_agg(event_type ORDER BY occurred_at, event_type) FROM ipd_events WHERE admission_id = %s",
                (a["id"],))
    assert sorted(cur.fetchone()[0]) == ["admission_created", "bed_assigned"]


def test_occupied_bed_is_refused_and_leaves_no_admission(world):
    cur, c = world["cur"], world["clinic"]
    _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    p = _patient(cur, c)
    assert "ipd_bed_unavailable:occupied" in _err(lambda: _admit(cur, c, p, world["doctor"], world["bed"])).pgerror
    cur.execute("SELECT count(*) FROM ipd_admissions WHERE patient_id = %s", (p,))
    assert cur.fetchone()[0] == 0   # the whole admission rolled back with the bed failure


def test_twenty_concurrent_admissions_to_one_bed_exactly_one_wins(world, real_postgres_uri):
    cur, c = world["cur"], world["clinic"]
    patients = [_patient(cur, c) for _ in range(20)]
    results = _parallel(real_postgres_uri, 20,
                        lambda k, i: _admit(k, c, patients[i], world["doctor"], world["bed"]))
    assert results.count("ok") == 1, results
    assert all("ipd_bed_unavailable" in r for r in results if r != "ok"), results
    cur.execute("SELECT count(*) FROM ipd_admissions WHERE current_bed_id = %s", (world["bed"],))
    assert cur.fetchone()[0] == 1


def test_one_active_stay_per_patient_even_concurrently(world, real_postgres_uri):
    cur, c = world["cur"], world["clinic"]
    p = _patient(cur, c)
    results = _parallel(real_postgres_uri, 8, lambda k, i: _admit(k, c, p, world["doctor"]))
    assert results.count("ok") == 1, results
    assert all("ipd_active_admission_exists" in r for r in results if r != "ok"), results


def test_idempotency_key_replays_the_same_admission(world):
    cur, c = world["cur"], world["clinic"]
    p = _patient(cur, c)
    a1 = _admit(cur, c, p, world["doctor"], idempotency_key="key-" + uuid.uuid4().hex)
    a2 = _admit(cur, c, p, world["doctor"], idempotency_key=a1["idempotency_key"])
    assert a1["id"] == a2["id"] and a2.get("replayed") is True


def test_cross_tenant_ids_are_not_found(world):
    cur, c = world["cur"], world["clinic"]
    other = _clinic(cur)
    other_bed = _bed(cur, other, _ward(cur, other))
    other_patient = _patient(cur, other)
    assert "ipd_bed_not_found" in _err(lambda: _admit(cur, c, _patient(cur, c), world["doctor"], other_bed)).pgerror
    assert "ipd_patient_not_found" in _err(lambda: _admit(cur, c, other_patient, world["doctor"])).pgerror
    assert "ipd_doctor_not_found" in _err(
        lambda: _admit(cur, c, _patient(cur, c), _doctor(cur, other))).pgerror
    # composite FK: even a hand-written insert cannot cross tenants
    e = _err(lambda: cur.execute(
        """INSERT INTO ipd_admissions (clinic_id, admission_year, admission_seq, patient_id, admitting_doctor_id,
           attending_doctor_id, source, reason) VALUES (%s, 2026, 999999, %s, %s, %s, 'direct', 'xyz')""",
        (c, other_patient, world["doctor"], world["doctor"])))
    assert e.pgcode == "23503"


def test_bed_state_cannot_be_written_directly(world):
    cur = world["cur"]
    e = _err(lambda: cur.execute("UPDATE ipd_beds SET status = 'maintenance' WHERE id = %s", (world["bed"],)))
    assert "ipd_bed_state_via_function_only" in e.pgerror
    cur.execute("UPDATE ipd_beds SET room_label = 'R-101' WHERE id = %s", (world["bed"],))  # config stays editable


def test_gender_restricted_ward(world):
    cur, c = world["cur"], world["clinic"]
    fbed = _bed(cur, c, _ward(cur, c, gender="female"))
    assert "ipd_bed_ineligible:gender" in _err(
        lambda: _admit(cur, c, _patient(cur, c, "male"), world["doctor"], fbed)).pgerror
    assert "gender_unknown" in _err(lambda: _admit(cur, c, _patient(cur, c, None), world["doctor"], fbed)).pgerror
    assert _admit(cur, c, _patient(cur, c, "female"), world["doctor"], fbed)["status"] == "admitted"


def test_reservation_hold_expiry_and_cancel(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"])
    assert a["status"] == "pending"
    cur.execute("SELECT ipd_reserve_bed(%s, %s, %s, 30, NULL, 'desk')", (c, a["id"], world["bed"]))
    other = _admit(cur, c, _patient(cur, c), world["doctor"])
    assign = lambda: cur.execute("SELECT ipd_assign_bed(%s, %s, %s, NULL, 'desk')", (c, other["id"], world["bed"]))
    assert "ipd_bed_unavailable:reserved" in _err(assign).pgerror
    # expire it, the way only the functions may touch bed state
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('kriya.ipd_tx', %s, true)", (c,))
    cur.execute("UPDATE ipd_beds SET reserved_until = now() - interval '1 minute' WHERE id = %s", (world["bed"],))
    cur.execute("COMMIT")
    assign()
    assert _bed_row(cur, world["bed"])[:2] == ("occupied", other["id"])
    cur.execute("SELECT ipd_cancel_admission(%s, %s, 'Patient went home', NULL, 'desk')", (c, a["id"]))
    cur.execute("SELECT count(*) FROM ipd_beds WHERE reserved_admission_id = %s", (a["id"],))
    assert cur.fetchone()[0] == 0


def test_own_reservation_is_released_when_admitted_elsewhere(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"])
    cur.execute("SELECT ipd_reserve_bed(%s, %s, %s, 30, NULL, 'desk')", (c, a["id"], world["bed"]))
    cur.execute("SELECT ipd_assign_bed(%s, %s, %s, NULL, 'desk')", (c, a["id"], world["bed2"]))
    assert _bed_row(cur, world["bed"])[0] == "available"
    assert _bed_row(cur, world["bed2"])[0] == "occupied"


# ── transfers ────────────────────────────────────────────────────────────────

def _transfer(cur, c, adm, to_bed, expected):
    cur.execute("SELECT ipd_transfer(%s, %s, %s, %s, 'Needs monitoring', NULL, 'nurse')", (c, adm, to_bed, expected))
    return cur.fetchone()[0]


def test_transfer_moves_patient_and_keeps_history(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    t = _transfer(cur, c, a["id"], world["bed2"], world["bed"])
    assert t["id"] == a["id"] and t["current_bed_id"] == world["bed2"]
    assert _bed_row(cur, world["bed"])[:2] == ("cleaning", None)
    cur.execute("SELECT bed_id::text, end_reason FROM ipd_bed_assignments WHERE admission_id = %s ORDER BY started_at",
                (a["id"],))
    assert cur.fetchall() == [(world["bed"], "transfer"), (world["bed2"], None)]
    cur.execute("SELECT count(*) FROM ipd_admissions WHERE patient_id = %s", (a["patient_id"],))
    assert cur.fetchone()[0] == 1   # still one stay, same admission number


def test_failed_transfer_changes_nothing(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    _admit(cur, c, _patient(cur, c), world["doctor"], world["bed2"])
    before = (_bed_row(cur, world["bed"]), _bed_row(cur, world["bed2"]))
    assert "ipd_bed_unavailable" in _err(lambda: _transfer(cur, c, a["id"], world["bed2"], world["bed"])).pgerror
    assert (_bed_row(cur, world["bed"]), _bed_row(cur, world["bed2"])) == before
    cur.execute("SELECT count(*) FROM ipd_bed_assignments WHERE admission_id = %s AND ended_at IS NULL", (a["id"],))
    assert cur.fetchone()[0] == 1


def test_stale_transfer_is_refused(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    assert "ipd_stale_bed" in _err(lambda: _transfer(cur, c, a["id"], world["bed2"], world["bed2"])).pgerror


def test_concurrent_transfers_into_one_bed(world, real_postgres_uri):
    cur, c = world["cur"], world["clinic"]
    beds = [_bed(cur, c, world["ward"]) for _ in range(6)]
    adms = [_admit(cur, c, _patient(cur, c), world["doctor"], b) for b in beds]
    target = world["bed2"]
    results = _parallel(real_postgres_uri, 6, lambda k, i: _transfer(k, c, adms[i]["id"], target, beds[i]))
    assert results.count("ok") == 1, results
    cur.execute("SELECT count(*) FROM ipd_admissions WHERE current_bed_id = %s", (target,))
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT count(*) FROM ipd_bed_assignments WHERE bed_id = %s AND ended_at IS NULL", (target,))
    assert cur.fetchone()[0] == 1


def test_concurrent_swap_does_not_deadlock(world, real_postgres_uri):
    """A: bed1 -> bed3 while B: bed3... (B in bed2 -> bed1 after A leaves). Lock order is by bed id."""
    cur, c = world["cur"], world["clinic"]
    b3 = _bed(cur, c, world["ward"])
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    b = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed2"])
    moves = [(a["id"], b3, world["bed"]), (b["id"], world["bed"], world["bed2"])]
    results = _parallel(real_postgres_uri, 2, lambda k, i: _transfer(k, c, *moves[i]))
    assert "deadlock" not in " ".join(results)
    cur.execute("SELECT count(*) FROM ipd_beds WHERE clinic_id = %s AND status = 'occupied'", (c,))
    assert cur.fetchone()[0] == 2


def test_bed_status_rules(world):
    cur, c = world["cur"], world["clinic"]
    setst = lambda bed, s, r: cur.execute("SELECT ipd_set_bed_status(%s, %s, %s, %s, NULL, 'x')", (c, bed, s, r))
    assert "ipd_reason_required" in _err(lambda: setst(world["bed"], "maintenance", "")).pgerror
    setst(world["bed"], "maintenance", "Broken rail")
    setst(world["bed"], "maintenance", "Broken rail")   # retry is a no-op
    assert "ipd_bed_unavailable:maintenance" in _err(
        lambda: _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])).pgerror
    _admit(cur, c, _patient(cur, c), world["doctor"], world["bed2"])
    assert "ipd_bed_in_use" in _err(lambda: setst(world["bed2"], "available", None)).pgerror


# ── clinical records ─────────────────────────────────────────────────────────

def _obs(cur, c, adm, **v):
    cols = {"clinic_id": c, "admission_id": adm, "kind": "vitals", "recorded_by_name": "Nurse A", **v}
    cur.execute(f"INSERT INTO ipd_observations ({', '.join(cols)}, observed_at) "
                f"VALUES ({', '.join(['%s'] * len(cols))}, now()) RETURNING id", list(cols.values()))
    return str(cur.fetchone()[0])


def test_observation_immutable_and_corrected_with_provenance(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    o = _obs(cur, c, a["id"], pulse_bpm=88, bp_systolic=120, bp_diastolic=80)
    assert "ipd_record_locked" in _err(lambda: cur.execute(
        "UPDATE ipd_observations SET pulse_bpm = 90 WHERE id = %s", (o,))).pgerror
    assert "ipd_append_only" in _err(lambda: cur.execute("DELETE FROM ipd_observations WHERE id = %s", (o,))).pgerror
    cur.execute("SELECT ipd_correct_observation(%s, %s, 'Wrong patient chart', %s::jsonb, NULL, 'Nurse B')",
                (c, o, json.dumps({"pulse_bpm": 78, "bp_systolic": 118, "bp_diastolic": 76})))
    r = cur.fetchone()[0]
    assert r["replacement"]["corrects_id"] == o and r["replacement"]["pulse_bpm"] == 78
    cur.execute("SELECT status, error_reason FROM ipd_observations WHERE id = %s", (o,))
    assert cur.fetchone() == ("entered_in_error", "Wrong patient chart")


def test_observation_typo_bounds_and_shape(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    assert _err(lambda: _obs(cur, c, a["id"], pulse_bpm=880)).pgcode == "23514"
    assert _err(lambda: _obs(cur, c, a["id"], bp_systolic=120)).pgcode == "23514"   # BP needs both values
    assert _err(lambda: _obs(cur, c, a["id"], kind="intake")).pgcode == "23514"     # I/O needs volume + route
    _obs(cur, c, a["id"], kind="output", io_route="urine", volume_ml=400)


def test_no_clinical_writes_on_a_stay_without_a_bed(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"])
    assert "ipd_admission_not_active" in _err(lambda: _obs(cur, c, a["id"], pulse_bpm=80)).pgerror


def test_notes_and_amendment_by_author_only(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    author = str(uuid.uuid4())
    assert _err(lambda: cur.execute(
        "INSERT INTO ipd_notes (clinic_id, admission_id, note_type, body, author_name) VALUES (%s,%s,'progress','x','Y')",
        (c, a["id"]))).pgcode == "23514"   # progress notes need a doctor author
    n = str(_q(cur, """INSERT INTO ipd_notes (clinic_id, admission_id, note_type, body, author_id, author_name,
                       author_doctor_id) VALUES (%s,%s,'progress','Stable',%s,'Dr. Rao',%s) RETURNING id""",
               c, a["id"], author, world["doctor"]))
    assert "ipd_record_locked" in _err(lambda: cur.execute("UPDATE ipd_notes SET body = 'x' WHERE id = %s", (n,))).pgerror
    assert "ipd_not_author" in _err(lambda: cur.execute(
        "SELECT ipd_amend_note(%s,%s,'Stable, afebrile','typo',NULL,%s,'Other')", (c, n, str(uuid.uuid4())))).pgerror
    cur.execute("SELECT ipd_amend_note(%s,%s,'Stable, afebrile','typo',NULL,%s,'Dr. Rao')", (c, n, author))
    assert cur.fetchone()[0]["amends_id"] == n
    cur.execute("SELECT status FROM ipd_notes WHERE id = %s", (n,))
    assert cur.fetchone()[0] == "amended"


def test_mar_rules(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    med = str(_q(cur, """INSERT INTO ipd_orders (clinic_id, admission_id, order_type, description, drug_name, dose, route,
                         frequency, ordered_by_doctor_id, ordered_by_name)
                         VALUES (%s,%s,'medication','Inj Ceftriaxone','Ceftriaxone','1 g','iv','BD',%s,'Dr. Rao')
                         RETURNING id""", c, a["id"], world["doctor"]))
    diet = str(_q(cur, """INSERT INTO ipd_orders (clinic_id, admission_id, order_type, description, ordered_by_doctor_id,
                          ordered_by_name) VALUES (%s,%s,'diet','Soft diet',%s,'Dr. Rao') RETURNING id""",
                  c, a["id"], world["doctor"]))
    mar = ("INSERT INTO ipd_med_administrations (clinic_id, admission_id, order_id, outcome, administered_at, reason, "
           "recorded_by_name) VALUES (%s,%s,%s,%s,now(),%s,'Nurse A')")
    assert "ipd_order_not_medication" in _err(lambda: cur.execute(mar, (c, a["id"], diet, "given", None))).pgerror
    assert _err(lambda: cur.execute(mar, (c, a["id"], med, "refused", None))).pgcode == "23514"   # reason required
    cur.execute(mar, (c, a["id"], med, "refused", "Patient vomiting"))
    cur.execute(mar, (c, a["id"], med, "given", None))
    assert "ipd_record_locked:ipd_orders:dose" in _err(lambda: cur.execute(
        "UPDATE ipd_orders SET dose = '2 g' WHERE id = %s", (med,))).pgerror
    cur.execute("UPDATE ipd_orders SET status='discontinued', ended_at=now(), end_reason='Course done' WHERE id=%s", (med,))
    assert "ipd_order_not_active" in _err(lambda: cur.execute(mar, (c, a["id"], med, "given", None))).pgerror


# ── charges, final bill, discharge ───────────────────────────────────────────

def _discharge_ready(cur, c, a, doctor):
    """Initiate discharge, sign a summary, accrue + post bed charges, generate the final bill."""
    cur.execute("SELECT ipd_initiate_discharge(%s,%s,'regular',NULL,'dr')", (c, a))
    s = _q(cur, "INSERT INTO ipd_discharge_summaries (clinic_id, admission_id, final_diagnosis) "
                "VALUES (%s,%s,'Typhoid') RETURNING id", c, a)
    cur.execute("SELECT ipd_sign_discharge_summary(%s,%s,%s,NULL,'Dr. Rao')", (c, s, doctor))
    cur.execute("SELECT ipd_accrue_bed_charges(%s,%s,NULL,'billing')", (c, a))
    cur.execute("SELECT array_agg(id) FROM ipd_charges WHERE admission_id=%s AND status='proposed'", (a,))
    cur.execute("SELECT ipd_post_charges(%s,%s,%s::uuid[],NULL,'billing')", (c, a, cur.fetchone()[0]))
    cur.execute("SELECT ipd_generate_final_bill(%s,%s,NULL,'billing')", (c, a))
    return cur.fetchone()[0]["invoice_id"]


def test_bed_charges_are_idempotent_and_priced_from_tariff(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])["id"]
    cur.execute("SELECT ipd_accrue_bed_charges(%s,%s,NULL,'b')", (c, a))
    assert cur.fetchone()[0]["added"] == 1
    cur.execute("SELECT ipd_accrue_bed_charges(%s,%s,NULL,'b')", (c, a))
    assert cur.fetchone()[0]["added"] == 0
    cur.execute("SELECT unit_price_paise, status FROM ipd_charges WHERE admission_id = %s", (a,))
    assert cur.fetchall() == [(150000, "proposed")]


def test_missing_tariff_is_reported_not_guessed(world):
    cur, c = world["cur"], world["clinic"]
    suite = _bed(cur, c, world["ward"], category="suite")
    a = _admit(cur, c, _patient(cur, c), world["doctor"], suite)["id"]
    cur.execute("SELECT ipd_accrue_bed_charges(%s,%s,NULL,'b')", (c, a))
    r = cur.fetchone()[0]
    assert r["added"] == 0 and r["missing_tariffs"] == ["suite"]


def test_posted_charge_is_immutable(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])["id"]
    ch = _q(cur, """INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date, unit_price_paise)
                    VALUES (%s,%s,'procedure','Dressing',CURRENT_DATE,50000) RETURNING id""", c, a)
    cur.execute("SELECT ipd_post_charges(%s,%s,ARRAY[%s]::uuid[],NULL,'b')", (c, a, ch))
    assert "ipd_record_locked" in _err(lambda: cur.execute(
        "UPDATE ipd_charges SET unit_price_paise = 1 WHERE id = %s", (ch,))).pgerror
    assert "ipd_charge_bill_via_function_only" in _err(lambda: cur.execute(
        "UPDATE ipd_charges SET invoice_id = gen_random_uuid() WHERE id = %s", (ch,))).pgerror


def test_final_bill_requires_discharge_pending(world):
    cur, c = world["cur"], world["clinic"]
    a = _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])["id"]
    assert "ipd_bill_needs_discharge_pending" in _err(lambda: cur.execute(
        "SELECT ipd_generate_final_bill(%s,%s,NULL,'b')", (c, a))).pgerror


def test_discharge_end_to_end(world):
    cur, c, d = world["cur"], world["clinic"], world["doctor"]
    a = _admit(cur, c, _patient(cur, c), d, world["bed"])["id"]
    complete = lambda w=None: cur.execute("SELECT ipd_complete_discharge(%s,%s,%s,NULL,'desk')", (c, a, w))
    assert "ipd_discharge_blocked" in _err(complete).pgerror
    inv = _discharge_ready(cur, c, a, d)
    cur.execute("SELECT count(*), sum(quantity * unit_price_paise) FROM opd_invoice_items WHERE invoice_id = %s", (inv,))
    assert cur.fetchone() == (1, 150000)
    cur.execute("SELECT ipd_generate_final_bill(%s,%s,NULL,'b')", (c, a))
    assert cur.fetchone()[0] == {"invoice_id": inv, "replayed": True}   # no second bill
    assert "ipd_bill_lines_locked" in _err(lambda: cur.execute(
        "DELETE FROM opd_invoice_items WHERE invoice_id = %s", (inv,))).pgerror
    assert "ipd_discharge_blocked" in _err(complete).pgerror   # a draft bill is not issued
    cur.execute("UPDATE opd_invoices SET status = 'issued' WHERE id = %s", (inv,))   # OPD issue path
    assert "ipd_settlement_waiver_required" in _err(complete).pgerror
    complete("Insurance claim pending TPA approval")
    cur.execute("SELECT status, current_bed_id, settlement_waiver_reason FROM ipd_admissions WHERE id = %s", (a,))
    assert cur.fetchone() == ("discharged", None, "Insurance claim pending TPA approval")
    assert _bed_row(cur, world["bed"])[:2] == ("cleaning", None)
    cur.execute("SELECT count(*) FROM ipd_bed_assignments WHERE admission_id = %s AND ended_at IS NULL", (a,))
    assert cur.fetchone()[0] == 0
    complete()   # replay is a no-op
    assert "ipd_record_locked" in _err(lambda: cur.execute(
        "UPDATE ipd_admissions SET reason = 'changed' WHERE id = %s", (a,))).pgerror


def test_active_order_blocks_discharge(world):
    cur, c, d = world["cur"], world["clinic"], world["doctor"]
    a = _admit(cur, c, _patient(cur, c), d, world["bed"])["id"]
    cur.execute("""INSERT INTO ipd_orders (clinic_id, admission_id, order_type, description, ordered_by_doctor_id,
                   ordered_by_name) VALUES (%s,%s,'nursing','Daily dressing',%s,'Dr. Rao')""", (c, a, d))
    cur.execute("SELECT ipd_initiate_discharge(%s,%s,'regular',NULL,'dr')", (c, a))
    cur.execute("SELECT ipd_discharge_checklist(%s,%s)", (c, a))
    items = {i["key"]: i for i in cur.fetchone()[0]["items"]}
    assert items["orders_closed"] == {**items["orders_closed"], "done": False, "count": 1}


def test_summary_signing_rules(world):
    cur, c, d = world["cur"], world["clinic"], world["doctor"]
    a = _admit(cur, c, _patient(cur, c), d, world["bed"])["id"]
    other_doc = _doctor(cur, c)
    s = _q(cur, "INSERT INTO ipd_discharge_summaries (clinic_id, admission_id) VALUES (%s,%s) RETURNING id", c, a)
    sign = lambda sid, doc: cur.execute("SELECT ipd_sign_discharge_summary(%s,%s,%s,NULL,'x')", (c, sid, doc))
    assert "ipd_not_attending_doctor" in _err(lambda: sign(s, other_doc)).pgerror
    assert "ipd_summary_incomplete" in _err(lambda: sign(s, d)).pgerror
    assert "ipd_sign_via_function_only" in _err(lambda: cur.execute(
        "UPDATE ipd_discharge_summaries SET status='signed', signed_by_doctor_id=%s, signed_at=now(), "
        "final_diagnosis='x' WHERE id=%s", (d, s))).pgerror
    cur.execute("UPDATE ipd_discharge_summaries SET final_diagnosis = 'Dengue' WHERE id = %s", (s,))
    sign(s, d)
    assert "ipd_record_locked" in _err(lambda: cur.execute(
        "UPDATE ipd_discharge_summaries SET final_diagnosis = 'x' WHERE id = %s", (s,))).pgerror
    s2 = _q(cur, """INSERT INTO ipd_discharge_summaries (clinic_id, admission_id, version, supersedes_id, final_diagnosis,
                    amend_reason) VALUES (%s,%s,2,%s,'Dengue with thrombocytopenia','Platelets added') RETURNING id""",
            c, a, s)
    sign(s2, d)
    cur.execute("SELECT status FROM ipd_discharge_summaries WHERE id IN (%s,%s) ORDER BY version", (s, s2))
    assert [r[0] for r in cur.fetchall()] == ["superseded", "signed"]


def test_unregistered_attending_cannot_sign(real_pg_conn):
    cur = real_pg_conn.cursor()
    c = _clinic(cur)
    d = _doctor(cur, c, reg=None)
    a = _admit(cur, c, _patient(cur, c), d, _bed(cur, c, _ward(cur, c)))["id"]
    s = _q(cur, "INSERT INTO ipd_discharge_summaries (clinic_id, admission_id, final_diagnosis) VALUES (%s,%s,'x') "
                "RETURNING id", c, a)
    assert "ipd_doctor_registration_missing" in _err(lambda: cur.execute(
        "SELECT ipd_sign_discharge_summary(%s,%s,%s,NULL,'x')", (c, s, d))).pgerror


def test_voided_bill_releases_charges_for_a_new_bill(world):
    cur, c, d = world["cur"], world["clinic"], world["doctor"]
    a = _admit(cur, c, _patient(cur, c), d, world["bed"])["id"]
    inv = _discharge_ready(cur, c, a, d)
    cur.execute("UPDATE opd_invoices SET status='void', voided_at=now(), void_reason='Wrong payer' WHERE id=%s", (inv,))
    cur.execute("SELECT ipd_generate_final_bill(%s,%s,NULL,'b')", (c, a))
    new = cur.fetchone()[0]["invoice_id"]
    assert new != inv
    cur.execute("SELECT DISTINCT invoice_id::text FROM ipd_charges WHERE admission_id = %s AND status = 'posted'", (a,))
    assert cur.fetchall() == [(new,)]


def test_purge_removes_everything_and_clinic_can_be_deleted(world):
    cur, c, d = world["cur"], world["clinic"], world["doctor"]
    a = _admit(cur, c, _patient(cur, c), d, world["bed"])["id"]
    _discharge_ready(cur, c, a, d)
    assert _err(lambda: cur.execute("DELETE FROM clinics WHERE id = %s", (c,))).pgcode == "P0001"
    cur.execute("BEGIN")
    cur.execute("SELECT ipd_purge_clinic(%s)", (c,))
    cur.execute("DELETE FROM clinics WHERE id = %s", (c,))
    cur.execute("COMMIT")
    cur.execute("SELECT count(*) FROM ipd_admissions WHERE clinic_id = %s", (c,))
    assert cur.fetchone()[0] == 0


def test_rollback_refuses_while_admitted_then_reapplies(world):
    cur, c = world["cur"], world["clinic"]
    _admit(cur, c, _patient(cur, c), world["doctor"], world["bed"])
    down = (MIG / "rollback" / "111_down.sql").read_text(encoding="utf-8")
    assert "patients are still admitted" in _err(lambda: cur.execute(down)).pgerror
    cur.execute("BEGIN")   # everything below is rolled back
    try:
        cur.execute("SELECT ipd_purge_clinic(clinic_id) FROM (SELECT DISTINCT clinic_id FROM ipd_admissions) x")
        cur.execute(down)
        cur.execute("SELECT to_regclass('public.ipd_admissions')")
        assert cur.fetchone()[0] is None
        cur.execute((MIG / "111_ipd_os_core.sql").read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")
