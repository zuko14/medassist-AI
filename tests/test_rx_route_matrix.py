"""/admin/rx route matrix (docs/pharmacy-os/SECURITY.md §3.4, TEST-PLAN §5).

1. The route count is pinned: a new /admin/rx route must be added here (and so to the role
   matrix below) in the same commit.
2. Every /admin/rx route is in both global security matrices (cross-tenant rejection and
   the super-admin leak / feature halves), which discover routes from the app.
3. Role matrix on real Postgres: who may read, change settings and manage stores.
"""

import uuid

import pytest

from app.main import app
from tests.test_admin_super_admin_scope_matrix import ROUTES as SCOPE_ROUTES
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_phase2_route_adversarial_matrix import ALL_ADMIN_ROUTES

RX_ROUTES = {
    ("GET", "/admin/rx/setup"), ("GET", "/admin/rx/settings"), ("PATCH", "/admin/rx/settings"),
    ("GET", "/admin/rx/stores"), ("POST", "/admin/rx/stores"), ("PATCH", "/admin/rx/stores/{store_id}"),
    # P2 catalogue (135)
    ("GET", "/admin/rx/manufacturers"), ("POST", "/admin/rx/manufacturers"),
    ("GET", "/admin/rx/generics"), ("POST", "/admin/rx/generics"),
    ("GET", "/admin/rx/items"), ("POST", "/admin/rx/items"), ("GET", "/admin/rx/items/resolve"),
    ("POST", "/admin/rx/items/import/dry-run"), ("POST", "/admin/rx/items/import/apply"),
    ("GET", "/admin/rx/items/{item_id}"), ("PATCH", "/admin/rx/items/{item_id}"),
    ("POST", "/admin/rx/items/{item_id}/barcodes"),
    # P2 stock (136)
    ("GET", "/admin/rx/stock"), ("GET", "/admin/rx/batches"), ("GET", "/admin/rx/expiry"), ("GET", "/admin/rx/reorder"),
    ("POST", "/admin/rx/batches/{batch_id}/block"), ("POST", "/admin/rx/batches/{batch_id}/unblock"),
    ("POST", "/admin/rx/write-offs"), ("POST", "/admin/rx/stock/opening-import"),
}


def _app_rx_routes() -> set[tuple[str, str]]:
    flat = []
    for r in app.routes:
        flat.extend(getattr(getattr(r, "original_router", None), "routes", None) or [r])
    return {(m, r.path) for r in flat if getattr(r, "path", "").startswith("/admin/rx")
            for m in (getattr(r, "methods", None) or set()) if m not in ("HEAD", "OPTIONS")}


def test_route_count_is_pinned():
    assert _app_rx_routes() == RX_ROUTES


def test_every_rx_route_is_in_both_security_matrices():
    adversarial = {r for r in ALL_ADMIN_ROUTES if r[1].startswith("/admin/rx")}
    scope = {(m, p) for m, p in SCOPE_ROUTES if p.startswith("/admin/rx")}
    assert adversarial == RX_ROUTES and scope == RX_ROUTES


# (role, stored grants) -> expected status for: read setup, change settings, add a store
MATRIX = {
    ("PHARMACY_MANAGER", ()): (200, 200, 201),
    ("PHARMACIST", ()): (200, 403, 403),
    ("STORE_KEEPER", ()): (200, 403, 403),
    ("PHARMACY_OPERATOR", ()): (200, 403, 403),
    ("FRONT_DESK", ("RX_STORE_MANAGE",)): (403, 403, 201),     # a stored grant is honoured on its own
    ("NURSE", ("IPD_NURSING",)): (403, 403, 403),
    ("CASHIER", ("OPD_BILLING",)): (403, 403, 403),
    ("DOCTOR", ("OPD_CLINICAL",)): (403, 403, 403),
}


@pytest.mark.parametrize("role,grants", sorted(MATRIX))
def test_role_matrix(api, role, grants):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    user = api.U(role.lower(), "staff", clinic, str(uuid.uuid4()), list(grants), staff_role=role)
    api.as_(user)
    read = api.call("GET", "/admin/rx/setup", clinic).status_code
    version = _q(api.conn, "SELECT COALESCE((rx_settings ->> 'version')::int, 0) FROM clinics WHERE id = %s", clinic)
    settings = api.call("PATCH", "/admin/rx/settings", clinic, {"version": version, "return_days": 5}).status_code
    store = api.call("POST", "/admin/rx/stores", clinic,
                     {"code": "T" + uuid.uuid4().hex[:6].upper(), "name": "OT tray", "store_type": "ot_tray"}).status_code
    assert (read, settings, store) == MATRIX[(role, grants)]


# P2: (role, stored grants) -> expected status for: list items, add a generic, write off (bad batch -> 404 when allowed)
P2_MATRIX = {
    ("PHARMACY_MANAGER", ()): (200, 201, 404),
    ("PHARMACIST", ()): (200, 403, 403),
    ("STORE_KEEPER", ()): (200, 403, 404),
    ("PHARMACY_OPERATOR", ()): (200, 403, 403),
    ("FRONT_DESK", ("RX_CATALOGUE",)): (200, 201, 403),       # the drug-master grant reads and edits the catalogue
    ("FRONT_DESK", ("RX_ADJUST",)): (403, 403, 404),
    ("NURSE", ("IPD_NURSING",)): (403, 403, 403),
    ("CASHIER", ("OPD_BILLING",)): (403, 403, 403),
}


@pytest.mark.parametrize("role,grants", sorted(P2_MATRIX))
def test_p2_role_matrix(api, role, grants):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    store = _q(api.conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", clinic)
    api.as_(api.U(role.lower(), "staff", clinic, str(uuid.uuid4()), list(grants), staff_role=role))
    items = api.call("GET", "/admin/rx/items", clinic).status_code
    generic = api.call("POST", "/admin/rx/generics", clinic, {"name": "Cetirizine " + uuid.uuid4().hex[:4],
                                                              "strength": "10 mg", "dosage_form": "tablet"}).status_code
    writeoff = api.call("POST", "/admin/rx/write-offs", clinic, {"store_id": str(store), "lines": [
        {"batch_id": str(uuid.uuid4()), "qty_units": 1, "kind": "damage", "reason": "Broken"}]}).status_code
    assert (items, generic, writeoff) == P2_MATRIX[(role, grants)]
