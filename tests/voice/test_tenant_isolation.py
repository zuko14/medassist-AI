"""Session 57: one hospital's receptionist never sees, says or changes another hospital's data.

A fake database holds rows of TWO hospitals for the SAME patient phone. It applies the filters each query really
sets, so a query that forgot `.eq("clinic_id", ...)` returns the other hospital's rows and the test fails. This is
the behavioural twin of tests/test_lint_unscoped_queries.py (which checks the source statically).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.tenancy import TENANT_OWNED_TABLES
from app.voice import knowledge, store
from app.voice.tools import CallContext, KriyaTools

A, B = "clinic-A", "clinic-B"
PHONE = "+919876543210"

ROWS = {
    "appointments": [
        {"id": "a-1", "clinic_id": A, "patient_phone": PHONE, "status": "confirmed", "payment_id": None,
         "doctor_id": "dA", "doctor_name": "Dr. A Rao", "appointment_date": "2099-01-01", "appointment_time": "10:00",
         "booking_ref": "KR-AAAA1", "updated_at": "2099-01-01"},
        {"id": "b-1", "clinic_id": B, "patient_phone": PHONE, "status": "confirmed", "payment_id": None,
         "doctor_id": "dB", "doctor_name": "Dr. B Shah", "appointment_date": "2099-01-01", "appointment_time": "11:00",
         "booking_ref": "KR-BBBB1", "updated_at": "2099-01-01"},
        {"id": "b-2", "clinic_id": B, "patient_phone": PHONE, "status": "refunded", "payment_id": "pay_b",
         "doctor_name": "Dr. B Shah", "appointment_date": "2099-01-02", "appointment_time": "11:00",
         "updated_at": "2099-01-02"},
    ],
    "lab_reports": [
        {"id": "r-a", "clinic_id": A, "patient_phone": PHONE, "report_name": "CBC", "status": "sent", "file_path": "a"},
        {"id": "r-b", "clinic_id": B, "patient_phone": PHONE, "report_name": "MRI", "status": "sent", "file_path": "b"},
    ],
    "voice_lexicon_entries": [
        {"clinic_id": A, "kind": "doctor_alias", "phrase": "rao sir", "canonical": "Dr. A Rao", "is_active": True},
        {"clinic_id": B, "kind": "doctor_alias", "phrase": "shah sir", "canonical": "Dr. B Shah", "is_active": True},
    ],
    "voice_knowledge_entries": [
        {"id": "q-a", "clinic_id": A, "question": "parking", "answer": "A has basement parking", "is_active": True},
        {"id": "q-b", "clinic_id": B, "question": "parking", "answer": "B has valet parking", "is_active": True},
    ],
    "specialty_treatments": [
        {"id": "t-a", "clinic_id": A, "name": "Hair PRP", "is_active": True},
        {"id": "t-b", "clinic_id": B, "name": "LASIK", "is_active": True},
    ],
    "treatment_doctors": [],
    "branches": [{"clinic_id": B, "name": "B Main", "is_active": True}, {"clinic_id": B, "name": "B Two", "is_active": True}],
}


class Q:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload, self.eqs, self.ins = db, table, "select", None, {}, {}

    def select(self, *a, **k):
        return self

    def insert(self, p):
        self.op, self.payload = "insert", p
        return self

    def update(self, p):
        self.op, self.payload = "update", p
        return self

    def eq(self, k, v):
        self.eqs[k] = v
        return self

    def in_(self, k, v):
        self.ins[k] = list(v)
        return self

    def __getattr__(self, name):          # gte / order / limit / range: not filters that matter here
        return lambda *a, **k: self

    def rows(self):
        out = [r for r in ROWS.get(self.table, []) if all(r.get(k) == v for k, v in self.eqs.items())
               and all(r.get(k) in v for k, v in self.ins.items())]
        return out


class FakeDB:
    def __init__(self):
        self.log = []
        self.supabase = SimpleNamespace(table=lambda name: Q(self, name))

    async def sb(self, q):
        self.log.append(q)
        if q.op == "insert":
            return SimpleNamespace(data=[q.payload])
        return SimpleNamespace(data=q.rows())

    def assert_only(self, clinic):
        for q in self.log:
            if q.table not in TENANT_OWNED_TABLES:
                continue
            if q.op == "insert":
                assert q.payload.get("clinic_id") == clinic, (q.table, q.payload)
            else:
                assert q.eqs.get("clinic_id") == clinic, (q.table, q.op, q.eqs)


@pytest.fixture
def db():
    d = FakeDB()
    with patch.multiple("app.voice.tools", sb=d.sb, supabase=d.supabase), \
         patch.multiple("app.voice.knowledge", sb=d.sb, supabase=d.supabase), \
         patch.multiple("app.voice.store", sb=d.sb, supabase=d.supabase):
        yield d


def tools(clinic=A, mode="live"):
    return KriyaTools(CallContext(call_id="call-1", call_ref="CALL-1", clinic={"id": clinic, "name": clinic},
                                  branch_id=None, caller_phone=PHONE, mode=mode))


@pytest.mark.asyncio
async def test_bookings_reports_refunds_are_this_hospitals_only(db):
    t = tools(A)
    appts = await t.upcoming_appointments()
    assert [a["id"] for a in appts] == ["a-1"]                     # B's booking on the same phone: invisible
    assert [r["id"] for r in await t.reports()] == ["r-a"]
    assert await t.refunds() == []                                  # B's refund: invisible
    db.assert_only(A)


@pytest.mark.asyncio
async def test_another_hospitals_booking_can_never_be_cancelled(db):
    res = await tools(A).cancel({"id": "b-1"})                     # an id of hospital B, same phone
    assert res == {"status": "failed", "verified": False}
    assert not [q for q in db.log if q.op in ("update", "delete")]
    db.assert_only(A)


@pytest.mark.asyncio
async def test_lexicon_and_staff_notes_stay_in_the_hospital(db):
    assert [e["phrase"] for e in await store.load_lexicon(A)] == ["rao sir"]
    t = tools(A)
    await t.create_callback("x")
    await t.note_for_staff("y")
    db.assert_only(A)


@pytest.mark.asyncio
async def test_knowledge_answers_come_from_this_hospitals_records_only(db):
    async def doctors(cid, **kw):
        return [{"id": "dA", "name": "Dr. A Rao", "department": "Dermatology"}] if cid == A else \
               [{"id": "dB", "name": "Dr. B Shah", "department": "Ophthalmology"}]
    with patch("app.database.get_doctors", new=doctors), \
         patch("app.database.get_lab_tests", new=AsyncMock(return_value=[])), \
         patch("app.services.faq_engine.answer", new=AsyncMock(return_value=None)), \
         patch("app.services.home_collection.get_settings", new=AsyncMock(return_value={})), \
         patch("app.services.masters.effective_catalog", new=AsyncMock(return_value=[])):
        kb = await knowledge.load({"id": A, "name": "A Hospital"}, None)
    blob = knowledge.facts_text(kb)
    assert "Dr. A Rao" in blob and "Hair PRP" in blob and "basement parking" in blob
    for other in ("B Shah", "LASIK", "valet", "B Main", "Ophthalmology"):
        assert other not in blob, other
    db.assert_only(A)
    assert [q["id"] for q in kb["qa"]] == ["q-a"]                   # only this hospital's own Q&A can be spoken


@pytest.mark.asyncio
async def test_hospital_b_sees_its_own_and_not_a(db):
    t = tools(B)
    assert [a["id"] for a in await t.upcoming_appointments()] == ["b-1"]
    assert [r["id"] for r in await t.refunds()] == ["b-2"]
    db.assert_only(B)
