"""/admin/mi route pin + both global security matrices + who may import / search (SPEC §5)."""

import uuid

import pytest

from app.main import app
from tests.test_admin_super_admin_scope_matrix import ROUTES as SCOPE_ROUTES
from tests.test_ipd_api_e2e import api  # noqa: F401  (fixture)
from tests.test_mi_api_helpers import mi_clinic, upload
from tests.test_phase2_route_adversarial_matrix import ALL_ADMIN_ROUTES

MI_ROUTES = {
    ("GET", "/admin/mi/progress"), ("GET", "/admin/mi/batches"), ("GET", "/admin/mi/batches/{batch_id}"),
    ("GET", "/admin/mi/batches/{batch_id}/errors.csv"), ("GET", "/admin/mi/templates/{kind}.csv"),
    ("POST", "/admin/mi/{kind}/dry-run"), ("POST", "/admin/mi/{kind}/apply/{batch_id}"),
    ("DELETE", "/admin/mi/batches/{batch_id}"), ("GET", "/admin/mi/legacy-search"), ("POST", "/admin/mi/links"),
    ("GET", "/admin/mi/patients/{patient_id}/legacy"), ("POST", "/admin/mi/matches/refresh"),
    ("GET", "/admin/mi/matches"), ("POST", "/admin/mi/matches/{match_id}/confirm"),
    ("POST", "/admin/mi/matches/{match_id}/reject"), ("POST", "/admin/mi/dues/{due_id}/close"),
    ("GET", "/admin/mi/diagnosis-map"), ("PUT", "/admin/mi/diagnosis-map"), ("GET", "/admin/mi/chronic-candidates"),
}


def _app_routes():
    flat = []
    for r in app.routes:
        flat.extend(getattr(getattr(r, "original_router", None), "routes", None) or [r])
    return {(m, r.path) for r in flat if getattr(r, "path", "").startswith("/admin/mi/")
            for m in (getattr(r, "methods", None) or set()) if m not in ("HEAD", "OPTIONS")}


def test_route_set_is_pinned_and_in_both_matrices():
    assert _app_routes() == MI_ROUTES
    assert {r for r in ALL_ADMIN_ROUTES if r[1].startswith("/admin/mi/")} == MI_ROUTES
    assert {(m, p) for m, p in SCOPE_ROUTES if p.startswith("/admin/mi/")} == MI_ROUTES


@pytest.mark.parametrize("role,staff_role,grants,expect", [
    ("clinic_admin", None, (), (200, 200)),
    ("staff", "FRONT_DESK", ("DATA_IMPORT",), (200, 200)),
    ("staff", "FRONT_DESK", ("OPD_FRONT_DESK",), (403, 200)),
    ("staff", "CASHIER", ("OPD_BILLING",), (403, 403)),
    ("staff", "DOCTOR", ("OPD_CLINICAL",), (403, 403)),
])
def test_role_matrix(api, role, staff_role, grants, expect):  # noqa: F811
    clinic = mi_clinic(api)
    api.as_(api.U("u", role, clinic, str(uuid.uuid4()), list(grants), staff_role=staff_role))
    got = (upload(api, clinic, "payers", "Payer,Type\nA,TPA\n").status_code,
           api.call("GET", "/admin/mi/legacy-search", clinic, q="abc").status_code)
    assert got == expect
