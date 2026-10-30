"""G12 masters end to end: real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

Tariff master CRUD and permissions, doctors' orders priced from it, legacy JSON
catalogue still read (fail-open) and written through by the old settings API,
branch scoping, and ICD-10 autocomplete.
"""

import uuid

import pytest

from app.routers.opd import Diagnosis
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)
from tests.test_orders_api_e2e import _order, _visit, world  # noqa: F401  (fixture)

XRAY = {"service_code": "XRAY_CHEST", "name": "X-ray chest PA", "category": "radiology", "standard_price_paise": 45000}


def _svc(code):
    return {"order_type": "investigation", "catalog_type": "service_catalog", "catalog_code": code}


def _services(api, w, user="admin", **params):  # noqa: F811
    r = api.as_(w["u"][user]).call("GET", "/admin/masters/services", w["clinic"], **params)
    assert r.status_code == 200, r.text
    return {s["code"]: s for s in r.json()["services"]}


def test_tariff_master_crud_prices_orders(api, world):  # noqa: F811
    w = world
    api.as_(w["u"]["admin"])
    created = api.call("POST", "/admin/masters/services", w["clinic"], XRAY)
    assert created.status_code == 201, created.text
    svc = created.json()
    assert (svc["hsn_sac"], svc["tax_rate_bps"], svc["is_active"]) == ("999312", 0, True)
    assert api.call("POST", "/admin/masters/services", w["clinic"], XRAY).status_code == 409          # duplicate code
    assert api.call("POST", "/admin/masters/services", w["clinic"],
                    {**XRAY, "service_code": "BAD", "tax_rate_bps": 1000}).status_code == 422          # not a GST slab
    assert api.call("PATCH", f"/admin/masters/services/{svc['id']}", w["clinic"],
                    {"service_code": "XRAY_NEW"}).status_code == 422                                   # code is fixed

    # the desk reads the master but cannot change it
    assert _services(api, w, "desk")["XRAY_CHEST"]["source"] == "master"
    assert api.call("POST", "/admin/masters/services", w["clinic"], {**XRAY, "service_code": "X2"}).status_code == 403
    catalog = api.as_(w["u"]["doctor"]).call("GET", "/admin/opd/orders/catalog", w["clinic"]).json()
    assert any(s["code"] == "XRAY_CHEST" and s["item_type"] == "diagnostic" for s in catalog["services"])

    _, enc = _visit(api, w)
    first = _order(api, w, enc, [_svc("XRAY_CHEST")]).json()["orders"][0]
    assert first["unit_price_paise"] == 45000

    api.as_(w["u"]["admin"])
    stale = api.call("PATCH", f"/admin/masters/services/{svc['id']}", w["clinic"],
                     {"standard_price_paise": 50000, "expected_updated_at": "2000-01-01T00:00:00+00:00"})
    assert stale.status_code == 409
    upd = api.call("PATCH", f"/admin/masters/services/{svc['id']}", w["clinic"],
                   {"standard_price_paise": 50000, "expected_updated_at": svc["updated_at"]})
    assert upd.status_code == 200 and upd.json()["standard_price_paise"] == 50000
    second = _order(api, w, enc, [_svc("XRAY_CHEST")]).json()["orders"][0]
    assert second["unit_price_paise"] == 50000
    assert _q(api.conn, "SELECT unit_price_paise FROM opd_orders WHERE id = %s", first["id"]) == 45000   # snapshot kept

    # a billed / ordered service is deactivated, never deleted; an unused one is deleted
    gone = api.as_(w["u"]["admin"]).call("DELETE", f"/admin/masters/services/{svc['id']}", w["clinic"]).json()
    assert gone == {**gone, "deleted": False, "deactivated": True}
    assert _order(api, w, enc, [_svc("XRAY_CHEST")]).status_code == 422
    spare = api.as_(w["u"]["admin"]).call("POST", "/admin/masters/services", w["clinic"],
                                          {**XRAY, "service_code": "SPARE"}).json()
    assert api.call("DELETE", f"/admin/masters/services/{spare['id']}", w["clinic"]).json()["deleted"] is True
    assert "SPARE" not in _services(api, w)


def test_legacy_json_catalogue_still_works_and_settings_write_through(api, world):  # noqa: F811
    w = world
    _q(api.conn, """UPDATE clinics SET opd_settings = COALESCE(opd_settings, '{}'::jsonb) || %s::jsonb WHERE id = %s""",
       '{"service_catalog": [{"code": "ECG", "name": "ECG", "item_type": "diagnostic", "price_paise": 25000}]}',
       w["clinic"])
    assert _services(api, w)["ECG"]["source"] == "legacy"                     # not backfilled: still offered
    _, enc = _visit(api, w)
    assert _order(api, w, enc, [_svc("ECG")]).json()["orders"][0]["unit_price_paise"] == 25000

    api.as_(w["u"]["admin"])
    r = api.call("PUT", "/admin/opd/setup/settings", w["clinic"], {"service_catalog": [
        {"code": "ECG", "name": "ECG 12-lead", "item_type": "diagnostic", "price_paise": 30000}]})
    assert r.status_code == 200, r.text
    ecg = _services(api, w)["ECG"]
    assert (ecg["source"], ecg["name"], ecg["price_paise"]) == ("master", "ECG 12-lead", 30000)
    assert _order(api, w, enc, [_svc("ECG")]).json()["orders"][0]["unit_price_paise"] == 30000


def test_branch_pinned_manager_prices_only_their_branch(api, world):  # noqa: F811
    w = world
    a = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'A') RETURNING id", w["clinic"]))
    b = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'B') RETURNING id", w["clinic"]))
    mgr = api.U("mgr_a", "staff", w["clinic"], str(uuid.uuid4()), ["SERVICES_MANAGE"], staff_role="CUSTOM_ROLE",
                branch_id=a)
    mine = api.as_(mgr).call("POST", "/admin/masters/services", w["clinic"], {**XRAY, "service_code": "PHYSIO_A",
                                                                               "category": "physiotherapy"})
    assert mine.status_code == 201 and mine.json()["branch_id"] == a
    api.as_(w["u"]["admin"])
    theirs = api.call("POST", "/admin/masters/services", w["clinic"], {**XRAY, "service_code": "PHYSIO_B", "branch_id": b}).json()
    shared = api.call("POST", "/admin/masters/services", w["clinic"], {**XRAY, "service_code": "SHARED"}).json()
    api.as_(mgr)
    assert api.call("PATCH", f"/admin/masters/services/{theirs['id']}", w["clinic"],
                    {"standard_price_paise": 1}).status_code == 403
    assert api.call("POST", "/admin/masters/services", w["clinic"],
                    {**XRAY, "service_code": "SNEAK", "branch_id": b}).status_code == 403
    seen = api.call("GET", "/admin/masters/services", w["clinic"]).json()["services"]
    assert {"PHYSIO_A", "SHARED"} <= {s["code"] for s in seen} and "PHYSIO_B" not in {s["code"] for s in seen}
    assert shared["branch_id"] is None


def test_icd10_autocomplete(api, world):  # noqa: F811
    w = world
    api.as_(w["u"]["doctor"])
    r = api.call("GET", "/admin/masters/icd10/search", w["clinic"], q="j06")
    assert r.status_code == 200, r.text
    top = r.json()["results"][0]
    assert top == {**top, "system": "ICD-10", "code": "J06.9", "text": "Acute upper respiratory infection, unspecified"}
    assert any(x["code"] == "K64.9" for x in api.call("GET", "/admin/masters/icd10/search", w["clinic"],
                                                      q="piles").json()["results"])
    assert api.call("GET", "/admin/masters/icd10/search", w["clinic"], q="x" * 61).status_code == 422
    assert api.call("GET", "/admin/masters/icd10/search", w["clinic"]).status_code == 422
    # a WHO U-code (COVID-19) is a valid encounter diagnosis
    assert Diagnosis(system="ICD-10", code="U07.1", text="COVID-19, virus identified").code == "U07.1"
    with pytest.raises(ValueError):
        Diagnosis(system="ICD-10", code="J06-9", text="bad")
