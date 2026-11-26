"""RXR-10 — purchase order to the distributor: CSV export + WhatsApp document (RETAIL-PLAN §7) on real Postgres.

CSV: distributor-friendly columns, one row per line, formula-safe; works for a draft (numbered "DRAFT").
WhatsApp: only an approved PO (status sent) goes out, only through the clinic's own template (rx_settings.po_template,
set on the Business & GST card) with the PO PDF as the header document and clinic name + PO number in the body; a
supplier without a number, a missing template and a draft are refused with a reason; a pharmacist cannot send; another
clinic's PO reads as not found. The pinned /admin/rx route set is untouched (routes live under /admin/pharmacy).
"""

import csv
import io
import uuid
from unittest.mock import AsyncMock

import pytest

from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_rx_purchase import buy  # noqa: F401  (fixture)


@pytest.fixture
def po(api, buy):  # noqa: F811
    c = buy["clinic"]
    r = api.call("POST", "/admin/rx/purchase-orders", c, {"store_id": buy["store"], "supplier_id": buy["supplier"],
                 "lines": [{"item_id": buy["items"]["AUG625"], "ordered_packs": 10, "free_packs": 1,
                            "expected_rate_pack_paise": 10050}]})
    assert r.status_code == 201, r.text
    return {**buy, "po": r.json()["id"]}


@pytest.fixture
def wa(monkeypatch):
    from app.services.whatsapp import whatsapp_service
    up = AsyncMock(return_value="MEDIA123")
    tpl = AsyncMock(return_value=True)
    monkeypatch.setattr(whatsapp_service, "upload_media", up)
    monkeypatch.setattr(whatsapp_service, "send_template", tpl)
    return up, tpl


def test_csv_export_of_a_draft_and_an_approved_po(api, po):  # noqa: F811
    c = po["clinic"]
    r = api.call("GET", f"/admin/pharmacy/purchase-orders/{po['po']}.csv", c)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:3] == ["PO number", "Line", "Item code"]
    assert rows[1][0] == "DRAFT" and rows[1][2] == "AUG625" and rows[1][5:] == ["10", "1", "100.50"]
    assert api.call("POST", f"/admin/rx/purchase-orders/{po['po']}/send", c).status_code == 200
    rows = list(csv.reader(io.StringIO(api.call("GET", f"/admin/pharmacy/purchase-orders/{po['po']}.csv", c).text)))
    assert rows[1][0].startswith("PO-")


def test_csv_cells_are_formula_safe():
    from app.services.rx_retail import po_csv
    out = po_csv({"po_number": "PO-1", "lines": [{"line_no": 1, "ordered_packs": 1, "item": {"brand_name": "=CMD()"}}]})
    assert "'=CMD()" in out


def test_whatsapp_send_rules(api, po, wa):  # noqa: F811
    up, tpl = wa
    c, path = po["clinic"], f"/admin/pharmacy/purchase-orders/{po['po']}/whatsapp"
    r = api.call("POST", path, c)
    assert r.status_code == 409 and "Approve" in r.text                           # a draft never goes out
    assert api.call("POST", f"/admin/rx/purchase-orders/{po['po']}/send", c).status_code == 200
    r = api.call("POST", path, c)
    assert r.status_code == 409 and "template" in r.text
    assert api.call("PUT", "/admin/pharmacy/business", c, {"po_template": "Bad Name"}).status_code == 422
    assert api.call("PUT", "/admin/pharmacy/business", c, {"po_template": "rx_purchase_order_v1"}).json()["po_template"] \
        == "rx_purchase_order_v1"
    r = api.call("POST", path, c)
    assert r.status_code == 409 and "WhatsApp number" in r.text                  # the supplier has no phone yet
    assert api.call("PATCH", f"/admin/rx/suppliers/{po['supplier']}", c, {"phone": "+91 98480 22338"}).status_code == 200
    r = api.call("POST", path, c)
    assert r.status_code == 200, r.text
    assert r.json()["to"].endswith("2338") and "98480" not in r.json()["to"]
    assert up.await_args.args[1][:4] == b"%PDF" and up.await_args.args[3] == "application/pdf"
    sent = tpl.await_args
    assert sent.args[1] == "+919848022338" and sent.args[2] == "rx_purchase_order_v1"
    header, body = sent.kwargs["components"]
    assert header["parameters"][0]["document"]["id"] == "MEDIA123"
    assert body["parameters"][1]["text"].startswith("PO-")
    tpl.return_value = False
    assert api.call("POST", path, c).status_code == 502
    api.as_(api.U("pharma", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACIST"))
    assert api.call("POST", path, c).status_code == 403


def test_another_clinic_cannot_reach_the_po(api, po):  # noqa: F811
    other = _clinic(api.conn)
    assert api.owner_toggle(other, "pharmacy_enabled", True).status_code == 200
    api.as_(api.U("admin", "clinic_admin", other, str(uuid.uuid4())))
    assert api.call("GET", f"/admin/pharmacy/purchase-orders/{po['po']}.csv", other).status_code == 404
