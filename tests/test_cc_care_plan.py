"""Care plan = the verbatim projection of the latest signed prescription (ADR-CC-05; TEST-PLAN P3 goldens).

Only an OD / BD / TDS / QID / HS oral line becomes a medicine prompt at the clinic's times; SOS, STAT, custom
("1-0-1", "weekly"), tapers, injections, inhalers, topicals, insulin and every H1 / X / narcotic drug are shown
exactly as signed with no prompt; methotrexate weekly never becomes daily. A newer signed prescription supersedes the
plan atomically; an unsigned draft is ignored.
"""

import asyncio
from datetime import date

import pytest

from app.services import cc
from tests.cc_world import care, signed_rx, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

TIMES = cc.DEFAULT_SETTINGS["med_times"]
START = date(2026, 11, 25)


def _line(**kw):
    return {"id": "li-1", "drug_name": "Metformin", "strength": "500 mg", "formulation": "tablet", "route": "oral",
            "dosage": "1", "frequency": "BD", "timing": "after_food", "duration_days": 30, "instructions": None, **kw}


@pytest.mark.parametrize("freq,times", [("OD", ["08:00"]), ("BD", ["08:00", "20:00"]),
                                        ("TDS", ["08:00", "14:00", "20:00"]),
                                        ("QID", ["08:00", "12:00", "16:00", "20:00"]), ("HS", ["22:00"]),
                                        ("bd", ["08:00", "20:00"])])
def test_standard_frequencies_become_adherence_prompts(freq, times):
    it = cc.plan_item_for_line(_line(frequency=freq), TIMES, START, set())
    assert it["prompt_mode"] == "adherence" and it["schedule"] == {"times": times}
    assert it["display_text"] == "Metformin · 500 mg · 1 · " + freq + " · after food · for 30 days"
    assert (it["starts_on"], it["ends_on"]) == ("2026-11-25", "2026-12-24")


@pytest.mark.parametrize("kw", [
    {"frequency": "SOS"}, {"frequency": "STAT"}, {"frequency": "1-0-1"}, {"frequency": "weekly"},
    {"frequency": "OD", "instructions": "taper by half every 5 days"}, {"frequency": "OD", "instructions": "SOS for pain"},
    {"frequency": "OD", "dosage": "alternate day"}, {"formulation": "injection", "route": "sc"},
    {"formulation": "inhaler", "route": "inhalation"}, {"formulation": "ointment", "route": "topical"},
    {"drug_name": "Insulin Glargine", "formulation": "injection"}, {"drug_name": "Human Insulin 30/70"},
    {"drug_name": "Alprazolam", "frequency": "HS"}, {"drug_name": "Tramadol", "frequency": "BD"},
    {"drug_name": "Cefixime", "frequency": "BD"}, {"drug_name": "Methotrexate", "frequency": "weekly"},
])
def test_display_only_lines_are_shown_verbatim_without_a_prompt(kw):
    it = cc.plan_item_for_line(_line(**kw), TIMES, START, set())
    assert it["prompt_mode"] == "display_only" and it["schedule"] == {}
    assert it["display_text"].startswith(kw.get("drug_name", "Metformin"))


def test_clinic_controlled_schedule_wins_and_no_duration_means_open_ended():
    it = cc.plan_item_for_line(_line(id="li-9", duration_days=None), TIMES, START, {"li-9"})
    assert it["prompt_mode"] == "display_only" and it["ends_on"] is None


def test_clinic_medicine_times_are_used():
    it = cc.plan_item_for_line(_line(frequency="OD"), {**TIMES, "OD": ["09:30"]}, START, set())
    assert it["schedule"] == {"times": ["09:30"]}


def _plan(care, e):
    return asyncio.run(cc.current_plan(care.clinic, e["id"]))


def test_signed_rx_projects_and_new_version_supersedes(care):  # noqa: F811
    from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
    c = care.conn
    from tests.cc_world import patient
    pid = patient(c, care.clinic)
    signed_rx(c, care.clinic, pid, care.doctor, [
        {"drug_name": "Metformin", "strength": "500 mg", "formulation": "tablet", "dosage": "1", "frequency": "BD",
         "timing": "after_food", "duration_days": 30},
        {"drug_name": "Insulin Glargine", "formulation": "injection", "route": "sc", "dosage": "10 units",
         "frequency": "HS"},
        {"drug_name": "Paracetamol", "formulation": "tablet", "dosage": "1", "frequency": "SOS"}], follow_up="2026-12-20")
    signed_rx(c, care.clinic, pid, care.doctor, [{"drug_name": "Glimepiride", "formulation": "tablet", "dosage": "1",
                                                   "frequency": "OD"}], sign=False)          # an unsigned draft
    e, _ = care.enroll(patient=pid)
    plan = _plan(care, e)
    meds = [i for i in plan["items"] if i["kind"] == "medication"]
    assert [(i["display_text"].split(" · ")[0], i["prompt_mode"]) for i in meds] == [
        ("Metformin", "adherence"), ("Insulin Glargine", "display_only"), ("Paracetamol", "display_only")]
    assert not [i for i in meds if "Glimepiride" in i["display_text"]]                 # draft ignored
    assert any(i["kind"] == "visit" and i["schedule"] == {"due_on": "2026-12-20"} for i in plan["items"])
    assert any(i["kind"] == "measure" and i["measure_code"] == "fbg" for i in plan["items"])     # pack reading item
    assert any(i["kind"] == "lab" and i["measure_code"] == "hba1c" for i in plan["items"])
    v1 = plan["version"]
    signed_rx(c, care.clinic, pid, care.doctor, [{"drug_name": "Metformin", "strength": "1000 mg",
                                                   "formulation": "tablet", "dosage": "1", "frequency": "OD"}])
    invalidate_tenant_cache()
    clinic = asyncio.run(get_clinic_by_id(care.clinic))
    assert asyncio.run(cc.rebuild_on_new_rx(clinic)) == 1
    assert asyncio.run(cc.rebuild_on_new_rx(clinic)) == 0                              # watermark: no rebuild twice
    plan2 = _plan(care, e)
    assert plan2["version"] == v1 + 1
    assert [i["display_text"] for i in plan2["items"] if i["kind"] == "medication"] == ["Metformin · 1000 mg · 1 · OD"]
    assert _q(c, "SELECT count(*) FROM cc_care_plans WHERE enrollment_id = %s AND status = 'active'", e["id"]) == 1


def test_preview_matches_and_staff_rebuild(care):  # noqa: F811
    from tests.cc_world import patient
    pid = patient(care.conn, care.clinic)
    signed_rx(care.conn, care.clinic, pid, care.doctor, [{"drug_name": "Amlodipine", "formulation": "tablet",
                                                          "dosage": "1", "frequency": "OD"}])
    e, _ = care.enroll(patient=pid)
    care.api.as_(care.u.nurse)
    pv = care.api.call("GET", f"/admin/cc/enrollments/{e['id']}/plan/preview", care.clinic)
    assert pv.status_code == 200 and pv.json()["items"][0]["display_text"] == "Amlodipine · 1 · OD"
    assert care.api.call("POST", f"/admin/cc/enrollments/{e['id']}/plan/rebuild", care.clinic).status_code == 403
    care.api.as_(care.u.coord)
    r = care.api.call("POST", f"/admin/cc/enrollments/{e['id']}/plan/rebuild", care.clinic)
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["superseded"]
