"""Protocol activation (ADR-CC-06; TEST-PLAN P2): a doctor with a registration number signs; super-admin never
signs; parameters outside the pack's bounds are refused; a new draft never applies by itself — only signing it
moves enrolled patients to the new parameters; an activation with enrolled patients cannot be retired."""


from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def test_params_outside_bounds_are_refused(care):  # noqa: F811
    api = care.api
    api.as_(care.u.admin)
    r = api.call("POST", "/admin/cc/activations", care.clinic, {"protocol_code": "t2dm", "params": {"hypo_mgdl": 40}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "cc_param_out_of_bounds"
    assert api.call("POST", "/admin/cc/activations", care.clinic, {"protocol_code": "nope"}).status_code == 404
    assert api.call("POST", "/admin/cc/activations", care.clinic,
                    {"protocol_code": "t2dm", "params": {"made_up": 1}}).status_code == 422
    assert api.call("POST", "/admin/cc/activations", care.clinic,
                    {"protocol_code": "t2dm", "extra": 1}).status_code == 422


def test_super_admin_and_admin_never_sign(care):  # noqa: F811
    api = care.api
    api.as_(care.u.admin)
    d = api.call("POST", "/admin/cc/activations", care.clinic, {"protocol_code": "t2dm"}).json()
    assert d["status"] == "draft"
    assert api.call("POST", f"/admin/cc/activations/{d['id']}/sign", care.clinic).status_code == 403
    api.as_(api.U("owner", "super_admin", None, "platform_owner_env", doctor_id=care.doctor))
    assert api.call("POST", f"/admin/cc/activations/{d['id']}/sign", care.clinic).status_code == 403


def test_new_draft_does_not_apply_until_signed(care):  # noqa: F811
    api, conn = care.api, care.conn
    e, _ = care.enroll()
    old = care.activation["id"]
    api.as_(care.u.admin)
    d = api.call("POST", "/admin/cc/activations", care.clinic, {"protocol_code": "t2dm", "params": {"hypo_mgdl": 75}}).json()
    assert _q(conn, "SELECT activation_id FROM cc_enrollments WHERE id = %s", e["id"]) == old
    assert _q(conn, "SELECT status FROM cc_protocol_activations WHERE id = %s", old) == "active"
    r = api.call("POST", f"/admin/cc/activations/{old}/retire", care.clinic, {"reason": "replace it"})
    assert r.status_code == 409                                             # patients are enrolled on it
    api.as_(care.u.doctor)
    s = api.call("POST", f"/admin/cc/activations/{d['id']}/sign", care.clinic)
    assert s.status_code == 200 and s.json()["params"]["hypo_mgdl"] == 75
    assert _q(conn, "SELECT status FROM cc_protocol_activations WHERE id = %s", old) == "retired"
    assert _q(conn, "SELECT activation_id FROM cc_enrollments WHERE id = %s", e["id"]) == d["id"]
    acts = api.call("GET", "/admin/cc/protocols", care.clinic).json()
    assert acts["can_sign"] is True and len(acts["packs"]) == 22
    assert sum(a["status"] == "active" for a in acts["activations"] if a["protocol_code"] == "t2dm") == 1


def test_enrollment_needs_a_signed_activation(care):  # noqa: F811
    from tests.test_ipd_api_e2e import _patient
    api = care.api
    api.as_(care.u.doctor)
    r = api.call("POST", "/admin/cc/enrollments", care.clinic, {"patient_id": _patient(care.conn, care.clinic),
                                                                  "protocol_code": "htn", "primary_doctor_id": care.doctor})
    assert r.status_code == 409 and "signed activation" in r.text


def test_coordinator_enrollment_waits_for_the_doctor(care):  # noqa: F811
    from tests.test_ipd_api_e2e import _patient
    api = care.api
    api.as_(care.u.coord)
    r = api.call("POST", "/admin/cc/enrollments", care.clinic, {"patient_id": _patient(care.conn, care.clinic),
                                                                  "protocol_code": "t2dm", "primary_doctor_id": care.doctor})
    assert r.status_code == 201 and r.json()["status"] == "awaiting_doctor"
    e = r.json()
    assert api.call("POST", f"/admin/cc/enrollments/{e['id']}/approve", care.clinic).status_code == 403
    r = api.call("POST", f"/admin/cc/enrollments/{e['id']}/consent/counter", care.clinic, {"notice_version": "cc-notice-1"})
    assert r.status_code == 409                                             # not doctor-approved: cannot start
    api.as_(care.u.doctor)
    ok = api.call("POST", f"/admin/cc/enrollments/{e['id']}/approve", care.clinic)
    assert ok.status_code == 200 and ok.json()["status"] == "invited"
    assert api.call("POST", f"/admin/cc/enrollments/{e['id']}/approve", care.clinic).status_code == 409
