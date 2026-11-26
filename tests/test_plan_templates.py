"""Suggested module template per plan (owner console): every plan has one, every key is an owner opt-in flag, the
apply order respects dependencies, and applying one through the real route on a real Postgres switches the ticked
modules on — nothing else, nothing for another clinic."""

import json
import uuid

from app.services.tenant import OPT_IN_FEATURES, PLAN_FEATURES, PLAN_MODULE_TEMPLATES, TEMPLATE_APPLY_ORDER
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def test_every_plan_has_a_template_of_opt_in_flags():
    assert set(PLAN_MODULE_TEMPLATES) == set(PLAN_FEATURES)
    for plan, tpl in PLAN_MODULE_TEMPLATES.items():
        keys = list(tpl["recommended"]) + list(tpl["optional"])
        assert tpl["summary"] and len(keys) == len(set(keys)), plan
        assert set(keys) <= set(OPT_IN_FEATURES) and set(keys) <= set(TEMPLATE_APPLY_ORDER), plan


def test_plan_shapes():
    rec = {p: set(t["recommended"]) for p, t in PLAN_MODULE_TEMPLATES.items()}
    assert {"opd_enabled", "ipd_enabled", "pharmacy_enabled", "dx_enabled"} <= rec["multispecialty"]
    assert "ipd_enabled" not in rec["polyclinic"] and {"opd_enabled", "pharmacy_enabled"} <= rec["polyclinic"]
    for diag in ("diagstream", "diagbooking"):    # no OPD on a lab plan: diagnostics end to end only
        assert "dx_enabled" in rec[diag] and not rec[diag] & {"opd_enabled", "ipd_enabled", "pharmacy_enabled"}
    assert rec["pharmacy"] >= {"pharmacy_enabled"} and "opd_enabled" not in rec["pharmacy"]


def test_apply_order_puts_dependencies_first():
    i = TEMPLATE_APPLY_ORDER.index
    for dep, later in (("opd_enabled", "ipd_enabled"), ("opd_enabled", "pharmacy_enabled"),
                       ("opd_enabled", "care_enabled"), ("specialty_os", "optical_enabled"),
                       ("specialty_os", "paediatrics_enabled"), ("dx_enabled", "revenue_guard"),
                       ("pharmacy_enabled", "revenue_guard")):
        assert i(dep) < i(later)


def _clinic(conn, plan):
    return str(_q(conn, "INSERT INTO clinics (name, whatsapp_number, plan, is_active, features) VALUES "
                        "('Template Hospital', %s, %s, true, '{}'::jsonb) RETURNING id",
                  "+9196" + uuid.uuid4().hex[:8], plan))


def test_apply_multispecialty_template_end_to_end(api):  # noqa: F811
    clinic, other = _clinic(api.conn, "multispecialty"), _clinic(api.conn, "multispecialty")
    view = api.client.get(f"/platform/clinics/{clinic}/template").json()
    assert view["plan"] == "multispecialty" and all(m["tier"] in ("recommended", "optional") for m in view["modules"])
    rec = [m["key"] for m in view["modules"] if m["tier"] == "recommended"]

    bad = api.client.post(f"/platform/clinics/{clinic}/template/apply", json={"modules": ["corporate_health"]})
    assert bad.status_code == 400                     # not in this plan's template

    r = api.client.post(f"/platform/clinics/{clinic}/template/apply", json={"modules": list(reversed(rec))})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["failed"] == 0, out["results"]
    feats = json.loads(json.dumps(_q(api.conn, "SELECT features FROM clinics WHERE id = %s", clinic)))
    assert all(feats.get(k) is True for k in rec)
    assert not set(feats) - set(rec)                  # nothing beyond what was ticked
    assert _q(api.conn, "SELECT opd_state FROM clinics WHERE id = %s", clinic) == "CONFIGURING"
    assert _q(api.conn, "SELECT ipd_state FROM clinics WHERE id = %s", clinic) == "ACTIVE"
    assert out["opd_setup"]["total"] > 0 and out["opd_setup"]["done"] < out["opd_setup"]["total"]
    assert _q(api.conn, "SELECT features FROM clinics WHERE id = %s", other) == {}

    again = api.client.post(f"/platform/clinics/{clinic}/template/apply", json={"modules": rec}).json()
    assert again["applied"] == 0 and all(x.get("already_on") for x in again["results"])


def test_apply_diagnostic_template(api):  # noqa: F811
    clinic = _clinic(api.conn, "diagstream")
    r = api.client.post(f"/platform/clinics/{clinic}/template/apply",
                        json={"modules": ["dx_enabled", "revenue_guard", "corporate_health"]})
    assert r.status_code == 200 and r.json()["failed"] == 0, r.text
    assert _q(api.conn, "SELECT dx_state FROM clinics WHERE id = %s", clinic) == "ACTIVE"
    assert api.client.get(f"/platform/clinics/{uuid.uuid4()}/template").status_code == 404
