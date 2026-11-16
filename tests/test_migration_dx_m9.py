"""Migration 133 (Diagnostic OS instruments and QC — DX-M9) on real PostgreSQL.

Agents and instruments bound to a site of the same clinic; raw instrument results replay-safe and immutable
except their mapping; the QC run log append-only; value meta keeps the instrument result id; purge v9 and a
guarded rollback that re-applies cleanly twice.
"""

import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_dx_m3 import _err, _q, lab  # noqa: F401  (fixture)

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "133_dx_instruments_qc.sql", MIG / "rollback" / "133_rollback_dx_instruments_qc.sql"


def unwind_133(cur) -> None:
    """Inside an open transaction: purge every clinic with DX-M9 rows, run the 133 rollback.
    Called first by unwind_132 (newest first)."""
    from tests.test_migration_rx_m1 import unwind_134
    unwind_134(cur)  # newest first: 134's rx_stores sits on 103's branches
    cur.execute("SELECT to_regclass('public.dx_bridge_agents') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT dx_purge_clinic(x.clinic_id) FROM (
                     SELECT clinic_id FROM dx_bridge_agents UNION SELECT clinic_id FROM dx_qc_results
                     UNION SELECT clinic_id FROM dx_instrument_results) x""")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _agent(s, prefix=None):
    return str(_q(s["c"], "INSERT INTO dx_bridge_agents (clinic_id, site_id, name, key_hash, key_prefix) VALUES "
                          "(%s, %s, 'Lab PC', %s, %s) RETURNING id", s["clinic"], s["site"], "a" * 64,
                  prefix or "kba_" + uuid.uuid4().hex[:8]))


def _instrument(s, agent):
    return str(_q(s["c"], "INSERT INTO dx_instruments (clinic_id, site_id, agent_id, name, protocol) VALUES "
                          "(%s, %s, %s, 'Sysmex XN', 'astm') RETURNING id", s["clinic"], s["site"], agent))


def _iresult(s, inst, msg="m1"):
    return str(_q(s["c"], "INSERT INTO dx_instrument_results (clinic_id, instrument_id, message_id, barcode, raw_message, "
                          "parsed) VALUES (%s, %s, %s, 'B1', 'H|...', '[]') RETURNING id", s["clinic"], inst, msg))


def _material(s, inst):
    return str(_q(s["c"], "INSERT INTO dx_qc_materials (clinic_id, instrument_id, parameter_code, level, lot, target_mean, "
                          "target_sd, valid_from, valid_to) VALUES (%s, %s, 'HGB', 'L1', 'LOT9', 12.5, 0.3, CURRENT_DATE, "
                          "CURRENT_DATE + 90) RETURNING id", s["clinic"], inst))


def test_agent_and_instrument_bound_to_own_site(lab):
    c = lab["c"]
    agent = _agent(lab)
    _instrument(lab, agent)
    other = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('Other', %s, 'diagstream', "
                      "true) RETURNING id", "+9171" + uuid.uuid4().hex[:8]))
    assert "dx_bridge_agents_site_fk" in _err(c, "INSERT INTO dx_bridge_agents (clinic_id, site_id, name, key_hash, "
                                                 "key_prefix) VALUES (%s, %s, 'x pc', %s, 'kba_abcdefgh')",
                                              other, lab["site"], "a" * 64)
    assert "dx_bridge_agents_prefix_key" in _err(c, "INSERT INTO dx_bridge_agents (clinic_id, site_id, name, key_hash, "
                                                    "key_prefix) SELECT clinic_id, site_id, 'dup', key_hash, key_prefix "
                                                    "FROM dx_bridge_agents WHERE id = %s", agent)
    assert "key_hash" in _err(c, "INSERT INTO dx_bridge_agents (clinic_id, site_id, name, key_hash, key_prefix) VALUES "
                                 "(%s, %s, 'pc 2', 'not-a-hash', 'kba_zzzzzzzz')", lab["clinic"], lab["site"])
    assert "protocol" in _err(c, "INSERT INTO dx_instruments (clinic_id, site_id, agent_id, name, protocol) VALUES "
                                 "(%s, %s, %s, 'Box', 'csv')", lab["clinic"], lab["site"], agent)


def test_instrument_results_replay_safe_and_raw_immutable(lab):
    c = lab["c"]
    inst = _instrument(lab, _agent(lab))
    rid = _iresult(lab, inst)
    assert "dx_instrument_results_message_key" in _err(
        c, "INSERT INTO dx_instrument_results (clinic_id, instrument_id, message_id, barcode, raw_message, parsed) "
           "VALUES (%s, %s, 'm1', 'B1', 'x', '[]')", lab["clinic"], inst)
    _q(c, "UPDATE dx_instrument_results SET mapping_status = 'ignored', mapped_by_name = 'tech' WHERE id = %s", rid)
    assert "dx_instrument_result_append_only" in _err(c, "UPDATE dx_instrument_results SET raw_message = 'y' WHERE id = %s",
                                                      rid)
    assert "dx_instrument_result_append_only" in _err(c, "DELETE FROM dx_instrument_results WHERE id = %s", rid)
    assert "raw_message" in _err(c, "INSERT INTO dx_instrument_results (clinic_id, instrument_id, message_id, barcode, "
                                    "raw_message, parsed) VALUES (%s, %s, 'm2', 'B', repeat('x', 200001), '[]')",
                                 lab["clinic"], inst)


def test_qc_results_append_only(lab):
    c = lab["c"]
    mat = _material(lab, _instrument(lab, _agent(lab)))
    qid = str(_q(c, "INSERT INTO dx_qc_results (clinic_id, material_id, value, z_score, westgard_flags) VALUES "
                    "(%s, %s, 13.2, 2.33, '{1_2s}') RETURNING id", lab["clinic"], mat))
    assert "dx_qc_result_append_only" in _err(c, "UPDATE dx_qc_results SET accepted = false WHERE id = %s", qid)
    assert "dx_qc_result_append_only" in _err(c, "DELETE FROM dx_qc_results WHERE id = %s", qid)
    assert "dx_qc_results_flags" in _err(c, "INSERT INTO dx_qc_results (clinic_id, material_id, value, z_score, "
                                            "westgard_flags) VALUES (%s, %s, 1, 0, '{bogus}')", lab["clinic"], mat)
    assert "target_sd" in _err(c, "INSERT INTO dx_qc_materials (clinic_id, instrument_id, parameter_code, level, lot, "
                                  "target_mean, target_sd, valid_from, valid_to) SELECT clinic_id, instrument_id, 'X', "
                                  "'L1', 'L', 1, 0, CURRENT_DATE, CURRENT_DATE FROM dx_qc_materials WHERE id = %s", mat)


def test_purge_v9_removes_instrument_and_qc_rows(lab):
    c, clinic = lab["c"], lab["clinic"]
    inst = _instrument(lab, _agent(lab))
    _iresult(lab, inst)
    _q(c, "INSERT INTO dx_qc_results (clinic_id, material_id, value, z_score) VALUES (%s, %s, 12.5, 0)", clinic,
       _material(lab, inst))
    out = _q(c, "SELECT dx_purge_clinic(%s)", clinic)
    assert (out["qc_results"], out["instrument_results"], out["bridge_agents"]) == (1, 1, 1)
    for tbl in ("dx_bridge_agents", "dx_instruments", "dx_instrument_results", "dx_qc_materials", "dx_qc_results"):
        assert _q(c, f"SELECT count(*) FROM {tbl} WHERE clinic_id = %s", clinic) == 0


def test_rollback_refuses_rows_then_reapplies(lab):
    _iresult(lab, _instrument(lab, _agent(lab)))
    cur = lab["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "133_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_133(cur)
        cur.execute("SELECT to_regclass('public.dx_bridge_agents'), "
                    "position('v9 (133)' in pg_get_functiondef('dx_purge_clinic(uuid)'::regprocedure)), "
                    "position('instrument_result_id' in pg_get_functiondef("
                    "'dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)'::regprocedure))")
        assert cur.fetchone() == (None, 0, 0)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("SELECT (length(pg_get_functiondef('dx_purge_clinic(uuid)'::regprocedure)) "
                    "- length(replace(pg_get_functiondef('dx_purge_clinic(uuid)'::regprocedure), 'v9 (133)', ''))) / 8")
        assert cur.fetchone()[0] == 1          # re-running never patches twice
    finally:
        cur.execute("ROLLBACK")
