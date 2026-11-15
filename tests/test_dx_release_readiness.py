"""Diagnostic OS release readiness (P11: DX-63, DX-64).

Tenancy: every dx_* table in the database is a registered tenant table. Auth: every /admin/dx route needs a staff
session and its handler opens with a DX scope helper; every /internal/dx route refuses an unsigned call; a
B2B_PARTNER login reaches only its portal (swept over every /admin route). Schema: verify_supabase_schema.sql
§19–§27 all PASS on a fresh database; the rollbacks 133 -> 125 run newest first and 125 -> 133 re-apply.
Lifecycle: one visit from the desk to the patient's WhatsApp — preprinted label bound, tube collected, sent to
the hub in a batch, received, entered with a calculated parameter, verified, approved, released, held for dues,
paid, dispatched — then the ROLLOUT §4 reconciliation queries return nothing and purge v9 empties every DX table.
"""

import ast
import inspect
import pathlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.tenancy import TENANT_OWNED_TABLES
from tests.test_dx_reports_e2e import _dispatch, _pay, _releases, _targets, rep  # noqa: F401  (fixture)
from tests.test_dx_results_e2e import _approve, _as, _levels, _order, _save, bench  # noqa: F401
from tests.test_dx_visits_e2e import dxw, visit as _desk_visit  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

MIG = pathlib.Path(__file__).resolve().parents[1] / "migrations"
DX_TABLES = sorted(t for t in TENANT_OWNED_TABLES if t.startswith("dx_"))
SCOPE_HELPERS = {"_dx_scope", "_partner", "_mis"}


def _routes(prefix):
    from app.main import app
    out = []
    for r in app.routes:
        for sub in (r.original_router.routes if hasattr(r, "original_router") else [r]):
            for m in getattr(sub, "methods", None) or ():
                if getattr(sub, "path", "").startswith(prefix):
                    out.append((m, sub.path, sub))
    return out


# ── tenancy and auth (DX-63) ────────────────────────────────────────────────

def test_every_dx_table_in_the_database_is_a_tenant_table(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'dx\\_%%'")
    assert sorted(r[0] for r in cur.fetchall()) == DX_TABLES and len(DX_TABLES) == 41
    cur.execute("SELECT count(*) FROM pg_class WHERE relname = ANY(%s) AND relkind = 'r' AND relrowsecurity "
                "AND relforcerowsecurity", (DX_TABLES,))
    assert cur.fetchone()[0] == 41


def _scope_helpers() -> set:
    """_dx_scope / _partner / _mis plus every router-module function that (transitively) calls one of them."""
    from app.routers import dx, dx_bridge
    funcs = {n: f for mod in (dx, dx_bridge) for n, f in vars(mod).items()
             if inspect.isfunction(f) and f.__module__ == mod.__name__}
    calls = {n: {c.func.id for c in ast.walk(ast.parse(inspect.getsource(f))) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Name)} for n, f in funcs.items()}
    known = set(SCOPE_HELPERS)
    while True:
        more = {n for n, cs in calls.items() if n not in known and cs & known}
        if not more:
            return known
        known |= more


def test_every_admin_dx_route_needs_a_session_and_opens_with_a_dx_scope():
    from app.routers.admin import verify_credentials
    helpers = _scope_helpers()
    routes = _routes("/admin/dx")
    assert len(routes) >= 156
    for method, path, r in routes:
        assert any(d.call is verify_credentials for d in r.dependant.dependencies), f"{method} {path}: no session"
        src = inspect.getsource(r.endpoint)
        called = {n.func.id for n in ast.walk(ast.parse(src.lstrip()))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert called & helpers, f"{method} {path}: handler never reaches {sorted(SCOPE_HELPERS)}"


def test_internal_dx_routes_refuse_unsigned_calls():
    from app.main import app
    routes = _routes("/internal/dx")
    assert {(m, p) for m, p, _ in routes} == {("GET", "/internal/dx/bridge/orders"),
                                              ("POST", "/internal/dx/bridge/results"),
                                              ("POST", "/internal/dx/bridge/heartbeat")}
    client = TestClient(app)
    for method, path, _ in routes:
        r = client.request(method, path + ("?barcode=ABC123" if method == "GET" else ""), content=b"{}")
        assert r.status_code == 401, (method, path, r.status_code)


def test_partner_login_reaches_only_its_portal_on_every_admin_route():
    """Swept over every /admin route of the app: the partner portal, /admin/me and its own credential changes."""
    from app.routers.admin import AdminUser, _enforce_dx_confined_scope
    partner = AdminUser("hosp", "staff", "c1", "u1", [], staff_role="B2B_PARTNER", dx_b2b_account_id="a1")
    allowed = set()
    for method, path, _ in _routes("/admin"):
        concrete = path.replace("{", "x").replace("}", "")
        try:
            _enforce_dx_confined_scope(SimpleNamespace(url=SimpleNamespace(path=concrete), method=method), partner)
            allowed.add((method, path))
        except HTTPException as e:
            assert e.status_code == 403
    portal = {(m, p) for m, p, _ in _routes("/admin/dx/partner")}
    assert portal and portal <= allowed
    extra = {p for _, p in allowed - portal}
    assert extra <= {"/admin/me", "/admin/change-password", "/admin/change-username"}, extra


# ── schema pipeline (DX-64) ─────────────────────────────────────────────────

def test_verify_script_sections_19_to_27_all_pass(real_postgres_uri):
    import psycopg2
    conn = psycopg2.connect(real_postgres_uri)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        script = (MIG / "verify_supabase_schema.sql").read_text(encoding="utf-8")
        cur.execute(script.replace("DROP TABLE IF EXISTS _verification_results;", ""))   # keep the results to read
        cur.execute("SELECT category, status, count(*) FROM _verification_results "
                    "WHERE category IN ('M125','M126','M127','M128','M129','M130','M131','M132','M133') "
                    "GROUP BY 1, 2 ORDER BY 1, 2")
        rows = cur.fetchall()
    finally:
        conn.close()
    assert {r[1] for r in rows} == {"PASS"}, rows
    assert {r[0]: r[2] for r in rows} == {"M125": 4, "M126": 4, "M127": 5, "M128": 3, "M129": 4, "M130": 3,
                                         "M131": 3, "M132": 5, "M133": 3}


def test_rollbacks_run_newest_first_then_all_nine_reapply(real_pg_conn):
    from tests.test_migration_dx_m1 import unwind_125
    cur = real_pg_conn.cursor()
    cur.execute("BEGIN")
    try:
        unwind_125(cur)                      # chains 133 -> 132 -> ... -> 125, purging each DX clinic first
        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'dx\\_%%'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname = 'dx_purge_clinic'")
        assert cur.fetchone()[0] == 0
        for n in ("125_dx_os_foundation", "126_dx_catalogue", "127_dx_visits_and_billing", "128_dx_collection",
                  "129_dx_logistics", "130_dx_result_workflow", "131_dx_reports", "132_dx_b2b",
                  "133_dx_instruments_qc"):
            cur.execute((MIG / f"{n}.sql").read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'dx\\_%%'")
        assert cur.fetchone()[0] == 41
        cur.execute("SELECT position('v9 (133)' in pg_get_functiondef('dx_purge_clinic(uuid)'::regprocedure)) > 0")
        assert cur.fetchone()[0] is True
    finally:
        cur.execute("ROLLBACK")


# ── desk to release, reconciliation, purge (DX-63, DX-64) ───────────────────

@pytest.fixture
def life(rep, monkeypatch):  # noqa: F811
    from app.services import dx
    w, c, conn, U = rep, rep["clinic"], rep["c"], rep["api"].U

    async def no_notice(*_a, **_k):
        return True
    monkeypatch.setattr(dx, "notify_recollection", no_notice)
    w["hub_branch"] = str(_q(conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Hub') RETURNING id", c))
    w["hub"] = str(_q(conn, "INSERT INTO dx_sites (clinic_id, branch_id, kind, code, display_name) VALUES "
                            "(%s, %s, 'processing', 'HUB', 'Central hub') RETURNING id", c, w["hub_branch"]))
    uid = lambda: str(uuid.uuid4())  # noqa: E731
    w["u"].update({"runner": U("runner", "staff", c, uid(), [], staff_role="LOGISTICS_RUNNER", branch_id=w["branch"]),
                   "hubtech": U("hubtech", "staff", c, uid(), [], staff_role="LAB_OPERATOR", branch_id=w["hub_branch"])})
    return w


def _recon(w) -> dict:
    """ROLLOUT §4 queries 1, 2, 3 and 7 for this clinic: every count must be 0."""
    c, conn = w["clinic"], w["c"]
    return {
        "bill_vs_items": _q(conn, """SELECT count(*) FROM dx_accessions a WHERE a.clinic_id = %s AND a.billing_mode = 'patient'
              AND EXISTS (SELECT 1 FROM opd_invoices i WHERE i.clinic_id = a.clinic_id AND i.accession_id = a.id)
              AND (SELECT COALESCE(sum(total_paise), 0) FROM opd_invoices i WHERE i.clinic_id = a.clinic_id
                     AND i.accession_id = a.id AND i.status <> 'void')
               <> (SELECT COALESCE(sum(price_paise - discount_paise), 0) FROM dx_accession_items t
                     WHERE t.clinic_id = a.clinic_id AND t.accession_id = a.id AND t.status = 'active')""", c),
        "collected_order_without_tube": _q(conn, """SELECT count(*) FROM opd_orders o JOIN dx_samples s
              ON s.clinic_id = o.clinic_id AND s.id = o.dx_sample_id WHERE o.clinic_id = %s AND o.source = 'dx'
              AND o.status = 'sample_collected' AND s.status NOT IN ('collected', 'in_transit', 'received', 'stored')""", c),
        "stale_batch_tubes": _q(conn, """SELECT count(*) FROM dx_batches b JOIN dx_batch_samples bs
              ON bs.clinic_id = b.clinic_id AND bs.batch_id = b.id JOIN dx_samples s ON s.clinic_id = bs.clinic_id
              AND s.id = bs.sample_id WHERE b.clinic_id = %s AND b.status = 'dispatched'
              AND b.dispatched_at < now() - interval '24 hours' AND s.status = 'in_transit'""", c),
        "dx_report_without_release": _q(conn, """SELECT count(*) FROM lab_reports l WHERE l.clinic_id = %s
              AND l.source = 'dx' AND NOT EXISTS (SELECT 1 FROM dx_release_deliveries d
              WHERE d.clinic_id = l.clinic_id AND d.lab_report_id = l.id)""", c),
    }


def test_desk_to_release_lifecycle_then_purge(life):
    w, c = life, life["clinic"]
    _levels(w, 3)
    assert _as(w, "admin").call("POST", "/admin/dx/routing-rules", c, {"to_site_id": w["hub"]}).status_code == 201
    # 1. registration at Main; the lipid tube is routed to the hub
    reg = _desk_visit(w, ["lipid"], branch_id=w["branch"])
    assert reg.status_code == 201, reg.text
    v = reg.json()
    vid, tube = v["visit"]["id"], v["samples"][0]
    assert tube["barcode"] and v["visit"]["accession_number"].startswith("LAB-")
    # 2. a preprinted label (e.g. a partner's stock) is bound to its tube on the same visit
    cur = w["c"].cursor()
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (c,))
    cur.execute("""INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode_source,
                   collection_site_id, processing_site_id) VALUES (%s, %s, %s, 'serum', 'preprinted', %s, %s) RETURNING id""",
                (c, vid, w["serum"], w["site"], w["hub"]))
    pre = str(cur.fetchone()[0])
    cur.execute("COMMIT")
    bind = _as(w, "col").call("POST", f"/admin/dx/samples/{pre}/bind", c, {"barcode": "PRE77001"})
    assert bind.status_code == 200 and bind.json()["barcode"] == "PRE77001"
    # 3. collection at the desk
    col = _as(w, "col").call("POST", f"/admin/dx/visits/{vid}/collect", c,
                             {"samples": [{"id": tube["id"]}], "idempotency_key": uuid.uuid4().hex})
    assert col.status_code == 200, col.text
    # 4. logistics: the runner batches and dispatches it, the hub receives it
    run = _as(w, "runner")
    b = run.call("POST", "/admin/dx/batches", c, {"to_site_id": w["hub"], "carrier_name": "Ravi", "temp_c": 4})
    assert b.status_code == 201, b.text
    bid = b.json()["id"]
    assert run.call("POST", f"/admin/dx/batches/{bid}/samples", c, {"barcodes": [tube["barcode"]]}).json()["added"] == 1
    assert run.call("POST", f"/admin/dx/batches/{bid}/dispatch", c, {"temp_c": 5}).status_code == 200
    got = _as(w, "hubtech").call("POST", f"/admin/dx/batches/{bid}/receive", c, {"barcodes": [tube["barcode"]], "temp_c": 6})
    assert got.status_code == 200 and got.json()["status"] == "received", got.text
    # 5. entry with a calculated parameter (TC/HDL ratio = LIPID / HDL)
    oid = _order(v, "Lipid Profile")
    saved = _save(w, oid, {"Lipid Profile": "200", "HDL": "50"})
    assert saved.status_code == 200, saved.text
    ratio = next(x for x in saved.json()["values"] if x["parameter_name"] == "TC/HDL ratio")
    assert ratio["is_calculated"] and str(ratio["numeric_value"]).startswith("4.0")
    r = saved.json()
    # 6. verification (third level) and pathologist approval
    ver = _as(w, "sup").call("POST", f"/admin/dx/results/{r['id']}/verify", c,
                             {"expected_updated_at": r["updated_at"], "decision": "verified"})
    assert ver.status_code == 200, ver.text
    appr = _approve(w, [r])
    assert appr.status_code == 200 and appr.json()["signed"] == 1, appr.text
    w["drain"]()
    # 7. release: rendered PDF, every target held for unpaid dues (no payment, no report)
    rel = _releases(w, v)["releases"][0]
    assert _targets(rel)["patient"]["status"] == "held_dues" and not w["sent"]
    pdf = _as(w, "disp").call("GET", f"/admin/dx/releases/{rel['id']}/pdf", c)
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    # 8. payment at the desk clears the hold; the outbox dispatches the report once
    _pay(w, v)
    _dispatch(w)
    assert _targets(_releases(w, v)["releases"][0])["patient"]["status"] == "sent" and len(w["sent"]) == 1
    _dispatch(w)
    assert len(w["sent"]) == 1
    track = _as(w, "runner").call("GET", f"/admin/dx/samples/{tube['barcode']}/track", c).json()
    kinds = [e["kind"] for e in track["timeline"]]
    assert kinds.index("collected") < kinds.index("dispatched") < kinds.index("received")
    # 9. ROLLOUT §4 reconciliation: nothing to fix
    assert _recon(w) == {"bill_vs_items": 0, "collected_order_without_tube": 0, "stale_batch_tubes": 0,
                         "dx_report_without_release": 0}
    # 10. instruments and QC rows too, then purge v9 empties every DX table of this clinic
    agent = _as(w, "admin").call("POST", "/admin/dx/bridge-agents", c, {"site_id": w["hub"], "name": "Hub PC"}).json()
    inst = _as(w, "admin").call("POST", "/admin/dx/instruments", c,
                                {"agent_id": agent["id"], "name": "Cobas", "protocol": "hl7v2"}).json()
    mat = str(_q(w["c"], "INSERT INTO dx_qc_materials (clinic_id, instrument_id, parameter_code, level, lot, target_mean, "
                         "target_sd, valid_from, valid_to) VALUES (%s, %s, 'CHOL', 'L1', 'L', 200, 5, CURRENT_DATE, "
                         "CURRENT_DATE) RETURNING id", c, inst["id"]))
    _q(w["c"], "INSERT INTO dx_qc_results (clinic_id, material_id, value, z_score) VALUES (%s, %s, 201, 0.2)", c, mat)
    used = [t for t in DX_TABLES if _q(w["c"], f"SELECT count(*) FROM {t} WHERE clinic_id = %s", c)]
    assert len(used) >= 20, used
    _q(w["c"], "SELECT dx_purge_clinic(%s)", c)
    left = {t: n for t in DX_TABLES if (n := _q(w["c"], f"SELECT count(*) FROM {t} WHERE clinic_id = %s", c))}
    assert left == {}
    assert _q(w["c"], "SELECT count(*) FROM opd_orders WHERE clinic_id = %s AND source = 'dx'", c) == 0
