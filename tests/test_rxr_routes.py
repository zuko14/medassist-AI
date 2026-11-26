"""A standalone pharmacy end to end through the real routes on real Postgres (lane RXR).

The owner's 3-step wizard creates a `pharmacy`-plan clinic with Pharmacy OS on, one store per shop with its drug
licence, the GSTIN and the pharmacist; OPD stays off (no doctor routes); customers are registered and searched through
the same registry service (MRN); cashier shifts open / close without OPD; the WhatsApp front door turns a prescription
photo into an order request and never offers a booking; the stock answer never makes an H / H1 / X drug orderable;
the pharmacist works the queue; a hospital's OPD shift routes are unchanged and a non-pharmacy clinic is refused.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import rx_retail
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
from tests.cc_world import WA
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_rx_catalogue_api import ROWS, csv_bytes, upload

GSTIN = "36AABCU9603R1ZO"
WIZARD = {"name": "Sri Sai Medicals", "meta_phone_number_id": "1234567890", "meta_access_token": "EAAG" + "x" * 40,
          "gstin": GSTIN, "pharmacist_name": "Ravi Kumar", "pharmacist_registration": "TSPC/2019/4411",
          "hospital_address": "12 MG Road, Hyderabad",
          "shops": [{"name": "Ameerpet", "drug_licence_no": "TS/HYD/20B/1234"},
                    {"name": "Kukatpally", "drug_licence_no": "TS/HYD/21B/5678"}]}


@pytest.fixture
def shop(api, monkeypatch):  # noqa: F811
    wa = WA().install(monkeypatch)
    r = api.client.post("/platform/pharmacies", json={**WIZARD, "whatsapp_number": "+9188" + str(uuid.uuid4().int)[:8],
                                                      "meta_phone_number_id": str(uuid.uuid4().int)[:15]})
    assert r.status_code == 201, r.text
    cid = r.json()["clinic"]["id"]
    invalidate_tenant_cache()
    return SimpleNamespace(clinic=cid, stores=r.json()["stores"], wa=wa, admin=api.U("admin", "clinic_admin", cid,
                                                                                     str(uuid.uuid4())),
                           pharmacist=api.U("pharma", "staff", cid, str(uuid.uuid4()), [], staff_role="PHARMACIST"))


def test_wizard_creates_a_pharmacy_with_stores_licences_and_pharmacist(api, shop):  # noqa: F811
    row = _q(api.conn, "SELECT row_to_json(c) FROM clinics c WHERE id = %s", shop.clinic)
    assert (row["plan"], row["rx_state"], row["features"]["pharmacy_enabled"]) == ("pharmacy", "ACTIVE", True)
    assert row["opd_settings"]["gstin"] == GSTIN and row["rx_settings"]["pharmacist_registration"] == "TSPC/2019/4411"
    assert row["opd_state"] in (None, "OFF", "NOT_CONFIGURED") and not row["features"].get("opd_enabled")
    stores = {s["name"]: s["drug_licence_no"] for s in shop.stores}
    assert stores == {"Ameerpet": "TS/HYD/20B/1234", "Kukatpally": "TS/HYD/21B/5678"}
    assert _q(api.conn, "SELECT count(*) FROM doctors WHERE clinic_id = %s", shop.clinic) == 0
    bad = {**WIZARD, "whatsapp_number": "+918800000001", "gstin": "XX", "meta_phone_number_id": "99" + uuid.uuid4().hex[:10]}
    assert api.client.post("/platform/pharmacies", json=bad).status_code == 422
    assert api.client.post("/platform/pharmacies", json={**WIZARD, "whatsapp_number": "+918800000002", "shops": [],
                                                         "meta_phone_number_id": "98" + uuid.uuid4().hex[:10]}).status_code == 422
    api.as_(shop.admin)
    assert api.call("GET", "/admin/opd/setup", shop.clinic).status_code == 403     # no OPD on a pharmacy
    assert api.call("GET", "/admin/rx/setup", shop.clinic).status_code == 200


def test_customers_and_shifts_without_opd(api, shop):  # noqa: F811
    api.as_(shop.pharmacist)
    body = {"phone": "+919812345678", "name": "Anita Rao", "data_consent": True, "gender": "female", "age_years": 44}
    r = api.call("POST", "/admin/pharmacy/customers", shop.clinic, body)
    assert r.status_code == 201, r.text
    assert r.json()["patient_id"]
    assert api.call("POST", "/admin/pharmacy/customers", shop.clinic, {**body, "data_consent": False}).status_code == 422
    hits = api.call("GET", "/admin/pharmacy/customers", shop.clinic, q="Anita").json()
    assert any(h.get("name") == "Anita Rao" for h in (hits if isinstance(hits, list) else hits.get("results", [])))
    o = api.call("POST", "/admin/pharmacy/shifts/open", shop.clinic, {"opening_float_paise": 50000})
    assert o.status_code == 200, o.text
    cur = api.call("GET", "/admin/pharmacy/shifts/current", shop.clinic).json()
    sid = (cur.get("shift") or cur).get("id")
    assert sid
    c = api.call("POST", f"/admin/pharmacy/shifts/{sid}/close", shop.clinic, {"declared_cash_paise": 50000})
    assert c.status_code == 200, c.text
    api.as_(api.U("op", "staff", shop.clinic, str(uuid.uuid4()), [], staff_role="STORE_KEEPER"))
    assert api.call("POST", "/admin/pharmacy/shifts/open", shop.clinic, {"opening_float_paise": 0}).status_code == 403
    api.as_(shop.admin)
    assert api.call("GET", "/admin/pharmacy/shifts", shop.clinic).status_code == 200


def test_new_routes_refuse_a_clinic_without_pharmacy_and_hospital_opd_shifts_unchanged(api):  # noqa: F811
    hosp = _clinic(api.conn)                                                    # OPD on, pharmacy off
    api.as_(api.U("admin", "clinic_admin", hosp, str(uuid.uuid4())))
    for method, path, body in (("POST", "/admin/pharmacy/shifts/open", {"opening_float_paise": 0}),
                               ("GET", "/admin/pharmacy/customers?q=ab", None), ("GET", "/admin/pharmacy/order-requests", None)):
        r = api.client.request(method, path.split("?")[0], params={"clinic_id": hosp, **({"q": "ab"} if "?" in path else {})},
                               json=body)
        assert r.status_code == 403 and "not enabled" in r.text, (path, r.text)
    assert api.call("POST", "/admin/opd/shifts/open", hosp, {"opening_float_paise": 0}).status_code == 200


def _m():
    from app.services.whatsapp import whatsapp_service
    return SimpleNamespace(whatsapp=whatsapp_service)


def _turn(clinic, phone, state, message="", mtype="text", data=None):
    states = []

    async def set_state(st):
        states.append(st)
    out = asyncio.run(rx_retail.handle(_m(), clinic, phone, state, message, mtype, data, "en", set_state))
    return out, states


def test_whatsapp_front_door(api, shop):  # noqa: F811
    clinic = asyncio.run(get_clinic_by_id(shop.clinic))
    phone = "+919800012345"
    assert _turn(clinic, phone, "main_menu", "book appointment with doctor") == (True, ["main_menu"])
    menu = shop.wa.of("list")[-1]
    assert [r["id"] for r in menu.args[1][0]["rows"]][:5] == ["rx_menu_refill", "rx_menu_send", "rx_menu_stock",
                                                              "rx_menu_hours", "rx_menu_staff"]
    assert _turn(clinic, phone, "main_menu", mtype="interactive", data={"id": "rx_menu_send"}) == (True, ["rx_upload_rx"])
    ok, st = _turn(clinic, phone, "rx_upload_rx", mtype="image", data={"type": "image", "media_id": "MEDIA123",
                                                                       "caption": "for my mother"})
    assert ok and st == ["main_menu"] and "with our pharmacist" in shop.wa.of("text")[-1].args[0]
    _turn(clinic, phone, "rx_upload_rx", mtype="image", data={"type": "image", "media_id": "MEDIA124"})
    assert "already have your request" in shop.wa.of("text")[-1].args[0]
    rows = _q(api.conn, "SELECT json_agg(json_build_array(kind, source, media_ref, note, phone_masked)) "
                        "FROM pharmacy_order_requests WHERE clinic_id = %s", shop.clinic)
    assert rows == [["prescription", "whatsapp_photo", "MEDIA123", "for my mother", "+91XXXXXX2345"]]
    assert _turn(clinic, phone, "main_menu", mtype="interactive", data={"id": "menu_emergency"})[0] is False
    _turn(clinic, phone, "main_menu", mtype="interactive", data={"id": "rx_menu_refill"})
    assert _q(api.conn, "SELECT count(*) FROM pharmacy_order_requests WHERE clinic_id = %s AND kind = 'refill'", shop.clinic) == 1


def test_stock_answer_never_makes_a_prescription_drug_orderable(api, shop):  # noqa: F811
    api.as_(shop.admin)
    assert upload(api, "/admin/rx/items/import/apply", shop.clinic, csv_bytes(ROWS)).status_code == 200
    clinic = asyncio.run(get_clinic_by_id(shop.clinic))
    _turn(clinic, "+919800054321", "main_menu", mtype="interactive", data={"id": "rx_menu_stock"})
    _turn(clinic, "+919800054321", "rx_stock_query", "Augmentin 625 Duo")
    aug = shop.wa.of("text")[-1].args[0]
    assert "only against a valid prescription" in aug and "available at the counter" not in aug    # no stock yet
    _turn(clinic, "+919800054321", "rx_stock_query", "zzzz unknown med")
    assert "could not find" in shop.wa.of("text")[-1].args[0]
    for s in shop.wa.sent:                                                       # never a dose, a substitute or a price
        assert "mg twice" not in str(s.args) and "₹" not in str(s.args) and "instead" not in str(s.args)


def test_pharmacist_queue(api, shop):  # noqa: F811
    api.as_(shop.pharmacist)
    r = api.call("POST", "/admin/pharmacy/order-requests", shop.clinic, {"phone": "+919800099999", "kind": "callback"})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert api.call("POST", "/admin/pharmacy/order-requests", shop.clinic, {"phone": "+919800099999",
                                                                      "kind": "callback"}).status_code == 409
    q = api.call("GET", "/admin/pharmacy/order-requests", shop.clinic).json()["requests"]
    assert [x["id"] for x in q] == [rid] and "phone" not in q[0] and q[0]["has_photo"] is False
    mv = lambda to, **kw: api.call("POST", f"/admin/pharmacy/order-requests/{rid}/move", shop.clinic, {"to": to, **kw})  # noqa: E731
    assert mv("collected").status_code == 409                                    # new → collected skips the review
    assert mv("rejected").status_code == 422
    assert mv("ready").status_code == 200
    assert "ready to collect" in shop.wa.of("text")[-1].args[0]
    assert mv("collected").status_code == 422                                    # needs the sale it was billed on
    assert mv("collected", sale_id=str(uuid.uuid4())).status_code == 404         # not a sale of this clinic
    assert mv("cancelled").status_code == 200
    assert api.call("GET", f"/admin/pharmacy/order-requests/{rid}/photo", shop.clinic).status_code == 404
    api.as_(api.U("x", "staff", shop.clinic, str(uuid.uuid4()), [], staff_role="FRONT_DESK"))
    assert api.call("GET", "/admin/pharmacy/order-requests", shop.clinic).status_code == 403
