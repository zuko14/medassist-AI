"""Every /admin route, driven as a super_admin, in both directions.

The tenant fix made enforce_clinic_access() fail closed, which trades a data
leak for a possible feature break. This matrix pins both halves of that trade
across all ~90 admin routes, so neither can regress silently:

  LEAK HALF     — super_admin with NO clinic named must never get 200/201.
                  Before the fix, "no clinic" meant "every clinic": the doctor
                  list returned every tenant's doctors and DELETE removed a row
                  by id with no tenant predicate (KRIYA-TENANT-001, 2026-09-01).

  FEATURE HALF  — super_admin WITH a clinic named must never be turned away
                  for lack of scope. This is the half that catches a silent
                  feature failure: a route whose clinic_id never reaches
                  enforce_clinic_access (a Form-only parameter, a raw fetch in
                  the panel that skips withScope, a handler that forgets to
                  pass it through) would answer "No clinic selected" forever,
                  and the leak half alone would happily call that a pass.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import AdminUser, verify_credentials

CLINIC = "11111111-1111-1111-1111-111111111111"
DUMMY = "99999999-9999-9999-9999-999999999999"

#: Routes that carry no tenant scope by design. Each is either pre-auth or
#: returns only the caller's own identity / a static asset.
NO_SCOPE_ROUTES = {
    ("POST", "/admin/login"),
    ("POST", "/admin/logout"),
    ("GET", "/admin/me"),
    ("PUT", "/admin/change-password"),
    ("PUT", "/admin/change-username"),  # self-identity, like change-password
    ("GET", "/admin/clinics"),           # the picker itself: how a scope is chosen
    ("GET", "/admin/connectors/types"),  # global connector catalogue
    ("GET", "/admin/lab-tests/csv-template"),
    ("GET", "/admin"),
    ("GET", "/admin-panel"),
}

SUPER = AdminUser(
    username="kriyaai_superadmin", role="super_admin", clinic_id=None, user_id="env"
)


def _admin_routes():
    all_routes = []
    for r in app.routes:
        if hasattr(r, "original_router"):
            all_routes.extend(getattr(r.original_router, "routes", []))
        else:
            all_routes.append(r)
    seen = []
    for r in all_routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", set()) or set()
        if not path.startswith("/admin"):
            continue
        for m in sorted(methods):
            if m in ("OPTIONS", "HEAD"):
                continue
            if (m, path) in NO_SCOPE_ROUTES:
                continue
            seen.append((m, path))
    return sorted(set(seen))


ROUTES = _admin_routes()


def _concrete(path):
    for token, value in (
        ("{staff_id}", DUMMY), ("{doctor_id}", DUMMY), ("{doctor_name}", "Dr. Test"),
        ("{test_id}", DUMMY), ("{leave_id}", DUMMY), ("{holiday_date}", "2026-12-25"),
        ("{appointment_id}", DUMMY), ("{report_id}", DUMMY), ("{prescription_id}", DUMMY),
        ("{booking_id}", DUMMY), ("{connector_id}", DUMMY), ("{failed_report_id}", DUMMY),
        ("{branch_id}", DUMMY), ("{notification_id}", DUMMY), ("{clinic_id}", CLINIC),
    ):
        path = path.replace(token, value)
    return path


def _call(client, method, path, params):
    body = {"name": "X", "phone": "9876543210", "doctor_name": "Dr. Test"}
    fn = {
        "GET": lambda: client.get(path, params=params),
        "DELETE": lambda: client.delete(path, params=params),
        "POST": lambda: client.post(path, params=params, json=body),
        "PUT": lambda: client.put(path, params=params, json=body),
        "PATCH": lambda: client.patch(path, params=params, json=body),
    }.get(method)
    return fn() if fn else None


@pytest.fixture
def client():
    app.dependency_overrides[verify_credentials] = lambda: SUPER
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(verify_credentials, None)


def test_there_are_admin_routes_to_check():
    """Guard against the matrix silently testing nothing."""
    assert len(ROUTES) > 50, f"only {len(ROUTES)} admin routes discovered"


@pytest.mark.parametrize("method,path", ROUTES)
def test_unscoped_super_admin_never_succeeds(client, method, path):
    """LEAK HALF: no clinic named -> never 200/201."""
    resp = _call(client, method, _concrete(path), None)
    if resp is None:
        pytest.skip(f"unsupported method {method}")
    assert resp.status_code not in (200, 201), (
        f"TENANT LEAK: {method} {path} succeeded for a super_admin who named no "
        f"clinic. An unscoped /admin call must be refused, never widened to "
        f"every tenant. Body: {resp.text[:300]}"
    )


@pytest.mark.parametrize("method,path", ROUTES)
def test_scoped_super_admin_is_not_refused_for_scope(client, method, path):
    """FEATURE HALF: clinic named -> never rejected for lack of scope.

    A failure here means clinic_id does not actually reach
    enforce_clinic_access on this route, so the feature is dead for any
    account without its own clinic.
    """
    resp = _call(client, method, _concrete(path), {"clinic_id": CLINIC})
    if resp is None:
        pytest.skip(f"unsupported method {method}")
    assert not (
        resp.status_code == 400 and "No clinic selected" in resp.text
    ), (
        f"SILENT FEATURE FAILURE: {method} {path} still reports 'No clinic "
        f"selected' after ?clinic_id was supplied. The parameter is not "
        f"reaching enforce_clinic_access (declared as Form-only? not passed "
        f"through by the handler?). Body: {resp.text[:300]}"
    )
