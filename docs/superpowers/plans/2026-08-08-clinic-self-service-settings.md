# Clinic Self-Service Settings + Plan-Aware Admin Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let clinic admins self-service their own Razorpay keys and choose full/partial/no-payment booking, and make the admin panel show only the tabs each clinic's plan actually includes — without touching doctor management, clinic onboarding, CallMedex, or the AI conversation engine.

**Architecture:** Everything new lives in the existing per-clinic `clinics.config` JSONB (no new tables). Two new admin endpoints (`GET/PUT /admin/settings/payment`) reuse the existing `clinic_admins` auth + `enforce_clinic_access` tenant isolation. A new `GET /admin/me` exposes the existing `PLAN_FEATURES`/`has_feature` registry to the frontend for the first time. The WhatsApp booking flow's binary "Razorpay configured?" check becomes a three-way `resolve_payment_mode()` call that fails safe to free direct booking.

**Tech Stack:** FastAPI, Supabase (Postgres), Pydantic, vanilla JS (`admin/index.html`), pytest + `unittest.mock`.

## Global Constraints

- Never touch `app/integrations/callmedex/**` or the conversational AI engine (`app/services/ai_engine.py`) — out of scope per the design spec.
- Never touch doctor/fee/timing management (`/admin/doctors`, `/admin/leaves`) or clinic onboarding (`app/routers/clinics.py`) — already correct, out of scope.
- Every existing clinic must keep working identically with zero config changes (back-compat default: `payment_mode` absent → `full` if Razorpay keys are set, else `none`).
- Money amounts are always integer paise — never floats (existing invariant in `app/services/payment.py`).
- Follow the existing test convention in this repo: call router functions directly with a constructed `AdminUser`/mocked `supabase`, not `TestClient` + real HTTP (see `tests/test_admin_staff_identity.py`).
- Spec reference: `docs/superpowers/specs/2026-08-08-clinic-self-service-settings-design.md`

---

### Task 1: Fix stale `clinics.plan` CHECK constraint

**Files:**
- Create: `migrations/016_fix_plan_constraint.sql`

**Interfaces:**
- Produces: `clinics.plan` CHECK constraint now allows `soloclinic|diagstream|essential|polyclinic|enterprise` (matches every other file in the codebase already using these values).

This repo's migrations are hand-run in the Supabase SQL Editor (see the header comment in every existing file under `migrations/`) — there is no local Postgres test harness for them, so verification here is a manual query, matching the existing convention (no `tests/test_migrations.py` exists in this repo).

- [ ] **Step 1: Write the migration**

```sql
-- Migration 016: Fix clinics.plan CHECK constraint drift
-- Run in Supabase SQL Editor
--
-- migrations/006_alter_clinics_plan.sql created the constraint with
-- ('basic','pro','enterprise'), but every application code path (Pydantic
-- models in app/routers/clinics.py, the PLAN_FEATURES registry in
-- app/services/tenant.py, admin/platform.html) has since moved to
-- ('soloclinic','diagstream','essential','polyclinic','enterprise').
-- This migration brings the DB constraint in line with the code.

DO $$
DECLARE
    bad_count INT;
    con RECORD;
BEGIN
    -- Abort loudly if any clinic holds a value outside the known old+new sets —
    -- never silently reclassify data we don't recognize.
    SELECT COUNT(*) INTO bad_count FROM clinics
    WHERE plan NOT IN ('basic', 'pro', 'enterprise',
                        'soloclinic', 'diagstream', 'essential', 'polyclinic');
    IF bad_count > 0 THEN
        RAISE EXCEPTION 'Found % clinics with unexpected plan value — resolve before migrating', bad_count;
    END IF;

    -- Map legacy tier values to the closest clinic-type equivalent.
    -- 'enterprise' needs no mapping — it exists in both the old and new sets.
    UPDATE clinics SET plan = 'essential' WHERE plan IN ('basic', 'pro');

    -- Drop whichever CHECK constraint currently governs the plan column —
    -- looked up dynamically so this works regardless of the auto-generated
    -- constraint name in any given environment.
    FOR con IN
        SELECT pg_constraint.conname
        FROM pg_constraint
        JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
        WHERE pg_class.relname = 'clinics'
          AND pg_constraint.contype = 'c'
          AND pg_get_constraintdef(pg_constraint.oid) LIKE '%plan%'
    LOOP
        EXECUTE format('ALTER TABLE clinics DROP CONSTRAINT %I', con.conname);
    END LOOP;

    ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check
        CHECK (plan IN ('soloclinic', 'diagstream', 'essential', 'polyclinic', 'enterprise'));
END $$;

-- Verify — every row should now show one of the 5 new plan values
SELECT id, name, plan FROM clinics ORDER BY created_at;
```

- [ ] **Step 2: Manual verification**

Run this migration in the Supabase SQL Editor against a staging/dev project first. Confirm the final `SELECT` shows only `soloclinic|diagstream|essential|polyclinic|enterprise` in the `plan` column, then run it against production.

- [ ] **Step 3: Commit**

```bash
git add migrations/016_fix_plan_constraint.sql
git commit -m "fix(db): align clinics.plan CHECK constraint with soloclinic/diagstream/essential/polyclinic/enterprise values already used in app code"
```

---

### Task 2: Expose the plan/feature registry as a flat list

**Files:**
- Modify: `app/services/tenant.py` (add after the `PLAN_FEATURES` dict, ~line 252)
- Test: `tests/test_plan_features.py` (create)

**Interfaces:**
- Consumes: existing `PLAN_FEATURES: dict[str, set[str]]` and `has_feature(clinic, feature) -> bool` (both already defined in `app/services/tenant.py`).
- Produces: `ALL_FEATURES: list[str]` — sorted union of every named feature across all plans (excludes the `"*"` enterprise sentinel). Task 5 (`GET /admin/me`) imports this.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_plan_features.py
"""Tests for the flat ALL_FEATURES list derived from PLAN_FEATURES."""

from app.services.tenant import ALL_FEATURES, PLAN_FEATURES, has_feature


def test_all_features_excludes_wildcard_sentinel():
    assert "*" not in ALL_FEATURES


def test_all_features_is_sorted_and_deduplicated():
    assert ALL_FEATURES == sorted(set(ALL_FEATURES))


def test_all_features_contains_every_named_plan_feature():
    named = {f for feats in PLAN_FEATURES.values() for f in feats if f != "*"}
    assert set(ALL_FEATURES) == named


def test_soloclinic_features_subset_of_all_features():
    clinic = {"plan": "soloclinic"}
    resolved = [f for f in ALL_FEATURES if has_feature(clinic, f)]
    assert "booking" in resolved
    assert "lab_reports" not in resolved  # soloclinic doesn't have this feature


def test_diagstream_has_lab_reports_not_booking():
    clinic = {"plan": "diagstream"}
    resolved = [f for f in ALL_FEATURES if has_feature(clinic, f)]
    assert "lab_reports" in resolved
    assert "booking" not in resolved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_plan_features.py -v`
Expected: FAIL with `ImportError: cannot import name 'ALL_FEATURES'`

- [ ] **Step 3: Add `ALL_FEATURES` to `app/services/tenant.py`**

Insert immediately after the closing `}` of the `PLAN_FEATURES` dict (currently ends at line 252, right before `def has_feature(clinic: dict, feature: str) -> bool:`):

```python
# Flat, sorted list of every named feature across all plans — excludes the
# "*" enterprise wildcard sentinel. Used by GET /admin/me (app/routers/admin.py)
# to tell the admin panel frontend which tabs to show, without duplicating
# this registry in JS.
ALL_FEATURES: list[str] = sorted(
    {feature for features in PLAN_FEATURES.values() for feature in features if feature != "*"}
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_plan_features.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/tenant.py tests/test_plan_features.py
git commit -m "feat(tenant): expose ALL_FEATURES flat list for the admin panel's plan-aware UI"
```

---

### Task 3: `resolve_payment_mode()` + partial-deposit support in `PaymentService`

**Files:**
- Modify: `app/services/payment.py` (add helper after `get_razorpay_creds`, ~line 55; modify `create_booking_with_payment`, ~lines 68-99)
- Test: `tests/test_payment.py` (extend)

**Interfaces:**
- Consumes: existing `get_razorpay_creds(clinic: dict) -> tuple[str, str, str]` (already in `app/services/payment.py`).
- Produces:
  - `resolve_payment_mode(clinic: dict) -> tuple[str, int]` — returns `(mode, percent)` where `mode` is `"full"|"partial"|"none"` and `percent` is `100` unless `mode == "partial"`.
  - `PaymentService.create_booking_with_payment(..., deposit_percent: int = 100)` — new optional kwarg; scales the charged `amount_paise`.
  - Task 4 (`conversation.py`) calls both of these.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_payment.py`, after the `WEBHOOK_SECRET` constant and helper functions (before `class TestWebhookSignatureVerification:`, ~line 96):

```python
class TestResolvePaymentMode:
    """Test the full/partial/none payment mode resolution."""

    def test_defaults_to_full_when_keys_configured_no_mode_set(self):
        from app.services.payment import resolve_payment_mode

        clinic = {"config": {"razorpay_key_id": "rzp_1", "razorpay_key_secret": "secret1"}}
        mode, percent = resolve_payment_mode(clinic)
        assert mode == "full"
        assert percent == 100

    def test_defaults_to_none_when_no_keys_and_no_mode_set(self):
        from app.services.payment import resolve_payment_mode

        clinic = {"config": {}}
        mode, percent = resolve_payment_mode(clinic)
        assert mode == "none"
        assert percent == 100

    def test_explicit_none_with_keys_configured_stays_none(self):
        from app.services.payment import resolve_payment_mode

        clinic = {
            "config": {
                "razorpay_key_id": "rzp_1",
                "razorpay_key_secret": "secret1",
                "payment_mode": "none",
            }
        }
        mode, percent = resolve_payment_mode(clinic)
        assert mode == "none"
        assert percent == 100

    def test_partial_with_keys_configured_returns_percent(self):
        from app.services.payment import resolve_payment_mode

        clinic = {
            "config": {
                "razorpay_key_id": "rzp_1",
                "razorpay_key_secret": "secret1",
                "payment_mode": "partial",
                "payment_deposit_percent": 20,
            }
        }
        mode, percent = resolve_payment_mode(clinic)
        assert mode == "partial"
        assert percent == 20

    def test_full_mode_without_keys_fails_safe_to_none(self):
        from app.services.payment import resolve_payment_mode

        clinic = {"config": {"payment_mode": "full"}}
        mode, percent = resolve_payment_mode(clinic)
        assert mode == "none"
        assert percent == 100

    def test_partial_mode_without_keys_fails_safe_to_none(self):
        from app.services.payment import resolve_payment_mode

        clinic = {
            "config": {"payment_mode": "partial", "payment_deposit_percent": 20}
        }
        mode, percent = resolve_payment_mode(clinic)
        assert mode == "none"
        assert percent == 100
```

Add to `tests/test_payment.py`, inside `class TestBookingCreation:` right after `test_successful_booking_creates_order` (~line 336, before `class TestRefundFlow:`):

```python
    @pytest.mark.asyncio
    async def test_partial_deposit_scales_amount(self):
        """deposit_percent < 100 should charge that fraction of the full fee."""
        from app.services.payment import PaymentService

        service = PaymentService()

        mock_booking = {"id": "new-booking-uuid", "booking_ref": "MC-2026-5679"}
        mock_order = {"id": "order_partial_test"}

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ), patch.object(
            service,
            "_create_razorpay_order",
            new_callable=AsyncMock,
            return_value=mock_order,
        ), patch.object(
            service, "_log_payment_event"
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[])
            )

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="General Medicine",
                doctor_name="Dr. Test",
                appointment_date="2026-07-05",
                appointment_time="10:00",
                deposit_percent=20,
            )

        assert result["success"] is True
        # 50000 paise full fee * 20% = 10000 paise deposit
        assert result["amount_paise"] == 10000

    @pytest.mark.asyncio
    async def test_full_deposit_percent_default_charges_full_fee(self):
        """Omitting deposit_percent must charge the full fee (back-compat)."""
        from app.services.payment import PaymentService

        service = PaymentService()

        mock_booking = {"id": "new-booking-uuid", "booking_ref": "MC-2026-5680"}
        mock_order = {"id": "order_full_test"}

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ), patch.object(
            service,
            "_create_razorpay_order",
            new_callable=AsyncMock,
            return_value=mock_order,
        ), patch.object(
            service, "_log_payment_event"
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[])
            )

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="General Medicine",
                doctor_name="Dr. Test",
                appointment_date="2026-07-05",
                appointment_time="10:00",
            )

        assert result["amount_paise"] == 50000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_payment.py -v -k "resolve_payment_mode or deposit or full_deposit_percent_default"`
Expected: FAIL — `resolve_payment_mode` doesn't exist yet; `deposit_percent` is an unexpected keyword argument.

- [ ] **Step 3: Add `resolve_payment_mode()` to `app/services/payment.py`**

Insert immediately after `get_razorpay_creds` (currently ends at line 55, right before `class PaymentService:`):

```python
def resolve_payment_mode(clinic: dict) -> tuple[str, int]:
    """Resolve a clinic's payment mode with a fail-safe default.

    Returns:
        (mode, percent) where mode is "full" | "partial" | "none" and percent
        is 100 unless mode == "partial" (then it's the configured deposit %).

    Back-compat default: if config.payment_mode is unset, every existing
    clinic behaves exactly as it did before this feature existed — "full"
    if Razorpay keys are configured, "none" if they aren't.

    Fail-safe: "full"/"partial" is never returned without working keys, so a
    clinic that saves a payment-gated mode and later loses its Razorpay keys
    falls back to free direct booking instead of silently blocking bookings.
    """
    cfg = clinic.get("config") or {}
    key_id, key_secret, _ = get_razorpay_creds(clinic)
    configured = bool(key_id and key_secret)

    mode = cfg.get("payment_mode") or ("full" if configured else "none")
    if mode in ("full", "partial") and not configured:
        mode = "none"

    percent = cfg.get("payment_deposit_percent", 100) if mode == "partial" else 100
    return mode, percent
```

- [ ] **Step 4: Add `deposit_percent` to `create_booking_with_payment`**

In `app/services/payment.py`, replace this (lines 68-99):

```python
    async def create_booking_with_payment(
        self,
        clinic_id: str,
        patient_phone: str,
        patient_name: str,
        department: str,
        doctor_name: str,
        appointment_date: str,
        appointment_time: str,
        symptoms: str = "",
        patient_id: Optional[str] = None,
        clinic: Optional[dict] = None,
        branch_id: Optional[str] = None,
        branch_name: Optional[str] = None,
    ) -> dict:
        """Create a pending_payment booking and a Razorpay order.

        Args:
            clinic: Optional full clinic dict. If provided, per-clinic Razorpay
                    credentials are used. Falls back to global settings if None.
            branch_id: Optional branch UUID for multi-branch clinics.
            branch_name: Optional branch display name for multi-branch clinics.

        Returns:
            dict with keys: success, booking_id, razorpay_order_id,
            payment_link, amount_paise, hold_expires_at, reason
        """
        # ── Resolve per-clinic Razorpay credentials ──
        key_id, key_secret, _ = get_razorpay_creds(clinic or {})

        # ── Determine fee from doctor's consultation_fee ──
        amount_paise = await self._get_doctor_fee_paise(clinic_id, doctor_name)
```

with:

```python
    async def create_booking_with_payment(
        self,
        clinic_id: str,
        patient_phone: str,
        patient_name: str,
        department: str,
        doctor_name: str,
        appointment_date: str,
        appointment_time: str,
        symptoms: str = "",
        patient_id: Optional[str] = None,
        clinic: Optional[dict] = None,
        branch_id: Optional[str] = None,
        branch_name: Optional[str] = None,
        deposit_percent: int = 100,
    ) -> dict:
        """Create a pending_payment booking and a Razorpay order.

        Args:
            clinic: Optional full clinic dict. If provided, per-clinic Razorpay
                    credentials are used. Falls back to global settings if None.
            branch_id: Optional branch UUID for multi-branch clinics.
            branch_name: Optional branch display name for multi-branch clinics.
            deposit_percent: 1-100. When < 100, only this fraction of the
                    doctor's full consultation_fee is charged now (the rest is
                    collected at the clinic). Defaults to 100 (full fee).

        Returns:
            dict with keys: success, booking_id, razorpay_order_id,
            payment_link, amount_paise, hold_expires_at, reason
        """
        # ── Resolve per-clinic Razorpay credentials ──
        key_id, key_secret, _ = get_razorpay_creds(clinic or {})

        # ── Determine fee from doctor's consultation_fee, scaled for deposits ──
        amount_paise = await self._get_doctor_fee_paise(clinic_id, doctor_name)
        if deposit_percent < 100:
            amount_paise = round(amount_paise * deposit_percent / 100)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_payment.py -v`
Expected: All tests PASS, including the new `TestResolvePaymentMode` class and the two new `TestBookingCreation` tests.

- [ ] **Step 6: Commit**

```bash
git add app/services/payment.py tests/test_payment.py
git commit -m "feat(payment): add resolve_payment_mode() and partial-deposit support to create_booking_with_payment"
```

---

### Task 4: Wire `resolve_payment_mode` into the booking confirmation flow

**Files:**
- Modify: `app/services/conversation.py:2172-2303` (inside `ConversationManager._handle_confirming_booking`)
- Test: `tests/test_conversation_payment_mode.py` (create)

**Interfaces:**
- Consumes: `resolve_payment_mode(clinic) -> tuple[str, int]` and `PaymentService.create_booking_with_payment(..., deposit_percent=...)` from Task 3.
- Produces: no new public interface — this task is wiring + the deposit message line.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conversation_payment_mode.py
"""Test that _handle_confirming_booking branches correctly on payment_mode."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("WABA_DISPLAY_NAME", "Test Hospital")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("HOSPITAL_NAME", "City Care Hospital")
os.environ.setdefault("HOSPITAL_EMERGENCY_NUMBER", "108")
os.environ.setdefault("HOSPITAL_PHONE", "+919876543210")
os.environ.setdefault("HOSPITAL_MAPS_LINK", "https://maps.google.com")
os.environ.setdefault("HOSPITAL_WEBSITE", "https://test.hospital.com")
os.environ.setdefault("HOSPITAL_PRIVACY_POLICY_URL", "https://test.hospital.com/privacy")
os.environ.setdefault("HOSPITAL_ADDRESS", "Test Address")
os.environ.setdefault("HOSPITAL_LANDMARK", "Test Landmark")
os.environ.setdefault("BOOKING_REF_PREFIX", "MC")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

mock_supabase = MagicMock()
mock_db_module = MagicMock()
mock_db_module.supabase = mock_supabase
mock_db_module.log_analytics_event = AsyncMock()
sys.modules["app.database"] = mock_db_module

from app.services.conversation import ConversationManager  # noqa: E402


def _clinic(config: dict) -> dict:
    return {"id": "clinic-1", "name": "Test Clinic", "config": config}


def _context() -> dict:
    return {
        "doctor_name": "Dr. Test",
        "appointment_date": "2026-07-05",
        "appointment_time": "10:00",
        "department": "General Medicine",
        "booking_name": "Patient",
    }


@pytest.mark.asyncio
async def test_partial_mode_sends_deposit_note_and_scaled_amount():
    manager = ConversationManager()
    manager.whatsapp.send_text = AsyncMock()
    manager.update_state = AsyncMock()

    clinic = _clinic(
        {
            "razorpay_key_id": "rzp_1",
            "razorpay_key_secret": "secret1",
            "payment_mode": "partial",
            "payment_deposit_percent": 20,
        }
    )

    with patch(
        "app.services.payment.payment_service.create_booking_with_payment",
        new_callable=AsyncMock,
        return_value={
            "success": True,
            "booking_id": "booking-1",
            "booking_ref": "MC-1",
            "razorpay_order_id": "order-1",
            "payment_link": "https://razorpay.example/pay",
            "amount_paise": 10000,
            "hold_expires_at": "2026-07-05T10:00:00Z",
        },
    ) as mock_create:
        await manager._handle_confirming_booking(
            clinic, "+919876543210", "yes", "confirm_booking", _context(), {"id": "patient-1"}, "en"
        )

    # deposit_percent scaled correctly reaches the payment service
    assert mock_create.call_args.kwargs["deposit_percent"] == 20

    sent_message = manager.whatsapp.send_text.call_args[0][2]
    assert "20%" in sent_message
    assert "80%" in sent_message


@pytest.mark.asyncio
async def test_none_mode_skips_payment_and_books_directly():
    manager = ConversationManager()
    manager.whatsapp.send_text = AsyncMock()
    manager.update_state = AsyncMock()

    clinic = _clinic({"payment_mode": "none"})

    with patch(
        "app.services.appointment.book_appointment",
        new_callable=AsyncMock,
        return_value={
            "success": True,
            "appointment": {"booking_ref": "MC-2"},
        },
    ) as mock_book, patch(
        "app.services.payment.payment_service.create_booking_with_payment",
        new_callable=AsyncMock,
    ) as mock_create:
        await manager._handle_confirming_booking(
            clinic, "+919876543210", "yes", "confirm_booking", _context(), {"id": "patient-1"}, "en"
        )

    mock_book.assert_called_once()
    mock_create.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_conversation_payment_mode.py -v`
Expected: FAIL — `deposit_percent` not passed to `create_booking_with_payment`, and no `"20%"`/`"80%"` text is sent yet.

- [ ] **Step 3: Replace the binary Razorpay check with `resolve_payment_mode`**

In `app/services/conversation.py`, replace (lines 2175-2198):

```python
            # ── Check if Razorpay is configured (per-clinic first, global fallback) ──
            from app.services.payment import get_razorpay_creds

            _rz_key_id, _rz_key_secret, _ = get_razorpay_creds(clinic)
            razorpay_configured = bool(_rz_key_id and _rz_key_secret)

            if razorpay_configured:
                # ═══ PATH A: Payment-gated booking ═══
                from app.services.payment import payment_service

                result = await payment_service.create_booking_with_payment(
                    clinic_id=clinic["id"],
                    patient_phone=phone,
                    patient_name=context.get("booking_name", "Patient"),
                    department=context.get("department", "General Medicine"),
                    doctor_name=context["doctor_name"],
                    appointment_date=context["appointment_date"],
                    appointment_time=context["appointment_time"],
                    symptoms=context.get("symptoms", ""),
                    patient_id=patient.get("id"),
                    clinic=clinic,
                    branch_id=context.get("branch_id"),
                    branch_name=context.get("branch_name"),
                )
```

with:

```python
            # ── Resolve this clinic's payment mode: full / partial / none ──
            from app.services.payment import resolve_payment_mode

            payment_mode, deposit_percent = resolve_payment_mode(clinic)

            if payment_mode in ("full", "partial"):
                # ═══ PATH A: Payment-gated booking ═══
                from app.services.payment import payment_service

                result = await payment_service.create_booking_with_payment(
                    clinic_id=clinic["id"],
                    patient_phone=phone,
                    patient_name=context.get("booking_name", "Patient"),
                    department=context.get("department", "General Medicine"),
                    doctor_name=context["doctor_name"],
                    appointment_date=context["appointment_date"],
                    appointment_time=context["appointment_time"],
                    symptoms=context.get("symptoms", ""),
                    patient_id=patient.get("id"),
                    clinic=clinic,
                    branch_id=context.get("branch_id"),
                    branch_name=context.get("branch_name"),
                    deposit_percent=deposit_percent,
                )
```

- [ ] **Step 4: Add the deposit note to the payment confirmation message**

Replace (lines 2200-2218):

```python
                if result["success"]:
                    amount_rupees = result["amount_paise"] / 100
                    date_display = datetime.strptime(
                        context["appointment_date"], "%Y-%m-%d"
                    ).strftime("%d %b %Y")

                    payment_msg = {
                        "en": (
                            f"💳 *Payment Required to Confirm Booking*\n\n"
                            f"👨‍⚕️ Doctor: {context['doctor_name']}\n"
                            f"📅 Date: {date_display}\n"
                            f"🕐 Time: {context['appointment_time']}\n"
                            f"💰 Amount: ₹{amount_rupees:.0f}\n\n"
                            f"⏱️ *This slot is held for 10 minutes.* Pay before it expires.\n\n"
                            f"👉 Click below to pay securely via Razorpay:\n"
                            f"{result['payment_link']}\n\n"
                            f"_Amount is refundable if cancelled {settings.refund_window_hours}+ hours before appointment. "
                            f"No-show bookings are non-refundable._"
                        ),
```

with:

```python
                if result["success"]:
                    amount_rupees = result["amount_paise"] / 100
                    date_display = datetime.strptime(
                        context["appointment_date"], "%Y-%m-%d"
                    ).strftime("%d %b %Y")

                    deposit_note_en = (
                        f"_This is a {deposit_percent}% deposit — the remaining "
                        f"{100 - deposit_percent}% is payable at the clinic._\n\n"
                        if payment_mode == "partial"
                        else ""
                    )
                    deposit_note_hi = (
                        f"_यह {deposit_percent}% जमा राशि है — शेष {100 - deposit_percent}% "
                        f"क्लिनिक में देय है।_\n\n"
                        if payment_mode == "partial"
                        else ""
                    )
                    deposit_note_te = (
                        f"_ఇది {deposit_percent}% డిపాజిట్ — మిగిలిన {100 - deposit_percent}% "
                        f"క్లినిక్‌లో చెల్లించాలి._\n\n"
                        if payment_mode == "partial"
                        else ""
                    )

                    payment_msg = {
                        "en": (
                            f"💳 *Payment Required to Confirm Booking*\n\n"
                            f"👨‍⚕️ Doctor: {context['doctor_name']}\n"
                            f"📅 Date: {date_display}\n"
                            f"🕐 Time: {context['appointment_time']}\n"
                            f"💰 Amount: ₹{amount_rupees:.0f}\n\n"
                            f"{deposit_note_en}"
                            f"⏱️ *This slot is held for 10 minutes.* Pay before it expires.\n\n"
                            f"👉 Click below to pay securely via Razorpay:\n"
                            f"{result['payment_link']}\n\n"
                            f"_Amount is refundable if cancelled {settings.refund_window_hours}+ hours before appointment. "
                            f"No-show bookings are non-refundable._"
                        ),
```

Then replace the `"hi"` block (lines 2219-2230):

```python
                        "hi": (
                            f"💳 *बुकिंग की पुष्टि के लिए भुगतान करें*\n\n"
                            f"👨‍⚕️ डॉक्टर: {context['doctor_name']}\n"
                            f"📅 तारीख: {date_display}\n"
                            f"🕐 समय: {context['appointment_time']}\n"
                            f"💰 राशि: ₹{amount_rupees:.0f}\n\n"
                            f"⏱️ *यह स्लॉट 10 मिनट के लिए होल्ड है।* समय से पहले भुगतान करें।\n\n"
                            f"👉 Razorpay से सुरक्षित भुगतान करें:\n"
                            f"{result['payment_link']}\n\n"
                            f"_अपॉइंटमेंट से {settings.refund_window_hours}+ घंटे पहले रद्द करने पर राशि वापस की जाएगी। "
                            f"नो-शो बुकिंग पर रिफंड नहीं होगा।_"
                        ),
```

with:

```python
                        "hi": (
                            f"💳 *बुकिंग की पुष्टि के लिए भुगतान करें*\n\n"
                            f"👨‍⚕️ डॉक्टर: {context['doctor_name']}\n"
                            f"📅 तारीख: {date_display}\n"
                            f"🕐 समय: {context['appointment_time']}\n"
                            f"💰 राशि: ₹{amount_rupees:.0f}\n\n"
                            f"{deposit_note_hi}"
                            f"⏱️ *यह स्लॉट 10 मिनट के लिए होल्ड है।* समय से पहले भुगतान करें।\n\n"
                            f"👉 Razorpay से सुरक्षित भुगतान करें:\n"
                            f"{result['payment_link']}\n\n"
                            f"_अपॉइंटमेंट से {settings.refund_window_hours}+ घंटे पहले रद्द करने पर राशि वापस की जाएगी। "
                            f"नो-शो बुकिंग पर रिफंड नहीं होगा।_"
                        ),
```

Then replace the `"te"` block (lines 2231-2242):

```python
                        "te": (
                            f"💳 *బుకింగ్ నిర్ధారించడానికి చెల్లింపు అవసరం*\n\n"
                            f"👨‍⚕️ డాక్టర్: {context['doctor_name']}\n"
                            f"📅 తేదీ: {date_display}\n"
                            f"🕐 సమయం: {context['appointment_time']}\n"
                            f"💰 మొత్తం: ₹{amount_rupees:.0f}\n\n"
                            f"⏱️ *ఈ స్లాట్ 10 నిమిషాలు హోల్డ్ చేయబడింది.* గడువులోపు చెల్లించండి.\n\n"
                            f"👉 Razorpay ద్వారా సురక్షితంగా చెల్లించండి:\n"
                            f"{result['payment_link']}\n\n"
                            f"_అపాయింట్‌మెంట్‌కు {settings.refund_window_hours}+ గంటల ముందు రద్దు చేస్తే మొత్తం రీఫండ్ అవుతుంది. "
                            f"నో-షో బుకింగ్‌లు రీఫండ్ కావు._"
                        ),
```

with:

```python
                        "te": (
                            f"💳 *బుకింగ్ నిర్ధారించడానికి చెల్లింపు అవసరం*\n\n"
                            f"👨‍⚕️ డాక్టర్: {context['doctor_name']}\n"
                            f"📅 తేదీ: {date_display}\n"
                            f"🕐 సమయం: {context['appointment_time']}\n"
                            f"💰 మొత్తం: ₹{amount_rupees:.0f}\n\n"
                            f"{deposit_note_te}"
                            f"⏱️ *ఈ స్లాట్ 10 నిమిషాలు హోల్డ్ చేయబడింది.* గడువులోపు చెల్లించండి.\n\n"
                            f"👉 Razorpay ద్వారా సురక్షితంగా చెల్లించండి:\n"
                            f"{result['payment_link']}\n\n"
                            f"_అపాయింట్‌మెంట్‌కు {settings.refund_window_hours}+ గంటల ముందు రద్దు చేస్తే మొత్తం రీఫండ్ అవుతుంది. "
                            f"నో-షో బుకింగ్‌లు రీఫండ్ కావు._"
                        ),
```

- [ ] **Step 5: Add the deposit note to the English fallback message**

Replace (lines 2245-2257):

```python
                    if not payment_msg:
                        payment_msg = (
                            f"💳 *Payment Required to Confirm Booking*\n\n"
                            f"👨‍⚕️ Doctor: {context['doctor_name']}\n"
                            f"📅 Date: {date_display}\n"
                            f"🕐 Time: {context['appointment_time']}\n"
                            f"💰 Amount: ₹{amount_rupees:.0f}\n\n"
                            f"⏱️ *This slot is held for 10 minutes.* Pay before it expires.\n\n"
                            f"👉 Click below to pay securely via Razorpay:\n"
                            f"{result['payment_link']}\n\n"
                            f"_Refundable if cancelled {settings.refund_window_hours}+ hours before appointment. "
                            f"No-show bookings are non-refundable._"
                        )
```

with:

```python
                    if not payment_msg:
                        payment_msg = (
                            f"💳 *Payment Required to Confirm Booking*\n\n"
                            f"👨‍⚕️ Doctor: {context['doctor_name']}\n"
                            f"📅 Date: {date_display}\n"
                            f"🕐 Time: {context['appointment_time']}\n"
                            f"💰 Amount: ₹{amount_rupees:.0f}\n\n"
                            f"{deposit_note_en}"
                            f"⏱️ *This slot is held for 10 minutes.* Pay before it expires.\n\n"
                            f"👉 Click below to pay securely via Razorpay:\n"
                            f"{result['payment_link']}\n\n"
                            f"_Refundable if cancelled {settings.refund_window_hours}+ hours before appointment. "
                            f"No-show bookings are non-refundable._"
                        )
```

- [ ] **Step 6: Update the PATH B comment for clarity**

Replace (line 2303-2304):

```python
            else:
                # ═══ PATH B: Direct booking (Razorpay NOT configured) ═══
```

with:

```python
            else:
                # ═══ PATH B: Direct booking (payment_mode == "none") ═══
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_conversation_payment_mode.py -v`
Expected: PASS (2 passed)

Then run the full payment + conversation-adjacent suite to check for regressions:

Run: `python -m pytest tests/test_payment.py tests/test_conversation_payment_mode.py tests/test_appointment.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add app/services/conversation.py tests/test_conversation_payment_mode.py
git commit -m "feat(conversation): route booking confirmation through resolve_payment_mode (full/partial/none)"
```

---

### Task 5: `GET /admin/me` — expose plan + resolved features to the frontend

**Files:**
- Modify: `app/routers/admin.py` (add import, add endpoint after `resolve_clinic_id_for_write`, ~line 248)
- Test: `tests/test_clinic_settings.py` (create)

**Interfaces:**
- Consumes: `AdminUser`, `verify_credentials`, `get_clinic_by_id` (existing); `ALL_FEATURES`, `has_feature` from Task 2.
- Produces: `GET /admin/me` → `{"username": str, "role": str, "clinic_id": str|None, "plan": str|None, "features": list[str]|None}`. Task 7 (frontend) consumes this shape.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_clinic_settings.py
"""Tests for GET /admin/me and GET/PUT /admin/settings/payment."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers.admin import AdminUser, get_current_admin


@pytest.mark.asyncio
async def test_me_super_admin_gets_no_plan_restriction():
    owner = AdminUser("owner", role="super_admin", clinic_id=None, user_id="super_admin_env")
    result = await get_current_admin(user=owner)
    assert result["role"] == "super_admin"
    assert result.get("plan") is None
    assert result.get("features") is None


@pytest.mark.asyncio
async def test_me_soloclinic_admin_gets_soloclinic_features():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {"id": "clinic-1", "plan": "soloclinic", "whatsapp_number": "+911111111111"}

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        result = await get_current_admin(user=admin)

    assert result["plan"] == "soloclinic"
    assert "booking" in result["features"]
    assert "lab_reports" not in result["features"]


@pytest.mark.asyncio
async def test_me_diagstream_admin_gets_diagstream_features():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    fake_clinic = {"id": "clinic-2", "plan": "diagstream", "whatsapp_number": "+912222222222"}

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        result = await get_current_admin(user=admin)

    assert result["plan"] == "diagstream"
    assert "lab_reports" in result["features"]
    assert "booking" not in result["features"]
    assert "payments_razorpay" not in result["features"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_clinic_settings.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_current_admin'`

- [ ] **Step 3: Add the import and endpoint to `app/routers/admin.py`**

Replace the existing import (line 23):

```python
from app.config import settings
```

with:

```python
from app.config import settings
from app.services.tenant import (
    ALL_FEATURES,
    get_clinic_by_id,
    has_feature,
    invalidate_tenant_cache,
    require_feature,
)
```

Insert after `resolve_clinic_id_for_write` (currently ends at line 247, right before `class LeaveCreate(BaseModel):`):

```python
@router.get("/me")
async def get_current_admin(user: AdminUser = Depends(verify_credentials)):
    """Return the caller's identity plus their clinic's plan and resolved
    feature set, so the admin panel frontend can show/hide tabs without
    duplicating the PLAN_FEATURES registry in JS."""
    if user.role == "super_admin" or not user.clinic_id:
        return {
            "username": user.username,
            "role": user.role,
            "clinic_id": user.clinic_id,
            "plan": None,
            "features": None,
        }

    clinic = await get_clinic_by_id(user.clinic_id)
    plan = clinic.get("plan", "soloclinic")
    features = (
        list(ALL_FEATURES)
        if plan == "enterprise"
        else [f for f in ALL_FEATURES if has_feature(clinic, f)]
    )
    return {
        "username": user.username,
        "role": user.role,
        "clinic_id": user.clinic_id,
        "plan": plan,
        "features": features,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_clinic_settings.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/routers/admin.py tests/test_clinic_settings.py
git commit -m "feat(admin): add GET /admin/me exposing clinic plan and resolved feature set"
```

---

### Task 6: `GET/PUT /admin/settings/payment` — self-service Razorpay + payment mode

**Files:**
- Modify: `app/routers/admin.py` (add `Literal` import, add `PaymentSettingsUpdate` model ~line 300, add endpoints after `/payments/stats` ~line 1031)
- Test: `tests/test_clinic_settings.py` (extend)

**Interfaces:**
- Consumes: `enforce_clinic_access`, `get_clinic_by_id`, `require_feature`, `invalidate_tenant_cache`, `log_admin_action`, `supabase` (all existing/from Task 5).
- Produces: `GET /admin/settings/payment` and `PUT /admin/settings/payment` — no other task depends on these.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_clinic_settings.py`:

```python
from fastapi import HTTPException, Request

from app.routers.admin import get_payment_settings, update_payment_settings, PaymentSettingsUpdate


def _mock_request() -> Request:
    req = MagicMock()
    req.client.host = "127.0.0.1"
    return req


@pytest.mark.asyncio
async def test_get_payment_settings_masks_secret():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {
        "id": "clinic-1",
        "plan": "soloclinic",
        "whatsapp_number": "+911111111111",
        "config": {
            "razorpay_key_id": "rzp_live_abc123",
            "razorpay_key_secret": "supersecretvalue",
            "payment_mode": "full",
        },
    }

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        result = await get_payment_settings(clinic_id="default", user=admin)

    assert result["razorpay_key_id"] == "rzp_live_abc123"
    assert result["razorpay_key_secret_masked"].endswith("alue")
    assert "supersecretvalue" not in result["razorpay_key_secret_masked"]
    assert result["payment_mode"] == "full"


@pytest.mark.asyncio
async def test_update_payment_settings_clinic_admin_updates_own_clinic():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {
        "id": "clinic-1",
        "plan": "soloclinic",
        "whatsapp_number": "+911111111111",
        "config": {},
    }
    updated_clinic = {
        "id": "clinic-1",
        "whatsapp_number": "+911111111111",
        "config": {
            "razorpay_key_id": "rzp_live_new",
            "razorpay_key_secret": "newsecret",
            "payment_mode": "partial",
            "payment_deposit_percent": 25,
        },
    }
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[updated_clinic]
    )

    body = PaymentSettingsUpdate(
        razorpay_key_id="rzp_live_new",
        razorpay_key_secret="newsecret",
        payment_mode="partial",
        payment_deposit_percent=25,
    )

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ), patch("app.routers.admin.supabase", mock_sb), patch(
        "app.routers.admin.invalidate_tenant_cache"
    ):
        result = await update_payment_settings(
            body=body, request=_mock_request(), clinic_id="default", user=admin
        )

    assert result["success"] is True
    sent_config = mock_table.update.call_args[0][0]["config"]
    assert sent_config["payment_mode"] == "partial"
    assert sent_config["payment_deposit_percent"] == 25


@pytest.mark.asyncio
async def test_update_payment_settings_cross_tenant_forbidden():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    body = PaymentSettingsUpdate(payment_mode="none")

    with pytest.raises(HTTPException) as exc:
        await update_payment_settings(
            body=body, request=_mock_request(), clinic_id="clinic-999", user=admin
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_payment_settings_rejects_diagstream_clinic():
    admin = AdminUser("labtech", role="clinic_admin", clinic_id="clinic-2", user_id="user-2")
    fake_clinic = {"id": "clinic-2", "plan": "diagstream", "whatsapp_number": "+912222222222", "config": {}}
    body = PaymentSettingsUpdate(payment_mode="full")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        with pytest.raises(HTTPException) as exc:
            await update_payment_settings(
                body=body, request=_mock_request(), clinic_id="default", user=admin
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_payment_settings_partial_without_percent_rejected():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {"id": "clinic-1", "plan": "soloclinic", "whatsapp_number": "+911111111111", "config": {}}
    body = PaymentSettingsUpdate(payment_mode="partial")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ):
        with pytest.raises(HTTPException) as exc:
            await update_payment_settings(
                body=body, request=_mock_request(), clinic_id="default", user=admin
            )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_payment_settings_empty_secret_does_not_clobber_stored():
    admin = AdminUser("drpatel", role="clinic_admin", clinic_id="clinic-1", user_id="user-1")
    fake_clinic = {
        "id": "clinic-1",
        "plan": "soloclinic",
        "whatsapp_number": "+911111111111",
        "config": {"razorpay_key_id": "rzp_live_existing", "razorpay_key_secret": "existingsecret"},
    }
    mock_sb = MagicMock()
    mock_table = MagicMock()
    mock_sb.table.return_value = mock_table
    mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "clinic-1", "whatsapp_number": "+911111111111", "config": fake_clinic["config"]}]
    )

    body = PaymentSettingsUpdate(razorpay_key_secret="")

    with patch(
        "app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=fake_clinic
    ), patch("app.routers.admin.supabase", mock_sb), patch(
        "app.routers.admin.invalidate_tenant_cache"
    ):
        await update_payment_settings(
            body=body, request=_mock_request(), clinic_id="default", user=admin
        )

    sent_config = mock_table.update.call_args[0][0]["config"]
    assert sent_config["razorpay_key_secret"] == "existingsecret"


def test_payment_settings_update_rejects_out_of_range_percent():
    with pytest.raises(ValueError):
        PaymentSettingsUpdate(payment_mode="partial", payment_deposit_percent=150)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_clinic_settings.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_payment_settings'`

- [ ] **Step 3: Add `Literal` to the typing import**

Replace (line 8):

```python
from typing import Optional
```

with:

```python
from typing import Literal, Optional
```

- [ ] **Step 4: Add the `PaymentSettingsUpdate` model**

Insert after `DoctorUpdate` (currently ends at line 277, right before `class BranchCreate(BaseModel):`):

```python
class PaymentSettingsUpdate(BaseModel):
    """Self-service payment settings a clinic_admin can set for their own
    clinic. Partial update — only fields explicitly sent are changed."""

    razorpay_key_id: Optional[str] = None
    razorpay_key_secret: Optional[str] = None
    razorpay_webhook_secret: Optional[str] = None
    payment_mode: Optional[Literal["full", "partial", "none"]] = None
    payment_deposit_percent: Optional[int] = None

    @field_validator("payment_deposit_percent")
    @classmethod
    def validate_percent_range(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (1 <= v <= 99):
            raise ValueError("payment_deposit_percent must be between 1 and 99")
        return v
```

- [ ] **Step 5: Add the endpoints**

Insert after `get_payment_stats` (currently ends at line 1030, right before the `# CONNECTOR MANAGEMENT` section header comment at line 1033):

```python
@router.get("/settings/payment")
async def get_payment_settings(
    clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)
):
    """Return this clinic's payment settings, with secrets masked."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(effective_clinic_id)
    cfg = clinic.get("config") or {}

    def _mask(secret: Optional[str]) -> Optional[str]:
        if not secret:
            return None
        return "•" * max(0, len(secret) - 4) + secret[-4:]

    key_id = cfg.get("razorpay_key_id")
    key_secret = cfg.get("razorpay_key_secret")
    default_mode = "full" if (key_id and key_secret) else "none"

    return {
        "razorpay_key_id": key_id,
        "razorpay_key_secret_masked": _mask(key_secret),
        "razorpay_webhook_secret_masked": _mask(cfg.get("razorpay_webhook_secret")),
        "payment_mode": cfg.get("payment_mode", default_mode),
        "payment_deposit_percent": cfg.get("payment_deposit_percent"),
    }


@router.put("/settings/payment")
async def update_payment_settings(
    body: PaymentSettingsUpdate,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Self-service update of a clinic's own Razorpay keys and payment mode.
    A clinic_admin may only update their own clinic (enforced via
    enforce_clinic_access); diagstream clinics are rejected — they don't
    take bookings, so payments_razorpay isn't in their feature set."""
    effective_clinic_id = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(effective_clinic_id)
    require_feature(clinic, "payments_razorpay")

    cfg = dict(clinic.get("config") or {})
    updates = body.dict(exclude_unset=True)

    for key in ("razorpay_key_id", "razorpay_key_secret", "razorpay_webhook_secret"):
        if key in updates and updates[key]:
            cfg[key] = updates[key]

    if "payment_mode" in updates and updates["payment_mode"] is not None:
        cfg["payment_mode"] = updates["payment_mode"]
    if (
        "payment_deposit_percent" in updates
        and updates["payment_deposit_percent"] is not None
    ):
        cfg["payment_deposit_percent"] = updates["payment_deposit_percent"]

    final_mode = cfg.get("payment_mode", "full")
    final_percent = cfg.get("payment_deposit_percent")
    if final_mode == "partial" and not (
        isinstance(final_percent, int) and 1 <= final_percent <= 99
    ):
        raise HTTPException(
            status_code=422,
            detail="payment_deposit_percent (1-99) is required when payment_mode is 'partial'",
        )

    result = (
        supabase.table("clinics")
        .update({"config": cfg})
        .eq("id", effective_clinic_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Clinic not found")

    updated_clinic = result.data[0]
    invalidate_tenant_cache(updated_clinic["whatsapp_number"])

    client_ip = request.client.host if request.client else "unknown"
    await log_admin_action(
        user=user,
        action="update_payment_settings",
        resource_type="clinic_config",
        resource_id=effective_clinic_id,
        details={
            "payment_mode": cfg.get("payment_mode"),
            "razorpay_configured": bool(cfg.get("razorpay_key_id")),
        },
        ip_address=client_ip,
    )

    return {"success": True}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_clinic_settings.py -v`
Expected: All PASS (10 passed)

- [ ] **Step 7: Run the full test suite to check for regressions**

Run: `python -m pytest -v`
Expected: All PASS (no regressions in existing `tests/test_rbac.py`, `tests/test_admin_staff_identity.py`, `tests/test_payment.py`, etc.)

- [ ] **Step 8: Commit**

```bash
git add app/routers/admin.py tests/test_clinic_settings.py
git commit -m "feat(admin): add self-service GET/PUT /admin/settings/payment for clinic admins"
```

---

### Task 7: Plan-aware admin panel UI + Payment Settings tab

**Files:**
- Modify: `admin/index.html`

**Interfaces:**
- Consumes: `GET /admin/me` (Task 5), `GET/PUT /admin/settings/payment` (Task 6), existing `api()`/`apiPut()`/`msg()`/`toast()` JS helpers, existing `go(page, el)` nav function and `loaders` map (all in `admin/index.html`).
- Produces: no interface other tasks depend on — this is the last task.

This is a browser UI change with no Python test runner. Verification is manual, per the design spec's testing section (login as different plans, confirm tab visibility) — start the app locally and check in a browser.

- [ ] **Step 1: Add the new nav-link and `data-feature` attributes to existing nav-links**

Replace (lines 663-694):

```html
        <div class="nav-links">
            <div class="nav-link on" data-page="dashboard" onclick="go('dashboard',this)">
                <span class="ico">📊</span> Dashboard
            </div>
            <div class="nav-link" data-page="appointments" onclick="go('appointments',this)">
                <span class="ico">📅</span> Appointments
            </div>
            <div class="nav-link" data-page="doctors" onclick="go('doctors',this)">
                <span class="ico">👨‍⚕️</span> Doctors
            </div>
            <div class="nav-link" data-page="leaves" onclick="go('leaves',this)">
                <span class="ico">📋</span> Leaves
            </div>
            <div class="nav-link" data-page="holidays" onclick="go('holidays',this)">
                <span class="ico">🗓️</span> Holidays
            </div>
            <div class="nav-link" data-page="patients" onclick="go('patients',this)">
                <span class="ico">👥</span> Patients
            </div>
            <div class="nav-link" data-page="labreports" onclick="go('labreports',this)">
                <span class="ico">🧪</span> Lab Reports
            </div>
            <div class="nav-link" data-page="prescriptions" onclick="go('prescriptions',this)">
                <span class="ico">💊</span> Prescriptions
            </div>
            <div class="nav-link" data-page="payments" onclick="go('payments',this)">
                <span class="ico">💳</span> Payments
            </div>
            <div class="nav-link" data-page="branches" onclick="go('branches',this)">
                <span class="ico">🏢</span> Branches
            </div>
        </div>
```

with:

```html
        <div class="nav-links">
            <div class="nav-link on" data-page="dashboard" onclick="go('dashboard',this)">
                <span class="ico">📊</span> Dashboard
            </div>
            <div class="nav-link" data-page="appointments" data-feature="booking" onclick="go('appointments',this)">
                <span class="ico">📅</span> Appointments
            </div>
            <div class="nav-link" data-page="doctors" data-feature="booking" onclick="go('doctors',this)">
                <span class="ico">👨‍⚕️</span> Doctors
            </div>
            <div class="nav-link" data-page="leaves" data-feature="roster_management" onclick="go('leaves',this)">
                <span class="ico">📋</span> Leaves
            </div>
            <div class="nav-link" data-page="holidays" data-feature="roster_management" onclick="go('holidays',this)">
                <span class="ico">🗓️</span> Holidays
            </div>
            <div class="nav-link" data-page="patients" onclick="go('patients',this)">
                <span class="ico">👥</span> Patients
            </div>
            <div class="nav-link" data-page="labreports" data-feature="lab_reports" onclick="go('labreports',this)">
                <span class="ico">🧪</span> Lab Reports
            </div>
            <div class="nav-link" data-page="prescriptions" onclick="go('prescriptions',this)">
                <span class="ico">💊</span> Prescriptions
            </div>
            <div class="nav-link" data-page="payments" data-feature="booking" onclick="go('payments',this)">
                <span class="ico">💳</span> Payments
            </div>
            <div class="nav-link" data-page="paysettings" data-feature="payments_razorpay" onclick="go('paysettings',this)">
                <span class="ico">⚙️</span> Payment Settings
            </div>
            <div class="nav-link" data-page="branches" data-feature="multi_branch" onclick="go('branches',this)">
                <span class="ico">🏢</span> Branches
            </div>
        </div>
```

- [ ] **Step 2: Add the Payment Settings page section**

Insert immediately before the `<!-- ─── BRANCHES ─── -->` comment (currently at line 974):

```html
        <!-- ─── PAYMENT SETTINGS ─── -->
        <div id="pg-paysettings" class="sec">
            <div class="page-head">
                <h2>Payment Settings</h2>
                <p>Manage your own Razorpay keys and how patients pay for bookings</p>
            </div>

            <div class="form-card">
                <h3>💳 Razorpay Credentials</h3>
                <div id="paySettingsMsg"></div>
                <div class="form-row">
                    <div class="field"><label>Key ID</label><input type="text" id="f-payKeyId" placeholder="rzp_live_xxxxxx"></div>
                    <div class="field"><label>Key Secret</label><input type="password" id="f-payKeySecret" placeholder="Leave blank to keep existing"></div>
                </div>
                <div class="form-row">
                    <div class="field"><label>Webhook Secret</label><input type="password" id="f-payWebhookSecret" placeholder="Leave blank to keep existing"></div>
                </div>

                <h3 style="margin-top:24px">How should patients pay?</h3>
                <div class="form-row">
                    <div class="field">
                        <label><input type="radio" name="payMode" value="full" id="f-payModeFull"> Full payment before booking is confirmed</label><br>
                        <label><input type="radio" name="payMode" value="partial" id="f-payModePartial"> Partial deposit now, rest at the clinic</label><br>
                        <label><input type="radio" name="payMode" value="none" id="f-payModeNone"> No payment required — book directly</label>
                    </div>
                </div>
                <div class="form-row" id="f-payPercentRow" style="display:none">
                    <div class="field"><label>Deposit Percentage</label><input type="number" id="f-payPercent" min="1" max="99" placeholder="e.g. 20"></div>
                </div>

                <button class="btn btn-accent" onclick="savePaymentSettings()">Save Payment Settings</button>
            </div>
        </div>

```

- [ ] **Step 3: Wire the radio-toggle for the deposit percentage field**

Insert right after the `document.getElementById('confirmPromptInput').addEventListener(...)` block (currently ends at line 1341, right before `async function confirmDialog(message, opts = {}) {`):

```javascript
document.querySelectorAll('input[name="payMode"]').forEach(radio => {
    radio.addEventListener('change', () => {
        document.getElementById('f-payPercentRow').style.display =
            document.getElementById('f-payModePartial').checked ? 'block' : 'none';
    });
});
```

- [ ] **Step 4: Add `applyFeatureVisibility()` and call it from `login()`**

Replace (lines 1184-1200):

```javascript
function login() {
    const u = document.getElementById('loginUser').value.trim();
    const p = document.getElementById('loginPass').value;
    if (!u || !p) return;
    auth = 'Basic ' + btoa(u + ':' + p);

    api('/admin/stats?days=30').then(data => {
        document.getElementById('loginScreen').style.display = 'none';
        document.getElementById('app').classList.add('open');
        renderDashboard(data);
        loadDoctorsSilent();
    }).catch(() => {
        auth = '';
        document.getElementById('loginError').innerHTML =
            '<div class="alert alert-err">Invalid username or password</div>';
    });
}
```

with:

```javascript
let myFeatures = null; // null = super_admin, sees everything

function applyFeatureVisibility() {
    document.querySelectorAll('[data-feature]').forEach(el => {
        const feature = el.dataset.feature;
        const visible = myFeatures === null || myFeatures.includes(feature);
        el.style.display = visible ? '' : 'none';
    });
}

function login() {
    const u = document.getElementById('loginUser').value.trim();
    const p = document.getElementById('loginPass').value;
    if (!u || !p) return;
    auth = 'Basic ' + btoa(u + ':' + p);

    api('/admin/stats?days=30').then(data => {
        document.getElementById('loginScreen').style.display = 'none';
        document.getElementById('app').classList.add('open');
        renderDashboard(data);
        loadDoctorsSilent();
        api('/admin/me').then(me => {
            myFeatures = me.features; // null for super_admin
            applyFeatureVisibility();
        }).catch(() => {});
    }).catch(() => {
        auth = '';
        document.getElementById('loginError').innerHTML =
            '<div class="alert alert-err">Invalid username or password</div>';
    });
}
```

- [ ] **Step 5: Register the `paysettings` loader in the `go()` nav function**

Replace (line 1227):

```javascript
    const loaders = { dashboard: loadDashboard, appointments: loadAppointments, doctors: loadDoctors, leaves: loadLeaves, holidays: loadHolidays, patients: loadPatients, labreports: loadLabReports, prescriptions: loadPrescriptions, payments: loadPayments, branches: loadBranches };
```

with:

```javascript
    const loaders = { dashboard: loadDashboard, appointments: loadAppointments, doctors: loadDoctors, leaves: loadLeaves, holidays: loadHolidays, patients: loadPatients, labreports: loadLabReports, prescriptions: loadPrescriptions, payments: loadPayments, paysettings: loadPaymentSettings, branches: loadBranches };
```

- [ ] **Step 6: Add `loadPaymentSettings()` and `savePaymentSettings()` JS functions**

Insert right after the `saveBranch()` function (find it in the script block, insert after its closing `}`):

```javascript
// ═══════ PAYMENT SETTINGS ═══════
async function loadPaymentSettings() {
    try {
        const data = await api('/admin/settings/payment');
        document.getElementById('f-payKeyId').value = data.razorpay_key_id || '';
        document.getElementById('f-payKeySecret').placeholder =
            data.razorpay_key_secret_masked ? 'Saved: ' + data.razorpay_key_secret_masked : 'Leave blank to keep existing';
        document.getElementById('f-payWebhookSecret').placeholder =
            data.razorpay_webhook_secret_masked ? 'Saved: ' + data.razorpay_webhook_secret_masked : 'Leave blank to keep existing';

        const mode = data.payment_mode || 'none';
        document.getElementById('f-payMode' + mode.charAt(0).toUpperCase() + mode.slice(1)).checked = true;
        document.getElementById('f-payPercent').value = data.payment_deposit_percent || '';
        document.getElementById('f-payPercentRow').style.display = mode === 'partial' ? 'block' : 'none';
    } catch (e) {
        msg('paySettingsMsg', 'Error loading payment settings: ' + e.message, true);
    }
}

async function savePaymentSettings() {
    const mode = document.querySelector('input[name="payMode"]:checked')?.value || 'none';
    const body = { payment_mode: mode };

    const keyId = document.getElementById('f-payKeyId').value.trim();
    const keySecret = document.getElementById('f-payKeySecret').value.trim();
    const webhookSecret = document.getElementById('f-payWebhookSecret').value.trim();
    if (keyId) body.razorpay_key_id = keyId;
    if (keySecret) body.razorpay_key_secret = keySecret;
    if (webhookSecret) body.razorpay_webhook_secret = webhookSecret;

    if (mode === 'partial') {
        const percent = parseInt(document.getElementById('f-payPercent').value, 10);
        if (!percent || percent < 1 || percent > 99) {
            msg('paySettingsMsg', 'Enter a deposit percentage between 1 and 99', true);
            return;
        }
        body.payment_deposit_percent = percent;
    }

    try {
        await apiPut('/admin/settings/payment', body);
        msg('paySettingsMsg', '✅ Payment settings saved!');
        document.getElementById('f-payKeySecret').value = '';
        document.getElementById('f-payWebhookSecret').value = '';
        loadPaymentSettings();
    } catch (e) {
        msg('paySettingsMsg', 'Error: ' + e.message, true);
    }
}
```

- [ ] **Step 7: Manual verification**

Run the app locally (`uvicorn app.main:app --reload` or the project's existing run command) and open `admin/index.html` (served by the app) in a browser:

1. Log in as the env-var `super_admin` (`ADMIN_USERNAME`/`ADMIN_PASSWORD`) — confirm every nav tab is visible, including the new "Payment Settings" tab.
2. In Supabase, create (or update) a `clinic_admins` row with `role='clinic_admin'`, `clinic_id` pointing at a clinic whose `plan='soloclinic'`. Log in as that user — confirm "Lab Reports" and "Branches" tabs are hidden, "Payment Settings" is visible.
3. Update that clinic's `plan` to `diagstream`. Log in again — confirm "Payment Settings", "Doctors", "Appointments" are hidden, "Lab Reports" is visible.
4. On the Payment Settings tab, enter a Key ID/Secret, select "Partial deposit", enter `20`, save. Reload the tab — confirm the Key ID persisted and the masked secret placeholder shows.
5. Book a test appointment via WhatsApp against that clinic (or trigger the conversation flow directly) and confirm the payment message shows the "20% deposit... 80% at the clinic" note from Task 4.

- [ ] **Step 8: Commit**

```bash
git add admin/index.html
git commit -m "feat(admin-ui): plan-aware tab visibility and self-service Payment Settings tab"
```

---

## Self-Review Notes

- **Spec coverage:** Payment self-service (Task 6), payment-mode booking flow (Tasks 3-4), plan-aware admin UI (Tasks 5, 7), migration fix (Task 1) — every section of the design spec has a task.
- **Type consistency checked:** `resolve_payment_mode(clinic) -> tuple[str, int]` (Task 3) is called identically in Task 4 (`payment_mode, deposit_percent = resolve_payment_mode(clinic)`) and its `mode`/`percent` naming matches. `PaymentSettingsUpdate` field names (Task 6) match exactly what `admin/index.html`'s `savePaymentSettings()` (Task 7) sends. `GET /admin/me`'s `features` key matches what `applyFeatureVisibility()` (Task 7) reads (`myFeatures = me.features`).
- **No placeholders:** every step has runnable code, not descriptions.
