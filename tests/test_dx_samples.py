"""Diagnostic OS samples and collection end to end (P4: W1 steps 4–6, W4, W7): real HTTP routes,
real Postgres (tests/ipd_pg_bridge.py).

Collection queue (STAT first, site filter), label print (PDF / ZPL, per tube /
per test / register book, every print logged, reprints flagged), bind, collect
(idempotent, collect-before-payment), reject with recollection + the WhatsApp
notice, same-site receipt, sample tracking, rejection reasons, permissions; home
collection: the phlebotomist's own visit only (tubes + doorstep cash), the
cashier handover that creates exactly one receipt on the shift (AUDIT A14).
"""

import uuid
from unittest.mock import MagicMock

import pytest

from tests.test_dx_bookings import _booking
from tests.test_dx_visits_e2e import dxw, visit  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _key() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def lab(dxw):  # noqa: F811
    U, c = dxw["api"].U, dxw["clinic"]
    dxw["u"].update({
        "col": U("col", "staff", c, str(uuid.uuid4()), ["DX_COLLECT"], staff_role="FRONT_DESK"),
        "rcv": U("rcv", "staff", c, str(uuid.uuid4()), [], staff_role="LAB_OPERATOR"),     # implied DX_RECEIVE
        "cash": U("cash", "staff", c, str(uuid.uuid4()), ["DX_BILLING"], staff_role="CASHIER"),
    })
    dxw["site"] = str(_q(dxw["c"], "SELECT id FROM dx_sites WHERE clinic_id = %s", c))
    return dxw


def _as(w, who):
    return w["api"].as_(w["u"][who])


def test_provisioning_seeded_rejection_reasons(lab):
    r = _as(lab, "rcv").call("GET", "/admin/dx/rejection-reasons", lab["clinic"])
    assert r.status_code == 200
    codes = [x["code"] for x in r.json()["reasons"]]
    assert {"HEMOLYSED", "CLOTTED", "INSUFFICIENT_QTY", "OTHER"} <= set(codes) and len(codes) == 11


def test_queue_stat_first_and_collect(lab):
    c = lab["clinic"]
    routine = visit(lab, ["cbc"]).json()
    stat = visit(lab, ["lipid", "hba1c"], priority="stat").json()
    api = _as(lab, "col")
    q = api.call("GET", "/admin/dx/collection/queue", c)
    assert q.status_code == 200, q.text
    ids = [v["id"] for v in q.json()["visits"]]
    assert ids.index(stat["visit"]["id"]) < ids.index(routine["visit"]["id"])
    tube = next(v for v in q.json()["visits"] if v["id"] == stat["visit"]["id"])["samples"]
    assert {t["container_code"] for t in tube} == {"01", "07"} and all(t["tests"] for t in tube)
    assert [v["id"] for v in api.call("GET", "/admin/dx/collection/queue", c, priority="stat").json()["visits"]] == \
        [stat["visit"]["id"]]
    assert api.call("GET", "/admin/dx/collection/queue", c, site_id=str(uuid.uuid4())).status_code == 404
    # collect both tubes with their scanned labels; retry is the same answer
    body = {"samples": [{"id": s["id"], "barcode": s["barcode"]} for s in stat["samples"]], "idempotency_key": _key()}
    r = api.call("POST", f"/admin/dx/visits/{stat['visit']['id']}/collect", c, body)
    assert r.status_code == 200, r.text
    assert r.json()["visit"]["status"] == "in_process" and {s["status"] for s in r.json()["samples"]} == {"collected"}
    assert api.call("POST", f"/admin/dx/visits/{stat['visit']['id']}/collect", c, body).json()["replayed"] is True
    assert {o["status"] for o in r.json()["orders"]} == {"sample_collected"}
    assert stat["visit"]["id"] not in [v["id"] for v in api.call("GET", "/admin/dx/collection/queue", c).json()["visits"]]
    # wrong tube scanned
    bad = api.call("POST", f"/admin/dx/visits/{routine['visit']['id']}/collect", c,
                   {"samples": [{"id": routine["samples"][0]["id"], "barcode": stat["samples"][0]["barcode"]}],
                    "idempotency_key": _key()})
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "dx_barcode_mismatch"


def test_collect_before_payment_refuses_until_paid(lab):
    c, conn = lab["clinic"], lab["c"]
    _q(conn, "UPDATE clinics SET dx_settings = dx_settings || '{\"collect_before_payment\": true}' WHERE id = %s", c)
    v = visit(lab, ["cbc"]).json()
    body = {"samples": [{"id": v["samples"][0]["id"]}], "idempotency_key": _key()}
    r = _as(lab, "col").call("POST", f"/admin/dx/visits/{v['visit']['id']}/collect", c, body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_awaiting_payment"
    _as(lab, "desk").call("POST", f"/admin/dx/visits/{v['visit']['id']}/bill", c, {"issue": True})
    paid = _as(lab, "desk").call("POST", f"/admin/dx/visits/{v['visit']['id']}/receipts", c,
                                 {"mode": "cash", "amount_paise": 35000, "idempotency_key": _key()})
    assert paid.status_code == 201, paid.text
    assert _as(lab, "col").call("POST", f"/admin/dx/visits/{v['visit']['id']}/collect", c, body).status_code == 200


def test_labels_pdf_zpl_per_test_register_and_reprint_log(lab):
    c, conn = lab["clinic"], lab["c"]
    v = visit(lab, ["lipid", "cbc", "hba1c"]).json()
    vid = v["visit"]["id"]
    api = _as(lab, "desk")                                    # the desk prints too (DX_REGISTER)
    r = api.call("POST", f"/admin/dx/visits/{vid}/labels/print", c, {})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert r.headers["x-reprint"] == "false"
    edta = next(s for s in v["samples"] if s["container_code"] == "07")
    z = api.call("POST", f"/admin/dx/visits/{vid}/labels/print", c,
                 {"sample_ids": [edta["id"]], "mode": "per_test", "format": "zpl", "copies": 2, "register_label": True})
    assert z.status_code == 200 and z.headers["content-type"].startswith("text/plain")
    text = z.text
    assert text.count("^XA") == 3                              # CBC + HBA1C labels, then the register-book label
    assert f">;{edta['barcode']}" in text and "^PQ2" in text and "CBC" in text and "HBA1C" in text
    assert z.headers["x-reprint"] == "true"
    reg = api.call("POST", f"/admin/dx/visits/{vid}/labels/print", c, {"mode": "register"})
    assert reg.status_code == 200
    rows = _q(conn, "SELECT jsonb_agg(jsonb_build_object('mode', mode, 'reprint', is_reprint, 'n', cardinality(sample_ids),"
                    " 'copies', copies) ORDER BY created_at) FROM dx_label_prints WHERE accession_id = %s", vid)
    assert rows == [{"mode": "per_sample", "reprint": False, "n": 2, "copies": 1},
                    {"mode": "per_test", "reprint": True, "n": 1, "copies": 2},
                    {"mode": "register", "reprint": True, "n": 0, "copies": 1}]
    assert _as(lab, "nobody").call("POST", f"/admin/dx/visits/{vid}/labels/print", c, {}).status_code == 403


def test_reject_recollect_notice_receive_and_track(lab, monkeypatch):
    from app.services import dx
    c = lab["clinic"]
    sent = []

    async def fake_notice(clinic, out, reason):
        sent.append((out["visit"]["id"], reason))
        return True
    monkeypatch.setattr(dx, "notify_recollection", fake_notice)
    v = visit(lab, ["cbc", "hba1c"]).json()
    vid, tube = v["visit"]["id"], v["samples"][0]
    _as(lab, "col").call("POST", f"/admin/dx/visits/{vid}/collect", c,
                         {"samples": [{"id": tube["id"]}], "idempotency_key": _key()})
    rcv = _as(lab, "rcv")
    got = rcv.call("POST", "/admin/dx/samples/receive-direct", c, {"barcode": tube["barcode"], "site_id": lab["site"]})
    assert got.status_code == 200, got.text
    assert got.json()["status"] == "received"
    r = rcv.call("POST", f"/admin/dx/samples/{tube['id']}/reject", c,
                 {"reason_code": "HEMOLYSED", "notes": "pink", "recollect": True})
    assert r.status_code == 200, r.text
    out = r.json()
    new = next(s for s in out["samples"] if s["id"] == out["replacement_sample_id"])
    assert new["recollect_of"] == tube["id"] and new["status"] == "pending_collection"
    assert new["barcode"] != tube["barcode"] and new["barcode"][2:4] == tube["barcode"][2:4]
    assert sent == [(vid, "Haemolysed sample")]
    assert {o["status"] for o in out["orders"] if o["dx_sample_id"] == tube["id"]} == {"cancelled"}
    assert rcv.call("POST", f"/admin/dx/samples/{tube['id']}/reject", c,
                    {"reason_code": "HEMOLYSED"}).json()["replayed"] is True and len(sent) == 1
    t = rcv.call("GET", f"/admin/dx/samples/{tube['barcode']}/track", c)
    assert t.status_code == 200
    assert [e["kind"] for e in t.json()["timeline"]] == ["registered", "collected", "received", "rejected"]
    assert t.json()["replaced_by"]["id"] == new["id"] and t.json()["visit"]["patient_name"] == "Asha"
    assert rcv.call("GET", "/admin/dx/samples/NOSUCHCODE/track", c).status_code == 404
    bad = rcv.call("POST", f"/admin/dx/samples/{new['id']}/reject", c, {"reason_code": "HEMOLYSED"})
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "dx_sample_not_rejectable"
    assert _as(lab, "desk").call("POST", f"/admin/dx/samples/{new['id']}/reject", c,
                                 {"reason_code": "CLOTTED"}).status_code == 403


def test_notice_goes_to_patient_text_or_template(lab, monkeypatch):
    import asyncio
    from app.services import dx
    from app.services.whatsapp import whatsapp_service
    calls = []

    async def no_window(clinic, phone, text, _source=""):
        calls.append(("text", phone))
        return False

    async def template(clinic, phone, name, components=None, _source=""):
        calls.append(("template", name, components[0]["parameters"][2]["text"]))
        return True
    monkeypatch.setattr(whatsapp_service, "send_text", no_window)
    monkeypatch.setattr(whatsapp_service, "send_template", template)
    v = visit(lab, ["cbc"]).json()
    clinic = {"id": lab["clinic"], "name": "Desk Lab"}
    assert asyncio.run(dx.notify_recollection(clinic, v, "Clotted sample")) is True
    assert calls[0][0] == "text" and calls[1] == ("template", "dx_recollection_required", "Clotted sample")


def test_bind_preprinted_label(lab):
    c, conn = lab["clinic"], lab["c"]
    v = visit(lab, ["cbc"]).json()
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (c,))
        cur.execute("""INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode_source,
                       collection_site_id, processing_site_id) VALUES (%s, %s, %s, 'serum', 'preprinted', %s, %s)
                       RETURNING id""", (c, v["visit"]["id"], lab["serum"], lab["site"], lab["site"]))
        sid = str(cur.fetchone()[0])
        cur.execute("COMMIT")
    api = _as(lab, "col")
    r = api.call("POST", f"/admin/dx/samples/{sid}/bind", c, {"barcode": "MD16583"})
    assert r.status_code == 200 and r.json()["barcode"] == "MD16583"
    dup = api.call("POST", f"/admin/dx/samples/{v['samples'][0]['id']}/bind", c, {"barcode": "MD16583"})
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "dx_barcode_already_set"
    assert api.call("POST", f"/admin/dx/samples/{sid}/bind", c, {"barcode": "bad code"}).status_code == 422


def test_branch_pinned_collector_sees_only_own_site(lab):
    c, conn = lab["clinic"], lab["c"]
    north = str(_q(conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'North') RETURNING id", c))
    _q(conn, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) VALUES (%s, %s, 'NOR', 'North')", c, north)
    v = visit(lab, ["cbc"], branch_id=lab["branch"]).json()
    U = lab["api"].U
    pinned = U("colN", "staff", c, str(uuid.uuid4()), ["DX_COLLECT"], staff_role="FRONT_DESK", branch_id=north)
    api = lab["api"].as_(pinned)
    assert v["visit"]["id"] not in [x["id"] for x in api.call("GET", "/admin/dx/collection/queue", c).json()["visits"]]
    r = api.call("POST", f"/admin/dx/visits/{v['visit']['id']}/collect", c,
                 {"samples": [{"id": v["samples"][0]["id"]}], "idempotency_key": _key()})
    assert r.status_code == 404


# ── home collection (W4, AUDIT A14) ──────────────────────────────────────────

def _phleb(w):
    uid = str(_q(w["c"], """INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, is_active,
                            full_name) VALUES (%s, %s, 'x', 'staff', 'PHLEBOTOMIST', true, 'Ravi P') RETURNING id""",
                 w["clinic"], "ph_" + uuid.uuid4().hex[:8]))
    return w["api"].U("ph", "staff", w["clinic"], uid, [], staff_role="PHLEBOTOMIST")


def test_home_visit_tubes_doorstep_cash_and_handover(lab):
    c, conn = lab["clinic"], lab["c"]
    appt = _booking(lab, "cbc", None, home_fee=10000)
    me, other = _phleb(lab), _phleb(lab)
    _q(conn, "UPDATE appointments SET phlebotomist_id = %s, collection_status = 'assigned' WHERE id = %s", me.user_id, appt)
    phl = lab["api"].as_(me)
    early = phl.call("POST", f"/admin/dx/home-collection/visits/{appt}/doorstep-cash", c,
                     {"amount_paise": 100, "mode": "cash", "idempotency_key": _key()})
    assert early.status_code == 409                                        # not registered as a visit yet
    conv = _as(lab, "desk").call("POST", "/admin/dx/bookings/convert", c,
                                 {"appointment_ids": [appt], "idempotency_key": _key()})
    assert conv.status_code == 201, conv.text
    vid = conv.json()["visit"]["id"]
    seen = lab["api"].as_(me).call("GET", f"/admin/dx/home-collection/visits/{appt}/samples", c)
    assert seen.status_code == 200 and seen.json()["due_paise"] == 45000 and "items" not in seen.json()
    tube = seen.json()["samples"][0]
    col = phl.call("POST", f"/admin/dx/home-collection/visits/{appt}/samples", c,
                   {"samples": [{"id": tube["id"], "barcode": tube["barcode"]}], "idempotency_key": _key()})
    assert col.status_code == 200 and col.json()["samples"][0]["status"] == "collected"
    assert _q(conn, "SELECT collection_mode FROM dx_samples WHERE id = %s", tube["id"]) == "home"
    # someone else's visit is a 404; a UPI amount needs its reference; more than due is refused
    assert lab["api"].as_(other).call("GET", f"/admin/dx/home-collection/visits/{appt}/samples", c).status_code == 404
    phl = lab["api"].as_(me)                                                # as_() switches the acting login
    assert phl.call("POST", f"/admin/dx/home-collection/visits/{appt}/doorstep-cash", c,
                    {"amount_paise": 100, "mode": "upi_to_clinic", "idempotency_key": _key()}).status_code == 422
    assert phl.call("POST", f"/admin/dx/home-collection/visits/{appt}/doorstep-cash", c,
                    {"amount_paise": 99999, "mode": "cash", "idempotency_key": _key()}).status_code == 409
    key = _key()
    fc = phl.call("POST", f"/admin/dx/home-collection/visits/{appt}/doorstep-cash", c,
                  {"amount_paise": 45000, "mode": "cash", "idempotency_key": key})
    assert fc.status_code == 201, fc.text
    assert fc.json()["status"] == "pending_handover"
    assert phl.call("POST", f"/admin/dx/home-collection/visits/{appt}/doorstep-cash", c,
                    {"amount_paise": 45000, "mode": "cash", "idempotency_key": key}).json()["replayed"] is True
    # cashier: the pending list, then the handover = exactly one receipt on the issued bill
    cash = _as(lab, "cash")
    assert cash.call("POST", f"/admin/dx/visits/{vid}/bill", c, {"issue": True}).status_code == 200
    pending = cash.call("GET", "/admin/dx/home-collection/field-collections", c).json()["field_collections"]
    assert [p["id"] for p in pending] == [fc.json()["id"]]
    h = cash.call("POST", f"/admin/dx/home-collection/field-collections/{fc.json()['id']}/handover", c, {})
    assert h.status_code == 200, h.text
    assert h.json()["status"] == "handed_over" and h.json()["receipt"]["amount_paise"] == 45000
    again = cash.call("POST", f"/admin/dx/home-collection/field-collections/{fc.json()['id']}/handover", c, {})
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert _q(conn, "SELECT count(*) FROM opd_receipts r JOIN opd_invoices i ON i.id = r.invoice_id "
                    "WHERE i.accession_id = %s", vid) == 1
    assert _q(conn, "SELECT status FROM opd_invoices WHERE accession_id = %s", vid) == "paid"
    assert cash.call("GET", "/admin/dx/home-collection/field-collections", c).json()["field_collections"] == []
    # the phlebotomist cannot hand over
    assert lab["api"].as_(me).call("POST", f"/admin/dx/home-collection/field-collections/{fc.json()['id']}/handover", c,
                    {}).status_code == 403


def _req(method, path):
    r = MagicMock()
    r.method, r.url.path = method, path
    return r


@pytest.mark.parametrize("method,path,allowed", [
    ("GET", "/admin/dx/home-collection/visits/abc/samples", True),
    ("POST", "/admin/dx/home-collection/visits/abc/samples", True),
    ("POST", "/admin/dx/home-collection/visits/abc/doorstep-cash", True),
    ("POST", "/admin/dx/home-collection/field-collections/abc/handover", False),
    ("GET", "/admin/dx/visits", False),
    ("GET", "/admin/dx/collection/queue", False),
])
def test_phlebotomist_confinement_reaches_only_its_dx_routes(method, path, allowed):
    from app.routers.admin import AdminUser, _enforce_phlebotomist_scope
    ph = AdminUser(username="ph", role="staff", clinic_id="c1", user_id="p1", staff_role="PHLEBOTOMIST")
    if allowed:
        assert _enforce_phlebotomist_scope(_req(method, path), ph) is ph
    else:
        with pytest.raises(Exception) as e:
            _enforce_phlebotomist_scope(_req(method, path), ph)
        assert getattr(e.value, "status_code", None) == 403
