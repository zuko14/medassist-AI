"""Pharmacy OS release readiness (P11: RX-64 cutover rehearsal, RX-65 release readiness).

Tenancy: every rx_* table in the database is a registered tenant table with forced RLS. Auth: every /admin/rx route
needs a staff session and its handler opens with `_rx_scope`; no RX module imports AI / voice / assistant code.
Schema: verify_supabase_schema.sql §28–§36 all PASS; the rollbacks 142 → 134 run newest first and 134 → 142 re-apply
(twice: idempotent). Cutover: a legacy item master and closing-stock CSV import with dry runs first and reconcile to
the paisa-unit (CSV = balances = ledger). Lifecycle: GRN → OPD Rx sale + cash and credit-note returns → IPD indent,
dispense, ward return → transfer → audit → registers verified → refill → GSTR-1, every CSV, metrics → purge v9 leaves
no row in any of the 41 RX tables.
"""

import ast
import inspect
import json
import pathlib
import uuid
from datetime import date, timedelta

from app.tenancy import TENANT_OWNED_TABLES
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m1 import _store
from tests.test_migration_rx_m4 import _grn, _line, _post
from tests.test_migration_rx_m5 import draft, line, open_shift, signed_rx
from tests.test_migration_rx_m7 import counts, dispatch, post, receive, start
from tests.test_migration_rx_m7 import draft as transfer_draft
from tests.test_rx_catalogue_api import ROWS, csv_bytes, upload
from tests.test_rx_ipd import OPEN, _dispense, _mm, _raise, ward  # noqa: F401  (fixture)
from tests.test_rx_no_llm import _banned, _imports, _rx_files
from tests.test_rx_route_matrix import RX_ROUTES

MIG = pathlib.Path(__file__).resolve().parents[1] / "migrations"
RX_TABLES = sorted(t for t in TENANT_OWNED_TABLES if t.startswith("rx_"))
RX_MIGRATIONS = ("134_rx_foundation", "135_rx_catalogue", "136_rx_stock_ledger", "137_rx_purchase", "138_rx_pos",
                 "139_rx_ipd", "140_rx_transfers_audits", "141_rx_registers", "142_rx_refills_alerts")


# ── tenancy and auth (RX-65) ────────────────────────────────────────────────

def test_every_rx_table_in_the_database_is_a_tenant_table_with_forced_rls(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'rx\\_%%'")
    assert sorted(r[0] for r in cur.fetchall()) == RX_TABLES and len(RX_TABLES) == 41
    cur.execute("SELECT count(*) FROM pg_class WHERE relname = ANY(%s) AND relkind = 'r' AND relrowsecurity "
                "AND relforcerowsecurity", (RX_TABLES,))
    assert cur.fetchone()[0] == 41


def _scope_helpers() -> set:
    """_rx_scope plus every router function that (transitively) calls it."""
    from app.routers import rx
    funcs = {n: f for n, f in vars(rx).items() if inspect.isfunction(f) and f.__module__ == rx.__name__}
    calls = {n: {c.func.id for c in ast.walk(ast.parse(inspect.getsource(f))) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Name)} for n, f in funcs.items()}
    known = {"_rx_scope"}
    while True:
        more = {n for n, cs in calls.items() if n not in known and cs & known}
        if not more:
            return known
        known |= more


def test_every_rx_route_needs_a_session_opens_with_rx_scope_and_is_in_the_matrix():
    from app.main import app
    from app.routers.admin import verify_credentials
    helpers, seen = _scope_helpers(), set()
    for r in app.routes:
        for sub in (r.original_router.routes if hasattr(r, "original_router") else [r]):
            if not getattr(sub, "path", "").startswith("/admin/rx"):
                continue
            for m in (getattr(sub, "methods", None) or ()):
                seen.add((m, sub.path))
            assert any(d.call is verify_credentials for d in sub.dependant.dependencies), f"{sub.path}: no session"
            called = {n.func.id for n in ast.walk(ast.parse(inspect.getsource(sub.endpoint).lstrip()))
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert called & helpers, f"{sub.path}: handler never reaches _rx_scope"
    assert seen == RX_ROUTES and len(RX_ROUTES) == 118
    assert not {p.as_posix(): _banned(_imports(p)) for p in _rx_files() if _banned(_imports(p))}       # RG3


# ── schema pipeline (RX-65) ─────────────────────────────────────────────────

def test_verify_script_sections_28_to_36_all_pass(real_postgres_uri):
    import psycopg2
    conn = psycopg2.connect(real_postgres_uri)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        script = (MIG / "verify_supabase_schema.sql").read_text(encoding="utf-8")
        cur.execute(script.replace("DROP TABLE IF EXISTS _verification_results;", ""))   # keep the results to read
        cur.execute("SELECT category, status, count(*) FROM _verification_results WHERE category IN "
                    "('M134','M135','M136','M137','M138','M139','M140','M141','M142') GROUP BY 1, 2 ORDER BY 1, 2")
        rows = cur.fetchall()
    finally:
        conn.close()
    assert {r[1] for r in rows} == {"PASS"}, rows
    assert len({r[0] for r in rows}) == 9 and sum(r[2] for r in rows) == 27


def test_rollbacks_run_newest_first_then_all_nine_reapply_twice(real_pg_conn):
    from tests.test_migration_rx_m1 import unwind_134
    cur = real_pg_conn.cursor()
    cur.execute("BEGIN")
    try:
        unwind_134(cur)                      # chains 142 -> 141 -> ... -> 134, purging each RX clinic first
        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'rx\\_%%'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname IN ('rx_purge_clinic', 'rx_complete_sale')")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'clinics' "
                    "AND column_name IN ('rx_state', 'rx_settings')")
        assert cur.fetchone()[0] == 0
        for _ in range(2):                   # the second pass proves every migration is idempotent
            for n in RX_MIGRATIONS:
                cur.execute((MIG / f"{n}.sql").read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'rx\\_%%'")
        assert cur.fetchone()[0] == 41
        cur.execute("SELECT count(*) FROM pg_class WHERE relname = ANY(%s) AND relforcerowsecurity", (RX_TABLES,))
        assert cur.fetchone()[0] == 41
        cur.execute("SELECT position('rx_refill_requests' in pg_get_functiondef('rx_purge_clinic(uuid)'::regprocedure)) > 0")
        assert cur.fetchone()[0] is True     # purge v9 (142) is the live one
    finally:
        cur.execute("ROLLBACK")


# ── cutover rehearsal (RX-64, ROLLOUT §5) ───────────────────────────────────

def test_cutover_rehearsal_reconciles_csv_balances_and_ledger(api):  # noqa: F811
    c, conn = _clinic(api.conn), api.conn
    assert api.owner_toggle(c, "pharmacy_enabled", True).status_code == 200
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    api.as_(api.U("admin", "clinic_admin", c, str(uuid.uuid4())))
    store = str(_q(conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", c))
    # T−7: item master dry run changes nothing, apply creates every item
    assert upload(api, "/admin/rx/items/import/dry-run", c, csv_bytes(ROWS)).status_code == 200
    assert _q(conn, "SELECT count(*) FROM rx_items WHERE clinic_id = %s", c) == 0
    assert upload(api, "/admin/rx/items/import/apply", c, csv_bytes(ROWS)).status_code == 200
    assert _q(conn, "SELECT count(*) FROM rx_items WHERE clinic_id = %s", c) == 3
    # T−1 night: the legacy closing stock by batch, dry run then apply
    legacy = [("DOLO650", "DN1", 200, 3000, 2000, 12, 137), ("DOLO650", "DN2", 500, 3000, 2100, 12, 260),
              ("CALPOL650", "CP1", 300, 2800, 1900, 12, 45), ("AUG625", "AG1", 400, 22000, 16000, 12, 80)]
    body = OPEN + "".join(f"{i},{b},{_mm(d)},{m / 100:.2f},{p / 100:.2f},{g},{q}\n" for i, b, d, m, p, g, q in legacy)
    dry = upload(api, "/admin/rx/stock/opening-import", c, body.encode(), "o.csv", store_id=store, dry_run="true")
    assert dry.status_code == 200, dry.text
    assert _q(conn, "SELECT count(*) FROM rx_stock_movements WHERE clinic_id = %s", c) == 0
    assert upload(api, "/admin/rx/stock/opening-import", c, body.encode(), "o.csv", store_id=store,
                  dry_run="false").status_code == 200
    # reconciliation: 0 % variance, in total and per batch
    want = {(i, b): q for i, b, *_, q in legacy}
    bal = {(r[0], r[1]): r[2] for r in _q(conn, """SELECT COALESCE(json_agg(json_build_array(i.item_code, b.batch_no,
            s.qty_units)), '[]') FROM rx_stock_balances s JOIN rx_batches b ON b.id = s.batch_id
            JOIN rx_items i ON i.id = s.item_id WHERE s.clinic_id = %s""", c)}
    led = {(r[0], r[1]): r[2] for r in _q(conn, """SELECT COALESCE(json_agg(json_build_array(item_code, batch_no, q)), '[]')
            FROM (SELECT i.item_code, b.batch_no, SUM(m.qty_units) q FROM rx_stock_movements m
                  JOIN rx_batches b ON b.id = m.batch_id JOIN rx_items i ON i.id = m.item_id
                  WHERE m.clinic_id = %s GROUP BY 1, 2) x""", c)}
    assert bal == led == want and sum(bal.values()) == 522
    assert {m for m in _q(conn, "SELECT json_agg(DISTINCT movement_type) FROM rx_stock_movements WHERE clinic_id = %s", c)} == {
        "opening"}


# ── one hospital, end to end, then purge (RX-65) ────────────────────────────

def test_hospital_lifecycle_then_purge_leaves_no_rx_row(api, ward, monkeypatch):  # noqa: F811
    w, c, conn = ward, ward["clinic"], api.conn
    monkeypatch.setattr("app.routers.rx.log_admin_action", lambda **kw: _done())
    store, dolo = w["store"], w["item"]
    mgr = api.U("mgr", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACY_MANAGER")
    aug = str(_q(conn, "SELECT id FROM rx_items WHERE clinic_id = %s AND item_code = 'AUG625'", c))
    _q(conn, "INSERT INTO rx_item_aliases (clinic_id, item_id, alias_norm, source) VALUES (%s, %s, 'augmentin duo', "
             "'manual')", c, aug)
    # 1. supplier, PO sent, GRN posted (Schedule H: the register opens with the receipt)
    sup = str(_q(conn, "INSERT INTO rx_suppliers (clinic_id, name, lead_time_days) VALUES (%s, 'Sri Balaji Pharma', 4) "
                       "RETURNING id", c))
    wg = {"clinic": c, "store": store, "supplier": sup, "item": aug}
    po = str(_q(conn, "INSERT INTO rx_purchase_orders (clinic_id, store_id, supplier_id) VALUES (%s, %s, %s) RETURNING id",
                c, store, sup))
    _q(conn, "INSERT INTO rx_po_lines (clinic_id, po_id, line_no, item_id, ordered_packs) VALUES (%s, %s, 1, %s, 10)",
       c, po, aug)
    assert _q(conn, "SELECT rx_send_po(%s, %s)", c, po)["po_number"].startswith("PO-")
    grn = _grn(conn, wg, "INV-77", po=po)
    _line(conn, wg, grn)
    _q(conn, "UPDATE rx_grns SET invoice_total_paise = taxable_paise + cgst_paise + sgst_paise + igst_paise WHERE id = %s",
       grn)
    _post(conn, wg, grn)
    gbatch = str(_q(conn, "SELECT batch_id FROM rx_grn_lines WHERE grn_id = %s", grn))
    # 2. OPD prescription sold at the counter for cash; a cash return and a credit-note return
    rx = signed_rx(conn, c, lines=(("Augmentin 625 Duo", "tablet", "1", "BD", 5),))
    shift, cashier = open_shift(conn, c)
    ws = {"clinic": c, "store": store, "item": aug, "batch": gbatch}
    sale = draft(conn, ws, "opd_rx", rx["rx"], rx["patient"], snapshot={"registration_number": "TSMC/2012/777"})
    sl = line(conn, ws, sale, 10, rx_item=rx["rx_items"][0])
    net = _q(conn, "SELECT net_paise FROM rx_sales WHERE id = %s", sale)
    _q(conn, "SELECT rx_complete_sale(%s, %s, %s::jsonb, %s, NULL, %s, 'Ravi', NULL, false, false)", c, sale,
       json.dumps([{"mode": "cash", "amount_paise": net}]), shift, cashier)
    for qty, mode in ((2, "cash"), (1, "credit_note")):
        _q(conn, "SELECT rx_record_return(%s, %s, %s::jsonb, 'unopened_unused', %s, %s, NULL, %s, 'Ravi', false, NULL)",
           c, sale, json.dumps([{"sale_item_id": sl, "qty_units": qty, "restock": True}]), mode,
           shift if mode == "cash" else None, cashier)
    # 3. IPD: indent, dispense, ward receipt, ward return
    indent = _raise(api, w, units=6).json()
    d = _dispense(api, w, indent).json()["dispense_id"]
    dl = str(_q(conn, "SELECT id FROM rx_dispense_lines WHERE dispense_id = %s", d))
    api.as_(w["u"]["nurse"])
    assert api.call("POST", f"/admin/rx/ipd/dispenses/{d}/receive", c).status_code == 200
    r = api.call("POST", f"/admin/rx/ipd/dispenses/{d}/returns", c, {
        "lines": [{"dispense_line_id": dl, "qty_units": 2, "restock": True}], "reason": "Switched to IV",
        "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 201, r.text
    # 4. transfer to an OT tray, received in full
    dlate = str(_q(conn, "SELECT id FROM rx_batches WHERE clinic_id = %s AND batch_no = 'DLATE'", c))
    wt = {"clinic": c, "central": store, "tray": _store(conn, c, "OT1", "ot_tray", parent=store), "item": dolo,
          "batch": dlate}
    t = transfer_draft(conn, wt, units=10)
    dispatch(conn, wt, t)
    receive(conn, wt, t, 10)
    # 5. a physical count of Dolo at the counter, counted right: zero variance
    a = start(conn, wt, scope="item_list", items=[dolo])
    lines = _q(conn, "SELECT json_agg(json_build_array(batch_id, system_units_at_start + movement_delta_since_start)) "
                     "FROM rx_stock_audit_lines WHERE audit_id = %s", a["audit_id"])
    counts(conn, wt, a["audit_id"], [(b, n) for b, n in lines])
    _q(conn, "SELECT rx_review_audit(%s, %s)", c, a["audit_id"])
    post(conn, wt, a["audit_id"], reasons=[])
    # 6. registers: the H chain holds the receipt, the sale and the returns, and verifies
    h = _q(conn, "SELECT count(*) FROM rx_register_entries WHERE clinic_id = %s AND register = 'H'", c)
    assert h >= 3
    for reg in ("H", "H1", "X", "narcotic", "psychotropic"):
        assert _q(conn, "SELECT ok FROM rx_register_verify(%s, %s)", c, reg) is True, reg
    # 7. chronic refill: a plan and a WhatsApp request
    plan = str(_q(conn, "INSERT INTO rx_refill_plans (clinic_id, patient_id, item_id, sale_item_id, units_per_day, "
                        "covered_until, remind_on, status, opt_in_by, opt_in_at) VALUES (%s, %s, %s, %s, 2, %s, %s, "
                        "'scheduled', %s, now()) RETURNING id", c, rx["patient"], aug, sl,
                  date.today() + timedelta(days=5), date.today() + timedelta(days=2), cashier))
    _q(conn, "INSERT INTO rx_refill_requests (clinic_id, plan_id, patient_id, channel) VALUES (%s, %s, %s, 'whatsapp')",
       c, plan, rx["patient"])
    # 8. GSTR-1, every CSV report and the A7 metrics
    api.as_(mgr)
    today = date.today()
    g = api.call("GET", "/admin/rx/reports/gstr1", c, month=today.month, year=today.year)
    assert g.status_code == 200 and g.json()["doc_summary"]["pharmacy_returns"]["total"] == 2, g.text
    from app.services.rx_reports import KINDS
    for kind in KINDS:
        assert api.call("GET", f"/admin/rx/reports/{kind}.csv", c).status_code == 200, kind
    assert api.call("POST", "/admin/rx/metrics/recompute", c).json()["rows"] >= 2
    assert api.call("GET", "/admin/rx/metrics", c).json()["rows"]
    # 9. ledger = balance everywhere, then purge v9 empties every RX table of this clinic
    assert not _q(conn, "SELECT count(*) FROM rx_ledger_check(%s)", c)
    used = [t for t in RX_TABLES if _q(conn, f"SELECT count(*) FROM {t} WHERE clinic_id = %s", c)]
    assert len(used) >= 30, sorted(set(RX_TABLES) - set(used))
    _q(conn, "SELECT rx_purge_clinic(%s)", c)
    left = {t: n for t in RX_TABLES if (n := _q(conn, f"SELECT count(*) FROM {t} WHERE clinic_id = %s", c))}
    assert left == {}


async def _done():
    return None
