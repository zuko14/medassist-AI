"""Kriya Ketu slot resolvers (action-registry-spec.md §3). Pure, zero tokens.

Reuses the voice receptionist's resolvers read-only: dates.parse_date (IST,
te/hi/en relative dates) and lexicon.match_doctors (several results on an
ambiguous name, so the caller asks instead of guessing).
"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

from app.voice.dates import _MONTHS, parse_date, parse_time_period
from app.voice.lexicon import match_doctors

_NUM = r"(-?\d[\d,]*(?:\.\d+)?)"
_PERCENT = re.compile(_NUM + r"\s*(?:%|per\s?cent\b|pc\b|శాతం|प्रतिशत|फीसदी|taka\b)", re.I)
_RUPEES_BEFORE = re.compile(r"(?:₹|\brs\b\.?|\binr\b)\s*" + _NUM, re.I)
_RUPEES_AFTER = re.compile(_NUM + r"\s*(?:rupees?\b|rs\b|రూపాయలు|రూపాయిలు|रुपये|रुपए|रुपया)", re.I)


# "14 to 16 Oct", "from 14 oct till 16 oct", "20 se 22 oct tak", "20 nundi 22 oct varaku".
_RANGE_SEP = re.compile(r"\s(?:to|till|until|upto|up to|se|nundi|से|నుండి|-|–)\s")
_LAST_DAY = re.compile(r"(\d{1,2})\D*$")
_LEAVE_TYPE = {"MORNING": "half_morning", "AFTERNOON": "half_evening", "EVENING": "half_evening"}


# D-29: a fully written date is taken literally. parse_date rolls a past day forward (right for a
# patient booking) and ignored an explicit year ("1 oct 2026" -> 2027-10-01) and misread ISO dates.
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_DAY_MONTH_YEAR = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+),?\s+(\d{4})(?!\d)", re.I)
_MONTH_DAY_YEAR = re.compile(r"(?<![a-z])([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)", re.I)
_NUMERIC_DMY = re.compile(r"(?<![\d/.])(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?!\d)")  # Indian order


def _literal_date(text: str):
    """(date or None, found): a fully written date, if the text has one. An impossible one
    ("2026-02-30") is found but has no date, so the caller asks instead of guessing."""
    t = text or ""
    for rx, order in ((_ISO, "ymd"), (_NUMERIC_DMY, "dmy"), (_DAY_MONTH_YEAR, "dMy"), (_MONTH_DAY_YEAR, "Mdy")):
        m = rx.search(t)
        if not m:
            continue
        parts = dict(zip(order, m.groups()))
        month = _MONTHS.get(parts["M"].lower()) if "M" in parts else int(parts["m"])
        if month is None:
            continue  # "on 20, 2027" etc.: not a month name
        try:
            return date(int(parts["y"]), month, int(parts["d"])), True
        except ValueError:
            return None, True
    return None, False


def _date_in(text: str, now: Optional[datetime]) -> Optional[date]:
    d, found = _literal_date(text)
    return d if found else parse_date(text, now=now)


def resolve_range(text: str, now: Optional[datetime] = None) -> Optional[tuple[date, date]]:
    """(start, end) when the text states a range of at least two days; None otherwise.
    A bare day before the separator takes the end's month and year ("14 to 16 Oct")."""
    for m in _RANGE_SEP.finditer(text or ""):
        end = _date_in(text[m.end():], now)
        if end is None:
            continue
        left = text[:m.start()]
        start = _date_in(left, now)
        if start is None and (d := _LAST_DAY.search(left)):
            try:
                start = end.replace(day=int(d.group(1)))
            except ValueError:
                start = None
        if start is not None and start < end:
            return start, end
    return None


def resolve_date(text: str, now: Optional[datetime] = None) -> Optional[date]:
    """The date the text names; for a range, its first day."""
    found = resolve_range(text, now=now)
    return found[0] if found else _date_in(text, now)


def resolve_leave_type(text: str) -> Optional[str]:
    """half_morning / half_evening when a session is named; None means a full day."""
    return _LEAVE_TYPE.get(parse_time_period(text) or "")


def resolve_amount(text: str) -> Optional[tuple[str, Decimal]]:
    """("percent", n) or ("amount", rupees) when the text states one; None otherwise.
    A bare number is not money (it could be a count or a test code); <= 0 is refused."""
    for kind, rx in (("percent", _PERCENT), ("amount", _RUPEES_BEFORE), ("amount", _RUPEES_AFTER)):
        m = rx.search(text or "")
        if m:
            try:
                value = Decimal(m.group(1).replace(",", ""))
            except InvalidOperation:
                return None
            return (kind, value) if value > 0 else None
    return None


_DIGITS = re.compile(r"\d{4,}")
# The search term follows the last of these words ("find patient Ravi", "patient named Sita", "patient dhundo Ravi").
_TERM_MARK = re.compile(r"(?:^|\s)(?:patients?|named|called|vetuku|dhundo|ढूंढो|వెతుకు)(?=\s|$)")


def resolve_term(text: str) -> Optional[str]:
    """A patient search term: the first run of >= 4 digits (part of a phone), else the words after the
    last "patient" / "named" / "dhundo" / "vetuku". Lower-cased, 2..40 chars; None when there is none."""
    t = (text or "").strip().lower()
    m = _DIGITS.search(t)
    if m:
        return m.group(0)
    marks = list(_TERM_MARK.finditer(t))
    term = re.sub(r"\s+", " ", t[marks[-1].end():]).strip(" .?!,") if marks else ""
    return term if 2 <= len(term) <= 40 else None


# A booking ref as generate_booking_ref makes it: PREFIX-YEAR-BODY ("MC-2026-ABCD2345").
BOOKING_REF = re.compile(r"\b([a-z]{1,6}-\d{4}-[a-z0-9]{6,10})\b", re.I)
# Words around a patient's name in a desk instruction; what is left is the name.
_BOOKING_NOISE = {
    "check", "in", "checkin", "arrived", "has", "have", "is", "are", "came", "come", "here", "cancel", "confirm",
    "reject", "decline", "approve", "booking", "bookings", "appointment", "appointments", "appt", "patient",
    "patients", "the", "a", "an", "for", "of", "please", "pls", "now", "today", "tomorrow", "aaj", "kal", "ee",
    "roju", "repu", "ka", "ki", "ke", "ko", "ni", "nu", "karo", "kar", "kardo", "do", "cheyyi", "cheyandi", "chey",
    "cheyyandi", "raddu", "radd", "payment", "paid", "his", "her", "their", "mr", "mrs", "ms", "with", "and", "to",
    "my", "this", "that", "vachharu", "vacharu", "vachindi", "aa", "gaye", "gaya", "gayi", "hai", "hain",
    "pending", "review", "wala", "wali", "vala", "gari", "garu",
    # reports and messages (KK-P4-03)
    "report", "reports", "resend", "send", "again", "lab", "pdf", "whatsapp", "message", "msg", "malli", "pampu",
    "pampandi", "dobara", "bhejo", "bhej", "phir", "se", "tell", "text", "delivery", "deliveries", "lead",
    "leads", "new", "all", "everyone", "every", "contacts", "contact",  # never a name: one recipient per run (T-19)
    # OPD billing and refunds (KK-P4-15). shortcut: a patient literally named "Bill" needs the ref or phone.
    "make", "bill", "bills", "invoice", "invoices", "issue", "link", "pay", "refund", "generate", "create",
    "finalise", "finalize", "banao", "online", "money"}
# A message is only ever the staff member's own words: inside quotes, or after the first colon.
_QUOTED = re.compile(r"[\"“”]([^\"“”]+)[\"“”]")
_CONTENT_START = re.compile(r"\s(?:that|saying|to say|ani|ki)\s", re.I)


def resolve_booking(text: str) -> Optional[str]:
    """Which booking a desk instruction names: its ref (upper-cased), else the first run of >= 4 digits
    (part of a phone), else the Latin-script words left once the instruction words are removed
    (lower-cased, 2..40 chars). None when nothing names a booking ("check in the patient").
    shortcut: names in Telugu/Devanagari script are not extracted (None -> the form asks); add with G-KK-ASR."""
    t = (text or "").replace("’", "'")
    m = BOOKING_REF.search(t)
    if m:
        return m.group(1).upper()
    low = t.lower()
    m = _DIGITS.search(low)
    if m:
        return m.group(0)
    words = [w for w in re.findall(r"[a-z]+", re.sub(r"'s\b", "", low)) if w not in _BOOKING_NOISE]
    name = " ".join(words)
    return name if 2 <= len(name) <= 40 else None


def _message_span(text: str, colon: bool = True) -> Optional[tuple[int, int]]:
    t = text or ""
    m = _QUOTED.search(t)
    if m:
        return m.start(), m.end()
    i = t.find(":") if colon else -1
    return (i, len(t)) if i >= 0 else None


def without_verbatim(text: str, colon: bool) -> str:
    """The instruction minus the words it carries verbatim (quoted, or after the colon when `colon`)."""
    span = _message_span(text, colon)
    return text[:span[0]] + " " + text[span[1]:] if span else text


def resolve_message(text: str) -> Optional[str]:
    """The text to send, verbatim: inside quotes or after the first colon; 1..1000 chars, else None.
    Kriya never composes patient-facing text from an instruction ("tell Ravi that ...")."""
    span = _message_span(text)
    if span is None:
        return None
    body = text[span[0]:span[1]].strip().strip("\"“”").lstrip(":").strip()
    return body if 1 <= len(body) <= 1000 else None


def resolve_lead(text: str) -> Optional[str]:
    """Who a message or report instruction names (as resolve_booking), ignoring the message itself."""
    span = _message_span(text)
    if span:
        return resolve_booking(text[:span[0]] + " " + text[span[1]:])
    # "message Ravi that the doctor is late": the name ends where the (unsendable) content begins.
    return resolve_booking(_CONTENT_START.split(text, maxsplit=1)[0])


_FOR = re.compile(r"\bfor\s+(.+)$", re.I)


def resolve_holiday_name(text: str) -> Optional[str]:
    """The holiday's name as staff said it: quoted, after a colon, or after "for" ("... for Diwali");
    never a date ("holiday for 2 nov"). 1..100 chars (the column is VARCHAR(100)); None = no name given."""
    name = resolve_message(text)
    if name is None and (m := _FOR.search(text or "")):
        name = m.group(1).strip(" .!?,")
        if parse_date(name) is not None:
            name = None
    return name if name and len(name) <= 100 else None


def resolve_doctor(text: str, doctors: list, tenant_entries: Iterable[dict] = ()) -> list:
    """Every doctor the text could mean: [] none, [one] exact, several = ask the user.
    The voice matcher ignores words under 4 letters (call noise), which misses short surnames
    ("Dr Rao", "Das", "Jha"); typed text gets an exact-word fallback for those (still never a guess:
    two doctors called Rao are both returned)."""
    found = match_doctors(text, doctors, tenant_entries)
    if found:
        return found
    words = set(re.findall(r"[a-z]{3,}", (text or "").lower())) - _NAME_NOISE
    return [d for d in doctors if words & set(re.findall(r"[a-z]{3,}", str(d.get("name", "")).lower()))]


# ── KK-P4-06: lead calls (PB-03) and the automatic lead calls switch (PB-05) ──
_ON = re.compile(r"\b(?:turn on|switch on|enable|start|chalu|shuru|on|automatically)\b", re.I)
_OFF = re.compile(r"\b(?:turn off|switch off|disable|stop|band|aapu|aapandi|off)\b", re.I)
_LEAD_WORDS = _BOOKING_NOISE | {"call", "calls", "hot", "open", "from", "week", "month", "todays", "these", "those",
                                "them", "people", "cheyyi", "cheyandi", "karo", "bulao", "pilu", "pilavandi", "is",
                                "hafte", "vaaram", "ee", "last", "past", "days", "can", "you"}


def resolve_on_off(text: str) -> Optional[str]:
    """"on" / "off" when the text says exactly one of them; None when it says neither or both."""
    on, off = bool(_ON.search(text or "")), bool(_OFF.search(text or ""))
    return "on" if on and not off else "off" if off and not on else None


def resolve_segment(text: str) -> str:
    """Which leads PB-03 may call: "hot" when said, else "open". booked/dnc are never offered (WA-08)."""
    return "hot" if re.search(r"\bhot\b", text or "", re.I) else "open"


def resolve_lead_days(text: str) -> int:
    """How far back to look: today = 1 day, "month" = 30, otherwise (incl. "this week") 7."""
    t = (text or "").lower()
    if re.search(r"\b(?:today|today's|aaj|ee roju|ivala)\b", t):
        return 1
    return 30 if re.search(r"\bmonth\b", t) else 7


def resolve_interest_term(text: str) -> Optional[str]:
    """Words left once the lead-call words are removed ("dermatology"), or None. The preview must
    match them to a known interest label or refuse: an ignored filter would widen who is called."""
    words = [w for w in re.findall(r"[a-z]+", re.sub(r"'s\b", "", (text or "").lower().replace("’", "'")))
             if w not in _LEAD_WORDS]
    term = " ".join(words)
    return term if 2 <= len(term) <= 40 else None


# ── KK-P4-07: front desk registration, walk-in token, queue moves ──
_PERSON_NOISE = _BOOKING_NOISE | {"register", "registration", "new", "person", "phone", "mobile", "number", "no", "name",
                                  "family", "member", "as", "different", "walk", "token", "give", "ivvandi", "do",
                                  "dijiye", "visit", "followup", "follow", "up", "kotha", "naya", "nayi", "add"}
_DOCTOR_PART = re.compile(r"\s(?:for|with|to|ke liye|ki|ko)\s+(?:dr\b\.?|doctor\b)", re.I)
_PHONE_RUN = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?(\d(?:[\s-]?\d){9})(?!\d)")
_TOKEN = re.compile(r"(?:token|టోకెన్|टोकन)\s*(?:number|no\.?|#)?\s*(\d{1,4})", re.I)
_QUEUE_NOISE = _PERSON_NOISE | {"billing", "bill", "done", "completed", "complete", "finished", "finish", "waiting", "wait",
                                "queue", "vitals", "consultation", "recall", "move", "back", "again", "call", "mark",
                                "announce", "room", "pilu", "bulao"}
_STAGES = (("cancelled", r"\bcancel"), ("billing", r"\bbill"), ("completed", r"\b(?:done|complete|completed|finish|finished)\b"),
           ("vitals_pending", r"\bvitals?\b"), ("waiting", r"\b(?:waiting|wait|queue)\b"),
           ("in_consultation", r"\b(?:consultation|doctor room)\b"))


def _cased_words(text: str, noise: set) -> Optional[str]:
    """The words of `text` that are not instruction words, in their original casing; 2..60 chars."""
    words = [w for w in re.findall(r"[A-Za-z]+(?:'[a-z]+)?", re.sub(r"'s\b", "", (text or "").replace("’", "'")))
             if w.lower() not in noise]
    name = " ".join(words)
    return name if 2 <= len(name) <= 60 else None


def resolve_phone(text: str) -> Optional[str]:
    """A 10-digit Indian mobile written in the text ("98765 43210", "+91 98765-43210"), else None."""
    m = _PHONE_RUN.search(text or "")
    return re.sub(r"\D", "", m.group(1)) if m else None


def resolve_person_name(text: str) -> Optional[str]:
    """The patient's name as typed: the words before the phone number (or before "for Dr …"),
    without the instruction words. Casing kept, since it goes into the patient record."""
    t = text or ""
    m = _PHONE_RUN.search(t)
    if m:
        before = t[:m.start()]
        t = before if _cased_words(before, _PERSON_NOISE) else t
    return _cased_words(_DOCTOR_PART.split(t, maxsplit=1)[0], _PERSON_NOISE)


def resolve_queue_target(text: str) -> Optional[str]:
    """"#5" for a token number, else the patient's name (lower-cased) for a queue row."""
    m = _TOKEN.search(text or "")
    if m:
        return f"#{int(m.group(1))}"
    words = [w for w in re.findall(r"[a-z]+", re.sub(r"'s\b", "", (text or "").lower())) if w not in _QUEUE_NOISE]
    name = " ".join(words)
    return name if 2 <= len(name) <= 40 else None


def resolve_stage(text: str) -> Optional[str]:
    """The queue stage the text moves to; None for a cancellation (not a Kriya stage move) or none named."""
    t = (text or "").lower()
    found = next((stage for stage, rx in _STAGES if re.search(rx, t)), None)
    return None if found == "cancelled" else found


def resolve_new_person(text: str) -> Optional[str]:
    """"yes" only when staff said the patient is a different person from the phone's existing records."""
    return "yes" if re.search(r"\b(?:new|different|another|separate)\s+person\b", text or "", re.I) else None


def resolve_visit_type(text: str) -> str:
    t = (text or "").lower()
    return "followup" if re.search(r"follow[\s-]?up", t) else "review" if re.search(r"\breview\b", t) else "new"


_NAME_NOISE = {"doctor", "the", "for", "and", "leave", "today", "tomorrow", "from", "until", "till", "cancel",
               "remove", "delete", "add", "mark", "off", "not", "chutti", "selavu", "kal", "aaj", "repu"}


# ── KK-P4-08 lab catalogue: test names, prices, rounding, collection hours ──

_UP = re.compile(r"\b(?:increase|raise|hike|up|penchu|penchandi|badhao|badhaa|badha)\b|పెంచు|बढ़ा", re.I)
_DOWN = re.compile(r"\b(?:decrease|reduce|lower|cut|drop|discount|tagginchu|tagginchandi|kam|ghatao|ghata)\b"
                   r"|తగ్గించు|घटा|कम", re.I)
_TO_BARE = re.compile(r"\b(?:to|as)\s+(\d[\d,]*)(?![\d,.]*\s*(?:%|per\s?cent|pc\b))", re.I)
_BY_END = re.compile(r"\bby\s*$", re.I)
_TO_END = re.compile(r"\b(?:to|as)\s*$", re.I)
_ROUND = re.compile(r"\b(?:round(?:ed)?(?:\s+off)?(?:\s+to)?(?:\s+the)?(?:\s+nearest)?|nearest)\s*(?:₹|rs\.?\s*)?"
                    r"(10|5|1)\b", re.I)
_ALL = re.compile(r"\b(?:all|every|anni|sab|sabhi|saare|sare)\b|అన్ని|सभी|सब", re.I)
_LAB_NOISE = {
    "change", "set", "update", "edit", "make", "price", "prices", "rate", "rates", "cost", "costs", "dhara", "dharalu",
    "daam", "dam", "of", "to", "the", "a", "an", "test", "tests", "lab", "by", "as", "for", "at", "is", "new",
    "increase", "decrease", "raise", "reduce", "lower", "cut", "hike", "up", "drop", "discount", "rs", "inr",
    "rupee", "rupees", "percent", "per", "cent", "pc", "round", "rounded", "nearest", "off", "deactivate", "disable",
    "hide", "stop", "offering", "offer", "inactive", "band", "aapu", "aapandi", "karo", "kar", "do", "cheyyi",
    "cheyandi", "marchu", "marchandi", "badlo", "badal", "penchu", "penchandi", "tagginchu", "tagginchandi", "badhao",
    "kam", "ghatao", "ka", "ki", "ke", "ko", "ni", "nu", "please", "pls", "all", "every", "anni", "sab", "sabhi",
    "saare", "sare", "category", "catalogue", "catalog", "service", "services", "only", "from", "now"}
_AMOUNTS = re.compile(r"(?:₹|\brs\b\.?|\binr\b)\s*[\d,.]+|[\d,.]+\s*(?:%|per\s?cent\b|pc\b|rupees?\b|rs\b)|\d[\d,.]*",
                      re.I)
_CLOCK = r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?"
_WINDOW = re.compile(r"(?<![\d:])" + _CLOCK + r"\s*(?:to|-|–|till|until|se|nundi)\s*" + _CLOCK + r"(?![\d:])", re.I)


def _num(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _direction(text: str) -> int:
    up, down = bool(_UP.search(text)), bool(_DOWN.search(text))
    return 1 if up and not down else -1 if down and not up else 0


def resolve_price(text: str) -> Optional[list]:
    """A price change, JSON-safe: ["amount", "350"] (set to), ["delta", "-100"] (by rupees) or
    ["percent", "10"] (signed). A percent needs a direction word ("increase"/"reduce" ...); a bare number
    counts only right after "to"/"as" ("price to 350"); <= 0 is refused (None)."""
    t = text or ""
    found = resolve_amount(t)
    sign = _direction(t)
    if found and found[0] == "percent":
        return ["percent", _num(found[1] * sign)] if sign else None
    if found:
        m = _RUPEES_BEFORE.search(t) or _RUPEES_AFTER.search(t)
        before = t[:m.start()]
        if _BY_END.search(before) or (sign and not _TO_END.search(before)):
            return ["delta", _num(found[1] * (sign or 1))]
        return ["amount", _num(found[1])]
    m = _TO_BARE.search(t)
    if m:
        value = Decimal(m.group(1).replace(",", ""))
        return ["amount", _num(value)] if value > 0 else None
    return None


def resolve_percent_change(text: str) -> Optional[str]:
    """A bulk change is a signed percent only ("increase all prices by 10%" -> "10"); None otherwise."""
    found = resolve_price(text)
    return found[1] if found and found[0] == "percent" else None


def resolve_round(text: str) -> int:
    """Round new prices to the nearest Rs 1 (default), 5 or 10, as stated."""
    m = _ROUND.search(text or "")
    return int(m.group(1)) if m else 1


def _lab_words(text: str) -> list:
    t = _ROUND.sub(" ", _AMOUNTS.sub(" ", (text or "").lower()))
    return [w for w in re.findall(r"[a-z][a-z0-9]*", re.sub(r"'s\b", "", t)) if w not in _LAB_NOISE]


def resolve_lab_test(text: str) -> Optional[str]:
    """The test a catalogue instruction names (a search term, lower-cased, 2..60 chars): quoted, else the
    words left once instruction words and amounts are removed. Never stored: it only picks a row."""
    quoted = resolve_quoted(text)
    term = quoted.lower() if quoted else " ".join(_lab_words(text))
    return term if 2 <= len(term) <= 60 else None


def resolve_lab_scope(text: str) -> str:
    """"all", or the category words of a bulk instruction ("all imaging prices" -> "imaging")."""
    return " ".join(_lab_words(text))[:60] or "all"


def resolve_quoted(text: str) -> Optional[str]:
    """Text inside quotes, exactly as typed (a new test's name); 1..100 chars, else None."""
    m = _QUOTED.search(text or "")
    name = m.group(1).strip() if m else ""
    return name if 1 <= len(name) <= 100 else None


def _clock(h: str, m: Optional[str], mer: Optional[str]) -> Optional[int]:
    hour, minute = int(h), int(m or 0)
    if minute > 59:
        return None
    if mer:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if mer.lower().startswith("p") else 0)
    elif hour > 23:
        return None
    return hour * 60 + minute


def resolve_time_window(text: str) -> Optional[str]:
    """Collection hours "HH:MM-HH:MM" from "7 to 11", "6:30 am to 10:30 am", "9 am to 1 pm", "07:00-11:30".
    One am/pm applies to both ends unless that puts the start after the end ("11 to 2 pm" = 11:00-14:00);
    with none, an end not after the start is read as pm ("7 to 1" = 07:00-13:00). Start >= end -> None."""
    m = _WINDOW.search(text or "")
    if not m:
        return None
    h1, m1, p1, h2, m2, p2 = m.groups()
    end = _clock(h2, m2, p2)
    if end is None:
        return None
    if p1 or not p2:
        start = _clock(h1, m1, p1)
    else:  # "11 to 2 pm": the start takes the end's am/pm unless that puts it after the end
        start = _clock(h1, m1, p2)
        if start is not None and start >= end:
            start = _clock(h1, m1, "am" if p2.lower().startswith("p") else "pm")
    if start is not None and not p1 and not p2 and end <= start and end < 12 * 60:
        end += 12 * 60
    if start is None or start >= end or end >= 24 * 60:
        return None
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


# ── KK-P4-10: treatments (PB-10) and dental plans (PB-15) ──

_DR = re.compile(r"\b(?:dr\.?|doctor)\s+[a-z]+(?:\s+[a-z]+)?", re.I)
_TRT_NOISE = _LAB_NOISE | {
    "treatment", "treatments", "procedure", "procedures", "publish", "unpublish", "hide", "unhide", "show",
    "visible", "patients", "menu", "only", "does", "do", "doing", "perform", "performs", "chestaru", "chestharu",
    "sirf", "karti", "karte", "karta", "hain", "hai", "chupao", "chhupao", "dachu", "dachandi", "chupinchu",
    "dikhao", "who", "can"}
_HIDE = re.compile(r"\b(?:unpublish|hide|chupao|chhupao|dachu|dachandi)\b", re.I)
_SHOW = re.compile(r"\b(?:publish|unhide|show|chupinchu|dikhao)\b", re.I)


def _trt_words(text: str) -> list:
    t = _ROUND.sub(" ", _AMOUNTS.sub(" ", _DR.sub(" ", (text or "").lower())))
    return [w for w in re.findall(r"[a-z][a-z0-9]*", re.sub(r"'s\b", "", t)) if w not in _TRT_NOISE]


def resolve_treatment(text: str) -> Optional[str]:
    """The treatment a catalogue instruction names (search words, lower-cased, 2..60), never a doctor's name."""
    quoted = resolve_quoted(text)
    term = quoted.lower() if quoted else " ".join(_trt_words(text))
    return term if 2 <= len(term) <= 60 else None


def resolve_treatment_scope(text: str) -> str:
    """"all", or the category words of a bulk treatment instruction ("all laser treatments" -> "laser")."""
    return " ".join(_trt_words(text))[:60] or "all"


def resolve_show_hide(text: str) -> Optional[str]:
    """"hide" or "show" (publish) when the text says which; hide wins a tie (the safer reading)."""
    return "hide" if _HIDE.search(text or "") else "show" if _SHOW.search(text or "") else None


_FOR_PATIENT = re.compile(r"\bfor\s+([A-Za-z][A-Za-z.' ]*?)(?=\s*(?:[,\d+]|\b(?:with|on|at|first|next|sitting|sittings|"
                          r"plan|from|to|and)\b|$))")
_PATIENT_POSSESSIVE = re.compile(r"\b([A-Za-z]+)(?:'s|\s+ki|\s+ka|\s+ke)\s+(?:next|agli|agla|agle|first)\s+sitting",
                                 re.I)
_PLAN_HEAD = re.compile(r"^\s*(?:start\s+(?:a\s+)?|new\s+|make\s+(?:a\s+)?)?([A-Za-z][A-Za-z ]*?)\s+plan\b", re.I)
_PLAN_NOISE = {"a", "the", "new", "dental", "treatment", "start", "make"}
_SITTINGS = re.compile(r"\b(\d{1,2})\s*sittings?\b", re.I)
_AT_CLOCK = re.compile(r"\bat\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b", re.I)
_HH_MM = re.compile(r"(?<![\d:])(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", re.I)
_H_MER = re.compile(r"\b(\d{1,2})\s*(am|pm)\b", re.I)


def resolve_patient_for(text: str) -> Optional[str]:
    """The patient's name after "for" ("plan for Suresh Kumar 98765..."), casing kept; 2..60 chars."""
    m = _FOR_PATIENT.search(text or "")
    return _cased_words(m.group(1), _PERSON_NOISE) if m else None


def resolve_sitting_patient(text: str) -> Optional[str]:
    """Whose next sitting: "Suresh's next sitting", "Suresh ki agli sitting", or "... for Suresh"."""
    m = _PATIENT_POSSESSIVE.search(text or "")
    if m and m.group(1).lower() not in _PERSON_NOISE:
        return m.group(1)
    return resolve_patient_for(text)


def resolve_plan_treatment(text: str) -> Optional[str]:
    """The treatment named before "plan" ("Root canal plan for ..." -> "root canal"); None otherwise."""
    m = _PLAN_HEAD.search(text or "")
    words = [w for w in (m.group(1).lower().split() if m else []) if w not in _PLAN_NOISE]
    term = " ".join(words)
    return term if 2 <= len(term) <= 60 else None


def resolve_sittings(text: str) -> Optional[int]:
    m = _SITTINGS.search(text or "")
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 30 else None


def resolve_clock(text: str) -> Optional[str]:
    """A sitting time "HH:MM": "at 10:30", "at 11 am", "4:30 pm", "11 am"; None when the text has none."""
    t = text or ""
    m = _AT_CLOCK.search(t) or _HH_MM.search(t)
    if m:
        h, mm, mer = m.group(1), m.group(2), m.group(3)
    else:
        m = _H_MER.search(t)
        if not m:
            return None
        h, mm, mer = m.group(1), None, m.group(2)
    minutes = _clock(h, mm, mer)
    return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


# ── KK-P4-11: doctor schedule, new doctor, branch assignment ──

_MORNING = re.compile(r"\bmornings?\b|ఉదయం|सुबह", re.I)
_EVENING = re.compile(r"\bevenings?\b|సాయంత్రం|शाम", re.I)
_MERIDIEM = re.compile(r"\d\s*(?:am|pm|a\.m\.|p\.m\.)", re.I)
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAY_WORD = r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*"
_DAY_RANGE = re.compile(_DAY_WORD + r"\s*(?:-|–|to|till|se)\s*" + _DAY_WORD, re.I)
_NEW_DOCTOR = re.compile(r"\b(?:add|new|create)\b(?:\s+(?:a|the))?(?:\s+new)?(?:\s+doctor)?\s+"
                         r"(dr\.?\s+[A-Za-z]+(?:\s+[A-Za-z]+)?)", re.I)
_BRANCH_NAME = re.compile(r"\b(?:to|at|in|ni|ko|ki)?\s*(?:the\s+)?([A-Za-z]+(?:\s+[A-Za-z]+)?)\s+branch\b", re.I)
_BRANCH_NOISE = {"to", "at", "in", "the", "ni", "ko", "ki", "assign", "dr", "doctor", "a", "this", "that", "same"}


def resolve_session(text: str) -> Optional[str]:
    """"morning" or "evening" when the text names exactly one; None for both or neither."""
    m, e = bool(_MORNING.search(text or "")), bool(_EVENING.search(text or ""))
    return "morning" if m and not e else "evening" if e and not m else None


def resolve_session_window(text: str, session: str) -> Optional[str]:
    """A session's hours "HH:MM-HH:MM". In the evening, hours said without am/pm are afternoon hours
    ("evening 5 to 8" = 17:00-20:00)."""
    window = resolve_time_window(text)
    if not window or session != "evening" or _MERIDIEM.search(text or ""):
        return window
    start, end = window.split("-")
    if start >= "12:00":
        return window

    def pm(hm: str) -> str:
        return f"{int(hm[:2]) + 12:02d}{hm[2:]}" if hm < "12:00" else hm
    return f"{pm(start)}-{pm(end)}"


def resolve_days(text: str) -> Optional[str]:
    """Working days as the doctors table keeps them ("Mon,Tue,Wed,Thu,Fri"): a range ("Mon-Sat",
    "monday to friday"), "weekdays", "all days", or a list ("Mon, Wed and Fri"); None when none is said."""
    t = (text or "").lower()
    if re.search(r"\b(?:all days|every day|daily|all week)\b", t):
        return ",".join(_DAY_NAMES)
    if re.search(r"\bweekdays?\b", t):
        return ",".join(_DAY_NAMES[:5])
    m = _DAY_RANGE.search(t)
    if m:
        a, b = (_DAY_NAMES.index(x.capitalize()) for x in m.groups())
        return ",".join(_DAY_NAMES[a:b + 1]) if a <= b else None
    found = [d for d in _DAY_NAMES if re.search(rf"\b{d.lower()}[a-z]*\b", t)]
    return ",".join(found) if found else None


def resolve_new_doctor(text: str) -> Optional[str]:
    """The new doctor's name as typed, after "add" / "new doctor" ("Dr Kiran Kumar"); 4..60 chars."""
    m = _NEW_DOCTOR.search(text or "")
    if not m:
        return None
    name = " ".join(m.group(1).split())
    return name if 4 <= len(name) <= 60 else None


def resolve_branch(text: str) -> Optional[str]:
    """The branch named before the word "branch" (lower-cased search words), else None."""
    m = _BRANCH_NAME.search(text or "")
    words = [w for w in (m.group(1).lower().split() if m else []) if w not in _BRANCH_NOISE]
    term = " ".join(words)
    return term if 2 <= len(term) <= 60 else None


def resolve_assign_session(text: str) -> str:
    """A branch assignment's session: one named session, else both."""
    return resolve_session(text) or "both"


def resolve_window_after(text: str, session: str) -> Optional[str]:
    """The hours said after a session word ("mornings 9 to 1 and evenings 5 to 8"), else None."""
    rx, other = (_MORNING, _EVENING) if session == "morning" else (_EVENING, _MORNING)
    m = rx.search(text or "")
    if not m:
        return None
    seg = text[m.start():]
    o = other.search(seg)
    return resolve_session_window(seg[:o.start()] if o else seg, session)


# ── KK-P4-12: staff login hand-off pre-fill ──

_STAFF_NAME = re.compile(r"\b(?i:staff|login|account)\s+(?:(?i:for)\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")
_STAFF_ROLES = (("front desk", "FRONT_DESK"), ("receptionist", "RECEPTIONIST"), ("appointment manager",
                "APPOINTMENT_MANAGER"), ("branch manager", "BRANCH_MANAGER"), ("pharmacy", "PHARMACY_OPERATOR"),
                ("lab", "LAB_OPERATOR"), ("cashier", "CASHIER"), ("phlebotomist", "PHLEBOTOMIST"), ("doctor", "DOCTOR"))


def resolve_staff_name(text: str) -> Optional[str]:
    """A new staff member's name as typed ("add staff Ramesh as ..."), casing kept; only pre-fills a form."""
    m = _STAFF_NAME.search(text or "")
    return m.group(1) if m else None


def resolve_staff_role(text: str) -> Optional[str]:
    """The Staff page's role option for the words used ("receptionist" -> RECEPTIONIST), else None."""
    t = (text or "").lower()
    return next((value for words, value in _STAFF_ROLES if re.search(rf"\b{words}\b", t)), None)


# ── KK-P4-13: voice receptionist knowledge / lexicon ──

_ABOUT = re.compile(r"\b(?:about|on)\s+(.+)$", re.I)
# up to two words may sit between the phrase and "for" ("anitha amma" antaru for Dr Anitha)
_LEX_TARGET = re.compile(r"[\"“”][^\"“”]+[\"“”](?:\s+\S+){0,2}?\s+(?:for|means|is|=)\s+(.+)$", re.I)


def _words(found: Optional[str]) -> Optional[str]:
    term = re.sub(r"\s+", " ", (found or "").lower()).strip(" .?!,")
    return term if 2 <= len(term) <= 60 else None


def resolve_about(text: str) -> Optional[str]:
    """Which saved answer: the words after "about" ("remove the answer about parking"), lower-cased."""
    m = _ABOUT.search(text or "")
    return _words(m.group(1)) if m else None


def resolve_lexicon_target(text: str) -> Optional[str]:
    """What a quoted caller phrase means ('callers say "anitha madam" for Dr Anitha'), lower-cased."""
    m = _LEX_TARGET.search(text or "")
    return _words(m.group(1)) if m else None


# ── KK-P4-15: home collection assignment ("assign Lakshmi's home visit to Kiran") ──

_ASSIGN_VISIT = re.compile(r"\bassign\s+(.+?)\s+to\s+(.+?)\s*$", re.I)
_VISIT_NOISE = {"home", "visit", "visits", "collection", "sample", "pickup", "the", "s", "patient"}
_PHLEB_NOISE = {"phlebotomist", "the", "sister", "mr", "ms", "brother"}


def _assign_part(text: str, group: int, noise: set) -> Optional[str]:
    m = _ASSIGN_VISIT.search((text or "").replace("’", "'"))
    if not m:
        return None
    words = [w for w in re.findall(r"[a-z]+", re.sub(r"'s\b", "", m.group(group).lower())) if w not in noise]
    return _words(" ".join(words))


def resolve_visit_patient(text: str) -> Optional[str]:
    return _assign_part(text, 1, _VISIT_NOISE)


def resolve_phleb_name(text: str) -> Optional[str]:
    return _assign_part(text, 2, _PHLEB_NOISE)


# ── voice.settings_update: one AI receptionist setting from the text ──

_VS_NAME = (re.compile(r"\brename\s+(?:the\s+)?(?:ai\s+)?receptionist\s+(?:to|as)\s+(.+)$", re.I),
            re.compile(r"\breceptionist(?:'s)?\s+name\s+(?:to|as|is|=)\s+(.+)$", re.I),
            re.compile(r"\breceptionist\s+ka\s+naam\s+(.+?)\s+(?:rakho|rakhiye|karo)\b", re.I),
            re.compile(r"\breceptionist\s+peru\s+(.+?)\s+(?:pettu|pettandi|marchu)\b", re.I))
_VS_LANG = {"telugu": "te-IN", "hindi": "hi-IN", "english": "en-IN"}
_VS_PACE = re.compile(r"\b(?:pace|speed)\s+(\d(?:\.\d{1,2})?)\b", re.I)
_VS_CAP = (re.compile(r"\b(?:lead\s+)?call\s+limit\s+(?:to\s+|of\s+)?(\d{1,4})\b", re.I),
           re.compile(r"\b(\d{1,4})\s+(?:lead\s+)?calls?\s+(?:a|per)\s+day\b", re.I))
_VS_TRIES = re.compile(r"\b(?:try|call)\s+(?:each|every)\s+lead\s+(\d{1,2})\s+times?\b", re.I)
_VS_EMERGENCY = re.compile(r"\bemergency\s+(?:number|contact|phone)\D{0,6}(\+?\d[\d\s-]{1,18}\d)", re.I)
_BETWEEN = re.compile(r"\bbetween\s+(.+?)\s+and\s+(.+)$", re.I)


def _vs_window(text: str) -> Optional[str]:
    return resolve_time_window(_BETWEEN.sub(r"\1 to \2", text or ""))


def resolve_voice_setting(text: str) -> Optional[list]:
    """[field, value] for the one receptionist setting the text changes, else None (the form asks).
    Lead-call settings first ("lead calls between 10 and 7"), then reception hours, name, language, pace,
    emergency number. Values are checked against the endpoint's bounds by the preview, not here."""
    t = (text or "").strip()
    low = t.lower()
    if re.search(r"\blead\s+calls?\b|\bcall\s+window\b|\bcall\s+limit\b|\beach\s+lead\b|\bevery\s+lead\b", low):
        m = _VS_TRIES.search(t)
        if m:
            return ["max_attempts", int(m.group(1))]
        for rx in _VS_CAP:
            m = rx.search(t)
            if m:
                return ["daily_cap", int(m.group(1))]
        window = _vs_window(t)
        if window:
            return ["outbound_window", window]
    if re.search(r"\breception(?:ist)?\s+(?:hours?|timings?|time|samayam|ka\s+samay)\b", low):
        window = _vs_window(t)
        if window:
            return ["reception_hours", {"window": window, "days": resolve_days(t)}]
    for rx in _VS_NAME:
        m = rx.search(t)
        if m:
            name = m.group(1).strip().strip("\"“”'").strip()
            return ["assistant_name", name] if name else None
    if "receptionist" in low:
        lang = next((code for word, code in _VS_LANG.items() if re.search(rf"\b{word}\b", low)), None)
        if lang and re.search(r"\b(?:language|speak|bhasha|bhaasha)\b", low):
            return ["primary_language", lang]
        if re.search(r"\b(?:slower|slowly|slow)\b", low):
            return ["pace", "-0.1"]
        if re.search(r"\b(?:faster|quickly|fast)\b", low):
            return ["pace", "+0.1"]
        m = _VS_PACE.search(t)
        if m:
            return ["pace", m.group(1)]
    m = _VS_EMERGENCY.search(t)
    if m:
        return ["emergency_number", re.sub(r"[\s-]", "", m.group(1))]
    return None
