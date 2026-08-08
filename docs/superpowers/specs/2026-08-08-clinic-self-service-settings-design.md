# Clinic Self-Service Settings + Plan-Aware Admin Panel

**Status:** Approved for implementation
**Date:** 2026-08-08

## Problem

Today, onboarding a clinic (item 6 — manual, by the platform owner, via `POST /admin/clinics`)
already works, and doctor/fee/timing management (items 2 & 7) already works self-service via the
existing `/admin/doctors` endpoints, scoped per clinic. Both are out of scope for this change.

Two things are missing:

1. **Payment self-service.** A clinic's Razorpay keys can only be set by the platform owner
   (`PATCH /admin/clinics/{id}` behind `X-Admin-Secret`). Clinics cannot manage their own payment
   details, and there is no way for a clinic to choose "pay in full", "pay a partial deposit", or
   "no payment required" — today it is an implicit binary (keys present → full fee; keys absent →
   free booking).
2. **Plan-aware admin UI.** `admin/index.html` is a single page that shows every section to every
   clinic regardless of plan (soloclinic / diagstream / essential / polyclinic / enterprise). The
   feature registry that should drive this (`app/services/tenant.py::PLAN_FEATURES`,
   `has_feature`) already exists and is correct — it's just never been wired to the frontend.

There is also a stale DB constraint: migration `006_alter_clinics_plan.sql` still restricts
`clinics.plan` to `basic/pro/enterprise`, but all application code (Pydantic models, the feature
registry, `platform.html`) has already moved to `soloclinic/diagstream/essential/polyclinic/enterprise`.
This must be fixed or inserts/updates with the real plan values will fail at the DB layer the
moment the CHECK constraint is actually enforced (or already silently isn't, which is its own bug).

## Non-Goals

- CallMedex integration and the MediAssist AI conversational workflow are not touched.
- Doctor/fee/timing/slot management is not touched (already self-service, already correct).
- Manual clinic onboarding flow (owner sets WhatsApp number, plan, Meta phone number ID, Meta
  access token → creates `clinic_id`) is not touched. Diagnostic-center MOC/HMIS connector setup
  stays owner-only (unchanged) — not exposed to clinic admins.
- No new payment gateway besides Razorpay.
- No refund-flow changes for partial payments beyond what's specified below.

## Data Model

No new tables. Two new keys added to the existing `clinics.config` JSONB (same place
`razorpay_key_id` / `razorpay_key_secret` / `razorpay_webhook_secret` already live):

```
config.payment_mode            "full" | "partial" | "none"   (default: derived, see below)
config.payment_deposit_percent  int 1-99                      (only read when mode = "partial")
```

**Default/back-compat rule:** if `payment_mode` is absent (every existing clinic today), it is
treated as `"full"` when Razorpay keys are configured and `"none"` when they are not — i.e.
exactly today's behavior. No backfill migration needed; the fallback is computed at read time.

### Migration `016_fix_plan_constraint.sql`

```sql
-- Verify no rows hold a value outside the new set before dropping the old constraint
-- (fails loudly if any clinic is on a value neither the old nor new constraint would expect)
DO $$
DECLARE bad_count INT;
BEGIN
    SELECT COUNT(*) INTO bad_count FROM clinics
    WHERE plan NOT IN ('basic','pro','enterprise',
                        'soloclinic','diagstream','essential','polyclinic');
    IF bad_count > 0 THEN
        RAISE EXCEPTION 'Found % clinics with unexpected plan value — resolve before migrating', bad_count;
    END IF;
END $$;

-- Map legacy tier values to the closest clinic-type equivalent
UPDATE clinics SET plan = 'essential'  WHERE plan = 'basic';
UPDATE clinics SET plan = 'essential'  WHERE plan = 'pro';
-- 'enterprise' is unchanged — it exists in both old and new sets

ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_plan_check;
ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check
    CHECK (plan IN ('soloclinic','diagstream','essential','polyclinic','enterprise'));
```

(The exact constraint name is verified against `information_schema` during implementation —
Postgres auto-names differ between the `CREATE TABLE` and `ALTER TABLE` origins of this column.)

## API Changes (`app/routers/admin.py`)

### `GET /admin/me`
Auth: `verify_credentials` (existing). Returns the caller's own identity plus their clinic's plan
and resolved feature set, so the frontend can drive visibility without duplicating
`PLAN_FEATURES` in JS:

```json
{
  "username": "drpatel",
  "role": "clinic_admin",
  "clinic_id": "5b1e...",
  "plan": "soloclinic",
  "features": ["booking", "reminders", "payments_razorpay", "admin_dashboard", ...]
}
```

For `super_admin` (no `clinic_id`), `plan`/`features` are omitted — the owner sees everything.

### `GET /admin/settings/payment`
Auth: `verify_credentials` + `enforce_clinic_access` (existing helper — a `clinic_admin` may only
read their own clinic; `super_admin` may pass `?clinic_id=`). Returns current config with secrets
masked (`rzp_live_••••1234`, last 4 chars only) so the settings page can pre-fill without leaking
the full secret back over the wire on every page load.

### `PUT /admin/settings/payment`
Same auth. Body:

```json
{
  "razorpay_key_id": "rzp_live_xxxx",
  "razorpay_key_secret": "xxxx",
  "razorpay_webhook_secret": "xxxx",
  "payment_mode": "partial",
  "payment_deposit_percent": 20
}
```

All fields optional/partial-update (`exclude_unset`), matching the existing `DoctorUpdate`
pattern. Validation:
- `payment_mode` must be one of the three literals.
- `payment_deposit_percent` required and in `1..99` when `payment_mode == "partial"`, rejected
  (422) otherwise.
- Secret fields: an empty string is ignored (never overwrites a stored secret with blank) —
  matching the existing `if req.razorpay_key_id:` guard pattern in `clinics.py`.
- `403` for `diagstream` clinics — `payments_razorpay` isn't in their feature set (checked via
  `require_feature`), since diagnostic centers don't take bookings.
- Every write is recorded via the existing `log_admin_action` audit helper (`resource_type=
  "payment_settings"`), consistent with how doctor/branch mutations are already audited.

Merges into `clinics.config` via a read-modify-write on the JSONB (same approach `clinics.py`
already uses for onboarding) and calls the existing `invalidate_tenant_cache` so the next inbound
WhatsApp message picks up the new settings immediately.

## Booking Flow Change (`app/services/conversation.py`, `app/services/payment.py`)

In `_handle_confirming_booking`, replace:

```python
razorpay_configured = bool(_rz_key_id and _rz_key_secret)
if razorpay_configured: ... payment-gated ...
else: ... direct booking ...
```

with a three-way resolution (new helper `resolve_payment_mode(clinic) -> tuple[str, int]` in
`payment.py`, next to `get_razorpay_creds`):

```python
def resolve_payment_mode(clinic: dict) -> tuple[str, int]:
    cfg = clinic.get("config") or {}
    key_id, key_secret, _ = get_razorpay_creds(clinic)
    configured = bool(key_id and key_secret)
    mode = cfg.get("payment_mode") or ("full" if configured else "none")
    if mode in ("full", "partial") and not configured:
        mode = "none"          # fail safe — never block a booking on missing keys
    percent = cfg.get("payment_deposit_percent", 100) if mode == "partial" else 100
    return mode, percent
```

- `mode == "none"` → existing direct-booking path, unchanged.
- `mode == "full"` → existing payment-gated path, unchanged amount calculation.
- `mode == "partial"` → payment-gated path, but `amount_paise` passed into
  `create_booking_with_payment` is `round(full_fee_paise * percent / 100)`. The confirmation
  message gains one line: *"This is a deposit of ₹X. The remaining ₹Y is payable at the clinic."*
  `payment_events` logs continue to record the actual `amount_paise` charged — no change to the
  audit trail shape, just a smaller number for partial bookings.

Refunds (existing `refund` admin action) already operate on whatever `amount_paise` was actually
charged, so partial-payment refunds work with zero changes to `payment.py`'s refund logic.

## Admin UI (`admin/index.html`)

- On successful login, call `GET /admin/me` once, store `features` in a JS variable.
- Wrap each existing tab's nav item and panel with a `data-feature="..."` attribute; a small
  `applyFeatureVisibility()` function hides any tab whose `data-feature` isn't in the returned
  list. `super_admin` (no `features` key returned) sees all tabs, unchanged from today.
- New **Payment Settings** tab (`data-feature="payments_razorpay"`): form with Razorpay Key
  ID/Secret/Webhook Secret (secret inputs show masked existing value, only sent if changed), a
  radio group (Full payment / Partial deposit / No payment required), and a percent input that
  only appears when "Partial" is selected. Submits to `PUT /admin/settings/payment`.
- No new files, no separate per-plan pages — one dynamic page, per the approved direction.

## Error Handling

- `PUT /admin/settings/payment` on a `diagstream` clinic → `403` with the existing
  `require_feature` message ("Feature 'payments_razorpay' is not available on your current plan").
- Invalid `payment_deposit_percent` (out of `1..99`, or missing when mode=partial) → `422` via
  Pydantic validator, same pattern as existing `DoctorCreate` validators.
- Booking-time fail-safe: `resolve_payment_mode` never returns `full`/`partial` without confirmed
  working keys — a clinic that saves `mode=full` and later has its Razorpay keys revoked
  automatically falls back to free direct booking rather than silently failing bookings.
- Migration `016`: aborts loudly (`RAISE EXCEPTION`) if any clinic has a plan value outside the
  known old+new sets, rather than silently corrupting data.

## Testing

- `tests/test_payment.py` (existing file, extend): unit tests for `resolve_payment_mode` covering
  all 6 combinations of `{full, partial, none} x {keys configured, not configured}`.
- New test for `PUT /admin/settings/payment`: clinic_admin can update own clinic, cannot update
  another clinic's settings (403 via `enforce_clinic_access`), diagstream clinic gets 403,
  invalid percent gets 422, empty-string secret does not clobber stored secret.
- New test for `GET /admin/me`: returns correct feature list per plan for a `clinic_admin`,
  returns no plan/features restriction for `super_admin`.
- Manual verification: run the admin panel locally, log in as a `soloclinic` clinic_admin, confirm
  Payment Settings tab appears and Lab Reports/Branches tabs do not; log in as `diagstream`,
  confirm the reverse.
