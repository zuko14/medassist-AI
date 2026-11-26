"""Revenue Guard rules (RI-03 / RI-04 / RI-05) on real PostgreSQL through ri_scan.

One fixture per rule: fires, does not fire, exemption, auto-resolve with the recovered amount (docs/revenue-integrity/
SPEC.md §3, §5). Then the Python job layer: integrity alert, digest text, manual-run cap, the nightly sweep skipping
clinics without the opt-in.
"""

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.test_migration_115_orders import CBC, _place
from tests.test_migration_122_ot import _admit

D = date(2026, 11, 2)                      # the service day scanned
NIGHT = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)       # 01:30 IST on D+1
OPD = {"min_opd_invoices_30d": 0}


def _q(c, sql, *args):
    cur = c.cursor()
    cur.execute(sql, args)
    return cur.fetchone()[0] if cur.description else None


def scan(c, clinic, settings=None, modules=("opd",), day=D, now=NIGHT):
    return _q(c, "SELECT ri_scan(%s, %s, %s, %s::jsonb, %s)", clinic, day, now, json.dumps(OPD if settings is None
                                                                                           else settings), list(modules))


def findings(c, clinic, status="open"):
    cur = c.cursor()
    cur.execute("SELECT rule_code, amount_paise, resolution, recovered_paise, source_id::text FROM ri_findings "
                "WHERE clinic_id = %s AND status = %s ORDER BY rule_code, source_id", (clinic, status))
    return cur.fetchall()


@pytest.fixture
def opd(real_pg_conn):
    c = real_pg_conn
    clinic = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, features, opd_state)
                          VALUES ('RI Clinic', %s, 'polyclinic', true, '{"opd_enabled": true}', 'READY') RETURNING id""",
                    "+9179" + uuid.uuid4().hex[:8]))
    doctor = str(_q(c, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active, consultation_fee)
                          VALUES (%s, 'Dr. Rao', 'Medicine', 'Physician', true, 500) RETURNING id""", clinic))
    w = {"c": c, "clinic": clinic, "doctor": doctor}
    w["patient"] = _pat(w)
    w["cbc"] = str(_q(c, "INSERT INTO lab_tests (clinic_id, name, sample_type, price_paise) VALUES (%s, 'CBC', 'EDTA', 35000) "
                         "RETURNING id", clinic))
    return w


def _pat(w, name="Ravi Kumar"):
    return str(_q(w["c"], "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, %s, %s) RETURNING id",
                  w["clinic"], name, "+9178" + uuid.uuid4().hex[:8]))


def _visit(w, day=D, checked_in=True, payment_id=None, patient=None):
    t = f"{9 + uuid.uuid4().int % 9:02d}:{uuid.uuid4().int % 60:02d}:00"
    return str(_q(w["c"], """INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id,
                               doctor_name, department, appointment_date, appointment_time, status, booking_type,
                               checked_in_at, payment_id, amount_paise)
                             VALUES (%s, %s, '+919000000001', 'Ravi Kumar', %s, 'Dr. Rao', 'Medicine', %s, %s,
                                     'confirmed', 'consultation', %s, %s, %s) RETURNING id""",
                  w["clinic"], patient or w["patient"], w["doctor"], day, t,
                  datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=5) if checked_in else None,
                  payment_id, 50000 if payment_id else 0))


_SEQ = iter(range(1, 10 ** 6))


def _invoice(w, appt, status="issued", total=50000, patient=None, discount_reason=None):
    """A bill the way the counter makes one: a draft with a line (subtotal is derived), then numbered at issue."""
    inv = str(_q(w["c"], """INSERT INTO opd_invoices (clinic_id, appointment_id, patient_id, status, discount_reason)
                            VALUES (%s, %s, %s, 'draft', %s) RETURNING id""",
                 w["clinic"], appt, patient or w["patient"], discount_reason))
    _q(w["c"], "INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, unit_price_paise) "
               "VALUES (%s, %s, 1, 'consultation', 'Consultation', %s)", w["clinic"], inv, total)
    if status != "draft":
        _q(w["c"], "UPDATE opd_invoices SET status = 'issued', invoice_year = 2026, invoice_seq = %s, issued_at = %s "
                   "WHERE id = %s", next(_SEQ), NIGHT - timedelta(days=1), inv)
    return inv


# ── OPD ─────────────────────────────────────────────────────────────────────

def test_seen_not_billed_fires_then_resolves_billed_with_recovered(opd):
    c, clinic = opd["c"], opd["clinic"]
    appt = _visit(opd)
    _visit(opd, checked_in=False)                                      # booked, never seen: nothing to bill
    _visit(opd, day=D - timedelta(days=1))                             # another day: not this scan
    out = scan(c, clinic)
    assert out["rules"] == {"opd_seen_not_billed": 1} and out["new"] == 1 and out["opd_gate"] is True
    assert findings(c, clinic) == [("opd_seen_not_billed", 50000, None, None, appt)]
    assert scan(c, clinic)["new"] == 0                                 # a re-run never duplicates
    _invoice(opd, appt, "issued", 60000)
    out = scan(c, clinic, day=D + timedelta(days=1), now=NIGHT + timedelta(days=1))
    assert out["resolved"] == 1
    assert findings(c, clinic, "resolved") == [("opd_seen_not_billed", 50000, "billed", 60000, appt)]


def test_a_draft_bill_is_not_a_missing_bill_and_zero_fee_is_skipped(opd):
    c, clinic = opd["c"], opd["clinic"]
    _invoice(opd, _visit(opd), "draft")
    free_doc = str(_q(c, "INSERT INTO doctors (clinic_id, name, department, specialization, is_active, consultation_fee) "
                         "VALUES (%s, 'Dr. Free', 'Medicine', 'GP', true, 0) RETURNING id", clinic))
    a = _visit(opd)
    _q(c, "UPDATE appointments SET doctor_id = %s WHERE id = %s", free_doc, a)
    assert scan(c, clinic)["rules"] == {}


def test_prepaid_goes_to_review_with_zero_leakage(opd):
    c, clinic = opd["c"], opd["clinic"]
    appt = _visit(opd, payment_id="pay_" + uuid.uuid4().hex[:10])
    assert scan(c, clinic)["rules"] == {"prepaid_not_invoiced": 1}       # never opd_seen_not_billed (VERIFICATION D12)
    assert _q(c, "SELECT kind || ':' || amount_paise || ':' || (detail->>'prepaid_paise') FROM ri_findings "
                 "WHERE clinic_id = %s", clinic) == "review:0:50000"
    _invoice(opd, appt, "issued")
    scan(c, clinic, day=D + timedelta(days=1), now=NIGHT + timedelta(days=1))
    assert findings(c, clinic, "resolved") == [("prepaid_not_invoiced", 0, "billed", 0, appt)]   # nothing new recovered


def test_billing_adoption_gate(opd):
    c, clinic = opd["c"], opd["clinic"]
    _visit(opd)
    out = scan(c, clinic, settings={})                                  # default: 20 OPD bills in 30 days
    assert out["opd_gate"] is False and out["rules"] == {} and out["opd_invoices_30d"] == 0
    for _ in range(3):
        _invoice(opd, None, "issued")
    assert scan(c, clinic, settings={"min_opd_invoices_30d": 3})["rules"] == {"opd_seen_not_billed": 1}
    assert scan(c, clinic, settings={"min_opd_invoices_30d": 4})["opd_gate"] is False
    # gated off: an open OPD finding is left alone, not auto-closed
    assert len(findings(c, clinic)) == 1


def test_free_review_exemption_is_recorded_once(opd):
    c, clinic = opd["c"], opd["clinic"]
    first = _visit(opd, day=D - timedelta(days=5))
    _invoice(opd, first, "issued")
    review = _visit(opd)
    out = scan(c, clinic, settings={**OPD, "review_free_days": 7})
    assert out["exempt"] == 1 and out["rules"] == {} and findings(c, clinic) == []   # rules = open candidates
    assert _q(c, "SELECT resolution || ':' || (detail->>'exemption') FROM ri_findings WHERE source_id = %s",
              review) == "exempt:free_review_within_7_days"
    assert scan(c, clinic, settings={**OPD, "review_free_days": 7})["exempt"] == 0     # never repeated
    assert scan(c, clinic, settings={**OPD, "review_free_days": 2})["new"] == 1        # outside the window


def test_order_done_and_stale_orders(opd):
    c, clinic = opd["c"], opd["clinic"]
    appt = _visit(opd)
    enc = str(_q(c, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id) VALUES (%s, %s, %s, %s) "
                    "RETURNING id", clinic, appt, opd["patient"], opd["doctor"]))
    w = {**opd, "enc": enc}
    done = _place(c, w, [CBC(w)])["orders"][0]["id"]
    waiting = _place(c, w, [CBC(w)])["orders"][0]["id"]
    _q(c, "UPDATE opd_orders SET status = 'sample_collected', sample_collected_at = now(), specimen_barcode = %s "
          "WHERE id = %s", "BC" + uuid.uuid4().hex[:8], done)
    _invoice(opd, appt, "issued")                                       # the consultation is billed
    out = scan(c, clinic, now=datetime.now(timezone.utc))
    assert out["rules"] == {"order_done_not_billed": 1}                 # the waiting order is < 48 h old
    out = scan(c, clinic, now=datetime.now(timezone.utc) + timedelta(days=3))
    assert out["rules"] == {"order_done_not_billed": 1, "order_stale_unbilled": 1}
    _q(c, "SELECT opd_cancel_order(%s, %s, 'Patient declined', NULL)", clinic, waiting)
    # the counter bills the collected test on a supplementary bill (a live line pointing at the order)
    sup = str(_q(c, "INSERT INTO opd_invoices (clinic_id, encounter_id, patient_id, status) VALUES (%s, %s, %s, 'draft') "
                    "RETURNING id", clinic, enc, opd["patient"]))
    _q(c, "INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, unit_price_paise, order_id) "
          "VALUES (%s, %s, 1, 'diagnostic', 'CBC', 35000, %s)", clinic, sup, done)
    scan(c, clinic, now=datetime.now(timezone.utc) + timedelta(days=3))
    got = {r[0]: r[2:4] for r in findings(c, clinic, "resolved")}
    assert got == {"order_done_not_billed": ("billed", 35000), "order_stale_unbilled": ("voided", None)}


def test_stale_draft_bill_and_discount_exemption(opd):
    c, clinic = opd["c"], opd["clinic"]
    a, b = _visit(opd, checked_in=False), _visit(opd, checked_in=False)
    stale = _invoice(opd, a, "draft", 30000)
    _invoice(opd, b, "draft", 20000, discount_reason="Staff family")
    assert scan(c, clinic)["rules"] == {}                               # under 24 h
    later = datetime.now(timezone.utc) + timedelta(days=2)
    out = scan(c, clinic, now=later)
    assert out["rules"] == {"draft_bill_stale": 1} and out["exempt"] == 1
    assert findings(c, clinic) == [("draft_bill_stale", 30000, None, None, stale)]
    _q(c, "UPDATE opd_invoices SET status = 'issued', invoice_year = 2026, invoice_seq = %s, issued_at = now() "
          "WHERE id = %s", next(_SEQ), stale)
    scan(c, clinic, now=later)
    assert ("draft_bill_stale", 30000, "billed", 30000) in [r[:4] for r in findings(c, clinic, "resolved")]


def test_dismissed_source_never_reopens(opd):
    c, clinic = opd["c"], opd["clinic"]
    _visit(opd)
    scan(c, clinic)
    _q(c, "UPDATE ri_findings SET status = 'dismissed', resolution = 'dismissed', resolved_on = CURRENT_DATE, "
          "dismissed_by = %s, dismiss_reason = 'Doctor waived the fee' WHERE clinic_id = %s", str(uuid.uuid4()), clinic)
    assert scan(c, clinic)["new"] == 0 and findings(c, clinic) == []


def test_tenant_isolation_of_the_scan(opd, real_pg_conn):
    c = real_pg_conn
    _visit(opd)
    other = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('Other', %s, 'polyclinic', true) "
                      "RETURNING id", "+9177" + uuid.uuid4().hex[:8]))
    assert scan(c, other)["rules"] == {}
    assert _q(c, "SELECT count(*) FROM ri_findings WHERE clinic_id = %s", other) == 0


# ── IPD / OT / DX ────────────────────────────────────────────────────────────

@pytest.fixture
def ipd(real_pg_conn):
    c = real_pg_conn
    clinic = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, ipd_settings)
                          VALUES ('RI Hospital', %s, 'polyclinic', true, '{"bed_tariffs": {"general": 100000}}')
                          RETURNING id""", "+9176" + uuid.uuid4().hex[:8]))
    doctor = str(_q(c, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active, registration_number)
                          VALUES (%s, 'Dr. Iyer', 'Surgery', 'Surgeon', true, 'KMC/1/1') RETURNING id""", clinic))
    ward = str(_q(c, "INSERT INTO ipd_wards (clinic_id, code, name) VALUES (%s, 'W1', 'Ward') RETURNING id", clinic))
    w = {"c": c, "clinic": clinic, "doctor": doctor, "ward": ward}
    w["adm"] = _admit(c, w)
    return w


def _charge(w, status="proposed", price=120000):
    return str(_q(w["c"], """INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date,
                               unit_price_paise, status, posted_at)
                             VALUES (%s, %s, 'procedure', 'Dressing', %s, %s, %s, %s) RETURNING id""",
                  w["clinic"], w["adm"], D, price, status, datetime.now(timezone.utc) if status == "posted" else None))


def test_ipd_charge_unposted_then_posted(ipd):
    c, clinic = ipd["c"], ipd["clinic"]
    ch = _charge(ipd)
    assert scan(c, clinic, modules=("ipd",))["rules"] == {}             # < 24 h old
    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert scan(c, clinic, modules=("ipd",), now=later)["rules"] == {"ipd_charge_unposted": 1}
    assert scan(c, clinic, modules=("opd",), now=later)["resolved"] == 0  # IPD off: the IPD finding is left alone
    _q(c, "UPDATE ipd_charges SET status = 'posted', posted_at = now() WHERE id = %s", ch)
    scan(c, clinic, modules=("ipd",), now=later)
    assert findings(c, clinic, "resolved") == [("ipd_charge_unposted", 120000, "billed", 120000, ch)]


def test_discharge_pending_with_open_charges(ipd):
    c, clinic = ipd["c"], ipd["clinic"]
    _charge(ipd, "posted", 80000)
    _replica(c, ("UPDATE ipd_admissions SET status = 'discharge_pending', discharge_type = 'regular' WHERE id = %s",
                 ipd["adm"]))
    later = datetime.now(timezone.utc) + timedelta(days=2)
    out = scan(c, clinic, modules=("ipd",), now=later)
    assert out["rules"] == {"ipd_discharge_pending_open": 1}
    assert _q(c, "SELECT amount_paise FROM ri_findings WHERE clinic_id = %s AND rule_code = 'ipd_discharge_pending_open'",
              clinic) >= 80000


def test_discharged_with_posted_charge_off_the_bill(ipd):
    c, clinic = ipd["c"], ipd["clinic"]
    ch = _charge(ipd, "posted", 70000)
    _charge(ipd, "proposed", 5000)                                      # proposed is the unposted rule, not this one
    _replica(c, ("UPDATE ipd_admissions SET status = 'discharged', discharged_at = now(), discharge_type = 'regular', "
                 "current_bed_id = NULL WHERE id = %s", ipd["adm"]))
    assert scan(c, clinic, modules=("ipd",))["rules"] == {"ipd_discharged_unbilled": 1}
    inv = str(_q(c, "INSERT INTO opd_invoices (clinic_id, admission_id, patient_id, status) "
                    "SELECT clinic_id, id, patient_id, 'draft' FROM ipd_admissions WHERE id = %s RETURNING id", ipd["adm"]))
    _replica(c, ("UPDATE ipd_charges SET invoice_id = %s WHERE id = %s", inv, ch))
    scan(c, clinic, modules=("ipd",))
    assert findings(c, clinic, "resolved") == [("ipd_discharged_unbilled", 70000, "billed", 70000, ch)]


def test_ward_dispense_without_its_charge(ipd):
    c, clinic = ipd["c"], ipd["clinic"]
    disp, line = str(uuid.uuid4()), str(uuid.uuid4())
    cols = _q(c, "SELECT string_agg(column_name || ':' || is_nullable, ',') FROM information_schema.columns "
                 "WHERE table_name = 'rx_dispenses' AND is_nullable = 'NO' AND column_default IS NULL")
    values = {"id": disp, "clinic_id": clinic, "admission_id": ipd["adm"]}
    names = ["id"] + [x.split(":")[0] for x in cols.split(",")]
    _replica(c, (f"INSERT INTO rx_dispenses ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))})",
                 *[values.get(n, _fill(c, "rx_dispenses", n)) for n in names]))
    lcols = [x.split(":")[0] for x in _q(c, "SELECT string_agg(column_name || ':' || is_nullable, ',') FROM "
                                            "information_schema.columns WHERE table_name = 'rx_dispense_lines' "
                                            "AND is_nullable = 'NO' AND column_default IS NULL").split(",")]
    lv = {"id": line, "clinic_id": clinic, "dispense_id": disp, "qty_units": 4, "unit_price_paise": 1250}
    _replica(c, (f"INSERT INTO rx_dispense_lines ({', '.join(lcols)}) VALUES ({', '.join(['%s'] * len(lcols))})",
                 *[lv.get(n, _fill(c, "rx_dispense_lines", n)) for n in lcols]))
    assert scan(c, clinic, modules=("ipd", "rx"))["rules"] == {"rx_dispense_no_charge": 1}
    assert scan(c, clinic, modules=("rx",))["rules"] == {}              # needs IPD too
    assert _q(c, "SELECT kind || ':' || amount_paise FROM ri_findings WHERE clinic_id = %s", clinic) == "integrity:5000"


def _fill(c, table, col):
    """A placeholder for a NOT NULL column of a fixture row inserted in replica mode."""
    t = _q(c, "SELECT data_type FROM information_schema.columns WHERE table_name = %s AND column_name = %s", table, col)
    return {"uuid": str(uuid.uuid4()), "text": "x" + uuid.uuid4().hex[:6], "integer": 1, "smallint": 1, "bigint": 1,
            "boolean": False, "date": D, "jsonb": "{}", "numeric": 1}.get(t, datetime.now(timezone.utc))


def test_integrity_findings_raise_an_admin_alert(monkeypatch):
    from app.services import revenue_guard as ri
    notes = []

    class _Res:
        data = {"rules": {"ot_done_no_charge": 2, "opd_seen_not_billed": 3}}

    async def _sb(q):
        return _Res()

    async def _notify(cid, title, msg):
        notes.append((cid, title, msg))
    monkeypatch.setattr(ri, "sb", _sb)
    monkeypatch.setattr(ri, "_notify", _notify)
    monkeypatch.setattr(ri.supabase, "rpc", lambda *a, **k: None)
    asyncio.run(ri.scan_clinic({"id": "c1", "features": {"opd_enabled": True}, "plan": "polyclinic"}))
    assert notes == [("c1", "[Revenue Guard] Billing integrity issue", "Surgery done, no OT charge: 2. Open Revenue "
                      "Guard to see each item; nothing has been changed automatically.")]


def test_ot_done_without_charge_is_integrity(ot):
    from tests.test_migration_122_ot import _book
    c, clinic = ot["c"], ot["clinic"]
    sid = _book(ot)["id"]
    _replica(c, ("UPDATE ot_surgeries SET status = 'completed', actual_start = now() - interval '2 hours', "
                 "actual_end = now(), procedure_notes = 'Done', completed_by_name = 'Dr. Iyer', service_id = %s "
                 "WHERE id = %s", ot["service"], sid))
    out = scan(c, clinic, modules=("ipd", "ot"))
    assert out["rules"] == {"ot_done_no_charge": 1}
    assert _q(c, "SELECT kind || ':' || amount_paise FROM ri_findings WHERE clinic_id = %s", clinic) == "integrity:4500000"
    assert scan(c, clinic, modules=("ipd",))["rules"] == {}             # OT rule needs OT


def _replica(c, *stmts):
    """Fixture rows for modules whose own guards / FKs need a whole workflow: replica mode skips FK and guard
    triggers (CHECK constraints still apply). ri_scan only reads these rows."""
    _q(c, "SET session_replication_role = replica")
    try:
        return [_q(c, sql, *args) for sql, *args in stmts]
    finally:
        _q(c, "SET session_replication_role = origin")


def test_dx_b2b_charges_left_off_a_statement(opd):
    c, clinic = opd["c"], opd["clinic"]
    acct = str(uuid.uuid4())
    old, recent = _replica(c, *[(
        "INSERT INTO dx_b2b_ledger (clinic_id, account_id, entry_type, amount_paise, accession_id, accession_item_id, "
        "created_by_name, created_at) VALUES (%s, %s, 'charge', %s, %s, %s, 'desk', %s) RETURNING id",
        clinic, acct, amt, str(uuid.uuid4()), str(uuid.uuid4()), at) for amt, at in (
            (90000, datetime(2026, 9, 10, tzinfo=timezone.utc)), (40000, datetime.now(timezone.utc)))])
    out = scan(c, clinic, modules=("dx",), now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert out["rules"] == {"dx_b2b_unstatemented": 1}
    assert findings(c, clinic)[0][:2] == ("dx_b2b_unstatemented", 90000)
    _replica(c, ("UPDATE dx_b2b_ledger SET statement_id = %s WHERE id = %s", str(uuid.uuid4()), str(old)))
    scan(c, clinic, modules=("dx",), now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert findings(c, clinic, "resolved") == [("dx_b2b_unstatemented", 90000, "billed", 90000, str(old))]
    assert recent


def test_rx_signed_not_sold_is_opportunity_only_on_its_day(opd):
    c, clinic = opd["c"], opd["clinic"]
    signed = datetime.combine(D, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=6)
    (rx,) = _replica(c, ("INSERT INTO opd_prescriptions (clinic_id, encounter_id, appointment_id, patient_id, doctor_id, "
                         "status, signed_at, signed_by_admin_id, signer_snapshot, letterhead_snapshot, patient_snapshot) "
                         "VALUES (%s, %s, %s, %s, %s, 'signed', %s, %s, '{\"registration_number\": \"R1\"}', '{}', '{}') "
                         "RETURNING id",
                         clinic, str(uuid.uuid4()), str(uuid.uuid4()), opd["patient"], opd["doctor"], signed,
                         str(uuid.uuid4())))
    out = scan(c, clinic, modules=("rx",))
    assert out["rules"] == {"rx_signed_not_sold": 1}
    assert _q(c, "SELECT kind || ':' || amount_paise FROM ri_findings WHERE clinic_id = %s", clinic) == "opportunity:0"
    # the next nights scan later days: it stays on the list for a week, then closes (never counted as leakage)
    scan(c, clinic, modules=("rx",), day=D + timedelta(days=1), now=NIGHT + timedelta(days=1))
    assert len(findings(c, clinic)) == 1
    scan(c, clinic, modules=("rx",), day=D + timedelta(days=9), now=NIGHT + timedelta(days=9))
    assert findings(c, clinic, "resolved") == [("rx_signed_not_sold", 0, "auto_closed", None, str(rx))]


# ── Python job layer ────────────────────────────────────────────────────────

def test_job_layer_alert_digest_cap_and_sweep(api, monkeypatch):
    from app.services import revenue_guard as ri
    from app.services.tenant import invalidate_tenant_cache
    c = api.conn
    clinic = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, features, opd_state, ri_settings)
                          VALUES ('RI Live', %s, 'polyclinic', true, '{"opd_enabled": true, "revenue_guard": true}',
                                  'READY', '{"min_opd_invoices_30d": 0}') RETURNING id""", "+9175" + uuid.uuid4().hex[:8]))
    off = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, features, opd_state)
                       VALUES ('RI Off', %s, 'polyclinic', true, '{"opd_enabled": true}', 'READY') RETURNING id""",
                 "+9174" + uuid.uuid4().hex[:8]))
    invalidate_tenant_cache()
    doctor = str(_q(c, "INSERT INTO doctors (clinic_id, name, department, specialization, is_active, consultation_fee) "
                       "VALUES (%s, 'Dr. A', 'Medicine', 'GP', true, 400) RETURNING id", clinic))
    w = {"c": c, "clinic": clinic, "doctor": doctor}
    w["patient"] = _pat(w)
    from app.utils.helpers import today_ist
    yday = today_ist() - timedelta(days=1)
    _visit(w, day=yday)
    scanned = []
    real = ri.scan_clinic
    monkeypatch.setattr(ri, "scan_clinic", lambda cl, day=None, now=None: (scanned.append(cl["id"]), real(cl, day, now))[1])
    stats = asyncio.run(ri.run_nightly())
    assert clinic in scanned and off not in scanned and stats["failed"] == 0
    text = asyncio.run(ri.digest_text(clinic))
    assert text.startswith("Revenue Guard — yesterday: ₹400 done but not billed (1 item)")
    assert asyncio.run(ri.digest_text(off)) is None
    sent = []

    class _WA:
        async def send_text(self, cl, phone, msg, _source=""):
            sent.append((cl["id"], phone, _source))
            return True
    import app.services.whatsapp as wa
    monkeypatch.setattr(wa, "whatsapp_service", _WA())
    _q(c, "UPDATE clinics SET config = '{\"admin_phone\": \"+919811112222\"}'::jsonb WHERE id = %s", clinic)
    invalidate_tenant_cache()
    out = asyncio.run(ri.run_digest())
    assert out["clinics"] >= 1 and (clinic, "+919811112222", "revenue_guard") in sent
    assert _q(c, "SELECT count(*) FROM admin_notifications WHERE clinic_id = %s AND title LIKE 'Revenue Guard%%'", clinic) == 1
    # manual re-run: three a day, then 429
    from fastapi import HTTPException
    cl = {"id": clinic, "features": {"opd_enabled": True, "revenue_guard": True}, "plan": "polyclinic",
          "account_type": "tenant", "ri_settings": {"min_opd_invoices_30d": 0}}
    for left in (2, 1, 0):
        assert asyncio.run(ri.manual_run(cl))["manual_runs_left"] == left
    with pytest.raises(HTTPException) as e:
        asyncio.run(ri.manual_run(cl))
    assert e.value.status_code == 429


from tests.test_ipd_api_e2e import api  # noqa: E402,F401  (fixture)
from tests.test_migration_122_ot import ot  # noqa: E402,F401  (fixture)


def test_settings_bounds():
    from fastapi import HTTPException
    from app.services.revenue_guard import validate_settings
    assert validate_settings({"review_free_days": 30, "exempt_reasons": ["Staff", "staff", "VIP"]}) == {
        "review_free_days": 30, "exempt_reasons": ["staff", "vip"]}
    for bad in ({"review_free_days": 31}, {"min_opd_invoices_30d": -1}, {"min_opd_invoices_30d": True},
                {"exempt_reasons": ["ab"]}, {"made_up": 1}):
        with pytest.raises(HTTPException):
            validate_settings(bad)


def test_rupee_grouping():
    from app.services.revenue_guard import _rupees
    assert (_rupees(1840000), _rupees(12345678900), _rupees(50)) == ("₹18,400", "₹12,34,56,789", "₹0")
