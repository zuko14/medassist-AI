"""Migration 128 (Diagnostic OS collection and rejection, DX-M4) against real PostgreSQL.

Bind a pre-printed label, collect (orders follow with the barcode, visit goes
in_process, replay, scan mismatch, collect-before-payment), reject with
recollection (ADR-DX-05: fresh sequence, replacement orders, refused once a
result is signed), same-site receipt, storage, the sample guard (barcode
immutable, no direct writes), doorstep money and its handover (AUDIT A14),
the label-print log, purge v4 and a guarded rollback.
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_dx_m3 import _err, _q, _register, lab  # noqa: F401  (fixture)

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "128_dx_collection.sql", MIG / "rollback" / "128_rollback_dx_collection.sql"


def unwind_128(cur) -> None:
    """Inside an open transaction: purge every clinic with DX-M4 rows, run the 128
    rollback. Called first by unwind_127 (newest first)."""
    cur.execute("SELECT to_regclass('public.dx_field_collections') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT dx_purge_clinic(x.clinic_id) FROM (
                     SELECT clinic_id FROM dx_field_collections UNION SELECT clinic_id FROM dx_label_prints
                     UNION SELECT clinic_id FROM dx_samples WHERE status <> 'pending_collection' OR recollect_of IS NOT NULL
                     UNION SELECT clinic_id FROM opd_orders WHERE source = 'dx' AND replaces_order_id IS NOT NULL) x""")
    cur.execute("SELECT opd_purge_clinic(x.clinic_id) FROM (SELECT DISTINCT clinic_id FROM opd_invoices "
                "WHERE accession_id IS NOT NULL) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


@pytest.fixture
def m4(lab):
    _q(lab["c"], "SELECT dx_seed_rejection_reasons(%s)", lab["clinic"])
    return lab


def _collect(s, visit, samples=None, key=None, mode="centre"):
    rows = samples if samples is not None else [{"id": x["id"]} for x in visit["samples"]]
    return _q(s["c"], "SELECT dx_collect_samples(%s, %s, %s::jsonb, %s, NULL, 'col', %s)", s["clinic"],
              visit["visit"]["id"], json.dumps(rows), mode, key or uuid.uuid4().hex)


def _reject(s, sample_id, code="HEMOLYSED", recollect=True, notes=None):
    return _q(s["c"], "SELECT dx_reject_sample(%s, %s, %s, %s, %s, NULL, 'tech')", s["clinic"], sample_id, code, notes,
              recollect)


def _orders(s, visit_id):
    return _q(s["c"], "SELECT jsonb_agg(to_jsonb(o) ORDER BY created_at, id) FROM opd_orders o WHERE accession_id = %s",
              visit_id) or []


# ── reasons ──────────────────────────────────────────────────────────────────

def test_seed_reasons_once(m4):
    assert _q(m4["c"], "SELECT count(*) FROM dx_rejection_reasons WHERE clinic_id = %s", m4["clinic"]) == 11
    assert _q(m4["c"], "SELECT dx_seed_rejection_reasons(%s)", m4["clinic"]) == 0          # idempotent


# ── collect ──────────────────────────────────────────────────────────────────

def test_collect_moves_tubes_orders_and_visit(m4):
    v = _register(m4, ["lipid", "cbc", "hba1c"])
    out = _collect(m4, v, key="collect-key-1")
    assert out["replayed"] is False and out["visit"]["status"] == "in_process"
    assert {x["status"] for x in out["samples"]} == {"collected"}
    by_sample = {x["id"]: x["barcode"] for x in out["samples"]}
    for o in _orders(m4, v["visit"]["id"]):
        assert o["status"] == "sample_collected" and o["specimen_barcode"] == by_sample[o["dx_sample_id"]]
        assert o["sample_collected_by_name"] == "col"
    # retry with the same key: same answer, no second event
    again = _collect(m4, v, key="collect-key-1")
    assert again["replayed"] is True
    assert _q(m4["c"], "SELECT count(*) FROM dx_events WHERE accession_id = %s AND kind = 'collected'",
              v["visit"]["id"]) == 1
    assert "dx_sample_not_pending" in _err(m4["c"], "SELECT dx_collect_samples(%s, %s, %s::jsonb, 'centre', NULL, 'c', 'k-other-1')",
                                          m4["clinic"], v["visit"]["id"], json.dumps([{"id": v["samples"][0]["id"]}]))


def test_scanned_barcode_must_match_the_tube(m4):
    v = _register(m4, ["cbc"])
    assert "dx_barcode_mismatch" in _err(m4["c"], "SELECT dx_collect_samples(%s, %s, %s::jsonb, 'centre', NULL, 'c', 'k-mismatch')",
                                        m4["clinic"], v["visit"]["id"],
                                        json.dumps([{"id": v["samples"][0]["id"], "barcode": "999999999999"}]))
    out = _collect(m4, v, [{"id": v["samples"][0]["id"], "barcode": v["samples"][0]["barcode"], "volume_ml": 3}])
    assert out["samples"][0]["status"] == "collected" and float(out["samples"][0]["volume_ml"]) == 3.0


def test_collect_before_payment(m4):
    c = m4["c"]
    _q(c, "UPDATE clinics SET dx_settings = '{\"collect_before_payment\": true}' WHERE id = %s", m4["clinic"])
    v = _register(m4, ["cbc"])
    assert _q(c, "SELECT dx_visit_due(%s, %s)", m4["clinic"], v["visit"]["id"]) == 35000
    assert "dx_awaiting_payment" in _err(c, "SELECT dx_collect_samples(%s, %s, %s::jsonb, 'centre', NULL, 'c', 'k-unpaid1')",
                                         m4["clinic"], v["visit"]["id"], json.dumps([{"id": v["samples"][0]["id"]}]))
    # doorstep money awaiting handover counts for the collection gate (the report stays held, P7)
    actor = str(uuid.uuid4())
    _q(c, "SELECT dx_record_field_collection(%s, %s, 35000, 'cash', NULL, %s, 'phleb', 'fc-key-0001')",
       m4["clinic"], v["visit"]["id"], actor)
    assert _q(c, "SELECT dx_visit_due(%s, %s)", m4["clinic"], v["visit"]["id"]) == 0
    assert _collect(m4, v)["visit"]["status"] == "in_process"


# ── bind (pre-printed / external labels) ─────────────────────────────────────

def _preprinted(s, visit_id):
    """A pending tube waiting for a sticker (as the shadow / sticker-roll mode creates)."""
    cur = s["c"].cursor()
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (s["clinic"],))
    cur.execute("""INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode_source,
                   collection_site_id, processing_site_id) VALUES (%s, %s, %s, 'serum', 'preprinted', %s, %s) RETURNING id""",
                (s["clinic"], visit_id, s["serum"], s["site"], s["site"]))
    sid = str(cur.fetchone()[0])
    cur.execute("COMMIT")
    return sid


def test_bind_then_collect_and_refusals(m4):
    v = _register(m4, ["cbc"])
    vid = v["visit"]["id"]
    sid = _preprinted(m4, vid)
    assert "dx_barcode_required" in _err(m4["c"], "SELECT dx_collect_samples(%s, %s, %s::jsonb, 'home', NULL, 'p', 'k-nobarcode')",
                                         m4["clinic"], vid, json.dumps([{"id": sid}]))
    taken = v["samples"][0]["barcode"]
    assert "dx_barcode_in_use" in _err(m4["c"], "SELECT dx_bind_barcode(%s, %s, %s, NULL, 'p')", m4["clinic"], sid, taken)
    assert "dx_barcode_invalid" in _err(m4["c"], "SELECT dx_bind_barcode(%s, %s, 'a b', NULL, 'p')", m4["clinic"], sid)
    assert "dx_barcode_already_set" in _err(m4["c"], "SELECT dx_bind_barcode(%s, %s, 'MD16583', NULL, 'p')",
                                            m4["clinic"], v["samples"][0]["id"])
    out = _q(m4["c"], "SELECT dx_bind_barcode(%s, %s, 'MD16583', NULL, 'p')", m4["clinic"], sid)
    assert out["barcode"] == "MD16583"
    assert _q(m4["c"], "SELECT dx_bind_barcode(%s, %s, 'MD16583', NULL, 'p')", m4["clinic"], sid)["barcode"] == "MD16583"
    # binding inside collect, by scan
    sid2 = _preprinted(m4, vid)
    out = _collect(m4, v, [{"id": sid2, "barcode": "MD16584"}], mode="home")
    assert next(x for x in out["samples"] if x["id"] == sid2)["barcode"] == "MD16584"


# ── reject ───────────────────────────────────────────────────────────────────

def test_reject_with_recollection(m4):
    v = _register(m4, ["cbc", "hba1c"])
    vid = v["visit"]["id"]
    _collect(m4, v)
    old = v["samples"][0]
    out = _reject(m4, old["id"], "hemolysed", notes="pink serum")
    assert out["replayed"] is False and out["reason_label"] == "Haemolysed sample"
    samples = {x["id"]: x for x in out["samples"]}
    assert samples[old["id"]]["status"] == "rejected" and samples[old["id"]]["rejection_reason_code"] == "HEMOLYSED"
    new = samples[out["replacement_sample_id"]]
    assert new["status"] == "pending_collection" and new["recollect_of"] == old["id"]
    # fresh sequence, same container code (AUDIT A7)
    assert new["barcode"][:4] == old["barcode"][:4] and new["barcode"][4:] != old["barcode"][4:]
    orders = _orders(m4, vid)
    dead = [o for o in orders if o["dx_sample_id"] == old["id"]]
    live = [o for o in orders if o["dx_sample_id"] == new["id"]]
    assert {o["status"] for o in dead} == {"cancelled"}
    assert {o["cancellation_reason"] for o in dead} == {"sample_rejected:HEMOLYSED"}
    assert sorted(o["replaces_order_id"] for o in live) == sorted(o["id"] for o in dead)
    assert {o["status"] for o in live} == {"ordered"}
    assert _reject(m4, old["id"])["replayed"] is True                                 # double click
    # the replacement is collected like any tube
    assert _collect(m4, out, [{"id": new["id"]}])["visit"]["status"] == "in_process"
    assert {o["status"] for o in _orders(m4, vid) if o["dx_sample_id"] == new["id"]} == {"sample_collected"}


def test_reject_without_recollection_and_refusals(m4):
    v = _register(m4, ["cbc"])
    sid = v["samples"][0]["id"]
    assert "dx_sample_not_rejectable" in _err(m4["c"], "SELECT dx_reject_sample(%s, %s, 'CLOTTED', NULL, true, NULL, 't')",
                                              m4["clinic"], sid)
    _collect(m4, v)
    assert "dx_rejection_reason_unknown" in _err(m4["c"], "SELECT dx_reject_sample(%s, %s, 'NOPE', NULL, true, NULL, 't')",
                                                 m4["clinic"], sid)
    assert "dx_rejection_notes_required" in _err(m4["c"], "SELECT dx_reject_sample(%s, %s, 'OTHER', '', true, NULL, 't')",
                                                 m4["clinic"], sid)
    out = _reject(m4, sid, "CLOTTED", recollect=False)
    assert out["replacement_sample_id"] is None and len(out["samples"]) == 1


def test_reject_refused_once_a_result_is_signed(m4):
    v = _register(m4, ["cbc"])
    _collect(m4, v)
    order = _orders(m4, v["visit"]["id"])[0]
    _q(m4["c"], "UPDATE opd_orders SET status = 'resulted', resulted_at = now() WHERE id = %s", order["id"])
    assert "dx_sample_results_signed" in _err(m4["c"], "SELECT dx_reject_sample(%s, %s, 'CLOTTED', NULL, true, NULL, 't')",
                                              m4["clinic"], v["samples"][0]["id"])


# ── receive, store ───────────────────────────────────────────────────────────

def test_receive_same_site_then_store(m4):
    c = m4["c"]
    v = _register(m4, ["cbc"])
    code = v["samples"][0]["barcode"]
    assert "dx_sample_not_collected" in _err(c, "SELECT dx_receive_sample(%s, %s, %s, NULL, 'rcv')", m4["clinic"], code,
                                             m4["site"])
    _collect(m4, v)
    other_branch = str(_q(c, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'North') RETURNING id", m4["clinic"]))
    other = str(_q(c, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) VALUES (%s, %s, 'NOR', 'North') "
                      "RETURNING id", m4["clinic"], other_branch))
    assert "dx_sample_wrong_site" in _err(c, "SELECT dx_receive_sample(%s, %s, %s, NULL, 'rcv')", m4["clinic"], code, other)
    out = _q(c, "SELECT dx_receive_sample(%s, %s, %s, NULL, 'rcv')", m4["clinic"], code, m4["site"])
    assert out["status"] == "received" and out["received_site_id"] == m4["site"] and out["replayed"] is False
    assert _q(c, "SELECT dx_receive_sample(%s, %s, %s, NULL, 'rcv')", m4["clinic"], code, m4["site"])["replayed"] is True
    assert "dx_storage_location_invalid" in _err(c, "SELECT dx_mark_sample_stored(%s, %s, ' ', NULL, 'r')",
                                                 m4["clinic"], out["id"])
    st = _q(c, "SELECT dx_mark_sample_stored(%s, %s, 'Fridge 2 / Rack B', NULL, 'r')", m4["clinic"], out["id"])
    assert st["status"] == "stored" and st["storage_location"] == "Fridge 2 / Rack B"
    kinds = _q(c, "SELECT jsonb_agg(kind ORDER BY id) FROM dx_events WHERE accession_id = %s", v["visit"]["id"])
    assert kinds == ["registered", "collected", "received", "stored"]


# ── guards ───────────────────────────────────────────────────────────────────

def test_sample_guard_and_barcode_immutability(m4):
    c = m4["c"]
    v = _register(m4, ["cbc"])
    sid = v["samples"][0]["id"]
    assert "dx_managed:dx_samples" in _err(c, "UPDATE dx_samples SET status = 'collected' WHERE id = %s", sid)
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (m4["clinic"],))
        with pytest.raises(psycopg2.Error) as e:
            cur.execute("UPDATE dx_samples SET barcode = '111111111111' WHERE id = %s", (sid,))
        assert "dx_barcode_immutable" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    assert "dx_managed:dx_field_collections" in _err(c, """INSERT INTO dx_field_collections (clinic_id, accession_id,
        amount_paise, mode, collected_by, collected_by_name, collected_at, idempotency_key)
        VALUES (%s, %s, 100, 'cash', gen_random_uuid(), 'x', now(), 'direct-write')""", m4["clinic"], v["visit"]["id"])
    _q(c, """INSERT INTO dx_label_prints (clinic_id, accession_id, sample_ids, mode, printed_by_name)
             VALUES (%s, %s, ARRAY[%s]::uuid[], 'per_sample', 'col')""", m4["clinic"], v["visit"]["id"], sid)
    assert "dx_append_only:dx_label_prints" in _err(c, "UPDATE dx_label_prints SET copies = 2 WHERE accession_id = %s",
                                                    v["visit"]["id"])


# ── doorstep money (AUDIT A14) ───────────────────────────────────────────────

def _shift_receipt(s, invoice_id, amount, key):
    c, cashier = s["c"], str(uuid.uuid4())
    shift = _q(c, "INSERT INTO opd_cashier_shifts (clinic_id, cashier_admin_id, cashier_name) VALUES (%s, %s, 'cash') "
                  "RETURNING id", s["clinic"], cashier)
    return str(_q(c, """INSERT INTO opd_receipts (clinic_id, invoice_id, shift_id, mode, amount_paise, idempotency_key,
                        received_by_admin_id, received_by_name) VALUES (%s, %s, %s, 'cash', %s, %s, %s, 'cash')
                        RETURNING id""", s["clinic"], invoice_id, shift, amount, key, cashier))


def test_field_collection_and_handover(m4):
    c = m4["c"]
    v = _register(m4, ["cbc"])
    vid = v["visit"]["id"]
    phleb = str(uuid.uuid4())
    assert "dx_field_amount_over_due" in _err(c, "SELECT dx_record_field_collection(%s, %s, 99999, 'cash', NULL, %s, 'p', "
                                                 "'fc-over-0001')", m4["clinic"], vid, phleb)
    assert "dx_field_collections_upi_ref" in _err(c, "SELECT dx_record_field_collection(%s, %s, 100, 'upi_to_clinic', NULL, "
                                                     "%s, 'p', 'fc-upi-00001')", m4["clinic"], vid, phleb)
    fc = _q(c, "SELECT dx_record_field_collection(%s, %s, 20000, 'cash', NULL, %s, 'p', 'fc-key-00001')",
            m4["clinic"], vid, phleb)
    assert fc["status"] == "pending_handover" and fc["replayed"] is False
    assert _q(c, "SELECT dx_record_field_collection(%s, %s, 20000, 'cash', NULL, %s, 'p', 'fc-key-00001')",
              m4["clinic"], vid, phleb)["id"] == fc["id"]
    assert _q(c, "SELECT dx_visit_due(%s, %s)", m4["clinic"], vid) == 15000
    # cashier: bill issued, receipt on the shift with key fc:<id>, then the handover stamp
    inv = _q(c, "SELECT dx_bill_visit(%s, %s, NULL, 'cash')", m4["clinic"], vid)["invoice_id"]
    _q(c, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", inv)
    wrong = _shift_receipt(m4, inv, 5000, "not-the-fc-key")   # wrong key (and amount)
    assert "dx_handover_receipt_invalid" in _err(c, "SELECT dx_handover_field_collection(%s, %s, %s, NULL, 'cash')",
                                                 m4["clinic"], fc["id"], wrong)
    rid = _shift_receipt(m4, inv, 20000, f"fc:{fc['id']}")
    out = _q(c, "SELECT dx_handover_field_collection(%s, %s, %s, NULL, 'cash')", m4["clinic"], fc["id"], rid)
    assert out["status"] == "handed_over" and out["receipt_id"] == rid
    assert _q(c, "SELECT dx_handover_field_collection(%s, %s, %s, NULL, 'cash')", m4["clinic"], fc["id"], rid)["replayed"]
    assert "dx_field_collection_handed_over" in _err(c, "SELECT dx_handover_field_collection(%s, %s, %s, NULL, 'c')",
                                                     m4["clinic"], fc["id"], wrong)
    # the doorstep money is now a receipt on the bill (35000 - 5000 stray receipt - 20000 handed over)
    assert _q(c, "SELECT dx_visit_due(%s, %s)", m4["clinic"], vid) == 10000


# ── purge, rollback ──────────────────────────────────────────────────────────

def test_purge_v4_removes_collection_rows(m4):
    c = m4["c"]
    v = _register(m4, ["cbc"])
    _collect(m4, v)
    _reject(m4, v["samples"][0]["id"])
    _q(c, "SELECT dx_record_field_collection(%s, %s, 100, 'cash', NULL, gen_random_uuid(), 'p', 'fc-purge-001')",
       m4["clinic"], v["visit"]["id"])
    out = _q(c, "SELECT dx_purge_clinic(%s)", m4["clinic"])
    assert out["visits"] == 1 and out["field_collections"] == 1 and out["orders"] == 2
    for t in ("dx_samples", "dx_field_collections", "dx_label_prints", "dx_rejection_reasons"):
        assert _q(c, f"SELECT count(*) FROM {t} WHERE clinic_id = %s", m4["clinic"]) == 0


def test_rollback_refuses_collected_tubes_then_reapplies(m4):
    v = _register(m4, ["cbc"])
    _collect(m4, v)
    cur = m4["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "128_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_128(cur)
        cur.execute("SELECT to_regclass('public.dx_field_collections'), to_regprocedure('dx_collect_samples(uuid,uuid,"
                    "jsonb,text,uuid,text,text)')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT to_regclass('public.dx_samples') IS NOT NULL")
        assert cur.fetchone() == (True,)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'dx\\_%%'
                         AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x WHERE x LIKE 'search_path=%%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
