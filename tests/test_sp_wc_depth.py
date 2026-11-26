"""Women & Child completion (SP-WC-02, 04, 06–08; migration 156) on real Postgres.

Delivery → each live-born baby is a family member under the mother's account via the OPD registration path (MRN),
with its birth measurements stored with WHO z-scores; a stillbirth creates no child record; the pregnancy closes and a
second delivery for it is refused. Birth registration is due within 21 days and recorded with the registrar's number.
Growth: standing height under 2 y gets +0.7 cm (WHO), z-scores match WHO's published cut-offs. Government ids feed the
U-WIN / RCH CSVs (formula-safe). NICU rows need an IPD admission of this clinic.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services import sp_growth
from tests.sp_world import sp_world
from tests.test_ipd_api_e2e import _admit, _q, _setup_hospital, api  # noqa: F401  (fixture)

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.mark.parametrize("ind,sex,months,value,z", [
    ("wfa", "male", 12, 7.7, -2.0), ("wfa", "male", 12, 12.0, 2.0), ("wfa", "female", 36, 20.9, 3.0),
    ("lhfa", "female", 48, 94.1, -2.0), ("hcfa", "male", 6, 45.8, 2.0), ("bfa", "male", 18, 13.9, -2.0),
    ("bfa", "female", 120, 22.6, 2.0), ("bfa", "female", 186, 14.5, -3.0), ("wfa", "male", 12, 9.6479, 0.0),
])
def test_z_scores_match_who_published_cutoffs(ind, sex, months, value, z):
    """WHO publishes the SD lines rounded to 0.1 kg / cm, so a cut-off value lands within ±0.06 of its z."""
    assert abs(sp_growth.zscore(ind, sex, months, value) - z) <= 0.06


def test_growth_engine_rules():
    assert sp_growth.zscore("wfa", "male", 121, 30) is None                       # WHO weight-for-age stops at 10 y
    assert sp_growth.zscore("hcfa", "female", 61, 50) is None
    assert sp_growth.zscore("wfa", "male", 12, 20) > 3                            # restricted method beyond +3 SD
    dob = date(2025, 1, 1)
    lying = sp_growth.measurement_z("female", dob, date(2025, 12, 1), length_cm=74)
    standing = sp_growth.measurement_z("female", dob, date(2025, 12, 1), length_cm=73.3, position="standing")
    assert lying["lhfa"] == standing["lhfa"]                                       # +0.7 cm under 24 months
    assert sp_growth.measurement_z("undisclosed", dob, date(2025, 6, 1), weight_kg=7) == {}
    assert sp_growth.band(-2.5) == "beyond_2" and sp_growth.band(1) == "within_2"


def _pregnancy(api, w):  # noqa: F811
    api.as_(w.u.doctor)
    p = api.call("POST", "/admin/sp/anc/pregnancies", w.clinic, {"patient_id": w.patient, "edd": str(date.today() + timedelta(days=5)),
                                                                 "edd_source": "dating_scan", "gravida": 1}).json()
    return p["id"]


def test_delivery_creates_the_newborn_and_birth_registration(api):  # noqa: F811
    w = sp_world(api, plan="womenchild", packs=("anc", "imm"))
    pid = _pregnancy(api, w)
    born = datetime.now(IST).replace(microsecond=0) - timedelta(days=25)
    body = {"delivered_at": born.isoformat(), "mode": "caesarean", "gestation_weeks": 38, "babies": [
        {"outcome": "live_birth", "sex": "female", "birth_weight_g": 2900, "length_cm": 49, "head_cm": 34, "apgar_1": 8, "apgar_5": 9},
        {"outcome": "stillbirth"}]}
    r = api.call("POST", f"/admin/sp/anc/pregnancies/{pid}/delivery", w.clinic, body)
    assert r.status_code == 201, r.text
    live, still = r.json()["newborns"]
    assert live["family_member_id"] and still["family_member_id"] is None
    fm = _q(w.conn, "SELECT row_to_json(f) FROM family_members f WHERE id = %s", live["family_member_id"])
    assert fm["relationship"] == "child" and fm["date_of_birth"] == str(born.date()) and fm["mrn"]
    z = _q(w.conn, "SELECT z FROM child_growth_measurements WHERE family_member_id = %s", live["family_member_id"])
    assert set(z) >= {"wfa", "lhfa", "hcfa"} and abs(z["wfa"] - sp_growth.zscore("wfa", "female", 0, 2.9)) < 0.01
    assert _q(w.conn, "SELECT status FROM obs_pregnancies WHERE id = %s", pid) == "delivered"
    assert api.call("POST", f"/admin/sp/anc/pregnancies/{pid}/delivery", w.clinic, body).status_code == 409
    births = api.call("GET", "/admin/sp/wc/birth-registrations", w.clinic).json()["births"]
    assert [(b["state"], b["register_by"]) for b in births] == [("overdue", str(born.date() + timedelta(days=21)))]
    ok = api.call("POST", f"/admin/sp/wc/newborns/{live['id']}/registration", w.clinic,
                  {"birth_reg_no": "B-2026-HYD-00421", "registered_on": str(date.today())})
    assert ok.status_code == 200, ok.text
    assert api.call("GET", "/admin/sp/wc/birth-registrations", w.clinic).json()["births"][0]["state"] == "registered"
    assert api.call("POST", f"/admin/sp/wc/newborns/{still['id']}/registration", w.clinic,
                    {"birth_reg_no": "X-1", "registered_on": str(date.today())}).status_code == 404


def test_growth_gov_ids_and_exports(api):  # noqa: F811
    w = sp_world(api, plan="womenchild", packs=("anc", "imm"))
    child = w.patient
    _q(w.conn, "UPDATE patients SET date_of_birth = %s, gender = 'male' WHERE id = %s", date.today() - timedelta(days=400), child)
    api.as_(w.u.nurse)
    bad = api.call("POST", "/admin/sp/wc/growth", w.clinic, {"patient_id": child, "measured_on": str(date.today()), "length_cm": 75})
    assert bad.status_code == 422                                                      # lying or standing?
    g = api.call("POST", "/admin/sp/wc/growth", w.clinic, {"patient_id": child, "measured_on": str(date.today()),
                                                           "weight_kg": 9.6, "length_cm": 76.5, "position": "recumbent", "head_cm": 46})
    assert g.status_code == 201, g.text
    assert set(g.json()["z"]) >= {"wfa", "lhfa", "bfa", "hcfa", "bmi"}
    assert api.call("POST", "/admin/sp/wc/growth", w.clinic, {"patient_id": child, "measured_on": str(date.today() + timedelta(days=1)),
                                                              "weight_kg": 9}).status_code == 422
    chart = api.call("GET", "/admin/sp/wc/growth", w.clinic, patient_id=child).json()
    assert chart["sex_known"] and chart["points"][0]["bands"]["wfa"] == "within_2"
    assert api.call("PUT", "/admin/sp/wc/gov-ids", w.clinic, {"patient_id": child, "rch_id": "12345"}).status_code == 422
    ids = api.call("PUT", "/admin/sp/wc/gov-ids", w.clinic, {"patient_id": child, "rch_id": "123456789012", "uwin_id": "UW-778899"})
    assert ids.status_code == 200, ids.text
    assert api.call("GET", "/admin/sp/wc/gov-ids", w.clinic, patient_id=child).json()["uwin_id"] == "UW-778899"
    api.call("POST", "/admin/sp/immunisation/doses", w.clinic, {"patient_id": child, "schedule_code": "iap_2023", "vaccine_code": "bcg",
                                                                "dose_no": 1, "given_on": str(date.today() - timedelta(days=300)),
                                                                "batch_no": "=cmd|' /C calc'!A0", "expiry": str(date.today()),
                                                                "site": "left_arm"})
    api.as_(w.u.admin)
    csv = api.call("GET", "/admin/sp/wc/uwin.csv", w.clinic, **{"from": str(date.today() - timedelta(days=360)), "to": str(date.today())})
    assert csv.status_code == 200 and "UW-778899" in csv.text and "123456789012" in csv.text
    assert "'=CMD" in csv.text.upper()                                                   # formula-safe
    _pregnancy(api, w)
    api.as_(w.u.admin)
    rch = api.call("GET", "/admin/sp/wc/rch.csv", w.clinic)
    assert rch.status_code == 200 and rch.text.startswith("RCH id,MCP card,Mother name")
    api.as_(w.u.nurse)
    assert api.call("GET", "/admin/sp/wc/rch.csv", w.clinic).status_code == 403          # exports: SP_ADMIN


def test_nicu_flowsheet_needs_an_admission_of_this_clinic(api):  # noqa: F811
    w = sp_world(api, plan="womenchild", packs=("anc", "imm"))
    api.as_(w.u.admin)
    import uuid
    assert api.call("GET", f"/admin/sp/wc/nicu/{uuid.uuid4()}", w.clinic).status_code == 403      # IPD OS off
    doctor, u, beds = _setup_hospital(api, w.clinic)
    adm = _admit(api, w.clinic, u, doctor, beds["ICU1"])
    assert adm.status_code in (200, 201), adm.text
    aid = adm.json()["id"]
    api.as_(w.u.nurse)
    at = datetime.now(IST).replace(microsecond=0)
    for i, (wt, feed) in enumerate(((1850, 20), (1870, 25))):
        r = api.call("POST", f"/admin/sp/wc/nicu/{aid}/entries", w.clinic, {
            "recorded_at": (at - timedelta(hours=6 - 3 * i)).isoformat(), "weight_g": wt, "temp_c": 36.8, "spo2": 96,
            "feed_type": "expressed_breast_milk", "feed_ml": feed, "phototherapy": i == 0, "bilirubin_mg_dl": 11.2,
            "resp_support": "oxygen"})
        assert r.status_code == 201, r.text
    sheet = api.call("GET", f"/admin/sp/wc/nicu/{aid}", w.clinic).json()
    assert len(sheet["entries"]) == 2 and sheet["weight_change_g"] == 20 and sheet["feeds_24h_ml"] == 45
    assert api.call("POST", f"/admin/sp/wc/nicu/{uuid.uuid4()}/entries", w.clinic,
                    {"recorded_at": at.isoformat()}).status_code == 404
