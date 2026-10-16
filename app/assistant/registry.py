"""Kriya Ketu action registry (action-registry-spec.md §1, §2, §5).

Every action names the EXISTING endpoint it calls; validate() enforces the
registration rules and tests/test_assistant_registry.py checks each spec's
permission against the real route, so the catalogue cannot drift from the code.
Endpoint-backed actions are added in P4, each after its endpoint passes the
KK-P0-15 hardening checklist (docs/kriya-ketu/hardening/).
"""

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.assistant import playbooks, present
from app.services.permissions import PERMISSIONS
from app.services.tenant import (
    ai_receptionist_enabled,
    dental_plans_enabled,
    home_collection_available,
    ipd_access,
    opd_enabled,
    specialty_enabled,
)
from app.voice.dates import today_ist

RISKS = ("R0", "R1", "R2", "R3", "H")
WRITE_RISKS = ("R1", "R2", "R3")
PERM_SOURCES = ("dependency", "handler", "role", "none")
ADMIN_ROLES = frozenset({"clinic_admin", "super_admin"})
ALL_ROLES = frozenset({"clinic_admin", "super_admin", "staff"})

#: Sidebar data-page -> label (the nav.open page table; RT-14 keeps it equal to the sidebar).
PAGES: dict[str, str] = {
    "dashboard": "Dashboard",
    "insights": "Insights",
    "profile": "Hospital Profile",
    "appointments": "Appointments",
    "doctors": "Doctors",
    "leaves": "Leaves",
    "holidays": "Holidays",
    "patients": "Patients",
    "voice": "AI Receptionist",
    "opdsetup": "OPD Setup",
    "opddesk": "Front Desk",
    "opdqueue": "Live Queue",
    "opdworkspace": "Consultation",
    "opdearnings": "Doctor Collections",
    "opdbilling": "OPD Billing",
    "opdanalytics": "OPD Analytics",
    "ipdboard": "Bed Board",
    "ipdadmit": "Admissions",
    "ipdchart": "Inpatient Chart",
    "ipdsetup": "IPD Setup",
    "leads": "WhatsApp Leads",
    "diagreports": "Report Automation",
    "labreports": "Lab Reports",
    "labtests": "Lab Tests / Services",
    "homecollect": "Home Collections",
    "corporate": "Corporate Health",
    "treatments": "Treatments",
    "dentalplans": "Treatment Plans",
    "prescriptions": "Prescriptions",
    "payments": "Payments",
    "paysettings": "Payment Settings",
    "branches": "Branches",
    "staff": "Staff Accounts",
    "data": "Data & Support",
    "connectors": "Report Connector",
}

#: Extra names per page (en / te-Latin / Telugu / hi-Latin / Devanagari), besides the key and label.
PAGE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "dashboard": ("home", "home page", "main page", "డాష్‌బోర్డ్", "డ్యాష్‌బోర్డ్", "डैशबोर्ड"),
    "insights": ("ఇన్‌సైట్స్",),
    "profile": ("profile", "clinic profile", "ప్రొఫైల్", "प्रोफाइल"),
    "appointments": ("appointment list", "అపాయింట్మెంట్స్", "అపాయింట్‌మెంట్స్", "అపాయింట్మెంట్లు", "अपॉइंटमेंट्स"),
    "doctors": ("doctor list", "డాక్టర్లు", "डॉक्टर्स", "डॉक्टरों"),
    "leaves": ("leave", "doctor leaves", "chutti", "chuttiyan", "selavu", "selavulu", "సెలవు", "సెలవులు",
               "छुट्टी", "छुट्टियां", "छुट्टियाँ"),
    "holidays": ("holiday", "hospital holidays", "సెలవు దినాలు", "अवकाश"),
    "patients": ("patient list", "పేషెంట్లు", "రోగులు", "पेशेंट्स", "मरीज", "मरीज़", "मरीजों"),
    "voice": ("receptionist", "voice receptionist", "voice calls", "రిసెప్షనిస్ట్", "रिसेप्शनिस्ट"),
    "opdqueue": ("queue", "క్యూ", "क्यू", "कतार"),
    "opddesk": ("reception desk", "ఫ్రంట్ డెస్క్"),
    "opdworkspace": ("consultation room",),
    "opdearnings": ("collections",),
    "opdbilling": ("billing", "bills", "బిల్లింగ్", "बिलिंग"),
    "ipdboard": ("bed board", "beds", "wards", "ward board", "bed status", "బెడ్లు", "बेड"),
    "ipdadmit": ("admit", "admission", "ipd admission", "admit patient", "bharti", "भर्ती"),
    "ipdchart": ("inpatient chart", "ipd chart", "nursing station", "patient chart"),
    "ipdsetup": ("ipd setup", "ward setup", "bed setup", "bed tariff"),
    "leads": ("leads", "lead list", "లీడ్స్", "लीड्स"),
    "diagreports": ("report delivery",),
    "labreports": ("ల్యాబ్ రిపోర్ట్స్", "लैब रिपोर्ट"),
    "labtests": ("lab tests", "test catalogue", "test prices", "టెస్టులు"),
    "homecollect": ("home collection", "home visits"),
    "treatments": ("procedures", "ట్రీట్మెంట్స్"),
    "dentalplans": ("dental plans",),
    "prescriptions": ("ప్రిస్క్రిప్షన్లు",),
    "payments": ("పేమెంట్లు", "पेमेंट्स", "भुगतान"),
    "paysettings": ("upi settings",),
    "branches": ("locations", "బ్రాంచ్‌లు", "शाखाएं"),
    "staff": ("staff", "users", "స్టాఫ్", "स्टाफ"),
    "data": ("support", "data"),
    "connectors": ("connector", "connectors"),
}


@dataclass(frozen=True)
class SlotSpec:
    """A parameter the router fills from the text (resolve.py); missing + required -> clarify form."""
    name: str
    kind: str              # "date" | "doctor" | "amount" | "term" (free search text, never asked for)
    required: bool = True
    label: str = ""        # the question shown on the clarify form (e.g. a consent question)


@dataclass(frozen=True)
class Handoff:
    """H tier: open `page` and pre-fill form elements; the human presses the page's own Save."""
    page: str
    fields: tuple[tuple[str, str], ...] = ()  # (element id, param name); never a password
    button: str = ""   # the page's own button the person presses ("" = a button on a list row)
    note: str = ""     # what the person does there


#: H-tier fields that look like a credential are refused at registration (and by the widget's fill).
SECRET_FIELD = re.compile(r"pass|secret|key|token|pin|otp|cvv", re.I)


@dataclass(frozen=True)
class ActionSpec:
    id: str
    module: str
    summary: str
    risk: str
    permission: Optional[str] = None
    perm_source: str = "none"
    roles: frozenset = ALL_ROLES
    feature: Optional[Callable[[dict], bool]] = None  # the SAME gate helper the route uses
    params: Any = None                                # pydantic model, extra="forbid"
    triggers: tuple[str, ...] = ()
    slots: tuple = ()
    preview: Optional[Callable] = None
    call: Optional[Callable] = None
    verify: Optional[Callable] = None
    present: Optional[Callable] = None              # (outcomes, params) -> {"message", "cards"} for R0 results
    handoff: Optional[Handoff] = None
    page: Optional[str] = None
    max_items: int = 1
    endpoint: Optional[tuple[str, str]] = None        # (METHOD, route path) that `call` dispatches to
    also: tuple[str, ...] = ()                         # handler only: other permissions it accepts instead
    one_of: tuple[str, ...] = ()                       # optional slots of which at least one is required
    unless: tuple[str, ...] = ()                       # phrases naming another object: any one drops this action


def prohibited_path(path: str) -> bool:
    # P tier: only the clinic panel's own /admin API, never /platform, webhooks, or the assistant itself.
    return not path.startswith("/admin/") or path.startswith("/admin/assistant")


def validate(spec: ActionSpec) -> None:
    """Raise ValueError listing every registration rule the spec breaks."""
    p = []
    if spec.risk not in RISKS:
        p.append(f"unknown risk {spec.risk!r}")
    if spec.perm_source not in PERM_SOURCES:
        p.append(f"unknown perm_source {spec.perm_source!r}")
    if spec.permission is not None and spec.permission not in PERMISSIONS:
        p.append(f"unknown permission {spec.permission!r}")
    if spec.perm_source in ("none", "role") and spec.permission is not None:
        p.append(f"permission must be None for perm_source={spec.perm_source!r}")
    if spec.perm_source in ("dependency", "handler") and spec.permission is None:
        p.append(f"perm_source={spec.perm_source!r} needs a permission")
    if spec.also and spec.perm_source != "handler":
        p.append("only a handler check accepts alternative permissions")
    p.extend(f"unknown permission {a!r}" for a in spec.also if a not in PERMISSIONS)
    if spec.perm_source == "role" and spec.roles != ADMIN_ROLES:
        p.append("perm_source='role' needs roles == admin roles")
    if spec.page is not None and spec.page not in PAGES:
        p.append(f"unknown page {spec.page!r}")
    if spec.endpoint is not None and prohibited_path(spec.endpoint[1]):
        p.append(f"endpoint {spec.endpoint[1]} is prohibited")
    if spec.max_items < 1:
        p.append("max_items must be >= 1")
    if spec.risk == "H":
        if spec.handoff is None:
            p.append("H action needs a handoff")
        elif spec.handoff.page not in PAGES:
            p.append(f"unknown handoff page {spec.handoff.page!r}")
        if spec.call or spec.verify or spec.endpoint or spec.preview:
            p.append("H action is never executable (no call/verify/endpoint/preview)")
        if spec.handoff:
            p.extend(f"handoff field {f!r} looks like a secret" for pair in spec.handoff.fields for f in pair
                     if SECRET_FIELD.search(f))
    if spec.risk in WRITE_RISKS:
        if spec.endpoint is None:
            p.append(f"{spec.risk} action needs an endpoint")
        if spec.call is None:
            p.append(f"{spec.risk} action needs a call")
        if spec.verify is None:
            p.append(f"{spec.risk} action needs verify (read-back)")
    if spec.endpoint is not None and spec.call is None and spec.risk == "R0":
        p.append("R0 action with an endpoint needs a call")
    optional = {s.name for s in spec.slots if not s.required}
    p.extend(f"one_of slot {n!r} must be an optional slot" for n in spec.one_of if n not in optional)
    if spec.risk == "R3" and spec.permission is None and not spec.roles <= ADMIN_ROLES:
        p.append("R3 without a stated permission must be admin-only")
    if p:
        raise ValueError(f"{spec.id}: " + "; ".join(p))


def _build(*specs: ActionSpec) -> dict[str, ActionSpec]:
    out = {}
    for spec in specs:
        validate(spec)
        if spec.id in out:
            raise ValueError(f"duplicate action id {spec.id!r}")
        out[spec.id] = spec
    return out


# ── R0 reads (KK-P4-01): the SAME endpoint and query the page uses, so results match the page ──

def _get(path: str, query: Optional[Callable[[dict], dict]] = None) -> Callable:
    def call(params: dict) -> list:
        from app.assistant.executor import Call, Step  # lazy: the executor imports this module
        return [Step(label="Read", call=Call("GET", path, query(params) if query else {}))]
    return call


def _day(params: dict) -> str:
    return params.get("date") or today_ist().isoformat()


def _on(params: dict) -> str:
    return present.fmt(_day(params), "date")


_DATE = (SlotSpec("date", "date", required=False),)  # no date in the text = today (IST)


def _read(id: str, summary: str, path: str, page: Optional[str], triggers: tuple, view: Callable, *,
          query: Optional[Callable[[dict], dict]] = None, **kw) -> ActionSpec:
    return ActionSpec(id=id, module=id.split(".")[0], summary=summary, risk="R0", page=page, triggers=triggers,
                      endpoint=("GET", path), call=_get(path, query),
                      present=lambda outcomes, params: view(outcomes[0].data, params), **kw)


def _rows(data, key: str) -> list:
    return data.get(key) if isinstance(data, dict) else data if key == "" else []


def _queue_rows(data) -> list:
    doctors = data.get("doctors") if isinstance(data, dict) else None
    return [r for d in doctors or [] if isinstance(d, dict) for r in d.get("rows") or []]


def _report_queue_rows(data) -> list:
    if not isinstance(data, dict):
        return []
    return [{**r, "issue": issue} for key, issue in (("needs_review", "Needs review"), ("failed_reports", "Failed"),
                                                     ("connector_failures", "Connector failed"))
            for r in data.get(key) or [] if isinstance(r, dict)]


def _patients_matching(data, params: dict) -> list:
    """The Patients page's own filter: name contains q (any case) or phone contains q."""
    q = (params.get("q") or "").lower()
    rows = [p for p in _rows(data, "patients") or [] if isinstance(p, dict)]
    return [p for p in rows if not q or q in (p.get("name") or "").lower() or q in (p.get("phone") or "")]


_APPT = (("Time", "appointment_time", "text"), ("Patient", "patient_name", "text"),
         ("Department", "department", "text"), ("Status", "status", "label"), ("Ref", "booking_ref", "text"))
_OPD, _VOICE, _HANDLER = ("OPD_FRONT_DESK", "OPD_CLINICAL", "OPD_BILLING"), "VOICE_VIEW", "handler"


def _opd_live(clinic: dict) -> bool:  # _opd_scope(..., live=True): OPD on and its setup finished
    return opd_enabled(clinic) and clinic.get("opd_state") == "READY"


READS = (
    _read("stats.summary", "Dashboard figures", "/admin/stats", "dashboard",
          ("dashboard stats", "clinic stats", "monthly stats", "dashboard numbers", "stats summary", "nela stats",
           "mahine ke stats", "గణాంకాలు", "आंकड़े"),
          lambda d, p: present.metrics("Last 30 days", "dashboard", d, (
              ("Appointments", "total_appointments", "num"), ("Confirmed", "confirmed", "num"),
              ("Completed", "completed", "num"), ("Cancelled", "cancelled", "num"), ("No-shows", "no_show", "num"),
              ("New patients", "new_patients", "num"), ("All patients", "total_patients", "num"))),
          query=lambda p: {"days": 30}),
    _read("insights.view", "Insights", "/admin/insights", "insights",
          ("booking trends", "revenue trend", "business trends", "insights report", "trends enti", "trends batao",
           "ట్రెండ్స్", "ट्रेंड्स"),
          lambda d, p: present.metrics("Insights, last 30 days", "insights", d, (
              ("Bookings", "bookings.totals.total", "num"), ("Confirmed", "bookings.totals.confirmed", "num"),
              ("Completed", "bookings.totals.completed", "num"), ("Cancelled", "bookings.totals.cancelled", "num"),
              ("Unique patients", "bookings.unique_patients", "num"), ("Repeat rate", "bookings.repeat_rate", "pct"),
              ("Collected", "revenue.collected_inr", "rupees"), ("Pending", "revenue.pending_inr", "rupees"))),
          query=lambda p: {"days": 30}, perm_source="role", roles=ADMIN_ROLES),
    _read("opd.analytics", "OPD figures", "/admin/opd/analytics", "opdanalytics",
          ("opd footfall", "opd numbers", "opd summary", "footfall today", "opd lekkalu", "opd ka hisaab",
           "ఓపీడీ లెక్కలు", "ओपीडी फुटफॉल"),
          lambda d, p: present.metrics(f"OPD on {_on(p)}", "opdanalytics", d, (
              ("Footfall", "footfall.total", "num"), ("Walk-ins", "footfall.walk_in", "num"),
              ("Booked", "footfall.booked", "num"), ("No-shows", "no_shows", "num"),
              ("Left the queue", "cancellations_in_queue", "num"), ("Net collected", "collections.net", "paise"),
              ("Outstanding", "collections.outstanding_paise", "paise"),
              ("Prescriptions signed", "prescriptions.signed", "num"))),
          query=lambda p: {"from": _day(p), "to": _day(p)}, slots=_DATE,
          permission="OPD_ADMIN", perm_source=_HANDLER, feature=opd_enabled),
    _read("appointments.search", "Appointments", "/admin/appointments", "appointments",
          ("today's appointments", "tomorrow's appointments", "yesterday's appointments", "appointments today",
           "appointments tomorrow", "appointments yesterday", "appointments on", "appointments for",
           "how many appointments", "list appointments", "ee roju appointments", "repu appointments",
           "aaj ke appointments", "kal ke appointments", "ఈ రోజు అపాయింట్మెంట్స్", "आज के अपॉइंटमेंट"),
          lambda d, p: present.table(f"Appointments on {_on(p)}", "appointments", _APPT, _rows(d, "appointments"),
                                     present.at(d, "total")),
          query=lambda p: {"date_basis": "visit", "date_from": _day(p), "date_to": _day(p), "limit": 100},
          slots=_DATE),
    _read("bookings.pending_review", "Bookings to review", "/admin/bookings/pending-review", "payments",
          ("bookings pending review", "pending review bookings", "bookings to review", "pending bookings",
           "review bookings", "review lo unna bookings", "review wale bookings", "రివ్యూ బుకింగ్స్",
           "रिव्यू वाली बुकिंग"),
          lambda d, p: present.table("Bookings waiting for review", "payments", (
              ("Date", "appointment_date", "date"), ("Time", "appointment_time", "text"),
              ("Patient", "patient_name", "text"), ("Ref", "booking_ref", "text"), ("Amount", "amount_paise", "paise")),
              _rows(d, "bookings"))),
    _read("payments.reconciliation", "Payment reconciliation", "/admin/payments/reconciliation", "payments",
          ("payment reconciliation", "reconciliation", "settlement check", "collection reconciliation",
           "dabbu lekka", "aaj ka hisaab", "రీకన్సిలియేషన్", "हिसाब मिलान"),
          lambda d, p: present.metrics(f"Online payments on {_on(p)}", "payments", d, (
              ("Confirmed bookings", "confirmed_count", "num"), ("Confirmed total", "confirmed_total_rupees", "rupees"),
              ("Waiting for review", "pending_review_count", "num"))),
          query=lambda p: {"date_str": _day(p)}, slots=_DATE, perm_source="role", roles=ADMIN_ROLES),
    _read("payments.stats", "Payment summary", "/admin/payments/stats", "payments",
          ("payment stats", "payment summary", "payments summary", "how much collected", "refund count",
           "payments lekkalu", "payment ka summary", "పేమెంట్ లెక్కలు", "पेमेंट का हिसाब"),
          lambda d, p: present.metrics("Payments, last 30 days", "payments", d, (
              ("Paid bookings", "confirmed_count", "num"), ("Collected", "confirmed_amount_rupees", "rupees"),
              ("Waiting for review", "pending_review_count", "num"), ("Refunded", "refunded_count", "num"),
              ("Refunded amount", "refunded_amount_rupees", "rupees"), ("Expired", "expired_count", "num"))),
          query=lambda p: {"days": 30}, perm_source="role", roles=ADMIN_ROLES),
    _read("opd.queue", "Live queue", "/admin/opd/queue", "opdqueue",
          ("who is waiting", "waiting patients", "queue status", "token status", "current token", "next token",
           "evaru wait chestunnaru", "kaun wait kar raha", "వెయిటింగ్ పేషెంట్లు", "कौन इंतज़ार कर रहा"),
          lambda d, p: present.table(f"Queue on {_on(p)}", "opdqueue", (
              ("Token", "token_number", "text"), ("Patient", "patient_name", "text"),
              ("Doctor", "doctor_name", "text"), ("Stage", "stage", "label")), _queue_rows(d)),
          query=lambda p: {"date": _day(p)}, slots=_DATE,
          permission=_OPD[0], also=_OPD[1:], perm_source=_HANDLER, feature=_opd_live),
    _read("doctors.list", "Doctors", "/admin/doctors", "doctors",
          ("list doctors", "which doctors", "doctors available", "doctor names", "how many doctors", "doctors evaru",
           "kaun se doctor", "ఏ డాక్టర్లు", "कौन से डॉक्टर"),
          lambda d, p: present.table("Doctors", "doctors", (
              ("Name", "name", "text"), ("Specialisation", "specialization", "text"),
              ("Department", "department", "text"), ("Active", "is_active", "yesno")), _rows(d, ""))),
    _read("lab_tests.list", "Lab tests", "/admin/lab-tests", "labtests",
          ("lab test rates", "test rates", "lab test prices", "list tests", "how many tests", "test dharalu",
           "test ke rate", "టెస్ట్ ధరలు", "टेस्ट के रेट"),
          lambda d, p: present.table("Lab tests", "labtests", (
              ("Test", "name", "text"), ("Price", "price_paise", "paise"), ("Sample", "sample_type", "label"),
              ("Active", "is_active", "yesno")), _rows(d, ""))),
    _read("treatments.list", "Treatments", "/admin/treatments", "treatments",
          ("list treatments", "treatment prices", "treatment rates", "which treatments", "treatment list",
           "treatment dharalu", "treatment ke rate", "ట్రీట్మెంట్ ధరలు", "ट्रीटमेंट के रेट"),
          lambda d, p: present.table("Treatments", "treatments", (
              ("Treatment", "name", "text"), ("Category", "category", "text"), ("From", "price_from_paise", "paise"),
              ("Shown", "is_active", "yesno")), _rows(d, "")),
          feature=specialty_enabled),
    _read("reports.deliveries", "Report deliveries", "/admin/reports/deliveries", "diagreports",
          ("report delivery status", "reports delivered", "reports sent today", "delivery log", "report deliveries",
           "reports pampinchara", "report bheje gaye", "రిపోర్ట్ డెలివరీ", "रिपोर्ट डिलीवरी"),
          lambda d, p: present.table("Report deliveries, last 7 days", "diagreports", (
              ("Sent", "sent_at", "time"), ("Patient", "patient_name", "text"), ("Report", "report_name", "text"),
              ("Status", "status", "label"), ("Delivery", "delivery_status", "label")), _rows(d, "deliveries")),
          query=lambda p: {"days": 7, "state": "all"}, permission="REPORTS_VIEW", perm_source="dependency"),
    _read("reports.queue", "Reports needing attention", "/admin/reports/queue", "diagreports",
          ("failed reports", "report queue", "unmatched reports", "reports needing review", "stuck reports",
           "fail aina reports", "atki reports", "ఫెయిల్ అయిన రిపోర్ట్స్", "अटकी रिपोर्ट"),
          lambda d, p: present.table("Reports needing attention", "diagreports", (
              ("Patient", "patient_name", "text"), ("Report", "report_name", "text"), ("Issue", "issue", "text")),
              _report_queue_rows(d)),
          permission="REPORTS_VIEW", perm_source="dependency"),
    _read("homecollect.visits", "Home visits", "/admin/home-collection/visits", "homecollect",
          ("home visits today", "home visits tomorrow", "today's home visits", "home visits on", "sample pickups",
           "pickups today", "ee roju home visits", "aaj ke home visit", "హోమ్ విజిట్స్", "होम विजिट"),
          lambda d, p: present.table(f"Home visits on {_on(p)}", "homecollect", (
              ("Slot", "collection_slot", "text"), ("Patient", "patient_name", "text"),
              ("Test", "lab_test_name", "text"), ("Status", "collection_status", "label"),
              ("Phlebotomist", "phlebotomist_name", "text")), _rows(d, "visits")),
          query=lambda p: {"date": _day(p)}, slots=_DATE,
          permission="HOME_COLLECTION_MANAGE", perm_source=_HANDLER, feature=home_collection_available),
    _read("leads.list", "WhatsApp leads", "/admin/leads", "leads",
          ("new leads", "recent leads", "how many leads", "list leads", "latest leads", "kotha leads", "naye leads",
           "కొత్త లీడ్స్", "नए लीड्स"),
          lambda d, p: present.table("WhatsApp leads, last 30 days", "leads", (
              ("Name", "name", "text"), ("Phone", "phone", "phone"), ("Stage", "stage", "label"),
              ("First contact", "first_contact_at", "time")), _rows(d, "rows"), present.at(d, "total")),
          query=lambda p: {"segment": "all", "days": 30, "limit": 25, "offset": 0},
          permission="LEADS_MANAGE", perm_source="dependency"),
    _read("messaging.usage", "WhatsApp usage", "/admin/messaging-usage", "dashboard",
          ("whatsapp usage", "message usage", "messages remaining", "message quota", "messages sent this month",
           "messages enni", "kitne message", "మెసేజ్ లెక్క", "कितने मैसेज"),
          lambda d, p: present.metrics("WhatsApp messages this month", "dashboard", d, (
              ("Plan", "plan_display_name", "text"), ("Sent", "messages_sent", "num"),
              ("Included", "included_messages", "num"), ("Remaining", "messages_remaining", "num"),
              ("Used", "usage_percent", "pct")))),
    _read("voice.calls", "Call log", "/admin/voice/calls", "voice",
          ("call log", "recent calls", "calls list", "receptionist calls", "call history", "calls chudali",
           "calls dikhao", "కాల్ లాగ్", "कॉल लॉग"),
          lambda d, p: present.table("Calls, last 30 days", "voice", (
              ("Time", "started_at", "time"), ("Caller", "caller_phone", "phone"), ("Status", "status", "label"),
              ("Outcome", "outcome", "label")), _rows(d, "calls"), present.at(d, "total")),
          query=lambda p: {"days": 30, "limit": 100},
          permission=_VOICE, perm_source=_HANDLER, feature=ai_receptionist_enabled),
    _read("voice.live", "Live calls", "/admin/voice/live", "voice",
          ("live calls", "ongoing calls", "calls in progress", "active calls", "ippudu jarugutunna calls",
           "abhi chal rahi calls", "లైవ్ కాల్స్", "लाइव कॉल"),
          lambda d, p: present.table("Live calls", "voice", (
              ("Started", "started_at", "time"), ("Caller", "caller_phone", "phone"), ("Status", "status", "label"),
              ("Waiting for", "waiting_for", "label")), _rows(d, "calls")),
          permission=_VOICE, perm_source=_HANDLER, feature=ai_receptionist_enabled),
    _read("voice.analytics", "Call analytics", "/admin/voice/analytics", "voice",
          ("call analytics", "call success rate", "receptionist performance", "autonomy rate", "call stats",
           "calls lekkalu", "calls ka analysis", "కాల్ విశ్లేషణ", "कॉल एनालिटिक्स"),
          lambda d, p: present.metrics("Calls, last 7 days", "voice", d, (
              ("Calls", "total_calls", "num"), ("Handled without staff", "autonomy_overall", "pct"),
              ("Average quality", "avg_quality", "num"), ("Minutes", "minutes", "num"))),
          query=lambda p: {"days": 7}, permission=_VOICE, perm_source=_HANDLER, feature=ai_receptionist_enabled),
    _read("voice.usage", "Call usage", "/admin/voice/usage", "voice",
          ("call minutes", "voice usage", "voice bill", "receptionist usage", "call cost", "call nimishalu",
           "kitne minute", "కాల్ నిమిషాలు", "कितने मिनट"),
          lambda d, p: present.metrics("Calls this month", "voice", d, (
              ("Calls", "calls", "num"), ("Minutes", "minutes", "num"), ("Cost", "cost_paise", "paise"),
              ("Budget", "budget_paise", "paise"), ("Budget used", "budget_used_pct", "pct"))),
          permission=_VOICE, perm_source=_HANDLER, feature=ai_receptionist_enabled),
    _read("voice.outbound_list", "Outbound calls", "/admin/voice/outbound", "voice",
          ("outbound calls", "outgoing calls", "follow up calls", "callback queue", "follow up calls enni",
           "follow up calls kitne", "ఫాలో అప్ కాల్స్", "आउटबाउंड कॉल"),
          lambda d, p: present.table("Outbound calls", "voice", (
              ("Created", "created_at", "time"), ("Phone", "patient_phone", "phone"), ("Status", "status", "label"),
              ("Attempts", "attempts", "num")), _rows(d, "jobs")),
          permission=_VOICE, perm_source=_HANDLER, feature=ai_receptionist_enabled),
    _read("notifications.list", "Notifications", "/admin/notifications", None,
          ("my notifications", "notifications", "unread notifications", "alerts", "notifications enti",
           "notifications dikhao", "నోటిఫికేషన్లు", "नोटिफिकेशन"),
          lambda d, p: present.table("Notifications", None, (
              ("Time", "created_at", "time"), ("Title", "title", "text"), ("Read", "is_read", "yesno")),
              _rows(d, "notifications")),
          query=lambda p: {"limit": 30}),
    _read("audit.recent", "Recent changes", "/admin/audit-logs", None,
          ("audit log", "audit trail", "activity log", "recent changes", "who changed", "evaru marcharu",
           "kisne badla", "ఆడిట్ లాగ్", "ऑडिट लॉग"),
          lambda d, p: present.table("Recent changes", None, (
              ("Time", "created_at", "time"), ("By", "username", "text"), ("Action", "action", "text")),
              _rows(d, "audit_logs")),
          query=lambda p: {"limit": 20}, perm_source="role", roles=ADMIN_ROLES),
    _read("ai.usage", "AI usage", "/admin/ai/usage", "profile",
          ("ai usage", "ai spend", "ai cost", "ai kharchu", "ai kharcha", "ఏఐ ఖర్చు", "एआई खर्च"),
          lambda d, p: present.metrics("AI use this month", "profile", d, (
              ("Total spend", "total_spend_rupees", "rupees"), ("Staff tools", "admin_spend_rupees", "rupees"),
              ("Patient chat", "patient_spend_rupees", "rupees"), ("Budget", "monthly_budget_rupees", "rupees"),
              ("Budget used up", "is_budget_exceeded", "yesno")))),
    _read("ai.budget", "AI budget", "/admin/ai/budget", "profile",
          ("ai budget", "ai limit", "monthly ai budget", "ai budget enta", "ai budget kitna", "ఏఐ బడ్జెట్",
           "एआई बजट"),
          lambda d, p: present.metrics("AI budget this month", "profile", d, (
              ("Budget", "budget_rupees", "rupees"), ("Spent", "current_spend_rupees", "rupees")))),
    _read("subscription.view", "Subscription", "/admin/subscription", "dashboard",
          ("subscription status", "subscription", "my plan", "plan validity", "plan details", "plan eppudu aipotundi",
           "plan kab khatam", "సబ్స్క్రిప్షన్", "सब्सक्रिप्शन"),
          lambda d, p: present.metrics("Subscription", "dashboard", d, (
              ("Status", "subscription.status", "label"), ("Days remaining", "subscription.days_remaining", "num"),
              ("Daily report limit", "daily_reports.limit", "num")))),
    _read("data.storage", "Data storage", "/admin/data/storage", "data",
          ("data storage", "storage used", "record limit", "storage left", "storage enta", "storage kitna",
           "డేటా స్టోరేజ్", "डेटा स्टोरेज"),
          lambda d, p: present.metrics("Imported records", "data", d, (
              ("Used", "used", "num"), ("Limit", "limit", "num"), ("Remaining", "remaining", "num"),
              ("Used share", "percent", "pct"))),
          perm_source="role", roles=ADMIN_ROLES),
    # Released 2026-10-14 after D-21 / D-22 (branch scope) were fixed.
    _read("connectors.failed", "Connector failures", "/admin/connectors/failed-reports", "connectors",
          ("connector failed reports", "connector failed", "connector failures", "connector errors",
           "documents not sent", "connector fail ayinavi", "connector fail hue", "కనెక్టర్ ఫెయిల్", "कनेक्टर फेल"),
          lambda d, p: present.table("Unresolved connector failures", "connectors", (
              ("Last try", "last_attempt_at", "time"), ("Patient", "patient_name", "text"),
              ("Report", "report_name", "text"), ("Error", "last_error", "text")), _rows(d, "failed_reports")),
          query=lambda p: {"unresolved_only": True}),
    _read("patients.search", "Find a patient", "/admin/patients", "patients",
          ("find patient", "search patient", "patient named", "look up patient", "patient search",
           "patient vetuku", "patient dhundo", "పేషెంట్ వెతుకు", "मरीज ढूंढो"),
          lambda d, p: present.table(f'Patients matching "{p["q"]}"' if p.get("q") else "Patients", "patients", (
              ("Name", "name", "text"), ("Phone", "phone", "phone"), ("Visits", "visit_count", "num")),
              _patients_matching(d, p)),
          slots=(SlotSpec("q", "term", required=False),)),
)

# ── PB-01 doctor leave (KK-P4-02; app/assistant/playbooks.py) ──
_LEAVE_SLOTS = (SlotSpec("doctor", "doctor"), SlotSpec("date", "date"), SlotSpec("date_end", "date_end", False),
                SlotSpec("leave_type", "leave_type", False))
LEAVES = (
    ActionSpec(id="doctor_leave.create", module="doctor_leave", summary="Add doctor leave", risk="R2",
               permission="DOCTOR_LEAVES_CREATE", perm_source="dependency", page="leaves",
               endpoint=("POST", "/admin/leaves"), slots=_LEAVE_SLOTS, max_items=playbooks.MAX_LEAVE_DAYS,
               triggers=("on leave", "is on leave", "will be on leave", "add leave", "mark leave", "add+leave", "create+leave", "apply+leave", "chutti",
                         "chutti pe", "chutti par", "selavu", "selavulo", "=సెలవు", "సెలవులో", "సెలవు పెట్టారు",
                         "=छुट्टी", "छुट्टी पर"),
               preview=playbooks.leave_create_preview, call=playbooks.leave_create_call,
               verify=playbooks.verify_leave_added),
    ActionSpec(id="doctor_leave.delete", module="doctor_leave", summary="Remove doctor leave", risk="R2",
               permission="DOCTOR_LEAVES_DELETE", perm_source="dependency", page="leaves",
               endpoint=("DELETE", "/admin/leaves/{leave_id}"), slots=_LEAVE_SLOTS[:3],
               max_items=playbooks.MAX_LEAVE_DAYS,
               triggers=("cancel+leave", "remove+leave", "delete+leave", "cancel+chutti", "radd+chutti",
                         "hatao+chutti", "raddu+selavu", "రద్దు+సెలవు", "रद्द+छुट्टी"),
               preview=playbooks.leave_delete_preview, call=playbooks.leave_delete_call,
               verify=playbooks.verify_leave_removed),
    ActionSpec(id="doctor_leave.impact", module="doctor_leave", summary="Bookings a leave would cancel", risk="R0",
               page="appointments", endpoint=("GET", "/admin/appointments"), slots=_LEAVE_SLOTS,
               triggers=("bookings affected", "affected bookings", "appointments affected", "affected appointments",
                         "affected by", "leave impact", "affect+leave", "affected+leave", "affect ayye", "kitni booking affect",
                         "प्रभावित बुकिंग"),
               call=playbooks.leave_impact_call, present=playbooks.leave_impact_present),
)

# ── KK-P4-05 front desk (app/assistant/playbooks.py). Steps come from the preview, which finds the one record. ──
_WHO = (SlotSpec("who", "booking"),)


def _desk(id: str, summary: str, risk: str, method: str, path: str, page: Optional[str], triggers: tuple, preview,
          **kw) -> ActionSpec:
    return ActionSpec(id=id, module=id.split(".")[0], summary=summary, risk=risk, page=page, triggers=triggers,
                      endpoint=(method, path), preview=preview, call=playbooks.desk_call,
                      verify=playbooks.verify_per_step, **{"slots": _WHO, **kw})


_CHECK_IN = ("check in", "checkin", "has arrived", "arrived", "vachharu", "vacharu", "aa gaye", "aa gaya", "aa gayi",
             "చెక్ ఇన్", "चेक इन")
DESK = (
    # Off where OPD is live: there a check-in becomes opd.arrive (VARIANTS, KK-FR-061).
    _desk("appointment.check_in", "Check in a patient", "R1", "POST", "/admin/appointments/{appointment_id}/check-in",
          "appointments", _CHECK_IN, playbooks.check_in_preview, feature=lambda c: not _opd_live(c)),
    _desk("appointment.cancel", "Cancel a booking", "R2", "DELETE", "/admin/appointments/{appointment_id}",
          "appointments", ("cancel+appointment", "cancel+booking", "cancel+appt", "radd+booking", "radd+appointment",
                           "raddu+booking", "raddu+appointment", "రద్దు+అపాయింట్", "రద్దు+బుకింగ్", "रद्द+बुकिंग",
                           "रद्द+अपॉइंटमेंट"), playbooks.cancel_preview),
    _desk("booking.confirm", "Confirm a booking under review", "R2", "POST", "/admin/bookings/{booking_id}/confirm",
          "payments", ("confirm+booking", "approve+booking", "confirm+payment", "బుకింగ్+కన్ఫర్మ్", "बुकिंग+कन्फर्म"),
          playbooks.booking_confirm_preview),
    _desk("booking.reject", "Reject a booking under review", "R2", "POST", "/admin/bookings/{booking_id}/reject",
          "payments", ("reject+booking", "decline+booking", "reject+payment", "బుకింగ్+రిజెక్ట్", "बुकिंग+रिजेक्ट"),
          playbooks.booking_reject_preview),
    # The Live Queue page's own endpoint; a clinical user may move only their own queue (enforced there).
    _desk("opd.call_next", "Call the next patient", "R1", "POST", "/admin/opd/queue/call-next", "opdqueue",
          ("call next", "next patient", "agla patient", "agle patient", "agla mareez", "తదుపరి పేషెంట్",
           "अगला मरीज"), playbooks.call_next_preview,
          slots=(SlotSpec("doctor", "doctor"),), permission=_OPD[0], also=_OPD[1:2], perm_source=_HANDLER,
          feature=_opd_live),
    _desk("notifications.mark_all_read", "Mark all notifications as read", "R1", "POST",
          "/admin/notifications/mark-all-read", None,
          ("mark all read", "mark all as read", "mark+notifications", "clear+notifications", "notifications+read karo",
           "notifications+read cheyyi", "notifications+read cheyandi", "నోటిఫికేషన్లు+చదివినట్టు", "नोटिफिकेशन+पढ़"),
          playbooks.mark_read_preview, slots=()),
)

# ── KK-P4-03: report resend (PB-02), lead message, connector run-now (app/assistant/playbooks.py) ──
_RESEND = ("POST", "/admin/reports/{report_id}/resend")
SENDS = (
    _desk("report.resend", "Resend a lab report", "R2", *_RESEND, "diagreports",
          ("resend+report", "send+report+again", "report+again", "report+malli", "report+dobara", "report+phir+se",
           "రిపోర్ట్+మళ్ళీ", "रिपोर्ट+दोबारा"), playbooks.report_resend_preview,
          slots=(SlotSpec("who", "lead"),), permission="REPORTS_RESOLVE", perm_source="dependency"),
    _desk("report.resend_failed", "Resend failed lab reports", "R3", *_RESEND, "diagreports",
          ("resend+failed", "resend+fail", "failed+again", "fail+malli", "failed+malli", "failed+dobara",
           "fail+dobara", "ఫెయిల్+మళ్ళీ", "फेल+दोबारा"), playbooks.report_resend_failed_preview,
          slots=(), permission="REPORTS_RESOLVE", perm_source="dependency", max_items=playbooks.MAX_RESEND),
    # The text is the staff member's own (quotes / after a colon); the endpoint enforces own-contact,
    # opt-out and the 24 h window (leads.send_lead_message). One recipient per run (T-19).
    _desk("lead.message", "Send a WhatsApp message", "R2", "POST", "/admin/leads/message", "leads",
          ("message", "send+message", "whatsapp+message", "msg", "message+pampu", "message+bhejo", "మెసేజ్+పంపు",
           "मैसेज+भेजो"), playbooks.lead_message_preview,
          slots=(SlotSpec("who", "lead"), SlotSpec("message", "message")), permission="LEADS_MANAGE",
          perm_source="dependency"),
    _desk("connector.run_now", "Run the report connector now", "R1", "POST", "/admin/connectors/{connector_id}/run-now",
          "connectors", ("run+connector", "connector+chalao", "sync+mocdoc", "mocdoc+sync", "కనెక్టర్+రన్",
                         "कनेक्टर+चलाओ"), playbooks.connector_run_preview,
          slots=(), permission="CONNECTOR_MANAGE", perm_source="dependency"),
)

# holiday.create: released after D-27 (migration 109: one holiday per clinic per date). R3 with the typed
# count of that day's confirmed bookings (registry §4.5); the stated permission lets permitted staff use it.
HOLIDAY = _desk("holiday.create", "Add a hospital holiday", "R3", "POST", "/admin/holidays", "holidays",
                ("add+holiday", "mark+holiday", "declare+holiday", "holiday+on", "close+clinic", "close+hospital",
                 "clinic+closed", "hospital+closed", "clinic+band", "hospital+band", "holiday+pettandi",
                 "holiday+pettu", "క్లినిక్+మూసివేయండి", "अवकाश+घोषित", "क्लिनिक+बंद"),
                playbooks.holiday_create_preview,
                slots=(SlotSpec("date", "date"), SlotSpec("name", "holiday_name", False)),
                permission="HOLIDAYS_CREATE", perm_source="dependency", max_items=playbooks.MAX_HOLIDAY_BOOKINGS)

# ── KK-P4-06: PB-03 leads -> calls, PB-04 funnel, PB-05 automatic lead calls (app/assistant/playbooks.py) ──

def read_only_post(call) -> bool:
    """The POSTs a preview may send, none of which write: the outbound bulk dry run (KK-P4-06a), the
    OPD duplicate-patient check (PB-06) and the lab auto-classify dry run (apply=false, KK-P4-09)."""
    if call.method.upper() != "POST":
        return False
    body = call.json or {}
    return ((call.path == "/admin/voice/outbound/bulk" and body.get("dry_run") is True)
            or call.path == "/admin/opd/patients/duplicate-check"
            or (call.path == "/admin/lab-tests/auto-classify" and body.get("apply") is False))


_VOICE_ON = dict(permission="VOICE_MANAGE", perm_source=_HANDLER, feature=ai_receptionist_enabled)
LEAD_CALLS = (
    # R3 always (typed count), stricter than PB-03's "R2 when N <= 10": one spec has one risk.
    _desk("leads.call", "Call leads with the AI receptionist", "R3", "POST", "/admin/voice/outbound/bulk", "leads",
          ("call+leads", "call+hot+leads", "call+open+leads", "call+new+leads", "call+all+leads",
           "leads ki call", "leads ko call", "లీడ్స్+కాల్", "लीड्स+कॉल"), playbooks.lead_calls_preview,
          slots=(SlotSpec("segment", "segment", False), SlotSpec("days", "lead_days", False),
                 SlotSpec("interest", "interest_term", False)), max_items=playbooks.MAX_LEAD_CALLS, **_VOICE_ON),
    # Reads the call queue and the booked-leads count; one card from both.
    ActionSpec(id="leads.funnel", module="leads", summary="Lead call results", risk="R0", page="voice",
               endpoint=("GET", "/admin/voice/outbound"),
               # "ela" / "kaise" (how) route to how-to help first (rules.HOW_TO), so they are not triggers here.
               triggers=("lead conversion", "lead calls+go", "lead call results", "lead funnel", "lead calls+result",
                         "lead calls+lekkalu", "lead calls+hisaab", "లీడ్ కాల్స్", "लीड कॉल"),
               call=playbooks.leads_funnel_call, present=playbooks.leads_funnel_present,
               permission="VOICE_VIEW", perm_source=_HANDLER, feature=ai_receptionist_enabled),
    _desk("voice.auto_leads", "Automatic lead calls on/off", "R2", "PUT", "/admin/voice/settings", "voice",
          ("auto+lead+calls", "automatic+lead+calls", "auto+leads", "call+leads+automatically", "auto lead call",
           "ఆటో+లీడ్", "ऑटो+लीड"), playbooks.auto_leads_preview,
          slots=(SlotSpec("switch", "on_off"),), **_VOICE_ON),
)
# ── KK-P4-07: PB-06 register + walk-in token; OPD arrive / recall / stage (live OPD only) ──
_DESK_OPD = dict(permission=_OPD[0], perm_source=_HANDLER, feature=_opd_live)
_ROOM_OPD = dict(permission=_OPD[0], also=_OPD[1:2], perm_source=_HANDLER, feature=_opd_live)  # + own-doctor rule
_TARGET = (SlotSpec("target", "queue_target"), SlotSpec("doctor", "doctor", False))
# "register" alone means a patient, but "register doctor Srinivas" / "register a nurse" / "doctor ni register"
# name another object right at the verb: those never open the patient form (consent, phone).
_NOT_PATIENTS = ("doctor", "dr", "doctors", "staff", "nurse", "receptionist", "cashier", "employee", "user", "login",
                 "branch", "test", "lab test", "company", "ward", "bed", "phlebotomist", "technician", "pharmacist",
                 "ambulance", "vehicle", "equipment", "machine", "device", "room", "department", "service", "package",
                 "scheme", "insurance", "insurer", "tpa", "vendor", "supplier", "medicine", "drug", "product", "item",
                 "hospital", "clinic", "lab", "pharmacy", "theatre", "ot", "icu", "leave", "holiday", "shift", "slot")
_REGISTER_OTHER = tuple(f"register {art}{who}" for who in _NOT_PATIENTS
                        for art in ("", "a ", "an ", "the ", "new ", "a new ")) + tuple(
    f"{who}{tail}" for who in _NOT_PATIENTS for tail in (" registration", " ni register", " ko register",
                                                          " nu register", " ki registration")) + (
    "డాక్టర్ని రిజిస్టర్", "డాక్టర్ ని రిజిస్టర్", "డాక్టర్ను రిజిస్టర్", "डॉक्टर को रजिस्टर", "डॉक्टर का रजिस्ट्रेशन")
_PERSON = ("patient", "patients", "person", "people", "family", "member", "child", "children", "baby", "kid", "boy",
           "girl", "man", "woman", "lady", "gentleman", "son", "daughter", "wife", "husband", "mother", "father",
           "brother", "sister", "walk", "new", "existing", "old", "returning", "senior", "pregnant", "more", "one",
           "rogi", "mareez", "marij")
_REGISTER_A_THING = re.compile(r"\bregister (?:(?:a|an|the|another) (?:new )?|new )(?!(?:" + "|".join(_PERSON) + r")\b)[a-z]+")
OPD_DESK = (
    _desk("patient.register", "Register a new patient", "R2", "POST", "/admin/opd/patients", "opddesk",
          ("register", "registration", "register+patient", "new patient", "register cheyyi", "register karo",
           "రిజిస్టర్", "रजिस्टर"), playbooks.register_preview,
          slots=(SlotSpec("name", "person_name"), SlotSpec("phone", "phone"),
                 SlotSpec("data_consent", "yesno", label="Patient agrees to their data being stored"),
                 SlotSpec("whatsapp_opt_in", "yesno", label="Patient agrees to WhatsApp messages"),
                 SlotSpec("new_person", "new_person", False)), unless=_REGISTER_OTHER + (_REGISTER_A_THING,), **_DESK_OPD),
    _desk("opd.walk_in", "Give a walk-in token", "R2", "POST", "/admin/opd/walk-ins", "opddesk",
          ("token for", "give+token", "walk in", "walkin", "walk-in", "token+ivvandi", "token+ivvu", "token+do",
           "token+dijiye", "టోకెన్+ఇవ్వండి", "टोकन+दो"), playbooks.walk_in_preview,
          slots=(SlotSpec("who", "person_name"), SlotSpec("doctor", "doctor"), SlotSpec("visit_type", "visit_type", False)),
          **_DESK_OPD),
    # No words of its own: a check-in becomes this on a live-OPD clinic (VARIANTS, applied by the router).
    _desk("opd.arrive", "Mark a booked patient arrived", "R1", "POST", "/admin/opd/appointments/{appointment_id}/arrive",
          "opddesk", (), playbooks.arrive_preview, **_DESK_OPD),
    _desk("opd.recall", "Recall a patient to the room", "R1", "POST", "/admin/opd/queue/{appointment_id}/recall",
          "opdqueue", ("recall", "call+again", "announce+again", "malli pilu", "malli pilavandi", "dobara bulao",
                       "రీకాల్", "रीकॉल"), playbooks.recall_preview, slots=_TARGET, **_ROOM_OPD),
    # A move to "cancelled" is never offered (resolve_stage returns None for it): the page's own flow.
    _desk("opd.stage", "Move a patient in the queue", "R1", "POST", "/admin/opd/queue/{appointment_id}/stage",
          "opdqueue", ("to billing", "billing ki", "billing ko", "mark+done", "move+token", "move+to", "back to waiting",
                       "to vitals", "బిల్లింగ్+కి", "बिलिंग+में"), playbooks.stage_preview,
          slots=_TARGET + (SlotSpec("stage", "stage"),), **_ROOM_OPD),
)

# ── KK-P4-08: PB-07 lab prices, lab test create / deactivate, collection hours (Test Catalogue page) ──
# All LAB_TESTS_MANAGE route dependencies. A pinned user only touches their branch's and the shared tests (D-31).
_LAB = dict(permission="LAB_TESTS_MANAGE", perm_source="dependency")
_LAB_TEST = SlotSpec("test", "lab_test")
_PUT_TEST = ("PUT", "/admin/lab-tests/{test_id}")
CATALOGUE = (
    _desk("lab_test.price", "Change a lab test price", "R2", *_PUT_TEST, "labtests",
          ("change+price", "set+price", "update+price", "price+to", "rate+to", "price+by", "rate+by",
           "increase+price", "reduce+price", "decrease+price", "raise+price", "price+rupees", "rate+rupees",
           "price+marchu", "rate+marchu", "price+badlo", "rate+badlo", "ధర+మార్చు", "दाम+बदलो", "रेट+बदलो"),
          playbooks.lab_price_preview, slots=(_LAB_TEST, SlotSpec("price", "price")), **_LAB),
    # R3 always (typed count), PB-07: "> 1 = R3"; a percent only, so one rule applies to every test.
    _desk("lab_test.price_bulk", "Change many lab test prices", "R3", *_PUT_TEST, "labtests",
          ("all+prices", "all+rates", "prices+by", "rates+by", "anni+prices", "anni+tests", "anni+rates",
           "sab+tests", "sab+daam", "sabhi+tests", "అన్ని+ధరలు", "सभी+दाम"),
          playbooks.lab_price_bulk_preview,
          slots=(SlotSpec("scope", "lab_scope"), SlotSpec("price", "percent", label="Percent change, e.g. +10 or -5"),
                 SlotSpec("round", "round_to", False)), max_items=playbooks.MAX_LAB_BULK, **_LAB),
    # The name is stored as typed, so it is only taken from quotes; otherwise the form asks.
    _desk("lab_test.create", "Add a lab test", "R2", "POST", "/admin/lab-tests", "labtests",
          ("add+test", "add+lab test", "new+test", "create+test", "register test", "register a test", "register lab test",
           "register a lab test", "test+add cheyyi", "test+jodo", "టెస్ట్+జోడించు",
           "टेस्ट+जोड़ो"), playbooks.lab_create_preview,
          slots=(SlotSpec("name", "test_name", label="Test name, exactly as patients should see it"),
                 SlotSpec("price", "price")), **_LAB),
    # Deactivate, never delete (deletion is H, registry §4.6).
    _desk("lab_test.deactivate", "Stop offering a lab test", "R2", *_PUT_TEST, "labtests",
          ("deactivate+test", "disable+test", "hide+test", "stop offering", "test+band", "test+aapu",
           "test+aapandi", "టెస్ట్+ఆపు", "टेस्ट+बंद"), playbooks.lab_deactivate_preview, slots=(_LAB_TEST,), **_LAB),
    _desk("lab_collection_window.update", "Change sample collection hours", "R2", "PUT", "/admin/lab-collection-window",
          "labtests", ("collection+hours", "collection+time", "collection+timings", "collection+timing",
                       "collection+window", "collection+samayam", "collection+samay", "కలెక్షన్+సమయం",
                       "कलेक्शन+समय"), playbooks.collection_window_preview,
          slots=(SlotSpec("window", "time_window", label="Collection hours, e.g. 7 to 11"),), **_LAB),
)

# ── KK-P4-09: PB-08 clean-up list + suggested fasting/prep fixes, auto-classify (owner, 2026-10-15) ──
# Duplicate deletions, price fixes, PB-09 import and AI details are hand-offs to the page (help.py).
CLEANUP = (
    ActionSpec(id="lab_tests.cleanup", module="lab_tests", summary="Catalogue clean-up suggestions", risk="R0",
               page="labtests", endpoint=("GET", "/admin/lab-tests/cleanup-suggestions"),
               triggers=("clean up+suggestions", "cleanup+suggestions", "clean up+catalogue", "cleanup+catalogue",
                         "duplicate tests", "duplicate+catalogue", "catalogue+problems", "catalogue+quality",
                         "కేటలాగ్+సమస్యలు", "कैटलॉग+समस्या"),
               call=playbooks.cleanup_call, present=playbooks.cleanup_present, **_LAB),
    _desk("lab_tests.fix_prep", "Add missing fasting instructions", "R3", "POST", "/admin/lab-tests/cleanup-apply",
          "labtests", ("fasting+instructions", "fix+fasting", "fasting+fix", "prep+instructions",
                       "preparation+instructions", "ఉపవాసం+సూచనలు", "उपवास+निर्देश"),
          playbooks.fix_prep_preview, slots=(), max_items=playbooks.MAX_PREP_FIXES, **_LAB),
    # Deterministic keyword rules (lab_classifier.py), not an LLM; fills only tests without a heading.
    # R2 as in registry §4.6; max_items is the endpoint's own reach (every unfiled test in one call).
    _desk("lab_tests.auto_classify", "File unfiled tests under a heading", "R2", "POST",
          "/admin/lab-tests/auto-classify", "labtests",
          ("auto classify", "classify+tests", "file+unfiled", "unfiled+tests", "category+daalo", "category+pettu",
           "category+mein", "వర్గం+పెట్టు", "श्रेणी+डालो"),
          playbooks.auto_classify_preview, slots=(), max_items=100000, **_LAB),
)

# ── KK-P4-10: PB-10 treatments (Treatments page) and PB-15 dental plans (Treatment Plans page) ──
# The same price words as the lab catalogue: on a tie the current page decides, else Kriya asks which.
_TRT = dict(permission="TREATMENTS_MANAGE", perm_source="dependency", feature=specialty_enabled)
_TREATMENT = SlotSpec("treatment", "treatment")
_PUT_TRT = ("PUT", "/admin/treatments/{treatment_id}")
TREATMENTS = (
    _desk("treatment.price", "Change a treatment price", "R2", *_PUT_TRT, "treatments",
          ("change+price", "set+price", "update+price", "price+to", "rate+to", "price+by", "rate+by",
           "increase+price", "reduce+price", "decrease+price", "raise+price", "price+rupees", "rate+rupees",
           "price+marchu", "rate+marchu", "price+badlo", "rate+badlo", "price+from", "ధర+మార్చు", "दाम+बदलो",
           "रेट+बदलो", "treatment+price", "treatment+rate"), playbooks.treatment_price_preview, slots=(_TREATMENT, SlotSpec("price", "price")), **_TRT),
    _desk("treatment.price_bulk", "Change many treatment prices", "R3", *_PUT_TRT, "treatments",
          ("all+treatments", "treatments+by", "anni+treatments", "sab+treatments", "sabhi+treatments", "treatments+daam",
           "treatments+badhao", "treatments+penchu",
           "అన్ని+ట్రీట్మెంట్స్", "सभी+ट्रीटमेंट"), playbooks.treatment_price_bulk_preview,
          slots=(SlotSpec("scope", "treatment_scope"), SlotSpec("price", "percent", label="Percent change, e.g. +10 or -5"),
                 SlotSpec("round", "round_to", False)), max_items=playbooks.MAX_TREATMENT_BULK, **_TRT),
    _desk("treatments.status", "Show or hide a treatment", "R2", "POST", "/admin/treatments/status", "treatments",
          ("hide", "unhide", "publish", "unpublish", "show+patients", "treatment+show", "treatment+chupao",
           "treatment+dachu", "ట్రీట్మెంట్+దాచు", "ट्रीटमेंट+छुपाओ"), playbooks.treatment_status_preview,
          slots=(_TREATMENT, SlotSpec("switch", "show_hide")), **_TRT),
    _desk("treatment.doctors", "Choose who does a treatment", "R2", "PUT", "/admin/treatments/{treatment_id}/doctors",
          "treatments", ("only+does", "only+do", "only+performs", "only+chestaru", "sirf+karti", "sirf+karte",
                         "sirf+karta"), playbooks.treatment_doctors_preview,
          slots=(_TREATMENT, SlotSpec("doctor", "doctor")), **_TRT),
)
# WhatsApp consent only from the yes/no form (never from a sentence), as patient registration (REG-03).
_DENT = dict(permission="DENTAL_PLANS_MANAGE", perm_source="dependency", feature=dental_plans_enabled)
_SITTING = (SlotSpec("doctor", "doctor", False), SlotSpec("date", "date", False), SlotSpec("time", "clock", False))
DENTAL = (
    _desk("dental.plan_create", "Start a dental treatment plan", "R2", "POST", "/admin/dental/plans", "dentalplans",
          ("treatment plan", "dental plan", "plan+sittings", "plan+sitting", "root canal+plan", "rct+plan",
           "braces+plan", "implant+plan", "plan+banao", "plan+cheyyi"), playbooks.dental_plan_preview,
          slots=(SlotSpec("name", "patient_for", label="Patient's name"), SlotSpec("phone", "phone"),
                 SlotSpec("treatment", "plan_treatment", label="Treatment"), SlotSpec("sittings", "sittings", False),
                 SlotSpec("quote", "amount", False), *_SITTING,
                 SlotSpec("whatsapp_consent", "yesno", label="Patient agrees to WhatsApp messages about their sittings")),
          **_DENT),
    _desk("dental.sitting_book", "Book the next dental sitting", "R2", "POST", "/admin/dental/plans/{plan_id}/sittings",
          "dentalplans", ("next sitting", "agli sitting", "agla sitting", "book+sitting", "sitting+book"),
          playbooks.dental_sitting_preview,
          slots=(SlotSpec("who", "sitting_patient", label="Patient's name"), SlotSpec("doctor", "doctor"),
                 SlotSpec("date", "date"), SlotSpec("time", "clock", label="Time, e.g. 10:30")), **_DENT),
)

# ── KK-P4-11: PB-11 doctor schedule change, doctor create, branch assignment ──
DOCTOR_ACTIONS = (
    # Full current shifts + the doctor's own duration are sent, never a branch field (SCH-02, D-34).
    _desk("doctor.schedule", "Change a doctor's hours", "R2", "PUT", "/admin/doctors/{doctor_id}", "doctors",
          ("morning+is now", "evening+is now", "morning+hours", "evening+hours", "morning+timings",
           "evening+timings", "morning+timing", "evening+timing", "dr+timings", "dr+hours", "ఉదయం+సమయం",
           "సాయంత్రం+సమయం", "सुबह+समय", "शाम+समय"), playbooks.doctor_schedule_preview,
          slots=(SlotSpec("doctor", "doctor"), SlotSpec("session", "session", label="Morning or evening"),
                 SlotSpec("window", "session_window", label="Hours, e.g. 5 to 8")),
          permission="DOCTORS_UPDATE", perm_source="dependency"),
    # Specialization and department are always asked on the form (never guessed from the sentence).
    _desk("doctor.create", "Add a doctor", "R2", "POST", "/admin/doctors", "doctors",
          # "create" phrases only as written: "create+doctor" would also catch "create leave for doctor Rao".
          ("new doctor", "add+new doctor", "add+dr+mornings", "add+dr+evenings", "add+dr+weekdays",
           "add+dr+mon", "create a doctor", "create the doctor", "create dr", "create new dr",
           # contiguous only: "register Ravi for Dr Rao" is a patient, "add leave for doctor Rao" a leave
           "create doctor", "add doctor", "add a doctor", "add dr", "add a dr", "onboard doctor", "onboard dr",
           "onboard a doctor", "register doctor", "register a doctor", "register the doctor", "register dr",
           "register a dr", "doctor ni register", "doctor ko register", "dr ni register", "dr ko register",
           "doctor+add cheyyi", "doctor+jodo", "డాక్టర్+జోడించు", "डॉक्टर+जोड़ो", "డాక్టర్ని రిజిస్టర్",
           "డాక్టర్ ని రిజిస్టర్", "డాక్టర్ను రిజిస్టర్", "डॉक्टर को रजिस्टर", "doctor registration", "dr registration"),
          playbooks.doctor_create_preview,
          slots=(SlotSpec("name", "new_doctor", label="Doctor's name, e.g. Dr Kiran Kumar"),
                 SlotSpec("specialization", "text_field", label="Specialization, e.g. Dermatology"),
                 SlotSpec("department", "text_field", label="Department"),
                 SlotSpec("fee", "price", label="Consultation fee in rupees"),
                 SlotSpec("days", "days", label="Working days, e.g. Mon-Sat"),
                 SlotSpec("morning", "morning_window", False, "Morning hours, e.g. 9 to 1 (blank if none)"),
                 SlotSpec("evening", "evening_window", False, "Evening hours, e.g. 5 to 8 (blank if none)"),
                 # Offered as a pick on the form only at a clinic with several branches (assistant._offer_branch).
                 SlotSpec("branch", "branch", False, "Branch")),
          permission="DOCTORS_CREATE", perm_source="dependency", one_of=("morning", "evening"),
          # "add dr Rao leave tomorrow" is a leave; "doctor registration number" an edit of an existing doctor
          unless=("leave", "leaves", "chutti", "selavu", "సెలవు", "छुट्टी", "holiday", "registration number",
                  "registration no", "reg number", "reg no")),
    _desk("branch.doctor_assign", "Assign a doctor to a branch", "R2", "POST", "/admin/branches/{branch_id}/doctors",
          "branches", ("assign+branch", "బ్రాంచ్+అసైన్", "ब्रांच+असाइन"), playbooks.branch_assign_preview,
          slots=(SlotSpec("doctor", "doctor"), SlotSpec("branch", "branch", label="Branch"),
                 SlotSpec("session", "assign_session", False)),
          permission="DOCTOR_BRANCH_ASSIGN", perm_source="dependency"),
)

# ── KK-P4-12: H tier — a person presses the page's own button (KK-FR-025). Nothing here is executable. ──

def _h(id: str, summary: str, page: str, button: str, triggers: tuple, note: str, *, fields=(), slots=(),
       feature=None, unless=()) -> ActionSpec:
    return ActionSpec(id=id, module=id.split(".")[0], summary=summary, risk="H", page=page, triggers=triggers,
                      slots=slots, feature=feature, unless=unless,
                      handoff=Handoff(page=page, fields=fields, button=button, note=note))


_ROW = "Use the buttons on its row in the list."
HANDOFF_ACTIONS = (
    _h("staff.create", "Create a staff login", "staff", "Create Staff Login",
       ("add+staff", "new staff", "staff+login", "create+login", "staff account", "naya staff", "register staff",
        "register a staff", "register nurse", "register a nurse", "add nurse", "add a nurse", "new nurse",
        "register receptionist", "add receptionist", "new receptionist", "register cashier", "add cashier",
        "స్టాఫ్+లాగిన్", "स्टाफ+लॉगिन"),
       "Choose the username and password yourself, check the details, then press Create Staff Login.",
       # Role first: choosing it shows or hides other fields (the name is only shown for some roles).
       fields=(("f-staffRole", "role"), ("f-staffFullName", "full_name")),
       slots=(SlotSpec("full_name", "staff_name", False, "Name"), SlotSpec("role", "staff_role", False, "Role"))),
    _h("staff.manage", "Change a staff login", "staff", "",
       ("reset+password", "change+password", "password+reset", "staff+permissions", "deactivate+staff",
        "delete+staff", "remove+staff", "disable+staff"),
       "Passwords, permissions and removal are done by a person. " + _ROW),
    _h("payment.settings", "Payment settings", "paysettings", "Save Payment Settings",
       ("payment settings", "razorpay", "phonepe", "upi id", "payment+key", "gateway+key"),
       "Enter the keys yourself on the page; Kriya never handles them."),
    _h("opd.record_payment", "Record a payment", "opdbilling", "Record Payment",
       ("record+payment", "cash+payment", "record+cash", "collect+cash", "payment+received"),
       "Cash is counted and recorded by a person.", feature=_opd_live),
    _h("opd.refund", "Refund a payment", "opdbilling", "Refund Payment",
       ("refund+invoice", "refund+bill", "refund+payment", "refund+receipt"),
       "Refunds are done by a person.", feature=_opd_live),
    _h("opd.void", "Void an invoice", "opdbilling", "Void Invoice",
       ("void+invoice", "void+bill", "cancel+invoice", "cancel+bill"),
       "Voids are done by a person.", feature=_opd_live),
    _h("visit.sign", "Sign a visit", "opdworkspace", "Finalise Visit",
       ("sign+prescription", "sign+visit", "finalise+visit", "finalize+visit", "sign+rx"),
       "Only the doctor signs; Kriya never writes clinical content.", feature=_opd_live),
    _h("doctor.delete", "Remove a doctor", "doctors", "", ("delete+doctor", "remove+doctor"),
       "Removing a doctor is done by a person. " + _ROW),
    _h("lab_test.delete", "Delete a lab test", "labtests", "", ("delete+test", "remove+test", "delete+lab test"),
       "Deleting is done by a person (Kriya can deactivate a test instead). " + _ROW),
    _h("treatment.delete", "Delete a treatment", "treatments", "", ("delete+treatment", "remove+treatment"),
       "Deleting is done by a person (Kriya can hide a treatment instead). " + _ROW),
    _h("branch.manage", "Add or change a branch", "branches", "Save Branch",
       ("add+branch", "new branch", "create+branch", "register branch", "register a branch", "delete+branch", "rename+branch", "edit+branch"),
       "Branches are what patients see on WhatsApp, so a person saves them."),
    _h("data.export", "Export data", "data", "Export data",
       ("export+data", "export+patients", "download+data", "export+patient"), "Exports are downloaded by a person."),
    _h("data.import", "Import patients", "data", "Import patients",
       ("import+patients", "upload+patients", "patients+excel", "import+excel"),
       "Imports are checked and started by a person."),
    _h("corporate.manage", "Corporate health", "corporate", "New company",
       ("corporate+company", "add+company", "corporate+client", "corporate health", "register company",
        "register a company"), "Done by a person on the page."),
    _h("voice.takeover", "Take over a live call", "voice", "Live",
       ("take over+call", "takeover+call", "live call+take", "pause+call"),
       "A live patient call is taken over by a person.", feature=ai_receptionist_enabled),
)

# ── KK-P4-13: voice receptionist knowledge, lexicon, learning review (AI Receptionist page) ──
# voice.settings_update was held for D-13 and is released with its fix (config_store + migration 110).
# Caller phrases come from quotes only.
_PHRASE = SlotSpec("phrase", "quoted", label="The phrase, exactly as callers say it")
VOICE_ADMIN = (
    _desk("voice.knowledge_add", "Teach the receptionist an answer", "R2", "POST", "/admin/voice/knowledge", "voice",
          ("teach+receptionist", "receptionist+should say", "receptionist+answer+add", "receptionist ki nerpu",
           "receptionist ki nerpinchu", "receptionist ko sikhao", "receptionist ko batao", "రిసెప్షనిస్ట్+నేర్పు",
           "रिसेप्शनिस्ट+सिखाओ"), playbooks.knowledge_add_preview,
          slots=(SlotSpec("question", "verbatim", label="What callers ask"),
                 SlotSpec("answer", "verbatim", label="What the receptionist should say")), **_VOICE_ON),
    _desk("voice.knowledge_delete", "Remove a receptionist answer", "R2", "DELETE", "/admin/voice/knowledge/{entry_id}",
          "voice", ("remove+answer", "delete+answer", "remove+answer+about", "delete+answer+about", "answer+teeseyi",
                    "answer+hatao", "జవాబు+తొలగించు", "जवाब+हटाओ"), playbooks.knowledge_delete_preview,
          slots=(SlotSpec("topic", "about", label="A word from the question"),), **_VOICE_ON),
    _desk("voice.lexicon_add", "Teach the receptionist a phrase", "R2", "POST", "/admin/voice/lexicon", "voice",
          ("callers say", "patients say", "people say", "callers call", "patients call", "callers+antaru",
           "patients+antaru", "log+kehte", "patients+kehte", "callers+kehte", "=అంటారు", "कहते हैं"),
          playbooks.lexicon_add_preview,
          slots=(_PHRASE, SlotSpec("target", "lex_target", label="What it means: a doctor, test or department")),
          **_VOICE_ON),
    _desk("voice.lexicon_delete", "Remove a receptionist phrase", "R2", "DELETE", "/admin/voice/lexicon/{entry_id}",
          "voice", ("remove+alias", "delete+alias", "remove+nickname", "delete+nickname", "remove+phrase",
                    "delete+phrase", "remove+the phrase", "delete+the phrase", "remove+the alias", "delete+the alias", "alias+teeseyi", "alias+hatao", "phrase+teeseyi", "phrase+hatao",
                    "మారుపేరు+తొలగించు", "उपनाम+हटाओ"), playbooks.lexicon_delete_preview, slots=(_PHRASE,),
          **_VOICE_ON),
    # Phrase, kind, count and match only: the callers' sample utterances are never shown or stored.
    _read("voice.learning_review", "What the receptionist learned", "/admin/voice/learning/suggestions", "voice",
          ("receptionist+learn", "receptionist+learned", "learning suggestions", "receptionist+suggestions",
           "receptionist+nerchukundi", "emi nerchukundi", "kya seekha", "receptionist ne+seekha", "నేర్చుకుంది",
           "सीखा"),
          lambda d, p: present.table("Phrases waiting for review", "voice", (
              ("Callers said", "patient_phrase", "text"), ("Type", "category", "label"),
              ("Times heard", "evidence_count", "num"), ("Suggested match", "proposed_canonical", "text")),
              _rows(d, "suggestions")),
          permission=_VOICE, perm_source=_HANDLER, feature=ai_receptionist_enabled),
    _desk("voice.learning_approve", "Approve a learned phrase", "R2", "POST",
          "/admin/voice/learning/suggestions/{suggestion_id}/approve", "voice",
          ("approve+suggestion", "approve+learning", "approve+phrase", "suggestion+ok cheyyi",
           "suggestion+approve cheyyi", "suggestion+approve karo", "suggestion+manzoor", "సూచన+ఆమోదించు",
           "सुझाव+मंजूर"), playbooks.learning_approve_preview, slots=(_PHRASE,), **_VOICE_ON),
    # One setting per request; the pitch, voice, concurrency and budget stay on the page / platform.
    _desk("voice.settings_update", "Change a receptionist setting", "R2", "PUT", "/admin/voice/settings", "voice",
          ("rename+receptionist", "receptionist+name", "receptionist+language", "receptionist+speak",
           "receptionist+pace", "receptionist+speed", "receptionist+hours", "reception hours", "receptionist+timings",
           "receptionist+settings", "emergency number", "lead calls+between", "lead calls+a day", "lead call+window",
           "call window", "lead call+limit", "call limit", "calls a day", "each lead", "every lead",
           "receptionist ka naam", "receptionist+peru", "receptionist+bhasha", "receptionist+samayam",
           "రిసెప్షనిస్ట్+పేరు", "रिसेप्शनिस्ट+नाम"), playbooks.voice_settings_preview,
          slots=(SlotSpec("setting", "voice_setting",
                          label='Which setting and its new value, e.g. "lead call limit 50"'),), **_VOICE_ON),
    _desk("voice.learning_dismiss", "Dismiss a learned phrase", "R2", "POST",
          "/admin/voice/learning/suggestions/{suggestion_id}/dismiss", "voice",
          ("dismiss+suggestion", "reject+suggestion", "ignore+suggestion", "dismiss+learning", "dismiss+phrase",
           "suggestion+teeseyi", "suggestion+hatao", "suggestion+nahi chahiye",
           "సూచన+తిరస్కరించు", "सुझाव+खारिज"), playbooks.learning_dismiss_preview, slots=(_PHRASE,), **_VOICE_ON),
)

# ── KK-P4-14: PB-12 / PB-13 briefings on demand. Many GETs, each filtered by its own route (no endpoint here). ──
# The L4a scheduled briefing is not built: notifications are clinic-wide (status.md §5 #17).
BRIEFINGS = (
    ActionSpec(id="briefing.morning", module="briefing", summary="Morning briefing", risk="R0",
               triggers=("good morning", "morning briefing", "today's briefing", "daily briefing", "brief me",
                         "what's on today", "ee roju emundi", "ee roju em undi", "aaj kya hai", "aaj ka briefing",
                         "ఈరోజు ఏముంది", "आज क्या है"),
               call=playbooks.briefing_morning_call, present=playbooks.briefing_morning_present),
    ActionSpec(id="briefing.eod", module="briefing", summary="End of day", risk="R0",
               triggers=("end of day", "wrap up the day", "wrap up", "day summary", "close the day", "eod summary",
                         "roju mugimpu", "ee roju summary", "din khatam", "aaj ka summary", "రోజు ముగింపు",
                         "दिन का सारांश"),
               call=playbooks.briefing_eod_call, present=playbooks.briefing_eod_present),
)

# ── KK-P4-15: OPD billing (live OPD), online-booking refund, home collection assignment ──
# booking.confirm / booking.reject were built in KK-P4-05. Cash, OPD refunds and voids stay H (KK-P4-12).
_BILL = dict(permission="OPD_BILLING", also=("OPD_ADMIN",), perm_source=_HANDLER, feature=_opd_live)
_HCM = dict(permission="HOME_COLLECTION_MANAGE", perm_source=_HANDLER, feature=home_collection_available)
BILLING = (
    _desk("opd.invoice_create", "Make a visit's bill", "R2", "POST", "/admin/opd/appointments/{appointment_id}/invoice",
          "opdbilling", ("make+bill", "create+bill", "generate+bill", "prepare+bill", "create+invoice", "make+invoice",
                         "bill+cheyyi", "bill+veyyi", "bill+banao", "బిల్లు+చేయి", "बिल+बनाओ"),
          playbooks.invoice_create_preview,
          **{**_BILL, "also": ("OPD_FRONT_DESK", "OPD_ADMIN")}),  # the route also lets the front desk make the draft
    _desk("opd.invoice_issue", "Issue an invoice", "R2", "POST", "/admin/opd/invoices/{invoice_id}/issue", "opdbilling",
          ("issue+invoice", "issue+bill", "finalise+bill", "finalize+bill", "finalise+invoice", "finalize+invoice",
           "invoice+issue cheyyi", "bill+issue cheyyi", "bill+issue karo", "invoice+issue karo", "బిల్లు+జారీ",
           "बिल+जारी"), playbooks.invoice_issue_preview, **_BILL),
    _desk("opd.payment_link", "Send a payment link", "R2", "POST", "/admin/opd/invoices/{invoice_id}/payment-link",
          "opdbilling", ("payment link", "pay link", "payment+link", "link+to pay", "payment link+pampu",
                         "payment link+pampandi", "payment link+bhejo", "payment link+bhej do", "పేమెంట్+లింక్",
                         "पेमेंट+लिंक"), playbooks.payment_link_preview, **_BILL),
    # R3 with the typed amount (rupees); the route is require_admin, so staff are refused up front.
    _desk("booking.refund", "Refund an online booking", "R3", "POST", "/admin/bookings/{booking_id}/refund", "payments",
          ("refund+booking", "refund+appointment", "refund+online", "booking+refund cheyyi", "booking+refund karo",
           "booking+refund ivvandi", "booking+refund do", "బుకింగ్+రీఫండ్", "बुकिंग+रिफंड"),
          playbooks.booking_refund_preview, perm_source="role", roles=ADMIN_ROLES,
          max_items=playbooks.MAX_REFUND_RUPEES),
    _desk("homecollect.assign", "Assign a home visit", "R2", "POST", "/admin/home-collection/visits/{visit_id}/assign",
          "homecollect", ("assign+home visit", "assign+visit", "assign+collection", "assign+pickup",
                          "visit+assign cheyyi", "visit+assign karo", "విజిట్+అసైన్", "विजिट+असाइन"),
          playbooks.homecollect_assign_preview,
          slots=(SlotSpec("who", "visit_patient", label="Patient's name"),
                 SlotSpec("phleb", "phleb_name", label="Phlebotomist"), SlotSpec("date", "date", False)), **_HCM),
    _desk("homecollect.auto_assign", "Auto-assign home visits", "R2", "POST", "/admin/home-collection/auto-assign",
          "homecollect", ("auto assign", "auto assign+visit", "auto assign+home visit", "assign+all+visits",
                          "assign+unassigned", "auto assign cheyyi", "auto assign karo", "ఆటో+అసైన్", "ऑटो+असाइन"),
          playbooks.homecollect_auto_assign_preview, slots=(), max_items=200, **_HCM),
)

# ── IPD OS (migration 111): reads + hand-offs only (docs/ipd-os/KRIYA-KETU.md, ADR-IPD-08) ──
# No IPD write is executable through Ketu in v1: bed allocation, clinical records
# and billing stay on the page where a person presses its own button.

def _ipd_on(clinic: dict) -> bool:  # _ipd_scope(): module ACTIVE or WIND_DOWN
    return ipd_access(clinic) is not None


_IPD_ANY = dict(permission="IPD_ADMISSIONS", also=("IPD_NURSING", "IPD_CLINICAL", "IPD_BILLING", "IPD_ADMIN"),
                perm_source=_HANDLER, feature=_ipd_on)
_IPD_LIST = (("Admission", "admission_number", "text"), ("Patient", "patient_name", "text"),
             ("MRN", "mrn", "text"))
IPD_ACTIONS = (
    _read("ipd.census", "Bed occupancy", "/admin/ipd/census", "ipdboard",
          ("bed occupancy", "occupancy", "free beds", "beds free", "empty beds", "how many beds", "ipd census",
           "inpatient count", "khali beds", "beds khali", "enni beds khali", "ఖాళీ బెడ్లు", "खाली बेड"),
          lambda d, p: present.metrics("Beds right now", "ipdboard", d, (
              ("Occupancy", "occupancy_pct", "pct"), ("Inpatients", "inpatients", "num"),
              ("Free beds", "beds.available", "num"), ("Occupied", "beds.occupied", "num"),
              ("Cleaning", "beds.cleaning", "num"), ("Waiting for a bed", "awaiting_bed", "num"),
              ("Discharge in progress", "discharge_pending", "num"), ("Admitted today", "admitted_today", "num"),
              ("Discharged today", "discharged_today", "num"))),
          **_IPD_ANY),
    _read("ipd.inpatients", "Inpatients", "/admin/ipd/admissions", "ipdchart",
          ("inpatient list", "inpatients", "admitted patients", "who is admitted", "list inpatients", "ward patients",
           "ipd patients", "admit ayina patients", "bharti mareez", "भर्ती मरीज"),
          lambda d, p: present.table("Inpatients", "ipdchart", (
              ("Bed", "bed", "text"), *_IPD_LIST, ("Attending", "attending_doctor", "text"),
              ("Status", "status", "label")), _rows(d, "admissions")),
          query=lambda p: {"status": "admitted,discharge_pending", "limit": 100}, **_IPD_ANY),
    _read("ipd.waiting", "Waiting for a bed", "/admin/ipd/worklist", "ipdadmit",
          ("waiting for a bed", "waiting for bed", "bed waiting list", "pending admissions", "admission queue",
           "bed kosam wait", "bed ka intezaar", "బెడ్ కోసం", "बेड का इंतज़ार"),
          lambda d, p: present.table("Waiting for a bed", "ipdadmit", (*_IPD_LIST, ("Priority", "priority", "label")),
                                     _rows(d, "awaiting_bed")),
          **_IPD_ANY),
    _read("ipd.discharges", "Discharges in progress", "/admin/ipd/worklist", "ipdchart",
          ("pending discharges", "discharges today", "discharge list", "who is being discharged",
           "discharge pending", "discharge evaru", "kiska discharge", "డిశ్చార్జ్", "डिस्चार्ज"),
          lambda d, p: present.table("Discharges in progress", "ipdchart", (
              *_IPD_LIST, ("Type", "discharge_type", "label")), _rows(d, "discharge_pending")),
          **_IPD_ANY),
    _h("ipd.admit", "Admit a patient", "ipdadmit", "Admit patient",
       ("admit+patient", "new admission", "ipd+admit", "admit+bed", "bharti+karo", "admit", "bharti"),
       "Pick the patient from the registry and a free bed, then press Admit patient. The bed is "
       "confirmed by the server when you press it.", feature=_ipd_on),
    _h("ipd.discharge", "Discharge a patient", "ipdchart", "",
       ("discharge+patient", "complete+discharge", "release+bed", "discharge+summary", "discharge",
        "discharge karo", "discharge cheyyi"),
       "Open the patient's chart, Discharge tab: the attending doctor decides and signs; the checklist and "
       "Complete discharge button are there.",
       # one patient at a time: a bulk discharge is not offered at all
       feature=_ipd_on, unless=("discharge all", "discharge everyone", "all patients", "sab ko discharge",
                                "andarini discharge")),
    _h("ipd.vitals", "Record vitals or notes", "ipdchart", "",
       ("record+vitals", "enter+vitals", "nursing+note", "intake+output", "give+medicine", "medication+given"),
       "Open the patient's chart; clinical entries are made and signed by staff, never by Kriya.",
       feature=_ipd_on),
    _h("ipd.transfer", "Move a patient to another bed", "ipdchart", "",
       ("transfer", "shift+bed", "change+bed", "move+bed", "bed+change", "bed+marchu", "bed+badlo"),
       "Open the patient's chart and press Transfer bed: the server confirms the new bed is still free.",
       feature=_ipd_on),
)

#: action -> the action that does the same job where the first is switched off for the clinic (KK-FR-061).
VARIANTS: dict[str, str] = {"appointment.check_in": "opd.arrive"}

REGISTRY: dict[str, ActionSpec] = _build(
    ActionSpec(id="nav.open", module="nav", summary="Open a page", risk="R0"),       # client go(page)
    ActionSpec(id="help.howto", module="help", summary="How do I…", risk="R0"),      # static, app/assistant/help.py
    *READS,
    *LEAVES,
    *DESK,
    *SENDS,
    HOLIDAY,
    *LEAD_CALLS,
    *OPD_DESK,
    *CATALOGUE,
    *CLEANUP,
    *TREATMENTS,
    *DENTAL,
    *DOCTOR_ACTIONS,
    *HANDOFF_ACTIONS,
    *VOICE_ADMIN,
    *BRIEFINGS,
    *BILLING,
    *IPD_ACTIONS,
)
