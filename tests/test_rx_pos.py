"""Pharmacy POS through the real /admin/rx routes on real Postgres (W2, W3, W5a; A1, A2, A4, A5, A9, A13, A14).

Every money and stock rule runs in SQL (rx_complete_sale / rx_record_return / rx_void_sale); these tests
drive them the way the counter does and check what the patient, the drawer and the ledger end up with.
"""

import uuid
from datetime import date, timedelta

import pytest

from app.routers.admin import hash_password
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m5 import signed_rx
from tests.test_rx_catalogue_api import ROWS, _code, csv_bytes, upload

GSTIN = "36AABCU9603R1ZO"
OPEN = "item_code,batch_no,expiry_mm_yyyy,mrp_per_pack,purchase_rate_per_pack,gst_rate_percent,qty_units\n"


def _mm(days):
    d = date.today() + timedelta(days=days)
    return f"{d.month:02d}/{d.year}"


@pytest.fixture
def pos(api):  # noqa: F811
    c = _clinic(api.conn)
    assert api.owner_toggle(c, "pharmacy_enabled", True).status_code == 200
    _q(api.conn, "UPDATE clinics SET opd_settings = COALESCE(opd_settings, '{}'::jsonb) || jsonb_build_object('gstin', %s) "
                 "WHERE id = %s", GSTIN, c)
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    admin = api.U("admin", "clinic_admin", c, str(uuid.uuid4()))
    api.as_(admin)
    assert upload(api, "/admin/rx/items/import/apply", c, csv_bytes(ROWS)).status_code == 200
    store = str(_q(api.conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", c))
    stock = OPEN + "\n".join([f"DOLO650,DNEAR,{_mm(200)},30.00,20.00,12,40", f"DOLO650,DLATE,{_mm(500)},30.00,20.00,12,100",
                              f"CALPOL650,CP1,{_mm(400)},33.00,22.00,12,60", f"AUG625,AG1,{_mm(300)},200.00,150.00,12,40"]) + "\n"
    assert upload(api, "/admin/rx/stock/opening-import", c, stock.encode(), "o.csv", store_id=store,
                  dry_run="false").status_code == 200
    items = dict(_q(api.conn, "SELECT json_agg(json_build_array(item_code, id)) FROM rx_items WHERE clinic_id = %s", c))
    pharmacist = api.U("pharma", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACIST")
    return {"clinic": c, "store": store, "items": items, "admin": admin, "pharmacist": pharmacist}


def _rx(api, pos, lines=(("Dolo 650", "tablet", "1", "TDS", 5),), token=None):  # noqa: F811
    return signed_rx(api.conn, pos["clinic"], lines, token=token)


def _new(api, pos, **body):  # noqa: F811
    r = api.call("POST", "/admin/rx/sales", pos["clinic"], {"store_id": pos["store"], **body})
    assert r.status_code == 201, r.text
    return r.json()


def _pay(api, pos, sale, payments, key=None, **extra):  # noqa: F811
    return api.call("POST", f"/admin/rx/sales/{sale['id']}/complete", pos["clinic"],
                    {"payments": payments, "idempotency_key": key or "k-" + uuid.uuid4().hex, **extra})


def test_signed_rx_loads_and_completes(api, pos):  # noqa: F811
    c = pos["clinic"]
    rx = _rx(api, pos, token=17)
    _q(api.conn, "UPDATE patients SET mrn = 'MRN-4211' WHERE id = %s", rx["patient"])
    api.as_(pos["pharmacist"])
    for q in ("17", "MRN-4211", rx["phone"][-10:], rx["rx"]):          # token, MRN, phone, printed-Rx QR
        found = api.call("GET", "/admin/rx/pos/lookup", c, q=q).json()
        assert [p["id"] for p in found["prescriptions"]] == [rx["rx"]], q
    line = found["prescriptions"][0]["lines"][0]
    assert (line["prescribed_units"], line["fulfilled_units"], line["pending"]) == (15, 0, 15)
    sale = _new(api, pos, sale_kind="opd_rx", opd_prescription_id=rx["rx"])
    assert sale["mapping"][0]["mapped_item_id"] == pos["items"]["DOLO650"]
    assert [(ln["batch"]["batch_no"], ln["qty_units"]) for ln in sale["lines"]] == [("DNEAR", 15)]   # FEFO
    assert sale["gross_paise"] == 3000 and sale["net_paise"] == 3000 and sale["is_tax_invoice"] is True   # 15 × ₹30/15
    done = _pay(api, pos, sale, [{"mode": "cash", "amount_paise": 1000}, {"mode": "upi", "amount_paise": 2000}])
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["sale_number"] == f"PHS-{date.today().year}-000001" and body["status"] == "completed"
    assert {p["mode"]: p["amount_paise"] for p in body["payments"]} == {"cash": 1000, "upi": 2000}
    assert all(p["shift_id"] for p in body["payments"] if p["mode"] == "cash")       # into the cashier shift
    after = api.call("GET", "/admin/rx/pos/lookup", c, q=rx["rx"]).json()["prescriptions"][0]
    assert (after["state"], after["lines"][0]["pending"]) == ("billed", 0)
    html = api.client.get(body["receipt_url"], params={"clinic_id": c})
    assert html.status_code == 200 and "TAX INVOICE" in html.text and body["sale_number"] in html.text
    assert "Dr. Meena · Reg TSMC/2012/777" in html.text and "Dr Dr" not in html.text and rx["phone"] not in html.text
    a5 = api.client.get(f"/admin/rx/sales/{body['id']}/receipt", params={"clinic_id": c, "format": "a5"})
    labels = api.client.get(f"/admin/rx/sales/{body['id']}/labels.pdf", params={"clinic_id": c})
    assert a5.content[:4] == b"%PDF" and labels.content[:4] == b"%PDF"


def test_complete_is_idempotent_and_second_counter_gets_same_draft(api, pos):  # noqa: F811
    rx = _rx(api, pos)
    api.as_(pos["pharmacist"])
    first = _new(api, pos, sale_kind="opd_rx", opd_prescription_id=rx["rx"])
    second = _new(api, pos, sale_kind="opd_rx", opd_prescription_id=rx["rx"])
    assert second["id"] == first["id"] and second["existing_draft"] is True
    r1 = _pay(api, pos, first, [{"mode": "upi", "amount_paise": 3000}], key="idem-0000-0001")
    r2 = _pay(api, pos, first, [{"mode": "upi", "amount_paise": 3000}], key="idem-0000-0001")
    assert r1.status_code == r2.status_code == 200 and r2.json()["replayed"] is True
    assert _q(api.conn, "SELECT count(*) FROM rx_sale_payments WHERE sale_id = %s", first["id"]) == 1


def test_superseded_rx_refused(api, pos):  # noqa: F811
    rx = _rx(api, pos)
    api.as_(pos["pharmacist"])
    sale = _new(api, pos, sale_kind="opd_rx", opd_prescription_id=rx["rx"])
    _q(api.conn, "UPDATE opd_prescriptions SET status = 'superseded', superseded_at = now() WHERE id = %s", rx["rx"])
    r = _pay(api, pos, sale, [{"mode": "upi", "amount_paise": 3000}])
    assert r.status_code == 409 and _code(r) == "rx_prescription_superseded"
    assert _q(api.conn, "SELECT count(*) FROM rx_stock_movements WHERE ref_table = 'rx_sale_items' AND clinic_id = %s",
              pos["clinic"]) == 0


def test_quantity_from_closed_frequency_map_only(api, pos):  # noqa: F811
    rx = _rx(api, pos, (("Dolo 650", "tablet", "1", "SOS", 5), ("Augmentin 625 Duo", "tablet", "1", "BD", 5),
                        ("Pan D", "capsule", "1", "OD", 10)))
    api.as_(pos["pharmacist"])
    sale = _new(api, pos, sale_kind="opd_rx", opd_prescription_id=rx["rx"])
    units = {ln["item"]["item_code"]: ln["qty_units"] for ln in sale["lines"]}
    assert units == {"DOLO650": 15, "AUG625": 10}                        # SOS → one pack (15); BD × 5 → 10
    unmapped = [m for m in sale["mapping"] if not m["mapped_item_id"]]
    assert [m["text"] for m in unmapped] == ["Pan D"]                   # never guessed: the pharmacist maps it
    assert any(w["code"] == "needs_rx" for w in sale["warnings"]) is False   # AUG625 is H; the in-house Rx satisfies it


def test_otc_blocks_h_without_external_rx_and_x_always_blocked(api, pos):  # noqa: F811
    c = pos["clinic"]
    api.as_(pos["pharmacist"])
    otc = _new(api, pos, sale_kind="otc", walkin_name="Ramesh")
    r = api.call("PUT", f"/admin/rx/sales/{otc['id']}/lines", c, [{"item_id": pos["items"]["AUG625"], "qty_units": 10}])
    assert any(w["code"] == "needs_rx" for w in r.json()["warnings"])
    assert _code(_pay(api, pos, otc, [{"mode": "upi", "amount_paise": 20000}])) == "rx_schedule_needs_rx"
    ext = _new(api, pos, sale_kind="otc", walkin_name="Ramesh", external_rx={
        "prescriber_name": "Dr. Outside", "registration_number": "KMC/99/1", "rx_date": str(date.today())})
    api.call("PUT", f"/admin/rx/sales/{ext['id']}/lines", c, [{"item_id": pos["items"]["AUG625"], "qty_units": 10}])
    assert _pay(api, pos, ext, [{"mode": "upi", "amount_paise": 20000}]).status_code == 200
    _q(api.conn, "SELECT 1; UPDATE rx_items SET schedule = 'X' WHERE id = %s", pos["items"]["AUG625"])
    x = _new(api, pos, sale_kind="otc", walkin_name="Ramesh")
    api.call("PUT", f"/admin/rx/sales/{x['id']}/lines", c, [{"item_id": pos["items"]["AUG625"], "qty_units": 1}])
    assert _code(_pay(api, pos, x, [{"mode": "upi", "amount_paise": 2000}])) == "rx_schedule_needs_rx"
    api.as_(pos["admin"])                                                 # even a manager: no Rx, no X
    assert _code(_pay(api, pos, x, [{"mode": "upi", "amount_paise": 2000}])) == "rx_schedule_needs_rx"


def test_walkin_sale_without_patient_substitution_and_fefo_reason(api, pos):  # noqa: F811
    c, it = pos["clinic"], pos["items"]
    api.as_(pos["pharmacist"])
    sale = _new(api, pos, sale_kind="otc")
    dlate = str(_q(api.conn, "SELECT id FROM rx_batches WHERE batch_no = 'DLATE' AND clinic_id = %s", c))
    r = api.call("PUT", f"/admin/rx/sales/{sale['id']}/lines", c, [{"item_id": it["DOLO650"], "batch_id": dlate, "qty_units": 5}])
    assert r.status_code == 422 and _code(r) == "rx_fefo_reason_required"
    r = api.call("PUT", f"/admin/rx/sales/{sale['id']}/lines", c, [
        {"item_id": it["DOLO650"], "batch_id": dlate, "qty_units": 5, "fefo_override_reason": "Patient asked longer expiry"},
        {"item_id": it["CALPOL650"], "qty_units": 3, "substituted_from_item_id": it["DOLO650"],
         "substitution_reason": "Patient request (cost)"}])
    assert r.status_code == 200, r.text
    bad = api.call("PUT", f"/admin/rx/sales/{sale['id']}/lines", c, [
        {"item_id": it["AUG625"], "qty_units": 1, "substituted_from_item_id": it["DOLO650"], "substitution_reason": "x stock"}])
    assert bad.status_code == 422 and _code(bad) == "rx_substitute_not_same_generic"
    short = api.call("PUT", f"/admin/rx/sales/{sale['id']}/lines", c, [{"item_id": it["CALPOL650"], "qty_units": 61}])
    assert short.status_code == 409 and _code(short) == "rx_insufficient_stock"
    assert _pay(api, pos, sale, [{"mode": "card", "amount_paise": 1700}]).status_code == 200   # 1000 + 660, rounded to ₹17
    gone = api.call("DELETE", f"/admin/rx/sales/{sale['id']}", c)
    assert gone.status_code == 409


def test_discount_override_single_use_bound(api, pos):  # noqa: F811
    c = pos["clinic"]
    mgr_name = "mgr_" + uuid.uuid4().hex[:6]
    _q(api.conn, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, is_active) "
                 "VALUES (%s, %s, %s, 'staff', 'PHARMACY_MANAGER', true)", c, mgr_name, hash_password("Mgr#pass123"))
    clerk_name = "clerk_" + uuid.uuid4().hex[:6]
    _q(api.conn, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, is_active) "
                 "VALUES (%s, %s, %s, 'staff', 'PHARMACIST', true)", c, clerk_name, hash_password("Clerk#pass1"))
    api.as_(pos["pharmacist"])
    sale = _new(api, pos, sale_kind="otc")
    api.call("PUT", f"/admin/rx/sales/{sale['id']}/lines", c, [{"item_id": pos["items"]["DOLO650"], "qty_units": 100}])
    p = api.call("PATCH", f"/admin/rx/sales/{sale['id']}", c, {"discount_paise": 3000}).json()   # 15 % of ₹200
    assert p["discount_needs_override"] is True and p["net_paise"] == 17000
    assert _code(_pay(api, pos, sale, [{"mode": "upi", "amount_paise": 17000}])) == "rx_discount_over_limit"
    ask = {"action": "discount_over_cap", "sale_id": sale["id"], "amount_paise": 3000, "reason": "Senior citizen"}
    wrong = api.call("POST", "/admin/rx/overrides", c, {**ask, "approver_username": clerk_name, "approver_password": "Clerk#pass1"})
    assert wrong.status_code == 403 and _code(wrong) == "rx_override_not_authorised"
    bad_pw = api.call("POST", "/admin/rx/overrides", c, {**ask, "approver_username": mgr_name, "approver_password": "nope"})
    assert bad_pw.status_code == 401
    ok = api.call("POST", "/admin/rx/overrides", c, {**ask, "approver_username": mgr_name, "approver_password": "Mgr#pass123"})
    assert ok.status_code == 201, ok.text
    token = ok.json()["override_id"]
    done = _pay(api, pos, sale, [{"mode": "upi", "amount_paise": 17000}], override_id=token)
    assert done.status_code == 200, done.text
    s2 = _new(api, pos, sale_kind="otc")
    api.call("PUT", f"/admin/rx/sales/{s2['id']}/lines", c, [{"item_id": pos["items"]["DOLO650"], "qty_units": 10}])
    api.call("PATCH", f"/admin/rx/sales/{s2['id']}", c, {"discount_paise": 500})
    assert _code(_pay(api, pos, s2, [{"mode": "upi", "amount_paise": 1500}], override_id=token)) == "rx_discount_over_limit"


def test_returns_partial_restock_writeoff_window_and_credit_note(api, pos):  # noqa: F811
    c, it = pos["clinic"], pos["items"]
    api.as_(pos["pharmacist"])
    sale = _new(api, pos, sale_kind="otc", walkin_name="Asha")
    api.call("PUT", f"/admin/rx/sales/{sale['id']}/lines", c, [{"item_id": it["CALPOL650"], "qty_units": 30}])   # 6600
    done = _pay(api, pos, sale, [{"mode": "cash", "amount_paise": 6600}]).json()
    li = done["lines"][0]["id"]
    r1 = api.call("POST", f"/admin/rx/sales/{sale['id']}/returns", c, {"lines": [{"sale_item_id": li, "qty_units": 10,
                  "restock": True}], "reason": "unopened_unused", "refund_mode": "cash", "idempotency_key": "ret-000001"})
    assert r1.status_code == 201, r1.text
    assert r1.json()["amount_paise"] == 2200 and r1.json()["return_number"].startswith("PRT-")
    over = api.call("POST", f"/admin/rx/sales/{sale['id']}/returns", c, {"lines": [{"sale_item_id": li, "qty_units": 21,
                    "restock": True}], "reason": "other", "refund_mode": "cash"})
    assert over.status_code == 409 and _code(over) == "rx_return_exceeds_sold"
    cn = api.call("POST", f"/admin/rx/sales/{sale['id']}/returns", c, {"lines": [{"sale_item_id": li, "qty_units": 5,
                  "restock": False}], "reason": "therapy_changed", "refund_mode": "credit_note"}).json()
    pdf = api.client.get(f"/admin/rx/credit-notes/{cn['credit_note_id']}.pdf", params={"clinic_id": c})
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    stock = {r["item"]["item_code"]: r["qty_units"] for r in api.call("GET", "/admin/rx/stock", c).json()["stock"]}
    assert stock["CALPOL650"] == 60 - 30 + 10                            # 5 opened units were written off
    detail = api.call("GET", f"/admin/rx/sales/{sale['id']}", c).json()
    assert (detail["refunded_paise"], detail["credited_paise"], len(detail["returns"])) == (2200, 1100, 2)
    _q(api.conn, "SELECT set_config('kriya.rx_tx', %s, true); UPDATE rx_sales SET completed_at = now() - interval '9 days' "
                 "WHERE id = %s", c, sale["id"])
    late = api.call("POST", f"/admin/rx/sales/{sale['id']}/returns", c, {"lines": [{"sale_item_id": li, "qty_units": 1,
                    "restock": True}], "reason": "other", "refund_mode": "credit_note"})
    assert late.status_code == 409 and _code(late) == "rx_return_window_closed"
    assert api.as_(pos["admin"]).call("POST", f"/admin/rx/sales/{sale['id']}/returns", c, {
        "lines": [{"sale_item_id": li, "qty_units": 1, "restock": True}], "reason": "other",
        "refund_mode": "credit_note"}).status_code == 201


def test_void_admin_same_day_and_z_report_matches_close_shift(api, pos):  # noqa: F811
    c = pos["clinic"]
    api.as_(pos["pharmacist"])
    sale = _new(api, pos, sale_kind="otc")
    api.call("PUT", f"/admin/rx/sales/{sale['id']}/lines", c, [{"item_id": pos["items"]["DOLO650"], "qty_units": 10}])
    done = _pay(api, pos, sale, [{"mode": "cash", "amount_paise": 2000}]).json()
    shift = done["payments"][0]["shift_id"]
    assert api.call("POST", f"/admin/rx/sales/{sale['id']}/void", c, {"reason": "Wrong patient"}).status_code == 403
    s2 = _new(api, pos, sale_kind="otc")
    api.call("PUT", f"/admin/rx/sales/{s2['id']}/lines", c, [{"item_id": pos["items"]["DOLO650"], "qty_units": 5}])
    _pay(api, pos, s2, [{"mode": "cash", "amount_paise": 1000}])
    z = api.call("GET", "/admin/rx/pos/day-summary", c, shift_id=shift).json()
    assert (z["sales_count"], z["net_paise"], z["by_mode"]["cash"]) == (2, 3000, 3000)
    assert z["by_gst_slab"]["12%"]["taxable_paise"] + z["by_gst_slab"]["12%"]["cgst_paise"] * 2 in (3000, 2999, 3001)
    expected = z["cash_expected_paise"]
    pharmacist_id = pos["pharmacist"].user_id
    closed = _q(api.conn, "SELECT expected_cash_paise FROM opd_close_shift(%s, %s, %s, 0, NULL)", c, shift, pharmacist_id)
    assert closed == expected == 3000
    summary = api.as_(pos["admin"]).call("GET", "/admin/opd/collections/summary", c)
    assert summary.status_code == 200, summary.text                      # RX-34 additive keys
    assert (summary.json()["pharmacy_net_paise"], summary.json()["pharmacy_cash_paise"]) == (3000, 3000)
    plain = _clinic(api.conn)                                             # a clinic without pharmacy: keys absent
    opd = api.as_(api.U("a2", "clinic_admin", plain, str(uuid.uuid4()))).call("GET", "/admin/opd/collections/summary", plain)
    assert opd.status_code == 200 and not any(k.startswith("pharmacy_") for k in opd.json())


def test_park_recall_and_stale_list(api, pos):  # noqa: F811
    c = pos["clinic"]
    api.as_(pos["pharmacist"])
    sale = _new(api, pos, sale_kind="otc", walkin_name="Lakshmi")
    parked = api.call("POST", f"/admin/rx/sales/{sale['id']}/park", c, {"label": "Lakshmi — waiting for son"}).json()
    assert parked["parked_label"] == "Lakshmi — waiting for son"
    listed = api.call("GET", "/admin/rx/sales", c, status="draft", parked="true").json()["sales"]
    assert [s["id"] for s in listed] == [sale["id"]] and listed[0]["stale"] is False
    _q(api.conn, "UPDATE rx_sales SET created_at = now() - interval '2 days' WHERE id = %s", sale["id"])
    assert api.call("GET", "/admin/rx/sales", c, status="draft").json()["sales"][0]["stale"] is True
    back = api.call("POST", f"/admin/rx/sales/{sale['id']}/recall", c).json()
    assert back["parked_label"] is None
    assert api.call("GET", "/admin/rx/sales", c, status="draft", parked="true").json()["sales"] == []


def test_inbox_lists_signed_today_excludes_superseded_and_pick_creates_single_draft(api, pos):  # noqa: F811
    c = pos["clinic"]
    a, b = _rx(api, pos), _rx(api, pos)
    _q(api.conn, "UPDATE opd_prescriptions SET status = 'superseded', superseded_at = now() WHERE id = %s", b["rx"])
    api.as_(pos["pharmacist"])
    inbox = api.call("GET", "/admin/rx/pos/inbox", c, store_id=pos["store"]).json()["prescriptions"]
    assert [(r["id"], r["state"]) for r in inbox] == [(a["rx"], "new")]
    picked = _new(api, pos, sale_kind="opd_rx", opd_prescription_id=a["rx"], pick=True)
    assert picked["picked_at"] and _new(api, pos, sale_kind="opd_rx", opd_prescription_id=a["rx"])["id"] == picked["id"]
    inbox = api.call("GET", "/admin/rx/pos/inbox", c, store_id=pos["store"]).json()["prescriptions"]
    assert (inbox[0]["state"], inbox[0]["draft_sale_id"]) == ("picked", picked["id"])


def test_repeat_bill_fresh_fefo_excludes_scheduled_and_history_panel(api, pos):  # noqa: F811
    c, it = pos["clinic"], pos["items"]
    rx = _rx(api, pos)
    _q(api.conn, "UPDATE patients SET allergies = ARRAY['Penicillin'], allergies_status = 'recorded' WHERE id = %s",
       rx["patient"])
    api.as_(pos["pharmacist"])
    first = _new(api, pos, sale_kind="otc", patient_id=rx["patient"], external_rx={
        "prescriber_name": "Dr. Outside", "registration_number": "KMC/99/1", "rx_date": str(date.today())})
    api.call("PUT", f"/admin/rx/sales/{first['id']}/lines", c, [{"item_id": it["DOLO650"], "qty_units": 45},
                                                                {"item_id": it["AUG625"], "qty_units": 10}])
    _pay(api, pos, first, [{"mode": "upi", "amount_paise": 9000 + 20000}])
    again = _new(api, pos, sale_kind="otc", patient_id=rx["patient"], repeat_of_sale_id=first["id"])
    assert [(ln["item"]["item_code"], ln["batch"]["batch_no"], ln["qty_units"]) for ln in again["lines"]] == \
        [("DOLO650", "DLATE", 45)]                                       # DNEAR has none left: fresh FEFO
    assert {m["brand_name"]: m["status"] for m in again["mapping"]} == {"Dolo 650": "added", "Augmentin 625 Duo": "needs_rx"}
    panel = again["patient_panel"]
    assert panel["allergies"][0] == {"text": "Penicillin", "source": "patient record"}
    assert panel["history"][0]["items"][0]["brand_name"] in ("Dolo 650", "Augmentin 625 Duo")
    assert "does not check" in panel["note"]
