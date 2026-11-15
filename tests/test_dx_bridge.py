"""Diagnostic OS analyser bridge and QC (P10: DX-60, DX-61, DX-62; ADR-DX-13).

Pure: ASTM E1381 frames + E1394 records and HL7 v2 ORU vectors, the request signature, Westgard rule vectors.
Real Postgres (tests/ipd_pg_bridge.py) through the real routes: an agent key shown once, signed host query /
results / heartbeat (stale, forged, inactive, foreign instrument refused), replay by message id, matching by
machine code into a worksheet PREFILL (never saved without the switch), the technician's save marking the
analyser source, auto-enter only when switched on and only for an order without a result, unmatched -> map /
ignore, QC runs with Westgard flags (rejection recorded, append-only) and the Levey-Jennings data.
"""

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from connectors.bridge import parsers
from tests.test_dx_results_e2e import _as, _collect_receive, _order, _visit, bench  # noqa: F401  (fixture)
from tests.test_dx_visits_e2e import dxw  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

CR = "\r"


def _astm_records(barcode, results=(("HGB", "14.2", "g/dL", "N"),), header_id="MSG-1"):
    rec = [f"H|\\^&|{header_id}||XN-550^Sysmex|||||||P|1", "P|1||PID1", f"O|1|{barcode}^01||^^^CBC|R"]
    rec += [f"R|{i + 1}|^^^{code}|{val}|{unit}||{flag}||F" for i, (code, val, unit, flag) in enumerate(results)]
    return CR.join(rec + ["L|1|N"]) + CR


def _astm(barcode, **kw):
    """Records split over frames (ETB for all but the last), as an analyser sends them."""
    text, size = _astm_records(barcode, **kw), 40
    chunks = [text[i:i + size] for i in range(0, len(text), size)]
    return "".join(parsers.astm_frame(i + 1, c, last=(i == len(chunks) - 1)) for i, c in enumerate(chunks))


def _hl7(barcode, value="14.2", code="HGB"):
    return CR.join(["MSH|^~\\&|XN|LAB|KRIYA|LAB|20261115101500||ORU^R01|HL7-77|P|2.5", "PID|1||P1||Rao^Ravi",
                    f"OBR|1|{barcode}|FILLER9|CBC^Complete blood count",
                    f"OBX|1|NM|{code}^Haemoglobin||{value}|g/dL^^UCUM|13-17|L|||F",
                    "OBX|2|ST|NOTE^Comment||A\\S\\B \\T\\ C||||||F"]) + CR


# ── pure: parsers ────────────────────────────────────────────────────────────

def test_astm_frames_records_and_checksum():
    out = parsers.parse_astm(_astm("BC1001", results=(("HGB", "14.2", "g/dL", "N"), ("WBC", "11.8", "10^3/uL", "H"))))
    assert out["message_id"] == "MSG-1"
    assert out["specimens"] == [{"barcode": "BC1001", "results": [
        {"code": "HGB", "value": "14.2", "unit": "g/dL", "flag": "N"},
        {"code": "WBC", "value": "11.8", "unit": "10^3/uL", "flag": "H"}]}]
    assert parsers.astm_checksum("1H|\\^&\x03") == f"{(sum(map(ord, '1H|' + chr(92) + '^&')) + 3) % 256:02X}"
    # unframed record text (a TCP analyser without E1381) parses the same
    assert parsers.parse_astm(_astm_records("BC1001"))["specimens"][0]["results"][0]["value"] == "14.2"


@pytest.mark.parametrize("damage, needle", [
    (lambda s: s[:-4] + "00\r\n", "checksum"),
    (lambda s: parsers.astm_frame(1, "H|\\^&\r", last=False) + parsers.astm_frame(3, "L|1\r"), "sequence"),
    (lambda s: s[:-6], "truncated"),
])
def test_astm_bad_frames_are_refused(damage, needle):
    with pytest.raises(parsers.ParseError) as e:
        parsers.parse_astm(damage(_astm("BC1001")))
    assert needle in str(e.value)


def test_astm_record_rules():
    with pytest.raises(parsers.ParseError):
        parsers.parse_astm("P|1\rL|1\r")                                    # no header
    with pytest.raises(parsers.ParseError):
        parsers.parse_astm("H|\\^&\rR|1|^^^HGB|14\rL|1\r")                # result before order
    two = parsers.parse_astm("H|\\^&\rO|1|A1\rR|1|^^^HGB|14\rO|2|B2\rR|1|^^^HGB|9\rL|1\r")
    assert [s["barcode"] for s in two["specimens"]] == ["A1", "B2"]


def test_hl7_oru_segments_and_escapes():
    out = parsers.parse_hl7("\x0b" + _hl7("BC2002") + "\x1c\r")                # MLLP block
    assert out["message_id"] == "HL7-77"
    sp = out["specimens"][0]
    assert sp["barcode"] == "BC2002"
    assert sp["results"][0] == {"code": "HGB", "value": "14.2", "unit": "g/dL", "flag": "L"}
    assert sp["results"][1]["value"] == "A^B & C"
    filler = parsers.parse_hl7(_hl7("").replace("OBR|1||FILLER9", "OBR|1||FILLER9"))
    assert filler["specimens"][0]["barcode"] == "FILLER9"                       # OBR-3 when OBR-2 is empty
    with pytest.raises(parsers.ParseError):
        parsers.parse_hl7("PID|1\r")
    with pytest.raises(parsers.ParseError):
        parsers.parse("csv", "x")


# ── pure: Westgard ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("z, history, flags", [
    (0.5, [], []),
    (2.1, [], ["1_2s"]),
    (3.2, [], ["1_2s", "1_3s"]),
    (2.2, [2.1], ["1_2s", "2_2s"]),
    (-2.3, [-2.5], ["1_2s", "2_2s"]),
    (2.4, [-2.1], ["1_2s", "R_4s"]),
    (1.2, [1.5, 1.1, 1.8], ["4_1s"]),
    (1.2, [1.5, 0.9, 1.8], []),
    (0.3, [0.2, 0.5, 0.1, 0.9, 0.4, 0.6, 0.2, 0.3, 0.1], ["10_x"]),
    (-0.3, [-0.2, -0.5, -0.1, -0.9, -0.4, -0.6, -0.2, -0.3, 0.1], []),
])
def test_westgard_vectors(z, history, flags):
    from app.services import dx_qc
    assert dx_qc.westgard(Decimal(str(z)), history) == flags
    assert dx_qc.accepted(flags) == (flags in ([], ["1_2s"]))


def test_z_score():
    from app.services import dx_qc
    assert dx_qc.z_score("13.1", "12.5", "0.3") == Decimal("2.0000")


# ── pure: signature ──────────────────────────────────────────────────────────

def test_signature_is_bound_to_method_path_time_and_body():
    from app.services import dx_bridge
    secret, prefix, key_hash = dx_bridge.new_agent_key()
    assert prefix == secret[:12] and prefix.startswith("kba_") and len(secret) > 40 and len(key_hash) == 64
    base = dx_bridge.sign_with_secret(secret, "POST", "/p", "t", b"{}")
    assert base == dx_bridge.sign(key_hash, "post", "/p", "t", b"{}")
    for other in (("GET", "/p", "t", b"{}"), ("POST", "/q", "t", b"{}"), ("POST", "/p", "u", b"{}"),
                  ("POST", "/p", "t", b"{ }")):
        assert dx_bridge.sign_with_secret(secret, *other) != base


# ── real Postgres through the routes ────────────────────────────────────────

@pytest.fixture
def lab(bench):  # noqa: F811
    w, c, U = bench, bench["clinic"], bench["api"].U
    _q(w["c"], "UPDATE lab_test_parameters SET machine_code = 'HGB' WHERE lab_test_id = %s", w["T"]["cbc"])
    w["u"]["qc"] = U("qc", "staff", c, str(uuid.uuid4()), ["DX_QC"], staff_role="STAFF")
    w["u"]["desk"] = U("desk2", "staff", c, str(uuid.uuid4()), ["DX_REGISTER"], staff_role="FRONT_DESK")
    a = _as(w, "admin").call("POST", "/admin/dx/bridge-agents", c, {"site_id": w["site"], "name": "Hub lab PC"})
    assert a.status_code == 201, a.text
    w["agent"] = a.json()
    i = _as(w, "admin").call("POST", "/admin/dx/instruments", c, {"agent_id": w["agent"]["id"], "name": "Sysmex XN",
                                                                   "protocol": "astm", "department_id": w["dept"]})
    assert i.status_code == 201, i.text
    w["inst"] = i.json()
    return w


def _signed(w, method, path, body=b"", secret=None, prefix=None, ts=None):
    from app.services import dx_bridge
    ts = ts or datetime.now(timezone.utc).isoformat()
    sig = dx_bridge.sign_with_secret(secret or w["agent"]["secret"], method, path, ts, body)
    headers = {"X-Bridge-Key-Prefix": prefix or w["agent"]["key_prefix"], "X-Bridge-Timestamp": ts,
               "X-Bridge-Signature": sig, "Content-Type": "application/json"}
    return w["api"].client.request(method, path, content=body, headers=headers)


def _post_result(w, raw, message_id=None, instrument=None, **kw):
    body = json.dumps({"instrument_id": instrument or w["inst"]["id"], "message_id": message_id or uuid.uuid4().hex,
                       "raw_message": raw}).encode()
    return _signed(w, "POST", "/internal/dx/bridge/results", body, **kw)


def _tube(w, receive=True):
    v = _visit(w, ["cbc"])
    _collect_receive(w, v, receive=receive)
    return v, v["samples"][0]["barcode"]


def test_agent_secret_shown_once_and_admin_only(lab):
    w, c = lab, lab["clinic"]
    assert w["agent"]["secret"].startswith(w["agent"]["key_prefix"])
    listed = _as(w, "admin").call("GET", "/admin/dx/bridge-agents", c).json()["agents"]
    assert listed[0]["key_prefix"] == w["agent"]["key_prefix"] and "secret" not in listed[0] and "key_hash" not in listed[0]
    stored = _q(w["c"], "SELECT key_hash FROM dx_bridge_agents WHERE id = %s", w["agent"]["id"])
    assert w["agent"]["secret"] not in json.dumps(listed) and len(stored) == 64
    assert _as(w, "tech").call("POST", "/admin/dx/bridge-agents", c, {"site_id": w["site"], "name": "x pc"}).status_code == 403
    rot = _as(w, "admin").call("PATCH", f"/admin/dx/bridge-agents/{w['agent']['id']}", c, {"rotate_key": True}).json()
    assert rot["secret"] != w["agent"]["secret"]
    assert _signed(w, "POST", "/internal/dx/bridge/heartbeat").status_code == 401        # the old key is dead
    w["agent"].update(rot)
    assert _signed(w, "POST", "/internal/dx/bridge/heartbeat").status_code == 200


def test_bridge_auth_refusals(lab):
    w = lab
    ok = _signed(w, "POST", "/internal/dx/bridge/heartbeat")
    assert ok.status_code == 200 and ok.json()["last_seen_at"]
    stale = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    assert _signed(w, "POST", "/internal/dx/bridge/heartbeat", ts=stale).status_code == 401
    assert _signed(w, "POST", "/internal/dx/bridge/heartbeat", secret="kba_" + "x" * 43).status_code == 401
    assert _signed(w, "POST", "/internal/dx/bridge/heartbeat", prefix="kba_zzzzzzzz").status_code == 401
    assert w["api"].client.post("/internal/dx/bridge/heartbeat").status_code == 401
    # the body is signed: the same headers with another body fail
    from app.services import dx_bridge
    ts = datetime.now(timezone.utc).isoformat()
    sig = dx_bridge.sign_with_secret(w["agent"]["secret"], "POST", "/internal/dx/bridge/results", ts, b"{}")
    r = w["api"].client.post("/internal/dx/bridge/results", content=b'{"x":1}', headers={
        "X-Bridge-Key-Prefix": w["agent"]["key_prefix"], "X-Bridge-Timestamp": ts, "X-Bridge-Signature": sig})
    assert r.status_code == 401
    _as(w, "admin").call("PATCH", f"/admin/dx/bridge-agents/{w['agent']['id']}", w["clinic"], {"is_active": False})
    assert _signed(w, "POST", "/internal/dx/bridge/heartbeat").status_code == 401


def test_host_query_lists_machine_codes(lab):
    w = lab
    _, barcode = _tube(w)
    r = _signed(w, "GET", f"/internal/dx/bridge/orders?barcode={barcode}")
    assert r.status_code == 200, r.text
    assert r.json()["tests"][0]["machine_codes"] == ["HGB"] and "phone" not in r.text and "Asha" not in r.text
    assert _signed(w, "GET", "/internal/dx/bridge/orders?barcode=NOPE123").status_code == 404


def test_results_prefill_replay_and_technician_save(lab):
    w, c = lab, lab["clinic"]
    v, barcode = _tube(w)
    oid = _order(v, "CBC")
    r = _post_result(w, _astm(barcode, results=(("HGB", "14.2", "g/dL", "N"), ("XYZ", "1", "", ""))), message_id="m-1")
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["mapping_status"], out["matched_orders"], out["unmatched_codes"], out["replayed"]) == \
           ("pending", 1, ["XYZ"], False)
    again = _post_result(w, _astm(barcode), message_id="m-1").json()
    assert again["replayed"] and again["id"] == out["id"]
    assert _q(w["c"], "SELECT count(*) FROM dx_instrument_results WHERE clinic_id = %s", c) == 1
    # a prefill only: nothing is saved without a person
    assert _q(w["c"], "SELECT count(*) FROM lab_results WHERE order_id = %s", oid) == 0
    ws = _as(w, "tech").call("GET", f"/admin/dx/orders/{oid}/worksheet", c).json()
    pre = ws["instrument_prefill"]
    pid = next(p["parameter_id"] for p in ws["parameters"] if p["name"] == "CBC")
    assert pre["instrument_result_id"] == out["id"] and pre["values"][pid]["value"] == "14.2"
    saved = _as(w, "tech").call("POST", f"/admin/dx/orders/{oid}/results", c, {
        "values": [{"parameter_id": pid, "value": "14.2"}], "instrument_result_id": out["id"]})
    assert saved.status_code == 200, saved.text
    meta = _q(w["c"], "SELECT source || ':' || instrument_result_id FROM dx_result_value_meta WHERE result_id = %s",
              saved.json()["id"])
    assert meta == f"instrument:{out['id']}"
    assert _q(w["c"], "SELECT mapping_status FROM dx_instrument_results WHERE id = %s", out["id"]) == "applied"
    # still needs a human approval: the bench shows it pending approval
    st = _as(w, "tech").call("GET", "/admin/dx/worklist", c).json()
    order = next(o for x in st["visits"] for o in x["orders"] if o["id"] == oid)
    assert order["stage"] == "pending_approval"


def test_typed_over_prefill_is_manual(lab):
    w, c = lab, lab["clinic"]
    v, barcode = _tube(w)
    oid = _order(v, "CBC")
    rid = _post_result(w, _astm(barcode)).json()["id"]
    ws = _as(w, "tech").call("GET", f"/admin/dx/orders/{oid}/worksheet", c).json()
    pid = next(p["parameter_id"] for p in ws["parameters"] if p["name"] == "CBC")
    saved = _as(w, "tech").call("POST", f"/admin/dx/orders/{oid}/results", c, {
        "values": [{"parameter_id": pid, "value": "14.0"}], "instrument_result_id": rid}).json()
    assert _q(w["c"], "SELECT source FROM dx_result_value_meta WHERE result_id = %s", saved["id"]) == "manual"
    wrong = _as(w, "tech").call("POST", f"/admin/dx/orders/{oid}/results", c, {
        "values": [{"parameter_id": pid, "value": "14.0"}], "instrument_result_id": str(uuid.uuid4())})
    assert wrong.status_code == 409


def test_auto_enter_only_when_switched_on_and_order_empty(lab):
    w, c = lab, lab["clinic"]
    _q(w["c"], "UPDATE clinics SET dx_settings = dx_settings || '{\"instrument_auto_enter\": true}' WHERE id = %s", c)
    v, barcode = _tube(w)
    oid = _order(v, "CBC")
    out = _post_result(w, _astm(barcode)).json()
    assert out["mapping_status"] == "applied" and out["applied_result_id"]
    row = _q(w["c"], "SELECT technician_name || '|' || status FROM lab_results WHERE id = %s", out["applied_result_id"])
    assert row == f"instrument:{w['inst']['id']}|entered"
    # a second message for the same order never overwrites what is there: it stays a prefill
    again = _post_result(w, _astm(barcode, results=(("HGB", "9.9", "g/dL", "L"),))).json()
    assert again["mapping_status"] == "pending"
    assert _q(w["c"], "SELECT count(*) FROM lab_results WHERE order_id = %s", oid) == 1
    # a tube not yet received is never entered automatically
    _, b2 = _tube(w, receive=False)
    assert _post_result(w, _astm(b2)).json()["mapping_status"] == "pending"


def test_unmatched_then_map_or_ignore(lab):
    w, c = lab, lab["clinic"]
    v, barcode = _tube(w)
    oid = _order(v, "CBC")
    lost = _post_result(w, _astm("NOTUBE99")).json()
    odd = _post_result(w, _astm(barcode, results=(("HB", "13.1", "g/dL", ""),))).json()
    assert (lost["mapping_status"], odd["mapping_status"]) == ("unmatched", "unmatched")
    listed = _as(w, "tech").call("GET", "/admin/dx/instrument-results", c, status="unmatched").json()["results"]
    assert {r["id"] for r in listed} == {lost["id"], odd["id"]} and listed[0]["instrument_name"] == "Sysmex XN"
    cand = _as(w, "tech").call("GET", f"/admin/dx/instrument-results/{odd['id']}/candidates", c, barcode=barcode).json()
    assert cand["orders"][0]["id"] == oid and cand["orders"][0]["parameters"][0]["machine_code"] == "HGB"
    pid = cand["orders"][0]["parameters"][0]["id"]
    m = _as(w, "tech").call("POST", f"/admin/dx/instrument-results/{odd['id']}/map", c,
                            {"order_id": oid, "codes": {"HB": str(pid)}})
    assert m.status_code == 200 and m.json()["mapping_status"] == "pending", m.text
    pre = _as(w, "tech").call("GET", f"/admin/dx/orders/{oid}/worksheet", c).json()["instrument_prefill"]
    assert pre["values"][str(pid)]["value"] == "13.1"
    assert _as(w, "tech").call("POST", f"/admin/dx/instrument-results/{lost['id']}/ignore", c).json()[
        "mapping_status"] == "ignored"
    assert _as(w, "desk").call("POST", f"/admin/dx/instrument-results/{lost['id']}/ignore", c).status_code == 403
    assert _q(w["c"], "SELECT raw_message LIKE %s FROM dx_instrument_results WHERE id = %s", "%NOTUBE99%", lost["id"])


def test_results_refusals(lab):
    w, c = lab, lab["clinic"]
    assert _post_result(w, "garbage").status_code == 422
    two = "H|\\^&\rO|1|A1\rR|1|^^^HGB|14\rO|2|B2\rR|1|^^^HGB|9\rL|1\r"
    assert _post_result(w, two).json()["detail"]["code"] == "dx_bridge_one_specimen"
    assert _post_result(w, _astm("X1"), instrument=str(uuid.uuid4())).status_code == 404
    # an instrument of another agent is not this agent's to post for
    other = _as(w, "admin").call("POST", "/admin/dx/bridge-agents", c, {"site_id": w["site"], "name": "Other PC"}).json()
    inst2 = _as(w, "admin").call("POST", "/admin/dx/instruments", c, {"agent_id": other["id"], "name": "Cobas",
                                                                       "protocol": "hl7v2"}).json()
    assert _post_result(w, _hl7("X1"), instrument=inst2["id"]).status_code == 404
    hl = _post_result(w, _hl7("X1"), instrument=inst2["id"], secret=other["secret"], prefix=other["key_prefix"])
    assert hl.status_code == 200 and hl.json()["mapping_status"] == "unmatched"


def test_qc_runs_westgard_and_chart(lab):
    w, c = lab, lab["clinic"]
    today = date.today()
    body = {"instrument_id": w["inst"]["id"], "parameter_code": "hgb", "level": "L1", "lot": "LOT-7",
            "target_mean": 12.5, "target_sd": 0.3, "valid_from": (today - timedelta(days=1)).isoformat(),
            "valid_to": (today + timedelta(days=90)).isoformat()}
    assert _as(w, "tech").call("POST", "/admin/dx/qc/materials", c, body).status_code == 403
    m = _as(w, "qc").call("POST", "/admin/dx/qc/materials", c, body)
    assert m.status_code == 201 and m.json()["parameter_code"] == "HGB", m.text
    mid = m.json()["id"]
    runs = [12.6, 13.2, 13.25, 11.4]          # 2 s high, 2 s high again (2_2s), then 3.67 s low -> R_4s + 1_3s
    out = [_as(w, "tech").call("POST", "/admin/dx/qc/results", c, {"material_id": mid, "value": x}).json() for x in runs]
    assert [o["westgard_flags"] for o in out] == [[], ["1_2s"], ["1_2s", "2_2s"], ["1_2s", "1_3s", "R_4s"]]
    assert [o["accepted"] for o in out] == [True, True, False, False]
    chart = _as(w, "tech").call("GET", "/admin/dx/qc/chart", c, material_id=mid).json()
    assert chart["lines"]["mean"] == "12.5" and Decimal(chart["lines"]["+2s"]) == Decimal("13.1")
    assert Decimal(chart["lines"]["-3s"]) == Decimal("11.6") and len(chart["points"]) == 4
    assert chart["points"][-1]["flags"] == ["1_2s", "1_3s", "R_4s"] and chart["points"][-1]["accepted"] is False
    assert len(_as(w, "tech").call("GET", "/admin/dx/qc/results", c, material_id=mid).json()["results"]) == 4
    assert _as(w, "desk").call("GET", "/admin/dx/qc/chart", c, material_id=mid).status_code == 403
    _q(w["c"], "UPDATE dx_qc_materials SET valid_to = CURRENT_DATE - 1, valid_from = CURRENT_DATE - 9 WHERE id = %s", mid)
    late = _as(w, "tech").call("POST", "/admin/dx/qc/results", c, {"material_id": mid, "value": 12.5})
    assert late.status_code == 409 and late.json()["detail"]["code"] == "dx_qc_material_expired"


def test_module_off_refuses_bridge(lab):
    w = lab
    _q(w["c"], "UPDATE clinics SET features = features - 'dx_enabled', dx_state = 'OFF' WHERE id = %s", w["clinic"])
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache(w["clinic"])
    assert _signed(w, "POST", "/internal/dx/bridge/heartbeat").status_code == 401


def test_auth_unit_window(lab):
    """authenticate_bridge_request directly: exactly 300 s passes, 301 s fails."""
    from app.services import dx_bridge
    w, now = lab, datetime.now(timezone.utc)
    ts = (now - timedelta(seconds=300)).isoformat()
    sig = dx_bridge.sign_with_secret(w["agent"]["secret"], "POST", "/x", ts, b"")
    hdr = {"x-bridge-key-prefix": w["agent"]["key_prefix"], "x-bridge-timestamp": ts, "x-bridge-signature": sig}
    agent, clinic = asyncio.run(dx_bridge.authenticate_bridge_request(hdr, "POST", "/x", b"", now=now))
    assert agent["id"] == w["agent"]["id"] and str(clinic["id"]) == w["clinic"]
    with pytest.raises(HTTPException):
        asyncio.run(dx_bridge.authenticate_bridge_request(hdr, "POST", "/x", b"", now=now + timedelta(seconds=1)))
