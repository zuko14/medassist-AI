# Security & Reliability Audit Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 11 findings from the 2026-08-09 adversarial security/reliability/payment audit of the MediAssist AI hospital-bot platform, without changing any public interface behavior for legitimate callers.

**Architecture:** Each finding is fixed at its root cause in the existing file that owns it — no new services, no new abstractions. Two findings (#2 token race, #5 broken payment link) require a new Postgres migration to add a real database-level guarantee, matching the existing `idx_unique_active_slot` pattern already used for slot double-booking. All fixes preserve existing function signatures except where a missing parameter (`clinic_id`) IS the bug (#1), in which case callers are updated in the same task.

**Tech Stack:** Python 3.11, FastAPI, Supabase (PostgreSQL) via `supabase-py` query builder, `pytest` + `pytest-asyncio`, `unittest.mock` (no live network/DB in tests — this repo mocks `app.services.payment.supabase` / `app.routers.admin.supabase` directly, see existing tests below).

## Global Constraints

- Every task must keep `pytest` green for the FULL suite (currently 268 tests), not just its own new test — run `python -m pytest tests/ -q` at the end of every task.
- No new third-party dependencies. Everything here is solvable with the existing `supabase-py`, `httpx`, `hmac`/`hashlib` stdlib, and this repo's own `PersistentRateLimiter`/`AdminUser`/`enforce_clinic_access` primitives.
- Match this repo's existing test-mocking convention exactly: patch `app.services.payment.supabase` (not a fixture), build `MagicMock()` chains for `.table().select().eq().execute()`, use `AsyncMock` for coroutine methods, `@pytest.mark.asyncio` on every async test.
- New Postgres migrations are numbered sequentially starting at `021` and follow the existing header comment style seen in `migrations/019_appointment_queue_tokens.sql` / `migrations/008_payments.sql` ("Run in Supabase SQL Editor", a `-- Verify` `SELECT` at the bottom).
- Every commit is a single `git commit` per task (not per step) once all of that task's steps pass, using this repo's existing terse commit style (`fix(payment): ...`, `fix(admin): ...`, see `git log`).
- Do not touch `app/integrations/callmedex/*` — explicitly out of scope (separate audit).

---

## Task 1: Fix cross-tenant BOLA in admin booking confirm/reject

**Severity:** CRITICAL. Any `clinic_admin` can confirm/reject another clinic's booking today.

**Files:**
- Modify: `app/services/payment.py:768-845` (`admin_confirm_booking`, `admin_reject_booking`)
- Modify: `app/routers/admin.py:1007-1048` (`admin_confirm_booking` route, `admin_reject_booking` route)
- Test: `tests/test_payment.py` (new test class `TestAdminBookingScoping`)
- Test: `tests/test_admin_staff_identity.py` (extend — audit logging on these two routes)

**Interfaces:**
- Consumes: `enforce_clinic_access(user: AdminUser, requested_clinic_id: str = "default") -> str` (already defined in `app/routers/admin.py:202-223`, unchanged), `log_admin_action(...)` (already defined `app/routers/admin.py:76-107`, unchanged).
- Produces: `PaymentService.admin_confirm_booking(booking_id: str, clinic_id: str = "default", admin_notes: str = "") -> dict` — **signature changed**, `clinic_id` inserted as 2nd positional param. `PaymentService.admin_reject_booking(booking_id: str, clinic_id: str = "default", admin_notes: str = "") -> dict` — same change. Both keep returning `{"success": bool, "reason": str}` on failure / `{"success": True}` on success. `404` now returned (was `400`) when `reason == "booking_not_found"`.

- [ ] **Step 1: Write the failing tests in `tests/test_payment.py`**

Add this class after `TestRefundFlow` (before `TestHoldExpiry`):

```python
class TestAdminBookingScoping:
    """Regression tests for the cross-tenant BOLA fix (Finding #1)."""

    @pytest.mark.asyncio
    async def test_admin_confirm_booking_rejects_cross_tenant_id(self):
        """A booking belonging to clinic B must not be confirmable by a
        request scoped to clinic A — the clinic_id filter must exclude it."""
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_select = MagicMock()
            mock_table.select.return_value = mock_select
            mock_eq_id = MagicMock()
            mock_select.eq.return_value = mock_eq_id
            # .eq("id", booking_id).eq("clinic_id", "clinic-A") -> no rows,
            # because this booking actually belongs to clinic-B
            mock_eq_id.eq.return_value.execute.return_value = MagicMock(data=[])

            result = await service.admin_confirm_booking(
                "booking-owned-by-clinic-b", clinic_id="clinic-A"
            )

        assert result["success"] is False
        assert result["reason"] == "booking_not_found"

    @pytest.mark.asyncio
    async def test_admin_confirm_booking_succeeds_for_own_clinic(self):
        """Same booking IS confirmable when clinic_id matches."""
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_booking = {
            "id": "booking-1",
            "clinic_id": "clinic-A",
            "status": "pending_review",
            "patient_phone": "+919876543210",
        }

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_increment_patient_visit_count", new_callable=AsyncMock
        ), patch.object(
            service, "_notify_payment_confirmed", new_callable=AsyncMock
        ), patch.object(
            service, "_log_payment_event"
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_select = MagicMock()
            mock_table.select.return_value = mock_select
            mock_eq_id = MagicMock()
            mock_select.eq.return_value = mock_eq_id
            mock_eq_id.eq.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[mock_booking])
            )

            result = await service.admin_confirm_booking(
                "booking-1", clinic_id="clinic-A"
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_admin_reject_booking_rejects_cross_tenant_id(self):
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_select = MagicMock()
            mock_table.select.return_value = mock_select
            mock_eq_id = MagicMock()
            mock_select.eq.return_value = mock_eq_id
            mock_eq_id.eq.return_value.execute.return_value = MagicMock(data=[])

            result = await service.admin_reject_booking(
                "booking-owned-by-clinic-b", clinic_id="clinic-A"
            )

        assert result["success"] is False
        assert result["reason"] == "booking_not_found"

    @pytest.mark.asyncio
    async def test_admin_confirm_booking_default_clinic_id_is_unscoped(self):
        """clinic_id='default' (super_admin path) must NOT add a clinic filter —
        preserves existing super_admin cross-clinic behavior."""
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_booking = {
            "id": "booking-1",
            "clinic_id": "clinic-A",
            "status": "pending_review",
            "patient_phone": "+919876543210",
        }

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_increment_patient_visit_count", new_callable=AsyncMock
        ), patch.object(
            service, "_notify_payment_confirmed", new_callable=AsyncMock
        ), patch.object(
            service, "_log_payment_event"
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_select = MagicMock()
            mock_table.select.return_value = mock_select
            # Only ONE .eq() call expected: .eq("id", booking_id) — no clinic filter
            mock_select.eq.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[mock_booking])
            )

            result = await service.admin_confirm_booking(
                "booking-1", clinic_id="default"
            )

        assert result["success"] is True
        mock_select.eq.assert_called_once_with("id", "booking-1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_payment.py::TestAdminBookingScoping -v`
Expected: FAIL — `TypeError: admin_confirm_booking() got an unexpected keyword argument 'clinic_id'` (current signature is `(self, booking_id, admin_notes="")`).

- [ ] **Step 3: Fix `app/services/payment.py` — add clinic_id scoping**

Replace lines 768-845 (the two methods) with:

```python
    async def admin_confirm_booking(
        self, booking_id: str, clinic_id: str = "default", admin_notes: str = ""
    ) -> dict:
        """Manually confirm a pending_review booking (admin override), scoped to clinic_id."""
        query = supabase.table("appointments").select("*").eq("id", booking_id)
        if clinic_id and clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        booking_result = query.execute()

        if not booking_result.data:
            return {"success": False, "reason": "booking_not_found"}

        booking = booking_result.data[0]

        if booking["status"] != "pending_review":
            return {
                "success": False,
                "reason": f"can_only_confirm_pending_review_not_{booking['status']}",
            }

        supabase.table("appointments").update({"status": "confirmed"}).eq(
            "id", booking_id
        ).execute()

        self._log_payment_event(
            booking_id,
            "manual_confirm",
            {
                "admin_notes": admin_notes,
                "previous_status": booking["status"],
            },
        )

        logger.info(f"Admin manually confirmed booking {booking_id}")
        await self._increment_patient_visit_count(
            booking.get("clinic_id"), booking.get("patient_phone")
        )
        await self._notify_payment_confirmed(booking)
        return {"success": True}

    async def admin_reject_booking(
        self, booking_id: str, clinic_id: str = "default", admin_notes: str = ""
    ) -> dict:
        """Manually reject a pending_review booking + initiate refund, scoped to clinic_id."""
        query = supabase.table("appointments").select("*").eq("id", booking_id)
        if clinic_id and clinic_id != "default":
            query = query.eq("clinic_id", clinic_id)
        booking_result = query.execute()

        if not booking_result.data:
            return {"success": False, "reason": "booking_not_found"}

        booking = booking_result.data[0]

        if booking["status"] != "pending_review":
            return {
                "success": False,
                "reason": f"can_only_reject_pending_review_not_{booking['status']}",
            }

        supabase.table("appointments").update({"status": "cancelled"}).eq(
            "id", booking_id
        ).execute()

        self._log_payment_event(
            booking_id,
            "manual_reject",
            {
                "admin_notes": admin_notes,
            },
        )

        if booking.get("payment_id"):
            await self.initiate_refund(
                booking_id, reason=f"Admin rejected: {admin_notes}"
            )

        return {"success": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_payment.py::TestAdminBookingScoping -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Fix `app/routers/admin.py` — enforce clinic access and add audit logging**

Replace lines 1007-1048 with:

```python
@router.post("/bookings/{booking_id}/confirm")
async def admin_confirm_booking(
    booking_id: str,
    clinic_id: str = "default",
    body: dict = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Manually confirm a pending_review booking (admin override)."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        from app.services.payment import payment_service

        admin_notes = (body or {}).get("admin_notes", f"Confirmed by admin: {user}")
        result = await payment_service.admin_confirm_booking(
            booking_id, clinic_id=effective_clinic_id, admin_notes=admin_notes
        )
        if not result["success"]:
            status_code = 404 if result.get("reason") == "booking_not_found" else 400
            raise HTTPException(status_code=status_code, detail=result.get("reason", "Failed"))
        await log_admin_action(
            user=user,
            action="BOOKING_MANUAL_CONFIRM",
            resource_type="appointment",
            resource_id=booking_id,
            details={"admin_notes": admin_notes},
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin confirm booking error: {e}")
        raise HTTPException(status_code=500, detail="Internal error")


@router.post("/bookings/{booking_id}/reject")
async def admin_reject_booking(
    booking_id: str,
    clinic_id: str = "default",
    body: dict = None,
    user: AdminUser = Depends(verify_credentials),
):
    """Manually reject a pending_review booking + initiate refund."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    try:
        from app.services.payment import payment_service

        admin_notes = (body or {}).get("admin_notes", f"Rejected by admin: {user}")
        result = await payment_service.admin_reject_booking(
            booking_id, clinic_id=effective_clinic_id, admin_notes=admin_notes
        )
        if not result["success"]:
            status_code = 404 if result.get("reason") == "booking_not_found" else 400
            raise HTTPException(status_code=status_code, detail=result.get("reason", "Failed"))
        await log_admin_action(
            user=user,
            action="BOOKING_MANUAL_REJECT",
            resource_type="appointment",
            resource_id=booking_id,
            details={"admin_notes": admin_notes},
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin reject booking error: {e}")
        raise HTTPException(status_code=500, detail="Internal error")
```

Note: `raise HTTPException(status_code=500, detail=str(e))` was changed to a generic `"Internal error"` message — the original leaked raw exception text to the HTTP response body, a minor info-disclosure smell fixed opportunistically since this exact block is already being touched.

- [ ] **Step 6: Add route-level BOLA regression tests to `tests/test_admin_staff_identity.py`**

Append at the end of the file:

```python
@pytest.mark.asyncio
async def test_admin_confirm_booking_route_scopes_by_clinic():
    """A clinic_admin scoped to clinic-A cannot confirm a clinic-B booking
    through the HTTP route — enforce_clinic_access must reject cross-tenant
    requested_clinic_id before payment_service is even called."""
    from app.routers.admin import admin_confirm_booking
    from fastapi import HTTPException

    user = AdminUser(
        username="staff_a", role="clinic_admin", clinic_id="clinic-A", user_id="u-1"
    )

    with pytest.raises(HTTPException) as exc_info:
        await admin_confirm_booking(
            booking_id="booking-1", clinic_id="clinic-B", body=None, user=user
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_confirm_booking_route_logs_audit_action():
    """A successful confirm must write an admin_audit_logs entry."""
    from app.routers.admin import admin_confirm_booking

    user = AdminUser(
        username="staff_a", role="clinic_admin", clinic_id="clinic-A", user_id="u-1"
    )

    with patch(
        "app.services.payment.payment_service.admin_confirm_booking",
        new_callable=AsyncMock,
        return_value={"success": True},
    ), patch(
        "app.routers.admin.log_admin_action", new_callable=AsyncMock
    ) as mock_log:
        result = await admin_confirm_booking(
            booking_id="booking-1", clinic_id="default", body=None, user=user
        )

    assert result["success"] is True
    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["action"] == "BOOKING_MANUAL_CONFIRM"
    assert mock_log.call_args.kwargs["resource_id"] == "booking-1"
```

- [ ] **Step 7: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: PASS, 274 tests (268 + 6 new).

- [ ] **Step 8: Commit**

```bash
git add app/services/payment.py app/routers/admin.py tests/test_payment.py tests/test_admin_staff_identity.py
git commit -m "fix(admin): scope booking confirm/reject to clinic_id, close cross-tenant BOLA"
```

---

## Task 2: Fix OPD token-number race condition with a DB-level unique constraint

**Severity:** CRITICAL (data-integrity, occurs under normal concurrent load, no attacker needed).

**Files:**
- Create: `migrations/021_unique_queue_token.sql`
- Modify: `app/database.py:583-621` (`check_in_appointment`)
- Test: `tests/test_admin_queue.py` (extend)

**Interfaces:**
- Consumes: existing Supabase `appointments` table columns `token_number`, `queue_status`, `clinic_id`, `doctor_name`, `appointment_date` (already present per migration 019).
- Produces: `check_in_appointment(clinic_id: str, appointment_id: str) -> Optional[dict]` — signature unchanged, now retries up to 5 times on a unique-constraint violation instead of silently allowing duplicate `token_number` values.

- [ ] **Step 1: Write the migration**

```sql
-- Migration 021: Enforce OPD token-number uniqueness at the database level
-- Run in Supabase SQL Editor
--
-- Migration 019 added token_number/queue_status but only a non-unique index.
-- Two concurrent "Check In" requests for the same doctor+date can both read
-- the same MAX(token_number) before either commits, assigning the SAME
-- token to two different patients. This mirrors the fix already applied to
-- appointment-slot double-booking (see migration 008's idx_unique_active_slot)
-- — a partial UNIQUE index, not application-level locking, is the fix.

CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_queue_token
    ON appointments (clinic_id, doctor_name, appointment_date, token_number)
    WHERE token_number IS NOT NULL;

-- Verify
SELECT indexname FROM pg_indexes
WHERE tablename = 'appointments' AND indexname = 'idx_unique_queue_token';
```

- [ ] **Step 2: Write the failing test in `tests/test_admin_queue.py`**

Append:

```python
@pytest.mark.asyncio
async def test_check_in_appointment_retries_on_token_conflict():
    """If the UNIQUE index rejects the first token due to a concurrent
    check-in, check_in_appointment must retry with the next number instead
    of returning None or raising."""
    from app.database import check_in_appointment

    with patch("app.database.supabase") as mock_sb:
        mock_table = MagicMock()
        mock_sb.table.return_value = mock_table

        mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"doctor_name": "Dr. Rao", "appointment_date": "2026-08-10"}]
        )

        select_chain = mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
        select_chain.execute.return_value = MagicMock(data=[])

        update_chain = mock_table.update.return_value.eq.return_value.eq.return_value
        update_chain.execute.side_effect = [
            Exception("duplicate key value violates unique constraint idx_unique_queue_token"),
            MagicMock(data=[{"id": "appt-1", "token_number": 2, "queue_status": "waiting"}]),
        ]

        result = await check_in_appointment("clinic-1", "appt-1")

    assert result is not None
    assert result["token_number"] == 2
    assert update_chain.execute.call_count == 2


@pytest.mark.asyncio
async def test_check_in_appointment_gives_up_after_max_retries():
    """After exhausting retries, return None instead of raising."""
    from app.database import check_in_appointment

    with patch("app.database.supabase") as mock_sb:
        mock_table = MagicMock()
        mock_sb.table.return_value = mock_table
        mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"doctor_name": "Dr. Rao", "appointment_date": "2026-08-10"}]
        )
        select_chain = mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
        select_chain.execute.return_value = MagicMock(data=[])

        update_chain = mock_table.update.return_value.eq.return_value.eq.return_value
        update_chain.execute.side_effect = Exception(
            "duplicate key value violates unique constraint idx_unique_queue_token"
        )

        result = await check_in_appointment("clinic-1", "appt-1")

    assert result is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_admin_queue.py -v`
Expected: FAIL — current implementation has no retry loop; the mocked exception propagates and the outer `except Exception` returns `None` on the FIRST attempt, failing the "retries" assertion.

- [ ] **Step 4: Fix `app/database.py` — replace `check_in_appointment` (lines 583-621) with a retry-on-conflict version**

```python
async def check_in_appointment(clinic_id: str, appointment_id: str) -> Optional[dict]:
    """Assign the next sequential token number for this appointment's doctor+date.

    Race-safe: relies on the UNIQUE partial index idx_unique_queue_token
    (migration 021) to reject collisions, and retries with the next number
    on conflict instead of allowing duplicate tokens under concurrent check-ins.
    """
    try:
        appt_result = (
            supabase.table("appointments")
            .select("doctor_name, appointment_date")
            .eq("clinic_id", clinic_id)
            .eq("id", appointment_id)
            .execute()
        )
        if not appt_result.data:
            return None
        doctor_name = appt_result.data[0]["doctor_name"]
        appointment_date = appt_result.data[0]["appointment_date"]

        max_retries = 5
        for attempt in range(max_retries):
            max_result = (
                supabase.table("appointments")
                .select("token_number")
                .eq("clinic_id", clinic_id)
                .eq("doctor_name", doctor_name)
                .eq("appointment_date", appointment_date)
                .order("token_number", desc=True)
                .limit(1)
                .execute()
            )
            current_max = (
                max_result.data[0]["token_number"]
                if max_result.data and max_result.data[0]["token_number"]
                else 0
            )
            next_token = current_max + 1 + attempt

            try:
                result = (
                    supabase.table("appointments")
                    .update({"token_number": next_token, "queue_status": "waiting"})
                    .eq("clinic_id", clinic_id)
                    .eq("id", appointment_id)
                    .execute()
                )
                return result.data[0] if result.data else None
            except Exception as e:
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    logger.info(
                        f"check_in_appointment: token {next_token} collision for "
                        f"{doctor_name}/{appointment_date}, retrying (attempt {attempt + 1})"
                    )
                    continue
                raise

        logger.error(f"check_in_appointment: exhausted retries for {appointment_id}")
        return None

    except Exception as e:
        logger.error(f"Error checking in appointment: {e}")
        return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_admin_queue.py -v`
Expected: PASS (4 tests: 2 existing + 2 new).

- [ ] **Step 6: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: PASS, 276 tests.

- [ ] **Step 7: Commit**

```bash
git add migrations/021_unique_queue_token.sql app/database.py tests/test_admin_queue.py
git commit -m "fix(db): enforce OPD token uniqueness at DB level, retry check-in on conflict"
```

**Manual step (tell the user):** Run `migrations/021_unique_queue_token.sql` in the Supabase SQL Editor for every environment (staging + production) — this task's tests mock the DB, they don't apply the migration.

---

## Task 3: Fail-closed Meta webhook signature verification

**Severity:** HIGH. Currently fails OPEN (`return True`) whenever `META_APP_SECRET` is unset.

**Files:**
- Modify: `app/config.py` (add one setting)
- Modify: `app/utils/security.py:25-77` (`verify_webhook_signature`)
- Test: create `tests/test_security_utils.py`

**Interfaces:**
- Consumes: `app.config.settings` (existing singleton).
- Produces: `settings.allow_unsigned_webhooks_dev: bool` (new, default `False`). `verify_webhook_signature(payload_body: bytes, signature_header: Optional[str], app_secret: str) -> bool` — signature unchanged, but now imports `from app.config import settings` internally and returns `False` (not `True`) when `app_secret` is falsy, UNLESS `settings.app_env == "development" and settings.allow_unsigned_webhooks_dev`.

- [ ] **Step 1: Confirm no existing test file covers this function, then write the failing tests**

Run: `Glob "tests/test_security*.py"` first to confirm no collision, then create `tests/test_security_utils.py`:

```python
"""Tests for app/utils/security.py — webhook signature fail-closed behavior (Finding #3)."""

import hashlib
import hmac
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

from app.utils.security import verify_webhook_signature


class TestWebhookSignatureFailClosed:
    def test_missing_secret_fails_closed_in_production(self):
        """No META_APP_SECRET + app_env=production -> REJECT, not accept."""
        with patch("app.utils.security.settings") as mock_settings:
            mock_settings.app_env = "production"
            mock_settings.allow_unsigned_webhooks_dev = False
            result = verify_webhook_signature(b"body", "sha256=whatever", "")
        assert result is False

    def test_missing_secret_fails_closed_by_default_in_development(self):
        """Even in development, missing secret rejects UNLESS the explicit
        opt-in flag is set — no more silent accept-by-default."""
        with patch("app.utils.security.settings") as mock_settings:
            mock_settings.app_env = "development"
            mock_settings.allow_unsigned_webhooks_dev = False
            result = verify_webhook_signature(b"body", "sha256=whatever", "")
        assert result is False

    def test_missing_secret_allowed_only_with_explicit_dev_opt_in(self):
        with patch("app.utils.security.settings") as mock_settings:
            mock_settings.app_env = "development"
            mock_settings.allow_unsigned_webhooks_dev = True
            result = verify_webhook_signature(b"body", "sha256=whatever", "")
        assert result is True

    def test_valid_signature_still_accepted(self):
        secret = "my_app_secret"
        body = b'{"test": "payload"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with patch("app.utils.security.settings") as mock_settings:
            mock_settings.app_env = "production"
            mock_settings.allow_unsigned_webhooks_dev = False
            result = verify_webhook_signature(body, sig, secret)
        assert result is True

    def test_invalid_signature_still_rejected(self):
        with patch("app.utils.security.settings") as mock_settings:
            mock_settings.app_env = "production"
            mock_settings.allow_unsigned_webhooks_dev = False
            result = verify_webhook_signature(b"body", "sha256=wrong", "real_secret")
        assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_security_utils.py -v`
Expected: FAIL on `test_missing_secret_fails_closed_in_production` and `test_missing_secret_fails_closed_by_default_in_development` (current code returns `True`); the other three should already pass since they test unchanged behavior.

- [ ] **Step 3: Add the setting in `app/config.py`**

Insert after line 48 (`rate_limit_login: str = "5/minute"`):

```python
    allow_unsigned_webhooks_dev: bool = False  # NEVER set true outside local dev — explicit opt-in only
```

- [ ] **Step 4: Fix `app/utils/security.py` — replace lines 25-77**

```python
def verify_webhook_signature(
    payload_body: bytes,
    signature_header: Optional[str],
    app_secret: str,
) -> bool:
    """
    Verify Meta X-Hub-Signature-256 header.

    Meta signs every webhook payload with HMAC-SHA256 using your App Secret.
    If this doesn't match, the request was NOT from Meta — reject it.

    Fails CLOSED by default in every environment when app_secret is missing.
    The only way to accept unsigned webhooks is the explicit
    ALLOW_UNSIGNED_WEBHOOKS_DEV=true flag, and only when APP_ENV=development —
    never a silently-missing secret in a misconfigured staging/prod deploy.

    Args:
        payload_body: Raw request body bytes (before JSON parsing).
        signature_header: Value of X-Hub-Signature-256 header.
        app_secret: Your Meta App Secret (from Meta Developer Console).

    Returns:
        True if signature is valid, False otherwise.
    """
    from app.config import settings

    if not app_secret:
        if settings.app_env == "development" and settings.allow_unsigned_webhooks_dev:
            logger.warning(
                "META_APP_SECRET not configured — signature verification SKIPPED "
                "(ALLOW_UNSIGNED_WEBHOOKS_DEV=true, app_env=development only)."
            )
            return True
        logger.error(
            "META_APP_SECRET not configured — REJECTING webhook (fail-closed). "
            "Set META_APP_SECRET, or ALLOW_UNSIGNED_WEBHOOKS_DEV=true for local dev only."
        )
        return False

    if not signature_header:
        logger.warning("Webhook request missing X-Hub-Signature-256 header — REJECTED")
        return False

    if not signature_header.startswith("sha256="):
        logger.warning("Webhook signature has invalid format — REJECTED")
        return False

    expected_signature = (
        "sha256="
        + hmac.new(
            app_secret.encode("utf-8"),
            payload_body,
            hashlib.sha256,
        ).hexdigest()
    )

    is_valid = hmac.compare_digest(expected_signature, signature_header)

    if not is_valid:
        logger.warning(
            "Webhook signature mismatch — REJECTED (possible spoofed request)"
        )

    return is_valid
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_security_utils.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: PASS, 281 tests.

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/utils/security.py tests/test_security_utils.py
git commit -m "fix(security): fail closed on missing META_APP_SECRET instead of accepting unsigned webhooks"
```

**Manual step (tell the user):** Confirm `META_APP_SECRET` is actually set in every real deployment's environment variables (Render/Railway dashboard) — after this fix, a missing secret means the bot stops receiving WhatsApp messages entirely instead of silently accepting spoofed ones. This is the intended trade-off, but verify it live once deployed.

---

## Task 4: Rate-limit Razorpay webhook signature-failure admin alerts

**Severity:** HIGH. Unauthenticated flood of bad-signature POSTs to `/webhooks/razorpay/{clinic_id}` currently triggers an unbounded number of real outbound WhatsApp alerts to the hospital's admin number.

**Files:**
- Modify: `app/services/payment.py:279-314` (`process_payment_webhook`, signature-failure branch); add `TYPE_CHECKING` import near top of file
- Modify: `app/routers/razorpay_webhook.py:1-78` (full file — add limiter + wire through)
- Test: `tests/test_payment.py` (extend `TestPaymentWebhookProcessing`)

**Interfaces:**
- Consumes: `PersistentRateLimiter` (existing class, `app/utils/security.py:154-324`, unchanged) — reused here, not modified.
- Produces: `PaymentService.process_payment_webhook(raw_body: bytes, signature: str, webhook_secret: Optional[str] = None, alert_limiter: Optional["PersistentRateLimiter"] = None, alert_key: Optional[str] = None) -> dict` — two new optional kwargs appended at the end, fully backward compatible.

- [ ] **Step 1: Write the failing test in `tests/test_payment.py`**

Add to `TestPaymentWebhookProcessing`:

```python
    @pytest.mark.asyncio
    async def test_signature_failure_alert_is_rate_limited(self):
        """Repeated bad-signature webhooks for the same key must not each
        trigger a fresh _alert_admin call once the limiter says no."""
        from app.services.payment import PaymentService
        from app.utils.security import PersistentRateLimiter

        service = PaymentService()
        payload = json.dumps(_make_payment_webhook_payload()).encode()
        limiter = PersistentRateLimiter(max_attempts=3, window_seconds=300)

        with patch.object(service, "_log_payment_event_raw"), patch.object(
            service, "_alert_admin", new_callable=AsyncMock
        ) as mock_alert, patch.object(
            limiter, "is_rate_limited", side_effect=[False, False, False, True, True]
        ), patch.object(
            limiter, "record_attempt"
        ):
            for _ in range(5):
                await service.process_payment_webhook(
                    payload,
                    "bad_signature",
                    alert_limiter=limiter,
                    alert_key="clinic-1:1.2.3.4",
                )

        assert mock_alert.call_count == 3

    @pytest.mark.asyncio
    async def test_signature_failure_without_limiter_still_alerts(self):
        """Backward compatibility: callers that don't pass a limiter still
        get alerted, same as before."""
        from app.services.payment import PaymentService

        service = PaymentService()
        payload = json.dumps(_make_payment_webhook_payload()).encode()

        with patch.object(service, "_log_payment_event_raw"), patch.object(
            service, "_alert_admin", new_callable=AsyncMock
        ) as mock_alert:
            await service.process_payment_webhook(payload, "bad_signature")

        mock_alert.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_payment.py::TestPaymentWebhookProcessing::test_signature_failure_alert_is_rate_limited -v`
Expected: FAIL — `TypeError: process_payment_webhook() got an unexpected keyword argument 'alert_limiter'`.

- [ ] **Step 3: Fix `app/services/payment.py` — replace lines 279-314**

```python
    async def process_payment_webhook(
        self,
        raw_body: bytes,
        signature: str,
        webhook_secret: Optional[str] = None,
        alert_limiter: Optional["PersistentRateLimiter"] = None,
        alert_key: Optional[str] = None,
    ) -> dict:
        """Process a Razorpay webhook event.

        Args:
            webhook_secret: Per-clinic webhook secret resolved by the router.
                            Falls back to settings if None.
            alert_limiter: Optional PersistentRateLimiter used to throttle the
                            _alert_admin() call on repeated signature failures
                            (an unauthenticated attacker can otherwise flood
                            the hospital's own WhatsApp admin number). Callers
                            that omit this keep the old unthrottled behavior.
            alert_key: Key to rate-limit on (e.g. "{clinic_id}:{client_ip}").

        Returns: {"status": "ok"|"error"|"ignored", "code": 200|400}
        """
        # ── Step 1: Verify signature FIRST ──
        if not self.verify_webhook_signature(
            raw_body, signature, webhook_secret=webhook_secret
        ):
            self._log_payment_event_raw(
                None,
                "signature_failed",
                {
                    "signature_provided": (
                        signature[:20] + "..." if signature else "none"
                    ),
                    "body_length": len(raw_body),
                },
            )
            logger.warning(
                "⚠️ Razorpay webhook SIGNATURE FAILED — possible spoofing attempt"
            )
            should_alert = True
            if alert_limiter is not None:
                key = alert_key or "global"
                should_alert = not alert_limiter.is_rate_limited(key)
                if should_alert:
                    alert_limiter.record_attempt(key)
            if should_alert:
                await self._alert_admin(
                    "🚨 Payment webhook signature verification FAILED. Possible spoofing attempt."
                )
            return {"status": "error", "code": 400, "reason": "signature_failed"}
```

Keep everything from the next step-comment onward (payload parsing) unchanged.

Add near the top of the file, after the existing imports:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.utils.security import PersistentRateLimiter
```

- [ ] **Step 4: Fix `app/routers/razorpay_webhook.py` — wire the limiter through**

Replace the file's imports and route function (lines 1-78) with:

```python
"""Razorpay webhook receiver — per-clinic signature-verified payment events."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services.payment import payment_service, get_razorpay_creds
from app.utils.security import PersistentRateLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["payments"])

# Throttles the WhatsApp admin alert sent on every bad-signature webhook —
# an unauthenticated attacker can otherwise flood the hospital's own WhatsApp
# number and burn Meta API quota by repeatedly POSTing garbage here.
_signature_alert_limiter = PersistentRateLimiter(max_attempts=3, window_seconds=300)


@router.post("/razorpay/{clinic_id}")
async def razorpay_webhook(clinic_id: str, request: Request):
    """Receive and process Razorpay payment webhook events for a specific clinic.

    Flow:
      1. Resolve the clinic from the database using clinic_id.
      2. Extract the per-clinic razorpay_webhook_secret (falls back to global settings).
      3. Read raw body (before parsing).
      4. Extract X-Razorpay-Signature header.
      5. Delegate to PaymentService.process_payment_webhook() with the resolved secret.
      6. Return appropriate HTTP status.
    """
    try:
        from app.services.tenant import get_clinic_by_id

        clinic = await get_clinic_by_id(clinic_id)
    except Exception as e:
        logger.warning(f"Razorpay webhook: unknown clinic_id={clinic_id} — {e}")
        return JSONResponse(status_code=200, content={"status": "unknown_clinic"})

    _, _, webhook_secret = get_razorpay_creds(clinic)

    raw_body = await request.body()

    signature = request.headers.get("X-Razorpay-Signature", "")
    client_ip = request.client.host if request.client else "unknown"

    if not signature:
        logger.warning(
            f"Razorpay webhook: NO signature header — "
            f"clinic={clinic_id} IP={client_ip}"
        )

    result = await payment_service.process_payment_webhook(
        raw_body,
        signature,
        webhook_secret=webhook_secret,
        alert_limiter=_signature_alert_limiter,
        alert_key=f"{clinic_id}:{client_ip}",
    )

    return JSONResponse(
        status_code=result.get("code", 200),
        content={"status": result.get("status", "ok")},
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_payment.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: PASS, 283 tests.

- [ ] **Step 7: Commit**

```bash
git add app/services/payment.py app/routers/razorpay_webhook.py tests/test_payment.py
git commit -m "fix(payment): rate-limit admin alerts on repeated webhook signature failures"
```

---

## Task 5: Fix non-functional Razorpay payment link

**Severity:** HIGH (business-critical — patients likely cannot pay today).

**Files:**
- Create: `migrations/022_razorpay_payment_link_id.sql`
- Modify: `app/services/payment.py:95-243` (`create_booking_with_payment`) and `:341-383` (`process_payment_webhook` idempotency/lookup block) and `:1012-1025` (`_build_payment_link`, replaced)
- Test: `tests/test_payment.py` (update `TestBookingCreation` mocks, extend with `TestPaymentLinkGeneration`)

**Interfaces:**
- Consumes: Razorpay Payment Links API (`POST https://api.razorpay.com/v1/payment_links`, returns `{"id": "plink_xxx", "short_url": "https://rzp.io/i/xxxxx", ...}`).
- Produces: `PaymentService._create_payment_link(amount_paise: int, booking_id: str, booking_ref: str, patient_phone: str, patient_name: str, key_id: str, key_secret: str) -> dict` (new method, replaces `_build_payment_link`). `create_booking_with_payment(...)` return dict's `"payment_link"` key now contains a real `rzp.io` short URL. New `appointments.razorpay_payment_link_id` column.

- [ ] **Step 1: Write the migration**

```sql
-- Migration 022: Track Razorpay Payment Link ID for reconciliation
-- Run in Supabase SQL Editor
--
-- create_booking_with_payment() previously built a raw
-- api.razorpay.com/v1/checkout/embedded URL, which is an API endpoint meant
-- for checkout.js embedding, not a browsable hosted page — patients tapping
-- this link in WhatsApp could not complete payment. Switched to Razorpay's
-- Payment Links API, which returns a real rzp.io short URL. Payment Links
-- attach captured payments to a payment_link_id (not order_id), so we need
-- a column to correlate incoming webhooks back to the booking.

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS razorpay_payment_link_id TEXT NULL;
CREATE INDEX IF NOT EXISTS idx_appointments_payment_link_id
    ON appointments (razorpay_payment_link_id) WHERE razorpay_payment_link_id IS NOT NULL;

-- Verify
SELECT column_name FROM information_schema.columns
WHERE table_name = 'appointments' AND column_name = 'razorpay_payment_link_id';
```

- [ ] **Step 2: Write the failing tests in `tests/test_payment.py`**

Add a new class after `TestBookingCreation` (before `TestRefundFlow`):

```python
class TestPaymentLinkGeneration:
    """Regression tests for the broken checkout-URL fix (Finding #5)."""

    @pytest.mark.asyncio
    async def test_create_payment_link_returns_hosted_short_url(self):
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "plink_test123",
            "short_url": "https://rzp.io/i/abc123",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            result = await service._create_payment_link(
                amount_paise=50000,
                booking_id="booking-1",
                booking_ref="MC-2026-1234",
                patient_phone="+919876543210",
                patient_name="Ramesh Sharma",
                key_id="rzp_test_key123",
                key_secret="rzp_test_secret456",
            )

        assert result["short_url"] == "https://rzp.io/i/abc123"
        assert result["id"] == "plink_test123"
        call_kwargs = mock_client.post.call_args
        assert "payment_links" in call_kwargs.args[0]
        assert call_kwargs.kwargs["json"]["amount"] == 50000
        assert call_kwargs.kwargs["json"]["reference_id"] == "MC-2026-1234"

    @pytest.mark.asyncio
    async def test_booking_with_payment_returns_rzp_io_link_not_api_endpoint(self):
        """End-to-end: the payment_link returned to the patient must be a
        real hosted page, never the old api.razorpay.com/v1/... API URL."""
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ), patch.object(
            service,
            "_create_payment_link",
            new_callable=AsyncMock,
            return_value={"id": "plink_1", "short_url": "https://rzp.io/i/xyz"},
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "booking-1"}]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[])
            )

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Ramesh Sharma",
                department="Cardiology",
                doctor_name="Dr. Rao",
                appointment_date="2026-08-10",
                appointment_time="10:00",
            )

        assert result["success"] is True
        assert result["payment_link"] == "https://rzp.io/i/xyz"
        assert "api.razorpay.com" not in result["payment_link"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_payment.py::TestPaymentLinkGeneration -v`
Expected: FAIL — `AttributeError: 'PaymentService' object has no attribute '_create_payment_link'`.

- [ ] **Step 4: Read the current `create_booking_with_payment` and `process_payment_webhook` bodies in full before editing**

Run `Read` on `app/services/payment.py` lines 95-400 and 1000-1030 to get exact current line numbers immediately before editing (line numbers may have drifted since the audit was written) — do not blind-offset from the numbers in this plan.

- [ ] **Step 5: Add `_create_payment_link`, replace `_build_payment_link`**

Replace the `_build_payment_link` method with:

```python
    async def _create_payment_link(
        self,
        amount_paise: int,
        booking_id: str,
        booking_ref: str,
        patient_phone: str,
        patient_name: str,
        key_id: str = "",
        key_secret: str = "",
    ) -> dict:
        """Create a Razorpay Payment Link — a real hosted checkout page.

        Unlike the old `checkout/embedded` API endpoint (which requires
        Razorpay's checkout.js to render and is NOT a standalone browsable
        page), this returns a short_url (rzp.io/i/xxxxx) that works when
        tapped directly from a WhatsApp message on a mobile browser.
        """
        effective_key_id = key_id or settings.razorpay_key_id
        effective_key_secret = key_secret or settings.razorpay_key_secret

        link_data = {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "description": f"Appointment booking {booking_ref}",
            "customer": {
                "name": patient_name,
                "contact": patient_phone,
            },
            "notify": {"sms": False, "email": False},
            "reference_id": booking_ref,
            "notes": {"booking_id": booking_id, "booking_ref": booking_ref},
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._razorpay_base}/payment_links",
                json=link_data,
                auth=(effective_key_id, effective_key_secret),
                timeout=15.0,
            )
            response.raise_for_status()
            return response.json()
```

- [ ] **Step 6: Update `create_booking_with_payment` to call `_create_payment_link` instead of creating an Order + building a checkout URL**

Locate the block that currently calls `self._create_razorpay_order(...)` then `self._build_payment_link(razorpay_order_id, ...)` (originally around lines 189-243, re-confirm via Step 4's fresh read) and replace it with:

```python
        # ── Create Razorpay Payment Link ──
        # (Payment Links attach captured payments to a payment_link_id, not
        # an order_id — no separate Order object is needed for this flow.)
        try:
            link = await self._create_payment_link(
                amount_paise=amount_paise,
                booking_id=booking_id,
                booking_ref=booking_ref,
                patient_phone=patient_phone,
                patient_name=patient_name,
                key_id=key_id,
                key_secret=key_secret,
            )

            payment_link_id = link["id"]
            payment_link = link["short_url"]

            supabase.table("appointments").update(
                {"razorpay_payment_link_id": payment_link_id}
            ).eq("id", booking_id).execute()

            self._log_payment_event(
                booking_id,
                "payment_link_created",
                {
                    "razorpay_payment_link_id": payment_link_id,
                    "amount_paise": amount_paise,
                    "booking_ref": booking_ref,
                },
            )

            return {
                "success": True,
                "booking_id": booking_id,
                "booking_ref": booking_ref,
                "razorpay_payment_link_id": payment_link_id,
                "payment_link": payment_link,
                "amount_paise": amount_paise,
                "hold_expires_at": hold_expires_at,
            }

        except Exception as e:
            logger.error(f"Razorpay payment link creation failed: {e}")
            try:
                supabase.table("appointments").update({"status": "cancelled"}).eq(
                    "id", booking_id
                ).execute()
                self._log_payment_event(
                    booking_id, "payment_link_creation_failed", {"error": str(e)[:500]}
                )
            except Exception:
                pass
            return {"success": False, "reason": "razorpay_error"}
```

Keep every variable this block depends on (`amount_paise`, `booking_id`, `booking_ref`, `patient_phone`, `patient_name`, `key_id`, `key_secret`, `hold_expires_at`) exactly as already computed earlier in the function — do not re-derive them.

- [ ] **Step 7: Update `process_payment_webhook`'s booking-lookup block to match on `payment_link_id`, and delete the dead `existing_event` query in the same edit (this also resolves Finding #9 — see Task 9's note)**

Locate the Step-4/Step-5 block in `process_payment_webhook` (idempotency check + booking lookup, originally lines 341-383) and replace it with:

```python
        # ── Step 3: Extract payment details ──
        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        payment_id = payment_entity.get("id")
        amount_paid = payment_entity.get("amount")  # in paise
        notes = payment_entity.get("notes", {})

        if not payment_id:
            logger.error("Razorpay webhook: missing payment_id")
            return {"status": "error", "code": 400, "reason": "missing_fields"}

        # ── Step 4: Idempotency check ──
        existing_confirmed = (
            supabase.table("appointments")
            .select("id")
            .eq("payment_id", payment_id)
            .eq("status", "confirmed")
            .execute()
        )

        if existing_confirmed.data:
            logger.info(
                f"Razorpay webhook: payment_id {payment_id} already processed (idempotent)"
            )
            return {"status": "ok", "code": 200, "reason": "already_processed"}

        # ── Step 5: Look up booking ──
        # Payment Links attach to payment_link_id — match on that first,
        # with the notes.booking_id fallback for defense-in-depth.
        payment_link_id = (
            payload.get("payload", {}).get("payment_link", {}).get("entity", {}).get("id")
        )

        booking_result = None
        if payment_link_id:
            booking_result = (
                supabase.table("appointments")
                .select("*")
                .eq("razorpay_payment_link_id", payment_link_id)
                .execute()
            )

        if not booking_result or not booking_result.data:
            booking_id_from_notes = notes.get("booking_id")
            if booking_id_from_notes:
                booking_result = (
                    supabase.table("appointments")
                    .select("*")
                    .eq("id", booking_id_from_notes)
                    .execute()
                )

        if not booking_result or not booking_result.data:
            logger.error(
                f"Razorpay webhook: no booking found for payment_link {payment_link_id}"
            )
            self._log_payment_event_raw(
                None,
                "webhook_received",
                {
                    "payment_id": payment_id,
                    "payment_link_id": payment_link_id,
                    "error": "no_booking_found",
                    "raw": payload,
                },
            )
            return {"status": "error", "code": 200, "reason": "booking_not_found"}
```

Keep the rest of the function (from `booking = booking_result.data[0]` onward) unchanged, except rename any remaining `order_id` references inside log-payload dicts (not code logic) to `payment_link_id` for consistency — grep `order_id` in the remainder of the function first to find them.

- [ ] **Step 8: Update existing tests that reference the old order-based flow**

Search `tests/test_payment.py` for `razorpay_order_id`, `_create_razorpay_order`, and `_build_payment_link`. Read each surrounding test fully, then update the mocks to patch `_create_payment_link`/reference `razorpay_payment_link_id` instead, following the same mock-chain style as the Step 2 tests. Preserve every other assertion in those tests unchanged.

- [ ] **Step 9: Run tests to verify they pass**

Run: `python -m pytest tests/test_payment.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 10: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add migrations/022_razorpay_payment_link_id.sql app/services/payment.py tests/test_payment.py
git commit -m "fix(payment): use Razorpay Payment Links API for a real hosted checkout URL"
```

**Manual step (tell the user, high priority):** Before this ships, test against a real Razorpay TEST-mode account: create a booking, confirm the returned `payment_link` is a `rzp.io` URL, open it on a mobile browser, complete a test payment, and confirm the webhook correctly confirms the booking. This is the single highest-impact fix in the whole audit (it may be why patients can't currently pay) and deserves a live smoke test, not just mocked unit tests.

---

## Task 6: Fix "call next patient" queue-advance race condition

**Severity:** HIGH.

**Files:**
- Modify: `app/database.py:624-654` (`call_next_patient`)
- Test: `tests/test_admin_queue.py` (extend)

**Interfaces:**
- Consumes: nothing new.
- Produces: `call_next_patient(clinic_id: str, doctor_name: str, date_str: str) -> Optional[dict]` — signature unchanged. Internally now uses a conditional `.eq("queue_status", "waiting")` guard on the claiming UPDATE and retries if a concurrent caller already claimed the row.

- [ ] **Step 1: Write the failing test in `tests/test_admin_queue.py`**

```python
@pytest.mark.asyncio
async def test_call_next_patient_retries_if_candidate_already_claimed():
    """If a concurrent call already claimed the first candidate (guarded
    UPDATE affects 0 rows), retry with the next waiting patient."""
    from app.database import call_next_patient

    with patch("app.database.supabase") as mock_sb:
        mock_table = MagicMock()
        mock_sb.table.return_value = mock_table

        mock_table.update.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        select_chain = mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
        select_chain.execute.side_effect = [
            MagicMock(data=[{"id": "appt-1", "token_number": 1}]),
            MagicMock(data=[{"id": "appt-2", "token_number": 2}]),
        ]

        claim_chain = mock_table.update.return_value.eq.return_value.eq.return_value.eq.return_value
        claim_chain.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[{"id": "appt-2", "queue_status": "in_consultation"}]),
        ]

        result = await call_next_patient("clinic-1", "Dr. Rao", "2026-08-10")

    assert result is not None
    assert result["id"] == "appt-2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_admin_queue.py::test_call_next_patient_retries_if_candidate_already_claimed -v`
Expected: FAIL — current implementation performs a single unconditional claim and returns `next_appt` regardless of whether the UPDATE actually matched a row.

- [ ] **Step 3: Fix `app/database.py` — replace lines 624-654**

```python
async def call_next_patient(clinic_id: str, doctor_name: str, date_str: str) -> Optional[dict]:
    """Mark the current in_consultation patient done, and the next waiting
    patient in_consultation. Race-safe: the claiming UPDATE is conditioned
    on queue_status still being 'waiting', so a concurrent caller that
    already claimed the same row causes a retry instead of a double-serve."""
    try:
        supabase.table("appointments").update({"queue_status": "done"}).eq(
            "clinic_id", clinic_id
        ).eq("doctor_name", doctor_name).eq("appointment_date", date_str).eq(
            "queue_status", "in_consultation"
        ).execute()

        max_retries = 5
        for _ in range(max_retries):
            next_result = (
                supabase.table("appointments")
                .select("*")
                .eq("clinic_id", clinic_id)
                .eq("doctor_name", doctor_name)
                .eq("appointment_date", date_str)
                .eq("queue_status", "waiting")
                .order("token_number")
                .limit(1)
                .execute()
            )
            if not next_result.data:
                return None

            candidate = next_result.data[0]
            claimed = (
                supabase.table("appointments")
                .update({"queue_status": "in_consultation"})
                .eq("clinic_id", clinic_id)
                .eq("id", candidate["id"])
                .eq("queue_status", "waiting")
                .execute()
            )
            if claimed.data:
                return claimed.data[0]

        return None
    except Exception as e:
        logger.error(f"Error calling next patient: {e}")
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_admin_queue.py -v`
Expected: PASS (all tests in file — existing `test_call_next_endpoint_advances_queue` mocks `call_next_patient` itself, not its internals, so it's unaffected).

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/database.py tests/test_admin_queue.py
git commit -m "fix(db): make call_next_patient's queue-advance claim atomic and retry-safe"
```

---

## Task 7: Fix admin-login rate limiter TOCTOU race

**Severity:** MEDIUM.

**Files:**
- Create: `migrations/023_atomic_rate_limit_rpc.sql`
- Modify: `app/utils/security.py:187-216` (add `check_and_record` after `is_rate_limited`)
- Modify: `app/routers/admin.py:136-145` (`verify_credentials`)
- Test: `tests/test_security_utils.py` (extend, from Task 3)

**Interfaces:**
- Consumes: new Postgres function `check_and_record_rate_limit(p_key text, p_max_attempts int, p_window_seconds int) RETURNS int` (atomic upsert, returns the attempt count AFTER incrementing).
- Produces: `PersistentRateLimiter.check_and_record(key: str) -> bool` (new method — returns `True` if this attempt IS rate-limited, doing the check-and-increment in one atomic round trip). `is_rate_limited()`/`record_attempt()` remain unchanged for any other caller; `verify_credentials` switches to the new atomic method.

- [ ] **Step 1: Confirm the exact table name backing `PersistentRateLimiter` before writing the migration**

Run: `Grep "def _get_supabase|self.table_name|\.table\(" app/utils/security.py` to confirm the actual Supabase table name used by `is_rate_limited`/`record_attempt` (referred to as `rate_limits` below — verify this matches, adjust the migration's table name if it differs).

- [ ] **Step 2: Write the migration**

```sql
-- Migration 023: Atomic rate-limit check-and-increment RPC
-- Run in Supabase SQL Editor
--
-- PersistentRateLimiter.is_rate_limited() + .record_attempt() were two
-- separate round trips (read-then-write), so parallelized concurrent login
-- attempts could all pass is_rate_limited() before any of their
-- record_attempt() writes landed — allowing more than max_attempts guesses
-- per window. This function makes check + increment one atomic statement.
--
-- NOTE: confirm the table name below (rate_limits) matches the table
-- PersistentRateLimiter actually reads/writes (see Step 1) before running.

CREATE OR REPLACE FUNCTION check_and_record_rate_limit(
    p_key TEXT,
    p_max_attempts INT,
    p_window_seconds INT
) RETURNS INT AS $$
DECLARE
    v_attempts INT;
BEGIN
    INSERT INTO rate_limits (key, attempts, window_start)
    VALUES (p_key, 1, now())
    ON CONFLICT (key) DO UPDATE SET
        attempts = CASE
            WHEN rate_limits.window_start >= now() - (p_window_seconds || ' seconds')::interval
                THEN rate_limits.attempts + 1
            ELSE 1
        END,
        window_start = CASE
            WHEN rate_limits.window_start >= now() - (p_window_seconds || ' seconds')::interval
                THEN rate_limits.window_start
            ELSE now()
        END
    RETURNING attempts INTO v_attempts;

    RETURN v_attempts;
END;
$$ LANGUAGE plpgsql;

-- Verify
SELECT proname FROM pg_proc WHERE proname = 'check_and_record_rate_limit';
```

- [ ] **Step 3: Write the failing tests**

Add to `tests/test_security_utils.py`:

```python
class TestPersistentRateLimiterAtomicCheck:
    def test_check_and_record_uses_single_rpc_call(self):
        """The atomic path must be ONE round trip (an .rpc() call), not a
        separate select-then-insert/update — that's the actual fix."""
        from unittest.mock import MagicMock
        from app.utils.security import PersistentRateLimiter

        limiter = PersistentRateLimiter(max_attempts=5, window_seconds=60)
        mock_supabase = MagicMock()
        mock_supabase.rpc.return_value.execute.return_value = MagicMock(data=3)

        with patch.object(limiter, "_get_supabase", return_value=mock_supabase):
            is_limited = limiter.check_and_record("1.2.3.4")

        assert is_limited is False  # 3 attempts < max_attempts=5
        mock_supabase.rpc.assert_called_once_with(
            "check_and_record_rate_limit",
            {"p_key": "1.2.3.4", "p_max_attempts": 5, "p_window_seconds": 60},
        )

    def test_check_and_record_returns_true_when_limit_exceeded(self):
        from unittest.mock import MagicMock
        from app.utils.security import PersistentRateLimiter

        limiter = PersistentRateLimiter(max_attempts=5, window_seconds=60)
        mock_supabase = MagicMock()
        mock_supabase.rpc.return_value.execute.return_value = MagicMock(data=6)

        with patch.object(limiter, "_get_supabase", return_value=mock_supabase):
            is_limited = limiter.check_and_record("1.2.3.4")

        assert is_limited is True

    def test_check_and_record_falls_back_to_in_memory_on_rpc_error(self):
        from unittest.mock import MagicMock
        from app.utils.security import PersistentRateLimiter

        limiter = PersistentRateLimiter(max_attempts=2, window_seconds=60)
        mock_supabase = MagicMock()
        mock_supabase.rpc.side_effect = Exception("rpc not found")

        with patch.object(limiter, "_get_supabase", return_value=mock_supabase):
            assert limiter.check_and_record("1.2.3.4") is False
            assert limiter.check_and_record("1.2.3.4") is False
            assert limiter.check_and_record("1.2.3.4") is True  # 3rd attempt, max=2
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `python -m pytest tests/test_security_utils.py::TestPersistentRateLimiterAtomicCheck -v`
Expected: FAIL — `AttributeError: 'PersistentRateLimiter' object has no attribute 'check_and_record'`.

- [ ] **Step 5: Add `check_and_record` to `app/utils/security.py`, right after `is_rate_limited`**

```python
    def check_and_record(self, key: str) -> bool:
        """Atomically check-and-increment the attempt count in one round trip.

        Fixes a TOCTOU race in the separate is_rate_limited()/record_attempt()
        pair: under parallelized concurrent attempts, multiple requests could
        each pass is_rate_limited() before any of their record_attempt()
        writes landed, allowing more than max_attempts within one window.

        Returns:
            True if this attempt IS rate-limited (caller should reject it).
        """
        supabase = self._get_supabase()

        if supabase and not self._use_fallback:
            try:
                result = supabase.rpc(
                    "check_and_record_rate_limit",
                    {
                        "p_key": key,
                        "p_max_attempts": self.max_attempts,
                        "p_window_seconds": self.window_seconds,
                    },
                ).execute()
                attempts = result.data
                return attempts is not None and attempts > self.max_attempts
            except Exception as e:
                logger.warning(
                    f"Supabase check_and_record_rate_limit RPC failed, "
                    f"using in-memory fallback: {e}"
                )
                self._use_fallback = True

        was_limited = self._fallback_is_limited(key)
        self._fallback[key].append(time.time())
        return was_limited
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_security_utils.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 7: Wire `verify_credentials` in `app/routers/admin.py` to use the atomic method**

Replace lines 136-145:

```python
    if login_rate_limiter.is_rate_limited(client_ip):
        remaining_wait = 60
        logger.warning(f"Admin login rate limit exceeded — IP={client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {remaining_wait} seconds.",
            headers={"Retry-After": str(remaining_wait)},
        )

    login_rate_limiter.record_attempt(client_ip)
```

with:

```python
    if login_rate_limiter.check_and_record(client_ip):
        remaining_wait = 60
        logger.warning(f"Admin login rate limit exceeded — IP={client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {remaining_wait} seconds.",
            headers={"Retry-After": str(remaining_wait)},
        )
```

- [ ] **Step 8: Check for and update any existing admin-auth tests that assert on the old two-call pattern**

Run: `Grep "is_rate_limited|record_attempt" tests/` — if any test on `verify_credentials`/admin login asserts these were called, update it to assert `check_and_record` was called instead, following the same mocking style already used in that test.

- [ ] **Step 9: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add migrations/023_atomic_rate_limit_rpc.sql app/utils/security.py app/routers/admin.py tests/test_security_utils.py
git commit -m "fix(security): make admin login rate limiting atomic to close a brute-force TOCTOU race"
```

**Manual step (tell the user):** Run `migrations/023_atomic_rate_limit_rpc.sql` in the Supabase SQL Editor for every environment.

---

## Task 8: Alert on sustained idempotency fail-open

**Severity:** MEDIUM.

**Files:**
- Modify: `app/services/message_queue.py:44-134` (`MessageQueueManager.acquire` and module-level additions)
- Modify: `app/services/scheduler.py` (add a companion alert job — read the file first, see Step 6)
- Test: `tests/test_message_queue.py` (create if it doesn't exist)

**Interfaces:**
- Consumes: existing `logger` module-level instance in `message_queue.py`.
- Produces: module-level `_fail_open_count: int` and `get_fail_open_count() -> int` in `message_queue.py`. No change to `acquire()`'s return type or behavior.

- [ ] **Step 1: Check for an existing test file**

Run: `Glob "tests/test_message_queue*.py"` — if found, read and extend it; if not, create `tests/test_message_queue.py` with this header:

```python
"""Tests for app/services/message_queue.py idempotency + fail-open alerting."""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")
```

- [ ] **Step 2: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_acquire_fail_open_increments_counter():
    """A non-duplicate DB error on acquire() must still fail open (process
    the message — don't drop real patient messages) but now increments a
    counter the scheduler can alert on if this becomes sustained."""
    from app.services.message_queue import MessageQueueManager, get_fail_open_count

    manager = MessageQueueManager()
    before = get_fail_open_count()

    mock_db_module = MagicMock()
    mock_db_module.supabase.table.return_value.insert.return_value.execute.side_effect = Exception(
        "connection refused"
    )

    with patch.dict("sys.modules", {"app.database": mock_db_module}):
        result = await manager.acquire("msg-1", clinic_id=None)

    assert result is True  # still fails open
    assert get_fail_open_count() == before + 1


@pytest.mark.asyncio
async def test_acquire_duplicate_does_not_increment_fail_open_counter():
    """A genuine duplicate (unique violation) is expected behavior, not a
    failure — must not count toward the fail-open alert threshold."""
    from app.services.message_queue import MessageQueueManager, get_fail_open_count

    manager = MessageQueueManager()
    before = get_fail_open_count()

    mock_db_module = MagicMock()
    mock_db_module.supabase.table.return_value.insert.return_value.execute.side_effect = Exception(
        "duplicate key value violates unique constraint"
    )

    with patch.dict("sys.modules", {"app.database": mock_db_module}):
        result = await manager.acquire("msg-1", clinic_id=None)

    assert result is False
    assert get_fail_open_count() == before
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_message_queue.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_fail_open_count'`.

- [ ] **Step 4: Read `app/services/message_queue.py` in full to get exact current line numbers for the two fail-open `return True` sites before editing**

Run `Read` on the full file — the audit's originally-noted line numbers (~116-134) may have drifted.

- [ ] **Step 5: Fix `app/services/message_queue.py`**

Add near the top-level constants (after `PHONE_LOCK_TIMEOUT_SECONDS`):

```python
# Counter of acquire() calls that failed open due to a non-duplicate error
# (e.g. Supabase outage). A sustained non-zero rate here means messages may
# be double-processed — scheduler.py polls this to page an admin.
_fail_open_count = 0


def get_fail_open_count() -> int:
    """Current count of acquire() fail-open events since process start."""
    return _fail_open_count


def _record_fail_open() -> None:
    global _fail_open_count
    _fail_open_count += 1
```

In `acquire()`, find the two `return True` statements that follow `logger.warning(f"Message queue: acquire error (failing open): ...")` — the outer one (any exception when no `clinic_id` was given, or when the clinic_id retry itself also failed non-uniquely) and the inner one (in the `except Exception as e2` branch after a failed clinic_id-scoped insert retry). Add `_record_fail_open()` immediately before each of those two `return True` lines. Do NOT add it to the branch where a clinic_id-less retry insert *succeeds* (that one already returns `True` for a different, non-error reason and must stay unchanged).

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_message_queue.py -v`
Expected: PASS.

- [ ] **Step 7: Read `app/services/scheduler.py` in full to find the existing `alert_failed_messages` job's exact registration/alerting pattern**

Run `Read` on the full file (it has not yet been read in this plan's fact-gathering) before writing the new job — match its `scheduler.add_job(...)` call style and whatever admin-alert mechanism it already uses exactly.

- [ ] **Step 8: Add a companion scheduled job `alert_message_queue_fail_open`**

Following the exact pattern found in Step 7, add a job that:
1. Reads `get_fail_open_count()` from `app.services.message_queue`.
2. Compares it against a module-level `_last_fail_open_count` in `scheduler.py` (starting at 0).
3. If the delta exceeds 5 since the last check, sends an admin alert via the same mechanism `alert_failed_messages` already uses, with text: `f"⚠️ Message queue fail-open rate elevated: {delta} messages processed without idempotency guarantee since last check."`
4. Updates `_last_fail_open_count` to the current value regardless of whether it alerted.

Register it with the scheduler using the same `add_job(...)` call signature/interval style as `alert_failed_messages`.

- [ ] **Step 9: Find the existing test for `alert_failed_messages` and write a symmetric test for the new job**

Run: `Grep "alert_failed_messages" tests/` to find its test, read it in full, then write `test_alert_message_queue_fail_open_triggers_above_threshold` and `test_alert_message_queue_fail_open_silent_below_threshold` in the same test file, following its exact mocking style (same admin-alert mock target, same scheduler-job-invocation pattern).

- [ ] **Step 10: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add app/services/message_queue.py app/services/scheduler.py tests/test_message_queue.py
git commit -m "feat(reliability): alert admin on sustained idempotency-gate fail-open events"
```

---

## Task 9: Remove dead idempotency query in webhook processing

**Severity:** MEDIUM (no security impact — dead code cleanup).

**Note:** If Task 5 was completed first, this dead `existing_event` query was already deleted as part of Task 5 Step 7's rewrite of the booking-lookup section. **Check before doing anything in this task.**

**Files:**
- Modify: `app/services/payment.py` (only if `existing_event` still present)

**Interfaces:** none — pure deletion, no signature changes.

- [ ] **Step 1: Check whether the dead code still exists**

Run: `Grep "existing_event" app/services/payment.py`

- [ ] **Step 2: If found, delete it**

Remove the block:
```python
        existing_event = (
            supabase.table("payment_events")
            .select("id")
            .eq("event_type", "confirmed")
            .eq("raw_payload->>payment_id", payment_id)
            .execute()
        )

        # Alternative idempotency: check if any confirmed event has this payment_id
```
leaving only the `existing_confirmed` query and its surrounding comment.

- [ ] **Step 3: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: PASS (no test depends on the unused `existing_event` variable, since it was never read).

- [ ] **Step 4: Commit (only if Step 2 made a change)**

```bash
git add app/services/payment.py
git commit -m "refactor(payment): remove dead idempotency query in webhook processing"
```

---

## Task 10: Document the accepted CSP `unsafe-inline` trade-off

**Severity:** MEDIUM.

**Files:**
- Modify: `app/utils/security.py:336-351` (`SECURITY_HEADERS`)

**Interfaces:** none — comment-only change, no code behavior change.

- [ ] **Step 1: Confirm the actual scope of inline JS in the admin panel before deciding the fix**

Run: `Grep -c "<script>" admin/index.html` and `Grep -c "onclick=" admin/index.html`. `admin/index.html` is a single static HTML file (per `CLAUDE.md`'s description of `docs/06-admin.md`), not served through a template engine — so nonce-based CSP (which requires per-request server-side injection into the HTML) is not achievable without converting this file to a template, which is a much larger refactor than a MEDIUM finding warrants. If the grep counts confirm dozens of inline handlers (expected), proceed with the documentation-only fix below rather than attempting nonce injection or extracting all inline JS to external files.

- [ ] **Step 2: Replace the CSP comment in `app/utils/security.py`**

Change the `"script-src 'self' 'unsafe-inline'; "` line to:

```python
        # 'unsafe-inline' required: admin/index.html is a single static file
        # with inline <script>/onclick handlers, not templated — extracting
        # to external JS is a larger refactor than this fix warrants. CSP
        # still blocks all THIRD-PARTY script sources, which is the main
        # protection this header provides.
        "script-src 'self' 'unsafe-inline'; "
```

- [ ] **Step 3: Run the full test suite to confirm nothing broke**

Run: `python -m pytest tests/ -q`
Expected: PASS (no behavior changed).

- [ ] **Step 4: Commit**

```bash
git add app/utils/security.py
git commit -m "docs(security): document accepted CSP unsafe-inline trade-off for static admin panel"
```

---

## Task 11: Persist orphan webhook security events to the database

**Severity:** LOW.

**Files:**
- Create: `migrations/024_webhook_security_events.sql`
- Modify: `app/services/payment.py:1148-1170` (`_log_payment_event_raw`)
- Test: `tests/test_payment.py` (extend)

**Interfaces:**
- Consumes: none new.
- Produces: `_log_payment_event_raw(booking_id: Optional[str], event_type: str, payload: dict) -> None` — signature unchanged. When `booking_id` is `None`, now inserts into a new `webhook_security_events` table instead of returning without any DB write.

- [ ] **Step 1: Write the migration**

```sql
-- Migration 024: Persist orphan webhook security events (no booking_id)
-- Run in Supabase SQL Editor
--
-- _log_payment_event_raw() previously skipped the payment_events insert
-- entirely for orphan events (booking_id=None) — the exact case for
-- signature-verification failures, i.e. the most security-relevant event
-- type — falling back to app logs only, which are weaker for forensic
-- replay after a suspected attack (rotation, retention, no structured query).

CREATE TABLE IF NOT EXISTS webhook_security_events (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    event_type TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhook_security_events_created_at
    ON webhook_security_events (created_at DESC);

-- Verify
SELECT table_name FROM information_schema.tables
WHERE table_name = 'webhook_security_events';
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_payment.py` in a new class after `TestPaymentWebhookProcessing`:

```python
class TestOrphanWebhookEventPersistence:
    def test_log_payment_event_raw_persists_orphan_events(self):
        """Events with no booking_id (e.g. signature failures) must now be
        written to webhook_security_events, not just logged."""
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table

            service._log_payment_event_raw(
                None, "signature_failed", {"body_length": 42}
            )

            mock_sb.table.assert_called_once_with("webhook_security_events")
            inserted = mock_table.insert.call_args[0][0]
            assert inserted["event_type"] == "signature_failed"

    def test_log_payment_event_raw_still_uses_payment_events_when_booking_id_present(self):
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table

            service._log_payment_event_raw(
                "booking-1", "webhook_received", {"payment_id": "pay_1"}
            )

            mock_sb.table.assert_called_once_with("payment_events")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_payment.py::TestOrphanWebhookEventPersistence -v`
Expected: FAIL — current code returns early on `booking_id is None` without ever calling `supabase.table(...)`.

- [ ] **Step 4: Fix `app/services/payment.py` — replace lines 1148-1170**

```python
    def _log_payment_event_raw(
        self, booking_id: Optional[str], event_type: str, payload: dict
    ) -> None:
        """Log payment event even when booking_id might be None (e.g. signature failures).

        Orphan events (no booking_id) go to webhook_security_events instead
        of payment_events, since payment_events.booking_id is a required FK —
        this keeps signature-failure/spoofing-attempt events queryable in the
        DB for forensic replay instead of only living in rotated app logs.
        """
        try:
            if booking_id:
                supabase.table("payment_events").insert(
                    {
                        "booking_id": booking_id,
                        "event_type": event_type,
                        "raw_payload": json.dumps(payload, default=str),
                    }
                ).execute()
            else:
                supabase.table("webhook_security_events").insert(
                    {
                        "event_type": event_type,
                        "raw_payload": json.dumps(payload, default=str),
                    }
                ).execute()
        except Exception as e:
            logger.error(
                f"CRITICAL: Failed to write raw payment_event ({event_type}): {e}"
            )
            logger.warning(
                f"Payment event without persisted record: {event_type} — "
                f"{json.dumps(payload, default=str)}"
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_payment.py::TestOrphanWebhookEventPersistence -v`
Expected: PASS.

- [ ] **Step 6: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add migrations/024_webhook_security_events.sql app/services/payment.py tests/test_payment.py
git commit -m "fix(payment): persist orphan webhook security events instead of log-only"
```

**Manual step (tell the user):** Run `migrations/024_webhook_security_events.sql` in the Supabase SQL Editor for every environment.

---

## Final Verification

- [ ] Run the complete suite one more time after all 11 tasks: `python -m pytest tests/ -q` — expect all tests green.
- [ ] Run `Grep "existing_event\b" app/services/payment.py` to confirm Task 9's dead code is gone regardless of which task removed it.
- [ ] Run `Grep "api.razorpay.com/v1/checkout/embedded" app/` to confirm the broken payment link URL is fully gone.
- [ ] Apply, in order, the four new migrations to Supabase (staging first, then production) — `021_unique_queue_token.sql`, `022_razorpay_payment_link_id.sql`, `023_atomic_rate_limit_rpc.sql`, `024_webhook_security_events.sql`.
- [ ] Manually smoke-test the Task 5 payment link fix against Razorpay TEST mode end-to-end before deploying to production — this is the one fix in this plan that mocked tests cannot fully validate.
- [ ] Confirm `META_APP_SECRET` is set in every real deployment's env vars before deploying Task 3 (fail-closed webhook verification) — otherwise the bot will stop receiving WhatsApp messages.

---

## Self-Review

**Spec coverage:** All 11 findings from the audit have a task (Findings 1-8, 10-11 get dedicated tasks; Finding 9 is folded into Task 5's rewrite with an explicit dedup check in Task 9's Step 1 to avoid double-editing the same lines if both tasks are executed).

**Placeholder scan:** Task 10 originally risked a "nonce injection" placeholder for static HTML with no templating support — resolved by explicitly scoping down to a documented trade-off instead of prescribing unbuildable nonce injection into files that aren't templates. Tasks 5 and 8 direct a fresh `Read` of current file contents before editing (rather than hard-coding possibly-drifted line numbers) since those files are large and line numbers shift — this is a deliberate, explicit instruction, not a placeholder. No other TBD/TODO left in this plan.

**Type consistency:** `check_in_appointment`/`call_next_patient` signatures unchanged across tasks. `admin_confirm_booking`/`admin_reject_booking` signature change (added `clinic_id`) is consistent between `payment.py` (Task 1) and its only caller `admin.py` (also Task 1, same task). `process_payment_webhook`'s new `alert_limiter`/`alert_key` kwargs (Task 4) are consistent with their only caller `razorpay_webhook.py` (also Task 4). `_build_payment_link` → `_create_payment_link` rename (Task 5) is consistent between its definition and its one call site in `create_booking_with_payment`, both edited in the same task. `PersistentRateLimiter.check_and_record` (Task 7) is consistent between its definition and its one caller in `admin.py`'s `verify_credentials`, both edited in the same task.
