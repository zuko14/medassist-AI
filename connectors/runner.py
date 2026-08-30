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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

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
from app.services.patient_match import patient_match_service
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


async def acquire_connector_lock(connector_id: str, worker_id: str = "worker-1") -> tuple[bool, int]:
    """Acquire distributed advisory lock on connector record (5 min lease).

    KA-10: Uses CAS-style atomic UPDATE to prevent TOCTOU race.
    Fails CLOSED on any exception (returns False, not True).

    Returns (acquired, remaining_minutes). remaining_minutes is 0 when
    acquired; otherwise it's the lock's remaining TTL rounded up to the
    nearest minute (minimum 1), for surfacing "retry in ~Nm" to the admin UI.
    """
    try:
        now_str = datetime.now(timezone.utc).isoformat()
        lease_cutoff = (datetime.now(timezone.utc) - LOCK_LEASE).isoformat()

        # CAS: atomically update only if unlocked or lease expired
        # This eliminates the TOCTOU race where two workers could both
        # read "unlocked" and then both write their lock.
        update_result = (
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
        await sb(supabase.table("integration_connectors").update({
            "locked_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", connector_id))
    except Exception as e:
        logger.warning(f"Could not renew lock for connector {connector_id}: {e}")


async def release_connector_lock(connector_id: str) -> None:
    """Release distributed advisory lock on connector record."""
    try:
        await sb(supabase.table("integration_connectors").update({
            "locked_at": None,
            "locked_by": None,
        }).eq("id", connector_id))
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


async def run_connector(
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

        connector_id = connector_row.get("id")
        if connector_id:
            locked, remaining = await acquire_connector_lock(connector_id)
            if not locked:
                summary["run_status"] = "locked"
                summary["error_message"] = f"Connector is busy — retry in ~{remaining}m"
                return summary

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
                # Step 1: Patient matching safety gate
                match_result = await patient_match_service.match(
                    clinic_id=clinic_id,
                    scraped_name=meta.patient_name,
                    scraped_phone=meta.patient_phone,
                    branch_id=branch_id,
                )

                if match_result.normalized_phone:
                    meta.patient_phone = match_result.normalized_phone

                if not match_result.is_safe_to_send:
                    summary["reports_failed"] += 1
                    summary["reports_needs_review"] += 1
                    logger.warning(
                        f"NEEDS_REVIEW for report {meta.external_report_id}: {match_result.review_reason}"
                    )
                    try:
                        await sb(supabase.table("lab_reports").insert({
                            "clinic_id": clinic_id,
                            "patient_phone": meta.patient_phone or "MISSING",
                            "patient_name": meta.patient_name or "Unknown",
                            "report_name": meta.report_name or "Lab Report",
                            "report_type": meta.report_type or "Laboratory",
                            "file_path": f"pending_review/{meta.external_report_id}",
                            "status": "needs_review",
                            "external_report_id": meta.external_report_id,
                            "source": connector_type,
                            "match_confidence": match_result.match_confidence,
                            "match_source": match_result.match_source,
                            "error_message": match_result.review_reason,
                        }))
                    except Exception as e_nr:
                        logger.error(f"Failed to record needs_review row: {e_nr}")

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
        summary["error_message"] = f"{type(e).__name__}: {str(e)[:200]}"
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
    """
    logger.info("=== Polling all enabled connectors ===")

    result = await sb(supabase.table("integration_connectors") \
        .select("clinic_id, connector_type, branch_id, config, last_run_at") \
        .eq("is_enabled", True))

    connectors = result.data or []

    if not connectors:
        logger.info("No enabled connectors found")
        return

    now = datetime.now(timezone.utc)
    for conn in connectors:
        config = conn.get("config") or {}
        poll_interval = config.get("poll_interval_minutes", 10)
        last_run_at = conn.get("last_run_at")
        if last_run_at:
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
            await run_connector(
                clinic_id=conn["clinic_id"],
                connector_type=conn["connector_type"],
                branch_id=conn.get("branch_id"),
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
