"""Client data portability: legacy patient import, CSV export, record quota,
and the clinic -> owner support inbox (migration 088)."""

import base64
import csv
import io
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routers.admin import AdminUser, verify_credentials
from app.services import client_data as cd

CLINIC = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
client = TestClient(app)

ADMIN = AdminUser(username="dental_admin", role="clinic_admin", clinic_id=CLINIC, user_id="u1")
STAFF = AdminUser(username="front_desk", role="staff", clinic_id=CLINIC, user_id="u2")


@pytest.fixture
def as_user():
    def _set(user):
        app.dependency_overrides[verify_credentials] = lambda: user
    yield _set
    app.dependency_overrides.pop(verify_credentials, None)


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


# ═══════ parsing ═══════


def test_parses_old_software_headers_and_normalises_values():
    raw = _csv(
        "UHID,Patient Name,Mobile No,Sex,DOB,Age,Last Visit,Remarks,Tooth Chart\n"
        "D-1,  Ravi   Kumar ,98765 43210,M,15/08/1985,39 yrs,2024-03-02 00:00:00,RCT 36,36-RCT\n"
    )
    p = cd.parse_patient_csv(raw)
    assert not p.errors and not p.warnings
    r = p.rows[0]
    assert r["full_name"] == "Ravi Kumar"
    assert r["phone"] == "+919876543210"          # same format the bot stores
    assert r["date_of_birth"] == "1985-08-15"      # day-first
    assert r["last_visit_date"] == "2024-03-02"
    assert r["age_years"] == 39
    assert r["external_id"] == "D-1"
    assert r["notes"] == "RCT 36"
    assert r["extra"] == {"Tooth Chart": "36-RCT"}  # unknown column preserved
    assert r["dedupe_key"] == "id:d-1"


def test_dirty_optional_values_warn_but_do_not_block():
    raw = _csv("Name,Phone,DOB,Age\nAsha,12345,31/02/1990,old\n")
    p = cd.parse_patient_csv(raw)
    assert not p.errors
    assert {w["column"] for w in p.warnings} == {"phone", "date_of_birth", "age"}
    r = p.rows[0]
    assert "phone" not in r and "date_of_birth" not in r and "age_years" not in r
    assert r["extra"]["phone (unparsed)"] == "12345"   # nothing silently lost


def test_missing_name_is_a_blocking_error_with_row_number():
    p = cd.parse_patient_csv(_csv("Name,Phone\nRavi,9876543210\n,9876543211\n"))
    assert p.errors == [{"row": 3, "column": "name", "problem": "Patient name is empty."}]


def test_in_file_duplicates_collapse_but_family_phone_keeps_members():
    p = cd.parse_patient_csv(_csv(
        "Name,Phone\nRavi,9876543210\nravi ,9876543210\nSita,9876543210\n"
    ))
    assert [r["full_name"] for r in p.rows] == ["Ravi", "Sita"]
    assert p.duplicates_in_file == 1


def test_future_dates_are_rejected_as_values():
    p = cd.parse_patient_csv(_csv("Name,Last Visit\nRavi,01/01/2999\n"))
    assert "last_visit_date" not in p.rows[0]
    assert p.warnings[0]["column"] == "last_visit_date"


def test_excel_cp1252_and_semicolon_files_are_read():
    raw = "Name;Phone\nJos\xe9;9876543210\n".encode("cp1252")
    p = cd.parse_patient_csv(raw)
    assert p.rows[0]["full_name"] == "José"
    assert p.rows[0]["phone"] == "+919876543210"


@pytest.mark.parametrize("raw, fragment", [
    (b"", "empty"),
    (b"Phone\n9876543210\n", "name column"),
    (b"Name,Phone\n", "no patients"),
    (b"x" * (cd.IMPORT_MAX_FILE_BYTES + 1), "larger than"),
], ids=["empty", "no-name-column", "header-only", "oversize"])
def test_file_level_problems(raw, fragment):
    with pytest.raises(cd.ImportFileError, match=fragment):
        cd.parse_patient_csv(raw)


def test_template_parses_cleanly():
    p = cd.parse_patient_csv(cd.IMPORT_TEMPLATE_CSV.encode())
    assert len(p.rows) == 2 and not p.errors and not p.warnings


# ═══════ quota ═══════


@pytest.mark.parametrize("used, limit, level, percent", [
    (0, 100, "ok", 0),
    (89, 100, "ok", 89),
    (90, 100, "warning", 90),     # the 90% banner threshold
    (99, 100, "warning", 99),
    (100, 100, "full", 100),
    (150, 100, "full", 100),      # owner lowered the limit below usage
    (0, 0, "full", 100),          # limit 0 = importing switched off
])
def test_quota_levels(used, limit, level, percent):
    s = cd.quota_state(used, limit)
    assert (s["level"], s["percent"]) == (level, percent)
    assert s["remaining"] == max(0, limit - used)


@pytest.mark.parametrize("cfg, expected", [
    (None, cd.DEFAULT_PATIENT_RECORDS_LIMIT),
    ({}, cd.DEFAULT_PATIENT_RECORDS_LIMIT),
    ({"patient_records_limit": 0}, 0),
    ({"patient_records_limit": 20000}, 20000),
    ({"patient_records_limit": "9"}, cd.DEFAULT_PATIENT_RECORDS_LIMIT),
    ({"patient_records_limit": True}, cd.DEFAULT_PATIENT_RECORDS_LIMIT),
    ({"patient_records_limit": -5}, cd.DEFAULT_PATIENT_RECORDS_LIMIT),
])
def test_records_limit_from_config(cfg, expected):
    assert cd.records_limit(cfg) == expected


# ═══════ export ═══════


@pytest.mark.parametrize("value, expected", [
    ("=HYPERLINK(\"x\")", "'=HYPERLINK(\"x\")"),
    ("@SUM(A1)", "'@SUM(A1)"),
    ("-2+3", "'-2+3"),
    ("  =cmd", "'  =cmd"),
    ("+919876543210", "+919876543210"),  # a phone is a number, not a formula
    ("Ravi", "Ravi"),
    (None, ""),
    (True, "Yes"),
])
def test_csv_cell_neutralises_formulas(value, expected):
    assert cd.csv_cell(value) == expected


def test_build_csv_has_bom_headers_and_safe_cells():
    out = cd.build_csv("appointments", [{
        "booking_ref": "KR-1", "patient_name": "=evil()", "patient_phone": "+919876543210",
        "appointment_date": "2026-09-01", "appointment_time": "10:30:00", "status": "confirmed",
        "amount_paise": 50000,
    }])
    assert out.startswith("﻿")
    rows = list(csv.reader(io.StringIO(out.lstrip("﻿"))))
    assert rows[0][0] == "Booking Ref"
    rec = dict(zip(rows[0], rows[1]))
    assert rec["Patient Name"] == "'=evil()"
    assert rec["Time"] == "10:30" and rec["Amount (Rs)"] == "500.00"


def test_export_range_rules():
    v = cd.validate_export_range
    v("patient_records", None, None)                       # no range needed
    v("appointments", date(2025, 1, 1), date(2025, 12, 31))
    with pytest.raises(ValueError, match="From and To"):
        v("patients", None, date(2025, 1, 1))
    with pytest.raises(ValueError, match="on or before"):
        v("appointments", date(2025, 2, 1), date(2025, 1, 1))
    with pytest.raises(ValueError, match="366"):
        v("appointments", date(2024, 1, 1), date(2025, 12, 31))
    with pytest.raises(ValueError, match="Unknown dataset"):
        v("doctors", date(2025, 1, 1), date(2025, 1, 2))


def test_ist_day_bounds_are_utc_z_strings():
    lo, hi = cd.ist_day_bounds_utc(date(2026, 9, 1), date(2026, 9, 30))
    assert (lo, hi) == ("2026-08-31T18:30:00Z", "2026-09-30T18:30:00Z")
    assert "+" not in lo + hi


def test_erased_shells_are_not_exported():
    assert cd._is_erased({"patient_phone": "[REDACTED]"})
    assert not cd._is_erased({"patient_phone": "+919876543210"})


# ═══════ admin routes ═══════


def test_staff_cannot_import_export_or_message(as_user):
    as_user(STAFF)
    assert client.get("/admin/data/storage").status_code == 403
    assert client.get("/admin/data/export?dataset=patient_records").status_code == 403
    assert client.post("/admin/data/patient-records/import",
                       files={"file": ("p.csv", b"Name\nRavi\n")}).status_code == 403
    assert client.post("/admin/support/messages",
                       json={"category": "other", "subject": "x", "message": "y"}).status_code == 403


def test_admin_cannot_reach_another_clinic(as_user):
    as_user(ADMIN)
    assert client.get(f"/admin/data/storage?clinic_id={OTHER}").status_code == 403
    assert client.get(f"/admin/data/export?dataset=patient_records&clinic_id={OTHER}").status_code == 403
    assert client.get(f"/admin/support/messages?clinic_id={OTHER}").status_code == 403


def _import_mocks(used=0, limit=100, existing=()):
    return (
        patch.object(cd, "fetch_clinic_config", AsyncMock(return_value={"patient_records_limit": limit})),
        patch.object(cd, "count_records", AsyncMock(return_value=used)),
        patch.object(cd, "fetch_existing_keys", AsyncMock(return_value=set(existing))),
        patch.object(cd, "import_records", AsyncMock(return_value={"id": "b1", "inserted_count": 2})),
        patch.object(cd, "clinic_quota", AsyncMock(return_value=cd.quota_state(used + 2, limit))),
        patch("app.routers.admin.log_admin_action", new_callable=AsyncMock),
    )


def _run(mocks, **kw):
    with mocks[0], mocks[1], mocks[2], mocks[3] as imp, mocks[4], mocks[5] as audit:
        res = client.post("/admin/data/patient-records/import", **kw)
        return res, imp, audit


def test_import_happy_path_writes_only_new_rows_and_audits(as_user):
    as_user(ADMIN)
    body = b"Name,Phone\nRavi,9876543210\nSita,9876543211\nOld,9876543212\n"
    res, imp, audit = _run(_import_mocks(existing={"pn:+919876543212|old"}),
                           files={"file": ("legacy.csv", body)})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["new_rows"] == 2 and data["already_imported"] == 1
    clinic_arg, _, _, _, new_rows, skipped = imp.await_args.args
    assert clinic_arg == CLINIC
    assert [r["full_name"] for r in new_rows] == ["Ravi", "Sita"] and skipped == 1
    assert audit.await_args.kwargs["action"] == "PATIENT_RECORDS_IMPORT"


def test_import_over_quota_is_rejected_with_nothing_written(as_user):
    as_user(ADMIN)
    body = b"Name\nA1\nA2\nA3\n"
    res, imp, _ = _run(_import_mocks(used=98, limit=100), files={"file": ("p.csv", body)})
    assert res.status_code == 409
    assert "only 2 of your 100" in res.json()["detail"]
    imp.assert_not_awaited()


def test_import_with_row_errors_returns_422_and_writes_nothing(as_user):
    as_user(ADMIN)
    res, imp, _ = _run(_import_mocks(), files={"file": ("p.csv", b"Name,Phone\n,9876543210\n")})
    assert res.status_code == 422
    assert res.json()["errors"][0]["row"] == 2
    imp.assert_not_awaited()


def test_dry_run_checks_quota_but_writes_nothing(as_user):
    as_user(ADMIN)
    res, imp, _ = _run(_import_mocks(), files={"file": ("p.csv", b"Name\nRavi\n")}, data={"dry_run": "true"})
    assert res.status_code == 200 and res.json()["dry_run"] is True
    assert res.json()["storage_after_import"]["used"] == 1
    imp.assert_not_awaited()


def test_import_write_failure_is_reported_as_rolled_back(as_user):
    as_user(ADMIN)
    mocks = list(_import_mocks())
    mocks[3] = patch.object(cd, "import_records", AsyncMock(side_effect=RuntimeError("db down")))
    res, _, _ = _run(mocks, files={"file": ("p.csv", b"Name\nRavi\n")})
    assert res.status_code == 500
    assert "rolled back" in res.json()["detail"]
    assert "db down" not in res.text  # no internals leaked


def test_export_rejects_bad_range_before_touching_db(as_user):
    as_user(ADMIN)
    with patch.object(cd, "fetch_export_rows", AsyncMock()) as fetch:
        res = client.get("/admin/data/export?dataset=appointments&date_from=2025-02-01&date_to=2025-01-01")
    assert res.status_code == 400
    fetch.assert_not_awaited()


def test_export_returns_csv_scoped_to_callers_clinic(as_user):
    as_user(ADMIN)
    rows = [{"name": "Ravi", "phone": "+919876543210", "opted_in": True, "visit_count": 2}]
    with patch.object(cd, "fetch_export_rows", AsyncMock(return_value=rows)) as fetch, \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock) as audit:
        res = client.get("/admin/data/export?dataset=patients&date_from=2026-01-01&date_to=2026-01-31")
    assert res.status_code == 200
    assert fetch.await_args.args[:2] == (CLINIC, "patients")
    assert 'filename="patients_2026-01-01_to_2026-01-31.csv"' in res.headers["content-disposition"]
    assert "Ravi,+919876543210,,Yes,2" in res.content.decode("utf-8-sig")
    assert audit.await_args.kwargs["details"]["rows"] == 1


def test_export_too_many_rows_asks_for_shorter_range(as_user):
    as_user(ADMIN)
    with patch.object(cd, "fetch_export_rows", AsyncMock(side_effect=OverflowError)):
        res = client.get("/admin/data/export?dataset=patient_records")
    assert res.status_code == 413


def test_delete_import_validates_id(as_user):
    as_user(ADMIN)
    assert client.delete("/admin/data/imports/not-a-uuid").status_code == 400


@pytest.mark.parametrize("payload", [
    {"category": "gossip", "subject": "s", "message": "m"},
    {"category": "other", "subject": "   ", "message": "m"},
    {"category": "other", "subject": "s", "message": "m" * 4001},
])
def test_support_message_validation(as_user, payload):
    as_user(ADMIN)
    assert client.post("/admin/support/messages", json=payload).status_code == 422


# ═══════ owner routes ═══════


def owner_auth():
    creds = f"{settings.owner_username}:{settings.owner_password}"
    return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}


@pytest.mark.parametrize("method, path", [
    ("GET", "/platform/data-storage"),
    ("PATCH", f"/platform/clinics/{CLINIC}/data-storage"),
    ("GET", "/platform/support-messages"),
    ("PATCH", "/platform/support-messages/33333333-3333-3333-3333-333333333333"),
])
def test_owner_routes_require_owner_auth(method, path):
    assert client.request(method, path, json={}).status_code == 401


def test_owner_sets_limit_and_addon_in_clinic_config():
    written = {}

    def table(name):
        obj = MagicMock()
        for m in ("select", "eq", "limit"):
            getattr(obj, m).return_value = obj
        obj._rows = [{"id": CLINIC, "name": "Smile Dental", "config": {"ai_budget_paise": 1}}]

        def _update(payload):
            written.update(payload)
            up = MagicMock()
            up.eq.return_value = up
            up._rows = [{}]
            return up
        obj.update.side_effect = _update
        return obj

    async def fake_sb(builder):
        return MagicMock(data=getattr(builder, "_rows", []))

    with patch("app.routers.platform.supabase") as sb_mod, \
         patch("app.routers.platform.sb", side_effect=fake_sb), \
         patch("app.routers.platform.log_admin_action", new_callable=AsyncMock), \
         patch.object(cd, "clinic_quota", AsyncMock(return_value=cd.quota_state(0, 20000))):
        sb_mod.table.side_effect = table
        res = client.patch(f"/platform/clinics/{CLINIC}/data-storage", headers=owner_auth(),
                           json={"records_limit": 20000, "addon_rupees": 499})
    assert res.status_code == 200, res.text
    assert written["config"] == {"ai_budget_paise": 1, "patient_records_limit": 20000,
                                 "data_storage_addon_paise": 49900}  # other keys preserved


def test_owner_limit_bounds():
    res = client.patch(f"/platform/clinics/{CLINIC}/data-storage", headers=owner_auth(),
                       json={"records_limit": -1})
    assert res.status_code == 422


# ═══════ service DB orchestration (recording fake PostgREST builder) ═══════


class _Q:
    """Records every chained call; `sb()` resolves it through the test's handler."""

    def __init__(self, table, log):
        self.table, self.calls, self.log = table, [], log

    def __getattr__(self, name):
        def _m(*a, **k):
            self.calls.append((name, a, k))
            return self
        return _m


def _fake_db(handler):
    log = []
    sup = MagicMock()
    sup.table.side_effect = lambda t: (log.append(_Q(t, log)) or log[-1])

    async def fake_sb(q):
        return handler(q)
    return sup, fake_sb, log


@pytest.mark.asyncio
async def test_import_records_rolls_back_whole_batch_when_a_chunk_fails(monkeypatch):
    monkeypatch.setattr(cd, "INSERT_CHUNK", 2)
    upserts = []

    def handler(q):
        op = q.calls[0][0]
        if q.table == "patient_import_batches" and op == "insert":
            return MagicMock(data=[{"id": "batch-1"}])
        if q.table == "patient_records":
            upserts.append(q.calls[0][1][0])
            if len(upserts) == 2:
                raise RuntimeError("connection reset")
            return MagicMock(data=q.calls[0][1][0])
        if op == "delete":
            return MagicMock(data=[{"id": "batch-1"}])
        raise AssertionError(q.table)

    sup, fake_sb, log = _fake_db(handler)
    monkeypatch.setattr(cd, "supabase", sup)
    monkeypatch.setattr(cd, "sb", fake_sb)
    parsed = cd.parse_patient_csv(b"Name,Phone\nA,9876543210\nB,\nC,9876543212\n")

    with pytest.raises(RuntimeError):
        await cd.import_records(CLINIC, "admin", "f.csv", parsed, parsed.rows, 0)

    # Every row carries the same keys (NULL where absent) and the clinic id.
    first = upserts[0]
    assert first[0].keys() == first[1].keys() and first[1]["phone"] is None
    assert all(r["clinic_id"] == CLINIC and r["import_batch_id"] == "batch-1" for r in first)
    # The batch (and by FK cascade its records) was removed, scoped to the clinic.
    delete_q = [q for q in log if q.table == "patient_import_batches" and q.calls[0][0] == "delete"][0]
    assert ("eq", ("clinic_id", CLINIC), {}) in delete_q.calls
    assert ("eq", ("id", "batch-1"), {}) in delete_q.calls


@pytest.mark.asyncio
async def test_fetch_export_rows_paginates_scopes_and_drops_erased(monkeypatch):
    monkeypatch.setattr(cd, "EXPORT_PAGE", 2)
    pages = [[{"phone": "+911"}, {"phone": "[REDACTED]"}], [{"phone": "+913"}]]
    sup, fake_sb, log = _fake_db(lambda q: MagicMock(data=pages[len([x for x in log if x.calls]) - 1]))
    monkeypatch.setattr(cd, "supabase", sup)
    monkeypatch.setattr(cd, "sb", fake_sb)

    rows = await cd.fetch_export_rows(CLINIC, "patients", date(2026, 9, 1), date(2026, 9, 30))
    assert [r["phone"] for r in rows] == ["+911", "+913"]
    first = log[0].calls
    assert ("eq", ("clinic_id", CLINIC), {}) in first
    assert ("gte", ("created_at", "2026-08-31T18:30:00Z"), {}) in first
    assert ("lt", ("created_at", "2026-09-30T18:30:00Z"), {}) in first
    assert ("range", (0, 1), {}) in first and ("range", (2, 3), {}) in log[1].calls


@pytest.mark.asyncio
async def test_fetch_export_rows_caps_total(monkeypatch):
    monkeypatch.setattr(cd, "EXPORT_PAGE", 2)
    monkeypatch.setattr(cd, "EXPORT_MAX_ROWS", 3)
    sup, fake_sb, _ = _fake_db(lambda q: MagicMock(data=[{"phone": "+91"}, {"phone": "+92"}]))
    monkeypatch.setattr(cd, "supabase", sup)
    monkeypatch.setattr(cd, "sb", fake_sb)
    with pytest.raises(OverflowError):
        await cd.fetch_export_rows(CLINIC, "patient_records", None, None)


@pytest.mark.asyncio
async def test_service_refuses_invalid_scope():
    for bad in ("default", "", None, "all"):
        with pytest.raises(ValueError):
            await cd.count_records(bad)


# ═══════ billing: storage add-on reaches the invoice ═══════


@patch("app.routers.platform.log_admin_action", new_callable=AsyncMock)
@patch("app.routers.platform._fetch_clinic_branch_counts", new_callable=AsyncMock)
@patch("app.services.platform_finance.fetch_invoices", new_callable=AsyncMock)
@patch("app.services.platform_finance.fetch_billing_rates", new_callable=AsyncMock)
@patch("app.services.message_accounting._get_plan_tiers", new_callable=AsyncMock)
@patch("app.routers.platform.supabase")
def test_invoice_adds_storage_addon_only_where_set(
    mock_supabase, mock_tiers, mock_rates, mock_invoices, mock_branches, _log
):
    inserted = {}

    def table_router(name):
        m = MagicMock()
        if name == "clinics":
            m.select.return_value.execute.return_value.data = [
                {"id": "dental", "name": "Smile", "plan": "p", "is_active": True,
                 "config": {"data_storage_addon_paise": 49_900}},
                {"id": "plain", "name": "Plain", "plan": "p", "is_active": True, "config": {}},
            ]
        elif name == "platform_invoices":
            def _insert(rows):
                inserted["rows"] = rows
                r = MagicMock()
                r.execute.return_value.data = rows
                return r
            m.insert.side_effect = _insert
        return m

    mock_supabase.table.side_effect = table_router
    mock_tiers.return_value = {"p": {"monthly_price_paise": 500_000}}
    mock_rates.return_value = {}
    mock_branches.return_value = {}
    mock_invoices.return_value = []

    res = client.post("/platform/finance/invoices/generate", headers=owner_auth(), json={"month": "2026-10"})
    assert res.status_code == 200, res.text
    rows = {r["clinic_id"]: r for r in inserted["rows"]}
    assert rows["dental"]["amount_paise"] == 549_900
    assert rows["dental"]["storage_addon_paise"] == 49_900
    assert rows["plain"]["amount_paise"] == 500_000
    assert "storage_addon_paise" not in rows["plain"]  # byte-identical to the pre-088 row


# ═══════ notifications: owner reply -> clinic bell, owner unread badge ═══════


def _support_patch(body, *, notif_fails=False):
    """PATCH one support message as the owner; returns (response, notification inserts)."""
    notifs = []
    msg_row = {"id": "33333333-3333-3333-3333-333333333333", "clinic_id": CLINIC,
               "subject": "Need more storage", "status": "in_progress"}

    def table(name):
        obj = MagicMock()
        for m in ("eq", "select", "limit"):
            getattr(obj, m).return_value = obj
        if name == "support_messages":
            def _update(payload):
                obj._rows = [{**msg_row, **payload}]
                return obj
            obj.update.side_effect = _update
        elif name == "admin_notifications":
            def _insert(payload):
                if notif_fails:
                    raise RuntimeError("insert failed")
                notifs.append(payload)
                obj._rows = [payload]
                return obj
            obj.insert.side_effect = _insert
        return obj

    async def fake_sb(builder):
        return MagicMock(data=getattr(builder, "_rows", []))

    with patch("app.routers.platform.supabase") as sb_mod, \
         patch("app.routers.platform.sb", side_effect=fake_sb), \
         patch("app.routers.platform.log_admin_action", new_callable=AsyncMock):
        sb_mod.table.side_effect = table
        res = client.patch(f"/platform/support-messages/{msg_row['id']}", headers=owner_auth(), json=body)
    return res, notifs


def test_owner_reply_notifies_the_clinic_bell():
    res, notifs = _support_patch({"reply": "Done - limit raised to 20,000."})
    assert res.status_code == 200, res.text
    assert len(notifs) == 1
    n = notifs[0]
    assert n["clinic_id"] == CLINIC and n["admin_id"] is None and n["is_read"] is False
    assert n["title"].startswith("Kriya Support")  # the prefix admin/index.html keys on
    assert "Need more storage" in n["title"] and "20,000" in n["message"]


def test_owner_resolving_without_reply_notifies_status():
    res, notifs = _support_patch({"status": "resolved"})
    assert res.status_code == 200
    assert notifs[0]["title"] == "Kriya Support: request resolved"


def test_owner_merely_opening_a_message_does_not_ping_the_clinic():
    res, notifs = _support_patch({})
    assert res.status_code == 200
    assert notifs == []


def test_notification_failure_never_fails_the_reply():
    res, _ = _support_patch({"reply": "ok"}, notif_fails=True)
    assert res.status_code == 200
    assert res.json()["message"]["owner_reply"] == "ok"


def test_owner_unread_count():
    q = MagicMock()
    for m in ("select", "is_", "limit"):
        getattr(q, m).return_value = q

    async def fake_sb(builder):
        return MagicMock(data=[], count=3)

    with patch("app.routers.platform.supabase") as sb_mod, \
         patch("app.routers.platform.sb", side_effect=fake_sb):
        sb_mod.table.return_value = q
        res = client.get("/platform/support-messages/unread-count", headers=owner_auth())
    assert res.status_code == 200 and res.json()["unread"] == 3
    q.is_.assert_called_with("owner_seen_at", "null")
    assert client.get("/platform/support-messages/unread-count").status_code == 401


# ═══════ follow-up ON/OFF switch ═══════


def test_followup_switch_saves_only_the_flag(as_user):
    """The switch sends {followup_enabled} alone; every other setting survives."""
    as_user(ADMIN)
    cfg = {"followup_enabled": True, "followup_days": 3, "followup_message": "Get well soon",
           "followup_message_template_name": "tpl", "address": "12 MG Road"}
    written = {}

    def _update(payload):
        written.update(payload)
        chain = MagicMock()
        chain.eq.return_value = chain
        chain.execute.return_value = MagicMock(data=[{"name": "Smile", "whatsapp_number": "+91"}])
        return chain

    with patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock,
               return_value={"id": CLINIC, "config": dict(cfg)}), \
         patch("app.routers.admin.supabase") as sup, \
         patch("app.routers.admin.invalidate_tenant_cache"), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock) as audit:
        sup.table.return_value.update.side_effect = _update
        res = client.put("/admin/profile", json={"followup_enabled": False})
    assert res.status_code == 200, res.text
    assert written["config"] == {**cfg, "followup_enabled": False}
    assert "name" not in written  # the hospital name is untouched
    assert audit.await_args.kwargs["details"]["followup_enabled"] is False


def test_staff_cannot_flip_followups(as_user):
    as_user(STAFF)
    assert client.put("/admin/profile", json={"followup_enabled": False}).status_code == 403
