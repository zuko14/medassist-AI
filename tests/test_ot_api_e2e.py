"""G8 operation theatre end to end: real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

Theatres, booking an admitted patient, the 409 on a double booking, the day's theatre
list, WHO checklist phase gating, the anaesthesia record locking on signature, completion
refused until the checklist is done and then exactly one posted charge, permissions
(old logins keep working through implied grants), tenant and branch isolation.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_ipd_api_e2e import _admit, _clinic, _code, _q, _setup_hospital, api  # noqa: F401  (fixture)

DAY = datetime(2027, 4, 5, tzinfo=timezone.utc)
ITEMS = {
    "sign_in": ["patient_identity_confirmed", "site_marked", "anaesthesia_machine_checked",
                "pulse_oximeter_functioning", "allergy_checked", "airway_risk_assessed", "blood_loss_risk_assessed"],
    "time_out": ["team_members_introduced", "patient_name_procedure_confirmed", "anticipated_critical_events",
                 "antibiotic_prophylaxis_given", "essential_imaging_displayed"],
    "sign_out": ["procedure_recorded", "instrument_swab_needle_count_correct", "specimen_labelled",
                 "equipment_problems_addressed", "key_recovery_concerns_reviewed"],
}


@pytest.fixture
def ot(api):  # noqa: F811
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    adm = _admit(api, clinic, u, doctor, beds["101"]).json()
    adm2 = _admit(api, clinic, u, doctor, beds["102"]).json()
    api.as_(u["admin"])
    theatre = api.call("POST", "/admin/ot/theatres", clinic, {"name": "OT 1"})
    assert theatre.status_code == 201, theatre.text
    service = str(_q(api.conn, """INSERT INTO billable_services (clinic_id, service_code, name, category,
                                  standard_price_paise) VALUES (%s, 'APPX', 'Appendicectomy', 'procedure', 3500000)
                                  RETURNING id""", clinic))
    return {"clinic": clinic, "doctor": doctor, "u": u, "adm": adm["id"], "adm2": adm2["id"],
            "theatre": theatre.json()["id"], "service": service}


def _book(api, o, user="desk", start_h=4.0, hours=1.5, adm=None, key=None, **extra):  # noqa: F811
    start = DAY + timedelta(hours=start_h)
    return api.as_(o["u"][user]).call("POST", "/admin/ot/surgeries", o["clinic"], {
        "admission_id": adm or o["adm"], "theatre_id": o["theatre"], "procedure_name": "Appendicectomy",
        "service_id": o["service"], "scheduled_start": start.isoformat(),
        "scheduled_end": (start + timedelta(hours=hours)).isoformat(), "primary_surgeon_id": o["doctor"],
        "anaesthetist_id": o["doctor"], "pre_op_diagnosis": "Acute appendicitis",
        "idempotency_key": key or uuid.uuid4().hex, **extra})


def _check(api, o, sid, phase, user="nurse", items=None):  # noqa: F811
    return api.as_(o["u"][user]).call("POST", f"/admin/ot/surgeries/{sid}/who-checklist/{phase}", o["clinic"],
                                      {"items": items or {k: True for k in ITEMS[phase]}})


def test_module_gate_and_theatres(api):  # noqa: F811
    bare = _clinic(api.conn)
    admin = api.U("admin", "clinic_admin", bare, str(uuid.uuid4()))
    assert api.as_(admin).call("GET", "/admin/ot/schedule", bare).status_code == 403   # no IPD module
    clinic = _clinic(api.conn)
    doctor, u, _ = _setup_hospital(api, clinic)
    assert api.as_(u["nurse"]).call("POST", "/admin/ot/theatres", clinic, {"name": "OT A"}).status_code == 403
    api.as_(u["admin"])
    t = api.call("POST", "/admin/ot/theatres", clinic, {"name": "OT A", "theatre_type": "minor"}).json()
    dup = api.call("POST", "/admin/ot/theatres", clinic, {"name": "OT A"})
    assert dup.status_code == 409 and _code(dup) == "ot_theatres_name_key"
    off = api.call("PATCH", f"/admin/ot/theatres/{t['id']}", clinic, {"is_active": False})
    assert off.status_code == 200 and off.json()["is_active"] is False
    listed = api.as_(u["nurse"]).call("GET", "/admin/ot/theatres", clinic).json()["theatres"]
    assert [x["name"] for x in listed] == ["OT A"]                                      # nurses can read
    assert api.as_(u["billing"]).call("GET", "/admin/ot/theatres", clinic).status_code == 403


def test_booking_conflict_replay_and_day_list(api, ot):  # noqa: F811
    key = uuid.uuid4().hex
    r = _book(api, ot, key=key)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["surgery_number"].startswith("OT-") and s["status"] == "scheduled"
    assert _book(api, ot, key=key).json()["replayed"] is True
    clash = _book(api, ot, adm=ot["adm2"], start_h=5)                                   # overlaps 09:30-11:00 IST
    assert clash.status_code == 409 and _code(clash) == "ot_theatre_busy"
    assert _book(api, ot, adm=ot["adm2"], start_h=5.5).status_code == 201               # right after: fine
    assert _book(api, ot, user="billing", start_h=9).status_code == 403
    assert _book(api, ot, start_h=6, hours=30).status_code == 422                        # > 24 h
    bad_doc = _book(api, ot, start_h=12, primary_surgeon_id=str(uuid.uuid4()))
    assert bad_doc.status_code == 404 and "Surgeon" in bad_doc.json()["detail"]["message"]

    day = api.as_(ot["u"]["nurse"]).call("GET", "/admin/ot/schedule", ot["clinic"], date="2027-04-05")
    assert day.status_code == 200, day.text
    rows = day.json()["surgeries"]
    assert [x["id"] for x in rows][0] == s["id"] and len(rows) == 2
    card = rows[0]
    assert card["patient_name"] == "Lakshmi Devi" and card["admission_number"].startswith("IPD-")
    assert card["surgeon_name"] and card["theatre_name"] == "OT 1" and card["checklist"] == {
        "sign_in": False, "time_out": False, "sign_out": False}
    assert api.call("GET", "/admin/ot/schedule", ot["clinic"], date="2027-04-06").json()["surgeries"] == []
    assert "Lakshmi" in api.as_(ot["u"]["desk"]).call("GET", "/admin/ot/options", ot["clinic"]).text


def test_checklist_gating_completion_and_one_charge(api, ot):  # noqa: F811
    sid = _book(api, ot).json()["id"]
    assert _check(api, ot, sid, "sign_in", user="billing").status_code == 403
    early = _check(api, ot, sid, "time_out")
    assert early.status_code == 409 and _code(early) == "ot_checklist_order"
    part = _check(api, ot, sid, "sign_in", items={"site_marked": True})
    assert part.status_code == 200 and part.json()["sign_in_completed"] is False
    assert _check(api, ot, sid, "sign_in", items={"made_up": True}).status_code == 422
    assert _check(api, ot, sid, "sign_in").json()["sign_in_completed"] is True
    started = api.as_(ot["u"]["nurse"]).call("POST", f"/admin/ot/surgeries/{sid}/status", ot["clinic"],
                                             {"action": "start"})
    assert started.status_code == 200 and started.json()["status"] == "in_theatre"
    assert _check(api, ot, sid, "time_out", user="doctor").status_code == 200           # clinicians tick too

    note = {"procedure_notes": "Inflamed appendix removed, stump ligated.", "post_op_diagnosis": "Acute appendicitis",
            "post_op_instructions": "NBM 6 h"}
    done = lambda user, **b: api.as_(ot["u"][user]).call(  # noqa: E731
        "POST", f"/admin/ot/surgeries/{sid}/complete", ot["clinic"], {**note, **b})
    early = done("doctor")
    assert early.status_code == 409 and _code(early) == "ot_checklist_incomplete"
    assert _check(api, ot, sid, "sign_out").status_code == 200
    assert done("nurse").status_code == 403                                              # OT_CLINICAL only
    assert done("admin").status_code == 403                                              # no doctor link
    ok = done("doctor")
    assert ok.status_code == 200, ok.text
    ch = ok.json()["charge"]
    assert (ch["status"], ch["category"], ch["unit_price_paise"]) == ("posted", "procedure", 3500000)
    assert ch["description"] == "Surgical Procedure: Appendicectomy"
    again = done("doctor", amount_paise=1)
    assert again.status_code == 200 and again.json()["replayed"] is True and again.json()["charge"]["id"] == ch["id"]
    assert _q(api.conn, "SELECT count(*) FROM ipd_charges WHERE admission_id = %s AND source_ref = %s",
              ot["adm"], f"ot_surgery:{sid}") == 1
    detail = api.as_(ot["u"]["nurse"]).call("GET", f"/admin/ot/surgeries/{sid}", ot["clinic"]).json()
    assert detail["status"] == "completed" and detail["charge"]["id"] == ch["id"]
    assert detail["who_checklist"]["sign_out_completed"] is True
    assert detail["post_op_instructions"] == "NBM 6 h"


def test_anaesthesia_record_and_status_reasons(api, ot):  # noqa: F811
    sid = _book(api, ot).json()["id"]
    path = f"/admin/ot/surgeries/{sid}/anaesthesia"
    assert api.as_(ot["u"]["nurse"]).call("POST", path, ot["clinic"], {"mallampati": "II"}).status_code == 403
    api.as_(ot["u"]["doctor"])
    saved = api.call("POST", path, ot["clinic"], {"mallampati": "II", "vital_signs_log": [
        {"time": "10:05", "bp_sys": 120, "bp_dia": 80, "pulse": 78, "spo2": 99}]})
    assert saved.status_code == 200 and saved.json()["vital_signs_log"][0]["pulse"] == 78
    assert api.call("POST", path, ot["clinic"], {"vital_signs_log": [{"time": "25:00"}]}).status_code == 422
    incomplete = api.call("POST", path, ot["clinic"], {"sign": True})
    assert incomplete.status_code == 422 and _code(incomplete) == "ot_anaesthesia_incomplete"
    signed = api.call("POST", path, ot["clinic"], {"asa_status": "II", "technique": "spinal", "sign": True}).json()
    assert signed["is_signed"] and signed["doctor_snapshot"]["registration_number"] == "TSMC/2012/777"
    locked = api.call("POST", path, ot["clinic"], {"blood_loss_ml": 50})
    assert locked.status_code == 409 and _code(locked) == "ot_anaesthesia_signed"

    status = f"/admin/ot/surgeries/{sid}/status"
    assert api.as_(ot["u"]["desk"]).call("POST", status, ot["clinic"], {"action": "postpone"}).status_code == 422
    assert api.as_(ot["u"]["nurse"]).call("POST", status, ot["clinic"],
                                          {"action": "cancel", "reason": "No consent"}).status_code == 403
    post = api.as_(ot["u"]["desk"]).call("POST", status, ot["clinic"], {"action": "postpone", "reason": "Fever"})
    assert post.status_code == 200 and post.json()["status"] == "postponed"
    assert _book(api, ot, adm=ot["adm2"]).status_code == 201                            # the slot is free again


def test_tenant_and_branch_isolation(api, ot):  # noqa: F811
    sid = _book(api, ot).json()["id"]
    other = _clinic(api.conn)
    stranger = api.U("other", "clinic_admin", other, str(uuid.uuid4()))
    assert api.as_(stranger).call("GET", f"/admin/ot/surgeries/{sid}", ot["clinic"]).status_code == 403
    _setup_hospital(api, other)
    assert api.as_(stranger).call("GET", f"/admin/ot/surgeries/{sid}", other).status_code == 404

    north = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'North') RETURNING id", ot["clinic"]))
    south = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'South') RETURNING id", ot["clinic"]))
    api.as_(ot["u"]["admin"])
    t_south = api.call("POST", "/admin/ot/theatres", ot["clinic"], {"name": "South OT", "branch_id": south}).json()
    s_south = _book(api, ot, user="admin", adm=ot["adm2"], start_h=10, theatre_id=t_south["id"]).json()
    assert s_south["branch_id"] == south
    pinned = api.U("northdesk", "staff", ot["clinic"], str(uuid.uuid4()), ["IPD_ADMISSIONS"],
                   branch_id=north, staff_role="FRONT_DESK")
    api.as_(pinned)
    assert api.call("GET", f"/admin/ot/surgeries/{s_south['id']}", ot["clinic"]).status_code == 403
    seen = [x["id"] for x in api.call("GET", "/admin/ot/schedule", ot["clinic"], date="2027-04-05").json()["surgeries"]]
    assert sid in seen and s_south["id"] not in seen                                    # branch-less + own branch only
    assert api.call("GET", "/admin/ot/schedule", ot["clinic"], date="2027-04-05", branch_id=south).status_code == 403
    assert _book(api, ot, user="admin", start_h=14, theatre_id=t_south["id"]).status_code == 201
    api.as_(pinned)
    blocked = api.call("POST", "/admin/ot/surgeries", ot["clinic"], {
        "admission_id": ot["adm"], "theatre_id": t_south["id"], "procedure_name": "Hernia repair",
        "scheduled_start": (DAY + timedelta(hours=20)).isoformat(),
        "scheduled_end": (DAY + timedelta(hours=21)).isoformat(), "primary_surgeon_id": ot["doctor"],
        "pre_op_diagnosis": "Inguinal hernia", "idempotency_key": uuid.uuid4().hex})
    assert blocked.status_code == 403


def test_old_logins_keep_working_through_implied_grants(api, ot):  # noqa: F811
    from app.services.permissions import holds_ot_permission
    u = ot["u"]
    assert holds_ot_permission(u["desk"], "OT_SCHEDULE") and not holds_ot_permission(u["desk"], "OT_CLINICAL")
    assert holds_ot_permission(u["nurse"], "OT_CHECKLIST") and not holds_ot_permission(u["nurse"], "OT_SCHEDULE")
    assert all(holds_ot_permission(u["doctor"], p) for p in ("OT_SCHEDULE", "OT_CHECKLIST", "OT_CLINICAL"))
    assert not holds_ot_permission(u["billing"], "OT_ANY")
    legacy_doc = api.U("drold", "staff", ot["clinic"], str(uuid.uuid4()), ["OPD_CLINICAL"], staff_role="DOCTOR")
    assert holds_ot_permission(legacy_doc, "OT_CLINICAL")                               # role implies it


def test_discharge_is_refused_while_a_surgery_is_open(api, ot):  # noqa: F811
    sid = _book(api, ot).json()["id"]
    path = f"/admin/ipd/admissions/{ot['adm']}/discharge/initiate"
    refused = api.as_(ot["u"]["doctor"]).call("POST", path, ot["clinic"], {"discharge_type": "regular"})
    assert refused.status_code == 409 and _code(refused) == "ipd_surgery_open"
    assert "Operation Theatre" in refused.json()["detail"]["message"]
    assert api.as_(ot["u"]["desk"]).call("POST", f"/admin/ot/surgeries/{sid}/status", ot["clinic"],
                                         {"action": "cancel", "reason": "Going home"}).status_code == 200
    ok = api.as_(ot["u"]["doctor"]).call("POST", path, ot["clinic"], {"discharge_type": "regular"})
    assert ok.status_code == 200 and ok.json()["status"] == "discharge_pending"
