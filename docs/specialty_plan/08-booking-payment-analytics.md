# Task 8 — Tag bookings (paid & unpaid), prep note, admin alerts, insights

**Files:**
- Modify: `app/services/conversation.py` (`_show_booking_confirmation`, `_handle_confirming_booking`)
- Modify: `app/services/payment.py` (`create_booking_with_payment`, `_notify_payment_confirmed`)
- Modify: `app/services/analytics.py` (insights select ~637; service naming ~193)
- Modify: `app/routers/admin.py` (payments bookings select ~3659)
- Test: `tests/test_specialty_booking_payment.py`

**Interfaces:**
- Consumes: Task 1 columns; Task 7 `specialty_flow.revalidate_treatment` and `specialty_flow.send_prep_note`.
- Produces:
  - `PaymentService.create_booking_with_payment(..., treatment_id: Optional[str] = None, treatment_name: Optional[str] = None)`
  - appointment rows carry `treatment_id` / `treatment_name`, only when a treatment was chosen and `booking_type == "consultation"`.

**Payments for specialty plans need no new code:**
- The plans include `payments_razorpay`, so the existing Payment Settings page (`PUT /admin/settings/payment`), per-clinic Razorpay keys, `resolve_payment_mode()` (full / partial deposit / none), payment links, the webhook, hold expiry and refunds all apply.
- The amount is the chosen doctor's `consultation_fee`, exactly as for any consultation.
- `price_from_paise` is informational only and is **never** charged.

---

- [x] **Step 1: Write the failing test** — create `tests/test_specialty_booking_payment.py`:

```python
"""A treatment booking is an ordinary consultation with a tag: same payment,
same slot guard, same refunds. Bookings without a treatment are unchanged."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import specialty_flow
from app.services.conversation import ConversationManager
from app.services.payment import PaymentService

PHONE = "+919000000001"
T1 = "22222222-2222-2222-2222-222222222222"
D1 = "11111111-1111-1111-1111-111111111111"


async def _paid_booking(**extra):
    service = PaymentService()
    fake_supabase = MagicMock()
    results = [
        MagicMock(data=[{"consultation_fee": 500}]),        # _get_doctor_fee_paise
        MagicMock(data=[{"id": "b1", "booking_ref": "MC1"}]),  # insert
        MagicMock(data=[]),                                  # store payment link id
    ]
    with patch("app.services.payment.supabase", fake_supabase), \
         patch("app.services.payment.sb", AsyncMock(side_effect=results)), \
         patch("app.services.payment.get_razorpay_creds", return_value=("rzp_test_x", "secret", None)), \
         patch.object(service, "_create_payment_link", AsyncMock(return_value={"id": "plink_1", "short_url": "https://rzp.io/x"})), \
         patch.object(service, "_log_payment_event", AsyncMock()):
        result = await service.create_booking_with_payment(
            clinic_id="c1", patient_phone=PHONE, patient_name="Asha Rao", department="Dermatology",
            doctor_name="Dr. Mehta", appointment_date="2026-10-01", appointment_time="10:00",
            clinic={"id": "c1", "plan": "derma"}, doctor_id=D1, **extra,
        )
    return result, fake_supabase.table.return_value.insert.call_args.args[0]


@pytest.mark.asyncio
async def test_paid_treatment_booking_is_a_tagged_consultation_priced_from_the_doctor():
    result, row = await _paid_booking(treatment_id=T1, treatment_name="Hair PRP Therapy")
    assert result["success"] is True
    assert row["booking_type"] == "consultation"
    assert row["doctor_id"] == D1
    assert row["amount_paise"] == 50000
    assert row["treatment_id"] == T1 and row["treatment_name"] == "Hair PRP Therapy"


@pytest.mark.asyncio
async def test_paid_booking_without_treatment_has_no_new_keys():
    _, row = await _paid_booking()
    assert "treatment_id" not in row and "treatment_name" not in row


@pytest.mark.asyncio
async def test_treatment_is_never_attached_to_a_lab_test_booking():
    service = PaymentService()
    fake_supabase = MagicMock()
    results = [MagicMock(data=[{"id": "b2", "booking_ref": "MC2"}]), MagicMock(data=[])]
    with patch("app.services.payment.supabase", fake_supabase), \
         patch("app.services.payment.sb", AsyncMock(side_effect=results)), \
         patch("app.services.payment.get_razorpay_creds", return_value=("rzp_test_x", "secret", None)), \
         patch.object(service, "_get_lab_test_fee_paise", AsyncMock(return_value=30000)), \
         patch.object(service, "_create_payment_link", AsyncMock(return_value={"id": "plink_2", "short_url": "https://rzp.io/y"})), \
         patch.object(service, "_log_payment_event", AsyncMock()):
        await service.create_booking_with_payment(
            clinic_id="c1", patient_phone=PHONE, patient_name="Asha Rao", department="Lab Test",
            doctor_name=None, appointment_date="2026-10-01", appointment_time=None,
            clinic={"id": "c1", "plan": "ivf"}, booking_type="lab_test", lab_test_id="lt1",
            lab_test_name="AMH", treatment_id=T1, treatment_name="IVF",
        )
    row = fake_supabase.table.return_value.insert.call_args.args[0]
    assert row["booking_type"] == "lab_test" and "treatment_id" not in row


def _confirm_context(**extra):
    return {"doctor_name": "Dr. Mehta", "doctor_id": D1, "department": "Dermatology",
            "appointment_date": "2026-10-01", "appointment_time": "10:00",
            "booking_name": "Asha Rao", **extra}


async def _direct_confirm(context, treatment_row):
    m = ConversationManager()
    m.whatsapp = MagicMock(send_text=AsyncMock(), send_interactive_buttons=AsyncMock(), send_interactive_list=AsyncMock())
    booked = AsyncMock(return_value={"success": False, "reason": "error"})
    with patch("app.services.payment.resolve_payment_mode", return_value=("none", 100)), \
         patch("app.database.get_doctor_by_name", AsyncMock(return_value={"is_active": True})), \
         patch.object(specialty_flow, "get_treatment_by_id", AsyncMock(return_value=treatment_row)), \
         patch("app.services.conversation.book_appointment", booked), \
         patch.object(m, "update_state", AsyncMock()), \
         patch.object(m, "_send_main_menu", AsyncMock()):
        await m._handle_confirming_booking({"id": "c1", "whatsapp_number": "+911"}, PHONE, "Confirm",
                                           "confirm_booking", context, {"id": "p1"}, "en")
    return booked.await_args.args[1]


@pytest.mark.asyncio
async def test_unpaid_treatment_booking_carries_the_tag():
    data = await _direct_confirm(_confirm_context(treatment_id=T1, treatment_name="Chemical Peel"), {"id": T1})
    assert data["treatment_id"] == T1 and data["treatment_name"] == "Chemical Peel"
    assert "booking_type" not in data or data["booking_type"] == "consultation"


@pytest.mark.asyncio
async def test_treatment_deleted_mid_booking_books_the_consultation_untagged():
    data = await _direct_confirm(_confirm_context(treatment_id=T1, treatment_name="Chemical Peel"), None)
    assert "treatment_id" not in data


@pytest.mark.asyncio
async def test_unpaid_booking_without_treatment_is_unchanged():
    data = await _direct_confirm(_confirm_context(), None)
    assert "treatment_id" not in data and "treatment_name" not in data


@pytest.mark.asyncio
async def test_confirmation_screen_names_the_treatment_only_when_present():
    for ctx, expected in ((_confirm_context(treatment_name="Root Canal Treatment"), True), (_confirm_context(), False)):
        m = ConversationManager()
        m.whatsapp = MagicMock(send_interactive_buttons=AsyncMock())
        with patch.object(m, "update_state", AsyncMock()):
            await m._show_booking_confirmation({"id": "c1"}, PHONE, ctx, "en")
        body = m.whatsapp.send_interactive_buttons.await_args.kwargs["body"]
        assert ("Root Canal Treatment" in body) is expected


def test_insights_and_payments_read_the_treatment_name():
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    analytics = (repo / "app" / "services" / "analytics.py").read_text(encoding="utf-8")
    admin = (repo / "app" / "routers" / "admin.py").read_text(encoding="utf-8")
    assert "booking_type,lab_test_name,treatment_name,amount_paise" in analytics
    assert "booking_type, lab_test_id, lab_test_name, treatment_id, treatment_name, " in admin


def test_insights_service_mix_uses_treatment_names():
    from app.services import analytics

    source = analytics.__file__
    text = open(source, encoding="utf-8").read()
    block = text.split('if booking_type == "lab_test":\n            service =')[1][:400]
    assert 'a.get("treatment_name")' in block
```

- [x] **Step 2: Run and confirm failure**

```bash
pytest tests/test_specialty_booking_payment.py -q
```
Expected: `TypeError: create_booking_with_payment() got an unexpected keyword argument 'treatment_id'`, among other failures.

- [x] **Step 3: `payment.py`, signature.** In `create_booking_with_payment`, replace exactly:
```python
        lab_test_name: Optional[str] = None,
        doctor_id: Optional[str] = None,
    ) -> dict:
```
with:
```python
        lab_test_name: Optional[str] = None,
        doctor_id: Optional[str] = None,
        treatment_id: Optional[str] = None,
        treatment_name: Optional[str] = None,
    ) -> dict:
```

- [x] **Step 4: `payment.py`, row payload.** Replace exactly:
```python
        if booking_type == "lab_test":
            booking_data["lab_test_id"] = lab_test_id
            booking_data["lab_test_name"] = lab_test_name
```
with:
```python
        if booking_type == "lab_test":
            booking_data["lab_test_id"] = lab_test_id
            booking_data["lab_test_name"] = lab_test_name
        # Specialty treatment tag (migration 077). The row stays
        # booking_type='consultation', so uq_appointment_active_slot, the
        # doctor_id guard above, reminders, expiry and refunds all apply.
        if treatment_id and booking_type == "consultation":
            booking_data["treatment_id"] = treatment_id
            booking_data["treatment_name"] = treatment_name
```

- [x] **Step 5: `payment.py`, prep note after the paid confirmation.** In `_notify_payment_confirmed`, replace exactly:
```python
            if not patient_notified:
                logger.error(
```
with:
```python
            if patient_notified and booking.get("treatment_id"):
                from app.services.specialty_flow import send_prep_note

                await send_prep_note(whatsapp_service, clinic, patient_phone, booking["treatment_id"], lang)

            if not patient_notified:
                logger.error(
```

- [x] **Step 6: `payment.py`, treatment on the admin WhatsApp alert.** Replace exactly:
```python
                        f"💰 *Paid:* ₹{amount_rupees:.0f}\n"
                        f"🆔 *Payment ID:* {booking.get('payment_id', 'N/A')}"
                    )
                await self._alert_admin(clinic, admin_notif_msg)
```
with:
```python
                        f"💰 *Paid:* ₹{amount_rupees:.0f}\n"
                        f"🆔 *Payment ID:* {booking.get('payment_id', 'N/A')}"
                    )
                if booking.get("treatment_name") and booking.get("booking_type") != "lab_test":
                    admin_notif_msg += f"\n🩺 *Treatment:* {booking['treatment_name']}"
                await self._alert_admin(clinic, admin_notif_msg)
```
(This matches only the consultation branch, because the lab branch's `)` is followed by `else:`, not by `await self._alert_admin`.)

- [x] **Step 7: `payment.py`, treatment on the in-app notification.** Replace exactly:
```python
                    notif_row = {
                        "clinic_id": clinic_id_val,
```
with:
```python
                    if booking.get("treatment_name") and booking.get("booking_type") != "lab_test":
                        in_app_msg += f" Treatment: {booking['treatment_name']}."
                    notif_row = {
                        "clinic_id": clinic_id_val,
```

- [x] **Step 8: `conversation.py`, confirmation screen.** In `_show_booking_confirmation`, replace exactly:
```python
        await self.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=confirm_body,
```
with:
```python
        if context.get("treatment_name"):
            confirm_body += (
                "\n🩺 " + {"en": "Treatment", "hi": "उपचार", "te": "చికిత్స"}.get(lang, "Treatment")
                + f": {context['treatment_name']}"
            )

        await self.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=confirm_body,
```

- [x] **Step 9: `conversation.py`, revalidate before writing.** In `_handle_confirming_booking`, replace exactly:
```python
            # ── Resolve this clinic's payment mode: full / partial / none ──
```
with:
```python
            # A treatment deleted while the patient was booking would fail the
            # foreign key and lose the booking: drop the tag, keep the booking.
            await specialty_flow.revalidate_treatment(clinic, context)

            # ── Resolve this clinic's payment mode: full / partial / none ──
```

- [x] **Step 10: `conversation.py`, Path A (paid).** In `_handle_confirming_booking`, replace exactly:
```python
                    deposit_percent=deposit_percent,
                    doctor_id=context.get("doctor_id") or context.get("selected_doctor_id"),
                )
```
with:
```python
                    deposit_percent=deposit_percent,
                    doctor_id=context.get("doctor_id") or context.get("selected_doctor_id"),
                    treatment_id=context.get("treatment_id"),
                    treatment_name=context.get("treatment_name"),
                )
```

- [x] **Step 11: `conversation.py`, Path B (unpaid).** In `_handle_confirming_booking`, replace exactly:
```python
                if context.get("branch_id"):
                    appointment_data["branch_id"] = context["branch_id"]
                    appointment_data["branch_name"] = context.get("branch_name", "")

                result = await book_appointment(clinic["id"], appointment_data)
```
with:
```python
                if context.get("branch_id"):
                    appointment_data["branch_id"] = context["branch_id"]
                    appointment_data["branch_name"] = context.get("branch_name", "")

                if context.get("treatment_id"):
                    appointment_data["treatment_id"] = context["treatment_id"]
                    appointment_data["treatment_name"] = context.get("treatment_name")

                result = await book_appointment(clinic["id"], appointment_data)
```

- [x] **Step 12: `conversation.py`, prep note after the unpaid confirmation.** In `_handle_confirming_booking` (Path B success), replace exactly:
```python
                    await self.whatsapp.send_text(clinic, phone, dept_instruction)
```
with:
```python
                    await self.whatsapp.send_text(clinic, phone, dept_instruction)
                    await specialty_flow.send_prep_note(
                        self.whatsapp, clinic, phone, context.get("treatment_id"), lang
                    )
```

- [x] **Step 13: `analytics.py`.**

a) Replace exactly:
```python
                "booking_type,lab_test_name,amount_paise,payment_id,patient_phone",
```
with:
```python
                "booking_type,lab_test_name,treatment_name,amount_paise,payment_id,patient_phone",
```
b) Replace exactly:
```python
        if booking_type == "lab_test":
            service = (a.get("lab_test_name") or "").strip() or "Lab Test"
        else:
            service = department or "Consultation"
```
with:
```python
        if booking_type == "lab_test":
            service = (a.get("lab_test_name") or "").strip() or "Lab Test"
        elif (a.get("treatment_name") or "").strip():
            # Specialty clinics: the treatment is what the patient came for.
            service = a["treatment_name"].strip()
        else:
            service = department or "Consultation"
```

- [x] **Step 14: `admin.py`, payments bookings select.** Replace exactly:
```python
            "booking_type, lab_test_id, lab_test_name, "
```
with:
```python
            "booking_type, lab_test_id, lab_test_name, treatment_id, treatment_name, "
```

- [x] **Step 15: Run the new tests and the payment and analytics regression set**

```bash
pytest tests/test_specialty_booking_payment.py tests/test_conversation_payment_mode.py tests/test_lab_test_booking_payment.py tests/test_razorpay_webhook_default_clinic.py tests/test_phase1_payment_integrity.py tests/test_insights.py tests/test_appointments_list.py tests/test_diagbooking_admin_visibility.py tests/test_lint_unscoped_queries.py -q
```
Also run every other payment test file present:
```bash
ls tests/test_payment*.py
pytest $(ls tests/test_payment*.py) -q
```
Expected: all PASS.
- `test_insights.py` may assert the exact select string or the exact `by_service` output. If it asserts the select string, add `treatment_name` to its expected string; that is the only permitted test edit here. Rows without `treatment_name` must produce identical `by_service` output.
- If `_paid_booking` needs more `sb` results than listed, extend the list.

Run the orphan check.

- [x] **Step 16: Commit**

```bash
git add app/services/conversation.py app/services/payment.py app/services/analytics.py app/routers/admin.py tests/test_specialty_booking_payment.py
git commit -m "feat(booking): tag paid and unpaid consultations with the chosen treatment, prep note, alerts, insights

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
