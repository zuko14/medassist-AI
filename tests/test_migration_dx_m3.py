"""Migration 127 (Diagnostic OS visits, items, samples, billing, DX-M3) against real PostgreSQL.

Registration (pricing in SQL, panel/package explosion, tubes grouped by
container sharing the visit sequence, YYccSSSSSSSS barcodes, the go-live floor),
idempotency and concurrency (10 retries -> 1 visit; 20 desks -> gapless numbers),
approval / gender / discount / B2B refusals, bookings, the DX bill and its line
guard (AUDIT A10), supplementary bills, cancel item / visit, direct-write refusal,
the lab-test delete guard (A22), redaction, purge v3 and a guarded rollback.
"""

import json
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "127_dx_visits_and_billing.sql", MIG / "rollback" / "127_rollback_dx_visits_and_billing.sql"


def unwind_127(cur) -> None:
    """Inside an open transaction: purge every clinic with DX visit rows, run the 127
    rollback. Called first by unwind_126 (newest first)."""
    cur.execute("SELECT to_regclass('public.dx_accessions') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT dx_purge_clinic(x.clinic_id) FROM (
                     SELECT clinic_id FROM dx_accessions UNION SELECT clinic_id FROM dx_referrers
                     UNION SELECT clinic_id FROM dx_b2b_accounts UNION SELECT clinic_id FROM opd_orders WHERE source = 'dx'
                     UNION SELECT clinic_id FROM opd_invoices WHERE accession_id IS NOT NULL) x""")
    # a DX bill outlives the purge of its visit (OPD ledger); only the OPD purge removes it
    cur.execute("SELECT opd_purge_clinic(x.clinic_id) FROM (SELECT DISTINCT clinic_id FROM opd_invoices "
                "WHERE accession_id IS NOT NULL) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _q(conn, sql, *args):
    cur = conn.cursor()
    cur.execute(sql, args)
    out = cur.fetchone()[0] if cur.description else None
    cur.close()
    return out


def _err(conn, sql, *args) -> str:
    with pytest.raises(psycopg2.Error) as e:
        _q(conn, sql, *args)
    return str(e.value)


def _approve(c, clinic, test, doctor):
    d = _q(c, "SELECT dx_test_digest(%s, %s)", clinic, test)
    _q(c, "SELECT dx_approve_test(%s, %s, %s, %s, 'Dr')", clinic, test, d, doctor)


@pytest.fixture
def lab(real_pg_conn):
    c = real_pg_conn
    clinic = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, features, dx_state)
                          VALUES ('Visit Lab', %s, 'diagstream', true, '{"dx_enabled": true}', 'ACTIVE') RETURNING id""",
                    "+9170" + uuid.uuid4().hex[:8]))
    branch = str(_q(c, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Main') RETURNING id", clinic))
    site = str(_q(c, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) VALUES (%s, %s, 'MAIN', 'Main lab') "
                     "RETURNING id", clinic, branch))
    dept = str(_q(c, "INSERT INTO dx_departments (clinic_id, code, name, report_heading) VALUES (%s, 'BIO', 'Bio', 'BIO') "
                     "RETURNING id", clinic))
    serum = str(_q(c, "INSERT INTO dx_containers (clinic_id, code, name, colour_hex, sample_type) VALUES "
                      "(%s, '01', 'Plain', '#C0392B', 'serum') RETURNING id", clinic))
    edta = str(_q(c, "INSERT INTO dx_containers (clinic_id, code, name, colour_hex, sample_type) VALUES "
                     "(%s, '07', 'EDTA', '#8E44AD', 'whole_blood') RETURNING id", clinic))
    doctor = str(_q(c, "INSERT INTO doctors (clinic_id, name, department, specialization, registration_number, is_active) "
                       "VALUES (%s, 'Dr. Rao', 'Lab', 'Path', 'R/1', true) RETURNING id", clinic))

    def test(name, code, price, cont=None, kind="test", mode="parameters", gender="all", approve=True, active=True):
        t = str(_q(c, """INSERT INTO lab_tests (clinic_id, name, test_code, price_paise, kind, department_id, container_id,
                         dx_result_mode, gender_restriction, is_active) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                         RETURNING id""", clinic, name, code, price, kind, dept, cont, mode, gender, active))
        if kind == "test" and mode == "parameters":
            _q(c, "INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, parameter_code) VALUES (%s, %s, %s, %s)",
               clinic, t, name, code)
        return t

    T = {"lipid": test("Lipid Profile", "LIPID", 55000, serum), "cbc": test("CBC", "CBC", 35000, edta),
         "hba1c": test("HbA1c", "HBA1C", 45000, edta), "ecg": test("ECG", "ECG", 30000, mode="document")}
    panel = test("Health Panel", "HP", 80000, kind="panel", approve=False)
    for child in ("lipid", "cbc", "hba1c"):
        _q(c, "INSERT INTO dx_test_components (clinic_id, parent_test_id, child_test_id) VALUES (%s, %s, %s)",
           clinic, panel, T[child])
    T["panel"] = panel
    for t in T.values():
        _approve(c, clinic, t, doctor)
    patient = str(_q(c, "INSERT INTO patients (clinic_id, name, phone, gender, age_years, age_recorded_on) VALUES "
                        "(%s, 'Asha', %s, 'female', 34, CURRENT_DATE) RETURNING id", clinic, "+9169" + uuid.uuid4().hex[:8]))
    return {"c": c, "clinic": clinic, "branch": branch, "site": site, "dept": dept, "serum": serum, "edta": edta,
            "doctor": doctor, "T": T, "test": test, "patient": patient}


def _register(s, items, key=None, conn=None, **extra):
    payload = {"branch_id": s["branch"], "patient_id": s["patient"], "source": "walk_in",
               "idempotency_key": key or uuid.uuid4().hex, "discount_limit_percent": 10,
               "items": [i if isinstance(i, dict) else {"lab_test_id": s["T"][i]} for i in items], **extra}
    return _q(conn or s["c"], "SELECT dx_register_visit(%s, %s::jsonb, NULL, 'desk')", s["clinic"], json.dumps(payload))


# ── registration ─────────────────────────────────────────────────────────────

def test_register_prices_explodes_and_groups_tubes(lab):
    v = _register(lab, ["lipid", "cbc", "hba1c", "ecg"])
    visit = v["visit"]
    assert visit["accession_number"].startswith(f"LAB-{visit['accession_year']}-") and len(visit["accession_number"]) == 15
    assert v["replayed"] is False and visit["patient_snapshot"]["name"] == "Asha"
    assert [(i["item_name"], i["price_paise"], i["patient_share_paise"], i["price_source"]) for i in v["items"]] == [
        ("Lipid Profile", 55000, 55000, "mrp"), ("CBC", 35000, 35000, "mrp"), ("HbA1c", 45000, 45000, "mrp"),
        ("ECG", 30000, 30000, "mrp")]
    # two tubes (serum, EDTA) sharing the visit's sequence; the ECG needs none
    yy = f"{visit['accession_year'] % 100:02d}"
    seq = f"{visit['sample_seq']:08d}"
    assert sorted(s["barcode"] for s in v["samples"]) == [f"{yy}01{seq}", f"{yy}07{seq}"]
    by_name = {o["item_name"]: o for o in v["orders"]}
    edta = next(s for s in v["samples"] if s["container_code"] == "07")
    assert by_name["CBC"]["dx_sample_id"] == by_name["HbA1c"]["dx_sample_id"] == edta["id"]
    assert by_name["ECG"]["dx_sample_id"] is None and by_name["ECG"]["sample_type"] is None
    row = _q(lab["c"], "SELECT to_jsonb(o) FROM opd_orders o WHERE id = %s", by_name["CBC"]["id"])
    assert (row["source"], row["doctor_id"], row["unit_price_paise"], row["appointment_id"]) == ("dx", None, 0, None)
    assert _q(lab["c"], "SELECT kind FROM dx_events WHERE accession_id = %s", visit["id"]) == "registered"


def test_panel_explodes_to_member_orders(lab):
    v = _register(lab, ["panel"])
    assert len(v["items"]) == 1 and v["items"][0]["kind"] == "panel" and v["items"][0]["price_paise"] == 80000
    assert sorted(o["item_name"] for o in v["orders"]) == ["CBC", "HbA1c", "Lipid Profile"]
    assert len(v["samples"]) == 2


def test_sample_floor_and_second_visit_numbers(lab):
    _q(lab["c"], "UPDATE clinics SET dx_settings = '{\"sample_seq_floor\": 16570}' WHERE id = %s", lab["clinic"])
    a = _register(lab, ["lipid"])
    b = _register(lab, ["cbc"])
    assert a["visit"]["sample_seq"] == 16570 and b["visit"]["sample_seq"] == 16571
    assert a["samples"][0]["barcode"].endswith("0100016570")
    assert b["visit"]["accession_seq"] == a["visit"]["accession_seq"] + 1


def test_refusals(lab):
    s, c = lab, lab["c"]
    draft = s["test"]("Draft test", "DRAFT", 100, s["serum"])
    assert "dx_test_not_approved:Draft test" in _err(c, "SELECT dx_register_visit(%s, %s::jsonb, NULL, 'x')", s["clinic"],
                                                     json.dumps({"branch_id": s["branch"], "patient_id": s["patient"],
                                                                 "idempotency_key": "k" * 10,
                                                                 "items": [{"lab_test_id": draft}]}))
    _q(c, "UPDATE lab_tests SET is_active = false WHERE id = %s", s["T"]["cbc"])
    with pytest.raises(psycopg2.Error) as e:
        _register(s, ["cbc"])
    assert "dx_test_inactive:CBC" in str(e.value)
    psa = s["test"]("PSA", "PSA", 800, s["serum"], gender="male")
    _approve(c, s["clinic"], psa, s["doctor"])
    with pytest.raises(psycopg2.Error) as e:
        _register(s, [{"lab_test_id": psa}])
    assert "dx_gender_restricted:PSA" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:
        _register(s, [{"lab_test_id": s["T"]["lipid"], "discount_paise": 6000}])      # > 10 % of 550
    assert "dx_discount_over_limit" in str(e.value)
    assert _register(s, [{"lab_test_id": s["T"]["lipid"], "discount_paise": 5500}])["items"][0]["patient_share_paise"] == 49500
    acc = str(_q(c, "INSERT INTO dx_b2b_accounts (clinic_id, name, kind) VALUES (%s, 'City Hospital', 'hospital') "
                    "RETURNING id", s["clinic"]))
    with pytest.raises(psycopg2.Error) as e:
        _register(s, ["lipid"], b2b_account_id=acc, billing_mode="b2b_credit")
    assert "dx_b2b_not_enabled" in str(e.value)
    _q(c, "UPDATE dx_b2b_accounts SET status = 'on_hold' WHERE id = %s", acc)
    with pytest.raises(psycopg2.Error) as e:
        _register(s, ["lipid"], b2b_account_id=acc)
    assert "dx_b2b_on_hold" in str(e.value)
    assert _q(c, "SELECT count(*) FROM dx_accessions WHERE clinic_id = %s", s["clinic"]) == 1   # only the valid one


def test_payer_routed_accounts_never_default_to_the_patient(lab):
    c, clinic = lab["c"], lab["clinic"]
    assert "dx_b2b_payer_routed" in _err(c, "INSERT INTO dx_b2b_accounts (clinic_id, name, kind) VALUES "
                                            "(%s, 'Medi TPA', 'insurance_tpa')", clinic)   # default delivery 'patient'
    _q(c, "INSERT INTO dx_b2b_accounts (clinic_id, name, kind, report_delivery) VALUES (%s, 'Medi TPA', 'insurance_tpa', "
          "'account')", clinic)
    assert "dx_b2b_payer_routed" in _err(c, "INSERT INTO dx_b2b_accounts (clinic_id, name, kind, report_delivery) VALUES "
                                            "(%s, 'Corp', 'corporate', 'both')", clinic)
    _q(c, "INSERT INTO dx_b2b_accounts (clinic_id, name, kind, report_delivery, patient_copy_authorised_at, "
          "patient_copy_reason) VALUES (%s, 'Corp', 'corporate', 'both', now(), 'Employer consent form on file')", clinic)
    assert "dx_b2b_authorisation_shape" in _err(c, "INSERT INTO dx_b2b_accounts (clinic_id, name, kind, "
                                                   "patient_copy_authorised_at) VALUES (%s, 'Xy Hosp', 'hospital', now())", clinic)


# ── idempotency and concurrency ──────────────────────────────────────────────

def test_same_key_replays(lab):
    a = _register(lab, ["lipid"], key="desk-click-0001")
    b = _register(lab, ["cbc"], key="desk-click-0001")
    assert b["replayed"] is True and b["visit"]["id"] == a["visit"]["id"] and len(b["items"]) == 1


def _parallel(n, fn):
    out, errs = [], []

    def run(i):
        try:
            out.append(fn(i))
        except Exception as e:     # noqa: BLE001
            errs.append(str(e))
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out, errs


def test_ten_parallel_retries_one_visit(lab, real_postgres_uri):
    def go(_):
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            return _register(lab, ["lipid", "cbc"], key="same-key-xyz-1", conn=conn)["visit"]["id"]
        finally:
            conn.close()
    out, errs = _parallel(10, go)
    assert not errs and len(set(out)) == 1
    assert _q(lab["c"], "SELECT count(*) FROM dx_accessions WHERE clinic_id = %s", lab["clinic"]) == 1
    assert _q(lab["c"], "SELECT count(*) FROM opd_orders WHERE clinic_id = %s AND source = 'dx'", lab["clinic"]) == 2


def test_twenty_parallel_desks_gapless_numbers(lab, real_postgres_uri):
    def go(_):
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            return _register(lab, ["cbc"], conn=conn)["visit"]
        finally:
            conn.close()
    out, errs = _parallel(20, go)
    assert not errs
    seqs = sorted(v["accession_seq"] for v in out)
    assert seqs == list(range(seqs[0], seqs[0] + 20))
    samples = sorted(v["sample_seq"] for v in out)
    assert len(set(samples)) == 20 and samples == list(range(samples[0], samples[0] + 20))


# ── bookings ─────────────────────────────────────────────────────────────────

def _booking(s, test_key, status="confirmed", home_fee=None, patient=None, amount=None):
    phone = _q(s["c"], "SELECT phone FROM patients WHERE id = %s", patient or s["patient"])
    cols = {"clinic_id": s["clinic"], "patient_id": patient or s["patient"], "patient_phone": phone, "patient_name": "Asha",
            "department": "Lab", "appointment_date": "2026-11-13", "status": status, "booking_type": "lab_test",
            "lab_test_id": s["T"][test_key], "amount_paise": amount}
    if home_fee:
        cols.update({"collection_mode": "home", "home_collection_fee_paise": home_fee, "collection_slot": "07:00-09:00",
                     "collection_status": "unassigned", "collection_address": "12 MG Road", "collection_lat": 17.4,
                     "collection_lng": 78.4,
                     "collection_contact_phone": phone})
    keys = ", ".join(cols)
    return str(_q(s["c"], f"INSERT INTO appointments ({keys}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                  *cols.values()))


def test_bookings_link_once_and_bring_their_fee(lab):
    b1, b2 = _booking(lab, "lipid", home_fee=10000), _booking(lab, "cbc")
    v = _register(lab, [], booking_ids=[b1, b2], source="whatsapp_booking")
    kinds = [(i["kind"], i["item_name"], i["price_source"], i["source_appointment_id"]) for i in v["items"]]
    assert kinds == [("test", "Lipid Profile", "booking", b1), ("fee", "Home Collection", "fee", b1),
                     ("test", "CBC", "booking", b2)]
    assert len(v["bookings"]) == 2
    assert _q(lab["c"], "SELECT checked_in_at IS NOT NULL FROM appointments WHERE id = %s", b1)
    with pytest.raises(psycopg2.Error) as e:
        _register(lab, [], booking_ids=[b2])
    assert "dx_booking_linked" in str(e.value)
    other = str(_q(lab["c"], "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Ravi', %s) RETURNING id",
                   lab["clinic"], "+9168" + uuid.uuid4().hex[:8]))
    for bad in (_booking(lab, "hba1c", status="cancelled"), _booking(lab, "hba1c", patient=other)):
        with pytest.raises(psycopg2.Error) as e:
            _register(lab, [], booking_ids=[bad])
        assert "dx_booking_invalid" in str(e.value)


# ── billing ──────────────────────────────────────────────────────────────────

def _bill(s, visit_id):
    return _q(s["c"], "SELECT dx_bill_visit(%s, %s, NULL, 'cashier')", s["clinic"], visit_id)


def test_bill_equals_sum_of_active_items_and_lines_are_managed(lab):
    s, c = lab, lab["c"]
    v = _register(s, ["lipid", {"lab_test_id": s["T"]["cbc"], "discount_paise": 3000}])
    out = _bill(s, v["visit"]["id"])
    inv = _q(c, "SELECT to_jsonb(i) FROM opd_invoices i WHERE id = %s", out["invoice_id"])
    assert out["lines_added"] == 2 and inv["appointment_id"] is None and inv["accession_id"] == v["visit"]["id"]
    assert inv["subtotal_paise"] == 55000 + 32000 == sum(i["patient_share_paise"] for i in v["items"])
    assert _bill(s, v["visit"]["id"])["lines_added"] == 0                          # idempotent refresh
    # the OPD draft-bill save (or any direct write) cannot touch DX lines
    line = _q(c, "SELECT id::text FROM opd_invoice_items WHERE invoice_id = %s LIMIT 1", out["invoice_id"])
    assert "dx_bill_lines_managed" in _err(c, "UPDATE opd_invoice_items SET unit_price_paise = 1 WHERE id = %s", line)
    assert "dx_bill_lines_managed" in _err(c, "DELETE FROM opd_invoice_items WHERE id = %s", line)
    assert "dx_bill_lines_managed" in _err(c, """INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type,
                                                description, unit_price_paise) VALUES (%s, %s, 9, 'other', 'X', 100)""",
                                           s["clinic"], out["invoice_id"])
    assert "dx_identity_immutable" in _err(c, "UPDATE opd_invoices SET accession_id = NULL WHERE id = %s", out["invoice_id"])
    # issue through the unchanged OPD path; later items go on a supplementary bill
    _q(c, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", out["invoice_id"])
    added = _q(c, "SELECT dx_add_items(%s, %s, %s::jsonb, 'add-key-0001', 0, NULL, 'desk')", s["clinic"],
               v["visit"]["id"], json.dumps([{"lab_test_id": s["T"]["hba1c"]}]))
    assert len(added["items"]) == 3
    sup = _bill(s, v["visit"]["id"])
    assert sup["invoice_id"] != out["invoice_id"] and sup["lines_added"] == 1
    # HbA1c joins the visit's pending EDTA tube
    hba1c = next(o for o in added["orders"] if o["item_name"] == "HbA1c")
    cbc = next(o for o in added["orders"] if o["item_name"] == "CBC")
    assert hba1c["dx_sample_id"] == cbc["dx_sample_id"]
    # the same add retried is a replay
    again = _q(c, "SELECT dx_add_items(%s, %s, %s::jsonb, 'add-key-0001', 0, NULL, 'desk')", s["clinic"],
               v["visit"]["id"], json.dumps([{"lab_test_id": s["T"]["hba1c"]}]))
    assert again["replayed"] is True and len(again["items"]) == 3


def test_b2b_visit_has_no_patient_bill(lab):
    acc = str(_q(lab["c"], "INSERT INTO dx_b2b_accounts (clinic_id, name, kind) VALUES (%s, 'City', 'hospital') "
                           "RETURNING id", lab["clinic"]))
    v = _register(lab, ["lipid"], b2b_account_id=acc, billing_mode="internal")
    assert v["items"][0]["patient_share_paise"] == 0
    assert "dx_not_patient_billing" in _err(lab["c"], "SELECT dx_bill_visit(%s, %s, NULL, 'x')", lab["clinic"],
                                            v["visit"]["id"])


# ── cancel ───────────────────────────────────────────────────────────────────

def test_cancel_item_before_and_after_issue(lab):
    s, c = lab, lab["c"]
    v = _register(s, ["lipid", "cbc"])
    out = _bill(s, v["visit"]["id"])
    lipid_item = next(i for i in v["items"] if i["item_name"] == "Lipid Profile")
    after = _q(c, "SELECT dx_cancel_item(%s, %s, 'patient declined', NULL, 'desk')", s["clinic"], lipid_item["id"])
    assert next(i for i in after["items"] if i["id"] == lipid_item["id"])["status"] == "cancelled"
    assert all(o["status"] == "cancelled" for o in after["orders"] if o["accession_item_id"] == lipid_item["id"])
    assert _q(c, "SELECT subtotal_paise FROM opd_invoices WHERE id = %s", out["invoice_id"]) == 35000
    _q(c, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", out["invoice_id"])
    cbc_item = next(i for i in v["items"] if i["item_name"] == "CBC")
    assert "dx_refund_first" in _err(c, "SELECT dx_cancel_item(%s, %s, 'x y z', NULL, 'desk')", s["clinic"], cbc_item["id"])


def test_cancel_item_in_progress_refused(lab):
    s, c = lab, lab["c"]
    v = _register(s, ["cbc"])
    o = v["orders"][0]["id"]
    _q(c, """UPDATE opd_orders SET status = 'sample_collected', specimen_barcode = 'B-123', sample_collected_at = now(),
             sample_collected_by_name = 'col' WHERE id = %s""", o)
    _q(c, "UPDATE opd_orders SET status = 'in_progress' WHERE id = %s", o)
    assert "dx_item_in_progress" in _err(c, "SELECT dx_cancel_item(%s, %s, 'x y z', NULL, 'desk')", s["clinic"],
                                         v["items"][0]["id"])


def test_cancel_visit_releases_bookings(lab):
    s, c = lab, lab["c"]
    b = _booking(s, "lipid")
    v = _register(s, [], booking_ids=[b])
    _bill(s, v["visit"]["id"])
    out = _q(c, "SELECT dx_cancel_visit(%s, %s, 'wrong patient', NULL, 'desk')", s["clinic"], v["visit"]["id"])
    assert out["visit"]["status"] == "cancelled" and out["bookings"] == [] and out["invoices"] == []
    assert all(sm["status"] == "discarded" for sm in out["samples"])
    assert _register(s, [], booking_ids=[b])["bookings"][0]["appointment_id"] == b       # bookable again


def test_cancel_visit_refused_once_a_tube_is_collected(lab):
    s, c = lab, lab["c"]
    v = _register(s, ["cbc"])
    _q(c, "SELECT set_config('kriya.dx_tx', %s, false)", s["clinic"])
    _q(c, "UPDATE dx_samples SET status = 'collected', collected_at = now() WHERE accession_id = %s", v["visit"]["id"])
    _q(c, "SELECT set_config('kriya.dx_tx', '', false)")
    assert "dx_visit_has_samples" in _err(c, "SELECT dx_cancel_visit(%s, %s, 'x y z', NULL, 'desk')", s["clinic"],
                                          v["visit"]["id"])


# ── guards ───────────────────────────────────────────────────────────────────

def test_direct_writes_are_refused(lab):
    s, c = lab, lab["c"]
    v = _register(s, ["cbc"])
    vid = v["visit"]["id"]
    assert "dx_managed:dx_accessions" in _err(c, "UPDATE dx_accessions SET priority = 'stat' WHERE id = %s", vid)
    assert "dx_managed:dx_accession_items" in _err(c, "UPDATE dx_accession_items SET price_paise = 1 WHERE accession_id = %s",
                                                   vid)
    assert "dx_managed:dx_samples" in _err(c, "DELETE FROM dx_samples WHERE accession_id = %s", vid)
    assert "dx_append_only:dx_events" in _err(c, "UPDATE dx_events SET kind = 'x' WHERE accession_id = %s", vid)
    assert "dx_managed:dx_accessions" in _err(c, """INSERT INTO dx_accessions (clinic_id, branch_id, accession_year,
                                                    accession_seq, patient_id, source, patient_snapshot, created_by_name)
                                                    VALUES (%s, %s, 2026, 999999, %s, 'walk_in', '{}', 'x')""",
                                              s["clinic"], s["branch"], s["patient"])
    # an OPD order still needs a doctor; a DX order needs its visit
    assert "opd_orders_source_shape" in _err(c, """INSERT INTO opd_orders (clinic_id, source, patient_id, order_type,
                                                   catalog_type, item_name, ordered_by_name)
                                                   VALUES (%s, 'dx', %s, 'investigation', 'custom', 'X', 'x')""",
                                             s["clinic"], s["patient"])


def test_a_test_ordered_in_dx_cannot_be_deleted(lab):
    _register(lab, ["cbc"])
    assert "dx_test_ordered" in _err(lab["c"], "DELETE FROM lab_tests WHERE id = %s", lab["T"]["cbc"])
    unused = lab["test"]("Unused", "UNUSED", 100, lab["serum"])
    _q(lab["c"], "DELETE FROM lab_tests WHERE id = %s", unused)                 # never ordered: still deletable


def test_redaction_keeps_the_visit_and_its_money(lab):
    v = _register(lab, ["cbc"], external_ref="IP-778")
    assert _q(lab["c"], "SELECT dx_redact_patient(%s, %s)", lab["clinic"], lab["patient"]) == 1
    row = _q(lab["c"], "SELECT to_jsonb(a) FROM dx_accessions a WHERE id = %s", v["visit"]["id"])
    assert row["patient_snapshot"]["name"] == "[REDACTED]" and row["patient_snapshot"]["gender"] == "female"
    assert row["external_ref"] is None
    assert _q(lab["c"], "SELECT count(*) FROM dx_accession_items WHERE accession_id = %s", v["visit"]["id"]) == 1


def test_quote_matches_registration(lab):
    q = _q(lab["c"], "SELECT dx_quote(%s, %s::jsonb)", lab["clinic"], json.dumps({
        "branch_id": lab["branch"], "patient_id": lab["patient"], "discount_limit_percent": 10,
        "items": [{"lab_test_id": lab["T"]["panel"]}]}))
    v = _register(lab, ["panel"])
    assert q[0]["patient_share_paise"] == v["items"][0]["patient_share_paise"] == 80000
    assert len(q[0]["tests"]) == 3


# ── purge, rollback ──────────────────────────────────────────────────────────

def test_purge_v3_removes_visits_orders_and_catalogue(lab):
    v = _register(lab, ["lipid", "cbc"])
    _bill(lab, v["visit"]["id"])
    out = _q(lab["c"], "SELECT dx_purge_clinic(%s)", lab["clinic"])
    assert out["visits"] == 1 and out["orders"] == 2
    for t in ("dx_accessions", "dx_accession_items", "dx_samples", "dx_events", "dx_containers"):
        assert _q(lab["c"], f"SELECT count(*) FROM {t} WHERE clinic_id = %s", lab["clinic"]) == 0
    assert _q(lab["c"], "SELECT count(*) FROM opd_orders WHERE clinic_id = %s", lab["clinic"]) == 0
    _q(lab["c"], "SELECT opd_purge_clinic(%s)", lab["clinic"])                  # the OPD ledger goes with the OPD purge
    assert _q(lab["c"], "SELECT count(*) FROM opd_invoices WHERE clinic_id = %s", lab["clinic"]) == 0


def test_rollback_refuses_visits_then_reapplies(lab):
    _register(lab, ["cbc"])
    cur = lab["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "127_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_127(cur)
        cur.execute("SELECT to_regclass('public.dx_accessions'), to_regclass('public.dx_samples')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT is_nullable FROM information_schema.columns WHERE table_name = 'opd_orders' "
                    "AND column_name = 'doctor_id'")
        assert cur.fetchone() == ("NO",)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'dx\\_%%'
                         AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x WHERE x LIKE 'search_path=%%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
