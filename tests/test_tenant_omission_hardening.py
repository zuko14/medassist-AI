"""Regression guards for KA-A-17 (NULL-tenant payment events) and
KA-A-02 (tenant guessing on failure).

KA-A-17
-------
payment_events.clinic_id was optional on both audit loggers, and 14 of the 25
call sites in app/services/payment.py never passed it: every refund, admin
confirm/reject/cancel, hold-expiry and webhook-recovery event wrote a row with
a NULL tenant.

Isolation itself never depended on that column — the RLS policy joins through
appointments on booking_id, as does every shipped read. What the NULLs broke is
the column's trustworthiness: it is indexed and declared tenant-owned, so the
first aggregation to group or filter by it would have quietly undercounted.

The fix resolves the clinic inside the logger from the booking's own
appointments row, so it holds for the 14 known callers and for every caller
written after this. These tests exercise the real call shape, not the helper's
happy path, because a test that passed clinic_id would have passed before the
fix too.

KA-A-02
-------
Two branches resolved a tenant by falling back rather than by looking one up:
resolve_tenant() returned the synthetic env-var clinic when the query that
counts active clinics threw, and the payment webhook retried its idempotency
check with the clinic predicate dropped. Both replaced an unanswered question
about tenancy with a guess, at exactly the moment the database was least
trustworthy.
"""

import inspect
from typing import Optional

import pytest
from unittest.mock import MagicMock, patch

from app.database import is_valid_clinic_scope

CLINIC_A = "aaaaaaaa-1111-1111-1111-111111111111"
CLINIC_B = "bbbbbbbb-2222-2222-2222-222222222222"
BOOKING_A = "book-aaaa-0001"


# ── KA-A-17: every ledger write carries a tenant ────────────────────────────


class _LedgerRecorder:
    """Captures payment_events inserts and answers the clinic lookup.

    Mimics enough of the Supabase builder that the logger's own appointments
    lookup resolves, so the test sees exactly what production would write.
    """

    def __init__(self, booking_clinic: Optional[str] = CLINIC_A):
        self.booking_clinic = booking_clinic
        self.rows = []
        self.supabase = MagicMock()
        self.supabase.table.side_effect = self._table

    def _table(self, name):
        chain = MagicMock()
        for method in ("select", "eq", "limit", "update", "neq", "order"):
            getattr(chain, method).return_value = chain

        if name == "appointments":
            rows = [{"clinic_id": self.booking_clinic}] if self.booking_clinic else []
            chain.execute.return_value = MagicMock(data=rows)
            return chain

        def _insert(row):
            if name == "payment_events":
                self.rows.append(row)
            chain.execute.return_value = MagicMock(data=[row])
            return chain

        chain.insert.side_effect = _insert
        chain.execute.return_value = MagicMock(data=[])
        return chain

    def tables_touched(self):
        return [c.args[0] for c in self.supabase.table.call_args_list]


# The 14 call sites that omitted clinic_id, named by the event each one emits.
ORPHANED_EVENT_TYPES = [
    "payment_link_created",
    "payment_link_creation_failed",
    "recovery_confirmed",
    "hold_expired",
    "refund_initiated",
    "refund_completed",
    "refund_failed",
    "auto_refund_issued",
    "auto_refund_failed",
    "manual_confirm",
    "reject_aborted_refund_failed",
    "manual_reject",
    "admin_cancel_without_refund",
    "admin_cancel",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ORPHANED_EVENT_TYPES)
async def test_orphaned_call_sites_now_write_a_tenant(event_type):
    """Each previously-orphaned event type lands with a real clinic_id.

    Called the way those sites call it, booking_id and payload only with no
    clinic_id kwarg, which is precisely the shape that used to write NULL.
    """
    from app.services.payment import PaymentService

    rec = _LedgerRecorder()
    with patch("app.services.payment.supabase", rec.supabase):
        await PaymentService()._log_payment_event(BOOKING_A, event_type, {"k": "v"})

    assert len(rec.rows) == 1, f"{event_type} wrote no payment_events row"
    written = rec.rows[0].get("clinic_id")
    assert written == CLINIC_A
    assert is_valid_clinic_scope(written)


@pytest.mark.asyncio
async def test_explicit_clinic_id_is_not_overridden_by_lookup():
    """A caller that already knows the scope must win, and skip the extra read."""
    from app.services.payment import PaymentService

    rec = _LedgerRecorder(booking_clinic=CLINIC_B)
    with patch("app.services.payment.supabase", rec.supabase):
        await PaymentService()._log_payment_event(
            BOOKING_A, "confirmed", {}, clinic_id=CLINIC_A
        )

    assert rec.rows[0]["clinic_id"] == CLINIC_A
    assert "appointments" not in rec.tables_touched()


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel", ["default", "none", "null", "", "  "])
async def test_sentinel_clinic_id_is_replaced_by_the_real_owner(sentinel):
    """The string 'default' is not a tenant, and must not reach the column."""
    from app.services.payment import PaymentService

    rec = _LedgerRecorder()
    with patch("app.services.payment.supabase", rec.supabase):
        await PaymentService()._log_payment_event(
            BOOKING_A, "confirmed", {}, clinic_id=sentinel
        )

    assert rec.rows[0]["clinic_id"] == CLINIC_A


@pytest.mark.asyncio
async def test_orphan_events_without_a_booking_stay_out_of_payment_events():
    """Signature failures have no booking, so they cannot carry a tenant.

    They must keep routing to webhook_security_events, since the constraint
    from migration 074 would reject them in payment_events.
    """
    from app.services.payment import PaymentService

    rec = _LedgerRecorder()
    with patch("app.services.payment.supabase", rec.supabase):
        await PaymentService()._log_payment_event_raw(
            None, "signature_failed", {"body_length": 42}
        )

    assert rec.rows == []
    assert "webhook_security_events" in rec.tables_touched()


@pytest.mark.asyncio
async def test_unresolvable_booking_is_preserved_not_invented_and_not_dropped():
    """An unresolvable clinic must not be guessed, and must not lose the event.

    Migration 074 makes clinic_id mandatory, so a tenant-less row can no longer
    go in the ledger at all. The event still has to survive: this is an audit
    trail, and a dropped row is the worst of the three outcomes. It takes the
    same route as a booking-less signature failure.
    """
    from app.services.payment import PaymentService

    rec = _LedgerRecorder(booking_clinic=None)
    with patch("app.services.payment.supabase", rec.supabase):
        await PaymentService()._log_payment_event(BOOKING_A, "confirmed", {})

    assert rec.rows == [], "a tenant-less row must never reach payment_events"
    assert "webhook_security_events" in rec.tables_touched()


def test_no_call_site_regresses_to_omitting_the_tenant():
    """Static guard: the logger must keep resolving, whatever callers do.

    Pins the fix at the one place all 25 call sites route through, so a future
    caller cannot reintroduce the defect just by forgetting a kwarg.
    """
    from app.services.payment import PaymentService

    src = inspect.getsource(PaymentService._log_payment_event_raw)
    assert "_resolve_event_clinic_id" in src, (
        "payment_events inserts no longer resolve clinic_id — KA-A-17 has regressed"
    )


# ── KA-A-02: refuse to guess ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_count_failure_fails_closed_instead_of_defaulting():
    """A broken lookup must not route an unknown number to the env-var tenant."""
    from app.services import tenant as tenant_mod

    async def _boom(_builder):
        raise RuntimeError("connection reset by peer")

    tenant_mod._tenant_cache.clear()
    with patch("app.services.tenant.sb", side_effect=_boom):
        with pytest.raises((tenant_mod.TenantNotFound, RuntimeError)) as exc:
            await tenant_mod.resolve_tenant("+919999999999", phone_number_id="pnid-x")

    # Whichever guard fires first, the forbidden outcome is a synthetic clinic.
    assert "default" not in str(exc.value)


@pytest.mark.asyncio
async def test_tenant_count_failure_alone_still_fails_closed():
    """Isolate the branch: strategies 1 and 2 succeed, only the count throws.

    This is the branch that mattered. It fired only when the database was
    already misbehaving, and resolved that ambiguity by picking a tenant.
    """
    from app.services import tenant as tenant_mod

    calls = {"n": 0}

    async def _rows(_builder):
        calls["n"] += 1
        if calls["n"] >= 3:  # the active-clinic count query
            raise RuntimeError("statement timeout")
        return MagicMock(data=[])

    tenant_mod._tenant_cache.clear()
    with patch("app.services.tenant.sb", side_effect=_rows):
        with pytest.raises(tenant_mod.TenantNotFound):
            await tenant_mod.resolve_tenant("+919999999996")


@pytest.mark.asyncio
async def test_multi_tenant_unknown_number_still_refused():
    """The pre-existing guard stays put: >1 active clinic and no match = refuse."""
    from app.services import tenant as tenant_mod

    calls = {"n": 0}

    async def _rows(_builder):
        calls["n"] += 1
        # Strategies 1-2 find no clinic for this number; the count query then
        # reports two active tenants, so there is nothing to resolve TO.
        if calls["n"] >= 3:
            return MagicMock(data=[{"id": CLINIC_A}, {"id": CLINIC_B}])
        return MagicMock(data=[])

    tenant_mod._tenant_cache.clear()
    with patch("app.services.tenant.sb", side_effect=_rows):
        with pytest.raises(tenant_mod.TenantNotFound):
            await tenant_mod.resolve_tenant("+919999999998")


@pytest.mark.asyncio
async def test_single_tenant_deployment_still_resolves():
    """Zero regression: the one legitimate fallback must keep working."""
    from app.services import tenant as tenant_mod

    calls = {"n": 0}

    async def _rows(_builder):
        calls["n"] += 1
        # Strategies 1-2 find nothing; the count query finds exactly one clinic.
        if calls["n"] >= 3:
            return MagicMock(data=[{"id": CLINIC_A, "is_active": True}])
        return MagicMock(data=[])

    tenant_mod._tenant_cache.clear()
    with patch("app.services.tenant.sb", side_effect=_rows):
        clinic = await tenant_mod.resolve_tenant("+919999999997")

    assert clinic is not None
    assert clinic["id"] == CLINIC_A


def test_idempotency_failure_does_not_widen_past_the_tenant():
    """A failed scoped check must not retry across every clinic's appointments.

    The old retry dropped the clinic predicate, so a transient error could match
    another tenant's confirmed row and declare this payment already processed.
    """
    from app.services.payment import PaymentService

    src = inspect.getsource(PaymentService.process_payment_webhook)
    assert "retrying global check" not in src
    assert "IDEMPOTENCY_CHECK_FAILED" in src
    # The refusal must be non-2xx so Razorpay redelivers a transient failure.
    assert '"code": 503' in src


# ── Credential and clinic-resolution fallbacks (production audit follow-up) ──


def test_clinic_must_supply_its_own_whatsapp_credentials():
    """A real clinic row with no token must not borrow the platform's.

    The fallback decided which number a patient's message was sent FROM, so a
    clinic that never entered credentials sent as the platform — and replies
    came back to whichever clinic owns that number.
    """
    from app.services.whatsapp import WhatsAppService

    with pytest.raises(ValueError):
        WhatsAppService()._get_credentials({"id": CLINIC_A, "config": {}})


def test_partial_whatsapp_credentials_are_also_refused():
    """A clinic's own phone id paired with a borrowed token is still a mismatch."""
    from app.services.whatsapp import WhatsAppService

    with pytest.raises(ValueError):
        WhatsAppService()._get_credentials(
            {"id": CLINIC_A, "config": {"meta_phone_number_id": "111222333"}}
        )


def test_configured_clinic_credentials_still_resolve():
    """Zero regression for every correctly configured clinic."""
    from app.services.whatsapp import WhatsAppService

    token, phone_id = WhatsAppService()._get_credentials(
        {
            "id": CLINIC_A,
            "config": {"meta_access_token": "tok_a", "meta_phone_number_id": "111"},
        }
    )
    assert (token, phone_id) == ("tok_a", "111")


def test_single_tenant_synthetic_clinic_still_sends():
    """The bootstrap deployment must keep working with no clinics row.

    _build_fallback_clinic() copies the env-var credentials into the synthetic
    clinic's own config, so it resolves through the normal clinic-scoped path
    and removing the global fallback cannot strand it.

    Both env values are set explicitly here rather than read from the ambient
    environment: this deployment ships an EMPTY WHATSAPP_PHONE_NUMBER_ID, since
    each clinic carries its own, so the ambient value would prove nothing.
    """
    from app.services import tenant as tenant_mod
    from app.services.whatsapp import WhatsAppService

    with patch.object(tenant_mod.settings, "whatsapp_token", "env_tok"),          patch.object(tenant_mod.settings, "whatsapp_phone_number_id", "env_phone"):
        synthetic = tenant_mod._build_fallback_clinic()

    assert WhatsAppService()._get_credentials(synthetic) == ("env_tok", "env_phone")


def test_empty_global_phone_id_was_already_failing_closed():
    """Pins why removing the fallback is not a behaviour change here.

    This deployment sets WHATSAPP_PHONE_NUMBER_ID to an empty string, so the
    old `or settings.whatsapp_phone_number_id` resolved to "" and raised for
    any clinic without its own number. The dangerous case — a clinic sending
    as the platform — was never reachable while that env var stays empty. The
    fix removes the dependence on that happening to be true.
    """
    from app.config import settings as app_settings
    from app.services.whatsapp import WhatsAppService

    with patch.object(app_settings, "whatsapp_phone_number_id", ""),          patch.object(app_settings, "whatsapp_token", "platform_token"):
        with pytest.raises(ValueError):
            WhatsAppService()._get_credentials({"id": CLINIC_A, "config": {}})


@pytest.mark.asyncio
async def test_get_clinic_by_id_refuses_to_guess_between_tenants():
    """No clinic_id plus several active clinics must raise, not pick the oldest."""
    from app.services import tenant as tenant_mod

    async def _rows(_builder):
        return MagicMock(data=[{"id": CLINIC_A}, {"id": CLINIC_B}])

    with patch("app.services.tenant.sb", side_effect=_rows):
        for missing in (None, "default", "", "null", "none"):
            with pytest.raises(tenant_mod.TenantNotFound):
                await tenant_mod.get_clinic_by_id(missing)


@pytest.mark.asyncio
async def test_get_clinic_by_id_still_resolves_for_one_active_clinic():
    """Zero regression: with nothing to choose between, resolve it."""
    from app.services import tenant as tenant_mod

    async def _rows(_builder):
        return MagicMock(data=[{"id": CLINIC_A, "name": "Only Clinic"}])

    with patch("app.services.tenant.sb", side_effect=_rows):
        assert (await tenant_mod.get_clinic_by_id(None))["id"] == CLINIC_A


@pytest.mark.asyncio
async def test_get_clinic_by_id_named_clinic_is_untouched():
    """The normal path — an explicit id — must not have changed at all."""
    from app.services import tenant as tenant_mod

    async def _rows(_builder):
        return MagicMock(data=[{"id": CLINIC_B, "name": "Named", "status": "ACTIVE"}])

    with patch("app.services.tenant.sb", side_effect=_rows):
        assert (await tenant_mod.get_clinic_by_id(CLINIC_B))["id"] == CLINIC_B


# ── Preflight send-identity check (operator tool guard) ─────────────────────


def _preflight():
    """Load the operator script by path; scripts/ is not an importable package."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "preflight_tenant_check.py"
    spec = importlib.util.spec_from_file_location("preflight_tenant_check", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_preflight_flags_two_clinics_sharing_one_number():
    """The Meta token is shared across clinics, so phone_number_id is the only
    thing separating them. Two clinics on one number is a silent cross-wire:
    a token covering both numbers sends happily and Meta reports no error."""
    duplicates, drifted = _preflight().check_send_identity([
        {"id": CLINIC_A, "name": "Alpha", "config": {"meta_phone_number_id": "555"}},
        {"id": CLINIC_B, "name": "Beta", "config": {"meta_phone_number_id": "555"}},
    ])
    assert duplicates == {"555": ["Alpha", "Beta"]}
    assert drifted == []


def test_preflight_flags_inbound_outbound_drift():
    """Sending as one number while being recognised as another strands replies."""
    _, drifted = _preflight().check_send_identity([
        {
            "id": CLINIC_A,
            "name": "Alpha",
            "config": {"meta_phone_number_id": "new999"},
            "phone_number_id": "old111",
        },
    ])
    assert drifted == [(CLINIC_A, "Alpha", "new999", "old111")]


def test_preflight_passes_a_correctly_configured_fleet():
    """Zero false positives on the shape production actually has."""
    duplicates, drifted = _preflight().check_send_identity([
        {"id": CLINIC_A, "name": "Alpha",
         "config": {"meta_phone_number_id": "111"}, "phone_number_id": "111"},
        {"id": CLINIC_B, "name": "Beta",
         "config": {"meta_phone_number_id": "222"}, "phone_number_id": "222"},
    ])
    assert duplicates == {} and drifted == []


# ── Onboarding a new client (the shared-token risk surface) ─────────────────


def _clinic_request(**over):
    from app.routers.clinics import CreateClinicRequest

    base = dict(
        name="New Client",
        whatsapp_number="+919000000001",
        plan="polyclinic",
        meta_phone_number_id="1296654790197336",
        meta_access_token="EAAG_shared_app_token",
    )
    base.update(over)
    return CreateClinicRequest(**base)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "db_error, expected",
    [
        (
            'duplicate key value violates unique constraint "idx_clinics_phone_number_id"',
            "Meta Phone Number ID",
        ),
        (
            'duplicate key value violates unique constraint "clinics_whatsapp_number_key"',
            "WhatsApp number",
        ),
    ],
)
async def test_duplicate_onboarding_names_the_field_that_collided(db_error, expected):
    """The operator pastes the same permanent token for every client, so
    phone_number_id is the only field that separates them. Blaming the
    WhatsApp number for a phone_number_id clash sent them to check a field
    that was already correct."""
    from fastapi import HTTPException

    from app.routers import clinics as clinics_mod

    async def _boom(_builder):
        raise Exception(db_error)

    with patch("app.routers.clinics.sb", side_effect=_boom):
        with pytest.raises(HTTPException) as exc:
            await clinics_mod.provision_clinic(_clinic_request())

    assert exc.value.status_code == 409
    assert expected in exc.value.detail


def test_onboarding_can_pin_the_integration_secret():
    """The create form had no field for it, so every clinic was unpinned by
    construction rather than by oversight."""
    from app.routers.clinics import CreateClinicRequest

    req = _clinic_request(integration_secret="per_clinic_key")
    assert req.integration_secret == "per_clinic_key"
    assert "integration_secret" in CreateClinicRequest.model_fields
    # Optional, so existing onboarding calls keep working unchanged.
    assert _clinic_request().integration_secret is None
