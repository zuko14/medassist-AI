"""Zero-impact matrix for clinics without Pharmacy OS (RX-83; VERIFICATION.md §7, TEST-PLAN §6).

One test per row. Where a row already has its proof elsewhere (RX-67 branch delete, RX-68 erasure, clinic deletion)
the test runs that proof from here, so this file is the complete matrix; the rest prove the row on real Postgres
through the real routes with a spy on every query the app sends.
"""

import asyncio
import uuid

import pytest

from tests.ipd_pg_bridge import PgBridge
from tests.test_admin_branches import (
    test_delete_branch_deactivates_when_another_module_references_it as _branch_fk_fallback,
    test_delete_branch_hard_deletes_when_unused as _branch_unused,
)
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m5 import open_shift
from tests.test_opd_data_retention import (
    test_family_member_erasure_unreferenced_vs_referenced as _erasure,
    test_family_member_referenced_outside_opd_is_redacted_and_loop_continues as _erasure_outside,
)
from tests.test_rx_feature_gate import test_deletion_of_a_clinic_without_pharmacy_makes_no_rx_purge_call as _deletion
from tests.test_rx_route_matrix import RX_ROUTES

TODAY = __import__("datetime").date.today()


@pytest.fixture
def spy(monkeypatch):
    """Every table / RPC path the app queries in this test (through the PgBridge)."""
    paths, real = [], PgBridge.execute

    def execute(self, builder):
        paths.append(str(builder.request.path))
        return real(self, builder)
    monkeypatch.setattr(PgBridge, "execute", execute)
    return paths


def _rx(paths):
    return [p for p in paths if "/rx_" in p or p.rsplit("/", 1)[-1].startswith("rx_")]


@pytest.fixture
def plain(api):  # noqa: F811
    c = _clinic(api.conn)                                    # OPD on, pharmacy never enabled
    return {"clinic": c, "admin": api.U("admin", "clinic_admin", c, str(uuid.uuid4()))}


def test_every_rx_route_refuses_a_non_rx_clinic_before_any_rx_read(api, plain, spy):  # noqa: F811
    api.as_(plain["admin"])
    fill = {"register": "H1", "kind": "sales_register"}
    refused, by_body = 0, []
    for method, path in sorted(RX_ROUTES):
        concrete = path
        for part in [p.strip("{}") for p in path.split("/") if p.startswith("{")]:
            concrete = concrete.replace("{" + part + "}", fill.get(part, str(uuid.uuid4())))
        concrete = concrete.replace("{kind}", "sales_register")
        before = len(spy)
        r = api.call(method, concrete, plain["clinic"], {} if method in ("POST", "PATCH", "PUT") else None)
        assert r.status_code in (403, 422), (method, path, r.status_code, r.text)   # 422 = request refused first
        assert not _rx(spy[before:]), (method, path, _rx(spy[before:]))
        refused += r.status_code == 403
        if r.status_code == 422:                          # FastAPI request validation, never a handler's 422
            assert isinstance(r.json()["detail"], list) and r.json()["detail"][0]["type"], (method, path)
            by_body.append(path)
        else:
            assert "not enabled" in r.text, (method, path, r.text)
    assert refused + len(by_body) == len(RX_ROUTES) and refused > len(by_body)


def test_close_shift_cash_unchanged_without_rx(api, plain):  # noqa: F811
    c, conn = plain["clinic"], api.conn
    shift, cashier = open_shift(conn, c, float_paise=5000)
    closed = _q(conn, "SELECT expected_cash_paise FROM opd_close_shift(%s, %s, %s, 5000, NULL)", c, shift, cashier)
    assert closed == 5000                                    # float + OPD cash only, as migration 116 computed it


def test_collections_summary_has_no_pharmacy_keys(api, plain, spy):  # noqa: F811
    r = api.as_(plain["admin"]).call("GET", "/admin/opd/collections/summary", plain["clinic"])
    assert r.status_code == 200 and not [k for k in r.json() if k.startswith("pharmacy_")]
    assert not _rx(spy)


def test_gstr1_is_the_old_shape_with_no_rx_query(api, plain, spy):  # noqa: F811
    r = api.as_(plain["admin"]).call("GET", "/admin/billing/gstr1", plain["clinic"], month=TODAY.month, year=TODAY.year)
    assert r.status_code == 200
    assert set(r.json()) == {"period", "clinic_gstin", "b2b", "b2cs", "nil_exempt_paise", "credit_notes",
                             "doc_summary", "totals"}
    assert set(r.json()["doc_summary"]) == {"invoices", "credit_notes"}
    assert set(r.json()["totals"]) == {"invoice_value_paise", "tax_paise", "credit_value_paise", "credit_tax_paise"}
    assert not _rx(spy)


def test_clinic_deletion_makes_no_rx_purge_call(api, monkeypatch):  # noqa: F811
    _deletion(api, monkeypatch)


def test_branch_delete_and_erasure_keep_their_fk_fallbacks():
    """RX-67 (branch referenced elsewhere → deactivate, not 500) and RX-68 (referenced family member → redacted)."""
    for proof in (_branch_unused, _branch_fk_fallback, _erasure, _erasure_outside):
        asyncio.run(proof())


def test_whatsapp_refill_hook_claims_only_its_two_exact_payloads(monkeypatch):
    from app.services import conversation, dental_plans, rx_refills
    reads = []
    monkeypatch.setattr(rx_refills, "sb", lambda *a, **k: reads.append(a))

    async def claims(payload):
        return await rx_refills.handle_button(None, {"id": str(uuid.uuid4())}, "+919876543210", payload)
    for other in ("refill", "REFILL", "STOP", "stop", "rx_refill", "rx_refill:", "rx_refill:not-a-uuid",
                  dental_plans.REVIEW_PAYLOAD_PREFIX + f"{uuid.uuid4()}:5", "checkin_yes", "Hi"):
        assert asyncio.run(claims(other)) is False, other
    assert reads == []                                       # nothing else ever reaches the pharmacy tables
    src = open(conversation.__file__, encoding="utf-8").read()
    hook = src.index("rx_refills.handle_button")
    assert src.rindex('if message_type == "button" and interactive_data:', 0, hook) > src.rindex("\n    async def ", 0, hook)
    assert src.index("dental_plans.handle_review_reply") < hook   # dental buttons are matched first, unchanged


def test_opd_disable_allowed_off_refused_active_and_wind_down(api, plain, monkeypatch):  # noqa: F811
    from app.services import rx as rx_service
    c = plain["clinic"]
    assert api.owner_toggle(c, "opd_enabled", False, confirm=True).status_code == 200      # OFF: unchanged path
    assert api.owner_toggle(c, "opd_enabled", True).status_code == 200
    assert api.owner_toggle(c, "pharmacy_enabled", True).status_code == 200
    assert api.owner_toggle(c, "opd_enabled", False, confirm=True).status_code == 409      # ACTIVE

    async def _busy(clinic_id):
        return {"open_sales": 1, "open_indents": 0, "transfers_in_transit": 0, "open_audits": 0, "blocking": True}
    monkeypatch.setattr(rx_service, "deactivation_preview", _busy)
    assert api.owner_toggle(c, "pharmacy_enabled", False, confirm=True).status_code == 200
    assert _q(api.conn, "SELECT rx_state FROM clinics WHERE id = %s", c) == "WIND_DOWN"
    r = api.owner_toggle(c, "opd_enabled", False, confirm=True)
    assert r.status_code == 409 and "Pharmacy OS" in r.text                                 # WIND_DOWN
