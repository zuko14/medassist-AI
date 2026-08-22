"""Broadcast & Notification Service for Platform Owner to Clinic Admin messaging.

Provides:
- Broadcast creation and asynchronous fanout to target clinic admins.
- Batch insertion of in-app notifications into `admin_notifications`.
- Multi-tenant isolated notification retrieval and read status tracking.
- Extensible email and WhatsApp dispatch stubs for future gateway activation.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.database import supabase

logger = logging.getLogger(__name__)


# ─── Extensible Dispatch Stubs (Email / WhatsApp) ────────────────────────────


async def dispatch_email_stub(recipients: List[Dict[str, Any]], title: str, message: str) -> None:
    """Modular background dispatch stub for Email notifications.
    
    Ready for integration with SendGrid/AWS SES/SMTP when email gateway is provisioned.
    """
    try:
        logger.info(
            f"[Email Dispatch Stub] Queued email notification '{title}' to {len(recipients)} admin recipients."
        )
    except Exception as e:
        logger.warning(f"Error in email dispatch stub: {e}")


async def dispatch_whatsapp_stub(recipients: List[Dict[str, Any]], title: str, message: str) -> None:
    """Modular background dispatch stub for WhatsApp admin alerts.
    
    Ready for integration with Meta WABA utility templates when admin phone alerts are enabled.
    """
    try:
        logger.info(
            f"[WhatsApp Dispatch Stub] Queued WhatsApp alert '{title}' to {len(recipients)} admin recipients."
        )
    except Exception as e:
        logger.warning(f"Error in whatsapp dispatch stub: {e}")


# ─── Broadcast Service ───────────────────────────────────────────────────────


class BroadcastService:
    """Service for managing platform-wide broadcasts and clinic admin notifications."""

    @staticmethod
    async def create_broadcast(
        sender_id: str,
        title: str,
        message: str,
        target_type: str,
        target_clinic_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create a broadcast record and spawn background notification fanout."""
        target_type = target_type.upper()
        if target_type not in ("ALL", "SELECTIVE", "SINGLE"):
            raise ValueError(f"Invalid target_type '{target_type}'. Must be 'ALL', 'SELECTIVE', or 'SINGLE'.")

        clinic_ids = target_clinic_ids or []
        if target_type in ("SELECTIVE", "SINGLE") and not clinic_ids:
            raise ValueError("target_clinic_ids cannot be empty when target_type is SELECTIVE or SINGLE.")

        # 1. Insert master broadcast record
        broadcast_row = {
            "sender_id": sender_id,
            "title": title.strip(),
            "message": message.strip(),
            "target_type": target_type,
            "target_clinic_ids": clinic_ids,
            "recipient_count": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        res = supabase.table("broadcasts").insert(broadcast_row).execute()
        if not res.data:
            raise RuntimeError("Failed to create broadcast record in database.")

        broadcast = res.data[0]
        broadcast_id = broadcast["id"]

        # 2. Asynchronously dispatch in-app notifications in background with strong reference
        from app.utils.async_tasks import spawn_background_task

        spawn_background_task(
            BroadcastService._dispatch_notifications(
                broadcast_id=broadcast_id,
                target_type=target_type,
                target_clinic_ids=clinic_ids,
                title=title.strip(),
                message=message.strip(),
            ),
            name=f"broadcast_dispatch_{broadcast_id}",
        )

        return broadcast

    @staticmethod
    async def _dispatch_notifications(
        broadcast_id: str,
        target_type: str,
        target_clinic_ids: List[str],
        title: str,
        message: str,
    ) -> None:
        """Background worker: resolves target admins and performs chunked batch inserts."""
        try:
            # 1. Fetch active clinics (excluding soft-deleted ones)
            clinics_query = (
                supabase.table("clinics")
                .select("id, name, is_active, status")
                .eq("is_active", True)
            )
            clinics_res = clinics_query.execute()
            all_active_clinics = [
                c for c in (clinics_res.data or []) if c.get("status") != "DELETED"
            ]
            active_clinic_ids = {c["id"] for c in all_active_clinics}

            # Filter targeted clinics based on scope
            if target_type in ("SELECTIVE", "SINGLE"):
                allowed_clinic_ids = [cid for cid in target_clinic_ids if cid in active_clinic_ids]
            else:
                allowed_clinic_ids = list(active_clinic_ids)

            if not allowed_clinic_ids:
                logger.warning(f"Broadcast {broadcast_id}: No active clinics match target scope.")
                return

            # 2. Fetch active clinic admins for the targeted clinics
            admins_res = (
                supabase.table("clinic_admins")
                .select("id, clinic_id, username, is_active")
                .in_("clinic_id", allowed_clinic_ids)
                .eq("is_active", True)
                .execute()
            )
            target_admins = admins_res.data or []

            # 3. Construct notification records
            notifications_to_insert = []
            now_iso = datetime.now(timezone.utc).isoformat()

            for admin in target_admins:
                notifications_to_insert.append(
                    {
                        "broadcast_id": broadcast_id,
                        "clinic_id": admin["clinic_id"],
                        "admin_id": admin["id"],
                        "title": title,
                        "message": message,
                        "is_read": False,
                        "created_at": now_iso,
                    }
                )

            # If a targeted clinic has no explicit clinic_admin account yet, create a clinic-level notification
            admin_clinic_ids = {a["clinic_id"] for a in target_admins}
            for cid in allowed_clinic_ids:
                if cid not in admin_clinic_ids:
                    notifications_to_insert.append(
                        {
                            "broadcast_id": broadcast_id,
                            "clinic_id": cid,
                            "admin_id": None,
                            "title": title,
                            "message": message,
                            "is_read": False,
                            "created_at": now_iso,
                        }
                    )

            # 4. Batch insert in chunks of 100 to prevent payload limits
            chunk_size = 100
            total_inserted = 0
            for i in range(0, len(notifications_to_insert), chunk_size):
                chunk = notifications_to_insert[i : i + chunk_size]
                ins_res = supabase.table("admin_notifications").insert(chunk).execute()
                if ins_res.data:
                    total_inserted += len(ins_res.data)

            # 5. Update broadcast recipient count
            supabase.table("broadcasts").update(
                {"recipient_count": total_inserted}
            ).eq("id", broadcast_id).execute()

            logger.info(
                f"Broadcast {broadcast_id} dispatched: {total_inserted} notifications created across {len(allowed_clinic_ids)} clinics."
            )

            # 6. Trigger modular background stubs
            await dispatch_email_stub(target_admins, title, message)
            await dispatch_whatsapp_stub(target_admins, title, message)

        except Exception as e:
            logger.error(f"Error during broadcast {broadcast_id} notification dispatch: {e}")

    @staticmethod
    async def get_broadcasts(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Retrieve recent broadcasts with live delivery & read metrics."""
        res = (
            supabase.table("broadcasts")
            .select("*")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        broadcasts = res.data or []

        # Enrich each broadcast with delivery and read counts
        enriched = []
        for b in broadcasts:
            bid = b["id"]
            notifs_res = (
                supabase.table("admin_notifications")
                .select("id, is_read")
                .eq("broadcast_id", bid)
                .execute()
            )
            notifs = notifs_res.data or []
            delivered = len(notifs)
            read_count = sum(1 for n in notifs if n.get("is_read"))

            b_copy = dict(b)
            b_copy["delivered_count"] = delivered
            b_copy["read_count"] = read_count
            b_copy["pending_count"] = max(0, b.get("recipient_count", delivered) - delivered)
            enriched.append(b_copy)

        return enriched

    @staticmethod
    async def get_broadcast_by_id(broadcast_id: str) -> Optional[Dict[str, Any]]:
        """Get single broadcast details with full delivery summary."""
        res = supabase.table("broadcasts").select("*").eq("id", broadcast_id).execute()
        if not res.data:
            return None

        broadcast = res.data[0]
        notifs_res = (
            supabase.table("admin_notifications")
            .select("id, clinic_id, admin_id, is_read, read_at, created_at")
            .eq("broadcast_id", broadcast_id)
            .execute()
        )
        notifs = notifs_res.data or []
        delivered = len(notifs)
        read_count = sum(1 for n in notifs if n.get("is_read"))

        summary = {
            "broadcast": broadcast,
            "total_recipients": broadcast.get("recipient_count", delivered),
            "delivered_count": delivered,
            "read_count": read_count,
            "unread_count": delivered - read_count,
            "notifications": notifs,
        }
        return summary

    @staticmethod
    async def get_admin_notifications(
        clinic_id: str,
        admin_id: Optional[str] = None,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Fetch in-app notifications for a clinic admin enforcing strict tenant isolation."""
        query = (
            supabase.table("admin_notifications")
            .select("*")
            .eq("clinic_id", clinic_id)
        )

        if admin_id and admin_id not in ("super_admin_env", "platform_owner_env"):
            # Include notifications targeted directly to this admin or clinic-wide
            pass  # clinic_id filter provides tenant isolation

        if unread_only:
            query = query.eq("is_read", False)

        res = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        return res.data or []

    @staticmethod
    async def get_unread_count(clinic_id: str, admin_id: Optional[str] = None) -> int:
        """Get live unread notification count for the header badge."""
        res = (
            supabase.table("admin_notifications")
            .select("id", count="exact")
            .eq("clinic_id", clinic_id)
            .eq("is_read", False)
            .execute()
        )
        return res.count if res.count is not None else len(res.data or [])

    @staticmethod
    async def mark_notification_read(
        notification_id: str,
        clinic_id: str,
        admin_id: Optional[str] = None,
    ) -> bool:
        """Mark a single notification as read, strictly tenant-scoped."""
        now_iso = datetime.now(timezone.utc).isoformat()
        res = (
            supabase.table("admin_notifications")
            .update({"is_read": True, "read_at": now_iso})
            .eq("id", notification_id)
            .eq("clinic_id", clinic_id)
            .execute()
        )
        return bool(res.data)

    @staticmethod
    async def mark_all_notifications_read(
        clinic_id: str,
        admin_id: Optional[str] = None,
    ) -> int:
        """Mark all unread notifications for this clinic as read."""
        now_iso = datetime.now(timezone.utc).isoformat()
        res = (
            supabase.table("admin_notifications")
            .update({"is_read": True, "read_at": now_iso})
            .eq("clinic_id", clinic_id)
            .eq("is_read", False)
            .execute()
        )
        return len(res.data or [])


broadcast_service = BroadcastService()
