"""Pharmacy OS purchase through the real /admin/rx routes on real Postgres (RX-23…RX-26, W1 / W6).

Supplier GSTIN checksum; PO draft → send (manager approval) → partial / full receipt; GRN landed
cost (TEST-PLAN §4.3), interstate IGST, total mismatch, duplicate supplier invoice, void only while
unconsumed; supplier return of an expired batch with its debit note; store scope; PDFs; GS1 lookup.
"""

import uuid
from datetime import date, timedelta

import pytest

from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_rx_catalogue_api import ROWS, _code, csv_bytes, upload

CLINIC_GSTIN = "36AABCU9603R1ZO"        # Telangana
LOCAL_SUPPLIER = "36AAACB1234F1ZA"     # Telangana
OTHER_STATE = "29AAACS1234K1ZD"        # Karnataka


def _exp(days=500):
    d = date.today() + timedelta(days=days)
    return f"{d.month:02d}/{d.year}"


@pytest.fixture
def buy(api):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    _q(api.conn, "UPDATE clinics SET opd_settings = COALESCE(opd_settings, '{}'::jsonb) || jsonb_build_object('gstin', %s) "
                 "WHERE id = %s", CLINIC_GSTIN, clinic)
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    admin = api.U("admin", "clinic_admin", clinic, str(uuid.uuid4()))
    api.as_(admin)
    assert upload(api, "/admin/rx/items/import/apply", clinic, csv_bytes(ROWS)).status_code == 200
    items = dict(_q(api.conn, "SELECT json_agg(json_build_array(item_code, id)) FROM rx_items WHERE clinic_id = %s",
                    clinic))
    store = str(_q(api.conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", clinic))
    sup = api.call("POST", "/admin/rx/suppliers", clinic, {"name": "Sri Balaji Pharma", "gstin": LOCAL_SUPPLIER,
                                                           "drug_licence_no": "TS/HYD/20B-1", "payment_terms_days": 21})
    assert sup.status_code == 201, sup.text
    return {"clinic": clinic, "store": store, "items": items, "supplier": sup.json()["id"], "admin": admin}


def _line(item, **kw):
    return {"item_id": item, "batch_no": "B1", "expiry": _exp(), "packs": 10, "free_packs": 2, "rate_pack_paise": 10000,
            "scheme_discount_bps": 1000, "cash_discount_bps": 200, "tax_rate_bps": 500, "mrp_pack_paise": 15000, **kw}


def _grn(api, b, invoice="INV-1", lines=None, **kw):  # noqa: F811
    body = {"store_id": b["store"], "supplier_id": b["supplier"], "supplier_invoice_no": invoice,
            "supplier_invoice_date": str(date.today()), "lines": lines or [_line(b["items"]["AUG625"])], **kw}
    return api.call("POST", "/admin/rx/grns", b["clinic"], body)


def test_supplier_gstin_checksum_and_state(api, buy):  # noqa: F811
    bad = api.call("POST", "/admin/rx/suppliers", buy["clinic"], {"name": "Typo Pharma", "gstin": "36AAACB1234F1ZB"})
    assert bad.status_code == 422 and "check digit" in bad.text
    dup = api.call("POST", "/admin/rx/suppliers", buy["clinic"], {"name": "Again", "gstin": LOCAL_SUPPLIER.lower()})
    assert dup.status_code == 409
    other = api.call("POST", "/admin/rx/suppliers", buy["clinic"], {"name": "Bengaluru Dist", "gstin": OTHER_STATE,
                                                                   "state_code": "36"}).json()
    assert other["state_code"] == "29"                                    # the GSTIN decides the state
    upd = api.call("PATCH", f"/admin/rx/suppliers/{other['id']}", buy["clinic"], {"lead_time_days": 7, "is_active": False})
    assert (upd.json()["lead_time_days"], upd.json()["is_active"]) == (7, False)
    assert [s["name"] for s in api.call("GET", "/admin/rx/suppliers", buy["clinic"], active="true").json()["suppliers"]] \
        == ["Sri Balaji Pharma"]


def test_po_to_grn_to_stock(api, buy):  # noqa: F811
    c, aug = buy["clinic"], buy["items"]["AUG625"]
    po = api.call("POST", "/admin/rx/purchase-orders", c, {"store_id": buy["store"], "supplier_id": buy["supplier"],
                  "expected_on": str(date.today() + timedelta(days=3)),
                  "lines": [{"item_id": aug, "ordered_packs": 10, "expected_rate_pack_paise": 10000}]})
    assert po.status_code == 201, po.text
    pid, pl = po.json()["id"], po.json()["lines"][0]["id"]
    edited = api.call("PATCH", f"/admin/rx/purchase-orders/{pid}", c, {"lines": [
        {"item_id": aug, "ordered_packs": 12, "free_packs": 1, "expected_rate_pack_paise": 10000}]}).json()
    pl = edited["lines"][0]["id"]
    assert edited["lines"][0]["ordered_packs"] == 12
    assert edited["lines"][0]["item"]["tax_rate_bps"] == 1200          # GRN lines prefilled from the PO keep the item GST
    # a store keeper may draft, not approve
    keeper = api.U("keeper", "staff", c, str(uuid.uuid4()), [], staff_role="STORE_KEEPER")
    assert api.as_(keeper).call("POST", f"/admin/rx/purchase-orders/{pid}/send", c).status_code == 403
    sent = api.as_(buy["admin"]).call("POST", f"/admin/rx/purchase-orders/{pid}/send", c).json()
    assert sent["po_number"] == f"PO-{date.today().year}-00001"
    assert api.call("PATCH", f"/admin/rx/purchase-orders/{pid}", c, {"notes": "late"}).status_code == 409
    pdf = api.client.get(f"/admin/rx/purchase-orders/{pid}.pdf", params={"clinic_id": c})
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content[:4] == b"%PDF"
    # receive 8 of 12 against the PO (pay for 8, 1 free)
    g = _grn(api.as_(keeper), buy, po_id=pid, lines=[_line(aug, po_line_id=pl, packs=8, free_packs=1)])
    assert g.status_code == 201, g.text
    gid, body = g.json()["id"], g.json()
    total = body["computed_total_paise"]
    assert api.call("PATCH", f"/admin/rx/grns/{gid}", c, {"invoice_total_paise": total + 40}).json()["can_post"] is True
    out = api.call("POST", f"/admin/rx/grns/{gid}/post", c, {"idempotency_key": "grn-post-0001"}).json()
    assert out["grn_number"] == f"GRN-{date.today().year}-00001" and out["units"] == 90 and out["round_off_paise"] == 40
    assert api.call("POST", f"/admin/rx/grns/{gid}/post", c, {"idempotency_key": "grn-post-0001"}).json()["replayed"]
    assert api.call("GET", f"/admin/rx/purchase-orders/{pid}", c).json()["status"] == "partially_received"
    stock = {r["item"]["item_code"]: r["qty_units"] for r in api.call("GET", "/admin/rx/stock", c).json()["stock"]}
    assert stock == {"AUG625": 90}
    labels = api.client.get(f"/admin/rx/grns/{gid}/labels.pdf", params={"clinic_id": c, "copies": "per_pack"})
    assert labels.status_code == 200 and labels.content[:4] == b"%PDF"
    # last supplier now feeds the reorder list (W1 step 1): AUG625 reorder level 20 is met (90) → not listed
    reorder = api.call("GET", "/admin/rx/reorder", c).json()["reorder"]
    assert all(r["item"]["item_code"] != "AUG625" for r in reorder)
    # cancelling a partly received PO is refused; a fresh sent PO can be cancelled with a reason
    assert api.call("POST", f"/admin/rx/purchase-orders/{pid}/cancel", c, {"reason": "Not needed"}).status_code == 409
    listed = api.call("GET", "/admin/rx/purchase-orders", c, status="partially_received").json()["purchase_orders"]
    assert [p["supplier_name"] for p in listed] == ["Sri Balaji Pharma"]


def test_free_packs_dilute_cost(api, buy):  # noqa: F811
    g = _grn(api, buy).json()                                    # TEST-PLAN §4.3, clinic WITH GSTIN
    ln = g["lines"][0]
    assert (ln["taxable_paise"], ln["tax_paise"], ln["landed_unit_cost_paise_x100"]) == (88200, 4410, 73500)
    assert g["computed_total_paise"] == 92610 and g["is_interstate"] is False
    assert (g["cgst_paise"], g["sgst_paise"], g["igst_paise"]) == (2205, 2205, 0)
    assert any("drug master" in w for w in ln["warnings"])          # 5 % typed vs 12 % in the master
    assert not any("above MRP" in w for w in ln["warnings"])


def test_interstate_igst(api, buy):  # noqa: F811
    other = api.call("POST", "/admin/rx/suppliers", buy["clinic"], {"name": "Bengaluru Dist", "gstin": OTHER_STATE})
    g = _grn(api, {**buy, "supplier": other.json()["id"]}).json()
    assert g["is_interstate"] is True and (g["igst_paise"], g["cgst_paise"], g["sgst_paise"]) == (4410, 0, 0)


def test_total_mismatch_blocks_post(api, buy):  # noqa: F811
    g = _grn(api, buy, invoice_total_paise=92610 + 100).json()
    assert (g["difference_paise"], g["can_post"]) == (100, False)
    r = api.call("POST", f"/admin/rx/grns/{g['id']}/post", buy["clinic"], {})
    assert r.status_code == 409 and _code(r) == "rx_grn_total_mismatch" and r.json()["detail"]["reason"] == "100"
    assert _q(api.conn, "SELECT count(*) FROM rx_stock_movements WHERE clinic_id = %s", buy["clinic"]) == 0


def test_duplicate_supplier_invoice(api, buy):  # noqa: F811
    assert _grn(api, buy).status_code == 201
    dup = _grn(api, buy)
    assert dup.status_code == 409 and _code(dup) == "rx_grn_duplicate_invoice"
    assert _q(api.conn, "SELECT count(*) FROM rx_grns WHERE clinic_id = %s", buy["clinic"]) == 1


def test_grn_line_rules(api, buy):  # noqa: F811
    aug = buy["items"]["AUG625"]
    expired = _grn(api, buy, "EXP", [_line(aug, expiry=(date.today() - timedelta(days=40)).strftime("%m/%Y"))])
    assert expired.status_code == 422 and _code(expired) == "rx_grn_expired_batch"
    assert _q(api.conn, "SELECT count(*) FROM rx_grns WHERE clinic_id = %s", buy["clinic"]) == 0   # header rolled back
    warn = _grn(api, buy, "W", [_line(aug, rate_pack_paise=20000, expiry=_exp(20), tax_rate_bps=None)]).json()
    w = warn["lines"][0]["warnings"]
    assert any("above MRP" in x for x in w) and any("Expires in" in x for x in w)
    assert warn["lines"][0]["tax_rate_bps"] == 1200                      # default from the item master
    assert _grn(api, buy, "BAD", [_line(aug, expiry="31/02/2027")]).status_code == 422
    assert _grn(api, buy, "DAY", [_line(aug, expiry="15/03/2030")]).json()["lines"][0]["expiry_date"] == "2030-03-15"
    # a counter that does not receive goods
    tray = api.call("POST", "/admin/rx/stores", buy["clinic"], {"code": "OT1", "name": "OT tray", "store_type": "ot_tray"})
    r = _grn(api, {**buy, "store": tray.json()["id"]}, "TRAY")
    assert r.status_code == 409 and _code(r) == "rx_store_cannot_receive"


def test_void_only_when_unconsumed(api, buy):  # noqa: F811
    c = buy["clinic"]
    g = _grn(api, buy, invoice_total_paise=92610).json()
    api.call("POST", f"/admin/rx/grns/{g['id']}/post", c, {})
    batch = str(_q(api.conn, "SELECT batch_id FROM rx_grn_lines WHERE grn_id = %s", g["id"]))
    keeper = api.U("keeper", "staff", c, str(uuid.uuid4()), [], staff_role="STORE_KEEPER")
    assert api.as_(keeper).call("POST", f"/admin/rx/grns/{g['id']}/void", c, {"reason": "Wrong bill"}).status_code == 403
    api.as_(buy["admin"]).call("POST", "/admin/rx/write-offs", c, {"store_id": buy["store"], "lines": [
        {"batch_id": batch, "qty_units": 1, "kind": "damage", "reason": "Dropped"}]})
    r = api.call("POST", f"/admin/rx/grns/{g['id']}/void", c, {"reason": "Wrong bill"})
    assert r.status_code == 409 and _code(r) == "rx_grn_stock_consumed"
    g2 = _grn(api, buy, "INV-2", [_line(buy["items"]["DOLO650"], batch_no="D9", tax_rate_bps=1200)]).json()
    api.call("PATCH", f"/admin/rx/grns/{g2['id']}", c, {"invoice_total_paise": g2["computed_total_paise"]})
    api.call("POST", f"/admin/rx/grns/{g2['id']}/post", c, {})
    ok = api.call("POST", f"/admin/rx/grns/{g2['id']}/void", c, {"reason": "Duplicate entry"})
    assert ok.status_code == 200 and api.call("GET", f"/admin/rx/grns/{g2['id']}", c).json()["status"] == "void"
    assert {r["item"]["item_code"] for r in api.call("GET", "/admin/rx/stock", c).json()["stock"]} == {"AUG625"}


def test_supplier_return_of_expired_batch(api, buy):  # noqa: F811
    c = buy["clinic"]
    g = _grn(api, buy, invoice_total_paise=92610).json()
    api.call("POST", f"/admin/rx/grns/{g['id']}/post", c, {})
    batch = str(_q(api.conn, "SELECT batch_id FROM rx_grn_lines WHERE grn_id = %s", g["id"]))
    # an expired batch on the shelf (seeded the way an rx_* function would: kriya.rx_tx set)
    item = buy["items"]["DOLO650"]
    old = str(_q(api.conn, "INSERT INTO rx_batches (clinic_id, item_id, batch_no, expiry_date, mrp_pack_paise, "
                           "ptr_pack_paise, landed_unit_cost_paise_x100) VALUES (%s, %s, 'OLD1', %s, 3000, 2100, 14000) "
                           "RETURNING id", c, item, date.today() - timedelta(days=10)))
    _q(api.conn, "SELECT set_config('kriya.rx_tx', %s, true); INSERT INTO rx_stock_movements (clinic_id, store_id, "
                 "batch_id, item_id, qty_units, movement_type, ref_table, ref_id, unit_cost_paise_x100, actor_name) "
                 "VALUES (%s, %s, %s, %s, 30, 'opening', 'test', %s, 14000, 'T')",
       c, c, buy["store"], old, item, str(uuid.uuid4()))
    r = api.call("POST", "/admin/rx/supplier-returns", c, {"store_id": buy["store"], "supplier_id": buy["supplier"],
                 "reason": "expired", "lines": [{"batch_id": old, "qty_units": 30}, {"batch_id": batch, "qty_units": 10}]})
    assert r.status_code == 201, r.text
    ret = r.json()
    rates = {ln["batch"]["batch_no"]: (ln["rate_pack_paise"], ln["tax_rate_bps"]) for ln in ret["lines"]}
    assert rates == {"OLD1": (2100, 1200), "B1": (10000, 500)}            # GRN line rate, else batch PTR + item GST
    # debit note at GRN rate: OLD1 30 × 2100 / 15 = 4200 (+12 % = 504); B1 10 × 10000 / 10 = 10000 (+5 % = 500)
    assert ret["debit_note"] == {"taxable_paise": 14200, "tax_paise": 1004, "total_paise": 15204}
    assert api.call("PATCH", f"/admin/rx/supplier-returns/{ret['id']}/credit", c,
                    {"credit_received_paise": 1, "credit_ref": "CN-1"}).status_code == 409
    d = api.call("POST", f"/admin/rx/supplier-returns/{ret['id']}/dispatch", c).json()
    assert d["srn_number"] == f"SRN-{date.today().year}-00001" and d["units"] == 40
    assert api.call("POST", f"/admin/rx/supplier-returns/{ret['id']}/dispatch", c).json()["replayed"] is True
    assert _q(api.conn, "SELECT qty_units FROM rx_stock_balances WHERE batch_id = %s", old) == 0
    cr = api.call("PATCH", f"/admin/rx/supplier-returns/{ret['id']}/credit", c,
                  {"credit_received_paise": 15204, "credit_ref": "CN/2026/88"})
    assert cr.status_code == 200 and cr.json()["credit_ref"] == "CN/2026/88"
    assert [x["status"] for x in api.call("GET", "/admin/rx/supplier-returns", c).json()["returns"]] == ["dispatched"]


def test_store_scope_and_tenant_isolation(api, buy):  # noqa: F811
    c = buy["clinic"]
    g = _grn(api, buy).json()
    branch = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Miyapur') RETURNING id", c))
    pinned = api.U("pinned", "staff", c, str(uuid.uuid4()), [], staff_role="STORE_KEEPER", branch_id=branch)
    api.as_(pinned)
    assert api.call("GET", f"/admin/rx/grns/{g['id']}", c).status_code == 404       # MAIN is not their store
    assert api.call("GET", "/admin/rx/grns", c).json()["grns"] == []
    assert _grn(api, buy, "PIN").status_code == 404
    other = _clinic(api.conn)
    api.owner_toggle(other, "pharmacy_enabled", True)
    api.as_(api.U("x", "clinic_admin", other, str(uuid.uuid4())))
    assert api.call("GET", f"/admin/rx/grns/{g['id']}", other).status_code == 404
    assert api.call("PATCH", f"/admin/rx/suppliers/{buy['supplier']}", other, {"name": "Stolen"}).status_code == 404
    assert api.call("GET", "/admin/rx/suppliers", other).json()["suppliers"] == []


def test_gs1_scan_resolves_item(api, buy):  # noqa: F811
    c = buy["clinic"]
    gtin = "08901234560013"
    # the pack's EAN-13 is on the item; the scan carries its GTIN-14 form
    assert api.call("POST", f"/admin/rx/items/{buy['items']['DOLO650']}/barcodes", c,
                    {"barcode": "8901234560013"}).status_code == 201
    r = api.call("GET", "/admin/rx/gs1", c, code=f"(01){gtin}(17)280300(10)DL77(21)S1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["batch_no"], body["expiry_date"], body["serial"]) == ("DL77", "2028-03-31", "S1")
    assert body["item"]["item_code"] == "DOLO650"
    bad = api.call("GET", "/admin/rx/gs1", c, code="(01)08901234560014")
    assert bad.status_code == 422 and _code(bad) == "rx_gs1_invalid"
