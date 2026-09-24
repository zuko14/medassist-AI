"""Home-sample-collection booking on the CallMedex WhatsApp number.

Isolation contract (live clinics must be unaffected):
  * Runs ONLY for inbound messages whose Meta phone_number_id is the CallMedex
    number (is_callmedex_number). A phone_number_id that also belongs to any
    clinic is never treated as CallMedex — the clinic always wins.
  * Never touches clinic-scoped tables or app.services.whatsapp; state lives in
    callmedex_booking_sessions and replies go out on the number the patient
    messaged, with the CallMedex token.

Flow: menu -> date -> time window -> address (saved or typed) -> confirm ->
POST /whatsapp-bookings on CallMedex -> confirmation in the patient's language.
"""

import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.database import sb, supabase
from app.integrations.callmedex.api import client as callmedex_client
from app.integrations.callmedex.config.settings import callmedex_settings
from app.integrations.callmedex.whatsapp.service import WhatsAppDeliveryService
from app.services.ai_engine import EMERGENCY_KEYWORDS
from app.utils.validators import mask_phone, normalize_phone

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_TIMEOUT = timedelta(minutes=30)  # CLAUDE.md: mid-booking sessions expire after 30 min
PURGE_AFTER = timedelta(days=1)
SAME_DAY_CUTOFF_HOUR = 15  # no same-day bookings offered after 3 PM IST
LEAD_TIME = timedelta(hours=1)  # a window must start at least this far ahead
WINDOWS = {7: 9, 9: 11, 11: 13, 16: 18}  # start hour -> end hour, IST
PLACEHOLDER_IDS = {"", "100000000000000"}
PINCODE_RE = re.compile(r"\b[1-9]\d{5}\b")
CANCEL_WORDS = {"cancel", "stop", "exit", "quit"}
RESTART_WORDS = {"hi", "hello", "hey", "menu", "start", "book", "restart"}

EMERGENCY_TEXT = (
    "🚨 If this is a medical emergency, call 108 (ambulance) right now or go to the "
    "nearest hospital. This WhatsApp number cannot respond to emergencies."
)
CONFIRMATION = {
    "en": "✅ Your home sample collection is confirmed for {date} {window}! Our certified "
          "phlebotomist will arrive with barcoded collection tubes. Booking ID: {booking_id}",
    "hi": "✅ आपका होम सैंपल कलेक्शन {date} {window} के लिए कन्फर्म हो गया है! हमारे प्रमाणित "
          "फ्लेबोटोमिस्ट बारकोड वाली कलेक्शन ट्यूब के साथ आएंगे। बुकिंग ID: {booking_id}",
    "te": "✅ మీ హోమ్ శాంపిల్ కలెక్షన్ {date} {window} కి నిర్ధారించబడింది! మా సర్టిఫైడ్ "
          "ఫ్లెబోటమిస్ట్ బార్‌కోడ్ ఉన్న కలెక్షన్ ట్యూబ్‌లతో వస్తారు. బుకింగ్ ID: {booking_id}",
}


# ── Which inbound numbers are CallMedex's ─────────────────────────────────────

_ids_cache: dict = {"ids": frozenset(), "at": float("-inf")}
_CACHE_TTL_SECONDS = 60.0


def cached_callmedex_number(phone_number_id: Optional[str]) -> bool:
    """Set lookup only, no I/O — safe on the webhook's 200-fast path. False
    until the first refresh, which just means the old path runs once."""
    return bool(phone_number_id) and phone_number_id in _ids_cache["ids"]


async def is_callmedex_number(phone_number_id: Optional[str]) -> bool:
    if not phone_number_id:
        return False
    if time.monotonic() - _ids_cache["at"] > _CACHE_TTL_SECONDS:
        await _refresh_ids()
    return phone_number_id in _ids_cache["ids"]


async def _refresh_ids() -> None:
    ids = {callmedex_settings.whatsapp_phone_number_id}
    try:
        # Platform-level CallMedex number (single 'default' row).
        # unscoped: unique_row_key
        row = await sb(supabase.table("callmedex_whatsapp_settings").select("phone_number_id").eq("id", "default"))
        if row.data and row.data[0].get("phone_number_id"):
            ids.add(row.data[0]["phone_number_id"])
        # Every clinic's own Meta number, so none is ever routed here.
        # unscoped: platform_sweep
        clinics = await sb(supabase.table("clinics").select("phone_number_id, config"))
        clinic_ids = set()
        for c in clinics.data or []:
            cfg = c.get("config") or {}
            clinic_ids.update({c.get("phone_number_id"), cfg.get("meta_phone_number_id"), cfg.get("phone_number_id")})
    except Exception as e:
        # Can't prove the id isn't a clinic's -> keep the last known-good set.
        logger.warning(f"CallMedex number refresh failed (keeping previous set): {e}")
        _ids_cache["at"] = time.monotonic()
        return
    _ids_cache["ids"] = frozenset(i for i in ids - clinic_ids if i and i not in PLACEHOLDER_IDS)
    _ids_cache["at"] = time.monotonic()


# ── WhatsApp replies on the CallMedex number ─────────────────────────────────

def _text(body: str) -> dict:
    return {"type": "text", "text": {"preview_url": False, "body": body}}


def _buttons(body: str, buttons: list[tuple[str, str]]) -> dict:
    return {"type": "interactive", "interactive": {
        "type": "button",
        "body": {"text": body},
        "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in buttons[:3]]},
    }}


def _list(body: str, button: str, rows: list[tuple[str, str]]) -> dict:
    return {"type": "interactive", "interactive": {
        "type": "list",
        "body": {"text": body},
        "action": {"button": button[:20], "sections": [
            {"title": "Time slots", "rows": [{"id": i, "title": t[:24]} for i, t in rows[:10]]}
        ]},
    }}


async def _send(phone: str, payload: dict, phone_number_id: str) -> bool:
    """Reply from the number the patient wrote to (always inside the 24h window)."""
    token, _ = await WhatsAppDeliveryService()._get_effective_whatsapp_credentials()
    if not token or token in ("dev_whatsapp_token", "change_in_prod"):
        logger.error("CALLMEDEX_WHATSAPP_NOT_CONFIGURED: no token — cannot reply to CallMedex patient")
        return False
    body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": phone, **payload}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"https://graph.facebook.com/v22.0/{phone_number_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"CallMedex reply to {mask_phone(phone)} failed: {e}")
        return False


# ── Session state (callmedex_booking_sessions) ──────────────────────────────
# ponytail: last-write-wins per phone; fine for one patient tapping one button
# at a time. Add a version column + conditional update if double-taps matter.

async def _load(phone: str) -> Optional[dict]:
    res = await sb(supabase.table("callmedex_booking_sessions").select("state, data, updated_at").eq("phone", phone))
    row = res.data[0] if res.data else None
    if not row:
        return None
    updated = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
    return None if datetime.now(timezone.utc) - updated > SESSION_TIMEOUT else row


async def _save(phone: str, state: str, data: dict) -> None:
    await sb(supabase.table("callmedex_booking_sessions").upsert({
        "phone": phone, "state": state, "data": data,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))


async def _clear(phone: str) -> None:
    await sb(supabase.table("callmedex_booking_sessions").delete().eq("phone", phone))


async def _purge_stale() -> None:
    # ponytail: opportunistic purge on new sessions; move to the scheduler's
    # retention job if this number sees real volume.
    try:
        cutoff = (datetime.now(timezone.utc) - PURGE_AFTER).isoformat()
        await sb(supabase.table("callmedex_booking_sessions").delete().lt("updated_at", cutoff))
    except Exception as e:
        logger.warning(f"CallMedex booking session purge failed: {e}")


# ── Slots ────────────────────────────────────────────────────────────────────

def _fmt_hour(h: int) -> str:
    return f"{(h - 1) % 12 + 1}:00 {'AM' if h < 12 else 'PM'}"


def window_label(start: int) -> str:
    return f"{_fmt_hour(start)} – {_fmt_hour(WINDOWS[start])}"


def window_options(date_iso: str, now: datetime) -> list[int]:
    day = datetime.fromisoformat(date_iso).date()
    return [
        h for h in WINDOWS
        if datetime(day.year, day.month, day.day, h, tzinfo=IST) >= now + LEAD_TIME
    ]


def date_options(now: datetime) -> list[tuple[str, str]]:
    now = now.astimezone(IST)
    start = 0 if (now.hour < SAME_DAY_CUTOFF_HOUR and window_options(now.date().isoformat(), now)) else 1
    out = []
    for offset in range(start, start + 3):
        d = now.date() + timedelta(days=offset)
        name = "Today" if offset == 0 else "Tomorrow" if offset == 1 else d.strftime("%a")
        out.append((d.isoformat(), f"{name}, {d.day} {d.strftime('%b')}"))
    return out


def _date_label(date_iso: str) -> str:
    d = datetime.fromisoformat(date_iso).date()
    return f"{d.strftime('%a')}, {d.day} {d.strftime('%b')}"


# ── Inbound ──────────────────────────────────────────────────────────────────

def _extract(message) -> tuple[str, Optional[str]]:
    """(text, interactive reply id) from a webhook Message."""
    if getattr(message, "type", None) == "text" and getattr(message, "text", None):
        return message.text.body or "", None
    if getattr(message, "type", None) == "button" and getattr(message, "button", None):
        return message.button.text or "", message.button.payload
    inter = getattr(message, "interactive", None)
    if getattr(message, "type", None) == "interactive" and inter:
        reply = inter.button_reply or inter.list_reply or {}
        return reply.get("title", ""), reply.get("id")
    return "", None


async def handle_inbound(message, phone_number_id: str) -> None:
    phone = normalize_phone(getattr(message, "from_", ""))
    text, reply_id = _extract(message)
    await handle_turn(phone, text, reply_id, phone_number_id)


async def handle_turn(phone: str, text: str, reply_id: Optional[str], phone_number_id: str):
    async def send(payload: dict) -> bool:
        return await _send(phone, payload, phone_number_id)

    low = (text or "").strip().lower()
    # Word-bounded: patients type free-text addresses here ("fits" in "benefits").
    if low and any(re.search(rf"\b{re.escape(kw)}\b", low) for kw in EMERGENCY_KEYWORDS):
        await send(_text(EMERGENCY_TEXT))
        return
    if low in CANCEL_WORDS or reply_id == "cmx_cancel":
        await _clear(phone)
        await send(_text("Okay, cancelled. Message us anytime to book a home sample collection."))
        return

    if not callmedex_client.is_configured():
        logger.error("CallMedex booking requested but CALLMEDEX_BASE_URL is not set")
        await send(_text("Sorry, online booking isn't available right now. Please try again later."))
        return

    session = None if low in RESTART_WORDS else await _load(phone)
    if session is None:
        await _purge_stale()
        data = await _new_session(phone)
        if reply_id != "cmx_book":
            await _save(phone, "menu", data)
            await send(_menu(data))
            return
        session = {"state": "menu", "data": data}

    state, data = session["state"], dict(session["data"] or {})
    now = datetime.now(IST)

    if state == "menu":
        if reply_id == "cmx_book":
            return await _ask_date(phone, data, now, send)
        return await send(_menu(data))

    if state == "date":
        valid = {d for d, _ in date_options(now)}
        chosen = (reply_id or "").removeprefix("cmx_date_")
        if chosen in valid:
            data["date"] = chosen
            return await _ask_window(phone, data, now, send)
        return await _ask_date(phone, data, now, send)

    if state == "window":
        chosen = (reply_id or "").removeprefix("cmx_win_")
        if chosen.isdigit() and int(chosen) in window_options(data["date"], now):
            data["window"] = int(chosen)
            return await _ask_address(phone, data, send)
        return await _ask_window(phone, data, now, send)

    if state == "address":
        if reply_id == "cmx_addr_saved" and data.get("saved_address"):
            data["address"] = data["saved_address"]
            return await _ask_confirm(phone, data, send)
        await _save(phone, "address_line", data)
        return await send(_text(
            "Please type your full address including the 6-digit pincode.\n"
            "Example: Flat 302, Sai Residency, MVP Colony, 530017"
        ))

    if state == "address_line":
        m = PINCODE_RE.search(text or "")
        line1 = (PINCODE_RE.sub("", text or "").strip(" ,.-\n")) if m else ""
        if not m or len(line1) < 5:
            return await send(_text("I need your full address with a 6-digit pincode, e.g. Flat 302, Sai Residency, MVP Colony, 530017"))
        data["address"] = {"line1": line1[:200], "pincode": m.group(0)}
        await _save(phone, "address_city", data)
        return await send(_text("Which city is this in?"))

    if state == "address_city":
        city = (text or "").strip()
        if len(city) < 2 or reply_id:
            return await send(_text("Please type your city name, e.g. Visakhapatnam"))
        data["address"]["city"] = city[:80]
        return await _ask_confirm(phone, data, send)

    if state == "confirm":
        if reply_id == "cmx_confirm":
            return await _create_booking(phone, data, now, send)
        return await _ask_confirm(phone, data, send)

    # Unknown state (e.g. schema drift) — start over rather than dead-end.
    await _clear(phone)
    await send(_menu(data))


async def _new_session(phone: str) -> dict:
    session_id = str(uuid.uuid4())
    data: dict = {"session_id": session_id, "lang": "en"}
    resp = await callmedex_client.request(
        "GET", "/patients/lookup", params={"phone": phone},
        correlation_id=session_id, attempts=1, timeout=5.0,
    )
    if resp is not None and resp.status_code == 200:
        try:
            found = resp.json()
            data["patient_id"] = found.get("patient_id")
            if found.get("preferred_language") in CONFIRMATION:
                data["lang"] = found["preferred_language"]
            addr = found.get("default_address") or {}
            if addr.get("line1") and addr.get("city") and PINCODE_RE.fullmatch(str(addr.get("pincode") or "")):
                data["saved_address"] = {"line1": addr["line1"], "city": addr["city"], "pincode": str(addr["pincode"])}
        except Exception as e:
            logger.warning(f"CallMedex patient lookup returned unparseable body: {e}")
    return data


def _menu(data: dict) -> dict:
    return _buttons(
        "Welcome to CallMedex 👋\nBook a home blood sample collection — a certified phlebotomist "
        "comes to you.\n\nBy continuing you agree to share your address with CallMedex for this booking.",
        [("cmx_book", "Book home collection")],
    )


async def _ask_date(phone: str, data: dict, now: datetime, send) -> None:
    await _save(phone, "date", data)
    await send(_buttons("Which day should we come?", [(f"cmx_date_{d}", label) for d, label in date_options(now)]))


async def _ask_window(phone: str, data: dict, now: datetime, send) -> None:
    options = window_options(data["date"], now)
    if not options:
        await send(_text("No time slots are left on that day."))
        return await _ask_date(phone, data, now, send)
    await _save(phone, "window", data)
    await send(_list(
        f"Pick a collection time for {_date_label(data['date'])}:", "Choose time",
        [(f"cmx_win_{h}", window_label(h)) for h in options],
    ))


async def _ask_address(phone: str, data: dict, send) -> None:
    saved = data.get("saved_address")
    if saved:
        await _save(phone, "address", data)
        await send(_buttons(
            f"Collect at your saved address?\n{saved['line1']}, {saved['city']} - {saved['pincode']}",
            [("cmx_addr_saved", "Use this address"), ("cmx_addr_new", "New address")],
        ))
        return
    await _save(phone, "address_line", data)
    await send(_text(
        "Please type your full address including the 6-digit pincode.\n"
        "Example: Flat 302, Sai Residency, MVP Colony, 530017"
    ))


async def _ask_confirm(phone: str, data: dict, send) -> None:
    await _save(phone, "confirm", data)
    a = data["address"]
    await send(_buttons(
        "Please confirm your booking:\n"
        f"🩸 Home blood sample collection\n📅 {_date_label(data['date'])}, {window_label(data['window'])}\n"
        f"📍 {a['line1']}, {a['city']} - {a['pincode']}",
        [("cmx_confirm", "Confirm"), ("cmx_cancel", "Cancel")],
    ))


async def _create_booking(phone: str, data: dict, now: datetime, send) -> None:
    start = int(data["window"])
    # The patient may confirm long after picking — re-check the slot is still ahead.
    if start not in window_options(data["date"], now):
        await send(_text("That time slot has passed. Let's pick another one."))
        return await _ask_date(phone, data, now, send)

    a = data["address"]
    body = {
        "patient_id": data.get("patient_id"),
        "phone": phone,
        "service_type": "home_blood_collection",
        "requested_time_window": {
            "earliest": f"{data['date']}T{start:02d}:00:00+05:30",
            "latest": f"{data['date']}T{WINDOWS[start]:02d}:00:00+05:30",
        },
        "address": {"line1": a["line1"], "city": a["city"], "pincode": a["pincode"], "lat": None, "lng": None},
        "source": "whatsapp",
        "source_conversation_id": data["session_id"],
    }
    # Same booking details -> same key: a re-tap after a timeout replays
    # CallMedex's cached result instead of creating a second booking.
    key = callmedex_client.idempotency_key(
        "whatsapp-booking", phone, data["session_id"], data["date"], str(start), a["pincode"], a["line1"],
    )
    resp = await callmedex_client.request(
        "POST", "/whatsapp-bookings", json_body=body, idem_key=key,
        correlation_id=data["session_id"], attempts=1, timeout=20.0,
    )

    if resp is not None and resp.status_code == 201:
        result = resp.json()
        await _clear(phone)
        msg = CONFIRMATION.get(data.get("lang") or "en", CONFIRMATION["en"]).format(
            date=_date_label(data["date"]), window=window_label(start), booking_id=result.get("booking_id", "")
        )
        if result.get("status") != "confirmed":
            msg += "\nWe'll confirm the exact slot shortly."
        await send(_text(msg))
        logger.info(f"CallMedex WhatsApp booking {result.get('booking_id')} created for {mask_phone(phone)}")
        return

    if resp is not None and 400 <= resp.status_code < 500:
        # CallMedex caches 4xx under this key, so re-tapping cannot help.
        await _clear(phone)
        await send(_text("Sorry, we couldn't book this. Please reply HI to start again."))
        return

    await send(_buttons(
        "Sorry, we couldn't reach our booking system just now. Tap Confirm to try again.",
        [("cmx_confirm", "Confirm"), ("cmx_cancel", "Cancel")],
    ))
