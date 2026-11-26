"""RXR-04 — the in-panel "Business & GST" card (RETAIL-PLAN RXR-ADR-4) through the real routes on real Postgres.

Reads the wizard's values (GSTIN, pharmacist, licence per shop); a pharmacy manager updates them; the GSTIN is
checksum-validated and "" clears it; only opd_settings.gstin / rx_settings.pharmacist_* / rx_stores.drug_licence_no
change (other OPD / RX settings survive); a hospital with OPD keeps the GSTIN in OPD setup (409 here); a pharmacist
reads but cannot write; a store of another clinic is not found; the UI card lives in the RXR block of the panel.
"""

import uuid
from pathlib import Path

from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_rxr_routes import GSTIN, shop  # noqa: F401  (fixture)

OTHER_GSTIN = "29AAGCB7383J1Z4"


def test_card_reads_the_wizard_values(api, shop):  # noqa: F811
    api.as_(shop.pharmacist)
    r = api.call("GET", "/admin/pharmacy/business", shop.clinic)
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["gstin"], b["gstin_editable"], b["pharmacist_registration"]) == (GSTIN, True, "TSPC/2019/4411")
    assert sorted(s["drug_licence_no"] for s in b["stores"]) == ["TS/HYD/20B/1234", "TS/HYD/21B/5678"]


def test_manager_updates_only_the_owned_keys(api, shop):  # noqa: F811
    _q(api.conn, "UPDATE clinics SET opd_settings = opd_settings || '{\"upi_vpa\": \"shop@upi\"}', "
                 "rx_settings = rx_settings || '{\"return_days\": 7}' WHERE id = %s", shop.clinic)
    api.as_(shop.pharmacist)
    assert api.call("PUT", "/admin/pharmacy/business", shop.clinic, {"pharmacist_name": "X Y"}).status_code == 403
    api.as_(shop.admin)
    store = shop.stores[0]["id"]
    r = api.call("PUT", "/admin/pharmacy/business", shop.clinic, {
        "gstin": OTHER_GSTIN.lower(), "pharmacist_name": " Anil Rao ", "pharmacist_registration": "TSPC/2021/9",
        "licences": [{"store_id": store, "drug_licence_no": "TS/HYD/20B/9999"}]})
    assert r.status_code == 200, r.text
    assert r.json()["gstin"] == OTHER_GSTIN and r.json()["pharmacist_name"] == "Anil Rao"
    row = _q(api.conn, "SELECT row_to_json(c) FROM clinics c WHERE id = %s", shop.clinic)
    assert row["opd_settings"]["upi_vpa"] == "shop@upi" and row["rx_settings"]["return_days"] == 7     # untouched
    assert _q(api.conn, "SELECT drug_licence_no FROM rx_stores WHERE id = %s", store) == "TS/HYD/20B/9999"
    assert api.call("PUT", "/admin/pharmacy/business", shop.clinic, {"gstin": "36AABCU9603R1ZX"}).status_code == 422
    assert api.call("PUT", "/admin/pharmacy/business", shop.clinic, {"surprise": 1}).status_code == 422
    assert api.call("PUT", "/admin/pharmacy/business", shop.clinic, {"gstin": ""}).json()["gstin"] is None
    other = _clinic(api.conn)
    assert api.owner_toggle(other, "pharmacy_enabled", True).status_code == 200
    foreign = _q(api.conn, "SELECT id::text FROM rx_stores WHERE clinic_id = %s LIMIT 1", other)
    assert api.call("PUT", "/admin/pharmacy/business", shop.clinic,
                    {"licences": [{"store_id": foreign or str(uuid.uuid4()), "drug_licence_no": "ABC/1"}]}).status_code == 404


def test_opd_clinic_keeps_gstin_in_opd_setup(api):  # noqa: F811
    clinic = _clinic(api.conn)
    _q(api.conn, "UPDATE clinics SET features = coalesce(features, '{}') || '{\"opd_enabled\": true}' WHERE id = %s", clinic)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    b = api.call("GET", "/admin/pharmacy/business", clinic).json()
    assert b["gstin_editable"] is False
    assert api.call("PUT", "/admin/pharmacy/business", clinic, {"gstin": GSTIN}).status_code == 409
    assert api.call("PUT", "/admin/pharmacy/business", clinic, {"pharmacist_name": "Asha K"}).status_code == 200


def test_card_is_in_the_rxr_ui_block():
    html = (Path(__file__).resolve().parents[1] / "admin" / "index.html").read_text(encoding="utf-8")
    assert "/admin/pharmacy/business" in html and 'id="rxBizCard"' in html
