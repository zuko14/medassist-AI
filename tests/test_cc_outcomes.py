"""Outcomes (ADR-CC-15, OUTCOMES.md §4 worked example; TEST-PLAN P8).

Ten T2DM patients active ≥ 90 days. Baseline: 4 of 10 had a checked-in visit in the 90 days before enrollment → 40 %.
First 90 days: 7 of 10 had a verified visit; an 8th only has status 'completed' with no check-in → not counted → 70 %.
HbA1c actions due for 10, verified for 6 → labs on time 60 %. Verified revenue = the verifications' paid amounts:
7 × ₹500 + 6 × ₹450 + 12 refill sales (₹9,600) = ₹15,800; baseline revenue (paid bills in the prior 90 days) ₹7,100 →
delta ₹8,700. Of the 6 tested, 2 have HbA1c < 7.0 → at target 33 %. The report says "associated with", never "caused by"; the CSV is formula-safe.
"""

import asyncio
import uuid
from datetime import timedelta

from app.services import cc_verify
from app.utils.helpers import today_ist
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _visit(care, pid, day, checked_in=True, status="confirmed"):
    _q(care.conn, "INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id, doctor_name, "
                  "department, appointment_date, appointment_time, status, is_walk_in, booking_type, checked_in_at) VALUES "
                  "(%s, %s, %s, 'P', %s, 'Dr. Meena', 'Medicine', %s, %s, %s, false, 'consultation', "
                  "CASE WHEN %s THEN %s::date + time '10:00' END)", care.clinic, pid, care.phone(pid), care.doctor,
       day.isoformat(), f"{uuid.uuid4().int % 24:02d}:{uuid.uuid4().int % 60:02d}", status, checked_in, day.isoformat())


def test_worked_example(care):  # noqa: F811
    c, today = care.conn, today_ist()
    start = today - timedelta(days=100)
    pts = []
    for i in range(10):
        e, pid = care.enroll()
        _q(c, "UPDATE cc_enrollments SET enrolled_at = %s WHERE id = %s", start.isoformat() + "T10:00:00+05:30", e["id"])
        pts.append((e, pid))
        if i < 4:
            _visit(care, pid, start - timedelta(days=30))                       # baseline visit
        if i < 7:
            _visit(care, pid, start + timedelta(days=40))                       # verified visit in the first 90 days
        elif i == 7:
            _visit(care, pid, start + timedelta(days=40), checked_in=False, status="completed")   # no-show: not counted
    for i, (e, pid) in enumerate(pts):
        case = str(_q(c, "INSERT INTO cc_cases (clinic_id, patient_id, enrollment_id, reason, tier, due_by, status) VALUES "
                         "(%s, %s, %s, 't2dm:a1c_overdue', 'watch', now(), 'actioned') RETURNING id", care.clinic, pid, e["id"]))
        lab = str(_q(c, "INSERT INTO cc_actions (clinic_id, case_id, action_code, decided_by_name, decider_role, status) "
                        "VALUES (%s, %s, 'order_lab', 'Dr', 'doctor', %s) RETURNING id", care.clinic, case,
                     "verified" if i < 6 else "not_verified"))
        if i < 6:
            _q(c, "INSERT INTO cc_verifications (clinic_id, action_id, evidence_kind, evidence_ref, revenue_paise) VALUES "
                  "(%s, %s, 'validated_result', '{\"table\": \"lab_results\", \"id\": \"x\"}', 45000)", care.clinic, lab)
        if i < 7:
            v = str(_q(c, "INSERT INTO cc_actions (clinic_id, case_id, action_code, decided_by_name, decider_role, status) "
                          "VALUES (%s, %s, 'book_review_visit', 'Dr', 'doctor', 'verified') RETURNING id", care.clinic, case))
            _q(c, "INSERT INTO cc_verifications (clinic_id, action_id, evidence_kind, evidence_ref, revenue_paise) VALUES "
                  "(%s, %s, 'check_in', '{\"table\": \"appointments\", \"id\": \"x\"}', 50000)", care.clinic, v)
    for i, (e, pid) in enumerate(pts[:6]):                                    # the 6 tested: 2 below 7.0 %
        _q(c, "INSERT INTO cc_observations (clinic_id, enrollment_id, measure_code, value_num, unit, observed_at, source) "
              "VALUES (%s, %s, 'hba1c', %s, '%%', now(), 'whatsapp_typed')", care.clinic, e["id"], 6.6 if i < 2 else 7.6)
        if i == 2:                                                             # an older at-target value never wins
            _q(c, "INSERT INTO cc_observations (clinic_id, enrollment_id, measure_code, value_num, unit, observed_at, source) "
                  "VALUES (%s, %s, 'hba1c', 6.2, '%%', now() - interval '200 days', 'whatsapp_typed')", care.clinic, e["id"])
    refill_case = {}
    for i in range(12):                                                        # 12 verified refill sales = ₹9,600
        e, pid = pts[i % 10]
        if pid not in refill_case:
            refill_case[pid] = str(_q(c, "INSERT INTO cc_cases (clinic_id, patient_id, enrollment_id, reason, tier, due_by, "
                                         "status) VALUES (%s, %s, %s, 'refill_lapsed', 'watch', now(), 'actioned') RETURNING id",
                                      care.clinic, pid, e["id"]))
        case = refill_case[pid]
        act = str(_q(c, "INSERT INTO cc_actions (clinic_id, case_id, action_code, decided_by_name, decider_role, status) "
                        "VALUES (%s, %s, 'continue_plan', 'Coord', 'coordinator', 'verified') RETURNING id", care.clinic, case))
        _q(c, "INSERT INTO cc_verifications (clinic_id, action_id, evidence_kind, evidence_ref, revenue_paise) VALUES "
              "(%s, %s, 'completed_sale', '{\"table\": \"rx_sales\", \"id\": \"x\"}', 80000)", care.clinic, act)
    seq = uuid.uuid4().int % 900000 + 1000
    for n, (i, paise) in enumerate(((0, 50000), (1, 50000), (2, 50000), (3, 50000), (0, 45000), (1, 45000), (2, 420000))):
        _q(c, "INSERT INTO opd_invoices (clinic_id, patient_id, status, invoice_year, invoice_seq, subtotal_paise, paid_paise, "
              "issued_at) VALUES (%s, %s, 'paid', 2026, %s, %s, %s, %s)", care.clinic, pts[i][1], seq + n, paise, paise,
           (start - timedelta(days=20)).isoformat() + "T11:00:00+05:30")             # baseline: ₹7,100 in total
    _q(c, "INSERT INTO opd_invoices (clinic_id, patient_id, status, invoice_year, invoice_seq, subtotal_paise, paid_paise, "
          "issued_at) VALUES (%s, %s, 'paid', 2026, %s, 99900, 99900, %s)", care.clinic, pts[0][1], seq + 50,
       (start - timedelta(days=120)).isoformat() + "T11:00:00+05:30")                 # before the 90-day window
    r = asyncio.run(cc_verify.report(care.clinic, "t2dm", today.replace(day=1) - timedelta(days=1), today))
    assert (r["cohort_90d"], r["return_rate"], r["baseline_return_rate"]) == (10, 70, 40)
    assert (r["labs_due"], r["labs_on_time"]) == (10, 60)
    assert r["visits_verified"] == 7 and r["revenue_paise"] == 1_580_000                     # ₹15,800
    assert r["revenue_split_paise"] == {"visits": 350_000, "labs": 270_000, "refills": 960_000}
    assert r["refill_revenue_paise"] == 960_000 and r["verified_by"]["completed_sale"] == 12
    assert (r["baseline_revenue_paise"], r["revenue_delta_paise"]) == (710_000, 870_000)    # ₹7,100 → delta ₹8,700
    assert (r["at_target"], r["at_target_count"], r["with_value"]) == (33, 2, 6)
    assert r["verified_by"]["check_in"] == 7 and r["funnel"]["consented"] == 10
    assert "Associated with, not caused by" in r["note"] and r["method_version"] == "outcomes-1"
    care.api.as_(care.u.admin)
    api_r = care.api.call("GET", "/admin/cc/outcomes", care.clinic, protocol="t2dm").json()
    assert api_r["return_rate"] == 70
    csv = care.api.call("GET", "/admin/cc/outcomes/export.csv", care.clinic, protocol="t2dm")
    assert csv.status_code == 200 and "Return rate %,70" in csv.text and "Verified revenue (Rs),15800.0" in csv.text
    assert "At target % of tested,33" in csv.text and "Baseline revenue (Rs),7100.0" in csv.text
    assert "Verified refill revenue (Rs),9600.0" in csv.text
    care.api.as_(care.u.nurse)
    assert care.api.call("GET", "/admin/cc/outcomes/export.csv", care.clinic).status_code == 403
    assert care.api.call("GET", "/admin/cc/outcomes", care.clinic, **{"from": "2020-01-01"}).status_code == 422
    assert asyncio.run(cc_verify.snapshot_month(care.clinic, today.replace(day=1))) == 1
    assert asyncio.run(cc_verify.snapshot_month(care.clinic, today.replace(day=1))) == 1      # idempotent upsert
    assert _q(c, "SELECT count(*) FROM cc_outcome_snapshots WHERE clinic_id = %s", care.clinic) == 1
    assert _q(c, "SELECT at_target || ':' || refills_verified || ':' || (baseline->>'revenue_paise') FROM cc_outcome_snapshots "
                 "WHERE clinic_id = %s", care.clinic) == "2:12:710000"


def test_pct_and_csv_are_safe():
    assert cc_verify.pct(1, 3) == 33 and cc_verify.pct(0, 0) is None
    r = {"method_version": "outcomes-1", "from": "a", "to": "b", "protocol": "=HYPERLINK(1)", "return_rate": 1,
         "baseline_return_rate": 0, "labs_on_time": None, "visits_verified": 0, "revenue_paise": 0, "answered_rate": None,
         "note": "n"}
    assert "'=HYPERLINK" in cc_verify.report_csv(r) or "\"'=HYPERLINK" in cc_verify.report_csv(r)
