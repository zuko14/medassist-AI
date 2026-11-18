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
    # P3 purchase (137)
    ("GET", "/admin/rx/suppliers"), ("POST", "/admin/rx/suppliers"), ("PATCH", "/admin/rx/suppliers/{supplier_id}"),
    ("GET", "/admin/rx/purchase-orders"), ("POST", "/admin/rx/purchase-orders"),
    ("GET", "/admin/rx/purchase-orders/{po_id}"), ("PATCH", "/admin/rx/purchase-orders/{po_id}"),
    ("GET", "/admin/rx/purchase-orders/{po_id}.pdf"), ("POST", "/admin/rx/purchase-orders/{po_id}/send"),
    ("POST", "/admin/rx/purchase-orders/{po_id}/cancel"),
    ("GET", "/admin/rx/grns"), ("POST", "/admin/rx/grns"), ("GET", "/admin/rx/grns/{grn_id}"),
    ("PATCH", "/admin/rx/grns/{grn_id}"), ("POST", "/admin/rx/grns/{grn_id}/post"),
    ("POST", "/admin/rx/grns/{grn_id}/void"), ("GET", "/admin/rx/grns/{grn_id}/labels.pdf"),
    ("GET", "/admin/rx/supplier-returns"), ("POST", "/admin/rx/supplier-returns"),
    ("GET", "/admin/rx/supplier-returns/{return_id}"), ("POST", "/admin/rx/supplier-returns/{return_id}/dispatch"),
    ("PATCH", "/admin/rx/supplier-returns/{return_id}/credit"), ("GET", "/admin/rx/gs1"),
    # P4 counter sales (138)
    ("GET", "/admin/rx/pos/inbox"), ("GET", "/admin/rx/pos/lookup"), ("GET", "/admin/rx/pos/day-summary"),
    ("GET", "/admin/rx/sales"), ("POST", "/admin/rx/sales"), ("GET", "/admin/rx/sales/{sale_id}"),
    ("PUT", "/admin/rx/sales/{sale_id}/lines"), ("PATCH", "/admin/rx/sales/{sale_id}"),
    ("DELETE", "/admin/rx/sales/{sale_id}"), ("POST", "/admin/rx/sales/{sale_id}/complete"),
    ("POST", "/admin/rx/sales/{sale_id}/void"), ("POST", "/admin/rx/sales/{sale_id}/park"),
    ("POST", "/admin/rx/sales/{sale_id}/recall"), ("POST", "/admin/rx/sales/{sale_id}/returns"),
    ("GET", "/admin/rx/sales/{sale_id}/receipt"), ("GET", "/admin/rx/sales/{sale_id}/labels.pdf"),
    ("GET", "/admin/rx/credit-notes/{cn_id}.pdf"), ("POST", "/admin/rx/overrides"),
    # P5 inpatient pharmacy (139)
    ("GET", "/admin/rx/ipd/admissions/{admission_id}/indentable"), ("POST", "/admin/rx/ipd/indents"),
    ("GET", "/admin/rx/ipd/indents"), ("GET", "/admin/rx/ipd/indents/{indent_id}"),
    ("POST", "/admin/rx/ipd/indents/{indent_id}/dispense"), ("POST", "/admin/rx/ipd/indents/{indent_id}/cancel"),
    ("POST", "/admin/rx/ipd/dispenses/{dispense_id}/receive"), ("GET", "/admin/rx/ipd/dispenses/{dispense_id}/slip.pdf"),
    ("POST", "/admin/rx/ipd/dispenses/{dispense_id}/returns"),
    # P6 transfers, par levels, stock audits (140)
    ("GET", "/admin/rx/transfers"), ("POST", "/admin/rx/transfers"), ("GET", "/admin/rx/transfers/{transfer_id}"),
    ("PATCH", "/admin/rx/transfers/{transfer_id}"), ("POST", "/admin/rx/transfers/{transfer_id}/dispatch"),
    ("POST", "/admin/rx/transfers/{transfer_id}/receive"), ("POST", "/admin/rx/transfers/{transfer_id}/cancel"),
    ("GET", "/admin/rx/transfers/{transfer_id}/challan.pdf"),
    ("GET", "/admin/rx/par-levels"), ("POST", "/admin/rx/par-levels"), ("POST", "/admin/rx/par-levels/replenish"),
    ("GET", "/admin/rx/audits"), ("POST", "/admin/rx/audits"), ("GET", "/admin/rx/audits/{audit_id}"),
    ("PUT", "/admin/rx/audits/{audit_id}/counts"), ("POST", "/admin/rx/audits/{audit_id}/review"),
    ("POST", "/admin/rx/audits/{audit_id}/post"), ("POST", "/admin/rx/audits/{audit_id}/cancel"),
    # P7 statutory registers, batch recall, narcotic empties (141)
    ("GET", "/admin/rx/registers/{register}"), ("GET", "/admin/rx/registers/{register}/verify"),
    ("GET", "/admin/rx/registers/{register}/export.pdf"), ("GET", "/admin/rx/registers/{register}/export.csv"),
    ("GET", "/admin/rx/batches/{batch_id}/trace"), ("POST", "/admin/rx/dispenses/{dispense_line_id}/empties"),
    # P8 refills, alerts, expiry-return planner (142)
    ("GET", "/admin/rx/refills/requests"), ("POST", "/admin/rx/refills/requests/{request_id}/verify"),
    ("POST", "/admin/rx/refills/requests/{request_id}/pack"), ("POST", "/admin/rx/refills/requests/{request_id}/reject"),
    ("GET", "/admin/rx/refills/plans"), ("POST", "/admin/rx/refills/plans/{plan_id}/cancel"),
    ("GET", "/admin/rx/alerts"), ("POST", "/admin/rx/alerts/{alert_id}/ack"), ("GET", "/admin/rx/expiry/return-plan"),
    # P9 Ketu stock answer, dashboard, lost-sale call list (no migration)
    ("GET", "/admin/rx/stock/answer"), ("GET", "/admin/rx/dashboard"), ("GET", "/admin/rx/lost-sales"),
    ("POST", "/admin/rx/lost-sales/{prescription_id}/reveal-phone"),
    # P10 GSTR-1 pharmacy section, CSV reports, ABC / FSN / reorder analytics (no migration)
    ("GET", "/admin/rx/reports/gstr1"), ("GET", "/admin/rx/reports/{kind}.csv"), ("GET", "/admin/rx/metrics"),
    ("POST", "/admin/rx/metrics/recompute"), ("POST", "/admin/rx/metrics/apply-reorder"),
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


# P3: (role, stored grants) -> expected status for: list suppliers, add a supplier, send a draft PO
P3_MATRIX = {
    ("PHARMACY_MANAGER", ()): (200, 201, 200),
    ("STORE_KEEPER", ()): (200, 201, 403),                    # drafts and receives, never approves
    ("PHARMACIST", ()): (200, 403, 403),                      # reads purchase documents (RX_READ)
    ("FRONT_DESK", ("RX_PURCHASE",)): (200, 201, 403),
    ("FRONT_DESK", ("RX_PURCHASE", "RX_ADMIN")): (200, 201, 200),
    ("NURSE", ("IPD_NURSING",)): (403, 403, 403),
    ("CASHIER", ("OPD_BILLING",)): (403, 403, 403),
}


@pytest.mark.parametrize("role,grants", sorted(P3_MATRIX))
def test_p3_role_matrix(api, role, grants):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    store = str(_q(api.conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", clinic))
    gen = str(_q(api.conn, "INSERT INTO rx_generics (clinic_id, name, strength, dosage_form) VALUES (%s, 'Cetirizine', "
                           "'10 mg', 'tablet') RETURNING id", clinic))
    item = str(_q(api.conn, "INSERT INTO rx_items (clinic_id, item_code, brand_name, generic_id, base_unit, pack_size, "
                            "hsn_code, tax_rate_bps) VALUES (%s, 'CTZ', 'Okacet', %s, 'tablet', 10, '3004', 1200) "
                            "RETURNING id", clinic, gen))
    sup = str(_q(api.conn, "INSERT INTO rx_suppliers (clinic_id, name) VALUES (%s, 'Seed Supplier') RETURNING id", clinic))
    po = str(_q(api.conn, "INSERT INTO rx_purchase_orders (clinic_id, store_id, supplier_id) VALUES (%s, %s, %s) "
                          "RETURNING id", clinic, store, sup))
    _q(api.conn, "INSERT INTO rx_po_lines (clinic_id, po_id, line_no, item_id, ordered_packs) VALUES (%s, %s, 1, %s, 5)",
       clinic, po, item)
    api.as_(api.U(role.lower(), "staff", clinic, str(uuid.uuid4()), list(grants), staff_role=role))
    listed = api.call("GET", "/admin/rx/suppliers", clinic).status_code
    added = api.call("POST", "/admin/rx/suppliers", clinic, {"name": "New " + uuid.uuid4().hex[:6]}).status_code
    sent = api.call("POST", f"/admin/rx/purchase-orders/{po}/send", clinic).status_code
    assert (listed, added, sent) == P3_MATRIX[(role, grants)]


# P4: (role, stored grants) -> expected status for: open a walk-in draft, read the day summary, void a bill
P4_MATRIX = {
    ("PHARMACY_MANAGER", ()): (201, 200, 409),               # 409: the seeded bill is a draft, not completed
    ("PHARMACIST", ()): (201, 200, 403),                     # sells; voids need RX_ADMIN
    ("PHARMACY_OPERATOR", ()): (201, 200, 403),
    ("STORE_KEEPER", ()): (403, 200, 403),                   # stock, not the counter
    ("FRONT_DESK", ("RX_DISPENSE",)): (201, 200, 403),
    ("NURSE", ("IPD_NURSING",)): (403, 403, 403),
    ("CASHIER", ("OPD_BILLING",)): (403, 403, 403),
}


@pytest.mark.parametrize("role,grants", sorted(P4_MATRIX))
def test_p4_role_matrix(api, role, grants):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    store = str(_q(api.conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", clinic))
    seeded = str(_q(api.conn, "INSERT INTO rx_sales (clinic_id, store_id, sale_kind) VALUES (%s, %s, 'otc') RETURNING id",
                    clinic, store))
    api.as_(api.U(role.lower(), "staff", clinic, str(uuid.uuid4()), list(grants), staff_role=role))
    draft = api.call("POST", "/admin/rx/sales", clinic, {"store_id": store, "sale_kind": "otc"}).status_code
    summary = api.call("GET", "/admin/rx/pos/day-summary", clinic).status_code
    voided = api.call("POST", f"/admin/rx/sales/{seeded}/void", clinic, {"reason": "Wrong bill"}).status_code
    assert (draft, summary, voided) == P4_MATRIX[(role, grants)]


# P5: (role, stored grants) -> expected status for: read the indent queue, raise an indent (unknown stay -> 404 when
# allowed), dispense an indent (unknown indent -> 404 when allowed). IPD must be on for any of it.
P5_MATRIX = {
    ("PHARMACY_MANAGER", ()): (200, 404, 404),
    ("PHARMACIST", ()): (200, 403, 404),                     # dispenses; nurses raise
    ("NURSE", ("IPD_NURSING",)): (200, 404, 403),            # raises; never dispenses
    ("STORE_KEEPER", ()): (403, 403, 403),
    ("CASHIER", ("OPD_BILLING",)): (403, 403, 403),
    ("DOCTOR", ("IPD_CLINICAL",)): (403, 403, 403),
}


@pytest.mark.parametrize("role,grants", sorted(P5_MATRIX))
def test_p5_role_matrix(api, role, grants):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "ipd_enabled", True).status_code == 200
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    store = str(_q(api.conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", clinic))
    api.as_(api.U(role.lower(), "staff", clinic, str(uuid.uuid4()), list(grants), staff_role=role))
    queue = api.call("GET", "/admin/rx/ipd/indents", clinic).status_code
    raised = api.call("POST", "/admin/rx/ipd/indents", clinic, {
        "admission_id": str(uuid.uuid4()), "from_store_id": store,
        "lines": [{"ipd_order_id": str(uuid.uuid4()), "requested_units": 1}]}).status_code
    dispensed = api.call("POST", f"/admin/rx/ipd/indents/{uuid.uuid4()}/dispense", clinic, {
        "store_id": store, "lines": [{"indent_line_id": str(uuid.uuid4())}]}).status_code
    assert (queue, raised, dispensed) == P5_MATRIX[(role, grants)]


# P6: (role, stored grants) -> expected status for: list transfers, start an audit (unknown store -> 404 when allowed),
# replenish par levels (unknown store -> 404 when allowed). RX_STOCK of the v1 spec = RX_TRANSFER / RX_AUDIT.
P6_MATRIX = {
    ("PHARMACY_MANAGER", ()): (200, 404, 404),
    ("STORE_KEEPER", ()): (200, 404, 404),
    ("PHARMACIST", ()): (200, 403, 403),                     # reads transfers; neither counts nor moves stock
    ("NURSE", ("IPD_NURSING",)): (200, 403, 403),            # sees transfers into its ward stores only
    ("FRONT_DESK", ("RX_TRANSFER",)): (200, 403, 404),
    ("FRONT_DESK", ("RX_AUDIT",)): (403, 404, 403),
    ("CASHIER", ("OPD_BILLING",)): (403, 403, 403),
}


@pytest.mark.parametrize("role,grants", sorted(P6_MATRIX))
def test_p6_role_matrix(api, role, grants):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    api.as_(api.U(role.lower(), "staff", clinic, str(uuid.uuid4()), list(grants), staff_role=role))
    listed = api.call("GET", "/admin/rx/transfers", clinic).status_code
    audit = api.call("POST", "/admin/rx/audits", clinic, {"store_id": str(uuid.uuid4()), "scope": "full"}).status_code
    replenish = api.call("POST", "/admin/rx/par-levels/replenish", clinic, {"store_id": str(uuid.uuid4())}).status_code
    assert (listed, audit, replenish) == P6_MATRIX[(role, grants)]


# P7: (role, stored grants) -> expected status for: read the H1 register, trace an unknown batch (404 when allowed),
# receive empties on an unknown dispense line (404 when allowed; 403 without IPD). RX_REGISTER_EXPORT = RX_REGISTERS.
P7_MATRIX = {
    ("PHARMACY_MANAGER", ()): (200, 404, 403),               # IPD is off in this clinic: empties are an IPD route
    ("PHARMACIST", ()): (200, 403, 403),
    ("STORE_KEEPER", ()): (403, 403, 403),                   # RX_READ alone never opens patient registers
    ("FRONT_DESK", ("RX_REGISTERS",)): (200, 403, 403),
    ("NURSE", ("IPD_NURSING",)): (403, 403, 403),
    ("CASHIER", ("OPD_BILLING",)): (403, 403, 403),
}


@pytest.mark.parametrize("role,grants", sorted(P7_MATRIX))
def test_p7_role_matrix(api, role, grants):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    api.as_(api.U(role.lower(), "staff", clinic, str(uuid.uuid4()), list(grants), staff_role=role))
    listed = api.call("GET", "/admin/rx/registers/H1", clinic).status_code
    trace = api.call("GET", f"/admin/rx/batches/{uuid.uuid4()}/trace", clinic).status_code
    empties = api.call("POST", f"/admin/rx/dispenses/{uuid.uuid4()}/empties", clinic,
                       {"units": 1, "returned_by": str(uuid.uuid4())}).status_code
    assert (listed, trace, empties) == P7_MATRIX[(role, grants)]


# P8: (role, stored grants) -> expected status for: refill queue, verify an unknown request (404 when allowed),
# alerts, the expiry-return plan of an unknown store (404 when allowed).
P8_MATRIX = {
    ("PHARMACY_MANAGER", ()): (200, 404, 200, 404),
    ("PHARMACIST", ()): (200, 404, 200, 404),
    ("PHARMACY_OPERATOR", ()): (200, 404, 200, 404),
    ("STORE_KEEPER", ()): (200, 403, 200, 404),              # reads the queue; never verifies or packs
    ("NURSE", ("IPD_NURSING",)): (403, 403, 403, 403),
    ("CASHIER", ("OPD_BILLING",)): (403, 403, 403, 403),
}


@pytest.mark.parametrize("role,grants", sorted(P8_MATRIX))
def test_p8_role_matrix(api, role, grants):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    api.as_(api.U(role.lower(), "staff", clinic, str(uuid.uuid4()), list(grants), staff_role=role))
    queue = api.call("GET", "/admin/rx/refills/requests", clinic).status_code
    verify = api.call("POST", f"/admin/rx/refills/requests/{uuid.uuid4()}/verify", clinic).status_code
    alerts = api.call("GET", "/admin/rx/alerts", clinic).status_code
    plan = api.call("GET", "/admin/rx/expiry/return-plan", clinic, store_id=str(uuid.uuid4())).status_code
    assert (queue, verify, alerts, plan) == P8_MATRIX[(role, grants)]


# P9: (role, stored grants) -> expected status for: stock answer, dashboard, lost-sale list, reveal an unknown
# prescription's phone (404 when allowed).
P9_MATRIX = {
    ("PHARMACY_MANAGER", ()): (200, 200, 200, 404),
    ("PHARMACIST", ()): (200, 200, 200, 404),
    ("STORE_KEEPER", ()): (200, 200, 200, 403),               # reads the list; never sees a full phone
    ("NURSE", ("IPD_NURSING",)): (200, 403, 403, 403),       # stock answer only (ward scope)
    ("CASHIER", ("OPD_BILLING",)): (403, 403, 403, 403),
}


@pytest.mark.parametrize("role,grants", sorted(P9_MATRIX))
def test_p9_role_matrix(api, role, grants):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    api.as_(api.U(role.lower(), "staff", clinic, str(uuid.uuid4()), list(grants), staff_role=role))
    answer = api.call("GET", "/admin/rx/stock/answer", clinic, q="dolo").status_code
    dash = api.call("GET", "/admin/rx/dashboard", clinic).status_code
    lost = api.call("GET", "/admin/rx/lost-sales", clinic).status_code
    reveal = api.call("POST", f"/admin/rx/lost-sales/{uuid.uuid4()}/reveal-phone", clinic).status_code
    assert (answer, dash, lost, reveal) == P9_MATRIX[(role, grants)]
