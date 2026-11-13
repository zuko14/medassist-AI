"""Migration 129 (Diagnostic OS logistics and outsourcing, DX-M5) against real PostgreSQL.

Routing precedence (test > department > site default > fallback), tie refusal,
tubes split by destination at registration, outsourced orders carry the partner,
batches (create / add / dispatch / receive with missing, damaged and unexpected
tubes), one live batch per tube, the batch guard, rejection keeping the
destination, purge v5 and a guarded rollback.
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_dx_m3 import _err, _q, _register, lab  # noqa: F401  (fixture)

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "129_dx_logistics.sql", MIG / "rollback" / "129_rollback_dx_logistics.sql"


def unwind_129(cur) -> None:
    """Inside an open transaction: purge every clinic with DX-M5 rows, run the 129
    rollback. Called first by unwind_128 (newest first)."""
    from tests.test_migration_dx_m6 import unwind_130
    unwind_130(cur)   # newest first: 130's result rows sit on 129-era orders
    cur.execute("SELECT to_regclass('public.dx_batches') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT dx_purge_clinic(x.clinic_id) FROM (
                     SELECT clinic_id FROM dx_batches UNION SELECT clinic_id FROM dx_routing_rules
                     UNION SELECT clinic_id FROM dx_outsource_partners) x""")
    cur.execute("SELECT opd_purge_clinic(x.clinic_id) FROM (SELECT DISTINCT clinic_id FROM opd_invoices "
                "WHERE accession_id IS NOT NULL) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


@pytest.fixture
def m5(lab):
    c = lab["c"]
    _q(c, "SELECT dx_seed_rejection_reasons(%s)", lab["clinic"])
    hub_branch = str(_q(c, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Hub') RETURNING id", lab["clinic"]))
    lab["hub"] = str(_q(c, "INSERT INTO dx_sites (clinic_id, branch_id, kind, code, display_name) VALUES "
                           "(%s, %s, 'processing', 'HUB', 'Hub lab') RETURNING id", lab["clinic"], hub_branch))
    lab["partner"] = str(_q(c, "INSERT INTO dx_outsource_partners (clinic_id, name, legal_name, nabl_certificate_no) "
                               "VALUES (%s, 'RefLab', 'RefLab Pvt Ltd', 'MC-9') RETURNING id", lab["clinic"]))
    return lab


def _rule(s, priority=100, from_site=None, test=None, dept=None, to_site=None, partner=None):
    return str(_q(s["c"], """INSERT INTO dx_routing_rules (clinic_id, from_site_id, lab_test_id, department_id, to_site_id,
                             outsource_partner_id, priority) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  s["clinic"], from_site, test, dept, to_site, partner, priority))


def _route(s, test):
    return _q(s["c"], "SELECT dx_resolve_routing(%s, %s, %s, NULL)", s["clinic"], s["site"], s["T"][test])


def _collect(s, visit):
    return _q(s["c"], "SELECT dx_collect_samples(%s, %s, %s::jsonb, 'centre', NULL, 'col', %s)", s["clinic"],
              visit["visit"]["id"], json.dumps([{"id": x["id"]} for x in visit["samples"]]), uuid.uuid4().hex)


def _batch(s, to_site=None, partner=None, temp=4.0):
    return _q(s["c"], "SELECT dx_create_batch(%s, %s, %s, %s, 'Ravi (bike)', %s, NULL, 'runner')", s["clinic"], s["site"],
              to_site, partner, temp)


def _add(s, batch_id, codes):
    return _q(s["c"], "SELECT dx_batch_add(%s, %s, %s::text[], NULL, 'runner')", s["clinic"], batch_id, codes)


# ── routing ──────────────────────────────────────────────────────────────────

def test_routing_rule_precedence(m5):
    assert _route(m5, "cbc") == {"to_site_id": m5["site"], "outsource_partner_id": None, "rule_id": None,
                                 "matched": "fallback"}
    _rule(m5, to_site=m5["hub"])                                         # site default: everything to the hub
    assert _route(m5, "cbc")["matched"] == "site" and _route(m5, "cbc")["to_site_id"] == m5["hub"]
    _rule(m5, dept=m5["dept"], to_site=m5["site"], priority=500)          # department beats site default
    assert _route(m5, "cbc")["matched"] == "department" and _route(m5, "cbc")["to_site_id"] == m5["site"]
    _rule(m5, test=m5["T"]["hba1c"], partner=m5["partner"], priority=900)  # test beats department
    r = _route(m5, "hba1c")
    assert r["matched"] == "test" and r["outsource_partner_id"] == m5["partner"] and r["to_site_id"] is None
    # a rule for this site beats an any-site rule; then the lowest priority number
    _rule(m5, from_site=m5["site"], dept=m5["dept"], to_site=m5["hub"], priority=600)
    assert _route(m5, "cbc")["to_site_id"] == m5["hub"]
    _rule(m5, from_site=m5["site"], dept=m5["dept"], to_site=m5["site"], priority=10)
    assert _route(m5, "cbc")["to_site_id"] == m5["site"]
    # an inactive target is skipped
    _q(m5["c"], "UPDATE dx_outsource_partners SET is_active = false WHERE id = %s", m5["partner"])
    assert _route(m5, "hba1c")["matched"] == "department"


def test_conflicting_tie_refused(m5):
    _rule(m5, dept=m5["dept"], to_site=m5["hub"], priority=50)
    assert "uq_dx_routing_rules_tie" in _err(m5["c"], """INSERT INTO dx_routing_rules (clinic_id, department_id,
        outsource_partner_id, priority) VALUES (%s, %s, %s, 50)""", m5["clinic"], m5["dept"], m5["partner"])
    assert "dx_routing_rules_target_shape" in _err(m5["c"], "INSERT INTO dx_routing_rules (clinic_id, priority) "
                                                            "VALUES (%s, 7)", m5["clinic"])
    # an inactive rule does not block a new one
    _q(m5["c"], "UPDATE dx_routing_rules SET is_active = false WHERE clinic_id = %s", m5["clinic"])
    _rule(m5, dept=m5["dept"], partner=m5["partner"], priority=50)


def test_registration_splits_tubes_by_destination(m5):
    _rule(m5, test=m5["T"]["hba1c"], partner=m5["partner"])
    v = _register(m5, ["cbc", "hba1c", "lipid"])
    edta = [x for x in v["samples"] if x["container_id"] == m5["edta"]]
    assert len(edta) == 2                                             # same container, two destinations
    local = next(x for x in edta if x["outsource_partner_id"] is None)
    out = next(x for x in edta if x["outsource_partner_id"] == m5["partner"])
    assert local["processing_site_id"] == m5["site"] and out["processing_site_id"] is None
    assert local["barcode"] != out["barcode"]
    orders = _q(m5["c"], "SELECT jsonb_object_agg(item_name, jsonb_build_object('p', outsource_partner_id, 's', "
                         "dx_sample_id)) FROM opd_orders WHERE accession_id = %s", v["visit"]["id"])
    assert orders["HbA1c"] == {"p": m5["partner"], "s": out["id"]}
    assert orders["CBC"] == {"p": None, "s": local["id"]} and orders["Lipid Profile"]["p"] is None


def test_no_rule_keeps_pre_129_grouping(m5):
    v = _register(m5, ["cbc", "hba1c"])
    assert len(v["samples"]) == 1 and v["samples"][0]["processing_site_id"] == m5["site"]


# ── batches ──────────────────────────────────────────────────────────────────

def test_batch_lifecycle_with_discrepancies(m5):
    c = m5["c"]
    _rule(m5, to_site=m5["hub"])
    v1, v2, v3 = _register(m5, ["cbc"]), _register(m5, ["lipid"]), _register(m5, ["hba1c"])
    for v in (v1, v2, v3):
        _collect(m5, v)
    b = _batch(m5, to_site=m5["hub"])
    assert b["status"] == "open" and b["batch_number"].startswith("TRN-") and float(b["dispatch_temp_c"]) == 4.0
    codes = [v["samples"][0]["barcode"] for v in (v1, v2, v3)]
    out = _add(m5, b["id"], codes + [codes[0]])                        # a double scan is ignored
    assert out["added"] == 3 and len(out["samples"]) == 3
    assert "phone" not in json.dumps(out)                              # data-minimised read model
    assert out["samples"][0]["patient_name"] == "Asha" and out["samples"][0]["tests"] == ["CBC"]
    assert _add(m5, b["id"], codes[:1])["added"] == 0                  # replay
    # one tube damaged on the way (rejected while in transit), one never arrives
    d = _q(c, "SELECT dx_dispatch_batch(%s, %s, 5.5, 'cool box', NULL, 'runner')", m5["clinic"], b["id"])
    assert d["status"] == "dispatched" and float(d["dispatch_temp_c"]) == 5.5 and d["replayed"] is False
    assert {x["status"] for x in d["samples"]} == {"in_transit"}
    assert _q(c, "SELECT dx_dispatch_batch(%s, %s, 5.5, NULL, NULL, 'r')", m5["clinic"], b["id"])["replayed"] is True
    _q(c, "SELECT dx_reject_sample(%s, %s, 'LEAKED', NULL, false, NULL, 'hub')", m5["clinic"], v3["samples"][0]["id"])
    r = _q(c, "SELECT dx_receive_batch(%s, %s, %s::text[], 6.0, 'box lid open', NULL, 'hub')", m5["clinic"], b["id"],
           [codes[0], "999999999999"])
    assert r["status"] == "received_with_discrepancy" and float(r["receipt_temp_c"]) == 6.0
    assert r["discrepancy"] == {"received": [codes[0]], "missing": [codes[1]], "damaged": [codes[2]],
                                "unexpected": ["999999999999"]}
    by = {x["barcode"]: x for x in r["samples"]}
    assert by[codes[0]]["receipt"] == "received" and by[codes[0]]["status"] == "received"
    assert by[codes[1]]["receipt"] == "missing" and by[codes[1]]["status"] == "in_transit"
    assert by[codes[2]]["receipt"] == "damaged" and by[codes[2]]["receipt_notes"] == "rejected: LEAKED"
    assert _q(c, "SELECT received_site_id FROM dx_samples WHERE barcode = %s AND clinic_id = %s", codes[0],
              m5["clinic"]) == m5["hub"]
    kinds = _q(c, "SELECT jsonb_agg(kind ORDER BY id) FROM dx_events WHERE accession_id = %s", v1["visit"]["id"])
    assert kinds == ["registered", "collected", "dispatched", "received"]
    assert _q(c, "SELECT count(*) FROM dx_events WHERE accession_id = %s AND kind = 'missing_in_transit'",
              v2["visit"]["id"]) == 1
    again = _q(c, "SELECT dx_receive_batch(%s, %s, '{}'::text[], NULL, NULL, NULL, 'hub')", m5["clinic"], b["id"])
    assert again["replayed"] is True and again["status"] == "received_with_discrepancy"


def test_clean_receipt(m5):
    _rule(m5, to_site=m5["hub"])
    v = _register(m5, ["cbc"])
    _collect(m5, v)
    b = _batch(m5, to_site=m5["hub"])
    _add(m5, b["id"], [v["samples"][0]["barcode"]])
    _q(m5["c"], "SELECT dx_dispatch_batch(%s, %s, NULL, NULL, NULL, 'r')", m5["clinic"], b["id"])
    r = _q(m5["c"], "SELECT dx_receive_batch(%s, %s, %s::text[], 4, NULL, NULL, 'hub')", m5["clinic"], b["id"],
           [v["samples"][0]["barcode"]])
    assert r["status"] == "received" and r["notes"] is None


def test_batch_add_refusals(m5):
    c = m5["c"]
    _rule(m5, test=m5["T"]["hba1c"], partner=m5["partner"])
    v = _register(m5, ["cbc", "hba1c"])
    local = next(x for x in v["samples"] if x["outsource_partner_id"] is None)
    out = next(x for x in v["samples"] if x["outsource_partner_id"])
    to_partner = _batch(m5, partner=m5["partner"])
    assert "dx_sample_not_collected" in _err(c, "SELECT dx_batch_add(%s, %s, %s::text[], NULL, 'r')", m5["clinic"],
                                             to_partner["id"], [out["barcode"]])
    _collect(m5, v)
    assert "dx_batch_wrong_destination" in _err(c, "SELECT dx_batch_add(%s, %s, %s::text[], NULL, 'r')", m5["clinic"],
                                                to_partner["id"], [local["barcode"]])
    assert "dx_sample_not_found:NOPE123" in _err(c, "SELECT dx_batch_add(%s, %s, %s::text[], NULL, 'r')", m5["clinic"],
                                                 to_partner["id"], ["NOPE123"])
    _add(m5, to_partner["id"], [out["barcode"]])
    second = _batch(m5, partner=m5["partner"])
    assert "dx_sample_in_live_batch" in _err(c, "SELECT dx_batch_add(%s, %s, %s::text[], NULL, 'r')", m5["clinic"],
                                             second["id"], [out["barcode"]])
    assert "dx_batch_empty" in _err(c, "SELECT dx_dispatch_batch(%s, %s, NULL, NULL, NULL, 'r')", m5["clinic"],
                                    second["id"])
    assert "dx_batch_not_dispatched" in _err(c, "SELECT dx_receive_batch(%s, %s, '{}'::text[], NULL, NULL, NULL, 'r')",
                                             m5["clinic"], to_partner["id"])
    # the outsourced tube is handed to the partner: received with no Kriya site
    _q(c, "SELECT dx_dispatch_batch(%s, %s, NULL, NULL, NULL, 'r')", m5["clinic"], to_partner["id"])
    assert "dx_batch_not_open" in _err(c, "SELECT dx_batch_add(%s, %s, %s::text[], NULL, 'r')", m5["clinic"],
                                       to_partner["id"], [local["barcode"]])
    r = _q(c, "SELECT dx_receive_batch(%s, %s, %s::text[], NULL, NULL, NULL, 'r')", m5["clinic"], to_partner["id"],
           [out["barcode"]])
    assert r["status"] == "received" and r["samples"][0]["status"] == "received"
    assert "dx_batch_same_site" in _err(c, "SELECT dx_create_batch(%s, %s, %s, NULL, NULL, NULL, NULL, 'r')",
                                        m5["clinic"], m5["site"], m5["site"])
    assert "dx_batch_destination_invalid" in _err(c, "SELECT dx_create_batch(%s, %s, %s, %s, NULL, NULL, NULL, 'r')",
                                                  m5["clinic"], m5["site"], m5["hub"], m5["partner"])


def test_one_live_batch_index_and_guard(m5):
    c = m5["c"]
    _rule(m5, to_site=m5["hub"])
    v = _register(m5, ["cbc"])
    _collect(m5, v)
    b = _batch(m5, to_site=m5["hub"])
    _add(m5, b["id"], [v["samples"][0]["barcode"]])
    assert "dx_managed:dx_batches" in _err(c, "UPDATE dx_batches SET status = 'received' WHERE id = %s", b["id"])
    assert "dx_managed:dx_batch_samples" in _err(c, "DELETE FROM dx_batch_samples WHERE batch_id = %s", b["id"])
    b2 = _batch(m5, to_site=m5["hub"])
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (m5["clinic"],))
        with pytest.raises(psycopg2.Error) as e:
            cur.execute("INSERT INTO dx_batch_samples (clinic_id, batch_id, sample_id) VALUES (%s, %s, %s)",
                        (m5["clinic"], b2["id"], v["samples"][0]["id"]))
        assert "uq_dx_sample_one_live_batch" in str(e.value)
    finally:
        cur.execute("ROLLBACK")


def test_rejection_keeps_destination(m5):
    _rule(m5, test=m5["T"]["hba1c"], partner=m5["partner"])
    v = _register(m5, ["hba1c"])
    _collect(m5, v)
    out = _q(m5["c"], "SELECT dx_reject_sample(%s, %s, 'CLOTTED', NULL, true, NULL, 't')", m5["clinic"],
             v["samples"][0]["id"])
    new = next(x for x in out["samples"] if x["id"] == out["replacement_sample_id"])
    assert new["outsource_partner_id"] == m5["partner"] and new["processing_site_id"] is None
    assert _q(m5["c"], "SELECT outsource_partner_id FROM opd_orders WHERE dx_sample_id = %s", new["id"]) == m5["partner"]


# ── purge, rollback ──────────────────────────────────────────────────────────

def test_purge_v5_removes_logistics_rows(m5):
    c = m5["c"]
    _rule(m5, to_site=m5["hub"])
    v = _register(m5, ["cbc"])
    _collect(m5, v)
    b = _batch(m5, to_site=m5["hub"])
    _add(m5, b["id"], [v["samples"][0]["barcode"]])
    out = _q(c, "SELECT dx_purge_clinic(%s)", m5["clinic"])
    assert out["batches"] == 1 and out["routing_rules"] == 1 and out["visits"] == 1
    for t in ("dx_batches", "dx_batch_samples", "dx_routing_rules", "dx_outsource_partners", "dx_samples"):
        assert _q(c, f"SELECT count(*) FROM {t} WHERE clinic_id = %s", m5["clinic"]) == 0


def test_rollback_refuses_logistics_rows_then_reapplies(m5):
    _rule(m5, to_site=m5["hub"])
    cur = m5["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "129_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_129(cur)
        cur.execute("SELECT to_regclass('public.dx_batches'), to_regprocedure('dx_resolve_routing(uuid,uuid,uuid,uuid)')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE column_name = 'outsource_partner_id' "
                    "AND table_name IN ('opd_orders', 'dx_samples')")
        assert cur.fetchone() == (0,)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'dx\\_%%'
                         AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x WHERE x LIKE 'search_path=%%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
