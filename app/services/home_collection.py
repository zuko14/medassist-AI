"""Home sample collection for diagnostic centres (migration 097).

A home collection is an ordinary lab-test booking (appointments,
booking_type='lab_test') with collection_mode='home' and the visit details on
the same row, so payment, hold expiry, refunds and reconciliation are the
existing, unchanged code paths. This module owns what is new:

* the centre's settings (clinics.config / branches.config -> home_collection),
* which tests can be collected at home and which time slots are open,
* the service-area check on the patient's shared location,
* assigning the visit to a phlebotomist (automatically on confirmation, by a
  sweep for anything missed, or by an admin), and
* the visit's status lifecycle and the patient messages that go with it.

Tenancy: every query carries clinic_id; the one cross-tenant read is the
assignment sweep, which takes each row's clinic from the row itself.
"""

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.database import sb, supabase
from app.services.lab_classifier import CARDIAC, RADIOLOGY, SCANS, suggest_service_type
from app.services.permissions import PHLEBOTOMIST
from app.utils.validators import mask_phone

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

STATUSES = ("unassigned", "assigned", "en_route", "collected", "delivered", "failed")

#: What a phlebotomist (or a manager) may move a visit to from each status.
#: Assignment itself (-> "assigned") goes through assign(), never here.
TRANSITIONS: dict[str, frozenset] = {
    "unassigned": frozenset(),
    "assigned": frozenset({"en_route", "collected", "failed"}),
    "en_route": frozenset({"collected", "failed"}),
    "collected": frozenset({"delivered"}),
    "delivered": frozenset(),
    "failed": frozenset(),
}

#: Visits a phlebotomist is still responsible for: the booking is live.
LIVE_BOOKING_STATUSES = ("confirmed", "completed")

DEFAULT_SETTINGS: dict = {
    "enabled": False,
    "fee_paise": 0,
    # 0 = no threshold. Tests priced at or above it are collected free.
    "free_above_paise": 0,
    "slot_minutes": 60,
    # 0 = unlimited bookings per slot.
    "slot_capacity": 0,
    # Today's slots must start at least this far ahead.
    "lead_minutes": 60,
    # 0 = no service-area limit. Needs centre_lat / centre_lng to apply.
    "max_distance_km": 0,
    "centre_lat": None,
    "centre_lng": None,
    # Home-visit hours, e.g. [{"start": "07:00", "end": "11:00"}]. Empty = the
    # visits follow the lab's sample collection window (the original behaviour).
    "windows": [],
}

SLOT_MINUTES_ALLOWED = (30, 60, 90, 120)
#: Custom visit windows per centre (morning + afternoon + evening).
MAX_WINDOWS = 3
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
#: WhatsApp list messages hold at most 10 rows.
MAX_SLOT_ROWS = 10

_NOT_HOME_CATEGORY = re.compile(
    r"radiolog|imaging|\bscans?\b|x[\s-]*ray|ultrasound|sonograph|cardiac|\bct\b|\bmri\b",
    re.IGNORECASE,
)
_COORDS = re.compile(r"(-?\d{1,2}\.\d{3,})\s*,\s*(-?\d{1,3}\.\d{3,})")


# ── Settings ──────────────────────────────────────────────────────────────────


def normalize_settings(raw: Optional[dict]) -> dict:
    """Defaults filled in and every value coerced to its type. Anything
    unreadable falls back to the default rather than raising, because this is
    read on the patient's WhatsApp path."""
    out = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return out
    out["enabled"] = raw.get("enabled") is True
    for key in ("fee_paise", "free_above_paise", "slot_capacity", "lead_minutes", "max_distance_km"):
        try:
            out[key] = max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError):
            pass
    try:
        minutes = int(raw.get("slot_minutes") or DEFAULT_SETTINGS["slot_minutes"])
        out["slot_minutes"] = minutes if minutes in SLOT_MINUTES_ALLOWED else 60
    except (TypeError, ValueError):
        pass
    for key, lo, hi in (("centre_lat", -90, 90), ("centre_lng", -180, 180)):
        try:
            v = float(raw.get(key))
            out[key] = v if lo <= v <= hi else None
        except (TypeError, ValueError):
            out[key] = None
    out["windows"] = normalize_windows(raw.get("windows"))
    return out


def normalize_windows(raw) -> list[dict]:
    """Valid, sorted, non-overlapping HH:MM ranges; anything else is dropped."""
    if not isinstance(raw, list):
        return []
    ranges = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        start, end = str(w.get("start") or ""), str(w.get("end") or "")
        if _HHMM.match(start) and _HHMM.match(end) and start < end:
            ranges.append({"start": start, "end": end})
    ranges.sort(key=lambda w: w["start"])
    out: list[dict] = []
    for w in ranges:
        if out and w["start"] < out[-1]["end"]:
            continue  # overlaps the previous window
        out.append(w)
    return out[:MAX_WINDOWS]


async def get_settings(clinic: dict, branch_id: Optional[str] = None) -> dict:
    """The branch's own settings if it has them, else the clinic's.

    Same resolution as the lab collection window: a chain whose branches run
    different home services configures each branch. Never raises.
    """
    try:
        if branch_id:
            res = await sb(
                supabase.table("branches").select("config")
                .eq("id", branch_id).eq("clinic_id", clinic["id"])
            )
            if res.data:
                own = (res.data[0].get("config") or {}).get("home_collection")
                if isinstance(own, dict):
                    return normalize_settings(own)
        return normalize_settings((clinic.get("config") or {}).get("home_collection"))
    except Exception as e:
        logger.error(f"Could not read home collection settings for clinic {clinic.get('id')}: {e}")
        return normalize_settings(None)


def is_offered(clinic: dict, settings: dict) -> bool:
    from app.services.tenant import home_collection_available

    return home_collection_available(clinic) and bool(settings.get("enabled"))


def is_home_collectable(test: dict) -> bool:
    """Blood, urine and health packages can be collected at home; scans,
    X-rays and cardiac procedures need the centre's equipment."""
    category = (test.get("category") or "").strip()
    if category and _NOT_HOME_CATEGORY.search(category):
        return False
    return suggest_service_type(test.get("name") or "") not in (SCANS, RADIOLOGY, CARDIAC)


def fee_for(settings: dict, test_price_paise: int) -> int:
    fee = int(settings.get("fee_paise") or 0)
    free_above = int(settings.get("free_above_paise") or 0)
    if free_above and (test_price_paise or 0) >= free_above:
        return 0
    return fee


# ── Slots ─────────────────────────────────────────────────────────────────────


def _window_hours(window: dict, date_str: str) -> tuple[str, str]:
    start, end = window.get("start") or "07:00", window.get("end") or "11:00"
    try:
        sunday = datetime.strptime(date_str, "%Y-%m-%d").weekday() == 6
    except (TypeError, ValueError):
        sunday = False
    if sunday and window.get("sunday_start") and window.get("sunday_end"):
        start, end = window["sunday_start"], window["sunday_end"]
    return start, end


def slots_for(window: dict, settings: dict, date_str: str, now: Optional[datetime] = None) -> list[str]:
    """Visit slots ("07:00-08:00") on that date: inside the centre's own
    home-visit windows when it set any, else inside the collection window.

    The last slot of a window is cut short at its closing time rather than
    dropped. Today's slots that start sooner than lead_minutes from now are
    not offered.
    """
    now = now or datetime.now(IST)
    ranges = settings.get("windows") or [dict(zip(("start", "end"), _window_hours(window, date_str)))]
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return []
    step = timedelta(minutes=int(settings.get("slot_minutes") or 60))
    earliest = now + timedelta(minutes=int(settings.get("lead_minutes") or 0))
    out = []
    for r in ranges:
        try:
            cur = datetime.combine(day, datetime.strptime(r["start"], "%H:%M").time(), IST)
            end = datetime.combine(day, datetime.strptime(r["end"], "%H:%M").time(), IST)
        except (TypeError, ValueError, KeyError):
            continue
        while cur < end:
            nxt = min(cur + step, end)
            if cur >= earliest:
                out.append(f"{cur:%H:%M}-{nxt:%H:%M}")
            cur = nxt
    return out


async def slot_load(clinic_id: str, date_str: str, branch_id: Optional[str] = None) -> dict:
    """Live home bookings per slot on one date (held or confirmed)."""
    q = (
        supabase.table("appointments").select("collection_slot")
        .eq("clinic_id", clinic_id)
        .eq("appointment_date", date_str)
        .eq("collection_mode", "home")
        .in_("status", ["pending_payment", "confirmed"])
    )
    if branch_id:
        q = q.eq("branch_id", branch_id)
    rows = (await sb(q.limit(2000))).data or []
    load: dict = {}
    for r in rows:
        load[r.get("collection_slot")] = load.get(r.get("collection_slot"), 0) + 1
    return load


async def open_slots(
    clinic_id: str, window: dict, settings: dict, date_str: str,
    branch_id: Optional[str] = None, now: Optional[datetime] = None,
) -> list[str]:
    """slots_for() minus the slots already at capacity.

    ponytail: count-then-insert, so two patients racing for the last place in
    a slot can both get it (capacity + 1). A per-slot counter row with a CHECK
    is the upgrade if centres report overbooking.
    """
    slots = slots_for(window, settings, date_str, now)
    if slots and await all_off_on(clinic_id, date_str, branch_id):
        return []
    capacity = int(settings.get("slot_capacity") or 0)
    if not capacity or not slots:
        return slots
    load = await slot_load(clinic_id, date_str, branch_id)
    return [s for s in slots if load.get(s, 0) < capacity]


# ── Location ──────────────────────────────────────────────────────────────────


def parse_coordinates(text: str) -> Optional[tuple[float, float]]:
    """Coordinates typed or pasted as "17.7231, 83.3012" or inside a Google
    Maps link (?q=17.72,83.30 or @17.72,83.30,15z). For WhatsApp Web, which
    cannot share a location pin."""
    m = _COORDS.search(text or "")
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return lat, lng


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance (haversine)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def outside_service_area(settings: dict, lat: float, lng: float) -> Optional[float]:
    """The distance in km when the point is beyond the centre's radius, else None."""
    limit = int(settings.get("max_distance_km") or 0)
    clat, clng = settings.get("centre_lat"), settings.get("centre_lng")
    if not limit or clat is None or clng is None:
        return None
    d = distance_km(clat, clng, lat, lng)
    return d if d > limit else None


def maps_link(lat, lng) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}"


async def last_home_address(clinic_id: str, phone: str) -> Optional[dict]:
    """The patient's most recent home-collection address at this centre."""
    try:
        res = await sb(
            supabase.table("appointments")
            .select("collection_address, collection_landmark, collection_lat, collection_lng")
            .eq("clinic_id", clinic_id)
            .eq("patient_phone", phone)
            .eq("collection_mode", "home")
            .not_.is_("collection_lat", "null")
            .order("created_at", desc=True)
            .limit(1)
        )
    except Exception as e:
        logger.warning(f"Could not read last home address for {mask_phone(phone)}: {e}")
        return None
    row = (res.data or [None])[0]
    if not row or row.get("collection_address") in (None, "", "[REDACTED]"):
        return None
    return row


# ── Phlebotomists & assignment ────────────────────────────────────────────────


async def list_phlebotomists(clinic_id: str, active_only: bool = True) -> list[dict]:
    q = (
        supabase.table("clinic_admins")
        .select("id, username, full_name, phone, branch_id, is_active, off_dates")
        .eq("clinic_id", clinic_id)
        .eq("role", "staff")
        .eq("staff_role", PHLEBOTOMIST)
    )
    if active_only:
        q = q.eq("is_active", True)
    return (await sb(q.limit(500))).data or []


def display_name(p: Optional[dict]) -> str:
    if not p:
        return ""
    return (p.get("full_name") or "").strip() or p.get("username") or ""


def eligible_for(phleb: dict, appt: dict) -> bool:
    """A branch-pinned phlebotomist serves only that branch; an unpinned one
    serves every branch."""
    pb, ab = phleb.get("branch_id"), appt.get("branch_id")
    return not pb or not ab or str(pb) == str(ab)


def is_off(phleb: dict, date_str: Optional[str]) -> bool:
    """Marked on leave for that date (migration 101)."""
    return bool(date_str) and str(date_str) in {str(d) for d in (phleb.get("off_dates") or [])}


async def all_off_on(clinic_id: str, date_str: str, branch_id: Optional[str] = None) -> bool:
    """True when the centre has phlebotomists for this branch and every one of
    them is on leave that date, so no home visit can be honoured.

    A centre with no phlebotomist at all keeps offering slots, as before
    migration 101: those visits wait unassigned and the admins are alerted.
    Fails open (False) on a read error -- the slot list must not vanish
    because of a transient database fault.
    """
    try:
        serving = [p for p in await list_phlebotomists(clinic_id) if eligible_for(p, {"branch_id": branch_id})]
    except Exception as e:
        logger.warning(f"Could not read phlebotomist leave for clinic {clinic_id}: {e}")
        return False
    return bool(serving) and all(is_off(p, date_str) for p in serving)


def upcoming_off_dates(dates, today: Optional[str] = None) -> list[str]:
    """Valid YYYY-MM-DD dates from today on, sorted and de-duplicated."""
    today = today or datetime.now(IST).strftime("%Y-%m-%d")
    out = set()
    for d in dates or []:
        try:
            s = datetime.strptime(str(d), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            continue
        if s >= today:
            out.add(s)
    return sorted(out)


async def _day_loads(clinic_id: str, date_str: str) -> list[dict]:
    res = await sb(
        supabase.table("appointments")
        .select("phlebotomist_id, collection_slot")
        .eq("clinic_id", clinic_id)
        .eq("appointment_date", date_str)
        .eq("collection_mode", "home")
        .in_("status", list(LIVE_BOOKING_STATUSES))
        .not_.is_("phlebotomist_id", "null")
        .limit(2000)
    )
    return res.data or []


def pick_phlebotomist(appt: dict, phlebs: list[dict], loads: list[dict]) -> Optional[dict]:
    """Least busy in the same slot, then least busy that day; a phlebotomist
    pinned to the booking's branch before a floating one; then by name, so the
    choice is deterministic."""
    candidates = [
        p for p in phlebs
        if p.get("is_active", True) and eligible_for(p, appt) and not is_off(p, appt.get("appointment_date"))
    ]
    if not candidates:
        return None
    slot = appt.get("collection_slot")
    same_slot: dict = {}
    day: dict = {}
    for r in loads:
        pid = str(r.get("phlebotomist_id"))
        day[pid] = day.get(pid, 0) + 1
        if r.get("collection_slot") == slot:
            same_slot[pid] = same_slot.get(pid, 0) + 1
    ab = str(appt.get("branch_id") or "")
    return min(
        candidates,
        key=lambda p: (
            same_slot.get(str(p["id"]), 0),
            day.get(str(p["id"]), 0),
            0 if ab and str(p.get("branch_id") or "") == ab else 1,
            display_name(p).lower(),
            str(p["id"]),
        ),
    )


async def get_visit(clinic_id: str, appointment_id: str) -> Optional[dict]:
    res = await sb(
        supabase.table("appointments").select("*")
        .eq("clinic_id", clinic_id).eq("id", appointment_id)
        .eq("collection_mode", "home").limit(1)
    )
    return (res.data or [None])[0]


async def _write_assignment(clinic_id: str, appt: dict, phleb_id: str, expect_unassigned: bool) -> bool:
    """Compare-and-set, so a duplicate payment webhook, the sweep and an
    admin acting at the same moment cannot each assign the visit."""
    q = (
        supabase.table("appointments")
        .update({
            "phlebotomist_id": phleb_id,
            "collection_status": "assigned",
            "collection_status_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("clinic_id", clinic_id)
        .eq("id", appt["id"])
        .eq("status", "confirmed")
    )
    if expect_unassigned:
        q = q.is_("phlebotomist_id", "null")
    else:
        q = q.eq("collection_status", appt.get("collection_status"))
    return bool((await sb(q)).data)


async def auto_assign(clinic_id: str, appointment_id: str, alert_if_none: bool = False) -> Optional[dict]:
    """Assign a confirmed, unassigned home visit to the best phlebotomist.

    Returns the phlebotomist, or None when nothing was assigned (visit not
    confirmed, already assigned, or no phlebotomist available). Never raises:
    it runs inside payment confirmation, which must not fail on it.
    """
    try:
        appt = await get_visit(clinic_id, appointment_id)
        if not appt or appt.get("status") != "confirmed" or appt.get("phlebotomist_id"):
            return None
        phleb = pick_phlebotomist(
            appt,
            await list_phlebotomists(clinic_id),
            await _day_loads(clinic_id, appt["appointment_date"]),
        )
        if not phleb:
            logger.warning(
                f"HOME_COLLECTION_UNASSIGNED clinic={clinic_id} booking={appt.get('booking_ref')}: "
                f"no active phlebotomist for this branch"
            )
            if alert_if_none:
                await _notify_admins(
                    clinic_id,
                    f"Home collection needs a phlebotomist ({appt.get('booking_ref')})",
                    f"{appt.get('lab_test_name') or 'Lab test'} on {appt.get('appointment_date')} "
                    f"{appt.get('collection_slot')}: no active phlebotomist is available. "
                    f"Assign one from Home Collections.",
                )
            return None
        if not await _write_assignment(clinic_id, appt, phleb["id"], expect_unassigned=True):
            return None
        logger.info(f"Home collection {appt.get('booking_ref')} assigned to phlebotomist {phleb['id']}")
        await notify_patient(appt, "assigned", phleb)
        return phleb
    except Exception as e:
        logger.error(f"Home collection auto-assign failed for booking {appointment_id}: {e}")
        return None


async def assign(clinic_id: str, appt: dict, phleb: dict) -> bool:
    """Admin assignment or reassignment. The caller has checked the
    phlebotomist belongs to this clinic and may serve the booking's branch."""
    if appt.get("status") != "confirmed" or appt.get("collection_status") in ("collected", "delivered"):
        return False
    if str(appt.get("phlebotomist_id") or "") == str(phleb["id"]) and appt.get("collection_status") == "assigned":
        return True
    if not await _write_assignment(clinic_id, appt, phleb["id"], expect_unassigned=False):
        return False
    await notify_patient(appt, "assigned", phleb)
    return True


async def set_status(clinic_id: str, appt: dict, new_status: str, note: Optional[str] = None) -> bool:
    """Move a visit along TRANSITIONS. Compare-and-set on the current status,
    so two taps (or two devices) cannot both apply."""
    current = appt.get("collection_status")
    if new_status not in TRANSITIONS.get(current, frozenset()):
        return False
    update = {
        "collection_status": new_status,
        "collection_status_at": datetime.now(timezone.utc).isoformat(),
    }
    if note:
        update["collection_notes"] = note[:500]
    res = await sb(
        supabase.table("appointments").update(update)
        .eq("clinic_id", clinic_id).eq("id", appt["id"])
        .eq("collection_status", current)
    )
    if not res.data:
        return False
    if new_status in ("en_route", "collected", "failed"):
        phleb = None
        if appt.get("phlebotomist_id"):
            res2 = await sb(
                supabase.table("clinic_admins").select("id, username, full_name, phone")
                .eq("clinic_id", clinic_id).eq("id", appt["phlebotomist_id"]).limit(1)
            )
            phleb = (res2.data or [None])[0]
        await notify_patient(appt, new_status, phleb)
    return True


async def release_visits_of(clinic_id: str, phlebotomist_id: str, dates: Optional[list[str]] = None) -> int:
    """A phlebotomist was deactivated (dates=None: every upcoming day) or put
    on leave (those dates only): hand their unfinished visits to someone else.
    Collected samples stay with them (they carry the tubes)."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    q = (
        supabase.table("appointments")
        .update({
            "phlebotomist_id": None,
            "collection_status": "unassigned",
            "collection_status_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("clinic_id", clinic_id)
        .eq("phlebotomist_id", phlebotomist_id)
        .eq("collection_mode", "home")
        .eq("status", "confirmed")
        .in_("collection_status", ["assigned", "en_route"])
        .gte("appointment_date", today)
    )
    if dates is not None:
        if not dates:
            return 0
        q = q.in_("appointment_date", list(dates))
    res = await sb(q)
    released = res.data or []
    for row in released:
        await auto_assign(clinic_id, str(row["id"]), alert_if_none=True)
    return len(released)


async def assign_pending_sweep() -> int:
    """Assign confirmed home visits that are still unassigned (no phlebotomist
    existed at confirmation, or that attempt failed). Upcoming visits only."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    # unscoped: platform_sweep
    res = await sb(
        supabase.table("appointments").select("id, clinic_id")
        .eq("collection_mode", "home")
        .eq("status", "confirmed")
        .is_("phlebotomist_id", "null")
        .gte("appointment_date", today)
        .order("appointment_date")
        .limit(200)
    )
    assigned = 0
    for row in res.data or []:
        if await auto_assign(str(row["clinic_id"]), str(row["id"])):
            assigned += 1
    return assigned


# ── Notifications ─────────────────────────────────────────────────────────────


def _date_label(date_str: str) -> str:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a, %d %b")
    except (TypeError, ValueError):
        return date_str or ""


def patient_message(appt: dict, event: str, phleb: Optional[dict], lang: str) -> Optional[str]:
    name = display_name(phleb) or {"hi": "हमारे फ़्लेबोटोमिस्ट", "te": "మా ఫ్లెబోటమిస్ట్"}.get(lang, "Our phlebotomist")
    phone = (phleb or {}).get("phone") or ""
    when = f"{_date_label(appt.get('appointment_date'))}, {appt.get('collection_slot') or ''}"
    ref = appt.get("booking_ref") or ""
    test = appt.get("lab_test_name") or ""
    phone_line = f"\n📞 {phone}" if phone else ""
    msgs = {
        "assigned": {
            "en": f"👩‍⚕️ *Home sample collection assigned*\n\n*{name}* will collect the sample for *{test}* on *{when}*.{phone_line}\n\nThey will call you before arriving. Ref: `{ref}`",
            "hi": f"👩‍⚕️ *होम सैंपल कलेक्शन तय*\n\n*{name}* *{when}* को *{test}* का सैंपल लेने आएंगे।{phone_line}\n\nआने से पहले वे आपको कॉल करेंगे। संदर्भ: `{ref}`",
            "te": f"👩‍⚕️ *హోమ్ శాంపిల్ కలెక్షన్ కేటాయించబడింది*\n\n*{name}* *{when}* న *{test}* శాంపిల్ సేకరిస్తారు.{phone_line}\n\nవచ్చే ముందు మీకు కాల్ చేస్తారు. రెఫ్: `{ref}`",
        },
        "en_route": {
            "en": f"🚗 *{name}* is on the way to collect your sample (Ref `{ref}`). Please keep the patient ready.{phone_line}",
            "hi": f"🚗 *{name}* आपका सैंपल लेने आ रहे हैं (संदर्भ `{ref}`)। कृपया मरीज़ को तैयार रखें।{phone_line}",
            "te": f"🚗 *{name}* మీ శాంపిల్ సేకరించడానికి వస్తున్నారు (రెఫ్ `{ref}`). దయచేసి రోగిని సిద్ధంగా ఉంచండి.{phone_line}",
        },
        "collected": {
            "en": f"✅ Sample collected for *{test}* (Ref `{ref}`). We'll send your report here on WhatsApp as soon as it is ready.",
            "hi": f"✅ *{test}* का सैंपल ले लिया गया है (संदर्भ `{ref}`)। रिपोर्ट तैयार होते ही यहीं WhatsApp पर भेजेंगे।",
            "te": f"✅ *{test}* శాంపిల్ సేకరించబడింది (రెఫ్ `{ref}`). రిపోర్ట్ సిద్ధమైన వెంటనే ఇక్కడ WhatsApp లో పంపుతాము.",
        },
        "failed": {
            "en": f"⚠️ We could not collect your sample today (Ref `{ref}`). Our team will contact you to reschedule. You can also reply here.",
            "hi": f"⚠️ आज आपका सैंपल नहीं लिया जा सका (संदर्भ `{ref}`)। हमारी टीम दोबारा समय तय करने के लिए आपसे संपर्क करेगी।",
            "te": f"⚠️ ఈరోజు మీ శాంపిల్ సేకరించలేకపోయాము (రెఫ్ `{ref}`). మళ్లీ సమయం నిర్ణయించడానికి మా బృందం మిమ్మల్ని సంప్రదిస్తుంది.",
        },
    }
    by_lang = msgs.get(event)
    return by_lang.get(lang, by_lang["en"]) if by_lang else None


async def notify_patient(appt: dict, event: str, phleb: Optional[dict]) -> None:
    """Best effort. Outside Meta's 24-hour window a free-form message is
    refused by whatsapp_service; the visit itself is unaffected."""
    try:
        from app.services.tenant import get_clinic_by_id
        from app.services.whatsapp import whatsapp_service
        from app.database import get_patient_by_phone

        clinic = await get_clinic_by_id(str(appt["clinic_id"]))
        phone = appt.get("patient_phone")
        if not clinic or not phone or phone == "[REDACTED]":
            return
        patient = await get_patient_by_phone(clinic["id"], phone)
        lang = (patient or {}).get("language") or "en"
        text = patient_message(appt, event, phleb, lang)
        if text:
            await whatsapp_service.send_text(clinic, phone, text, _source="home_collection")
    except Exception as e:
        logger.warning(f"Home collection '{event}' message not sent for booking {appt.get('booking_ref')}: {e}")


async def _notify_admins(clinic_id: str, title: str, message: str) -> None:
    try:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("admin_notifications").insert({
            "clinic_id": clinic_id,
            "admin_id": None,
            "title": title[:200],
            "message": message[:1000],
            "is_read": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception as e:
        logger.warning(f"Could not create home collection admin notification: {e}")
