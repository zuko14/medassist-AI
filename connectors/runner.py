"""Connector Runner — Entry point for the MocDoc worker process.

This is a SEPARATE process from FastAPI. It uses APScheduler to poll
MocDoc (or any other enabled connector) at regular intervals.

Usage:
    # One-shot test run
    python -m connectors.runner --connector mocdoc --clinic-id <uuid> --once

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
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path so we can import app modules
PROJECT_ROOT = str(Path(__file__).parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from app.config import settings
from app.database import supabase

logger = logging.getLogger("connectors")


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


def decrypt_password(encrypted: str, key: str) -> str:
    """Decrypt a Fernet-encrypted password."""
    from cryptography.fernet import Fernet
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.decrypt(encrypted.encode() if isinstance(encrypted, str) else encrypted).decode()


def encrypt_password(plaintext: str, key: str) -> str:
    """Encrypt a password with Fernet for storage."""
    from cryptography.fernet import Fernet
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.encrypt(plaintext.encode()).decode()


async def send_admin_alert(clinic_id: str, message: str) -> None:
    """Send a WhatsApp alert to the admin phone number."""
    try:
        from app.services.whatsapp import whatsapp_service
        from app.services.tenant import get_clinic_by_id

        clinic = await get_clinic_by_id(clinic_id)

        # Get admin alert phone from connector config
        connector = supabase.table("integration_connectors") \
            .select("config") \
            .eq("clinic_id", clinic_id) \
            .eq("connector_type", "mocdoc") \
            .single() \
            .execute()

        admin_phone = connector.data.get("config", {}).get("admin_alert_phone")
        if admin_phone:
            await whatsapp_service.send_text(clinic, admin_phone, message)
            logger.info(f"Admin alert sent to ***{admin_phone[-4:]}")
        else:
            logger.warning("No admin_alert_phone configured — alert not sent")
    except Exception as e:
        logger.error(f"Failed to send admin alert: {e}")


async def run_connector(
    clinic_id: str,
    connector_type: str = "mocdoc",
    dry_run: bool = False,
    limit: int = 0,
    vam_id_filter: str = "",
) -> dict:
    """Execute a single connector run for a specific clinic.

    Args:
        clinic_id: UUID of the clinic
        connector_type: Type of connector (currently only "mocdoc")
        dry_run: If True, authenticate and parse only — no downloads
        limit: Max reports to process (0 = no limit)
        vam_id_filter: Only process report matching this VAM ID

    Returns:
        Summary dict with run results
    """
    start_time = time.time()
    summary = {
        "run_status": "failed",
        "reports_found": 0,
        "reports_new": 0,
        "reports_uploaded": 0,
        "reports_failed": 0,
        "duration_ms": 0,
        "error_message": None,
    }

    try:
        # Load connector config from database
        result = supabase.table("integration_connectors") \
            .select("*") \
            .eq("clinic_id", clinic_id) \
            .eq("connector_type", connector_type) \
            .single() \
            .execute()

        if not result.data:
            logger.error(f"No connector config found for clinic {clinic_id}")
            summary["error_message"] = "No connector config found"
            return summary

        connector_row = result.data
        config = connector_row.get("config", {})

        # Check kill switch
        if not connector_row.get("is_enabled", False):
            logger.info(f"Connector disabled for clinic {clinic_id} — skipping")
            summary["run_status"] = "skipped"
            return summary

        # Validate required config
        if not config.get("username"):
            msg = "No username in connector config"
            logger.error(msg)
            summary["error_message"] = msg
            return summary

        # Decrypt password
        password_encrypted = config.get("password_encrypted", "")
        encryption_key = settings.connector_encryption_key

        if password_encrypted and encryption_key:
            try:
                config["password"] = decrypt_password(password_encrypted, encryption_key)
            except Exception as e:
                msg = f"Password decryption failed: {type(e).__name__}"
                logger.error(msg)
                summary["error_message"] = msg
                await send_admin_alert(clinic_id, f"⚠️ MocDoc Connector: {msg}")
                return summary
        elif config.get("password"):
            # Plain text password (for development only)
            pass
        else:
            msg = "No password in connector config"
            logger.error(msg)
            summary["error_message"] = msg
            return summary

        # Build the MedAssist API URL
        medassist_url = os.environ.get(
            "MEDASSIST_URL",
            f"http://localhost:{settings.app_port}",
        )

        # Session directory for cookies
        session_dir = os.path.join(PROJECT_ROOT, ".connector_sessions")
        os.makedirs(session_dir, exist_ok=True)

        # Instantiate connector
        if connector_type == "mocdoc":
            from connectors.mocdoc.worker import MocDocConnector
            connector = MocDocConnector(
                clinic_id=clinic_id,
                config=config,
                medassist_url=medassist_url,
                integration_secret=settings.integration_secret,
                session_dir=session_dir,
            )
        else:
            summary["error_message"] = f"Unknown connector type: {connector_type}"
            return summary

        if dry_run:
            # Dry run: authenticate and parse only
            logger.info("=== DRY RUN MODE — No downloads or uploads ===")
            authenticated = await connector.authenticate()
            if not authenticated:
                summary["error_message"] = "Authentication failed"
                await send_admin_alert(
                    clinic_id,
                    "⚠️ MocDoc Connector: Authentication failed. "
                    "Check credentials or selectors.",
                )
                return summary

            reports = await connector.fetch_new_reports()
            summary["reports_found"] = len(reports)
            summary["run_status"] = "dry_run"

            # Apply filters for display
            if vam_id_filter:
                reports = [r for r in reports if r.vam_id == vam_id_filter]
                logger.info(f"Filtered to VAM ID {vam_id_filter}: {len(reports)} match")
            if limit > 0:
                reports = reports[:limit]

            logger.info(f"=== DRY RUN RESULTS: {len(reports)} reports found ===")
            for r in reports:
                logger.info(f"  → {r}")

            await connector.cleanup()
            return summary

        # Full run — with optional limit/vam_id filtering
        if limit > 0 or vam_id_filter:
            # Filtered run: we control the loop here
            authenticated = await connector.authenticate()
            if not authenticated:
                summary["error_message"] = "Authentication failed"
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

            for meta in reports:
                try:
                    pdf_bytes = await connector.download_report(meta)
                    if not pdf_bytes:
                        summary["reports_failed"] += 1
                        continue
                    result = await connector.submit_to_medassist(pdf_bytes, meta)
                    if not result.get("already_processed"):
                        summary["reports_uploaded"] += 1
                        logger.info(f"Uploaded: {meta}")
                except Exception as e:
                    summary["reports_failed"] += 1
                    logger.error(f"Failed: {meta.external_report_id}: {e}")

            await connector.cleanup()

            summary["run_status"] = (
                "success" if summary["reports_failed"] == 0
                else "partial" if summary["reports_uploaded"] > 0
                else "failed"
            )
        else:
            # Unfiltered full run
            run_summary = await connector.run()
            summary.update({
                "reports_found": run_summary["reports_found"],
                "reports_new": run_summary["reports_new"],
                "reports_uploaded": run_summary["reports_uploaded"],
                "reports_failed": run_summary["reports_failed"],
                "run_status": (
                    "success" if run_summary["reports_failed"] == 0
                    else "partial" if run_summary["reports_uploaded"] > 0
                    else "failed"
                ),
                "error_message": (
                    "; ".join(run_summary["errors"][:3])
                    if run_summary["errors"]
                    else None
                ),
            })

        # Alert admin if there were failures
        if summary.get("reports_failed", 0) > 0:
            await send_admin_alert(
                clinic_id,
                f"⚠️ MocDoc Connector Alert\n\n"
                f"Reports found: {summary['reports_found']}\n"
                f"Uploaded: {summary['reports_uploaded']}\n"
                f"Failed: {summary['reports_failed']}\n\n"
                f"Check admin dashboard for details.",
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
            )
        except Exception:
            pass

    finally:
        summary["duration_ms"] = int((time.time() - start_time) * 1000)

        # Save audit log
        try:
            supabase.table("connector_audit_log").insert({
                "clinic_id": clinic_id,
                "connector_type": connector_type,
                **summary,
            }).execute()
        except Exception as e:
            logger.error(f"Failed to save audit log: {e}")

        # Update connector's last_run timestamps
        try:
            update_data = {
                "last_run_at": datetime.now(timezone.utc).isoformat(),
                "last_error": summary.get("error_message"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if summary["run_status"] == "success":
                update_data["last_success_at"] = datetime.now(timezone.utc).isoformat()

            supabase.table("integration_connectors") \
                .update(update_data) \
                .eq("clinic_id", clinic_id) \
                .eq("connector_type", connector_type) \
                .execute()
        except Exception as e:
            logger.error(f"Failed to update connector timestamps: {e}")

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
    """Run all enabled connectors (called by scheduler)."""
    logger.info("=== Polling all enabled connectors ===")

    result = supabase.table("integration_connectors") \
        .select("clinic_id, connector_type") \
        .eq("is_enabled", True) \
        .execute()

    connectors = result.data or []

    if not connectors:
        logger.info("No enabled connectors found")
        return

    for conn in connectors:
        try:
            await run_connector(
                clinic_id=conn["clinic_id"],
                connector_type=conn["connector_type"],
            )
        except Exception as e:
            logger.error(
                f"Connector run failed for {conn['clinic_id']}: {e}"
            )

        # Small delay between clinics
        await asyncio.sleep(2)


async def cleanup_expired_storage() -> None:
    """Delete PDFs from Supabase Storage older than 90 days.

    Metadata + AI summary are preserved in the database for 7 years
    (NMC compliance). Only the PDF file in storage is deleted.
    """
    logger.info("=== Running storage cleanup (90-day retention) ===")
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)

    try:
        old_reports = supabase.table("lab_reports") \
            .select("id, file_path") \
            .lt("uploaded_at", cutoff.isoformat()) \
            .not_.is_("file_path", "null") \
            .execute()

        if not old_reports.data:
            logger.info("No expired PDFs to clean up")
            return

        deleted = 0
        for report in old_reports.data:
            try:
                file_path = report.get("file_path")
                if file_path:
                    supabase.storage.from_("lab-reports").remove([file_path])
                    supabase.table("lab_reports").update({
                        "file_path": None,
                    }).eq("id", report["id"]).execute()
                    deleted += 1
            except Exception as e:
                logger.warning(f"Failed to delete {report.get('file_path')}: {e}")

        logger.info(f"Storage cleanup complete: {deleted} PDFs deleted")

    except Exception as e:
        logger.error(f"Storage cleanup failed: {e}")

    # Also clean old audit logs (90 days)
    try:
        supabase.table("connector_audit_log") \
            .delete() \
            .lt("created_at", cutoff.isoformat()) \
            .execute()
        logger.info("Cleaned up old audit log entries")
    except Exception as e:
        logger.warning(f"Audit log cleanup failed: {e}")

    # Clean stale session files (7 days)
    session_dir = os.path.join(PROJECT_ROOT, ".connector_sessions")
    if os.path.exists(session_dir):
        cutoff_ts = time.time() - (7 * 24 * 3600)
        for f in os.listdir(session_dir):
            fpath = os.path.join(session_dir, f)
            try:
                if os.path.getmtime(fpath) < cutoff_ts:
                    os.remove(fpath)
                    logger.debug(f"Deleted stale session file: {f}")
            except Exception:
                pass


def start_scheduled_mode():
    """Start APScheduler with polling and cleanup jobs."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = AsyncIOScheduler()

    # Poll every 10 minutes
    scheduler.add_job(
        run_all_connectors,
        IntervalTrigger(minutes=10),
        id="poll_connectors",
        replace_existing=True,
    )

    # Storage cleanup daily at 2 AM
    scheduler.add_job(
        cleanup_expired_storage,
        CronTrigger(hour=2, minute=0),
        id="cleanup_storage",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Connector runner started in scheduled mode. "
        "Polling every 10 minutes. Storage cleanup daily at 2 AM."
    )

    # Run immediately on startup
    loop = asyncio.get_event_loop()
    loop.create_task(run_all_connectors())

    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down connector runner...")
        scheduler.shutdown()


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
