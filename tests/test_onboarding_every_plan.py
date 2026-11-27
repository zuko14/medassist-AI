"""Client onboarding SOP, pin to pin, for every plan (docs/CLIENT_ONBOARDING_SOP.md Part D/E): create the tenant the
way the owner panel does (POST /platform/clinics, or the pharmacy wizard POST /platform/pharmacies), apply the plan's
Suggested setup, then open every switched-on module's setup page as the clinic admin."""

import uuid

import pytest

from app.services.tenant import PLAN_MODULE_TEMPLATES
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

SETUP_PAGE = {"opd_enabled": "/admin/opd/setup", "ipd_enabled": "/admin/ipd/setup", "dx_enabled": "/admin/dx/setup",
              "pharmacy_enabled": "/admin/rx/setup", "care_enabled": "/admin/cc/setup", "specialty_os": "/admin/sp/status"}


@pytest.fixture(autouse=True)
def _no_meta(monkeypatch):
    import httpx

    async def refuse(*a, **k):
        raise httpx.ConnectError("offline test")
    monkeypatch.setattr(httpx.AsyncClient, "post", refuse)       # Meta /register is best-effort; never call out


def _create(api, plan):  # noqa: F811
    num = "+9198" + uuid.uuid4().hex[:8].translate(str.maketrans("abcdef", "123456"))
    base = {"name": f"Onboard {plan} {uuid.uuid4().hex[:4]}", "whatsapp_number": num,
            "meta_phone_number_id": "pn" + uuid.uuid4().hex[:10], "meta_access_token": "EAAG" + "x" * 40}
    if plan == "pharmacy":
        r = api.client.post("/platform/pharmacies", json={**base, "pharmacist_name": "R Rao",
                                                           "pharmacist_registration": "TS-PH-1234",
                                                           "shops": [{"name": "Main", "drug_licence_no": "DL-1"}]})
    else:
        r = api.client.post("/platform/clinics", json={**base, "plan": plan})
    assert r.status_code in (200, 201), r.text
    return r.json()["clinic"]["id"]


@pytest.mark.parametrize("plan", sorted(PLAN_MODULE_TEMPLATES))
def test_onboarding_sop_create_suggested_setup_and_module_pages(api, plan):  # noqa: F811
    cid = _create(api, plan)
    assert _q(api.conn, "SELECT plan FROM clinics WHERE id = %s", cid) == plan
    assert _q(api.conn, "SELECT count(*) FROM clinic_admins WHERE clinic_id = %s", cid) >= 1   # admin login made

    view = api.client.get(f"/platform/clinics/{cid}/template").json()
    rec = [m["key"] for m in view["modules"] if m["tier"] == "recommended"]
    assert set(rec) == set(PLAN_MODULE_TEMPLATES[plan]["recommended"])
    r = api.client.post(f"/platform/clinics/{cid}/template/apply", json={"modules": rec})
    assert r.status_code == 200 and r.json()["failed"] == 0, r.text

    feats = _q(api.conn, "SELECT features FROM clinics WHERE id = %s", cid)
    assert all(feats.get(k) is True for k in rec), (rec, feats)
    if "ipd_enabled" not in rec and plan == "polyclinic":
        assert not feats.get("ipd_enabled")                       # polyclinic: no wards unless the owner adds them

    admin = api.U("admin", "clinic_admin", cid, str(uuid.uuid4()), [])
    for key, path in SETUP_PAGE.items():
        if key in rec:
            res = api.as_(admin).call("GET", path, cid)
            assert res.status_code == 200, (plan, path, res.text)
