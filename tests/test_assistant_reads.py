"""KK-P4-01: every R0 read in action-registry-spec §4 (owner decisions 2026-10-14 in status.md).

Each read dispatches the SAME endpoint and query its page uses (through the
executor, as the caller), so the result is the page's own data for the same
filters. Cards keep at most 8 rows and mask phone numbers before anything is
stored in assistant_runs.result. A read never runs when the text asks for a
change ("cancel today's appointments").

connectors.failed and patients.search were held until D-21 / D-22 were fixed.
Deferred: patient.view, voice.call_detail (no id resolver), patient.duplicate_check
(KK-P4-07), voice.test_turn (KK-P4-13).
"""

import dataclasses
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.assistant import present, rules
from app.assistant.executor import Outcome
from app.assistant.registry import PAGE_SYNONYMS, PAGES, REGISTRY
from app.main import app
from app.routers import admin
from app.routers import assistant as assistant_router
from app.voice.dates import today_ist
from tests.test_assistant_runs import CID, _user, as_user, db  # noqa: F401  (fixtures)

READS = {
    "stats.summary", "insights.view", "opd.analytics", "appointments.search", "bookings.pending_review",
    "payments.reconciliation", "payments.stats", "opd.queue", "doctors.list", "lab_tests.list",
    "treatments.list", "reports.deliveries", "reports.queue", "homecollect.visits", "leads.list",
    "messaging.usage", "voice.calls", "voice.live", "voice.analytics", "voice.usage", "voice.outbound_list",
    "notifications.list", "audit.recent", "ai.usage", "ai.budget", "subscription.view", "data.storage",
    "connectors.failed", "patients.search",  # released 2026-10-14 after D-21 / D-22
    "lab_tests.cleanup",  # KK-P4-09
    "voice.learning_review",  # KK-P4-13
    "ipd.census", "ipd.inpatients", "ipd.waiting", "ipd.discharges",  # IPD OS (migration 111)
    "orders.to_collect", "lis.pending_signoff", "emergency.board", "services.prices",  # Hospital OS
    "ipd_finance.receivables",
    "ot.today",  # operation theatre (migration 122)
    "opd.collections", "opd.unpaid", "holidays.list",  # 2026-10-26: everyday questions from the probe
}
NOT_YET = {"patient.view", "voice.call_detail", "patient.duplicate_check", "voice.test_turn"}
CORPUS = json.loads((Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                    .read_text(encoding="utf-8"))["phrases"]
TODAY = today_ist().isoformat()
PHONE = "919876543210"


# ── registration ─────────────────────────────────────────────────────────────

def test_exactly_the_approved_reads_are_registered():
    playbook_reads = {"doctor_leave.impact", "leads.funnel"}  # several GETs, tested with their playbooks
    reads = {a for a, s in REGISTRY.items() if s.risk == "R0" and s.endpoint and a not in playbook_reads}
    assert reads == READS
    assert not NOT_YET & set(REGISTRY)


@pytest.mark.parametrize("aid", sorted(READS))
def test_a_read_is_one_get_of_its_endpoint_with_a_presenter(aid):
    spec = REGISTRY[aid]
    assert spec.endpoint[0] == "GET" and spec.present and spec.page in (None, *PAGES)
    steps = spec.call({})
    assert len(steps) == 1 and steps[0].call.method == "GET" and steps[0].call.path == spec.endpoint[1]
    assert steps[0].verify is None and "clinic_id" not in steps[0].call.query  # the executor sets the scope


@pytest.mark.parametrize("aid", sorted(READS))
def test_no_trigger_is_just_a_page_name(aid):
    names = {rules._norm(n) for p, label in PAGES.items() for n in (p, label, *PAGE_SYNONYMS.get(p, ()))}
    assert not {rules._norm(t) for t in REGISTRY[aid].triggers} & names  # bare page names stay navigation


@pytest.mark.parametrize("aid", sorted(READS))
def test_corpus_has_five_phrases_te_hi_and_two_negatives(aid):
    pos = [e for e in CORPUS if e["expect"] == f"action:{aid}"]
    assert len(pos) >= 5, aid
    assert {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}, aid
    assert len([e for e in CORPUS if e.get("near") == aid and e["expect"] != f"action:{aid}"]) >= 2, aid


# ── routing guards ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "cancel today's appointments", "delete the lab test rates", "send new leads a message",
    "mark all my notifications read", "aaj ke appointments cancel karo", "आज के अपॉइंटमेंट रद्द करो",
    "ee roju appointments raddu cheyyi", "discharge all admitted patients", "transfer the ward patients",
])
def test_a_read_never_runs_when_the_text_asks_for_a_change(text):
    r = rules.route(text)  # the matching write (preview + confirm) may answer it; a read never does
    assert r.kind != "action" or REGISTRY[r.action_id].risk != "R0"


def test_a_read_routed_by_the_llm_is_also_refused_for_a_change():
    assert rules.fill(REGISTRY["appointments.search"], "cancel today's appointments").kind == "none"


def test_a_date_fills_the_optional_slot_and_no_date_means_today():
    r = rules.route("appointments on 20 october 2026")
    assert (r.kind, r.action_id, r.params) == ("action", "appointments.search", {"date": "2026-10-20"})
    q = REGISTRY["appointments.search"].call({})[0].call.query
    assert (q["date_from"], q["date_to"]) == (TODAY, TODAY)
    q = REGISTRY["appointments.search"].call({"date": "2026-10-20"})[0].call.query
    assert (q["date_from"], q["date_to"]) == ("2026-10-20", "2026-10-20")


# ── presenters (pure) ────────────────────────────────────────────────────────

def test_table_keeps_8_rows_reports_the_total_and_masks_phones():
    rows = [{"name": f"P{i}", "phone": PHONE} for i in range(12)]
    out = present.table("Leads", "leads", (("Name", "name", "text"), ("Phone", "phone", "phone")), rows, 40)
    card = out["cards"][0]
    assert card["type"] == "table" and card["total"] == 40 and len(card["rows"]) == 8
    assert PHONE not in json.dumps(out) and card["rows"][0][1].endswith("3210")


def test_empty_table_is_a_plain_note_with_the_page_link():
    out = present.table("Leads", "leads", (("Name", "name", "text"),), [], 0)
    assert out["cards"][0]["type"] == "help" and out["cards"][0]["page"] == "leads"


@pytest.mark.parametrize("value, kind, expected", [
    (123456, "paise", "₹1,235"), (1500.0, "rupees", "₹1,500"), (None, "text", "—"), ("", "text", "—"),
    ("pending_review", "label", "Pending review"), (True, "yesno", "Yes"), (42.5, "pct", "42.5%"),
    ("2026-10-14T04:30:00+00:00", "time", "14 Oct, 10:00"), ("2026-10-14", "date", "14 Oct"),
    ("not a date", "time", "—"), ("x" * 200, "text", "x" * 80), (PHONE, "phone", "919XXXXX3210"),
])
def test_fmt(value, kind, expected):
    assert present.fmt(value, kind) == expected


@pytest.mark.parametrize("aid", sorted(READS))
@pytest.mark.parametrize("data", [None, {}, [], {"unexpected": 1}, "text"], ids=["none", "dict", "list", "odd", "str"])
def test_presenters_never_raise_on_odd_data(aid, data):
    out = REGISTRY[aid].present([Outcome("ok", 200, None, "", data)], {})
    assert out["cards"] and isinstance(out["message"], str)


# ── through the API: same endpoint, same query as the page, page data shown ──

fake = FastAPI()
SEEN: list = []
APPT = {"appointment_time": "10:30", "patient_name": "Ravi Kumar", "department": "ENT", "status": "confirmed",
        "booking_ref": "KR-1001", "appointment_date": TODAY, "patient_phone": PHONE}
DATA = {
    "/admin/ipd/census": {"date": TODAY, "occupancy_pct": 62.5, "inpatients": 5, "awaiting_bed": 1, "discharge_pending": 1,
                          "admitted_today": 2, "discharged_today": 1,
                          "beds": {"available": 3, "occupied": 5, "cleaning": 1, "reserved": 0, "maintenance": 0,
                                   "blocked": 0, "total": 9}},
    "/admin/ipd/admissions": {"admissions": [{"admission_number": "IPD-2026-00001", "patient_name": "Ravi Kumar",
                                              "mrn": "MRN-000001", "bed": "101", "attending_doctor": "Dr. Rao",
                                              "status": "admitted"}], "limit": 100, "offset": 0},
    "/admin/ipd/worklist": {"awaiting_bed": [{"admission_number": "IPD-2026-00002", "patient_name": "Sita",
                                              "mrn": "MRN-000002", "priority": "urgent"}],
                            "discharge_pending": [{"admission_number": "IPD-2026-00001", "patient_name": "Ravi Kumar",
                                                   "mrn": "MRN-000001", "discharge_type": "regular"}],
                            "beds_to_clean": [], "active_orders": []},
    "/admin/lab-tests/cleanup-suggestions": {"summary": {"total_tests_analyzed": 120, "duplicate_groups_count": 3,
                                                         "suspicious_prices_count": 2, "missing_prep_count": 5,
                                                         "uncategorized_count": 9}, "duplicates": [{"name": "CBC"}]},
    "/admin/stats": {"total_appointments": 42, "confirmed": 30, "completed": 8, "cancelled": 4, "no_show": 0,
                     "new_patients": 11, "total_patients": 300, "error": "Traceback secret"},
    "/admin/insights": {"bookings": {"totals": {"total": 50, "confirmed": 40, "completed": 5, "cancelled": 5},
                                     "unique_patients": 45, "repeat_rate": 12.5},
                        "revenue": {"collected_inr": 25000.0, "refunded_inr": 0.0, "pending_inr": 500.0}},
    "/admin/opd/analytics": {"footfall": {"total": 18, "walk_in": 6, "booked": 12}, "no_shows": 1,
                             "cancellations_in_queue": 0, "collections": {"net": 900000, "outstanding_paise": 5000},
                             "prescriptions": {"signed": 15}, "waits": {}, "peak_hours": []},
    "/admin/appointments": {"appointments": [APPT] * 3, "total": 3, "truncated": False},
    "/admin/bookings/pending-review": {"bookings": [{**APPT, "status": "pending_review", "amount_paise": 50000}]},
    "/admin/payments/reconciliation": {"date": TODAY, "confirmed_count": 7, "confirmed_total_rupees": 3500.0,
                                       "pending_review_count": 1},
    "/admin/payments/stats": {"confirmed_count": 20, "confirmed_amount_rupees": 10000.0, "pending_review_count": 1,
                              "refunded_count": 2, "refunded_amount_rupees": 1000.0, "expired_count": 3},
    "/admin/opd/queue": {"doctors": [{"doctor_name": "Dr. Rao", "rows": [
        {"token_number": 4, "patient_name": "Sita", "stage": "waiting", "doctor_name": "Dr. Rao"},
        {"token_number": 5, "patient_name": "Gita", "stage": "in_consultation", "doctor_name": "Dr. Rao"}]}]},
    "/admin/doctors": [{"name": "Dr. Rao", "specialization": "ENT", "department": "ENT", "is_active": True,
                        "consultation_fee": 500}],
    "/admin/opd/collections/summary": {"date": TODAY, "gross_paise": 950000, "refunds_paise": 50000,
                                       "net_paise": 900000, "counter_paise": 800000, "online_paise": 150000,
                                       "modes": {"cash": 500000, "upi": 250000, "card": 50000, "payment_link": 100000,
                                                 "prepaid_online": 50000},
                                       "outstanding_dues_paise": 30000, "invoices_count": 12,
                                       "cashiers": {"Asha": {"collections_paise": 800000}}},
    "/admin/opd/invoices": [{"invoice_number": "INV-2026-00007", "status": "partially_paid", "total_paise": 50000,
                             "paid_paise": 20000, "issued_at": "2026-10-14T04:30:00+00:00",
                             "patient_snapshot": {"name": "Ravi Kumar", "phone": PHONE}}],
    "/admin/holidays": [{"holiday_date": "2020-01-26", "name": "Republic Day 2020"},
                        {"holiday_date": "2099-11-01", "name": "Diwali"}],
    "/admin/diagnostic/worklist": {"orders": [{"patient_name": "Ravi Kumar", "mrn": "MRN-1", "item_name": "CBC",
                                               "urgency": "stat", "status": "billed", "doctor_name": "Dr Arjun"}],
                                   "total": 1},
    "/admin/lis/results": {"results": [{"patient_name": "Sita", "mrn": "MRN-2", "test_name": "LFT",
                                        "has_critical_value": True, "urgency": "routine",
                                        "entered_at": "2026-10-14T04:30:00+00:00"}], "total": 1},
    "/admin/emergency/visits": {"visits": [{"patient_name": "Unknown Male 1", "mrn": "T-1", "triage_category": "red",
                                            "status": "in_treatment", "doctor_name": "Dr Ravi",
                                            "mlc": {"mlc_number": "MLC-7"}, "created_at": "2026-10-14T04:30:00+00:00"}],
                                "counts": {"red": 1}, "open": 1},
    "/admin/ot/schedule": {"surgeries": [{"scheduled_start": "2026-10-14T03:30:00+00:00", "theatre_name": "OT 1",
                                          "patient_name": "Ravi", "mrn": "MRN-3", "procedure_name": "Appendicectomy",
                                          "surgeon_name": "Dr. Iyer", "status": "scheduled"}], "active": 1},
    "/admin/masters/services": {"services": [{"code": "XR1", "name": "X Ray Chest PA", "category": "radiology",
                                              "standard_price_paise": 45000}], "can_manage": True},
    "/admin/ipd/receivables": {"bills": [], "ageing_paise": {"0-30": 1250000, "31-60": 0, "61-90": 0, "90+": 0},
                               "total_due_paise": 1250000, "deposits_held": [], "deposits_held_paise": 50000},
    "/admin/lab-tests": [{"name": "CBC", "price_paise": 35000, "sample_type": "blood", "is_active": True}],
    "/admin/treatments": [{"name": "Root canal", "category": "Dental", "price_from_paise": 450000, "is_active": True}],
    "/admin/reports/deliveries": {"deliveries": [{"sent_at": "2026-10-14T04:30:00+00:00", "patient_name": "Ravi",
                                                  "patient_phone": "91XXXXXX3210", "report_name": "CBC",
                                                  "status": "sent", "delivery_status": "read"}]},
    "/admin/reports/queue": {"needs_review": [{"patient_name": "Anu", "report_name": "LFT", "status": "needs_review"}],
                             "failed_reports": [], "connector_failures": [
                                 {"patient_name": "Bala", "report_name": "TSH", "last_error": "timeout"}],
                             "total_queued": 2},
    "/admin/home-collection/visits": {"date": TODAY, "summary": {"assigned": 1}, "visits": [
        {"collection_slot": "07:00-08:00", "patient_name": "Lakshmi", "lab_test_name": "HbA1c",
         "collection_status": "assigned", "phlebotomist_name": "Kiran", "collection_contact_phone": PHONE}]},
    "/admin/leads": {"rows": [{"name": "Arun", "phone": PHONE, "stage": "hot",
                               "first_contact_at": "2026-10-13T05:00:00+00:00"}] * 2, "total": 9, "summary": {}},
    "/admin/messaging-usage": {"plan_display_name": "Essential", "messages_sent": 120, "included_messages": 1000,
                               "messages_remaining": 880, "usage_percent": 12.0},
    "/admin/voice/calls": {"calls": [{"started_at": "2026-10-14T04:30:00+00:00", "caller_phone": PHONE,
                                      "status": "completed", "outcome": "booked"}], "total": 1},
    "/admin/voice/live": {"calls": [{"started_at": "2026-10-14T04:30:00+00:00", "caller_phone": PHONE,
                                     "status": "in_progress", "waiting_for": "date"}]},
    "/admin/voice/analytics": {"days": 7, "total_calls": 31, "autonomy_overall": 74.2, "avg_quality": 4.1,
                               "minutes": 55.5},
    "/admin/voice/usage": {"calls": 31, "minutes": 55.5, "cost_paise": 12000, "budget_paise": 100000,
                           "budget_used_pct": 12.0},
    "/admin/voice/outbound": {"jobs": [{"created_at": "2026-10-14T04:30:00+00:00", "patient_phone": PHONE,
                                        "status": "queued", "attempts": 0}]},
    "/admin/notifications": {"success": True, "notifications": [
        {"created_at": "2026-10-14T04:30:00+00:00", "title": "Plan renewed", "is_read": False}]},
    "/admin/audit-logs": {"audit_logs": [{"created_at": "2026-10-14T04:30:00+00:00", "username": "desk1",
                                          "action": "CREATE_LEAVE", "details": {"phone": PHONE}}]},
    "/admin/ai/usage": {"month": "2026-10", "total_spend_rupees": 120.0, "admin_spend_rupees": 20.0,
                        "patient_spend_rupees": 100.0, "monthly_budget_rupees": 500.0, "is_budget_exceeded": False},
    "/admin/ai/budget": {"budget_rupees": 500.0, "current_spend_rupees": 120.0},
    "/admin/subscription": {"success": True, "subscription": {"status": "active", "days_remaining": 40},
                            "daily_reports": {"limit": 200}},
    "/admin/data/storage": {"success": True, "used": 1200, "limit": 5000, "remaining": 3800, "percent": 24.0},
    "/admin/connectors/failed-reports": {"failed_reports": [
        {"last_attempt_at": "2026-10-14T04:30:00+00:00", "patient_name": "Bala", "report_name": "TSH",
         "last_error": "upload timeout"}]},
    "/admin/patients": {"patients": [
        {"name": "Ravi Kumar", "phone": PHONE, "visit_count": 2, "last_visit": "2026-10-10"},
        {"name": "Sita Devi", "phone": "919000011111", "visit_count": 1},
        {"name": "Ravindra", "phone": "919000022222", "visit_count": 1}]},
    "/admin/voice/learning/suggestions": {"catalogue": {}, "suggestions": [
        {"patient_phrase": "skin doctor", "category": "specialty_synonym", "evidence_count": 4,
         "proposed_canonical": "Dermatology", "sample_utterances": [f"call me on {PHONE}"]}]},
}


@fake.get("/admin/{rest:path}")
async def _serve(rest: str, request: Request):
    path = "/admin/" + rest
    SEEN.append((path, dict(request.query_params)))
    return DATA[path]


# (text, action, path, the page's own query (subset), a string a row must show, total)
CASES = [
    ("how many beds are free", "ipd.census", "/admin/ipd/census", {}, "62.5", None),
    ("who is admitted", "ipd.inpatients", "/admin/ipd/admissions", {"status": "admitted,discharge_pending", "limit": "100"},
     "Ravi Kumar", 1),
    ("pending admissions today", "ipd.waiting", "/admin/ipd/worklist", {}, "Sita", 1),
    ("pending discharges", "ipd.discharges", "/admin/ipd/worklist", {}, "Ravi Kumar", 1),
    ("how many samples to collect", "orders.to_collect", "/admin/diagnostic/worklist",
     {"status": "to_collect", "limit": "100"}, "Ravi Kumar", 1),
    ("reports pending sign off", "lis.pending_signoff", "/admin/lis/results", {"status": "entered", "limit": "100"},
     "Sita", 1),
    ("who is in casualty", "emergency.board", "/admin/emergency/visits", {}, "MLC-7", 1),
    ("surgeries today", "ot.today", "/admin/ot/schedule", {}, "Appendicectomy", 1),
    ("price of x ray chest", "services.prices", "/admin/masters/services", {"active_only": "true"}, "₹450", 1),
    ("ipd outstanding", "ipd_finance.receivables", "/admin/ipd/receivables", {}, "₹12,500", None),
    ("dashboard stats", "stats.summary", "/admin/stats", {"days": "30"}, "42", None),
    ("booking trends", "insights.view", "/admin/insights", {"days": "30"}, "50", None),
    ("opd footfall", "opd.analytics", "/admin/opd/analytics", {"from": TODAY, "to": TODAY}, "18", None),
    ("today's appointments", "appointments.search", "/admin/appointments",
     {"date_basis": "visit", "date_from": TODAY, "date_to": TODAY}, "Ravi Kumar", 3),
    ("bookings pending review", "bookings.pending_review", "/admin/bookings/pending-review", {}, "KR-1001", 1),
    ("payment reconciliation", "payments.reconciliation", "/admin/payments/reconciliation", {"date_str": TODAY},
     "7", None),
    ("payment stats", "payments.stats", "/admin/payments/stats", {"days": "30"}, "20", None),
    ("who is waiting", "opd.queue", "/admin/opd/queue", {"date": TODAY}, "Sita", 2),
    ("list doctors", "doctors.list", "/admin/doctors", {}, "Dr. Rao", 1),
    ("lab test rates", "lab_tests.list", "/admin/lab-tests", {}, "CBC", 1),
    ("list treatments", "treatments.list", "/admin/treatments", {}, "Root canal", 1),
    ("report delivery status", "reports.deliveries", "/admin/reports/deliveries", {"days": "7", "state": "all"},
     "CBC", 1),
    ("failed reports", "reports.queue", "/admin/reports/queue", {}, "Anu", 2),
    ("home visits today", "homecollect.visits", "/admin/home-collection/visits", {"date": TODAY}, "Lakshmi", 1),
    ("new leads", "leads.list", "/admin/leads", {"segment": "all", "days": "30", "limit": "25", "offset": "0"},
     "Arun", 9),
    ("whatsapp usage", "messaging.usage", "/admin/messaging-usage", {}, "120", None),
    ("call log", "voice.calls", "/admin/voice/calls", {"days": "30", "limit": "100"}, "Completed", 1),
    ("live calls", "voice.live", "/admin/voice/live", {}, "In progress", 1),
    ("call analytics", "voice.analytics", "/admin/voice/analytics", {"days": "7"}, "31", None),
    ("call minutes", "voice.usage", "/admin/voice/usage", {}, "55.5", None),
    ("outbound calls", "voice.outbound_list", "/admin/voice/outbound", {}, "Queued", 1),
    ("my notifications", "notifications.list", "/admin/notifications", {"limit": "30"}, "Plan renewed", 1),
    ("audit log", "audit.recent", "/admin/audit-logs", {"limit": "20"}, "CREATE_LEAVE", 1),
    ("ai usage", "ai.usage", "/admin/ai/usage", {}, "₹120", None),
    ("ai budget", "ai.budget", "/admin/ai/budget", {}, "₹500", None),
    ("subscription status", "subscription.view", "/admin/subscription", {}, "Active", None),
    ("data storage", "data.storage", "/admin/data/storage", {}, "1200", None),
    ("connector failed reports", "connectors.failed", "/admin/connectors/failed-reports",
     {"unresolved_only": "true"}, "upload timeout", 1),
    ("find patient ravi", "patients.search", "/admin/patients", {}, "Ravi Kumar", 2),
    ("show catalogue clean up suggestions", "lab_tests.cleanup", "/admin/lab-tests/cleanup-suggestions", {}, "120",
     None),
    ("what did the receptionist learn", "voice.learning_review", "/admin/voice/learning/suggestions", {},
     "skin doctor", 1),
    ("today's collection", "opd.collections", "/admin/opd/collections/summary", {"date": TODAY}, "₹9,000", None),
    ("pending payments", "opd.unpaid", "/admin/opd/invoices", {"status": "issued,partially_paid"}, "₹300", 1),
    ("list holidays", "holidays.list", "/admin/holidays", {}, "Diwali", 1),   # past holidays are not listed
]
EVERYTHING_ON = {"id": CID, "plan": "diagbooking", "account_type": "tenant", "opd_state": "READY",
                 "features": {"kriya_ketu": True, "opd_enabled": True, "ai_receptionist": True, "booking": True,
                              "specialty_treatments": True, "ipd_enabled": True}}

client = TestClient(app)


@pytest.fixture
def reads(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    SEEN.clear()
    with patch.object(assistant_router, "DISPATCH_APP", fake), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=EVERYTHING_ON)):
        yield as_user


def _ask(text, page="dashboard"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": page}}).json()


def test_cases_cover_every_read():
    assert {c[1] for c in CASES} == READS


@pytest.mark.parametrize("text, aid, path, query, shows, total", CASES, ids=[c[1] for c in CASES])
def test_read_returns_the_pages_data_for_the_same_filters(db, reads, text, aid, path, query, shows,  # noqa: F811
                                                          total):
    out = _ask(text)
    assert (out["state"], out["action_id"]) == ("COMPLETED_VERIFIED", aid), out
    assert len(SEEN) == 1 and SEEN[0][0] == path
    assert {k: v for k, v in SEEN[0][1].items() if k in query} == query and SEEN[0][1]["clinic_id"] == CID
    card = out["cards"][0]
    assert card["type"] == "table" and card["page"] == REGISTRY[aid].page
    assert any(shows in cell for row in card["rows"] for cell in row), card
    if total is not None:
        assert card["total"] == total
    stored = json.dumps(db.tables["assistant_runs"], ensure_ascii=False)
    assert PHONE not in stored and "Traceback" not in stored
    assert out["steps"][0]["status"] == "done"


def test_a_feature_that_is_off_is_refused_before_any_call(db, reads):  # noqa: F811
    off = {**EVERYTHING_ON, "features": {"kriya_ketu": True}}
    with patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=off)):
        out = _ask("live calls")
    assert (out["state"], out["error_code"]) == ("REJECTED", "feature_disabled") and SEEN == []


def test_staff_without_the_permission_is_refused_by_name(db, reads):  # noqa: F811
    reads["user"] = _user()
    out = _ask("new leads")
    assert out["state"] == "REJECTED" and "Leads Manage" in out["message"] and SEEN == []


def test_opd_reads_accept_any_permission_the_handler_accepts(db, reads):  # noqa: F811
    user = _user()
    user.permissions = ["OPD_CLINICAL"]
    reads["user"] = user
    out = _ask("who is waiting")
    assert out["state"] == "COMPLETED_VERIFIED" and SEEN[0][0] == "/admin/opd/queue"


def _boom(outcomes, params):
    raise KeyError("x")


def test_a_presenter_error_still_finishes_the_run_with_a_page_link(db, reads):  # noqa: F811
    broken = dataclasses.replace(REGISTRY["doctors.list"], present=_boom)
    with patch.dict(REGISTRY, {"doctors.list": broken}):
        out = _ask("list doctors")
    assert out["state"] == "COMPLETED_VERIFIED" and out["cards"][0]["page"] == "doctors"


# ── handler-checked permissions: behavioural (registry spec §1.1) ────────────

HANDLER_READS = sorted(a for a in READS if a in REGISTRY and REGISTRY[a].perm_source == "handler")


def test_handler_reads_are_the_expected_ones():
    assert set(HANDLER_READS) == {"opd.analytics", "opd.queue", "opd.collections", "opd.unpaid",
                                  "homecollect.visits", "voice.calls", "voice.live",
                                  "voice.analytics", "voice.usage", "voice.outbound_list",
                                  "voice.learning_review", "ipd.census", "ipd.inpatients", "ipd.waiting",
                                  "ipd.discharges", "orders.to_collect", "lis.pending_signoff", "emergency.board",
                                  "services.prices", "ipd_finance.receivables", "ot.today"}


@pytest.mark.parametrize("aid", HANDLER_READS)
def test_handler_permission_is_really_checked_by_the_route(aid):
    spec = REGISTRY[aid]
    app.dependency_overrides[admin.verify_credentials] = lambda: _user()  # staff, no permissions
    try:
        r = TestClient(app).get(spec.endpoint[1], params={**spec.call({})[0].call.query, "clinic_id": CID})
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)
    assert r.status_code == 403 and spec.permission in r.text


# ── patients.search: the page's own filter (name contains / phone contains) ──

@pytest.mark.parametrize("text, term", [
    ("find patient Ravi Kumar", "ravi kumar"), ("search patient 98765", "98765"), ("patient named sita", "sita"),
    ("patient vetuku 43210", "43210"), ("patient dhundo ravi", "ravi"), ("find patient", None),
])
def test_patient_search_term(text, term):
    r = rules.route(text)
    assert (r.kind, r.action_id) == ("action", "patients.search")
    assert r.params.get("q") == term


@pytest.mark.parametrize("q, names", [
    ("ravi", ["Ravi Kumar", "Ravindra"]), ("RAVI KUMAR", ["Ravi Kumar"]), ("22222", ["Ravindra"]),
    ("nobody", []), (None, ["Ravi Kumar", "Sita Devi", "Ravindra"]),
])
def test_patient_search_filters_like_the_page(q, names):
    params = {"q": q} if q else {}
    out = REGISTRY["patients.search"].present([Outcome("ok", 200, None, "", DATA["/admin/patients"])], params)
    card = out["cards"][0]
    shown = [row[0] for row in card.get("rows", [])]
    assert shown == names
    assert PHONE not in json.dumps(out)


# ── 2026-10-26: wording staff actually use (probe of unseen requests) ───────

@pytest.mark.parametrize("text, aid", [
    ("pending lab reports", "reports.queue"), ("price of cbc test", "lab_tests.list"),
    ("no-show rate this month", "stats.summary"), ("aaj ka collection kitna hua", "opd.collections"),
    ("unpaid bills", "opd.unpaid"), ("when is the next holiday", "holidays.list"),
])
def test_everyday_wording_reaches_its_read(db, reads, text, aid):  # noqa: F811
    out = _ask(text)
    assert (out["state"], out["action_id"]) == ("COMPLETED_VERIFIED", aid), out


@pytest.mark.parametrize("text, aid", [("today's collection", "payments.reconciliation"),
                                       ("pending payments", "bookings.pending_review")])
def test_without_opd_the_same_question_reads_online_payments(db, reads, text, aid):  # noqa: F811
    no_opd = {**EVERYTHING_ON, "features": {**EVERYTHING_ON["features"], "opd_enabled": False}}
    with patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=no_opd)):
        out = _ask(text)
    assert (out["state"], out["action_id"]) == ("COMPLETED_VERIFIED", aid), out


@pytest.mark.parametrize("text, term", [
    ("price of cbc test", "cbc"), ("what is the rate of thyroid profile test", "thyroid profile"),
    ("how much does the lipid profile test cost", "lipid profile"), ("cbc test ka rate", "cbc"),
    ("lab test rates", None), ("how many tests do we have", None), ("test dharalu", None),
])
def test_test_price_term(text, term):
    r = rules.route(text)
    assert (r.kind, r.action_id) == ("action", "lab_tests.list"), r
    assert r.params.get("q") == term


def test_test_price_shows_only_matching_tests():
    rows = [{"name": "CBC", "price_paise": 35000}, {"name": "Thyroid Profile (T3, T4, TSH)", "price_paise": 60000},
            {"name": "Lipid Profile", "price_paise": 50000}]
    view = REGISTRY["lab_tests.list"].present
    shown = lambda q: [r[0] for r in view([Outcome("ok", 200, None, "", rows)], {"q": q})["cards"][0].get("rows", [])]
    assert shown("thyroid profile") == ["Thyroid Profile (T3, T4, TSH)"] and shown("cbc") == ["CBC"]
    assert shown(None) == ["CBC", "Thyroid Profile (T3, T4, TSH)", "Lipid Profile"]
    assert "nothing to show" in view([Outcome("ok", 200, None, "", rows)], {"q": "vitamin d"})["message"]


def test_no_show_rate_is_computed_from_the_same_figures():
    out = REGISTRY["stats.summary"].present([Outcome("ok", 200, None, "", {"total_appointments": 40, "no_show": 3})], {})
    assert ["No-show rate", "7.5%"] in out["cards"][0]["rows"]
    out = REGISTRY["stats.summary"].present([Outcome("ok", 200, None, "", {"total_appointments": 0, "no_show": 0})], {})
    assert not any(r[0] == "No-show rate" for r in out["cards"][0]["rows"])   # no division by zero


@pytest.mark.parametrize("text, aid", [
    ("what is dr meena's consultation fee", "doctors.list"), ("how much is the doctor fee", "doctors.list"),
    ("doctor fee kitna hai", "doctors.list"), ("show doctor fees", "doctors.list"),
    ("change dr meena consultation fee to 600", "doctor.fee"), ("update Dr Rao's fee", "doctor.fee"),
])
def test_a_fee_question_reads_the_fees_and_a_fee_change_is_handed_to_the_page(text, aid):
    r = rules.route(text)
    assert r.action_id == aid, r


def test_doctor_list_shows_the_fee():
    out = REGISTRY["doctors.list"].present([Outcome("ok", 200, None, "", DATA["/admin/doctors"])], {})
    card = out["cards"][0]
    assert "Fee" in card["columns"] and "₹500" in card["rows"][0]
