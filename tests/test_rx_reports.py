"""GSTR-1 pharmacy section and the management / statutory CSV reports through the real routes on real Postgres
(P10; RX-52, RX-53).

A GST-registered clinic sells at 12 % (two stores), 5 % to a buyer with a GSTIN, 0 % (nil), voids one bill, issues a
PCN- credit note on a return and posts one GRN. GSTR-1 folds the pharmacy in only when Pharmacy OS is on; a clinic
without it reads no rx_* table. Every CSV cell is formula-safe, no report carries a phone, margin is RX_ADMIN only and
every download is audited.
"""

import csv
import io
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.rx_reports import _csv_safe, expiry_bucket
from tests.test_ipd_api_e2e import _clinic, _patient, _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m1 import _store
from tests.test_migration_rx_m2 import _generic, _item
from tests.test_migration_rx_m3 import _batch, _move
from tests.test_migration_rx_m5 import complete, draft, line

GSTIN, BUYER = "36AABCU9603R1ZO", "29AAACR5055K1ZK"
TODAY = date.today()


def _sell(conn, w, item, qty, store=None, walkin="Asha K", buyer=None, patient=None):
    ww = {"clinic": w["clinic"], "store": store or w["store"], "item": w["items"][item], "batch": w["batches"][item]}
    s = draft(conn, ww, "otc", patient=patient, walkin=walkin)
    if buyer:
        _q(conn, "UPDATE rx_sales SET buyer_gstin = %s WHERE id = %s", buyer, s)
    ln = line(conn, ww, s, qty)
    complete(conn, ww, s, [{"mode": "upi", "amount_paise": _q(conn, "SELECT net_paise FROM rx_sales WHERE id = %s", s)}])
    return s, ln


@pytest.fixture
def rxw(api, monkeypatch):  # noqa: F811
    conn, c = api.conn, _clinic(api.conn)
    assert api.owner_toggle(c, "pharmacy_enabled", True).status_code == 200
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    audits = []

    async def _log(**kw):
        audits.append(kw)
    monkeypatch.setattr("app.routers.rx.log_admin_action", _log)
    _q(conn, "UPDATE clinics SET opd_settings = COALESCE(opd_settings, '{}'::jsonb) || jsonb_build_object('gstin', %s::text) "
             "WHERE id = %s", GSTIN, c)
    store = str(_q(conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", c))
    _q(conn, "UPDATE rx_stores SET can_receive_grn = true, can_sell = true WHERE id = %s", store)
    store2 = _store(conn, c, "S2")
    gen = _generic(conn, c)
    items = {"i12": _item(conn, c, gen, "PCM650", "=SUM(A1) Dolo"),
             "i5": _item(conn, c, gen, "AMX500", "Amoxy 500", hsn_code="30042019", tax_rate_bps=500),
             "i0": _item(conn, c, gen, "ORS01", "ORS Sachet", hsn_code="30039011", tax_rate_bps=0)}
    batches = {k: _batch(conn, c, items[k], "B" + k.upper(), mrp=m)
               for k, m in (("i12", 11200), ("i5", 10500), ("i0", 10000))}   # ₹3 landed per unit (30000 x100)
    for k in items:
        _move(conn, c, store, batches[k], items[k], 100, "opening")
    _move(conn, c, store2, batches["i12"], items["i12"], 50, "opening")
    soon = _batch(conn, c, items["i0"], "SOON", expiry=TODAY + timedelta(days=10), mrp=10000)
    gone = _batch(conn, c, items["i0"], "GONE", expiry=TODAY - timedelta(days=3), mrp=10000)
    _move(conn, c, store, soon, items["i0"], 4, "opening")
    _move(conn, c, store, gone, items["i0"], 2, "opening")
    w = {"clinic": c, "store": store, "store2": store2, "items": items, "batches": batches, "audits": audits}
    patient = _patient(conn, c)
    w["phone"] = _q(conn, "SELECT phone FROM patients WHERE id = %s", patient)
    w["s1"], s1_line = _sell(conn, w, "i12", 10, walkin="+cmd|calc")
    w["s2"], _ = _sell(conn, w, "i5", 10, walkin="Acme Traders", buyer=BUYER)
    w["s3"], _ = _sell(conn, w, "i0", 10, walkin=None, patient=patient)
    w["s4"], _ = _sell(conn, w, "i12", 10, store=store2)
    w["s5"], _ = _sell(conn, w, "i12", 10)
    _q(conn, "SELECT rx_void_sale(%s, %s, 'Wrong patient', NULL, NULL, 'M')", c, w["s5"])
    w["ret"] = _q(conn, "SELECT rx_record_return(%s, %s, %s::jsonb, 'unopened_unused', 'credit_note', NULL, NULL, NULL, "
                        "'Ravi', true, NULL)", c, w["s1"], json.dumps([{"sale_item_id": s1_line, "qty_units": 2,
                                                                        "restock": True}]))
    sup = str(_q(conn, "INSERT INTO rx_suppliers (clinic_id, name, lead_time_days) VALUES (%s, '@Pharma Distributors', 5) "
                       "RETURNING id", c))
    grn = str(_q(conn, "INSERT INTO rx_grns (clinic_id, store_id, supplier_id, supplier_invoice_no, supplier_invoice_date) "
                       "VALUES (%s, %s, %s, '-INV9', CURRENT_DATE) RETURNING id", c, store, sup))
    _q(conn, "INSERT INTO rx_grn_lines (clinic_id, grn_id, line_no, item_id, batch_no, expiry_date, packs, rate_pack_paise, "
             "tax_rate_bps, mrp_pack_paise) VALUES (%s, %s, 1, %s, 'G1', %s, 10, 5000, 1200, 11200) RETURNING id",
       c, grn, items["i12"], TODAY + timedelta(days=500))
    _q(conn, "UPDATE rx_grns SET invoice_total_paise = taxable_paise + cgst_paise + sgst_paise + igst_paise WHERE id = %s",
       grn)
    _q(conn, "SELECT rx_post_grn(%s, %s, NULL, 'Ravi')", c, grn)
    w["grn"] = _q(conn, "SELECT row_to_json(g) FROM rx_grns g WHERE id = %s", grn)
    w["users"] = {"mgr": api.U("mgr", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACY_MANAGER"),
                  "pharma": api.U("pharma", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACIST"),
                  "reports": api.U("acct", "staff", c, str(uuid.uuid4()), ["RX_REPORTS"], staff_role="FRONT_DESK"),
                  "admin": api.U("admin", "clinic_admin", c, str(uuid.uuid4()))}
    return w


def _csv(api, w, kind, user="mgr", **params):  # noqa: F811
    api.as_(w["users"][user])
    r = api.call("GET", f"/admin/rx/reports/{kind}.csv", w["clinic"], **params)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    return r.content.decode("utf-8-sig")


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def _money(rows, col):
    return sum(Decimal(r[col]) for r in rows)


# ── pure ────────────────────────────────────────────────────────────────────

def test_csv_formula_injection_guard():
    for bad in ("=SUM(A1)", "+cmd", "-1", "@foo", "\tx", "\rx"):
        assert _csv_safe(bad) == "'" + bad
    assert _csv_safe("Dolo 650") == "Dolo 650" and _csv_safe(None) == ""
    assert _csv_safe(-5) == -5 and _csv_safe(Decimal("-0.05")) == Decimal("-0.05")    # numbers stay numbers
    assert [expiry_bucket(d) for d in (-1, 0, 30, 31, 60, 61, 90, 91)] == [
        "Expired", "≤30d", "≤30d", "≤60d", "≤60d", "≤90d", "≤90d", None]


# ── GSTR-1 (RX-52) ──────────────────────────────────────────────────────────

def test_gstr1_without_pharmacy_reads_no_rx_table_and_is_unchanged(api, monkeypatch):  # noqa: F811
    c = _clinic(api.conn)
    tables = []
    import app.services.gst_billing as g
    real = g.scoped_query
    monkeypatch.setattr(g, "scoped_query", lambda t, *a, **k: tables.append(t) or real(t, *a, **k))

    async def _boom(*a, **k):
        raise AssertionError("pharmacy section read for a clinic without Pharmacy OS")
    monkeypatch.setattr("app.services.rx_reports.pharmacy_gstr1", _boom)
    api.as_(api.U("admin", "clinic_admin", c, str(uuid.uuid4())))
    r = api.call("GET", "/admin/billing/gstr1", c, month=TODAY.month, year=TODAY.year)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "period": f"{TODAY.year:04d}-{TODAY.month:02d}", "clinic_gstin": None, "b2b": [], "b2cs": [],
        "nil_exempt_paise": 0, "credit_notes": [],
        "doc_summary": {"invoices": {"from": None, "to": None, "total": 0, "cancelled": 0},
                        "credit_notes": {"from": None, "to": None, "total": 0, "cancelled": 0}},
        "totals": {"invoice_value_paise": 0, "tax_paise": 0, "credit_value_paise": 0, "credit_tax_paise": 0}}
    assert tables and not [t for t in tables if t.startswith("rx_")]


def test_gstr1_folds_in_pharmacy_sales_credit_notes_and_hsn(api, rxw):  # noqa: F811
    w, conn = rxw, api.conn
    api.as_(w["users"]["admin"])
    g = api.call("GET", "/admin/billing/gstr1", w["clinic"], month=TODAY.month, year=TODAY.year).json()
    s2 = _q(conn, "SELECT sale_number FROM rx_sales WHERE id = %s", w["s2"])
    assert g["b2b"] == [{"invoice_number": s2, "issued_at": g["b2b"][0]["issued_at"], "buyer_gstin": BUYER,
                         "buyer_name": "Acme Traders", "invoice_value_paise": 10500,
                         "rates": [{"rate_bps": 500, "taxable_paise": 10000, "cgst_paise": 250, "sgst_paise": 250}]}]
    assert g["b2cs"] == [{"rate_bps": 1200, "taxable_paise": 20000, "cgst_paise": 1200, "sgst_paise": 1200}]  # void out
    assert g["nil_exempt_paise"] == 10000 and g["clinic_gstin"] == GSTIN
    cn = _q(conn, "SELECT row_to_json(n) FROM rx_credit_notes n WHERE sale_id = %s", w["s1"])
    assert cn["cn_number"].startswith("PCN-")
    [pcn] = g["credit_notes"]
    tax = cn["tax_paise"]
    assert (pcn["credit_note_number"], pcn["taxable_paise"], pcn["cgst_paise"], pcn["sgst_paise"], pcn["total_paise"]) == (
        cn["cn_number"], cn["amount_paise"] - tax, tax // 2, tax - tax // 2, cn["amount_paise"])
    assert pcn["invoice_number"] == _q(conn, "SELECT sale_number FROM rx_sales WHERE id = %s", w["s1"])
    assert g["hsn_summary"] == [
        {"hsn_code": "30039011", "tax_rate_bps": 0, "total_qty_units": 10, "total_value_paise": 10000,
         "taxable_paise": 10000, "cgst_paise": 0, "sgst_paise": 0},
        {"hsn_code": "30042019", "tax_rate_bps": 500, "total_qty_units": 10, "total_value_paise": 10500,
         "taxable_paise": 10000, "cgst_paise": 250, "sgst_paise": 250},
        {"hsn_code": "30049099", "tax_rate_bps": 1200, "total_qty_units": 20, "total_value_paise": 22400,
         "taxable_paise": 20000, "cgst_paise": 1200, "sgst_paise": 1200}]
    ps = g["doc_summary"]["pharmacy_sales"]
    assert (ps["total"], ps["cancelled"]) == (5, 1) and ps["from"].startswith("PHS-")
    assert g["doc_summary"]["pharmacy_credit_notes"] == {"from": cn["cn_number"], "to": cn["cn_number"], "total": 1,
                                                         "cancelled": 0}
    assert g["doc_summary"]["invoices"]["total"] == 0                       # OPD series still there
    assert g["totals"] == {"invoice_value_paise": 11200 + 10500 + 10000 + 11200, "tax_paise": 1200 + 1200 + 500,
                           "credit_value_paise": cn["amount_paise"], "credit_tax_paise": tax}
    api.as_(w["users"]["mgr"])
    ph = api.call("GET", "/admin/rx/reports/gstr1", w["clinic"], month=TODAY.month, year=TODAY.year)
    assert ph.status_code == 200 and ph.json()["b2b"] == g["b2b"] and ph.json()["hsn_summary"] == g["hsn_summary"]
    api.as_(w["users"]["pharma"])
    assert api.call("GET", "/admin/rx/reports/gstr1", w["clinic"], month=TODAY.month, year=TODAY.year).status_code == 403


# ── CSV reports (RX-53) ─────────────────────────────────────────────────────

def test_all_report_kinds_totals_filters_audit_and_no_phone(api, rxw):  # noqa: F811
    w, conn = rxw, api.conn
    texts = {}

    sales = _rows(texts.setdefault("sales_register", _csv(api, w, "sales_register")))
    assert sorted(r["status"] for r in sales) == ["completed"] * 4 + ["void"]
    live = [r for r in sales if r["status"] == "completed"]
    assert _money(live, "net_inr") == Decimal("429.00") and _money(live, "taxable_inr") == Decimal("400.00")
    assert {r["patient"] for r in sales} >= {"'+cmd|calc", "Lakshmi Devi", "Acme Traders"}
    assert {r["payment_modes"] for r in live} == {"upi"}
    assert len(_rows(_csv(api, w, "sales_register", store_id=w["store2"]))) == 1
    yday = str(TODAY - timedelta(days=1))
    assert _rows(_csv(api, w, "sales_register", **{"from": yday, "to": yday})) == []

    [p] = _rows(texts.setdefault("purchase_register", _csv(api, w, "purchase_register")))
    g = w["grn"]
    assert (p["grn_number"], p["supplier_name"], p["invoice_no"], p["is_interstate"]) == (
        g["grn_number"], "'@Pharma Distributors", "'-INV9", "no")
    assert Decimal(p["invoice_total_inr"]) == Decimal(g["invoice_total_paise"]) / 100

    gst = {r["gst_slab"]: r for r in _rows(texts.setdefault("gst_summary", _csv(api, w, "gst_summary")))}
    assert list(gst) == ["0%", "5%", "12%", "18%", "28%", "40%", "Total"]
    assert (gst["12%"]["sales_taxable_inr"], gst["12%"]["sales_cgst_inr"], gst["5%"]["sales_sgst_inr"]) == (
        "200.00", "12.00", "2.50")
    ptax = Decimal(g["cgst_paise"] + g["sgst_paise"]) / 100
    assert Decimal(gst["12%"]["purchase_cgst_inr"]) + Decimal(gst["12%"]["purchase_sgst_inr"]) == ptax
    assert Decimal(gst["Total"]["net_gst_payable_inr"]) == Decimal("29.00") - ptax

    hsn = _rows(texts.setdefault("hsn_summary", _csv(api, w, "hsn_summary")))
    assert [(r["hsn_code"], r["rate_bps"], r["total_units"], r["total_value_inr"]) for r in hsn] == [
        ("30039011", "0", "10", "100.00"), ("30042019", "500", "10", "105.00"), ("30049099", "1200", "20", "224.00")]

    val = _rows(texts.setdefault("stock_valuation", _csv(api, w, "stock_valuation")))
    ors = next(r for r in val if r["item_code"] == "ORS01" and r["batch_no"] == "BI0")
    assert (ors["qty_units"], ors["valuation_at_cost_inr"], ors["valuation_at_mrp_inr"]) == ("90", "270.00", "900.00")
    assert {r["brand_name"] for r in val if r["item_code"] == "PCM650"} == {"'=SUM(A1) Dolo"}
    assert _money(val, "valuation_at_cost_inr") == Decimal(
        _q(conn, "SELECT SUM(qty_units * landed_unit_cost_paise_x100 / 100) FROM rx_v_batch_stock WHERE clinic_id = %s",
           w["clinic"])) / 100

    mv = _rows(texts.setdefault("movement", _csv(api, w, "movement")))
    assert len(mv) == _q(conn, "SELECT count(*) FROM rx_stock_movements WHERE clinic_id = %s", w["clinic"])
    assert {r["movement_type"] for r in _rows(_csv(api, w, "movement", store_id=w["store2"]))} == {"opening", "sale_out"}

    exp = _rows(texts.setdefault("expiry", _csv(api, w, "expiry")))
    assert [(r["batch_no"], r["days_to_expiry"], r["bucket"], r["value_at_cost_inr"]) for r in exp] == [
        ("GONE", "-3", "Expired", "6.00"), ("SOON", "10", "≤30d", "12.00")]

    [day] = _rows(texts.setdefault("day_book", _csv(api, w, "day_book", **{"from": str(TODAY), "to": str(TODAY)})))
    upi = _q(conn, "SELECT SUM(amount_paise) FROM rx_sale_payments WHERE clinic_id = %s AND mode = 'upi'", w["clinic"])
    assert (day["sales_count"], day["net_inr"], day["grn_count"]) == ("4", "429.00", "1")
    assert Decimal(day["returns_inr"]) == Decimal(w["ret"]["amount_paise"]) / 100
    assert Decimal(day["net_upi_inr"]) == Decimal(upi) / 100 and day["net_cash_inr"] == "0.00"

    mg = {r["item_code"]: r for r in _rows(texts.setdefault("margin", _csv(api, w, "margin")))}
    assert (mg["PCM650"]["units"], mg["PCM650"]["revenue_taxable_inr"], mg["PCM650"]["cogs_inr"],
            mg["PCM650"]["gross_margin_inr"], mg["PCM650"]["margin_pct"]) == ("20", "200.00", "60.00", "140.00", "70.00")

    for kind, text in texts.items():
        assert w["phone"] not in text and w["phone"][-10:] not in text, kind
    exports = [a for a in w["audits"] if a["action"] == "rx_report_export"]
    assert {a["details"]["kind"] for a in exports} == set(texts)
    sr = next(a for a in exports if a["details"]["kind"] == "sales_register")
    assert sr["details"]["rows"] == 5 and sr["details"]["store_id"] is None and sr["resource_id"] == "sales_register"


def test_report_permissions_margin_admin_only_and_unknown_kind(api, rxw):  # noqa: F811
    w, c = rxw, rxw["clinic"]
    for user, sales, margin in (("mgr", 200, 200), ("reports", 200, 403), ("pharma", 403, 403)):
        api.as_(w["users"][user])
        assert api.call("GET", "/admin/rx/reports/sales_register.csv", c).status_code == sales, user
        assert api.call("GET", "/admin/rx/reports/margin.csv", c).status_code == margin, user
    api.as_(w["users"]["mgr"])
    assert api.call("GET", "/admin/rx/reports/payroll.csv", c).status_code == 422
    assert api.call("GET", "/admin/rx/reports/sales_register.csv", c, **{"from": "2026-02-01",
                                                                          "to": "2026-01-01"}).status_code == 422
    assert api.call("GET", "/admin/rx/reports/sales_register.csv", c, store_id=str(uuid.uuid4())).status_code == 404
    assert not [a for a in w["audits"] if a["details"].get("kind") == "margin" and a["user"].username != "mgr"]
