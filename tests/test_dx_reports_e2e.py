"""Diagnostic OS reports end to end (P7: W10, W11, DELIVERY-POLICY §7): real HTTP routes, real Postgres
(tests/ipd_pg_bridge.py); storage and the WhatsApp pipeline are stubbed and record what they receive.

Every row of DELIVERY-POLICY §7 has its test here (named as in the policy), plus the ADR-DX-07 / -15 / -19
evidence: approval never sends per test, dues hold then payment releases, amendment supersedes, shadow
mode never sends; public verify and share links (DX-46).
"""

import asyncio
import io
import json
import uuid

import pdfplumber
import pytest

from tests.test_dx_results_e2e import (  # noqa: F401  (fixtures)
    _approve, _as, _collect_receive, _order, _q, _save, _visit, api, bench, dxw)


@pytest.fixture
def rep(bench, monkeypatch):  # noqa: F811
    """`bench` + DX delivery owner for the branch, a dispatcher, stubbed storage / pipeline / background tasks."""
    from app.services import dx_reports, dx_templates
    from app.services.lab_reports import LabReportService
    c, conn, U = bench["clinic"], bench["c"], bench["api"].U
    store, sent, jobs = {}, [], []

    class _Bucket:
        def upload(self, path, data, opts):
            assert path.startswith(f"{c}/")
            store[path] = data

        def download(self, path):
            return store[path]

    class _Storage:
        def from_(self, bucket):
            assert bucket == dx_reports.BUCKET == dx_templates.ASSET_BUCKET
            return _Bucket()

    async def fake_upload_and_send(self, **kw):
        sent.append(kw)
        return {"id": str(uuid.uuid4()), "status": "sent", "delivery_status": "sent"}

    monkeypatch.setattr(type(dx_reports.supabase), "storage", property(lambda self: _Storage()), raising=False)
    monkeypatch.setattr(LabReportService, "upload_and_send", fake_upload_and_send)
    monkeypatch.setattr(dx_reports, "spawn_background_task", lambda coro, name=None: jobs.append(coro))

    def drain():
        while jobs:
            asyncio.run(jobs.pop(0))

    _owner(bench, "dx")
    uid = lambda: str(uuid.uuid4())  # noqa: E731
    bench["u"]["disp"] = U("disp", "staff", c, uid(), ["DX_DISPATCH", "DX_READ"], staff_role="DIAGNOSTIC_OPERATOR")
    bench.update(store=store, sent=sent, jobs=jobs, drain=drain)
    _q(conn, "UPDATE patients SET phone = %s WHERE id = %s", "+9198" + str(uuid.uuid4().int)[:8], bench["patient"])
    return bench


def _owner(w, mode):
    _q(w["c"], "UPDATE clinics SET dx_settings = jsonb_set(dx_settings, '{delivery_owner}', %s::jsonb) WHERE id = %s",
       json.dumps({w["branch"]: mode}), w["clinic"])


def _signed_visit(w, items=("cbc",), values=None, **extra):
    """A visit whose tests are collected, received, entered and approved (auto release runs on approval)."""
    v = _visit(w, list(items), **extra)
    _collect_receive(w, v)
    names = {"cbc": "CBC", "lipid": "Lipid Profile", "hba1c": "HbA1c"}
    results = []
    for k in items:
        r = _save(w, _order(v, names[k]), {names[k]: (values or {}).get(k, "14")})
        assert r.status_code == 200, r.text
        results.append(r.json())
    out = _approve(w, results)
    assert out.status_code == 200 and out.json()["signed"] == len(items), out.text
    w["drain"]()
    return v, out.json()


def _releases(w, v, who="disp"):
    r = _as(w, who).call("GET", f"/admin/dx/visits/{v['visit']['id']}/releases", w["clinic"])
    assert r.status_code == 200, r.text
    return r.json()


def _targets(release):
    return {d["target"]: d for d in release["deliveries"]}


def _pay(w, v):
    """Bill, issue and settle in cash at the desk (the unchanged OPD path) — the receipt trigger fills the outbox."""
    api = _as(w, "desk")
    vid = v["visit"]["id"]
    bill = api.call("POST", f"/admin/dx/visits/{vid}/bill", w["clinic"], {"issue": True})
    assert bill.status_code == 200, bill.text
    inv = bill.json()["invoices"][-1]
    pay = api.call("POST", f"/admin/dx/visits/{vid}/receipts", w["clinic"],
                   {"mode": "cash", "amount_paise": inv["total_paise"], "idempotency_key": uuid.uuid4().hex})
    assert pay.status_code == 201, pay.text
    return inv


def _dispatch(w):
    from app.services import dx_reports
    asyncio.run(dx_reports.outbox_dispatch())
    w["drain"]()


def _text(pdf: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        return "\n".join((p.extract_text() or "") + "".join(ch["text"] for ch in p.chars) for p in doc.pages)


# ── DELIVERY-POLICY §7 ──────────────────────────────────────────────────────

def test_unpaid_visit_holds_every_target(rep):
    """Auto release after the last approval -> dx_create_release -> deliveries -> decision: all held."""
    ref = _as(rep, "desk").call("POST", "/admin/dx/referrers", rep["clinic"], {"name": "Dr Meena Iyer"}).json()
    _as(rep, "admin").call("PUT", f"/admin/dx/referrers/{ref['id']}", rep["clinic"],
                           {"report_copy": "whatsapp", "phone": "+919811112222"})
    v, approved = _signed_visit(rep, ("cbc", "lipid"), referrer_id=ref["id"],
                                delivery_prefs={"consent_at": "2026-11-15T09:00:00+05:30"})
    assert len(approved["releases"]) == 1 and approved["releases"][0]["kind"] == "final"
    rel = _releases(rep, v)["releases"][0]
    assert rel["status"] == "rendered" and rel["page_count"] >= 1
    t = _targets(rel)
    assert set(t) == {"patient", "referrer"}
    assert {d["status"] for d in t.values()} == {"held_dues"} and rep["sent"] == []
    assert t["patient"]["external_report_id"] == f"dx-{rel['id']}-p" and t["referrer"]["external_report_id"].endswith("-r")
    held = _as(rep, "disp").call("GET", "/admin/dx/deliveries", rep["clinic"], status="held_dues").json()["deliveries"]
    mine = [d for d in held if d["accession_id"] == v["visit"]["id"]]
    assert len(mine) == 2 and mine[0]["dues_paise"] == 35000 + 55000 and mine[0]["reason_text"] == "Unpaid dues"
    assert _releases(rep, v)["dues_paise"] == 90000


def test_dues_hold_then_payment_releases(rep):
    """ADR-DX-07 evidence + W10 step 2: payment at the desk -> outbox -> every held target goes, once."""
    v, _ = _signed_visit(rep)
    assert _targets(_releases(rep, v)["releases"][0])["patient"]["status"] == "held_dues"
    _pay(rep, v)
    assert _q(rep["c"], "SELECT count(*) FROM dx_outbox WHERE accession_id = %s AND processed_at IS NULL",
              v["visit"]["id"]) >= 1
    _dispatch(rep)
    rel = _releases(rep, v)["releases"][0]
    assert _targets(rel)["patient"]["status"] == "sent" and len(rep["sent"]) == 1
    kw = rep["sent"][0]
    assert kw["source"] == "dx" and kw["external_report_id"] == f"dx-{rel['id']}-p" and kw["ai_summary"] is True
    assert kw["file_bytes"][:5] == b"%PDF-" and kw["matched_patient_id"] == rep["patient"]
    _dispatch(rep)                                    # nothing new in the outbox: nothing sent twice
    assert len(rep["sent"]) == 1


def test_payment_link_settlement_releases_held_report(rep):
    """The existing gateway settlement (settle_opd_payment_link, untouched) releases the report."""
    from app.services.opd_billing import settle_opd_payment_link
    v, _ = _signed_visit(rep)
    bill = _as(rep, "desk").call("POST", f"/admin/dx/visits/{v['visit']['id']}/bill", rep["clinic"], {"issue": True})
    inv = bill.json()["invoices"][-1]
    out = asyncio.run(settle_opd_payment_link(clinic_id=rep["clinic"], gateway="razorpay",
                                              payment_id="pay_" + uuid.uuid4().hex[:14], amount_paid=inv["total_paise"],
                                              opd_invoice_id=inv["id"]))
    assert out["status"] == "ok", out
    _dispatch(rep)
    assert _targets(_releases(rep, v)["releases"][0])["patient"]["status"] == "sent"


def test_resend_rechecks_policy(rep):
    v, _ = _signed_visit(rep)
    d = _targets(_releases(rep, v)["releases"][0])["patient"]
    r = _as(rep, "disp").call("POST", f"/admin/dx/deliveries/{d['id']}/resend", rep["clinic"])
    assert r.status_code == 200 and r.json()["status"] == "held_dues" and rep["sent"] == []
    assert _as(rep, "tech").call("POST", f"/admin/dx/deliveries/{d['id']}/resend", rep["clinic"]).status_code == 403
    _pay(rep, v)
    r = _as(rep, "disp").call("POST", f"/admin/dx/deliveries/{d['id']}/resend", rep["clinic"])
    assert r.status_code == 200 and r.json()["status"] == "queued"
    rep["drain"]()
    assert len(rep["sent"]) == 1
    again = _as(rep, "disp").call("POST", f"/admin/dx/deliveries/{d['id']}/resend", rep["clinic"])
    assert again.status_code == 409 and again.json()["detail"]["code"] == "dx_delivery_already_sent"


def test_print_refused_with_dues_without_override(rep):
    v, _ = _signed_visit(rep)
    rel = _releases(rep, v)["releases"][0]
    url = f"/admin/dx/releases/{rel['id']}/print"
    r = _as(rep, "disp").call("POST", url, rep["clinic"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "held_dues"
    assert _q(rep["c"], "SELECT count(*) FROM dx_print_log WHERE release_id = %s", rel["id"]) == 0
    # the staff clinical view is never gated, but watermarked
    view = _as(rep, "disp").call("GET", f"/admin/dx/releases/{rel['id']}/pdf", rep["clinic"])
    assert view.status_code == 200 and "INTERNAL VIEW" in _text(view.content)
    # override: DX_ADMIN / clinic admin only, reason >= 10 characters
    ov_url = f"/admin/dx/visits/{v['visit']['id']}/delivery-overrides"
    assert _as(rep, "disp").call("POST", ov_url, rep["clinic"], {"reason": "patient admitted, bill on IPD"}).status_code == 403
    assert _as(rep, "admin").call("POST", ov_url, rep["clinic"], {"reason": "short"}).status_code == 422
    ov = _as(rep, "admin").call("POST", ov_url, rep["clinic"], {"reason": "patient admitted, bill on IPD"})
    assert ov.status_code == 201, ov.text
    rep["drain"]()                                       # the override re-evaluates and sends at once
    assert len(rep["sent"]) == 1
    printed = _as(rep, "disp").call("POST", url, rep["clinic"])
    assert printed.status_code == 200 and printed.content[:5] == b"%PDF-"
    log = _q(rep["c"], "SELECT to_jsonb(p) FROM dx_print_log p WHERE release_id = %s", rel["id"])
    assert log["override_id"] == ov.json()["id"] and log["printed_by_name"] == "disp"
    stored = rep["store"][_q(rep["c"], "SELECT pdf_storage_path FROM dx_report_releases WHERE id = %s", rel["id"])]
    assert printed.content == stored                     # a reprint is byte-identical


def test_share_unlock_rechecks_after_void(rep):
    v, _ = _signed_visit(rep)
    rel = _releases(rep, v)["releases"][0]
    url = f"/admin/dx/releases/{rel['id']}/share-link"
    held = _as(rep, "disp").call("POST", url, rep["clinic"])
    assert held.status_code == 409 and held.json()["detail"]["code"] == "held_dues"
    inv = _pay(rep, v)
    link = _as(rep, "disp").call("POST", url, rep["clinic"])
    assert link.status_code == 201, link.text
    token = link.json()["url"].rsplit("/", 1)[1]
    client = rep["api"].client
    page = client.get(f"/public/dx/r/{token}")
    assert page.status_code == 200 and "last 4 digits" in page.text and page.headers["cache-control"] == "no-store"
    phone = _q(rep["c"], "SELECT phone FROM patients WHERE id = %s", rep["patient"])
    ok = client.post(f"/public/dx/r/{token}/unlock", json={"phone_last4": phone[-4:]})
    assert ok.status_code == 200 and ok.content[:5] == b"%PDF-" and ok.headers["cache-control"] == "no-store"
    # money in doubt again after the link was issued: unlock re-checks and holds
    _q(rep["c"], "SELECT set_config('kriya.dx_tx', %s, false)", rep["clinic"])
    try:
        _q(rep["c"], "INSERT INTO dx_field_collections (clinic_id, accession_id, amount_paise, mode, collected_by, "
                     "collected_by_name, collected_at, idempotency_key) VALUES (%s, %s, 100, 'cash', %s, 'phleb', now(), "
                     "%s)", rep["clinic"], v["visit"]["id"], str(uuid.uuid4()), uuid.uuid4().hex)
    finally:
        _q(rep["c"], "SELECT set_config('kriya.dx_tx', '', false)")
    held = client.post(f"/public/dx/r/{token}/unlock", json={"phone_last4": phone[-4:]})
    assert held.status_code == 409 and held.json()["detail"]["code"] == "held"
    assert inv["status"] == "issued"
    # wrong digits burn attempts; the 5th attempt is the last
    for _ in range(3):
        assert client.post(f"/public/dx/r/{token}/unlock", json={"phone_last4": "0000"}).status_code in (403, 429)
    locked = client.post(f"/public/dx/r/{token}/unlock", json={"phone_last4": phone[-4:]})
    assert locked.status_code in (423, 429)
    assert client.post("/public/dx/r/" + "x" * 22 + "/unlock", json={"phone_last4": "1234"}).status_code in (404, 429)


def test_partner_download_respects_dues_for_patient_billed_visit(rep):
    """The partner portal arrives in P8b; its path is the decision 'partner_portal', which holds dues."""
    acc = str(_q(rep["c"], "INSERT INTO dx_b2b_accounts (clinic_id, name, kind, report_delivery) VALUES "
                           "(%s, 'City Hospital', 'hospital', 'both') RETURNING id", rep["clinic"]))
    v, _ = _signed_visit(rep, b2b_account_id=acc)                 # account default billing -> patient pays here
    rel = _releases(rep, v)["releases"][0]
    cur = rep["c"].cursor()
    cur.execute("SELECT allowed, reason FROM dx_delivery_decision(%s, %s, 'partner_portal')", (rep["clinic"], rel["id"]))
    assert cur.fetchone() == (False, "held_dues")
    _pay(rep, v)
    cur.execute("SELECT allowed, reason FROM dx_delivery_decision(%s, %s, 'partner_portal')", (rep["clinic"], rel["id"]))
    assert cur.fetchone() == (True, None)


def test_generic_resend_refuses_dx_rows(rep):
    """A DX report reached through the generic Lab Reports resend is refused (AUDIT A20)."""
    from app.services.lab_reports import DxReportResendRefused, LabReportService
    rid = str(_q(rep["c"], "INSERT INTO lab_reports (clinic_id, patient_phone, report_name, source, status, "
                           "external_report_id) VALUES (%s, '+919800000001', 'R1', 'dx', 'sent', %s) RETURNING id",
                 rep["clinic"], f"dx-{uuid.uuid4()}-p"))
    with pytest.raises(DxReportResendRefused):
        asyncio.run(LabReportService().resend_report(rid, new_phone="+919800000002", clinic_id=rep["clinic"]))
    r = _as(rep, "admin").call("POST", f"/admin/lab-reports/{rid}/resend", rep["clinic"])
    assert r.status_code in (403, 409)


def test_voice_cannot_reach_held_report(rep):
    """Voice reads lab_reports by caller phone; a held delivery never creates one (AUDIT A21)."""
    v, _ = _signed_visit(rep)
    phone = _q(rep["c"], "SELECT phone FROM patients WHERE id = %s", rep["patient"])
    assert _targets(_releases(rep, v)["releases"][0])["patient"]["status"] == "held_dues"
    assert _q(rep["c"], "SELECT count(*) FROM lab_reports WHERE clinic_id = %s AND (patient_phone = %s OR "
                        "external_report_id LIKE 'dx-%%')", rep["clinic"], phone) == 0


def test_chat_status_for_held_report(rep):
    """The patient's WhatsApp "my reports" list (lab_reports by phone) holds nothing for a held visit; the
    status-only chat intent with the payment link is DX-57 (P9)."""
    from app.services.lab_reports import LabReportService
    v, _ = _signed_visit(rep)
    phone = _q(rep["c"], "SELECT phone FROM patients WHERE id = %s", rep["patient"])
    assert asyncio.run(LabReportService().get_reports_by_phone(phone, clinic_id=rep["clinic"])) in ([], None) or \
        all(r.get("source") != "dx" for r in asyncio.run(LabReportService().get_reports_by_phone(phone,
                                                                                                 clinic_id=rep["clinic"])))


def test_tpa_visit_never_targets_patient(rep):
    acc = str(_q(rep["c"], "INSERT INTO dx_b2b_accounts (clinic_id, name, kind, report_delivery, desk_whatsapp) VALUES "
                           "(%s, 'MD India TPA', 'insurance_tpa', 'account', '+919800000111') RETURNING id", rep["clinic"]))
    v, _ = _signed_visit(rep, b2b_account_id=acc, billing_mode="internal")
    rel = _releases(rep, v)["releases"][0]
    t = _targets(rel)
    assert set(t) == {"account"} and t["account"]["status"] == "sent"
    (kw,) = rep["sent"]
    assert kw["patient_phone"] == "+919800000111" and kw["ai_summary"] is False and kw["matched_patient_id"] is None
    assert kw["match_source"] == "dx_account" and kw["external_report_id"].endswith("-a")
    r = _as(rep, "disp").call("POST", f"/admin/dx/releases/{rel['id']}/print", rep["clinic"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "payer_routed"
    r = _as(rep, "disp").call("POST", f"/admin/dx/releases/{rel['id']}/share-link", rep["clinic"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "payer_routed"


def test_copay_visit(rep):
    """b2b_split (priced by DX-M8): the account copy goes at once; the patient side waits for the co-pay."""
    acc = str(_q(rep["c"], "INSERT INTO dx_b2b_accounts (clinic_id, name, kind, report_delivery, desk_whatsapp, "
                           "patient_copy_authorised_at, patient_copy_reason) VALUES (%s, 'Wellness Corp', 'corporate', "
                           "'both', '+919800000222', now(), 'employee asked for their copy') RETURNING id", rep["clinic"]))
    v = _visit(rep, ["cbc"], b2b_account_id=acc)
    _q(rep["c"], "SELECT set_config('kriya.dx_tx', %s, false)", rep["clinic"])
    try:
        _q(rep["c"], "UPDATE dx_accessions SET billing_mode = 'b2b_split' WHERE id = %s", v["visit"]["id"])
    finally:
        _q(rep["c"], "SELECT set_config('kriya.dx_tx', '', false)")
    _collect_receive(rep, v)
    r = _save(rep, _order(v, "CBC"), {"CBC": "14"}).json()
    assert _approve(rep, [r]).json()["signed"] == 1
    rep["drain"]()
    t = _targets(_releases(rep, v)["releases"][0])
    assert t["account"]["status"] == "sent" and t["patient"]["status"] == "held_dues"
    assert [k["match_source"] for k in rep["sent"]] == ["dx_account"]


def test_critical_call_not_blocked_by_dues(rep):
    v = _visit(rep, ["cbc"])
    _collect_receive(rep, v)
    saved = _save(rep, _order(v, "CBC"), {"CBC": "40"})              # critical high on the bench fixture
    ev = saved.json()["critical_event_id"]
    assert ev and _releases(rep, v)["dues_paise"] > 0
    call = _as(rep, "sup").call("POST", f"/admin/dx/critical-events/{ev}/call", rep["clinic"], {
        "notified_to": "Dr. Anuradha", "notified_role": "referrer", "channel": "phone", "read_back_confirmed": True})
    assert call.status_code == 201, call.text


# ── ADR evidence, W11, shadow mode ───────────────────────────────────────────

def test_dx_signature_does_not_send_per_test(rep):
    """Approval signs; it never calls the per-test LIS delivery (ADR-DX-07). The release sends, once paid."""
    from app.services import lis
    calls = []
    orig = lis.deliver_report

    async def spy(*a, **k):
        calls.append(a)
        return await orig(*a, **k)
    lis.deliver_report = spy
    try:
        _signed_visit(rep)
    finally:
        lis.deliver_report = orig
    assert calls == [] and rep["sent"] == []


def test_amendment_supersedes_release(rep):
    v, _ = _signed_visit(rep)
    _pay(rep, v)
    _dispatch(rep)
    r1 = _releases(rep, v)["releases"][0]
    order = _order(v, "CBC")
    p = _as(rep, "tech").call("GET", f"/admin/dx/orders/{order}/worksheet", rep["clinic"]).json()["parameters"][0]
    amended = _as(rep, "tech").call("POST", f"/admin/dx/orders/{order}/results", rep["clinic"], {
        "values": [{"parameter_id": p["parameter_id"], "value": "15"}], "amendment_reason": "transcription error"})
    assert amended.status_code == 200, amended.text
    assert _approve(rep, [amended.json()]).json()["signed"] == 1
    rep["drain"]()
    rels = _releases(rep, v)["releases"]
    assert [r["kind"] for r in rels] == ["final", "amended"] and rels[0]["status"] == "superseded"
    assert rels[0]["superseded_by"] == rels[1]["id"]
    assert _targets(rels[1])["patient"]["status"] == "sent" and len(rep["sent"]) == 2
    pdf = rep["sent"][1]["file_bytes"]
    assert "AMENDED — replaces R1" in _text(pdf)
    old = _as(rep, "disp").call("GET", f"/admin/dx/releases/{r1['id']}/pdf", rep["clinic"])
    assert "SUPERSEDED" in _text(old.content)
    ver = rep["api"].client.get(f"/public/dx/verify/{r1['verify_code']}").json()
    assert ver["status"] == "superseded"


def test_shadow_mode_never_sends(rep):
    _owner(rep, "shadow")
    v = _visit(rep, ["cbc"])
    _pay(rep, v)
    _collect_receive(rep, v)
    r = _save(rep, _order(v, "CBC"), {"CBC": "14"}).json()
    _approve(rep, [r])
    rep["drain"]()
    rel = _releases(rep, v)["releases"][0]
    assert _targets(rel)["patient"]["status"] == "held_shadow" and rep["sent"] == []
    printed = _as(rep, "disp").call("POST", f"/admin/dx/releases/{rel['id']}/print", rep["clinic"])
    assert printed.status_code == 200 and "SHADOW" in _text(printed.content)
    # switching the branch to dx and pressing "send now" sends
    _owner(rep, "dx")
    d = _targets(rel)["patient"]
    assert _as(rep, "disp").call("POST", f"/admin/dx/deliveries/{d['id']}/resend", rep["clinic"]).json()["status"] == "queued"
    rep["drain"]()
    assert len(rep["sent"]) == 1


def test_connector_branch_does_not_auto_release(rep):
    _owner(rep, "connector")
    v, approved = _signed_visit(rep)
    assert approved["releases"] == [] and _releases(rep, v)["releases"] == []
    # manual release is still possible; nothing leaves DX
    r = _as(rep, "disp").call("POST", f"/admin/dx/visits/{v['visit']['id']}/releases", rep["clinic"])
    assert r.status_code == 201 and r.json()["deliveries"][0]["reason"] == "owner_connector"
    rep["drain"]()
    assert rep["sent"] == []


def test_manual_release_partial_then_final(rep):
    _q(rep["c"], "UPDATE clinics SET dx_settings = dx_settings || '{\"auto_release\": \"off\"}' WHERE id = %s",
       rep["clinic"])
    v = _visit(rep, ["cbc", "lipid"])
    _collect_receive(rep, v)
    r = _save(rep, _order(v, "CBC"), {"CBC": "14"}).json()
    assert _approve(rep, [r]).json()["releases"] == []
    url = f"/admin/dx/visits/{v['visit']['id']}/releases"
    assert _as(rep, "tech").call("POST", url, rep["clinic"]).status_code == 403
    p = _as(rep, "disp").call("POST", url, rep["clinic"])
    assert p.status_code == 201 and p.json()["kind"] == "partial"
    again = _as(rep, "disp").call("POST", url, rep["clinic"])
    assert again.status_code == 409 and again.json()["detail"]["code"] == "dx_release_nothing_new"
    rep["drain"]()
    pdf = rep["store"][_q(rep["c"], "SELECT pdf_storage_path FROM dx_report_releases WHERE id = %s", p.json()["id"])]
    assert "PARTIAL REPORT" in _text(pdf)
    wl = _as(rep, "disp").call("GET", "/admin/dx/worklist", rep["clinic"], open_only=True).json()
    row = next(x for x in wl["visits"] if x["id"] == v["visit"]["id"])
    assert {o["item_name"]: o["stage"] for o in row["orders"]}["CBC"] == "released"


# ── public verify (DX-46) ────────────────────────────────────────────────────

def test_public_verify_shows_no_values_or_identity(rep):
    v, _ = _signed_visit(rep)
    rel = _releases(rep, v)["releases"][0]
    client = rep["api"].client
    r = client.get(f"/public/dx/verify/{rel['verify_code']}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["status"] == "current" and body["release_no"] == 1 and body["tests"] == ["CBC"]
    assert body["sha256"] == rel["pdf_sha256"] and body["patient"] == {"initials": "A.", "age_sex": "34Y/F"}
    phone = _q(rep["c"], "SELECT phone FROM patients WHERE id = %s", rep["patient"])
    assert "Asha" not in r.text and phone not in r.text and "14" not in json.dumps(body["tests"])
    assert client.get("/public/dx/verify/AAAAAAAAAA").status_code in (404, 429)
    assert client.get("/public/dx/verify/not-a-code").status_code == 422
