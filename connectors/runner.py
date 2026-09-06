"""Connector Runner — Entry point for the MocDoc worker process.

This is a SEPARATE process from FastAPI. It uses APScheduler to poll
MocDoc (or any other enabled connector) at regular intervals.

Usage:
    # One-shot test run
    python -m connectors.runner --connector mocdoc --clinic-id <uuid> --once

    # One-shot test run for a specific branch of a multi-branch clinic
    python -m connectors.runner --connector mocdoc --clinic-id <uuid> --branch-id <uuid> --once

    # Dry run (login + parse, NO downloads)
    python -m connectors.runner --connector mocdoc --clinic-id <uuid> --once --dry-run

    # Production: poll all enabled connectors every 10 minutes
    python -m connectors.runner --all

    # Encrypt a MocDoc password (for initial setup)
    python -m connectors.runner --encrypt-password
"""

import argparse
import asyncio
import json
import logging
import math
import os
import re
import signal
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

# ── CRITICAL: Force stdlib event loop policy BEFORE any library imports ──
# uvicorn[standard] installs uvloop as a transitive dependency.  If uvloop's
# EventLoopPolicy gets activated (by import side-effects from app modules,
# APScheduler, or httptools), asyncio.get_event_loop() returns a uvloop.Loop
# whose _make_subprocess_transport() does NOT support the same child-watcher
# protocol as CPython's _UnixSelectorEventLoop — Playwright's subprocess
# spawning then raises NotImplementedError at base_events.py:528.
#
# Forcing DefaultEventLoopPolicy guarantees we get a _UnixSelectorEventLoop
# with full subprocess transport support.  This MUST happen before we import
# app modules (which pull in uvicorn, httptools, etc. as transitive deps).
if sys.platform != "win32":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

# Add project root to path so we can import app modules
PROJECT_ROOT = str(Path(__file__).parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from app.config import settings
from app.database import supabase
from app.utils.connector_crypto import (
    decrypt_password,
    describe_decrypt_failure,
    encrypt_password,
)
from app.services.patient_match import MatchResult, patient_match_service
from app.services.distributed_lock import distributed_job_lock
from connectors.mocdoc.worker import MocDocConnector
from app.database import sb  # T5.1: off-loop query execution

logger = logging.getLogger("connectors")

CONNECTOR_REGISTRY = {
    "mocdoc": MocDocConnector,
}


def setup_logging(level: str = "INFO"):
    """Configure logging for the connector worker."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reduce noise from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)


def _scope_by_branch(query, branch_id: str = None):
    """Apply a branch_id filter matching NULL (clinic-level) or a specific branch."""
    return query.is_("branch_id", "null") if not branch_id else query.eq("branch_id", branch_id)


def _mask_sample_name(name: str) -> str:
    """Mask a patient name for the dry-run sample: keep each word's first
    letter (e.g. 'John Smith' -> 'J•••• S••••'), never expose the full name."""
    if not name:
        return ""
    return " ".join(w[:1] + "•" * max(0, len(w) - 1) for w in name.split())


def _mask_phone(phone: str) -> str:
    """Mask a patient phone for the dry-run sample: last 4 digits only
    (e.g. '+919999999999' -> '***9999'), matching ReportMetadata.__repr__'s
    existing masking convention used in logs."""
    if not phone:
        return ""
    return f"***{phone[-4:]}"


LOCK_LEASE = timedelta(minutes=5)

# Connector IDs this process currently holds the advisory lock for.
# Drained on graceful shutdown (see release_all_locks_held) so a killed
# process doesn't leave a stale lock blocking the next Test Connection.
_locks_held_by_this_process: set[str] = set()

#: Identifies THIS process as a lock owner. The previous default was the
#: literal "worker-1" for every process, so locked_by named nobody and an
#: ownership check was impossible.
CONNECTOR_WORKER_ID = f"conn_{os.getpid()}_{uuid.uuid4().hex[:8]}"


async def acquire_connector_lock(connector_id: str, worker_id: str = None) -> tuple[bool, int]:
    """Acquire distributed advisory lock on connector record (5 min lease).

    KA-10: Uses CAS-style atomic UPDATE to prevent TOCTOU race.
    Fails CLOSED on any exception (returns False, not True).

    Returns (acquired, remaining_minutes). remaining_minutes is 0 when
    acquired; otherwise it's the lock's remaining TTL rounded up to the
    nearest minute (minimum 1), for surfacing "retry in ~Nm" to the admin UI.
    """
    worker_id = worker_id or CONNECTOR_WORKER_ID
    try:
        now_str = datetime.now(timezone.utc).isoformat()
        lease_cutoff = (datetime.now(timezone.utc) - LOCK_LEASE).isoformat()

        # CAS: atomically update only if unlocked or lease expired
        # This eliminates the TOCTOU race where two workers could both
        # read "unlocked" and then both write their lock.
        update_result = (
            # unscoped: unique_row_key
            await sb(supabase.table("integration_connectors")
            .update({
                "locked_at": now_str,
                "locked_by": worker_id,
            })
            .eq("id", connector_id)
            .or_(f"locked_at.is.null,locked_at.lt.{lease_cutoff}"))
        )

        if update_result.data:
            _locks_held_by_this_process.add(connector_id)
            return True, 0

        # Lock is held by another process — compute remaining TTL
        try:
            res = (
                # unscoped: unique_row_key
                await sb(supabase.table("integration_connectors")
                .select("locked_at")
                .eq("id", connector_id))
            )
            if res.data and res.data[0].get("locked_at"):
                dt = datetime.fromisoformat(res.data[0]["locked_at"].replace("Z", "+00:00"))
                elapsed = datetime.now(timezone.utc) - dt
                remaining = max(1, math.ceil((LOCK_LEASE - elapsed).total_seconds() / 60))
                return False, remaining
        except Exception:
            pass

        return False, 1

    except Exception as e:
        # KA-10: Fail CLOSED — do NOT proceed on lock acquisition failure.
        # The previous code returned (True, 0) here, allowing concurrent runs.
        logger.error(f"LOCK_ACQUIRE_FAILED connector={connector_id}: {e}")
        return False, 0


async def renew_connector_lock(connector_id: str) -> None:
    """Push the lock lease forward while a run is still making progress.

    A full 17-report run takes ~6 min, which is longer than LOCK_LEASE (5 min).
    Without renewal the lease expires mid-run and a second worker starts a
    concurrent run against the same MocDoc account — observed in production as
    two interleaved download temp dirs. Called after each report so a genuinely
    crashed run still frees the lock within one lease.
    """
    if connector_id not in _locks_held_by_this_process:
        return
    try:
        # Renew only OUR lease. Without the locked_by predicate a process whose
        # lease had already been taken over would keep pushing the new owner's
        # expiry forward, hiding the takeover from the TTL that is supposed to
        # surface it.
        # unscoped: unique_row_key
        renewed = await sb(supabase.table("integration_connectors").update({
            "locked_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", connector_id).eq("locked_by", CONNECTOR_WORKER_ID))
        if not renewed.data:
            logger.warning(
                f"Lease lost for connector {connector_id} — another process owns "
                f"it now; this run will stop renewing"
            )
            _locks_held_by_this_process.discard(connector_id)
    except Exception as e:
        logger.warning(f"Could not renew lock for connector {connector_id}: {e}")


async def release_connector_lock(connector_id: str) -> None:
    """Release the advisory lock — ONLY if this process actually holds it.

    This used to clear locked_at unconditionally. _run_connector() sets
    connector_id BEFORE attempting the lock, and its finally-block released on
    every exit path — including the early return taken when the lock could NOT
    be acquired. So a worker that lost the race freed the WINNER's lock on its
    way out, and the next tick walked straight in. Observed in production as
    seven concurrent runs of one connector, each ~6 minutes, all scraping the
    same MocDoc account at once.

    Two guards, because either alone is insufficient:
      - the in-process set stops us releasing a lock we never took;
      - the locked_by predicate stops us releasing one a DIFFERENT process took
        (our set would be empty after a restart, but so would our ownership).
    """
    if connector_id not in _locks_held_by_this_process:
        logger.debug(
            f"Not releasing connector {connector_id}: not held by this process"
        )
        return
    try:
        # unscoped: unique_row_key
        await sb(supabase.table("integration_connectors").update({
            "locked_at": None,
            "locked_by": None,
        }).eq("id", connector_id).eq("locked_by", CONNECTOR_WORKER_ID))
    except Exception as e:
        logger.warning(f"Could not release lock for connector {connector_id}: {e}")
    finally:
        _locks_held_by_this_process.discard(connector_id)


async def release_all_locks_held() -> None:
    """Release every connector lock this process currently holds.

    Called on graceful shutdown (FastAPI lifespan, SIGTERM in scheduled
    mode) so a killed process doesn't leave a stale lock blocking the
    next Test Connection for the full lease.
    """
    for connector_id in list(_locks_held_by_this_process):
        await release_connector_lock(connector_id)


async def send_admin_alert(clinic_id: str, message: str, branch_id: str = None) -> bool:
    """Send a WhatsApp alert to the admin phone number. Returns True if delivered.

    send_text returns False (it does not raise) when the send is refused — most
    commonly because the admin's 24h customer-service window has expired. This
    used to be logged as "Admin alert sent", so a connector could fail 17 times
    while every alert about it was silently dropped.
    """
    try:
        from app.services.whatsapp import whatsapp_service
        from app.services.tenant import get_clinic_by_id

        clinic = await get_clinic_by_id(clinic_id)

        # Get admin alert phone from connector config
        query = (
            supabase.table("integration_connectors")
            .select("config")
            .eq("clinic_id", clinic_id)
            .eq("connector_type", "mocdoc")
        )
        connector = await sb(_scope_by_branch(query, branch_id).single())

        admin_phone = connector.data.get("config", {}).get("admin_alert_phone")
        if not admin_phone:
            logger.warning("No admin_alert_phone configured — alert not sent")
            return False

        sent = await whatsapp_service.send_text(clinic, admin_phone, message)
        if sent:
            logger.info(f"Admin alert sent to ***{admin_phone[-4:]}")
            return True

        # Freeform was refused — almost always the admin's 24h customer-service
        # window has expired. Alerts matter most exactly when nobody has been
        # chatting with the bot, so fall back to the approved utility template.
        # Per-clinic template > connector config > global env var
        connector_cfg = connector.data.get("config", {})
        clinic_cfg = clinic.get("config", {}) if isinstance(clinic, dict) else {}
        template = (
            connector_cfg.get("admin_alert_template_name")
            or clinic_cfg.get("admin_alert_template_name")
            or settings.admin_alert_template_name
        )
        if not template:
            logger.error(
                f"ADMIN_ALERT_UNDELIVERED to ***{admin_phone[-4:]} — outside the "
                f"24h window and ADMIN_ALERT_TEMPLATE_NAME is not configured "
                f"(checked connector config, clinic config, and global env). "
                f"Undelivered alert: {message[:200]}"
            )
            return False

        # Meta rejects newlines, tabs and 4+ consecutive spaces inside template
        # parameters (error 132000). Every alert body here is multi-line, so it
        # must be flattened; 1024 is Meta's per-parameter ceiling.
        flat = re.sub(r"\s+", " ", message).strip()[:1000]
        sent = await whatsapp_service.send_template(
            clinic,
            admin_phone,
            template_name=template,
            components=[
                {"type": "body", "parameters": [{"type": "text", "text": flat}]}
            ],
            _source="connector_alert",
        )
        if sent:
            logger.info(f"Admin alert sent to ***{admin_phone[-4:]} via template")
        else:
            logger.error(
                f"ADMIN_ALERT_UNDELIVERED to ***{admin_phone[-4:]} — both freeform "
                f"and template '{template}' were refused. "
                f"Undelivered alert: {message[:200]}"
            )
        return sent
    except Exception as e:
        logger.error(f"Failed to send admin alert: {e}")
        return False


async def notify_unverified_deliveries(
    clinic_id: str, count: int, connector_type: str = "connector"
) -> bool:
    """Raise an in-panel notification for reports sent to unregistered numbers.

    These deliveries are intentional: a walk-in's number comes from the HMIS and
    the clinic has no prior record to match it against, so blocking on that
    check would stop every delivery. The control is visibility instead — the
    clinic sees the count, and each row stays stamped match_source="moc_doc_only"
    so an individual misroute can be found and recalled.

    Clinic-wide (admin_id NULL) so every admin of that clinic sees it. Never
    raises: a notification failure must not fail a connector run that already
    delivered the reports successfully.
    """
    if count <= 0:
        return False
    try:
        await sb(
            # unscoped: insert_scoped_by_payload — the row carries clinic_id and
            # admin_notifications is read back through clinic-scoped routes.
            supabase.table("admin_notifications").insert(
                {
                    "clinic_id": clinic_id,
                    "admin_id": None,
                    "title": f"{count} report{'s' if count != 1 else ''} sent to unverified numbers",
                    "message": (
                        f"{count} lab report{'s were' if count != 1 else ' was'} delivered "
                        f"via {connector_type} to phone number"
                        f"{'s' if count != 1 else ''} not registered in your patient list. "
                        f"This is normal for walk-in patients. Open Reports and filter by "
                        f"'Walk-in / unverified' to review the recipients."
                    )[:2000],
                    "is_read": False,
                }
            )
        )
        logger.info(
            f"Notified clinic {clinic_id}: {count} unverified-recipient deliveries"
        )
        return True
    except Exception as e:
        logger.error(f"Could not raise unverified-delivery notification: {e}")
        return False


async def record_report_failure(
    clinic_id: str,
    connector_type: str,
    external_report_id: str,
    error_message: str,
    vam_id: str = None,
    patient_name: str = None,
    alert_threshold: int = 3,
    branch_id: str = None,
) -> None:
    """Record a report failure and send an admin alert if failure threshold is reached."""
    try:
        now = datetime.now(timezone.utc).isoformat()

        query = (
            supabase.table("connector_failed_reports")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("connector_type", connector_type)
            .eq("external_report_id", external_report_id)
        )
        existing = await sb(_scope_by_branch(query, branch_id))

        if existing.data and len(existing.data) > 0:
            row = existing.data[0]
            new_count = row.get("failure_count", 0) + 1
            # unscoped: unique_row_key
            await sb(supabase.table("connector_failed_reports").update(
                {
                    "failure_count": new_count,
                    "last_error": error_message,
                    "last_attempt_at": now,
                    "resolved_at": None,
                }
            ).eq("id", row["id"]))

            if new_count >= alert_threshold and row.get("failure_count", 0) < alert_threshold:
                alert_msg = (
                    f"⚠️ MocDoc Report Failure Alert!\n"
                    f"Report ID: {external_report_id} (VAM: {vam_id or 'N/A'})\n"
                    f"Patient: {patient_name or 'Unknown'}\n"
                    f"Consecutive Failures: {new_count}\n"
                    f"Last Error: {error_message}"
                )
                await send_admin_alert(clinic_id, alert_msg, branch_id=branch_id)
        else:
            # unscoped: insert_scoped_by_payload
            await sb(supabase.table("connector_failed_reports").insert(
                {
                    "clinic_id": clinic_id,
                    "connector_type": connector_type,
                    "external_report_id": external_report_id,
                    "vam_id": vam_id,
                    "patient_name": patient_name,
                    "failure_count": 1,
                    "last_error": error_message,
                    "first_failed_at": now,
                    "last_attempt_at": now,
                    "resolved_at": None,
                    "branch_id": branch_id,
                }
            ))
    except Exception as e:
        logger.error(f"Failed to record report failure: {e}")


async def record_report_success(
    clinic_id: str,
    connector_type: str,
    external_report_id: str,
    branch_id: str = None,
) -> None:
    """Clear/resolve per-report failure tracking upon successful report processing."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        query = (
            supabase.table("connector_failed_reports")
            .update({"resolved_at": now})
            .eq("clinic_id", clinic_id)
            .eq("connector_type", connector_type)
            .eq("external_report_id", external_report_id)
        )
        await sb(_scope_by_branch(query, branch_id).is_("resolved_at", "null"))
    except Exception as e:
        logger.error(f"Failed to resolve report failure tracking: {e}")


PLACEHOLDER_CREDENTIAL_MARKERS = (
    "your_",
    "changeme",
    "change_me",
    "placeholder",
    "example.com",
)


def _placeholder_credential(config: dict) -> str | None:
    """Return the field name holding a template value, if any."""
    for field in ("username", "password"):
        value = str(config.get(field) or "").strip().lower()
        if value and any(m in value for m in PLACEHOLDER_CREDENTIAL_MARKERS):
            return field
    return None


# ── Subprocess-capable event loop for Playwright ─────────────────────────────
# Playwright starts its Node driver via asyncio.create_subprocess_exec, so the
# loop a connector runs on MUST implement _make_subprocess_transport.
#
# Very often it does not.  `uvicorn --reload` (which app/main.py enables
# whenever APP_ENV=development) and `uvicorn --workers N` both make uvicorn
# build a SelectorEventLoop.  On Windows that is BaseSelectorEventLoop, which
# never overrides _make_subprocess_transport, so BaseEventLoop's stub runs and
# raises a bare `NotImplementedError` carrying no message — exactly the
# "Error: NotImplementedError:" the admin dashboard reports.
#
# A running loop's type cannot be changed, so no event-loop-policy swap or
# child-watcher reinstall can repair the host loop.  The work has to move to a
# loop that can spawn.  Only the connector body moves; app/database.sb() already
# runs PostgREST calls on its own executor, and run_connector holds no
# loop-bound state, so it is safe on any loop.
#
# ponytail: 2 threads bounds concurrent Chromiums (memory). Raise it if manual
# admin runs start queueing behind scheduled polls.
_CONNECTOR_LOOP_POOL = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="connector-loop"
)


def _loop_supports_subprocess(loop) -> bool:
    """True if `loop` can spawn subprocesses, i.e. Playwright can start on it.

    Checks for the un-overridden BaseEventLoop stub rather than testing the
    platform or loop class, so uvloop, ProactorEventLoop and the Unix selector
    loop are all recognised without hardcoding a list.
    """
    # uvloop.Loop does not subclass BaseEventLoop and has no such attribute at
    # all, so this must be getattr-safe — probing it directly raised
    # "type object 'Loop' has no attribute '_make_subprocess_transport'".
    impl = getattr(type(loop), "_make_subprocess_transport", None)
    return (
        impl is not None
        and impl is not asyncio.BaseEventLoop._make_subprocess_transport
    )


def _new_subprocess_loop() -> asyncio.AbstractEventLoop:
    """A fresh event loop that can spawn subprocesses, independent of policy.

    Deliberately constructs the stdlib loop class directly rather than calling
    asyncio.new_event_loop(): that goes through the global policy, which may be
    uvloop's, and would hand back exactly the loop we are trying to avoid.
    """
    if sys.platform == "win32":
        # Windows SelectorEventLoop cannot spawn subprocesses at all.
        return asyncio.ProactorEventLoop()
    # POSIX: asyncio.SelectorEventLoop is _UnixSelectorEventLoop, which does
    # implement _make_subprocess_transport.
    return asyncio.SelectorEventLoop()


def _ensure_child_watcher() -> None:
    """Guarantee a usable child watcher for the loop we are about to run.

    On POSIX, _UnixSelectorEventLoop._make_subprocess_transport asks the GLOBAL
    policy for a child watcher. If that policy is uvloop's, the call can raise a
    message-less NotImplementedError — which surfaces on the dashboard as the
    bare "NotImplementedError:" with no location.

    Never raises: a failure here must not become the connector's error.
    """
    if sys.platform == "win32":
        return  # Windows has no child watchers; ProactorEventLoop needs none.

    import warnings

    with warnings.catch_warnings():
        # get_child_watcher is deprecated in 3.12 and removed in 3.14.
        warnings.simplefilter("ignore", DeprecationWarning)
        if not hasattr(asyncio, "get_child_watcher"):
            return  # 3.14+: loops handle child reaping themselves.
        try:
            watcher = asyncio.get_child_watcher()
            if watcher is not None and watcher.is_active():
                return
        except Exception:
            pass
        # The policy refused or gave an inactive watcher. Fall back to the
        # stdlib policy plus a ThreadedChildWatcher, which works off the main
        # thread. This does not disturb any already-running loop.
        try:
            asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
            asyncio.get_event_loop_policy().set_child_watcher(
                asyncio.ThreadedChildWatcher()
            )
        except Exception as exc:
            logger.warning("Could not install a child watcher: %r", exc)


async def run_connector(*args, **kwargs) -> dict:
    """Execute one connector run on a loop that is known to spawn Playwright.

    The run ALWAYS moves to a dedicated thread owning a freshly built stdlib
    loop. Earlier versions tried to detect whether the caller's loop was
    capable and ran inline when it looked fine; that guessed wrong repeatedly
    (uvicorn's uvloop.Loop in the web service, uvicorn's SelectorEventLoop
    under --reload on Windows, and whatever the worker's policy produced), and
    each wrong guess cost a deploy to disprove. Owning the loop outright is
    both smaller and deterministic: Playwright never touches the host loop.

    _run_connector holds no loop-bound state and app/database.sb() runs
    PostgREST calls on its own executor, so it is safe on any loop.
    """
    caller_loop = asyncio.get_running_loop()
    _ensure_child_watcher()

    def _in_own_loop() -> dict:
        with asyncio.Runner(loop_factory=_new_subprocess_loop) as runner:
            # Playwright uses get_running_loop(), but set the thread-local loop
            # too so any dependency calling get_event_loop() sees the right one.
            asyncio.set_event_loop(runner.get_loop())
            try:
                return runner.run(_run_connector(*args, **kwargs))
            finally:
                # Pool threads are reused — do not leak a closed loop.
                asyncio.set_event_loop(None)

    return await caller_loop.run_in_executor(_CONNECTOR_LOOP_POOL, _in_own_loop)


async def _run_connector(
    clinic_id: str,
    connector_type: str = "mocdoc",
    dry_run: bool = False,
    limit: int = 0,
    vam_id_filter: str = "",
    branch_id: str = None,
    ignore_enabled: bool = False,
) -> dict:
    """Execute a single connector run for a specific clinic (and, for
    multi-branch diagnostic centers, a specific branch's connector row).

    Args:
        clinic_id: UUID of the clinic
        connector_type: Type of connector (currently only "mocdoc")
        dry_run: If True, authenticate and parse only — no downloads
        limit: Max reports to process (0 = no limit)
        vam_id_filter: Only process report matching this VAM ID
        branch_id: UUID of the branch this connector belongs to, or None for
            a clinic-level (single-branch) connector

    Returns:
        Summary dict with run results
    """
    start_time = time.time()
    connector_id = None  # Must be set before try so finally-block guard works
    summary = {
        "run_status": "failed",
        "reports_found": 0,
        "reports_new": 0,
        "reports_matched": 0,
        "reports_needs_review": 0,
        "reports_uploaded": 0,
        "reports_delivered": 0,
        # Delivered to a phone number the clinic has no record of. For a
        # diagnostic centre that is every walk-in, which is exactly why these
        # are counted and reported rather than blocked.
        "reports_delivered_unverified": 0,
        "reports_skipped_already_processed": 0,
        "reports_failed": 0,
        "duration_ms": 0,
        "error_message": None,
    }

    try:
        # Load connector config from database
        query = (
            supabase.table("integration_connectors")
            .select("*")
            .eq("clinic_id", clinic_id)
            .eq("connector_type", connector_type)
        )
        result = await sb(_scope_by_branch(query, branch_id).single())

        if not result.data:
            logger.error(f"No connector config found for clinic {clinic_id}")
            summary["error_message"] = "No connector config found"
            return summary

        connector_row = result.data
        raw_config = connector_row.get("config")
        if isinstance(raw_config, str):
            try:
                config = json.loads(raw_config)
            except Exception:
                config = {}
        elif isinstance(raw_config, dict):
            config = dict(raw_config)
        else:
            config = {}

        # Check kill switch (bypassed for admin-triggered test runs)
        if not connector_row.get("is_enabled", False) and not ignore_enabled:
            logger.info(f"Connector disabled for clinic {clinic_id} — skipping")
            summary["run_status"] = "skipped"
            return summary

        # Validate required config
        if not config.get("username"):
            msg = "No username in connector config"
            logger.error(msg)
            summary["error_message"] = msg
            return summary

        # Seed/demo rows ship with template credentials. Authenticating with
        # them fails on every poll and pages the admin each time, so treat them
        # as "not configured yet" and stay quiet.
        placeholder = _placeholder_credential(config)
        if placeholder:
            msg = f"Placeholder credentials in connector config ({placeholder}) — configure real credentials to enable polling"
            logger.warning(f"Skipping clinic {clinic_id}: {msg}")
            summary["run_status"] = "skipped"
            summary["error_message"] = msg
            return summary

        # Decrypt password
        password_encrypted = config.get("password_encrypted", "")
        encryption_key = settings.connector_encryption_key

        if password_encrypted and encryption_key:
            try:
                config["password"] = decrypt_password(password_encrypted, encryption_key)
            except Exception as e:
                # The exception class alone ("ValueError") does not tell the
                # admin whether to fix the server key or re-enter the password,
                # and those remedies are mutually exclusive — one of them
                # destroys the stored credential. Say which.
                msg = f"Password decryption failed: {describe_decrypt_failure(encryption_key, e)}"
                logger.error(msg)
                summary["error_message"] = msg

                # A bad key is a static server misconfiguration: it cannot fix
                # itself, and every poll would page the admin again (12x/hour
                # at a 5 minute interval). Alert once per distinct error, then
                # stay quiet until the error changes or clears.
                if connector_row.get("last_error") != msg:
                    await send_admin_alert(
                        clinic_id, f"⚠️ MocDoc Connector: {msg}", branch_id=branch_id
                    )
                return summary
        elif config.get("password"):
            # Plain text password (for development only)
            pass
        else:
            msg = "No password in connector config"
            logger.error(msg)
            summary["error_message"] = msg
            return summary

        # Build the MedAssist API URL.
        # The fallback must use the port uvicorn actually binds — the Dockerfile
        # runs `--port ${PORT:-8000}`, so on Render/Railway PORT is set by the
        # platform and settings.app_port (8000) is wrong. Guessing 8000 made
        # every submit_to_medassist fail with "All connection attempts failed"
        # right after a perfectly good PDF download.
        medassist_url = os.environ.get("MEDASSIST_URL")
        if not medassist_url:
            port = os.environ.get("PORT") or settings.app_port
            medassist_url = f"http://127.0.0.1:{port}"
            logger.warning(
                f"MEDASSIST_URL not set — falling back to {medassist_url}. "
                f"This only works when the connector runs inside the web "
                f"process; the standalone worker MUST set MEDASSIST_URL."
            )

        # Session directory for cookies
        session_dir = os.path.join(PROJECT_ROOT, ".connector_sessions")
        os.makedirs(session_dir, exist_ok=True)

        candidate_id = connector_row.get("id")
        if candidate_id:
            locked, remaining = await acquire_connector_lock(candidate_id)
            if not locked:
                summary["run_status"] = "locked"
                summary["error_message"] = f"Connector is busy — retry in ~{remaining}m"
                return summary
            # Only now is this run the lock OWNER. connector_id drives the
            # finally-block release, so binding it before the acquisition is
            # what let a losing worker release the winner's lock.
            connector_id = candidate_id

        # Instantiate connector via registry
        connector_cls = CONNECTOR_REGISTRY.get(connector_type)
        if not connector_cls:
            summary["error_message"] = f"Unknown connector type: {connector_type}"
            return summary

        connector = connector_cls(
            clinic_id=clinic_id,
            config=config,
            medassist_url=medassist_url,
            integration_secret=settings.integration_secret,
            session_dir=session_dir,
            branch_id=branch_id,
        )

        authenticated = await connector.authenticate()
        if not authenticated:
            summary["error_message"] = "Authentication failed"
            await send_admin_alert(
                clinic_id,
                "⚠️ MocDoc Connector: Authentication failed. Check credentials or selectors.",
                branch_id=branch_id,
            )
            await connector.cleanup()
            return summary

        reports = await connector.fetch_new_reports()
        summary["reports_found"] = len(reports)

        if vam_id_filter:
            reports = [r for r in reports if r.vam_id == vam_id_filter]
            logger.info(f"Filtered to VAM ID {vam_id_filter}: {len(reports)} match")
        if limit > 0:
            reports = reports[:limit]
            logger.info(f"Limited to {limit} report(s)")

        summary["reports_new"] = len(reports)

        if dry_run:
            logger.info("=== DRY RUN MODE — No downloads or uploads ===")
            logger.info(f"=== DRY RUN RESULTS: {len(reports)} reports found ===")
            for r in reports:
                logger.info(f"  → {r}")
            summary["run_status"] = "dry_run"
            summary["sample"] = [
                {
                    "patient_name_masked": _mask_sample_name(r.patient_name),
                    "patient_phone_masked": _mask_phone(r.patient_phone),
                    "vam_id": r.vam_id,
                    "report_name": r.report_name,
                }
                for r in reports[:5]
            ]
            await connector.cleanup()
            return summary

        for meta in reports:
            if connector_id:
                await renew_connector_lock(connector_id)
            try:
                # Step 1: Patient matching safety gate.
                #
                # Skipped for a provider-routed report. The gate exists to catch
                # a receptionist typo in the PATIENT's mobile before a medical
                # PDF reaches a stranger; a TPA report goes to a number the
                # clinic itself configured in the connector, so there is no
                # scraped number to verify and no patient to disclose to.
                if meta.routed_recipient:
                    match_result = MatchResult(
                        status="matched",
                        is_safe_to_send=True,
                        match_source="provider_routing",
                        match_confidence=1.0,
                        matched_patient_id=None,
                        normalized_phone=meta.routed_recipient,
                        patient_name=meta.patient_name,
                        review_reason=None,
                    )
                else:
                    match_result = await patient_match_service.match(
                        clinic_id=clinic_id,
                        scraped_name=meta.patient_name,
                        scraped_phone=meta.patient_phone,
                        branch_id=branch_id,
                    )

                if match_result.normalized_phone:
                    meta.patient_phone = match_result.normalized_phone

                if not match_result.is_safe_to_send:
                    # A deliberate policy hold is not a delivery failure. Only
                    # a genuine problem — a missing or malformed phone, a name
                    # conflict on a shared number, a lookup error — is. Counting
                    # a configured hold as a failure marked every run "partial"
                    # and inflated the Delivery Failures tile with work that had
                    # not actually failed.
                    policy_hold = match_result.match_source == "moc_doc_only"
                    if not policy_hold:
                        summary["reports_failed"] += 1
                    summary["reports_needs_review"] += 1
                    logger.warning(
                        f"NEEDS_REVIEW for report {meta.external_report_id}: {match_result.review_reason}"
                    )
                    # Download the PDF even though we are not delivering it.
                    # Held reports are cleared by staff from the review queue
                    # with "send now", and that can only work if the file is in
                    # storage — a row pointing at "pending_review/..." makes the
                    # approve-and-send button silently do nothing.
                    held_bytes = None
                    try:
                        held_bytes = await connector.download_report(meta)
                    except Exception as e_dl:
                        logger.warning(
                            f"Could not download held report {meta.external_report_id} "
                            f"for review storage: {e_dl}"
                        )
                    from app.services.lab_reports import LabReportService

                    await LabReportService().store_for_review(
                        clinic_id=clinic_id,
                        patient_phone=meta.patient_phone,
                        patient_name=meta.patient_name,
                        report_name=meta.report_name,
                        report_type=meta.report_type,
                        review_reason=match_result.review_reason or "Patient match conflict / needs review",
                        file_bytes=held_bytes,
                        filename=f"{meta.external_report_id}.pdf",
                        external_report_id=meta.external_report_id,
                        source=connector_type,
                        match_confidence=match_result.match_confidence,
                        match_source=match_result.match_source,
                    )

                    if not policy_hold:
                        await record_report_failure(
                            clinic_id=clinic_id,
                            connector_type=connector_type,
                            external_report_id=meta.external_report_id,
                            error_message=match_result.review_reason or "Patient match conflict / needs review",
                            vam_id=meta.vam_id,
                            patient_name=meta.patient_name,
                            branch_id=branch_id,
                        )
                    continue

                summary["reports_matched"] += 1

                # Step 2: Download PDF
                pdf_bytes = await connector.download_report(meta)
                if not pdf_bytes:
                    # If this report was already processed in this run or session, don't count as failure or delivered (T5.2)
                    if meta.external_report_id in getattr(connector, "_processed_ids", set()):
                        summary["reports_skipped_already_processed"] += 1
                        continue
                    summary["reports_failed"] += 1
                    await record_report_failure(
                        clinic_id=clinic_id,
                        connector_type=connector_type,
                        external_report_id=meta.external_report_id,
                        error_message="PDF download failed or bill due pending",
                        vam_id=meta.vam_id,
                        patient_name=meta.patient_name,
                        branch_id=branch_id,
                    )
                    continue

                # Step 3: Submit to MedAssist API
                result = await connector.submit_to_medassist(
                    pdf_bytes,
                    meta,
                    match_confidence=match_result.match_confidence,
                    match_source=match_result.match_source,
                    matched_patient_id=match_result.matched_patient_id,
                )
                if result.get("success"):
                    summary["reports_delivered"] += 1
                    if getattr(match_result, "recipient_unverified", False):
                        summary["reports_delivered_unverified"] += 1
                if not result.get("already_processed"):
                    summary["reports_uploaded"] += 1
                    logger.info(f"Uploaded: {meta}")
                await record_report_success(
                    clinic_id=clinic_id,
                    connector_type=connector_type,
                    external_report_id=meta.external_report_id,
                    branch_id=branch_id,
                )
            except Exception as e:
                summary["reports_failed"] += 1
                logger.error(f"Failed: {meta.external_report_id}: {e}")
                await record_report_failure(
                    clinic_id=clinic_id,
                    connector_type=connector_type,
                    external_report_id=meta.external_report_id,
                    error_message=str(e),
                    vam_id=meta.vam_id,
                    patient_name=meta.patient_name,
                    branch_id=branch_id,
                )

        await connector.cleanup()

        # Tell the clinic, in the admin panel, that reports went out to numbers
        # it has no record of. One notification per run rather than one per
        # report: at a diagnostic centre EVERY delivery is unverified, so
        # per-report notifications would be pure noise and get ignored, which
        # is the opposite of a working control. The per-report evidence is the
        # lab_reports row, which stays stamped match_source="moc_doc_only".
        if summary.get("reports_delivered_unverified", 0) > 0:
            await notify_unverified_deliveries(
                clinic_id=clinic_id,
                count=summary["reports_delivered_unverified"],
                connector_type=connector_type,
            )

        summary["run_status"] = (
            "success" if summary["reports_failed"] == 0
            else "partial" if summary["reports_uploaded"] > 0
            else "failed"
        )

        # Alert admin if there were failures
        if summary.get("reports_failed", 0) > 0:
            await send_admin_alert(
                clinic_id,
                f"⚠️ MocDoc Connector Alert\n\n"
                f"Reports found: {summary['reports_found']}\n"
                f"Uploaded: {summary['reports_uploaded']}\n"
                f"Failed: {summary['reports_failed']}\n\n"
                f"Check admin dashboard for details.",
                branch_id=branch_id,
            )

    except Exception as e:
        # Some exceptions carry no message at all — a bare `raise SomeError`
        # inside a dependency is the common case. Formatting only
        # "{type}: {str(e)}" then stores the literally useless
        # "NotImplementedError: " on the connector row, which is exactly what
        # an operator sees on the dashboard: an error with no message, no
        # location, and nothing to act on.
        #
        # When there is no message, fall back to the deepest traceback frame so
        # last_error at least names the file, line and function that failed.
        detail = str(e).strip()
        if not detail:
            tb = e.__traceback__
            deepest = None
            while tb is not None:
                deepest = tb
                tb = tb.tb_next
            if deepest is not None:
                frame = deepest.tb_frame
                # Name the event loop too: a message-less NotImplementedError
                # out of _make_subprocess_transport means the loop cannot spawn
                # Playwright, and the loop class is the only thing that says why.
                try:
                    loop_name = type(asyncio.get_running_loop()).__name__
                except RuntimeError:
                    loop_name = "no-running-loop"
                detail = (
                    f"(no message) raised at "
                    f"{os.path.basename(frame.f_code.co_filename)}:"
                    f"{deepest.tb_lineno} in {frame.f_code.co_name}() "
                    f"[loop={loop_name}]"
                )
            else:
                detail = "(no message, no traceback)"

        summary["error_message"] = f"{type(e).__name__}: {detail[:200]}"
        logger.exception(f"Connector run failed for clinic {clinic_id}")

        try:
            await send_admin_alert(
                clinic_id,
                f"⚠️ MocDoc Connector Crashed\n\n"
                f"Error: {type(e).__name__}: {str(e)[:100]}\n\n"
                f"The connector will retry on the next poll.",
                branch_id=branch_id,
            )
        except Exception:
            pass

    finally:
        summary["duration_ms"] = int((time.time() - start_time) * 1000)

        # A "locked" run never touched the portal — another process was already
        # polling this connector. Recording it as a run polluted Run History with
        # phantom rows and, worse, stamped last_run_at + last_error below, which
        # pushed the next REAL poll a full interval out and flipped the connector
        # health badge to "running with errors".
        run_was_real = summary["run_status"] not in ("dry_run", "skipped", "locked")

        # Save audit log. Exclude in-memory metrics not in the DB schema
        # (e.g. "sample", "reports_skipped_already_processed").
        if summary["run_status"] != "locked":
            try:
                allowed_audit_cols = {
                    "run_status", "reports_found", "reports_new", "reports_matched",
                    "reports_needs_review", "reports_uploaded", "reports_delivered",
                    "reports_failed", "duration_ms", "error_message"
                }
                audit_row = {k: v for k, v in summary.items() if k in allowed_audit_cols}
                # unscoped: insert_scoped_by_payload
                await sb(supabase.table("connector_audit_log").insert({
                    "clinic_id": clinic_id,
                    "connector_type": connector_type,
                    "branch_id": branch_id,
                    **audit_row,
                }))
            except Exception as e:
                logger.error(f"Failed to save audit log: {e}")


        # A dry run is a "Test Connection" and a skipped run did nothing at
        # all — neither is a poll. Stamping them made the dashboard show a
        # disabled connector with a fresh "Last run", and pushed the next real
        # poll a whole interval into the future.
        if run_was_real:
            try:
                update_data = {
                    "last_run_at": datetime.now(timezone.utc).isoformat(),
                    "last_error": summary.get("error_message"),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                if summary["run_status"] == "success":
                    update_data["last_success_at"] = datetime.now(timezone.utc).isoformat()

                update_query = (
                    supabase.table("integration_connectors")
                    .update(update_data)
                    .eq("clinic_id", clinic_id)
                    .eq("connector_type", connector_type)
                )
                await sb(_scope_by_branch(update_query, branch_id))
            except Exception as e:
                logger.error(f"Failed to update connector timestamps: {e}")

        # Release advisory lock
        if connector_id:
            try:
                await release_connector_lock(connector_id)
            except Exception as e:
                logger.warning(f"Failed to release connector lock: {e}")

    logger.info(
        f"Connector run complete: "
        f"status={summary['run_status']} "
        f"found={summary['reports_found']} "
        f"uploaded={summary['reports_uploaded']} "
        f"failed={summary['reports_failed']} "
        f"duration={summary['duration_ms']}ms"
    )

    return summary


async def run_all_connectors() -> None:
    """Run all enabled connectors (called by scheduler), one per clinic OR
    per branch for multi-branch diagnostic centers.

    Respects each connector's configured poll_interval_minutes dynamically.
    Guarded by distributed lock to prevent duplicate sweeps across multiple workers.
    """
    async with distributed_job_lock("connector_polling_sweep", lease_seconds=120) as acquired:
        if not acquired:
            logger.debug("Connector sweep skipped: lock currently held by another worker instance")
            return

        logger.info("=== Polling all enabled connectors ===")

        # unscoped: platform_sweep
        result = await sb(supabase.table("integration_connectors") \
            .select("id, clinic_id, connector_type, branch_id, config, last_run_at") \
            .eq("is_enabled", True))

        connectors = result.data or []

        if not connectors:
            logger.info("No enabled connectors found")
            return

        now = datetime.now(timezone.utc)
        for conn in connectors:
            config = conn.get("config") or {}
            # KA-P0-A: an operator pressed Test / Run now in the admin panel.
            # That endpoint no longer spawns Chromium inside the web container
            # that must acknowledge Meta webhooks within 20s; it stamps the
            # request here and this worker owns the browser.
            #
            # The request is cleared BEFORE the run, not after: clearing after
            # would leave a crashed run re-requesting itself on every tick.
            requested = config.get("run_requested_at")
            dry_run = config.get("run_requested_mode") == "test"
            if requested:
                cleared = {
                    k: v
                    for k, v in config.items()
                    if k not in ("run_requested_at", "run_requested_mode")
                }
                try:
                    # unscoped: unique_row_key
                    await sb(
                        supabase.table("integration_connectors")
                        .update({"config": cleared})
                        .eq("id", conn["id"])
                    )
                except Exception as e:
                    # Never run on a request we could not clear.
                    logger.error(
                        f"Could not clear run request for {conn['id']}: {e} - skipping"
                    )
                    continue
                logger.info(
                    f"Operator-requested {'test' if dry_run else 'run'} for "
                    f"{conn['clinic_id']} branch={conn.get('branch_id')} - running now"
                )

            poll_interval = config.get("poll_interval_minutes", 10)
            last_run_at = conn.get("last_run_at")
            if last_run_at and not requested:
                try:
                    last_dt = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
                    if (now - last_dt) < timedelta(minutes=poll_interval - 0.5):
                        logger.debug(
                            f"Skipping {conn['clinic_id']} branch={conn.get('branch_id')} — "
                            f"poll interval {poll_interval}m not elapsed since {last_run_at}"
                        )
                        continue
                except Exception:
                    pass

            try:
                # Reinstall child watcher before EACH connector to prevent stale
                # watcher after the previous connector's Playwright cleanup.
                _ensure_subprocess_support()

                await run_connector(
                    clinic_id=conn["clinic_id"],
                    connector_type=conn["connector_type"],
                    branch_id=conn.get("branch_id"),
                    dry_run=dry_run,
                )
            except Exception as e:
                logger.error(
                    f"Connector run failed for {conn['clinic_id']} (branch={conn.get('branch_id')}): {e}"
                )

            # Small delay between clinics
            await asyncio.sleep(2)


async def cleanup_expired_storage() -> None:
    """Delete PDFs from Supabase Storage older than 90 days.

    Metadata + AI summary are preserved in the database for 7 years
    (NMC compliance). Only the PDF file in storage is deleted.
    Guarded by distributed lock (T1.2 / KRIYA-013) with bounded batches (limit=500).
    """
    async with distributed_job_lock("cleanup_expired_storage", lease_seconds=600) as acquired:
        if not acquired:
            logger.info("Storage cleanup skipped: lock currently held by another worker instance")
            return

        logger.info("=== Running storage cleanup (90-day retention) ===")
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        start_time = time.time()
        max_duration_seconds = 300  # 5 minute budget per execution

        total_deleted = 0
        try:
            while time.time() - start_time < max_duration_seconds:
                old_reports = (
                    # unscoped: platform_sweep
                    await sb(supabase.table("lab_reports")
                    .select("id, file_path")
                    .lt("uploaded_at", cutoff.isoformat())
                    .not_.is_("file_path", "null")
                    .limit(500))
                )

                if not old_reports.data:
                    break

                batch_deleted = 0
                for report in old_reports.data:
                    try:
                        file_path = report.get("file_path")
                        if file_path:
                            supabase.storage.from_("lab-reports").remove([file_path])
                            # unscoped: platform_sweep
                            await sb(supabase.table("lab_reports").update({
                                "file_path": None,
                            }).eq("id", report["id"]))
                            batch_deleted += 1
                    except Exception as e:
                        logger.warning(f"Failed to delete {report.get('file_path')}: {e}")

                total_deleted += batch_deleted
                if len(old_reports.data) < 500:
                    break

            logger.info(f"Storage cleanup complete: {total_deleted} PDFs deleted")

        except Exception as e:
            logger.error(f"Storage cleanup failed: {e}")

        # Also clean old audit logs (90 days)
        try:
            # unscoped: platform_sweep
            await sb(supabase.table("connector_audit_log") \
                .delete() \
                .lt("created_at", cutoff.isoformat()))
            logger.info("Cleaned up old audit log entries")
        except Exception as e:
            logger.warning(f"Audit log cleanup failed: {e}")

        # Clean stale session files and debug artifacts (24 hours)
        session_dir = os.path.join(PROJECT_ROOT, ".connector_sessions")
        if os.path.exists(session_dir):
            cutoff_ts = time.time() - (24 * 3600)
            for f in os.listdir(session_dir):
                fpath = os.path.join(session_dir, f)
                try:
                    if os.path.getmtime(fpath) < cutoff_ts:
                        os.remove(fpath)
                        logger.debug(f"Deleted stale session/debug file: {f}")
                except Exception:
                    pass


def _ensure_subprocess_support():
    """Install a fresh asyncio child watcher for subprocess support.

    On Python 3.11 in Docker, the SelectorEventLoop uses a child watcher
    to manage subprocess lifecycle (SIGCHLD handling).  When Playwright's
    cleanup() kills its Node.js driver subprocess, the child watcher can
    become stale — subsequent calls to _make_subprocess_transport() then
    fall through to BaseEventLoop's stub which raises NotImplementedError.

    This function ALWAYS installs a fresh ThreadedChildWatcher, ensuring
    the next Playwright launch works regardless of what happened before.
    It is designed to be called before EACH connector run, not just once.
    """
    if sys.platform == "win32":
        # Windows has no child watchers. NOTE: Windows is NOT automatically
        # on ProactorEventLoop — uvicorn picks SelectorEventLoop whenever
        # --reload or --workers is used, and that loop cannot spawn at all.
        # run_connector() handles that case by moving the run onto its own
        # subprocess-capable loop; nothing here can help.
        return

    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        policy = asyncio.get_event_loop_policy()

        # Always install a fresh watcher — the previous one may be stale
        # after a Playwright stop() killed its subprocess and the SIGCHLD
        # handler ran.
        try:
            old_watcher = policy.get_child_watcher()
            if old_watcher is not None:
                try:
                    old_watcher.close()
                except Exception:
                    pass
        except Exception:
            pass

        # set_child_watcher was UNGUARDED here. uvloop's policy raises a
        # message-less NotImplementedError from it, and this runs before every
        # connector poll — so a failure here aborted the run and produced the
        # locationless "NotImplementedError:" banner. Diagnostics must never be
        # the thing that breaks the run.
        try:
            policy.set_child_watcher(asyncio.ThreadedChildWatcher())
            logger.debug("Installed fresh ThreadedChildWatcher for subprocess support")
        except Exception as exc:
            logger.warning(
                "Could not install ThreadedChildWatcher (%r); run_connector owns "
                "its own loop, so this is not fatal.", exc
            )


def start_scheduled_mode():
    """Start APScheduler with polling and cleanup jobs."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    def _handle_sigterm(signum, frame):
        # Render sends SIGTERM on redeploy/stop. Route it through the same
        # shutdown path as Ctrl-C so in-flight locks get released below.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    # ── Critical: install a child watcher BEFORE creating the event loop ──
    # Without this, _make_subprocess_transport() raises NotImplementedError
    # when Playwright tries to spawn its Chromium/Node.js driver subprocess.
    _ensure_subprocess_support()

    scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Kolkata"))

    # Check connectors every 1 minute (each evaluates its own poll_interval_minutes)
    scheduler.add_job(
        run_all_connectors,
        IntervalTrigger(minutes=1),
        id="poll_connectors",
        replace_existing=True,
    )

    # Storage cleanup daily at 2 AM IST
    scheduler.add_job(
        cleanup_expired_storage,
        CronTrigger(hour=2, minute=0, timezone=ZoneInfo("Asia/Kolkata")),
        id="cleanup_storage",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Connector runner started in scheduled mode. "
        "Dynamic per-connector interval (evaluated every 1m). Storage cleanup daily at 2 AM IST."
    )

    # Run immediately on startup
    loop = asyncio.get_event_loop()
    loop.create_task(run_all_connectors())

    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down connector runner...")
        scheduler.shutdown()
        loop.run_until_complete(release_all_locks_held())


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="MedAssist AI — Integration Connector Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test login and parsing (no downloads)
  python -m connectors.runner --connector mocdoc --clinic-id <uuid> --once --dry-run

  # Run once for a specific clinic (full download + upload)
  python -m connectors.runner --connector mocdoc --clinic-id <uuid> --once

  # Production: scheduled polling every 10 minutes
  python -m connectors.runner --all

  # Encrypt a password for storage
  python -m connectors.runner --encrypt-password
        """,
    )

    parser.add_argument(
        "--connector",
        type=str,
        default="mocdoc",
        help="Connector type (default: mocdoc)",
    )
    parser.add_argument(
        "--clinic-id",
        type=str,
        help="Specific clinic ID to run for",
    )
    parser.add_argument(
        "--branch-id",
        type=str,
        default=None,
        help="Specific branch ID (multi-branch diagnostic centers only)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (don't start scheduler)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Authenticate and parse only — no downloads or uploads",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Start scheduled mode: poll all enabled connectors",
    )
    parser.add_argument(
        "--encrypt-password",
        action="store_true",
        help="Encrypt a password for connector config storage",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max number of reports to process (0 = all)",
    )
    parser.add_argument(
        "--vam-id",
        type=str,
        default="",
        help="Only process the report with this VAM ID (e.g. VAM-48471)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )

    args = parser.parse_args()
    setup_logging(args.log_level)

    # Encrypt password mode
    if args.encrypt_password:
        key = settings.connector_encryption_key
        if not key:
            print("ERROR: CONNECTOR_ENCRYPTION_KEY not set in .env")
            print("Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
            sys.exit(1)

        import getpass
        password = getpass.getpass("Enter MocDoc password to encrypt: ")
        encrypted = encrypt_password(password, key)
        print(f"\nEncrypted password (put this in connector config):\n{encrypted}")
        return

    # One-shot mode
    if args.once:
        if not args.clinic_id:
            print("ERROR: --clinic-id is required with --once")
            sys.exit(1)

        result = asyncio.run(
            run_connector(
                clinic_id=args.clinic_id,
                connector_type=args.connector,
                dry_run=args.dry_run,
                limit=args.limit,
                vam_id_filter=args.vam_id,
                branch_id=args.branch_id,
            )
        )
        print(f"\nResult: {json.dumps(result, indent=2)}")
        sys.exit(0 if result.get("run_status") in ("success", "dry_run", "skipped") else 1)

    # Scheduled mode
    if args.all:
        start_scheduled_mode()
        return

    # No mode specified
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
