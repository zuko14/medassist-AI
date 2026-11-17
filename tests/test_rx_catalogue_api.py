"""Pharmacy OS drug master through the real /admin/rx routes on real Postgres (RX-13…RX-15).

CRUD with the database guard behind it (immutables 409, tax change needs a reason, change
log), tenant isolation, the CSV/XLSX import (dry run writes nothing, all-or-nothing apply,
idempotent re-apply), and the deterministic resolver with same-generic substitutes.
"""

import io
import uuid

import pytest

from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)

HEADER = ("item_code,brand_name,generic_name,strength,dosage_form,route,manufacturer,item_kind,schedule,base_unit,"
          "pack_size,pack_label,allow_loose,hsn_code,gst_rate_percent,reorder_level_units,reorder_qty_units,is_chronic,"
          "is_cold_chain,rack_location,barcode")
ROWS = ["DOLO650,Dolo 650,Paracetamol,650 mg,tablet,oral,Micro Labs,drug,none,tablet,15,strip,yes,30049099,12,60,300,"
        "no,no,A-1,8901234560014",
        "CALPOL650,Calpol 650,Paracetamol,650 mg,Tablet,oral,GSK,drug,none,tablet,15,strip,yes,30049099,12,,,no,no,,",
        "AUG625,Augmentin 625 Duo,Amoxicillin + Clavulanic acid,500 mg + 125 mg,tablet,oral,GSK,drug,H,tablet,10,strip,"
        "yes,30042019,12,20,100,no,no,B-2,"]


def csv_bytes(rows):
    return ("\n".join([HEADER, *rows]) + "\n").encode()


def upload(api, path, clinic, content, filename="items.csv", **params):  # noqa: F811
    return api.client.post(path, params={**params, "clinic_id": clinic}, files={"file": (filename, content)})


@pytest.fixture
def rxc(api):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    return clinic


def _code(r):
    d = r.json().get("detail")
    return d.get("code") if isinstance(d, dict) else d


def test_catalogue_crud_guard_and_change_log(api, rxc):  # noqa: F811
    m = api.call("POST", "/admin/rx/manufacturers", rxc, {"name": "Cipla"})
    assert m.status_code == 201
    assert api.call("POST", "/admin/rx/manufacturers", rxc, {"name": "CIPLA"}).status_code == 409
    g = api.call("POST", "/admin/rx/generics", rxc, {"name": "Azithromycin", "strength": "500 mg", "dosage_form": "tablet"})
    assert g.status_code == 201, g.text
    body = {"item_code": "AZEE500", "brand_name": "Azee 500", "generic_id": g.json()["id"],
            "manufacturer_id": m.json()["id"], "schedule": "H", "base_unit": "tablet", "pack_size": 3,
            "hsn_code": "30042099", "tax_rate_bps": 1200}
    item = api.call("POST", "/admin/rx/items", rxc, body)
    assert item.status_code == 201, item.text
    iid = item.json()["id"]
    assert api.call("POST", "/admin/rx/items", rxc, body).status_code == 409                       # code taken
    assert api.call("POST", "/admin/rx/items", rxc, {**body, "item_code": "lower"}).status_code == 422
    assert api.call("POST", "/admin/rx/items", rxc, {**body, "item_code": "NOGEN", "generic_id": None}).status_code == 422
    listed = api.call("GET", "/admin/rx/items", rxc, q="azee").json()["items"]
    assert [(i["item_code"], i["generic"]["name"], i["stock_units"]) for i in listed] == [("AZEE500", "Azithromycin", 0)]
    # guard: immutables and schedule downgrade → 409 with the field
    r = api.call("PATCH", f"/admin/rx/items/{iid}", rxc, {"item_code": "AZEE"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "item_code"
    r = api.call("PATCH", f"/admin/rx/items/{iid}", rxc, {"schedule": "none"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "schedule"
    assert _code(api.call("PATCH", f"/admin/rx/items/{iid}", rxc, {"tax_rate_bps": 500})) == "rx_item_reason_required"
    ok = api.call("PATCH", f"/admin/rx/items/{iid}", rxc, {"tax_rate_bps": 500, "schedule": "H1",
                                                           "reason": "Notification 9/2026"})
    assert ok.status_code == 200 and (ok.json()["tax_rate_bps"], ok.json()["schedule"]) == (500, "H1")
    assert api.call("POST", f"/admin/rx/items/{iid}/barcodes", rxc, {"barcode": "8901111222233"}).status_code == 201
    assert api.call("POST", f"/admin/rx/items/{iid}/barcodes", rxc, {"barcode": "8901111222233"}).status_code == 409
    detail = api.call("GET", f"/admin/rx/items/{iid}", rxc).json()
    assert [b["symbology"] for b in detail["barcodes"]] == ["ean13"]
    assert {c["field"] for c in detail["changes"]} == {"schedule", "tax_rate_bps"}
    assert {c["changed_by_name"] for c in detail["changes"]} == {"admin"}
    assert detail["generic"]["name"] == "Azithromycin" and detail["manufacturer"]["name"] == "Cipla"
    # tenant isolation: another clinic sees none of it and cannot point at it
    other = _clinic(api.conn)
    assert api.owner_toggle(other, "pharmacy_enabled", True).status_code == 200
    api.as_(api.U("admin2", "clinic_admin", other, str(uuid.uuid4())))
    assert api.call("GET", f"/admin/rx/items/{iid}", other).status_code == 404
    assert api.call("PATCH", f"/admin/rx/items/{iid}", other, {"brand_name": "Leak"}).status_code == 404
    assert api.call("POST", f"/admin/rx/items/{iid}/barcodes", other, {"barcode": "12345678"}).status_code == 404
    assert api.call("POST", "/admin/rx/items", other, {**body, "item_code": "LEAK"}).status_code == 404   # their generic
    assert api.call("GET", "/admin/rx/items", other).json()["items"] == []
    assert api.call("GET", f"/admin/rx/items/{iid}", rxc).status_code == 403                    # not their clinic


def test_catalogue_closed_when_pharmacy_off_and_new_work_in_wind_down(api):  # noqa: F811
    clinic = _clinic(api.conn)
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    assert api.call("GET", "/admin/rx/items", clinic).status_code == 403
    assert api.call("POST", "/admin/rx/generics", clinic, {"name": "X1", "strength": "1", "dosage_form": "gel"}
                    ).status_code == 403
    api.owner_toggle(clinic, "pharmacy_enabled", True)
    # owner disabled with work open → WIND_DOWN: reads continue, nothing new is configured
    _q(api.conn, "UPDATE clinics SET rx_state = 'WIND_DOWN', features = features - 'pharmacy_enabled' WHERE id = %s",
       clinic)
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    assert api.call("GET", "/admin/rx/items", clinic).status_code == 200
    assert api.call("POST", "/admin/rx/generics", clinic, {"name": "Zinc", "strength": "20 mg", "dosage_form": "tablet"}
                    ).status_code == 409


def test_import_dry_run_apply_idempotent(api, rxc):  # noqa: F811
    dry = upload(api, "/admin/rx/items/import/dry-run", rxc, csv_bytes(ROWS))
    assert dry.status_code == 200, dry.text
    d = dry.json()
    assert (d["valid"], d["would_create"], d["would_update"], d["rows"]) == (True, 3, 0, 3)
    assert _q(api.conn, "SELECT count(*) FROM rx_items WHERE clinic_id = %s", rxc) == 0
    applied = upload(api, "/admin/rx/items/import/apply", rxc, csv_bytes(ROWS))
    assert applied.json() == {"created": 3, "updated": 0, "unchanged": 0}
    assert _q(api.conn, "SELECT count(*) FROM rx_generics WHERE clinic_id = %s", rxc) == 2      # Paracetamol 650 once
    assert _q(api.conn, "SELECT reorder_level_units FROM rx_items WHERE clinic_id = %s AND item_code = 'CALPOL650'",
              rxc) == 0
    again = upload(api, "/admin/rx/items/import/apply", rxc, csv_bytes(ROWS))
    assert again.json() == {"created": 0, "updated": 0, "unchanged": 3}
    changed = upload(api, "/admin/rx/items/import/apply", rxc, csv_bytes([ROWS[0].replace(",A-1,", ",A-9,"), *ROWS[1:]]))
    assert changed.json() == {"created": 0, "updated": 1, "unchanged": 2}


def test_import_row_errors_block_the_whole_file(api, rxc):  # noqa: F811
    upload(api, "/admin/rx/items/import/apply", rxc, csv_bytes(ROWS))
    bad = [ROWS[0].replace("DOLO650", "NEW1").replace("8901234560014", ""),        # fine on its own
           ROWS[1].replace(",12,,,", ",7,,,"),                                      # GST 7 % is not a slab
           ROWS[2].replace(",H,", ",none,"),                                        # schedule downgrade (database)
           ROWS[0].replace("DOLO650", "NEW2").replace(",Paracetamol,650 mg,tablet,", ",,,,"),   # drug without generic
           "NEW1,Dup,Paracetamol,650 mg,tablet,oral,,drug,none,tablet,15,strip,yes,3004,12,,,no,no,,"]
    dry = upload(api, "/admin/rx/items/import/dry-run", rxc, csv_bytes(bad)).json()
    assert dry["valid"] is False
    by_row = {(e["row"], e["field"]) for e in dry["errors"]}
    assert {(3, "gst_rate_percent"), (5, None), (6, "item_code")} <= by_row
    assert any(e["row"] == 4 and "schedule" in e["msg"] for e in dry["errors"])
    assert not any("violates" in e["msg"] or "rx_" in e["msg"] for e in dry["errors"])   # never raw DB text
    r = upload(api, "/admin/rx/items/import/apply", rxc, csv_bytes(bad))
    assert r.status_code == 422 and _code(r) == "rx_import_invalid"
    assert _q(api.conn, "SELECT count(*) FROM rx_items WHERE clinic_id = %s AND item_code = 'NEW1'", rxc) == 0
    assert upload(api, "/admin/rx/items/import/dry-run", rxc, b"").status_code == 422
    assert upload(api, "/admin/rx/items/import/dry-run", rxc, b"\xff\xfe\x00bad").status_code == 422


def test_import_xlsx(api, rxc):  # noqa: F811
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADER.split(","))
    ws.append(["ORS1", "Electral", None, None, None, None, "FDC", "otc", "none", "sachet", 1, "sachet", "yes", 30049099,
               5, 10, 50, "no", "no", None, None])
    buf = io.BytesIO()
    wb.save(buf)
    r = upload(api, "/admin/rx/items/import/apply", rxc, buf.getvalue(), "items.xlsx")
    assert r.status_code == 200, r.text
    assert _q(api.conn, "SELECT hsn_code || '/' || tax_rate_bps FROM rx_items WHERE clinic_id = %s", rxc) == "30049099/500"


def test_resolve_is_deterministic_and_substitutes_are_same_generic(api, rxc):  # noqa: F811
    upload(api, "/admin/rx/items/import/apply", rxc, csv_bytes(ROWS))
    store = str(_q(api.conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", rxc))
    opening = "item_code,batch_no,expiry_mm_yyyy,mrp_per_pack,purchase_rate_per_pack,gst_rate_percent,qty_units\n" \
              "CALPOL650,C1,12/2030,33.00,22.00,12,45\n"
    assert upload(api, "/admin/rx/stock/opening-import", rxc, opening.encode(), "o.csv", store_id=store,
                  dry_run="false").status_code == 200
    first = api.call("GET", "/admin/rx/items/resolve", rxc, q="dolo", store_id=store).json()
    assert [(c["item_code"], c["source"]) for c in first["candidates"]] == [("DOLO650", "brand")]
    assert [(s["item_code"], s["stock_units"]) for s in first["substitutes"]] == [("CALPOL650", 45)]   # never AUG625
    assert api.call("GET", "/admin/rx/items/resolve", rxc, q="dolo", store_id=store).json() == first
    assert api.call("GET", "/admin/rx/items/resolve", rxc, q="8901234560014").json()["candidates"][0]["source"] == "barcode"
    para = [c["item_code"] for c in api.call("GET", "/admin/rx/items/resolve", rxc, q="paracetamol 650").json()["candidates"]]
    assert para == ["CALPOL650", "DOLO650"]                                       # tie → brand name order
    assert api.call("GET", "/admin/rx/items/resolve", rxc, q="zzz").json() == {"candidates": [], "substitutes": []}
    aug = api.call("GET", "/admin/rx/items/resolve", rxc, q="augmentin").json()
    assert aug["substitutes"] == []                                               # only brand of its generic
