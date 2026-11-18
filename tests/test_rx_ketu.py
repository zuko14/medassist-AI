"""Pharmacy OS × Kriya Ketu and the voice receptionist (P9; RX-60, RX-61; docs/pharmacy-os/KRIYA-KETU.md).

Stock answer on real Postgres: one confident match with strips + loose units, out of stock with the same-generic list
(information only, no advice text), ambiguous candidates, not found, and the nurse's view (own ward store counts +
"available at the pharmacy", never another store's count). Registry: every rx.* action is R0 or a hand-off, gated by
_rx_on; the eleven pharmacy pages are in the page table; medicine-advice questions get the fixed refusal. Voice
PHARMACY_REFILL: a request only on the caller's own active plan, otherwise the pharmacy's number, never stock.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest

from app.assistant import present, rules
from app.assistant.registry import PAGES, REGISTRY, WRITE_RISKS, _rx_on
from app.routers.assistant import _REJECTED
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m3 import _batch, _move
from tests.test_rx_ipd import ward  # noqa: F401  (fixture)
from tests.voice.test_dialog import Conv, FakeTools

ADVICE = ("instead", "can be given", "give", "dose", "safe")
RX_PAGES = {"rxsetup", "rxitems", "rxstock", "rxpos", "rxpurchase", "rxindents", "rxtransfers", "rxaudit",
            "rxregisters", "rxrefills", "rxdash"}


def _answer(api, w, q, user=None):  # noqa: F811
    api.as_(user or w["pharmacist"])
    r = api.call("GET", "/admin/rx/stock/answer", w["clinic"], q=q)
    assert r.status_code == 200, r.text
    return r.json()


def test_confident_match_out_of_stock_ambiguous_and_not_found(api, ward):  # noqa: F811
    w = ward                                          # MAIN holds Dolo 650: 40 (DNEAR) + 100 (DLATE), strips of 15
    a = _answer(api, w, "Dolo 650")
    assert (a["status"], a["resolved_by"]) == ("match", "trgm")
    [m] = a["matches"]
    assert m["item"]["brand_name"] == "Dolo 650" and m["generic"]["name"] == "Paracetamol"
    [st] = m["stores"]
    assert (st["qty_units"], st["packs_loose"]) == (140, {"packs": 9, "loose": 5}) and st["nearest_expiry"]
    assert m["substitutes_in_stock"] == []           # Calpol 650 shares the generic but has no stock
    assert _answer(api, w, "DOLO650")["resolved_by"] == "alias"              # exact item code: confident
    out = _answer(api, w, "Calpol 650")
    assert out["status"] == "out_of_stock"
    assert [(s["brand_name"], s["qty_units"]) for s in out["matches"][0]["substitutes_in_stock"]] == [("Dolo 650", 140)]
    assert not any(word in json.dumps(out).lower() for word in ("instead", "can be given", "dose"))
    amb = _answer(api, w, "paracetamol")             # two items share the generic: never a guess
    assert amb["status"] == "ambiguous" and {m["item"]["brand_name"] for m in amb["matches"]} == {"Dolo 650", "Calpol 650"}
    assert _answer(api, w, "xyzzy")["status"] == "not_found"
    assert _answer(api, w, " ")["status"] == "not_found"


def test_nurse_sees_own_ward_store_and_pharmacy_yes_no_only(api, ward):  # noqa: F811
    w, c = ward, ward["clinic"]
    ipd_ward = str(_q(api.conn, "SELECT id FROM ipd_wards WHERE clinic_id = %s LIMIT 1", c))
    wstore = str(_q(api.conn, "INSERT INTO rx_stores (clinic_id, code, name, store_type, ward_id, parent_store_id) "
                              "VALUES (%s, 'W1', 'Ward 1 store', 'ward_substore', %s, %s) RETURNING id",
                    c, ipd_ward, w["store"]))
    calpol = str(_q(api.conn, "SELECT id FROM rx_items WHERE clinic_id = %s AND item_code = 'CALPOL650'", c))
    _move(api.conn, c, wstore, _batch(api.conn, c, calpol, "CW1", mrp=3300), calpol, 12, "opening")
    nurse = w["u"]["nurse"]
    a = _answer(api, w, "Dolo 650", nurse)
    m = a["matches"][0]
    assert a["status"] == "match" and m["available_at_pharmacy"] is True
    assert m["stores"] == []                          # no ward stock of Dolo; the pharmacy's 140 is never shown
    assert '"qty_units": 140' not in json.dumps(a)
    b = _answer(api, w, "Calpol 650", nurse)["matches"][0]
    assert [(s["store_name"], s["qty_units"]) for s in b["stores"]] == [("Ward 1 store", 12)]
    assert b["available_at_pharmacy"] is False
    assert all(s["qty_units"] is None for s in b["substitutes_in_stock"])
    api.as_(w["u"]["billing"])                        # no RX grant, not a nurse
    assert api.call("GET", "/admin/rx/stock/answer", c, q="Dolo").status_code == 403


def test_presenter_states_composition_only_and_never_advice():
    out = present.rx_stock({"status": "out_of_stock", "query": "augmentin", "matches": [{
        "item": {"brand_name": "Augmentin 625 Duo", "base_unit": "tablet"},
        "generic": {"name": "Amoxicillin + Clavulanic acid", "strength": "500/125 mg"},
        "stores": [{"store_name": "OPD Counter", "qty_units": 0, "packs_loose": {"packs": 0, "loose": 0}}],
        "substitutes_in_stock": [{"brand_name": "Moxikind-CV 625", "qty_units": 30},
                                 {"brand_name": "Clavam 625", "qty_units": 12}]}]})
    assert out["message"] == ("Augmentin 625 Duo is out of stock at OPD Counter (0). Same composition in stock: "
                              "Moxikind-CV 625 (30 tablet), Clavam 625 (12 tablet).")
    assert not any(w in json.dumps(out).lower() for w in ("instead", "can be given", "dose"))
    match = present.rx_stock({"status": "match", "matches": [{"item": {"brand_name": "Dolo 650", "base_unit": "tablet"},
                                                              "stores": [{"store_name": "MAIN", "qty_units": 64,
                                                                          "packs_loose": {"packs": 6, "loose": 4}}]}]})
    assert "MAIN: 6 strips + 4 tablet (64 tablet)" in match["message"]
    assert [c.get("page") for c in match["cards"]] == ["rxstock", "rxpos"]          # Open in Stock, Sell (POS)
    amb = present.rx_stock({"status": "ambiguous", "matches": [{"item": {"brand_name": "A"}}, {"item": {"brand_name": "B"}}]})
    assert amb["message"].startswith("Did you mean A or B?")


def test_registry_reads_and_handoffs_only_all_gated_and_eleven_pages():
    rx = {k: s for k, s in REGISTRY.items() if k.startswith("rx.")}
    assert set(rx) == {"rx.stock_lookup", "rx.batch_expiry", "rx.low_stock", "rx.near_expiry", "rx.pending_indents",
                       "rx.my_ward_indent_status", "rx.refill_requests", "rx.today_summary", "rx.find_bill",
                       "rx.sell", "rx.dispense_indent", "rx.raise_indent", "rx.receive_grn", "rx.register_export"}
    assert all(s.feature is _rx_on for s in rx.values())
    assert not [k for k, s in rx.items() if s.risk in WRITE_RISKS]
    assert {s.risk for s in rx.values()} == {"R0", "H"}
    assert all(s.endpoint[0] == "GET" and s.endpoint[1].startswith("/admin/rx/") for s in rx.values() if s.risk == "R0")
    assert RX_PAGES <= set(PAGES) and PAGES["rxdash"] == "Pharmacy Dashboard"
    assert not _rx_on({"account_type": "tenant", "features": {}}) and _rx_on(
        {"account_type": "tenant", "rx_state": "WIND_DOWN", "features": {}})


@pytest.mark.parametrize("text", [
    "what can I give instead of augmentin", "is dolo safe with alcohol", "what dose for a 5 year old",
    "augmentin ke badle kya de sakte hain", "is crocin safe in pregnancy"])
def test_medicine_advice_gets_the_fixed_refusal(text):
    r = rules.route(text)
    assert (r.kind, r.error_code) == ("rejected", "medicine_advice"), r
    assert _REJECTED["medicine_advice"] == "Kriya doesn't advise on medicines. Please check with the treating doctor."


def test_stock_question_about_a_named_drug_is_not_refused_but_a_dose_question_is():
    assert rules.route("is augmentin 625 in stock").action_id == "rx.stock_lookup"
    assert rules.route("what dose of augmentin 625").kind == "rejected"


# ── voice receptionist ──────────────────────────────────────────────────────

class RefillTools(FakeTools):
    def __init__(self, result=None, **kw):
        super().__init__(**kw)
        self.result = result

    async def rx_refill_request(self):
        self.calls.append(("rx_refill_request",))
        return self.result


def test_voice_refill_intent_creates_request_or_routes_to_pharmacy():
    t = RefillTools({"status": "requested", "pharmacy_phone": "+914012345678"})
    r = Conv(t, lang="en-IN").say("I need a refill of my medicines")
    assert "refill request has reached the pharmacy" in r and ("rx_refill_request",) in t.calls
    r = Conv(RefillTools({"status": "no_plan", "pharmacy_phone": "+914012345678"}), lang="en-IN").say("repeat medicine please")
    assert "call our pharmacy at 4 0 1 2 3, 4 5 6 7 8" in r
    r = Conv(FakeTools(), lang="hi-IN").say("मुझे दवा दोबारा चाहिए")    # a clinic without the tool: the counter
    assert "फार्मेसी काउंटर" in r
    for reply in (r,):
        assert not any(ch.isdigit() for ch in reply)


@pytest.mark.asyncio
async def test_voice_refill_tool_only_for_the_callers_active_plan(api, ward):  # noqa: F811
    from app.voice.tools import CallContext, KriyaTools
    w, c = ward, ward["clinic"]
    _q(api.conn, "UPDATE clinics SET config = COALESCE(config, '{}'::jsonb) || '{\"pharmacy_phone\": \"+914012345678\"}' "
                 "WHERE id = %s", c)
    clinic = _q(api.conn, "SELECT row_to_json(c) FROM clinics c WHERE id = %s", c)
    pat = _q(api.conn, "SELECT row_to_json(p) FROM patients p WHERE clinic_id = %s LIMIT 1", c)
    item = w["item"]
    store = w["store"]
    batch = str(_q(api.conn, "SELECT id FROM rx_batches WHERE clinic_id = %s LIMIT 1", c))
    sale = str(_q(api.conn, "INSERT INTO rx_sales (clinic_id, store_id, sale_kind, walkin_name) VALUES (%s, %s, 'otc', "
                            "'Asha K') RETURNING id", c, store))
    si = str(_q(api.conn, "INSERT INTO rx_sale_items (clinic_id, sale_id, line_no, item_id, batch_id, qty_units) "
                          "VALUES (%s, %s, 1, %s, %s, 2) RETURNING id", c, sale, item, batch))

    def tools(phone):
        t = KriyaTools(CallContext(call_id=str(uuid.uuid4()), call_ref="CALL-T", clinic=clinic, branch_id=None,
                                   caller_phone=phone))
        t._trace = AsyncMock()
        return t
    assert (await tools(pat["phone"]).rx_refill_request())["status"] == "no_plan"
    plan = str(_q(api.conn, "INSERT INTO rx_refill_plans (clinic_id, patient_id, item_id, sale_item_id, units_per_day, "
                            "covered_until, remind_on, status, opt_in_by, opt_in_at) VALUES (%s, %s, %s, %s, 2, "
                            "CURRENT_DATE + 5, CURRENT_DATE, 'sent', %s, now()) RETURNING id",
                  c, pat["id"], item, si, str(uuid.uuid4())))
    out = await tools("+919000000001").rx_refill_request()          # another caller: their phone matches nothing
    assert out == {"status": "no_plan", "pharmacy_phone": "+914012345678"}
    assert (await tools(pat["phone"]).rx_refill_request())["status"] == "requested"
    assert _q(api.conn, "SELECT json_agg(json_build_array(channel, status)) FROM rx_refill_requests WHERE plan_id = %s",
              plan) == [["voice", "new"]]
    assert (await tools(pat["phone"]).rx_refill_request())["status"] == "already_requested"
    _q(api.conn, "UPDATE clinics SET features = features || '{\"pharmacy_enabled\": false}', rx_state = 'OFF' "
                 "WHERE id = %s", c)
    off = {**clinic, "features": {**clinic["features"], "pharmacy_enabled": False}, "rx_state": "OFF"}
    t = KriyaTools(CallContext(call_id="x", call_ref="CALL-T", clinic=off, branch_id=None, caller_phone=pat["phone"]))
    t._trace = AsyncMock()
    assert (await t.rx_refill_request())["status"] == "off"
    assert asyncio.iscoroutinefunction(KriyaTools.rx_refill_request)
