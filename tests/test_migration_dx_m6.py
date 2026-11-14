"""Migration 130 (Diagnostic OS result workflow, DX-M6) against real PostgreSQL.

Record results (DX orders only, tube received, value meta replaced, critical
detection), verification (stale / self / returned), the gate on lab_results
(levels 3 only, DX only — OPD signing untouched), approval single and batch
(per-item outcome, not-normal refused), the critical call before approval,
attachments only on open results, append-only guards, purge v6 and a guarded
rollback.
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_115_orders import world as _orders_world  # noqa: F401  (fixture)
from tests.test_migration_118_lis import _collect as _opd_collect, _record as _opd_record, lab as opd_lab  # noqa: F401
from tests.test_migration_dx_m3 import _approve as _approve_test, _err, _q, _register, lab  # noqa: F401

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "130_dx_result_workflow.sql", MIG / "rollback" / "130_rollback_dx_result_workflow.sql"
SIGNER = {"name": "Dr. Rao", "registration_number": "R/1", "registration_council": "TSMC"}


def unwind_130(cur) -> None:
    """Inside an open transaction: purge every clinic with DX-M6 rows, run the 130
    rollback. Called first by unwind_129 (newest first)."""
    from tests.test_migration_dx_m7 import unwind_131
    unwind_131(cur)   # newest first: 131's releases reference 130-era results
    cur.execute("SELECT to_regclass('public.dx_result_value_meta') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT dx_purge_clinic(x.clinic_id) FROM (
                     SELECT clinic_id FROM dx_result_value_meta UNION SELECT clinic_id FROM dx_result_verifications
                     UNION SELECT clinic_id FROM dx_critical_events UNION SELECT clinic_id FROM dx_result_attachments) x""")
    cur.execute(DOWN.read_text(encoding="utf-8"))


@pytest.fixture
def m6(lab):
    """A received CBC tube and LIPID tube of one visit (same-site receipt)."""
    c = lab["c"]
    _q(c, "SELECT dx_seed_rejection_reasons(%s)", lab["clinic"])
    _q(c, "UPDATE lab_test_parameters SET normal_low = 10, normal_high = 20, critical_low = 5, critical_high = 30 "
          "WHERE lab_test_id = %s", lab["T"]["cbc"])
    _approve_test(c, lab["clinic"], lab["T"]["cbc"], lab["doctor"])      # a band edit clears the approval (P2)
    v = _register(lab, ["cbc", "lipid"])
    lab["visit"] = v
    _q(c, "SELECT dx_collect_samples(%s, %s, %s::jsonb, 'centre', NULL, 'col', %s)", lab["clinic"], v["visit"]["id"],
       json.dumps([{"id": s["id"]} for s in v["samples"]]), uuid.uuid4().hex)
    for s in v["samples"]:
        _q(c, "SELECT dx_receive_sample(%s, %s, %s, NULL, 'rcv')", lab["clinic"], s["barcode"], lab["site"])
    lab["orders"] = {o["item_name"]: o for o in v["orders"]}
    lab["param"] = {k: str(_q(c, "SELECT id FROM lab_test_parameters WHERE lab_test_id = %s", lab["T"][k]))
                    for k in ("cbc", "lipid")}
    lab["tech"], lab["sup"], lab["path"] = (str(uuid.uuid4()) for _ in range(3))
    return lab


def _levels(s, n, call=True):
    _q(s["c"], "UPDATE clinics SET dx_settings = dx_settings || %s::jsonb WHERE id = %s",
       json.dumps({"approval_levels": n, "critical_call_required": call}), s["clinic"])


def _enter(s, test="CBC", value="14", flag="normal", actor=None, meta=None, critical=False):
    key = "cbc" if test == "CBC" else "lipid"
    vals = [{"parameter_id": s["param"][key], "parameter_name": test, "numeric_value": value, "flag": flag}]
    return _q(s["c"], "SELECT dx_record_results(%s, %s, %s::jsonb, %s::jsonb, %s, NULL, %s, 'tech')", s["clinic"],
              s["orders"][test]["id"], json.dumps(vals), json.dumps(meta or []), critical, actor or s["tech"])


def _verify(s, r, decision="verified", actor=None, comment=None):
    return _q(s["c"], "SELECT dx_verify_result(%s, %s, %s, %s, %s, %s, 'sup')", s["clinic"], r["id"], r["updated_at"],
              decision, comment, actor or s["sup"])


def _approve(s, results, batch=False):
    items = [{"result_id": r["id"], "expected_updated_at": r["updated_at"], "pathologist_snapshot": SIGNER,
              "letterhead_snapshot": {}, "patient_snapshot": {}} for r in results]
    return _q(s["c"], "SELECT dx_approve_results(%s, %s::jsonb, %s, 'Dr. Rao', %s)", s["clinic"], json.dumps(items),
              s["path"], batch)


def _fresh(s, r):
    return _q(s["c"], "SELECT to_jsonb(r) FROM lab_results r WHERE id = %s", r["id"])


# ── entry ────────────────────────────────────────────────────────────────────

def test_record_results_meta_and_event(m6):
    c = m6["c"]
    r = _enter(m6, meta=[{"parameter_id": m6["param"]["cbc"], "previous_value": "12", "delta_pct": 16.67,
                          "delta_failed": True}])
    assert r["status"] == "entered" and r["created"] is True and r["critical_event_id"] is None
    assert _q(c, "SELECT delta_failed FROM dx_result_value_meta WHERE result_id = %s", r["id"]) is True
    r2 = _enter(m6, value="15")                                         # re-entry replaces the meta
    assert r2["id"] == r["id"] and _q(c, "SELECT count(*) FROM dx_result_value_meta WHERE result_id = %s", r["id"]) == 0
    assert _q(c, "SELECT status FROM opd_orders WHERE id = %s", m6["orders"]["CBC"]["id"]) == "in_progress"
    assert _q(c, "SELECT count(*) FROM dx_events WHERE order_id = %s AND kind = 'results_entered'",
              m6["orders"]["CBC"]["id"]) == 2
    assert "dx_managed:dx_result_value_meta" in _err(c, """INSERT INTO dx_result_value_meta (clinic_id, result_id,
        parameter_id) VALUES (%s, %s, %s)""", m6["clinic"], r["id"], m6["param"]["cbc"])


def test_entry_needs_a_received_tube(lab):
    v = _register(lab, ["cbc"])
    pid = str(_q(lab["c"], "SELECT id FROM lab_test_parameters WHERE lab_test_id = %s", lab["T"]["cbc"]))
    vals = [{"parameter_id": pid, "parameter_name": "CBC", "numeric_value": "1"}]
    assert "dx_sample_awaiting_receipt" in _err(lab["c"], "SELECT dx_record_results(%s, %s, %s::jsonb, '[]', false, NULL, "
                                                          "NULL, 't')", lab["clinic"], v["orders"][0]["id"],
                                                json.dumps(vals))


def test_critical_detection_and_call_before_approval(m6):
    c = m6["c"]
    _levels(m6, 2)
    r = _enter(m6, value="40", flag="critical_high", critical=True)
    ev = r["critical_event_id"]
    assert _q(c, "SELECT parameters FROM dx_critical_events WHERE id = %s", ev)[0]["flag"] == "critical_high"
    out = _approve(m6, [r])
    assert out["signed"] == 0 and "dx_critical_not_notified" in out["items"][0]["error"]
    assert _q(c, "SELECT dx_critical_pending(%s, %s)", m6["clinic"], r["id"]) is True
    # a call without read-back does not count; with read-back it does
    _q(c, "SELECT dx_log_critical_call(%s, %s, 'Dr. Anuradha', 'referrer', 'phone', false, 'no answer', NULL, 'sup')",
       m6["clinic"], ev)
    assert _q(c, "SELECT dx_critical_pending(%s, %s)", m6["clinic"], r["id"]) is True
    call = _q(c, "SELECT dx_log_critical_call(%s, %s, 'Dr. Anuradha', 'referrer', 'phone', true, NULL, NULL, 'sup')",
              m6["clinic"], ev)
    assert call["read_back_confirmed"] is True and call["notified_at"] is not None
    assert "dx_append_only:dx_critical_events" in _err(c, "UPDATE dx_critical_events SET notes = 'x' WHERE id = %s", ev)
    out = _approve(m6, [_fresh(m6, r)])
    assert out["signed"] == 1 and out["items"][0]["status"] == "validated"
    # a lab that switched the call rule off approves without one
    _levels(m6, 2, call=False)
    r2 = _enter(m6, test="Lipid Profile", value="40", flag="critical_high", critical=True)
    assert _approve(m6, [r2])["signed"] == 1


# ── verification gate (ADR-DX-06) ────────────────────────────────────────────

def test_levels_three_needs_a_verification_by_someone_else(m6):
    c = m6["c"]
    _levels(m6, 3)
    r = _enter(m6)
    out = _approve(m6, [r])
    assert out["refused"] == 1 and "dx_verification_required" in out["items"][0]["error"]
    assert "dx_self_verification" in _err(c, "SELECT dx_verify_result(%s, %s, %s, 'verified', NULL, %s, 't')",
                                          m6["clinic"], r["id"], r["updated_at"], m6["tech"])
    # a re-entry after the verification retires it
    _verify(m6, r)
    r2 = _enter(m6, value="16")
    assert "dx_verification_required" in _approve(m6, [r2])["items"][0]["error"]
    assert "lis_result_changed" in _err(c, "SELECT dx_verify_result(%s, %s, %s, 'verified', NULL, %s, 's')",
                                        m6["clinic"], r["id"], r["updated_at"], m6["sup"])
    _verify(m6, r2)
    out = _approve(m6, [r2])
    assert out["signed"] == 1, out
    assert _q(c, "SELECT status FROM opd_orders WHERE id = %s", m6["orders"]["CBC"]["id"]) == "completed"
    assert "dx_append_only:dx_result_verifications" in _err(c, "DELETE FROM dx_result_verifications WHERE result_id = %s",
                                                            r["id"])


def test_returned_after_verified_blocks_approval(m6):
    _levels(m6, 3)
    r = _enter(m6)
    _verify(m6, r)
    assert "dx_result_verifications_returned_comment" in _err(m6["c"], "SELECT dx_verify_result(%s, %s, %s, 'returned', "
                                                                       "'', %s, 'p')", m6["clinic"], r["id"],
                                                              r["updated_at"], m6["path"])
    _verify(m6, r, "returned", actor=m6["path"], comment="recheck smear")
    assert "dx_verification_required" in _approve(m6, [r])["items"][0]["error"]


def test_gate_ignores_opd_results(opd_lab):
    """A clinic on three levels still signs its OPD / IPD lab results exactly as before."""
    w = opd_lab
    _q(w["c"], "UPDATE clinics SET dx_settings = '{\"approval_levels\": 3}'::jsonb WHERE id = %s", w["clinic"])
    _opd_collect(w)
    r = _opd_record(w["c"], w)
    signed = _q(w["c"], "SELECT lis_validate_result(%s, %s, %s, %s, %s::jsonb, '{}'::jsonb, '{}'::jsonb)", w["clinic"],
                r["id"], r["updated_at"], str(uuid.uuid4()), json.dumps(SIGNER))
    assert signed["status"] == "validated"


# ── approval ─────────────────────────────────────────────────────────────────

def test_batch_approval_signs_normal_and_refuses_the_rest(m6):
    _levels(m6, 2)
    ok = _enter(m6)
    high = _enter(m6, test="Lipid Profile", value="300", flag="high")
    out = _approve(m6, [ok, high, {"id": str(uuid.uuid4()), "updated_at": ok["updated_at"]}], batch=True)
    assert out["signed"] == 1 and out["refused"] == 2
    by = {i["result_id"]: i for i in out["items"]}
    assert by[ok["id"]]["ok"] is True and "dx_batch_not_normal" in by[high["id"]]["error"]
    # a double click replays; the abnormal one is still approvable on its own
    assert _approve(m6, [_fresh(m6, ok)])["items"][0]["replayed"] is True
    assert _approve(m6, [high])["signed"] == 1
    assert "dx_approve_items_invalid" in _err(m6["c"], "SELECT dx_approve_results(%s, '[]'::jsonb, %s, 'p', false)",
                                              m6["clinic"], m6["path"])


# ── attachments ──────────────────────────────────────────────────────────────

def test_attachments_only_while_open(m6):
    c = m6["c"]
    _levels(m6, 2)
    r = _enter(m6)
    sha = "a" * 64
    ins = """INSERT INTO dx_result_attachments (clinic_id, result_id, kind, storage_path, file_name, sha256)
             VALUES (%s, %s, 'pdf', 'x/y.pdf', 'partner.pdf', %s) RETURNING id"""
    att = _q(c, ins, m6["clinic"], r["id"], sha)
    assert "dx_append_only:dx_result_attachments" in _err(c, "UPDATE dx_result_attachments SET caption = 'x' WHERE id = %s",
                                                          att)
    _approve(m6, [r])
    assert "dx_attachment_locked" in _err(c, ins, m6["clinic"], r["id"], sha)


# ── purge, rollback ──────────────────────────────────────────────────────────

def test_purge_v6_removes_result_rows(m6):
    c = m6["c"]
    _levels(m6, 3)
    r = _enter(m6, value="40", flag="critical_high", critical=True)
    _verify(m6, r)
    out = _q(c, "SELECT dx_purge_clinic(%s)", m6["clinic"])
    assert out["critical_events"] == 1 and out["visits"] == 1
    for t in ("dx_result_value_meta", "dx_result_verifications", "dx_critical_events", "dx_result_attachments",
              "lab_results"):
        assert _q(c, f"SELECT count(*) FROM {t} WHERE clinic_id = %s", m6["clinic"]) == 0


def test_rollback_refuses_result_rows_then_reapplies(m6):
    _levels(m6, 3)
    r = _enter(m6)
    _verify(m6, r)
    cur = m6["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "130_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_130(cur)
        cur.execute("SELECT to_regclass('public.dx_result_verifications'), "
                    "(SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_dx_lab_results_gate')")
        assert cur.fetchone() == (None, 0)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'dx\\_%%'
                         AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x WHERE x LIKE 'search_path=%%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
