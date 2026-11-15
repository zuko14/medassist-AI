"""Diagnostic OS B2B end to end (P8a / P8b: W5, W14, SECURITY §5): real HTTP routes, real Postgres
(tests/ipd_pg_bridge.py); storage and WhatsApp are stubbed and record what they receive.

Rate cards (CRUD, CSV import all-or-nothing), a desk B2B credit visit at contract price posting
charges, co-pay split billing the patient share on the OPD ledger, cancellation reversing the charge,
payments (idempotent), holds (manual + the daily job), receivables ageing, statements (issue, PDF,
WhatsApp, void), barcode stock, partner logins (creation, confinement, session account, own data only,
release PDF rules) and patient-billed visits untouched.
"""

import asyncio
import io
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pdfplumber
import pytest
from fastapi import HTTPException

from tests.test_dx_reports_e2e import rep  # noqa: F401  (fixture)
from tests.test_dx_results_e2e import bench  # noqa: F401  (fixture)
from tests.test_dx_visits_e2e import dxw, visit as _desk_visit  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


@pytest.fixture
def b2b(dxw, monkeypatch):  # noqa: F811
    from app.services import dx_b2b
    from app.services.whatsapp import whatsapp_service
    c, U = dxw["clinic"], dxw["api"].U
    store, sent = {}, []

    class _Bucket:
        def upload(self, path, data, opts):
            assert path.startswith(f"{c}/dx-statements/")
            store[path] = data

        def download(self, path):
            return store[path]

        def create_signed_url(self, path, seconds):
            return {"signedURL": f"https://files.test/{path}?exp={seconds}"}

    class _Storage:
        def from_(self, bucket):
            assert bucket == dx_b2b.BUCKET
            return _Bucket()

    async def fake_send_text(clinic, phone, text, _source=None):
        sent.append({"phone": phone, "text": text, "source": _source})
        return True

    monkeypatch.setattr(type(dx_b2b.supabase), "storage", property(lambda self: _Storage()), raising=False)
    monkeypatch.setattr(whatsapp_service, "send_text", fake_send_text)
    dxw["u"]["acct"] = U("acct", "staff", c, str(uuid.uuid4()), ["DX_B2B_MANAGE", "DX_READ"], staff_role="B2B_MANAGER")
    dxw.update(store=store, sent=sent)
    return dxw


def _as(w, who):
    return w["api"].as_(who if isinstance(who, w["api"].U) else w["u"][who])   # AdminUser is a str


def _account(w, **extra):
    body = {"name": "City Hospital", "kind": "hospital", "desk_whatsapp": "+919811112222", "credit_days": 30, **extra}
    r = _as(w, "acct").call("POST", "/admin/dx/b2b-accounts", w["clinic"], body)
    assert r.status_code == 201, r.text
    return r.json()


def _b2b_visit(w, account, items, mode="b2b_credit", **extra):
    r = _desk_visit(w, items, b2b_account_id=account["id"], billing_mode=mode, source="b2b", **extra)
    assert r.status_code == 201, r.text
    return r.json()


def _ledger(w, account):
    r = _as(w, "acct").call("GET", f"/admin/dx/b2b-accounts/{account['id']}/ledger", w["clinic"])
    assert r.status_code == 200, r.text
    return r.json()


def _partner(w, account, name="hosp"):
    r = _as(w, "acct").call("POST", f"/admin/dx/b2b-accounts/{account['id']}/users", w["clinic"],
                            {"username": f"{name}_{uuid.uuid4().hex[:6]}", "password": "S3cret-pass", "full_name": "Desk"})
    assert r.status_code == 201, r.text
    row = r.json()
    return w["api"].U(row["username"], "staff", w["clinic"], row["id"], [], staff_role="B2B_PARTNER",
                      dx_b2b_account_id=account["id"])


def _pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as doc:
        return "\n".join(p.extract_text() or "" for p in doc.pages)


# ── rate cards, desk B2B visit ──────────────────────────────────────────────

def test_rate_card_import_and_credit_visit_charges(b2b):
    c = b2b["clinic"]
    card = _as(b2b, "acct").call("POST", "/admin/dx/rate-cards", c, {"name": "City contract"})
    assert card.status_code == 201, card.text
    cid = card.json()["id"]
    bad = b2b["api"].client.post(f"/admin/dx/rate-cards/{cid}/import", params={"clinic_id": c},
                          files={"file": ("r.csv", b"test_code,price\nCBC,200\nNOPE,10\nLIPID,abc\n", "text/csv")})
    assert bad.status_code == 422 and len(bad.json()["detail"]["errors"]) == 2
    assert _as(b2b, "acct").call("GET", f"/admin/dx/rate-cards/{cid}", c).json()["items"] == []      # nothing applied
    ok = b2b["api"].client.post(f"/admin/dx/rate-cards/{cid}/import", params={"clinic_id": c},
                         files={"file": ("r.csv", b"test_code,price\ncbc,200\nLIPID,450.50\n", "text/csv")})
    assert ok.status_code == 200 and ok.json()["added"] == 2, ok.text
    edit = _as(b2b, "acct").call("POST", f"/admin/dx/rate-cards/{cid}/items", c,
                    {"items": [{"lab_test_id": b2b["T"]["lipid"], "price_paise": 45000}]})
    assert edit.json() == {"added": 0, "updated": 1, "removed": 0}
    assert _as(b2b, "acct").call("GET", "/admin/dx/rate-cards", c).json()["rate_cards"][0]["item_count"] == 2
    acc = _account(b2b, rate_card_id=cid, credit_limit_paise=500000)
    # the desk quote shows the contract price; the patient pays nothing on a credit visit
    q = _as(b2b, "desk").call("POST", "/admin/dx/visits/quote", c, {
        "patient_id": b2b["patient"], "b2b_account_id": acc["id"], "billing_mode": "b2b_credit",
        "items": [{"lab_test_id": b2b["T"]["cbc"]}]})
    assert q.status_code == 200 and q.json()["lines"][0]["price_paise"] == 20000 and q.json()["total_paise"] == 0
    v = _b2b_visit(b2b, acc, ["cbc", "lipid", "hba1c"])
    by = {i["item_name"]: i for i in v["items"]}
    assert (by["CBC"]["price_source"], by["CBC"]["payer_share_paise"], by["HbA1c"]["price_source"]) == \
        ("rate_card", 20000, "mrp")
    led = _ledger(b2b, acc)
    assert [e["entry_type"] for e in led["entries"]] == ["charge"] * 3
    assert led["balance"]["balance_paise"] == 20000 + 45000 + 45000 == led["entries"][-1]["running_balance_paise"]
    assert led["entries"][0]["accession_number"] == v["visit"]["accession_number"]
    listed = next(a for a in _as(b2b, "acct").call("GET", "/admin/dx/b2b-accounts", c).json()["accounts"] if a["id"] == acc["id"])
    assert listed["balance_paise"] == 110000
    # cancelling a test reverses its charge
    r = _as(b2b, "desk").call("POST", f"/admin/dx/visits/{v['visit']['id']}/items/{by['HbA1c']['id']}/cancel", c,
                              {"reason": "duplicate order"})
    assert r.status_code == 200, r.text
    assert _ledger(b2b, acc)["balance"]["balance_paise"] == 65000
    # a technician sees no money; a desk login cannot manage rate cards
    assert _as(b2b, "tech").call("GET", "/admin/dx/rate-cards", c).status_code == 403
    assert _as(b2b, "desk").call("POST", "/admin/dx/rate-cards", c, {"name": "Nope"}).status_code == 403


def test_copay_split_bills_patient_share(b2b):
    c = b2b["clinic"]
    acc = _account(b2b, name="TPA Panel", kind="insurance_tpa", default_billing_mode="b2b_split",
                   copay_mode="percent", copay_value=2000)
    assert acc["report_delivery"] == "account"
    v = _b2b_visit(b2b, acc, ["cbc"], mode="b2b_split")
    item = v["items"][0]
    assert (item["patient_share_paise"], item["payer_share_paise"]) == (7000, 28000)
    bill = _as(b2b, "desk").call("POST", f"/admin/dx/visits/{v['visit']['id']}/bill", c, {"issue": True})
    assert bill.status_code == 200, bill.text
    assert bill.json()["invoices"][-1]["total_paise"] == 7000
    assert _ledger(b2b, acc)["balance"]["balance_paise"] == 28000


def test_patient_visit_untouched(b2b):
    r = _desk_visit(b2b, ["cbc"])
    assert r.status_code == 201 and r.json()["items"][0]["patient_share_paise"] == 35000
    assert _q(b2b["c"], "SELECT count(*) FROM dx_b2b_ledger WHERE clinic_id = %s", b2b["clinic"]) == 0


# ── payments, holds, ageing ─────────────────────────────────────────────────

def test_payments_holds_and_ageing(b2b):
    c = b2b["clinic"]
    acc = _account(b2b, credit_limit_paise=30000)
    _b2b_visit(b2b, acc, ["cbc"])                                    # 35000
    neft = _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/payments", c,
                    {"amount_paise": 1000, "mode": "neft", "idempotency_key": uuid.uuid4().hex})
    assert neft.status_code == 422 and neft.json()["detail"]["code"] == "dx_payment_reference_required"
    body = {"amount_paise": 2000, "mode": "neft", "reference": "UTR42", "idempotency_key": "pay-0000000001"}
    p1 = _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/payments", c, body)
    p2 = _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/payments", c, body)
    assert p1.status_code == 201 and p2.json()["replayed"] is True and p2.json()["id"] == p1.json()["id"]
    # the daily job puts the account over its limit on hold; registration is then refused
    from app.services import dx_b2b
    assert asyncio.run(dx_b2b.hold_evaluation()) >= 1
    got = _as(b2b, "acct").call("GET", f"/admin/dx/b2b-accounts/{acc['id']}", c).json()
    assert (got["account"]["status"], got["account"]["hold_kind"], got["balance"]["balance_paise"]) == \
        ("on_hold", "limit", 33000)
    held = _desk_visit(b2b, ["cbc"], b2b_account_id=acc["id"], billing_mode="b2b_credit")
    assert held.status_code == 409 and held.json()["detail"]["code"] == "dx_b2b_on_hold"
    _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/payments", c,
             {"amount_paise": 10000, "mode": "cash", "idempotency_key": uuid.uuid4().hex})
    asyncio.run(dx_b2b.hold_evaluation())
    assert _as(b2b, "acct").call("GET", f"/admin/dx/b2b-accounts/{acc['id']}", c).json()["account"]["status"] == "active"
    # manual hold and release through the hold route
    assert _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/hold", c, {"status": "on_hold"}).status_code == 422
    h = _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/hold", c, {"status": "on_hold", "reason": "cheque bounced"})
    assert h.status_code == 200 and h.json()["hold_kind"] == "manual"
    asyncio.run(dx_b2b.hold_evaluation())                            # never lifts a manual hold
    assert _as(b2b, "acct").call("GET", f"/admin/dx/b2b-accounts/{acc['id']}", c).json()["account"]["hold_kind"] == "manual"
    _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/hold", c, {"status": "active"})
    ag = _as(b2b, "acct").call("GET", "/admin/dx/b2b-accounts/ageing", c).json()
    row = next(r for r in ag["accounts"] if r["account_id"] == acc["id"])
    assert row["current_paise"] == 23000 and ag["totals"]["balance_paise"] == 23000


# ── statements ──────────────────────────────────────────────────────────────

def test_statement_issue_pdf_send_void(b2b):
    c = b2b["clinic"]
    acc = _account(b2b, gstin="36AAAAA0000A1Z5", billing_address="Road 1, Hyderabad")
    v = _b2b_visit(b2b, acc, ["cbc", "lipid"])
    _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/payments", c,
             {"amount_paise": 50000, "mode": "upi", "reference": "UPI77", "idempotency_key": uuid.uuid4().hex})
    period = {"period_from": (date.today() - timedelta(days=29)).isoformat(), "period_to": date.today().isoformat()}
    st = _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/statements", c, period)
    assert st.status_code == 201, st.text
    st = st.json()
    assert st["statement_number"].startswith(f"B2B-{date.today().year}-") and len(st["statement_number"]) == 14
    assert (st["total_charges_paise"], st["total_credits_paise"], st["closing_paise"]) == (90000, 50000, 40000)
    assert st["pdf_storage_path"] in b2b["store"]
    pdf = _as(b2b, "acct").call("GET", f"/admin/dx/b2b-accounts/statements/{st['id']}/pdf", c)
    text = _pdf_text(pdf.content)
    assert pdf.status_code == 200 and st["statement_number"] in text and v["visit"]["accession_number"] in text
    assert "Rs 400.00" in text and "commission" in text            # closing balance; ADR-DX-11 line
    again = _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/statements", c, period)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "dx_statement_overlap"
    send = _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/statements/{st['id']}/send", c)
    assert send.json() == {"sent": True, "reason": None}
    msg = b2b["sent"][-1]
    assert msg["phone"] == "+919811112222" and st["statement_number"] in msg["text"] and "https://files.test/" in msg["text"]
    listed = _as(b2b, "acct").call("GET", f"/admin/dx/b2b-accounts/{acc['id']}/statements", c).json()["statements"]
    assert [s["id"] for s in listed] == [st["id"]]
    void = _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/statements/{st['id']}/void", c, {"reason": "wrong period"})
    assert void.status_code == 200 and void.json()["status"] == "void"
    assert _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/statements/{st['id']}/send", c).json()["sent"] is False
    re = _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/statements", c, period).json()
    assert re["closing_paise"] == 40000 and re["statement_seq"] == st["statement_seq"] + 1


# ── barcode stock ───────────────────────────────────────────────────────────

def test_barcode_stock_routes(b2b):
    c = b2b["clinic"]
    acc = _account(b2b)
    r = _as(b2b, "acct").call("POST", "/admin/dx/barcode-stock", c, {"account_id": acc["id"], "prefix": "CH", "range_from": 1,
                                                         "range_to": 500})
    assert r.status_code == 201, r.text
    over = _as(b2b, "acct").call("POST", "/admin/dx/barcode-stock", c, {"account_id": acc["id"], "prefix": "CH", "range_from": 400,
                                                            "range_to": 900})
    assert over.status_code == 409 and over.json()["detail"]["code"] == "dx_barcode_stock_overlap"
    both = _as(b2b, "acct").call("POST", "/admin/dx/barcode-stock", c, {"prefix": "X", "range_from": 1, "range_to": 2})
    assert both.status_code == 422
    assert len(_as(b2b, "acct").call("GET", "/admin/dx/barcode-stock", c).json()["stock"]) == 1


# ── partner portal ──────────────────────────────────────────────────────────

def test_partner_confinement_function():
    from app.routers.admin import AdminUser, _enforce_dx_confined_scope

    def req(path, method="GET"):
        return SimpleNamespace(url=SimpleNamespace(path=path), method=method)

    p = AdminUser("hosp", "staff", "c1", "u1", [], staff_role="B2B_PARTNER", dx_b2b_account_id="a1")
    assert _enforce_dx_confined_scope(req("/admin/dx/partner/visits"), p) is p
    assert _enforce_dx_confined_scope(req("/admin/me"), p) is p
    for path in ("/admin/dx/visits", "/admin/dx/b2b-accounts", "/fhir/Patient/1", "/admin/patients"):
        with pytest.raises(HTTPException) as e:
            _enforce_dx_confined_scope(req(path), p)
        assert e.value.status_code == 403
    orphan = AdminUser("hosp2", "staff", "c1", "u2", [], staff_role="B2B_PARTNER")
    with pytest.raises(HTTPException) as e:
        _enforce_dx_confined_scope(req("/admin/dx/partner/me"), orphan)
    assert e.value.status_code == 403 and "not linked" in e.value.detail


def test_partner_session_reads_account(b2b):
    import hashlib
    from app.routers.admin import resolve_admin_session
    acc = _account(b2b)
    user = _partner(b2b, acc)
    token = uuid.uuid4().hex
    _q(b2b["c"], """INSERT INTO admin_sessions (token_hash, username, role, clinic_id, user_id, staff_role, permissions,
                    expires_at) VALUES (%s, %s, 'staff', %s, %s, 'B2B_PARTNER', '{}', now() + interval '1 hour')""",
       hashlib.sha256(token.encode()).hexdigest(), user.username, b2b["clinic"], user.user_id)
    got = asyncio.run(resolve_admin_session(token))
    assert (got.staff_role, str(got.dx_b2b_account_id)) == ("B2B_PARTNER", acc["id"])


def test_partner_portal_own_data_only(b2b):
    c = b2b["clinic"]
    card = _as(b2b, "acct").call("POST", "/admin/dx/rate-cards", c, {"name": "Partner card"}).json()
    _as(b2b, "acct").call("POST", f"/admin/dx/rate-cards/{card['id']}/items", c,
                          {"items": [{"lab_test_id": b2b["T"]["cbc"], "price_paise": 18000}]})
    acc = _account(b2b, rate_card_id=card["id"])
    other = _account(b2b, name="Other Hospital", desk_whatsapp="+919833334444")
    other_visit = _b2b_visit(b2b, other, ["cbc"])
    p = _partner(b2b, acc)
    me = _as(b2b, p).call("GET", "/admin/dx/partner/me", c)
    assert me.status_code == 200, me.text
    tests = {t["test_code"]: t for t in me.json()["tests"]}
    assert (tests["CBC"]["price_paise"], tests["CBC"]["contract_price"]) == (18000, True)
    assert me.json()["account"]["id"] == acc["id"] and me.json()["balance"]["balance_paise"] == 0
    body = {"name": "Ravi (IP 2231)", "gender": "male", "age_years": 51, "external_ref": "IP2231",
            "items": [{"lab_test_id": b2b["T"]["cbc"]}], "idempotency_key": "req-" + str(uuid.uuid4())}   # as the portal sends
    r = _as(b2b, p).call("POST", "/admin/dx/partner/visits", c, body)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["items"][0]["payer_share_paise"] == 18000 and v["samples"][0]["barcode"]
    assert _as(b2b, p).call("POST", "/admin/dx/partner/visits", c, body).json()["replayed"] is True
    assert _q(b2b["c"], "SELECT source FROM dx_accessions WHERE id = %s", v["visit"]["id"]) == "partner_portal"
    listed = _as(b2b, p).call("GET", "/admin/dx/partner/visits", c).json()["visits"]
    assert [x["id"] for x in listed] == [v["visit"]["id"]] and other_visit["visit"]["id"] not in str(listed)
    assert _as(b2b, p).call("GET", "/admin/dx/partner/statements", c).json()["balance"]["balance_paise"] == 18000
    # releases: report_delivery 'patient' -> no portal download; another account's release -> 404
    assert acc["report_delivery"] == "patient"
    assert _as(b2b, p).call("GET", f"/admin/dx/partner/releases/{uuid.uuid4()}/pdf", c).status_code == 403
    # staff are not partners, and the partner routes never take an account from the request
    assert _as(b2b, "acct").call("GET", "/admin/dx/partner/me", c).status_code == 403
    assert _as(b2b, p).call("GET", "/admin/dx/partner/me", c, account_id=other["id"]).json()["account"]["id"] == acc["id"]
    # hold: the portal refuses new samples
    _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/hold", c, {"status": "on_hold", "reason": "dues"})
    held = _as(b2b, p).call("POST", "/admin/dx/partner/visits", c, {**body, "idempotency_key": "req-" + str(uuid.uuid4())})
    assert held.status_code == 409


def test_partner_user_route_and_generic_staff_refusal(b2b):
    acc = _account(b2b)
    p = _partner(b2b, acc)
    row = _q(b2b["c"], "SELECT to_jsonb(a) FROM clinic_admins a WHERE id = %s", p.user_id)
    assert (row["staff_role"], row["dx_b2b_account_id"], row["permissions"]) == ("B2B_PARTNER", acc["id"], [])
    dup = _as(b2b, "acct").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/users", b2b["clinic"],
                                {"username": p.username, "password": "Another-pass1"})
    assert dup.status_code == 409
    assert _as(b2b, "desk").call("POST", f"/admin/dx/b2b-accounts/{acc['id']}/users", b2b["clinic"],
                                 {"username": "x_" + uuid.uuid4().hex[:6], "password": "Another-pass1"}).status_code == 403


def test_partner_downloads_account_report(rep):  # noqa: F811
    """W5-A step 4: an account that receives reports downloads its own released PDF from the portal;
    the same visit's patient never gets it (payer-routed account)."""
    from tests.test_dx_reports_e2e import _signed_visit
    rep["u"]["acct"] = rep["api"].U("acct", "staff", rep["clinic"], str(uuid.uuid4()), ["DX_B2B_MANAGE", "DX_READ"],
                                    staff_role="B2B_MANAGER")
    acc = _account(rep, name="Corporate Panel", kind="corporate")
    assert acc["report_delivery"] == "account"
    v, _ = _signed_visit(rep, ("cbc",), b2b_account_id=acc["id"], billing_mode="b2b_credit", source="b2b")
    rels = _as(rep, "disp").call("GET", f"/admin/dx/visits/{v['visit']['id']}/releases", rep["clinic"]).json()
    rel = rels["releases"][0] if isinstance(rels, dict) else rels[0]
    p = _partner(rep, acc)
    listed = _as(rep, p).call("GET", "/admin/dx/partner/visits", rep["clinic"]).json()["visits"]
    assert listed[0]["releases"][0]["id"] == rel["id"]
    pdf = _as(rep, p).call("GET", f"/admin/dx/partner/releases/{rel['id']}/pdf", rep["clinic"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF", pdf.text[:200]
    # another account's partner gets 404 for this release
    other = _partner(rep, _account(rep, name="Other Corp", kind="corporate", desk_whatsapp="+919855556666"))
    assert _as(rep, other).call("GET", f"/admin/dx/partner/releases/{rel['id']}/pdf", rep["clinic"]).status_code == 404


def test_referrer_volume_counts_only(b2b):
    """ADR-DX-11: referral analytics are clinical volume — visits and tests per month, never money."""
    c = b2b["clinic"]
    ref = _as(b2b, "desk").call("POST", "/admin/dx/referrers", c, {"name": "Dr Volume", "qualification": "MD"}).json()
    for items in (["cbc", "lipid"], ["hba1c"]):
        assert _desk_visit(b2b, items, referrer_id=ref["id"]).status_code == 201
    out = _as(b2b, "desk").call("GET", "/admin/dx/referrers/volume", c, months=3)
    assert out.status_code == 200, out.text
    rows = [r for r in out.json()["rows"] if r["referrer_id"] == ref["id"]]
    assert len(rows) == 1 and (rows[0]["visits"], rows[0]["tests"]) == (2, 3)
    assert set(rows[0]) == {"referrer_id", "month", "visits", "tests"}
    assert "paise" not in out.text and "commission" not in out.text
