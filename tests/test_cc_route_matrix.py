"""/admin/cc route matrix (docs/continuous-care/API.md, SECURITY.md §2; CG2).

1. The route set is pinned: a new /admin/cc route is added here (and to the role matrix) in the same commit.
2. Every /admin/cc route is in both global security matrices (they discover routes from the app).
3. Role matrix on real Postgres: who may read, coordinate, approve and administer; branch pin; tenant scope.
"""

import uuid

import pytest

from app.main import app
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_admin_super_admin_scope_matrix import ROUTES as SCOPE_ROUTES
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_phase2_route_adversarial_matrix import ALL_ADMIN_ROUTES

CC_ROUTES = {
    ("GET", "/admin/cc/setup"), ("GET", "/admin/cc/settings"), ("PUT", "/admin/cc/settings"),
    ("POST", "/admin/cc/templates/submit"), ("GET", "/admin/cc/templates"),
    ("GET", "/admin/cc/measure-sources"), ("POST", "/admin/cc/measure-sources"),
    ("POST", "/admin/cc/measure-sources/seed"), ("DELETE", "/admin/cc/measure-sources/{source_id}"),
    ("GET", "/admin/cc/cards"), ("PUT", "/admin/cc/cards/{card_key}/{lang}"), ("GET", "/admin/cc/notice"),
    ("GET", "/admin/cc/protocols"), ("POST", "/admin/cc/activations"),
    ("POST", "/admin/cc/activations/{activation_id}/sign"), ("POST", "/admin/cc/activations/{activation_id}/retire"),
    ("GET", "/admin/cc/enrollments"), ("POST", "/admin/cc/enrollments"), ("GET", "/admin/cc/enrollments/{enrollment_id}"),
    ("POST", "/admin/cc/enrollments/{enrollment_id}/approve"), ("POST", "/admin/cc/enrollments/{enrollment_id}/optin"),
    ("POST", "/admin/cc/enrollments/{enrollment_id}/invite"),
    ("POST", "/admin/cc/enrollments/{enrollment_id}/consent/counter"),
    ("POST", "/admin/cc/enrollments/{enrollment_id}/pause"), ("POST", "/admin/cc/enrollments/{enrollment_id}/resume"),
    ("POST", "/admin/cc/enrollments/{enrollment_id}/end"), ("GET", "/admin/cc/enrollments/{enrollment_id}/plan/preview"),
    ("POST", "/admin/cc/enrollments/{enrollment_id}/plan/rebuild"),
    ("GET", "/admin/cc/enrollments/{enrollment_id}/observations"),
    ("POST", "/admin/cc/enrollments/{enrollment_id}/observations"),
    ("POST", "/admin/cc/observations/{observation_id}/correct"), ("POST", "/admin/cc/discharge-followups"),
    ("GET", "/admin/cc/radar"), ("POST", "/admin/cc/radar/rebuild"), ("POST", "/admin/cc/radar/{row_id}/dismiss"),
    ("GET", "/admin/cc/cases"), ("GET", "/admin/cc/cases/{case_id}"), ("POST", "/admin/cc/cases/{case_id}/acknowledge"),
    ("POST", "/admin/cc/cases/{case_id}/assign"), ("POST", "/admin/cc/cases/{case_id}/note"),
    ("POST", "/admin/cc/cases/{case_id}/escalate"), ("POST", "/admin/cc/cases/{case_id}/close"),
    ("POST", "/admin/cc/cases/{case_id}/request-doctor"), ("GET", "/admin/cc/inbox"),
    ("POST", "/admin/cc/cases/{case_id}/actions"), ("GET", "/admin/cc/actions"), ("GET", "/admin/cc/outcomes"),
    ("GET", "/admin/cc/outcomes/export.csv"),
}


def _app_cc_routes() -> set[tuple[str, str]]:
    flat = []
    for r in app.routes:
        flat.extend(getattr(getattr(r, "original_router", None), "routes", None) or [r])
    return {(m, r.path) for r in flat if getattr(r, "path", "").startswith("/admin/cc/")
            for m in (getattr(r, "methods", None) or set()) if m not in ("HEAD", "OPTIONS")}


def test_route_set_is_pinned():
    assert len(CC_ROUTES) == 48
    assert _app_cc_routes() == CC_ROUTES


def test_every_cc_route_is_in_both_security_matrices():
    assert {r for r in ALL_ADMIN_ROUTES if r[1].startswith("/admin/cc/")} == CC_ROUTES
    assert {(m, p) for m, p in SCOPE_ROUTES if p.startswith("/admin/cc/")} == CC_ROUTES


def test_every_body_model_forbids_extra_keys():
    from pydantic import BaseModel

    from app.routers import cc as route
    models = [v for v in vars(route).values() if isinstance(v, type) and issubclass(v, BaseModel) and v is not BaseModel]
    assert len(models) >= 15
    for m in models:
        assert m.model_config.get("extra") == "forbid", m.__name__
    fields = set(route.ActionParams.model_fields) | set(route.TakeActionIn.model_fields)
    assert not fields & {"drug", "drug_name", "dose", "dosage", "medicine", "frequency", "strength", "prescription"}


# (user key) -> setup (CC_ADMIN), enrollments (CC_VIEW), radar rebuild (CC_COORDINATE), inbox (CC_APPROVE),
#               settings PUT (CC_ADMIN), protocols (VIEW|ADMIN|APPROVE)
MATRIX = {
    "admin": (200, 200, 200, 403, 200, 200),
    "doctor": (403, 200, 403, 200, 403, 200),
    "coord": (403, 200, 200, 403, 403, 200),
    "nurse": (403, 200, 403, 403, 403, 200),
    "desk": (403, 403, 403, 403, 403, 403),
    "cashier": (403, 403, 403, 403, 403, 403),
}


@pytest.mark.parametrize("who", sorted(MATRIX))
def test_role_matrix(care, who):  # noqa: F811
    api, clinic = care.api, care.clinic
    api.as_(getattr(care.u, who))
    got = (api.call("GET", "/admin/cc/setup", clinic).status_code,
           api.call("GET", "/admin/cc/enrollments", clinic).status_code,
           api.call("POST", "/admin/cc/radar/rebuild", clinic).status_code,
           api.call("GET", "/admin/cc/inbox", clinic).status_code,
           api.call("PUT", "/admin/cc/settings", clinic, {"radar_threshold": 35}).status_code,
           api.call("GET", "/admin/cc/protocols", clinic).status_code)
    assert got == MATRIX[who]


def test_only_a_doctor_with_a_registration_number_signs(care):  # noqa: F811
    api, clinic, conn = care.api, care.clinic, care.conn
    api.as_(care.u.admin)
    draft = api.call("POST", "/admin/cc/activations", clinic, {"protocol_code": "thyroid"}).json()
    api.as_(care.u.doctor)
    r = api.call("POST", f"/admin/cc/activations/{draft['id']}/sign", clinic)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "cc_cards_unapproved"   # pack cards approved first
    api.as_(care.u.admin)
    assert api.call("PUT", "/admin/cc/cards/empty_stomach_timing/en", clinic,
                    {"body": "Take it on an empty stomach, 30 minutes before food."}).status_code == 200
    r = api.call("POST", f"/admin/cc/activations/{draft['id']}/sign", clinic)
    assert r.status_code == 403                                              # clinic admin: no doctor row
    api.as_(api.U("owner", "super_admin", None, "platform_owner_env"))
    assert api.call("POST", f"/admin/cc/activations/{draft['id']}/sign", clinic).status_code == 403
    nodoc = str(_q(conn, "INSERT INTO doctors (clinic_id, name, department, specialization, is_active) VALUES (%s, 'Dr. New', "
                         "'Medicine', 'Physician', true) RETURNING id", clinic))
    api.as_(api.U("drnew", "staff", clinic, str(uuid.uuid4()), [], staff_role="DOCTOR", doctor_id=nodoc))
    r = api.call("POST", f"/admin/cc/activations/{draft['id']}/sign", clinic)
    assert r.status_code == 403 and "registration number" in r.text
    api.as_(care.u.doctor)
    ok = api.call("POST", f"/admin/cc/activations/{draft['id']}/sign", clinic)
    assert ok.status_code == 200 and ok.json()["signer_snapshot"]["registration_number"] == "TSMC/2012/777"
    assert api.call("POST", f"/admin/cc/activations/{draft['id']}/sign", clinic).status_code == 409


def test_cross_tenant_is_refused(care):  # noqa: F811
    api = care.api
    e, _ = care.enroll()
    other = _clinic(api.conn)
    assert api.owner_toggle(other, "care_enabled", True).status_code == 200
    api.as_(api.U("admin", "clinic_admin", other, str(uuid.uuid4())))
    assert api.call("GET", "/admin/cc/enrollments", care.clinic).status_code == 403
    assert api.call("GET", f"/admin/cc/enrollments/{e['id']}", other).status_code == 404
    assert api.call("POST", f"/admin/cc/enrollments/{e['id']}/pause", other, {"reason": "not mine"}).status_code == 404
    listed = api.call("GET", "/admin/cc/enrollments", other).json()
    assert listed["enrollments"] == [] and listed["total"] == 0


def test_branch_pinned_login_sees_its_branch_only(care):  # noqa: F811
    api, clinic, conn = care.api, care.clinic, care.conn
    b1 = str(_q(conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'North') RETURNING id", clinic))
    b2 = str(_q(conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'South') RETURNING id", clinic))
    e1, _ = care.enroll()
    e2, _ = care.enroll()
    _q(conn, "UPDATE cc_enrollments SET branch_id = %s WHERE id = %s", b1, e1["id"])
    _q(conn, "UPDATE cc_enrollments SET branch_id = %s WHERE id = %s", b2, e2["id"])
    api.as_(api.U("c2", "staff", clinic, str(uuid.uuid4()), [], staff_role="CARE_COORDINATOR", branch_id=b1))
    ids = [x["id"] for x in api.call("GET", "/admin/cc/enrollments", clinic).json()["enrollments"]]
    assert ids == [e1["id"]]
    assert api.call("GET", f"/admin/cc/enrollments/{e2['id']}", clinic).status_code == 404
    assert api.call("PUT", "/admin/cc/settings", clinic, {"radar_threshold": 40}).status_code == 403
