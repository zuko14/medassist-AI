"""Regression tests for the P0/P1 findings of the 2026-08-30 forensic audit.

One test per defect. Each fails against the pre-fix code.

  RT-03  KA-P1-04  recovery must drop the stale claim before replaying
  RT-04  KA-P1-04  an unresolved tenant must dead-letter, never complete
  RT-06  KA-P1-05  no cross-tenant doctor read/write on the branch-only path
  RT-08  KA-P2-07  an amount mismatch must not demote a settled booking
  RT-16  chain     no migration may record itself in schema_migrations
"""

import pathlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── RT-16: the migration chain must apply to a fresh database ────────────────

def test_rt16_no_migration_records_itself_in_schema_migrations():
    """scripts/migrate.py owns schema_migrations; migrations must not write it.

    scripts/migrate.py:124 inserts (name, checksum) after applying each file,
    and checksum is NOT NULL (scripts/migrate.py:60). A migration that also
    does `INSERT INTO schema_migrations (name) VALUES (...)` omits the
    checksum, violates the constraint, and aborts.

    Migrations 056-063 all did this, so the chain died at 056 on any fresh
    database: every new deployment, every disaster-recovery rebuild, and the
    CI integration job. It was invisible because existing databases already
    had those rows and the runner skips applied files by name.
    """
    offenders = []
    for path in sorted(pathlib.Path("migrations").glob("[0-9]*.sql")):
        text = path.read_text(encoding="utf-8")
        # Ignore the token inside SQL comments — the fix left explanatory notes.
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("--")
        )
        if re.search(r"INSERT\s+INTO\s+schema_migrations", code, re.I):
            offenders.append(path.name)

    assert not offenders, (
        "These migrations record themselves in schema_migrations without a "
        "checksum, which breaks the chain on a fresh database: "
        f"{offenders}. Let scripts/migrate.py do it."
    )


def test_rt16b_rollback_scripts_are_invisible_to_the_runner():
    """Down-migrations must not sit where the runner will auto-apply them.

    apply_migrations() globs `migrations/[0-9]*.sql` (scripts/migrate.py:95),
    so a file named migrations/064_down.sql WOULD be picked up and applied as
    a forward migration — silently reverting the fix on the next deploy.
    Rollback scripts live in migrations/rollback/, which the non-recursive
    glob cannot reach.
    """
    runner_visible = {p.name for p in pathlib.Path("migrations").glob("[0-9]*.sql")}
    assert not any("down" in name.lower() for name in runner_visible), (
        "a rollback script is in the runner's glob path and will be "
        "auto-applied as a forward migration"
    )

    rollback_dir = pathlib.Path("migrations/rollback")
    if rollback_dir.exists():
        assert list(rollback_dir.glob("*.sql")), "rollback dir exists but is empty"


# ── RT-03: recovery must release the stale claim before replaying ────────────

@pytest.mark.asyncio
async def test_rt03_recovery_releases_stale_claim_before_replay():
    """KA-P1-04: the claim/durable-row write pair is not atomic.

    process_message() calls acquire() (writes processed_messages) and then
    claim_message() (moves the durable row to 'processing') in two separate
    round-trips. A crash between them leaves the claim written while the row
    is still 'received' — exactly what this sweep selects.

    Without the release, the replay's acquire() sees its own orphaned claim,
    logs "duplicate dropped", returns, and process_message_safe() marks the
    row completed. The patient's message is discarded and recorded as handled.
    """
    from contextlib import asynccontextmanager

    from app.services.scheduler import SchedulerService

    call_order = []

    row = {
        "message_id": "wamid.STALE1",
        "phone": "+919876543210",
        "display_phone": "+919999999999",
        "phone_number_id": "pnid-1",
        "payload": {"entry": [{"changes": [{"value": {"messages": [
            {"id": "wamid.STALE1", "from": "919876543210", "type": "text",
             "timestamp": "1756500000", "text": {"body": "hello"}}
        ]}}]}]},
        "attempt_count": 0,
    }

    db = MagicMock()
    (db.table.return_value.select.return_value.in_.return_value.lt.return_value
       .order.return_value.limit.return_value.execute.return_value) = MagicMock(data=[row])

    async def _release(mid):
        call_order.append(("release", mid))

    async def _process(msg, display_phone, payload, phone_number_id=None):
        call_order.append(("process", getattr(msg, "id", None)))

    mq = MagicMock()
    mq.claim_message = AsyncMock(return_value=True)
    mq.release = AsyncMock(side_effect=_release)
    mq.mark_failed = AsyncMock()

    @asynccontextmanager
    async def _lock(*a, **k):
        yield True

    # recover_pending_inbound_messages() imports `supabase` from app.database
    # inside the function body, shadowing the module-level scheduler binding —
    # so app.database.supabase is the one that must be patched.
    with patch("app.services.distributed_lock.distributed_job_lock", _lock), \
         patch("app.services.message_queue.message_queue", mq), \
         patch("app.database.supabase", db), \
         patch("app.routers.webhook.process_message_safe", AsyncMock(side_effect=_process)):
        await SchedulerService().recover_pending_inbound_messages()

    assert ("release", "wamid.STALE1") in call_order, (
        "recovery replayed without dropping the stale processed_messages claim "
        "— acquire() will reject the replay as its own duplicate and the "
        "message will be silently marked completed (KA-P1-04)"
    )
    release_idx = call_order.index(("release", "wamid.STALE1"))
    process_idx = next(
        (i for i, c in enumerate(call_order) if c[0] == "process"), len(call_order)
    )
    assert release_idx < process_idx, "the claim must be released BEFORE the replay"


# ── RT-04: an unresolved tenant must dead-letter, never complete ─────────────

@pytest.mark.asyncio
async def test_rt04_unresolved_tenant_dead_letters_and_never_completes():
    """KA-P1-04, second loss path.

    process_message() used to `return` when resolve_tenant() gave back None,
    so process_message_safe() fell through to mark_completed(). A misconfigured
    or newly-provisioned WABA mapping therefore discarded every inbound patient
    message and recorded each one as successfully handled — no dead-letter row,
    no alert, no retry.
    """
    from app.routers import webhook as webhook_mod

    message = MagicMock()
    message.id = "wamid.NOTENANT"
    message.from_ = "919876543210"
    message.type = "text"

    mq = MagicMock()
    mq.mark_completed = AsyncMock()
    mq.mark_failed = AsyncMock()
    mq.release = AsyncMock()

    with patch.object(webhook_mod, "message_queue", mq), \
         patch.object(webhook_mod, "resolve_tenant", AsyncMock(return_value=None)):
        await webhook_mod.process_message_safe(
            message, "+919999999999", {}, "pnid-unknown"
        )

    mq.mark_completed.assert_not_awaited()
    mq.mark_failed.assert_awaited_once()

    kwargs = mq.mark_failed.await_args.kwargs
    assert kwargs.get("max_retries") == 0, (
        "a missing WABA mapping is permanent — retrying it three times only "
        "produces three identical errors and pollutes the DLQ"
    )


# ── RT-06: no cross-tenant doctor access ─────────────────────────────────────

@pytest.mark.asyncio
async def test_rt06_branch_only_update_cannot_reach_another_tenants_doctor():
    """KA-P1-05.

    A body containing ONLY branch fields left update_data empty, skipped the
    clinic-scoped UPDATE, and fell into an else-branch that read the doctor by
    id alone — returning another tenant's full record and rewriting its
    doctor_branches rows.
    """
    from fastapi import HTTPException

    from app.routers.admin import AdminUser, DoctorUpdate, update_doctor

    attacker = AdminUser(
        username="clinic_a_admin",
        role="clinic_admin",
        clinic_id="11111111-1111-1111-1111-111111111111",
        user_id="user-a",
    )

    captured = {}

    def _table(name):
        tbl = MagicMock()

        def _select(*a, **k):
            q = MagicMock()

            def _eq(col, val):
                captured.setdefault(name, []).append((col, val))
                return q

            q.eq = _eq
            # Tenant B's doctor is NOT visible under clinic A's predicate.
            q.execute = MagicMock(return_value=MagicMock(data=[]))
            return q

        tbl.select = _select
        return tbl

    db = MagicMock()
    db.table = _table

    body = DoctorUpdate(branch_id="22222222-2222-2222-2222-222222222222")

    with patch("app.routers.admin.supabase", db):
        with pytest.raises(HTTPException) as exc:
            await update_doctor(
                doctor_id="99999999-9999-9999-9999-999999999999",  # tenant B
                doctor=body,
                request=None,
                clinic_id="default",
                user=attacker,
            )

    assert exc.value.status_code == 404, (
        "a foreign doctor_id must 404, not leak the record or a 400 that "
        "confirms the id exists in another tenant"
    )

    doctor_preds = captured.get("doctors", [])
    assert any(col == "clinic_id" for col, _ in doctor_preds), (
        "the doctors lookup ran without a clinic_id predicate — this is the "
        "unscoped cross-tenant read (KA-P1-05)"
    )


# ── RT-08: an amount mismatch must not demote a settled booking ──────────────

@pytest.mark.asyncio
async def test_rt08_amount_mismatch_cannot_demote_a_confirmed_booking():
    """KA-P2-07.

    The step-4 idempotency check only suppresses a REPEAT of the same
    payment_id. A second, DIFFERENT payment against an already-confirmed
    booking reached the mismatch branch, which had no status CAS, and demoted
    a confirmed appointment to pending_review while overwriting payment_id —
    orphaning the payment that actually confirmed it.
    """
    import json

    from app.services.payment import PaymentService

    service = PaymentService()

    confirmed_booking = {
        "id": "booking-1",
        "booking_ref": "MC-2026-0001",
        "clinic_id": "11111111-1111-1111-1111-111111111111",
        "status": "confirmed",
        "amount_paise": 50000,
        "payment_id": "pay_ORIGINAL",
        "patient_phone": "+919876543210",
    }

    updates = []

    def _table(name):
        tbl = MagicMock()
        q = MagicMock()
        preds = []

        def _eq(col, val):
            preds.append((col, val))
            return q

        q.eq = _eq
        q.neq = MagicMock(return_value=q)
        q.limit = MagicMock(return_value=q)
        q.in_ = MagicMock(return_value=q)

        def _execute():
            if name == "clinics":
                # Single-tenant, so the unscoped-webhook guard does not fire.
                return MagicMock(data=[{"id": "c1"}])
            if name != "appointments":
                return MagicMock(data=[])
            # Honour the predicates. The step-4 idempotency check filters on
            # payment_id == pay_SECOND; this booking was confirmed by
            # pay_ORIGINAL, so that query must return nothing — otherwise the
            # webhook short-circuits as "already_processed" and never reaches
            # the mismatch branch this test exists to exercise.
            for col, val in preds:
                if col == "payment_id" and val != confirmed_booking["payment_id"]:
                    return MagicMock(data=[])
            return MagicMock(data=[confirmed_booking])

        q.execute = _execute
        tbl.select = MagicMock(return_value=q)

        def _update(payload):
            updates.append(payload)
            return q

        tbl.update = _update
        tbl.insert = MagicMock(return_value=q)
        return tbl

    db = MagicMock()
    db.table = _table

    payload = json.dumps({
        "event": "payment.captured",
        "event_id": "evt_2",
        "payload": {"payment": {"entity": {
            "id": "pay_SECOND",
            "amount": 999,                       # wrong amount
            "notes": {"booking_id": "booking-1"},
        }}},
    }).encode()

    with patch("app.services.payment.supabase", db), \
         patch.object(service, "verify_webhook_signature", return_value=True), \
         patch.object(service, "_log_payment_event"), \
         patch.object(service, "_log_payment_event_raw"), \
         patch.object(service, "_alert_admin", new_callable=AsyncMock) as alert:
        result = await service.process_payment_webhook(
            payload, "sig", webhook_secret="s",
            clinic_id="11111111-1111-1111-1111-111111111111",
        )

    assert not any(u.get("status") == "pending_review" for u in updates), (
        "a confirmed booking was demoted to pending_review by a mismatched "
        "second payment (KA-P2-07)"
    )
    assert not any(u.get("payment_id") == "pay_SECOND" for u in updates), (
        "the original payment_id was overwritten, orphaning the payment that "
        "actually confirmed this booking"
    )
    assert result["reason"] == "mismatch_on_settled_booking"
    alert.assert_awaited()
