"""Admin-panel changes must reach the patient correctly and completely.

Covers four defects where what the clinic configured in the admin panel was
not what the patient saw or was charged:

  1. A declared holiday stayed invisible to the bot for up to 5 minutes,
     so patients kept booking a day the clinic had just closed.
  2. Any list longer than 10 rows was silently truncated, so an 11th doctor
     or an imported lab-test catalogue was unreachable in the bot.
  3. Lab-test edits skipped the price validation the create route enforces.
  4. A lab test with no usable price billed the generic booking fee.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 1. Holiday cache invalidation ────────────────────────────────────────────


def test_declaring_a_holiday_evicts_the_cached_negative_answer():
    """The cached '[] = not a holiday' must not survive an admin declaring one."""
    from app.database import _holiday_cache, invalidate_holiday_cache

    _holiday_cache.clear()
    _holiday_cache["clinic-1:2026-09-10"] = {"data": [], "cached_at": 9e9}
    _holiday_cache["clinic-1:2026-09-11"] = {"data": [], "cached_at": 9e9}
    _holiday_cache["clinic-2:2026-09-10"] = {"data": [], "cached_at": 9e9}

    invalidate_holiday_cache("clinic-1", "2026-09-10")

    assert "clinic-1:2026-09-10" not in _holiday_cache
    assert "clinic-1:2026-09-11" in _holiday_cache, "evicted an unrelated date"
    assert "clinic-2:2026-09-10" in _holiday_cache, "evicted another tenant"


def test_holiday_cache_can_be_cleared_per_clinic():
    from app.database import _holiday_cache, invalidate_holiday_cache

    _holiday_cache.clear()
    _holiday_cache["clinic-1:2026-09-10"] = {"data": [], "cached_at": 9e9}
    _holiday_cache["clinic-2:2026-09-10"] = {"data": [], "cached_at": 9e9}

    invalidate_holiday_cache("clinic-1")

    assert "clinic-1:2026-09-10" not in _holiday_cache
    assert "clinic-2:2026-09-10" in _holiday_cache


# ── 2. Interactive-list pagination ───────────────────────────────────────────


def _rows(n):
    return [{"id": f"x_{i}", "title": f"Item {i}", "description": ""} for i in range(n)]


def _cm():
    from app.services.conversation import ConversationManager

    return ConversationManager()


def test_short_list_is_untouched_and_gains_no_extra_tap():
    cm = _cm()
    rows, page = cm._page_rows(_rows(10), 0, "x_more", "en")
    assert len(rows) == 10
    assert page == 0
    assert all(r["id"] != "x_more" for r in rows)


def test_long_list_shows_nine_plus_a_more_row():
    """WhatsApp allows 10 rows; the 10th must be the way to reach item 11."""
    cm = _cm()
    rows, page = cm._page_rows(_rows(25), 0, "x_more", "en")
    assert len(rows) == 10
    assert rows[-1]["id"] == "x_more"
    assert [r["id"] for r in rows[:9]] == [f"x_{i}" for i in range(9)]
    assert "16" in rows[-1]["description"], "should say how many remain"
    assert page == 0


def test_second_page_continues_where_the_first_stopped():
    cm = _cm()
    rows, page = cm._page_rows(_rows(25), 1, "x_more", "en")
    assert page == 1
    assert [r["id"] for r in rows[:9]] == [f"x_{i}" for i in range(9, 18)]
    assert rows[-1]["id"] == "x_more"


def test_final_page_has_no_more_row():
    cm = _cm()
    rows, page = cm._page_rows(_rows(25), 2, "x_more", "en")
    assert page == 2
    assert [r["id"] for r in rows] == [f"x_{i}" for i in range(18, 25)]
    assert all(r["id"] != "x_more" for r in rows)


def test_paging_past_the_end_restarts_rather_than_dead_ending():
    cm = _cm()
    rows, page = cm._page_rows(_rows(25), 99, "x_more", "en")
    assert page == 0
    assert rows[0]["id"] == "x_0"


def test_every_row_of_a_large_catalogue_is_reachable():
    """The whole point: nothing the clinic configured is unreachable."""
    cm = _cm()
    all_rows = _rows(47)
    seen, page, guard = set(), 0, 0
    while guard < 20:
        guard += 1
        rows, page = cm._page_rows(all_rows, page, "x_more", "en")
        more = any(r["id"] == "x_more" for r in rows)
        seen.update(r["id"] for r in rows if r["id"] != "x_more")
        if not more:
            break
        page += 1
    assert seen == {r["id"] for r in all_rows}


def test_more_row_title_fits_whatsapp_limit_in_every_language():
    cm = _cm()
    for lang in ("en", "hi", "te", "xx"):
        rows, _ = cm._page_rows(_rows(25), 0, "x_more", lang)
        assert len(rows[-1]["title"]) <= 24


# ── 3. Lab test price validation parity ──────────────────────────────────────


@pytest.mark.parametrize("bad_price", [0, -1, -500])
def test_lab_test_update_rejects_non_positive_price(bad_price):
    """Create already rejected these; edit must not be a way around it."""
    import pydantic

    from app.routers.admin import LabTestUpdate

    with pytest.raises(pydantic.ValidationError):
        LabTestUpdate(price_rupees=bad_price)


def test_lab_test_update_still_allows_omitting_price():
    from app.routers.admin import LabTestUpdate

    assert LabTestUpdate(name="Renamed").price_rupees is None
    assert LabTestUpdate(price_rupees=450).price_rupees == 450


# ── 4. Lab test billing fails closed ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_lab_test_price_returns_none_not_a_fallback_fee():
    """Billing the generic booking fee charges an amount in no catalogue."""
    from app.services.payment import PaymentService

    service = PaymentService()
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"price_paise": 0}]
    )

    with patch("app.services.payment.supabase", mock_sb):
        assert await service._get_lab_test_fee_paise("clinic-1", "test-1") is None


@pytest.mark.asyncio
async def test_booking_a_lab_test_without_a_price_fails_instead_of_billing():
    from app.services.payment import PaymentService

    service = PaymentService()
    with patch.object(
        service, "_get_lab_test_fee_paise", new_callable=AsyncMock, return_value=None
    ):
        result = await service.create_booking_with_payment(
            clinic_id="clinic-1",
            patient_phone="+919876543210",
            patient_name="Test Patient",
            department="Lab Test",
            doctor_name=None,
            appointment_date="2026-09-10",
            appointment_time=None,
            booking_type="lab_test",
            lab_test_id="test-1",
        )

    assert result["success"] is False
    assert result["reason"] == "lab_test_price_unavailable"


# ── 5. The lab-test booking call site matches the payment signature ──────────


@pytest.mark.asyncio
async def test_lab_test_booking_reaches_the_payment_service():
    """Regression: the call site omitted clinic_id and department.

    Both are required, so every lab-test booking raised TypeError before it
    reached the payment service -- the patient picked a test and a date and got
    the generic failure message. autospec=True makes the mock enforce the real
    signature, so a mismatched call site fails here instead of in production.
    """
    from app.services.conversation import conversation_manager

    clinic = {"id": "clinic-1", "name": "Test Clinic"}
    context = {
        "lab_test_id": "test-1",
        "lab_test_name": "Lipid Profile",
        "branch_id": None,
        "branch_name": None,
    }
    booking = {
        "success": True,
        "booking_id": "b-1",
        "booking_ref": "REF1",
        "payment_link": "https://pay.example/1",
        "amount_paise": 45000,
        "hold_expires_at": "2026-09-10T10:10:00+00:00",
    }

    # autospec enforces the real signature (and yields an AsyncMock for an
    # async target), so a call site that does not bind fails right here.
    with patch(
        "app.services.payment.payment_service.create_booking_with_payment",
        autospec=True,
        return_value=booking,
    ) as mock_create, patch.object(
        conversation_manager.whatsapp, "send_text", new_callable=AsyncMock
    ), patch.object(
        conversation_manager, "update_state", new_callable=AsyncMock
    ):
        await conversation_manager._handle_confirming_collection_date(
            clinic=clinic,
            phone="+919876543210",
            message="",
            intent="",
            context=context,
            patient={"name": "Test Patient"},
            lang="en",
            interactive_data={"id": "labdate_2026-09-10"},
        )

    mock_create.assert_awaited_once()
    kwargs = mock_create.await_args.kwargs
    assert kwargs["clinic_id"] == "clinic-1"
    assert kwargs["department"] == "Lab Test"
    assert kwargs["booking_type"] == "lab_test"
    assert kwargs["lab_test_id"] == "test-1"


# ── 6. Every service call site binds to its real signature ───────────────────


def test_all_service_call_sites_bind_to_their_signatures():
    """Catch the whole bug class, not just the one instance of it.

    The lab-test booking call omitted two required arguments and raised
    TypeError on every attempt, killing the feature in production. Nothing
    caught it: the unit tests called the payment service directly with correct
    arguments and never exercised the conversation's call site.

    This walks every `<service>.<method>(...)` call in app/ and connectors/ and
    binds the literal arguments against the real signature. It only inspects
    calls whose arguments are fully static (no *args / **kwargs), so it cannot
    produce false positives from dynamic dispatch.
    """
    import ast
    import importlib
    import inspect
    import pathlib

    targets = {
        "payment_service": "app.services.payment:payment_service",
        "whatsapp_service": "app.services.whatsapp:whatsapp_service",
        "patient_match_service": "app.services.patient_match:patient_match_service",
        "message_queue": "app.services.message_queue:message_queue",
    }
    resolved = {}
    for alias, path in targets.items():
        module_name, attr = path.split(":")
        resolved[alias] = getattr(importlib.import_module(module_name), attr)

    checked, problems = 0, []
    roots = list(pathlib.Path("app").rglob("*.py")) + list(
        pathlib.Path("connectors").rglob("*.py")
    )
    for path in sorted(roots):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            base = node.func.value
            obj = None
            if isinstance(base, ast.Name) and base.id in resolved:
                obj = resolved[base.id]
            elif isinstance(base, ast.Attribute) and base.attr == "whatsapp":
                obj = resolved["whatsapp_service"]
            if obj is None:
                continue
            method = getattr(obj, node.func.attr, None)
            if not callable(method):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args):
                continue
            if any(kw.arg is None for kw in node.keywords):
                continue

            checked += 1
            try:
                inspect.signature(method).bind(
                    *[object()] * len(node.args),
                    **{kw.arg: object() for kw in node.keywords},
                )
            except TypeError as e:
                problems.append(f"{path}:{node.lineno} .{node.func.attr}() -> {e}")

    assert checked > 100, f"linter matched only {checked} call sites — it stopped working"
    assert not problems, "Call sites that do not match their signature:\n" + "\n".join(
        f"  - {p}" for p in problems
    )


# -- 7. Walk-in delivery is automatic, visible, and recoverable ---------------


@pytest.mark.asyncio
async def test_unverified_delivery_notification_is_raised_once_per_run():
    """The owner sees these in the admin panel instead of them being blocked."""
    from connectors.runner import notify_unverified_deliveries

    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "n1"}]
    )

    with patch("connectors.runner.supabase", mock_sb):
        ok = await notify_unverified_deliveries("clinic-1", 12, "mocdoc")

    assert ok is True
    row = mock_sb.table.return_value.insert.call_args[0][0]
    assert row["clinic_id"] == "clinic-1"
    assert row["admin_id"] is None, "clinic-wide so every admin sees it"
    assert row["is_read"] is False
    assert "12" in row["title"]
    assert len(row["title"]) <= 255


@pytest.mark.asyncio
async def test_no_notification_when_nothing_was_unverified():
    from connectors.runner import notify_unverified_deliveries

    mock_sb = MagicMock()
    with patch("connectors.runner.supabase", mock_sb):
        assert await notify_unverified_deliveries("clinic-1", 0) is False
    mock_sb.table.assert_not_called()


@pytest.mark.asyncio
async def test_notification_failure_never_breaks_a_successful_run():
    """The reports were already delivered; a notification error must not undo that."""
    from connectors.runner import notify_unverified_deliveries

    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception("boom")

    with patch("connectors.runner.supabase", mock_sb):
        assert await notify_unverified_deliveries("clinic-1", 3) is False


@pytest.mark.asyncio
async def test_release_held_walkins_sends_stored_and_reports_the_rest():
    """The backlog flush: the connector will not re-offer already-recorded reports."""
    from app.routers.admin import AdminUser, release_held_walkin_reports

    user = AdminUser("admin", role="super_admin", clinic_id=None, user_id="u1")
    # /admin is single-tenant: a super_admin must name the clinic it acts on.
    scope = "33333333-3333-3333-3333-333333333333"
    rows = [
        {"id": "r1", "file_path": "clinic-1/+91.../a.pdf", "match_source": "moc_doc_only"},
        {"id": "r2", "file_path": "pending_review/xyz", "match_source": "moc_doc_only"},
        {"id": "r3", "file_path": "clinic-1/+91.../c.pdf", "match_source": "moc_doc_only"},
    ]

    # Self-chaining builder: .eq()/.limit() return the chain, so the mock does
    # not break every time a query gains a predicate (this one gained clinic_id).
    chain = MagicMock()
    for m in ("select", "update", "eq", "limit", "order", "in_", "is_"):
        getattr(chain, m).return_value = chain
    chain.execute.return_value = MagicMock(data=rows)

    mock_sb = MagicMock()
    mock_sb.table.return_value = chain

    # Patching the unbound method, so `self` occupies the first slot.
    async def _resend(self, report_id, clinic_id=None, new_phone=None):
        if report_id == "r3":
            raise RuntimeError("WhatsApp rejected the media")
        return {"status": "sent"}

    with patch("app.routers.admin.supabase", mock_sb), patch(
        "app.routers.admin.LabReportService.resend_report", new=_resend
    ), patch("app.routers.admin.log_admin_action", new_callable=AsyncMock):
        result = await release_held_walkin_reports(
            clinic_id=scope, request=None, user=user
        )

    assert result["examined"] == 3
    assert result["sent"] == 1, "only r1 delivers"
    assert result["no_stored_pdf"] == 1, "r2 was never downloaded"
    assert result["failed"] == 1, "r3 failed at send time"
    assert result["errors"][0]["report_id"] == "r3"
