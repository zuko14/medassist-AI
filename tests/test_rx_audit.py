"""Physical stock audits through the real /admin/rx routes on real Postgres (W8, RX-45/46).

A store keeper counts blind (no system quantity), a manager may look; a counter sale during the count is
carried by the ledger trigger so it never shows as a variance; review → reasons → post writes adjust
movements; a frozen store refuses counter sales; above the value threshold a second pharmacy manager signs in.
"""

import uuid

from app.routers.admin import hash_password
from tests.test_ipd_api_e2e import _code, _q, api  # noqa: F401  (fixture)
from tests.test_rx_pos import _new, _pay, pos  # noqa: F401  (fixture)


def _users(api, pos):  # noqa: F811
    U, c = api.U, pos["clinic"]
    return {"keeper": U("keeper", "staff", c, str(uuid.uuid4()), [], staff_role="STORE_KEEPER"),
            "mgr": U("mgr", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACY_MANAGER"),
            "pharmacist": pos["pharmacist"]}


def _batches(api, pos):  # noqa: F811
    return dict(_q(api.conn, "SELECT json_agg(json_build_array(batch_no, id)) FROM rx_batches WHERE clinic_id = %s",
                   pos["clinic"]))


def _bal(api, pos, batch):  # noqa: F811
    return _q(api.conn, "SELECT qty_units FROM rx_stock_balances WHERE store_id = %s AND batch_id = %s", pos["store"], batch)


def _sell(api, pos, units):  # noqa: F811
    api.as_(pos["pharmacist"])
    sale = _new(api, pos, sale_kind="otc")
    api.call("PUT", f"/admin/rx/sales/{sale['id']}/lines", pos["clinic"], [{"item_id": pos["items"]["DOLO650"], "qty_units": units}])
    detail = api.call("GET", f"/admin/rx/sales/{sale['id']}", pos["clinic"]).json()
    return _pay(api, pos, sale, [{"mode": "upi", "amount_paise": detail["net_paise"]}])


def test_blind_count_concurrent_sale_review_and_post(api, pos):  # noqa: F811
    c, u, b = pos["clinic"], _users(api, pos), _batches(api, pos)
    api.as_(u["keeper"])
    r = api.call("POST", "/admin/rx/audits", c, {"store_id": pos["store"], "scope": "full"})
    assert r.status_code == 201, r.text
    audit = r.json()
    assert audit["summary"] == {"lines": 4, "counted": 0, "uncounted": 4, "blind": True}
    assert all(ln["system_units_at_start"] is None for ln in audit["lines"])       # blind for the counter
    api.as_(u["mgr"])
    peek = api.call("GET", f"/admin/rx/audits/{audit['id']}", c, show_system="true").json()
    assert {ln["batch"]["batch_no"]: ln["system_units_at_start"] for ln in peek["lines"]} == \
        {"DNEAR": 40, "DLATE": 100, "CP1": 60, "AG1": 40}
    assert _sell(api, pos, 15).status_code == 200                                    # sales continue (not frozen)
    api.as_(u["keeper"])
    assert api.call("POST", f"/admin/rx/audits/{audit['id']}/review", c).status_code == 409   # nothing counted yet
    shelf = {"DNEAR": 25, "DLATE": 98, "CP1": 60, "AG1": 41}
    counts = [{"batch_id": b[k], "counted_units": v} for k, v in shelf.items()]
    assert api.call("PUT", f"/admin/rx/audits/{audit['id']}/counts", c, counts[:2]).json()["uncounted"] == 2
    assert api.call("PUT", f"/admin/rx/audits/{audit['id']}/counts", c, counts).json()["uncounted"] == 0   # re-sent: fine
    rev = api.call("POST", f"/admin/rx/audits/{audit['id']}/review", c).json()
    assert rev["status"] == "review" and rev["summary"]["lines_with_variance"] == 2           # the sale is not a variance
    var = {ln["batch"]["batch_no"]: ln["variance_units"] for ln in rev["lines"]}
    assert var == {"DNEAR": 0, "DLATE": -2, "CP1": 0, "AG1": 1}
    bad = api.call("POST", f"/admin/rx/audits/{audit['id']}/post", c, {"reasons": []})
    assert bad.status_code == 422 and _code(bad) == "rx_variance_reason_required"
    reasons = [{"line_id": ln["id"], "variance_reason": "breakage" if ln["variance_units"] < 0 else "found"}
               for ln in rev["lines"] if ln["variance_units"]]
    done = api.call("POST", f"/admin/rx/audits/{audit['id']}/post", c, {"reasons": reasons})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "posted" and done.json()["approved_by"] is None
    assert {k: _bal(api, pos, b[k]) for k in shelf} == shelf                        # the system is now the shelf


def test_frozen_store_refuses_counter_sales_until_cancelled(api, pos):  # noqa: F811
    c, u = pos["clinic"], _users(api, pos)
    api.as_(u["keeper"])
    audit = api.call("POST", "/admin/rx/audits", c, {"store_id": pos["store"], "scope": "rack", "rack": "A-1",
                                                     "freeze_sales": True}).json()
    assert audit["freeze_sales"] is True and audit["summary"]["lines"] == 2         # DOLO650 sits on rack A-1
    again = api.call("POST", "/admin/rx/audits", c, {"store_id": pos["store"], "scope": "full"})
    assert again.status_code == 409 and _code(again) == "rx_audit_already_open"
    r = _sell(api, pos, 1)
    assert r.status_code == 409 and _code(r) == "rx_store_frozen_for_audit"
    api.as_(u["keeper"])
    assert api.call("POST", f"/admin/rx/audits/{audit['id']}/cancel", c, {"reason": "Count moved to night"}).status_code == 200
    assert _sell(api, pos, 1).status_code == 200


def test_second_approver_above_threshold(api, pos):  # noqa: F811
    c, u, b = pos["clinic"], _users(api, pos), _batches(api, pos)
    _q(api.conn, "UPDATE clinics SET rx_settings = rx_settings || '{\"audit_approval_paise\": 100}'::jsonb WHERE id = %s", c)
    mgr_name, clerk_name = "aud_mgr_" + uuid.uuid4().hex[:6], "aud_clerk_" + uuid.uuid4().hex[:6]
    for name, role, pw in ((mgr_name, "PHARMACY_MANAGER", "Mgr#pass123"), (clerk_name, "PHARMACIST", "Clerk#pass1")):
        _q(api.conn, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, is_active) "
                     "VALUES (%s, %s, %s, 'staff', %s, true)", c, name, hash_password(pw), role)
    api.as_(u["keeper"])
    audit = api.call("POST", "/admin/rx/audits", c, {"store_id": pos["store"], "scope": "item_list",
                                                     "item_ids": [pos["items"]["AUG625"]]}).json()
    api.call("PUT", f"/admin/rx/audits/{audit['id']}/counts", c, [{"batch_id": b["AG1"], "counted_units": 30}])
    rev = api.call("POST", f"/admin/rx/audits/{audit['id']}/review", c).json()
    reasons = [{"line_id": rev["lines"][0]["id"], "variance_reason": "theft_suspected"}]
    url = f"/admin/rx/audits/{audit['id']}/post"
    alone = api.call("POST", url, c, {"reasons": reasons})
    assert alone.status_code == 403 and _code(alone) == "rx_audit_needs_approver"
    clerk = api.call("POST", url, c, {"reasons": reasons, "approver_username": clerk_name, "approver_password": "Clerk#pass1"})
    assert clerk.status_code == 403 and _code(clerk) == "rx_approver_not_authorised"
    wrong = api.call("POST", url, c, {"reasons": reasons, "approver_username": mgr_name, "approver_password": "nope"})
    assert wrong.status_code == 401
    ok = api.call("POST", url, c, {"reasons": reasons, "approver_username": mgr_name, "approver_password": "Mgr#pass123"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["approved_by_name"] == mgr_name and ok.json()["variance_value_paise"] > 100
    assert _bal(api, pos, b["AG1"]) == 30
