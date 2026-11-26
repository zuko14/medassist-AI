"""Specialty OS WhatsApp hook (migrations 152, 154): claims only `spest:<estimate>:<option|no>` (estimate accept /
decline — no money moves) and `spivf:ack:<cycle>` (trigger time noted). Reached only for a clinic with sp_access (a
check on the clinic row already in memory), so every other clinic's conversation is unchanged. Any error → False and
the message flows on as before. Zero-LLM.
"""

import logging
import re

from app.services import sp_commerce, sp_ivf_wc

logger = logging.getLogger(__name__)
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
TEXT = {"accepted": "Thank you — we have noted your choice. Our team will call you to plan the next step. Nothing has "
                    "been charged.",
        "declined": "Thank you for letting us know. You can ask us about this estimate any time.",
        "ack": "Thank you — the time is noted. Please call the clinic if anything is unclear.",
        "stale": "This request has expired or was already answered. Please call the clinic."}


def claims(tap_id: str) -> bool:
    return tap_id.startswith((sp_commerce.EST_PREFIX, sp_ivf_wc.IVF_PREFIX))


async def handle_tap(manager, clinic: dict, phone: str, tap_id: str, message_id) -> bool:
    try:
        m = re.fullmatch(rf"spest:({_UUID}):([a-z0-9_]{{1,20}})", tap_id)
        if m:
            option = None if m.group(2) == "no" else m.group(2)
            out = await sp_commerce.respond(clinic["id"], m.group(1), option, message_id, phone=phone)
            key = "stale" if not out else ("accepted" if option else "declined")
        else:
            m = re.fullmatch(rf"spivf:ack:({_UUID})", tap_id)
            if not m:
                return tap_id.startswith(("spest:", "spivf:"))          # malformed SP id: absorbed, no effect
            out = await sp_ivf_wc.acknowledge_trigger(clinic["id"], m.group(1), "whatsapp_button", phone=phone)
            key = "ack" if out else "stale"
        await manager.whatsapp.send_text(clinic, phone, TEXT[key], _window_open=True)
        return True
    except Exception as e:                                               # noqa: BLE001
        logger.error(f"SP_HOOK_FAILED clinic={clinic.get('id')}: {type(e).__name__}")
        return False
