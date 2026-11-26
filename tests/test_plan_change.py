"""Owner plan change (PUT /platform/clinics/{id}/plan) on a real Postgres through the real routes: modules the new
plan cannot have are switched off through the normal toggle, plan-feature overrides reset, starter lists loaded, the
new plan's recommended modules optionally switched on — and a refused change writes nothing."""

import json
import uuid

import pytest

from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _clinic(api, plan, modules=(), overrides=None, name=None):  # noqa: F811
    name = name or f"Plan Test {uuid.uuid4().hex[:6]}"
    cid = str(_q(api.conn, "INSERT INTO clinics (name, whatsapp_number, plan, is_active, features) VALUES "
                           "(%s, %s, %s, true, %s::jsonb) RETURNING id",
                 name, "+9197" + uuid.uuid4().hex[:8], plan, json.dumps(overrides or {})))
    if modules:
        r = api.client.post(f"/platform/clinics/{cid}/template/apply", json={"modules": list(modules)})
        assert r.status_code == 200 and r.json()["failed"] == 0, r.text
    return cid, name


def _feats(api, cid):  # noqa: F811
    return _q(api.conn, "SELECT features FROM clinics WHERE id = %s", cid)


def _change(api, cid, name, plan, **kw):  # noqa: F811
    return api.client.put(f"/platform/clinics/{cid}/plan", json={"plan": plan, "confirm_name": name, **kw})


def test_polyclinic_to_diagnostic_switches_off_everything_the_lab_plan_cannot_have(api):  # noqa: F811
    cid, name = _clinic(api, "polyclinic", ("opd_enabled", "pharmacy_enabled", "dx_enabled", "care_enabled",
                                            "kriya_ketu"), overrides={"lab_reports": False})
    preview = api.client.get(f"/platform/clinics/{cid}/plan-change-preview", params={"plan": "diagstream"}).json()
    assert set(preview["switch_off"]) == {"opd_enabled", "pharmacy_enabled", "care_enabled"}
    assert preview["overrides_reset"] == ["lab_reports"]
    assert _q(api.conn, "SELECT plan FROM clinics WHERE id = %s", cid) == "polyclinic"      # preview writes nothing

    r = _change(api, cid, name, "diagstream")
    assert r.status_code == 200, r.text
    out = r.json()
    assert all(x["ok"] for x in out["switched_off"]), out["switched_off"]
    f = _feats(api, cid)
    assert _q(api.conn, "SELECT plan FROM clinics WHERE id = %s", cid) == "diagstream"
    assert f.get("opd_enabled") is False and f.get("pharmacy_enabled") is False and f.get("care_enabled") is False
    assert f.get("dx_enabled") is True and f.get("kriya_ketu") is True and "lab_reports" not in f
    assert _q(api.conn, "SELECT opd_state FROM clinics WHERE id = %s", cid) == "DISABLED"
    assert _q(api.conn, "SELECT count(*) FROM admin_audit_logs WHERE action = 'change_clinic_plan' "
                        "AND resource_id = %s", cid) == 1


def test_multispecialty_to_eye_keeps_eye_and_drops_other_packs_and_loads_eye_starters(api):  # noqa: F811
    cid, name = _clinic(api, "multispecialty", ("opd_enabled", "specialty_os", "optical_enabled", "derma_os"))
    r = _change(api, cid, name, "eye", apply_recommended=True)
    assert r.status_code == 200, r.text
    f = _feats(api, cid)
    assert f.get("derma_os") is False and f.get("optical_enabled") is True and f.get("specialty_os") is True
    assert f.get("pharmacy_enabled") is True                           # eye template recommends the optical/pharmacy
    assert _q(api.conn, "SELECT count(*) FROM specialty_treatments WHERE clinic_id = %s", cid) > 0
    assert all(x["ok"] for x in r.json()["applied"]), r.json()["applied"]


def test_polyclinic_to_standalone_pharmacy_keeps_pharmacy_os_running_without_opd(api):  # noqa: F811
    cid, name = _clinic(api, "polyclinic", ("opd_enabled", "pharmacy_enabled"))
    r = _change(api, cid, name, "pharmacy")
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["switched_off"]), r.text
    f = _feats(api, cid)
    assert f.get("opd_enabled") is False and f.get("pharmacy_enabled") is True
    assert _q(api.conn, "SELECT rx_state FROM clinics WHERE id = %s", cid) == "ACTIVE"


def test_refused_changes_write_nothing(api, monkeypatch):  # noqa: F811
    cid, name = _clinic(api, "polyclinic", ("opd_enabled", "pharmacy_enabled"))
    before = (_q(api.conn, "SELECT plan FROM clinics WHERE id = %s", cid), _feats(api, cid))
    assert _change(api, cid, "wrong name", "eye").status_code == 400
    assert _change(api, cid, name, "polyclinic").status_code == 400                  # same plan
    assert _change(api, cid, name, "not_a_plan").status_code == 400

    from app.routers import platform

    async def blockers(clinic_id, keys):
        return [{"key": "pharmacy_enabled", "preview": {"blocking": True}}] if "pharmacy_enabled" in keys else []
    monkeypatch.setattr(platform, "_wind_down_blockers", blockers)
    # diagstream drops OPD while pharmacy still has open work -> refused even with the wind-down confirmation
    assert _change(api, cid, name, "diagstream", confirm_wind_down=True).status_code == 409
    after = (_q(api.conn, "SELECT plan FROM clinics WHERE id = %s", cid), _feats(api, cid))
    assert after == before
    assert api.client.put(f"/platform/clinics/{uuid.uuid4()}/plan",
                          json={"plan": "eye", "confirm_name": "x"}).status_code == 404


@pytest.mark.parametrize("plan", ["soloclinic", "diagstream", "diagbooking", "essential", "polyclinic", "enterprise",
                                  "derma", "eye", "dental", "ivf", "multispecialty", "womenchild", "pharmacy"])
def test_every_plan_is_reachable_and_its_recommended_set_applies_cleanly(api, plan):  # noqa: F811
    start = "pharmacy" if plan != "pharmacy" else "soloclinic"
    cid, name = _clinic(api, start)
    r = _change(api, cid, name, plan, apply_recommended=True)
    assert r.status_code == 200, r.text
    assert all(x["ok"] for x in (r.json()["applied"] or [])), r.json()["applied"]
