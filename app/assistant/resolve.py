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
_BY_BARE = re.compile(r"\bby\s+(\d[\d,]*)(?![\d,.]*\s*(?:%|per\s?cent|pc\b))", re.I)
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
    m = _BY_BARE.search(t) if sign else None  # "increase the fee by 100": rupees, said without ₹
    if m:
        value = Decimal(m.group(1).replace(",", ""))
        return ["delta", _num(value * sign)] if value > 0 else None
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


_PRICE_WORD = re.compile(r"\b(?:price|prices|rate|rates|cost|costs|charge|charges|fee|fees|entha|enta|kitna|"
                         r"kitne|dhara|dharalu|keemat|kimat)\b", re.I)
_QUESTION_WORDS = {"what", "whats", "how", "much", "does", "do", "is", "are", "much", "tell", "me", "cost", "costs",
                   "charge", "charges", "entha", "enta", "kitna", "kitne", "hai", "ka", "ki", "ke", "dhara", "keemat"}


def resolve_price_term(text: str) -> Optional[str]:
    """The test a price question names ("price of cbc test" -> "cbc"), else None. Only when the text asks a
    price, so "how many tests do we have" keeps listing every test."""
    if not _PRICE_WORD.search(text or ""):
        return None
    term = resolve_lab_test(text)
    words = [w for w in (term or "").split() if w not in _QUESTION_WORDS]
    term = " ".join(words)
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
_NEW_DOCTOR = re.compile(r"\b(?:add|new|create|register|onboard)\b(?:\s+(?:a|the))?(?:\s+new)?(?:\s+doctor)?\s+"
                         r"(dr\.?\s+[A-Za-z]+(?:\s+[A-Za-z]+)?)", re.I)
# "create the doctor srikanth": no "Dr", so the name is the words after "doctor" up to a non-name word.
_NEW_DOCTOR_BARE = re.compile(r"\b(?:add|new|create|register|onboard)\b(?:\s+(?:a|the))?(?:\s+new)?\s+doctor\s+"
                              r"([A-Za-z]+(?:\s+[A-Za-z]+)?)", re.I)
_NOT_NAME = re.compile(r"^(?:for|with|in|on|at|of|and|as|from|to|is|fee|fees|rs|inr|department|dept|"
                       r"specialist|specialization|mornings?|evenings?|weekdays?|daily|leave|"
                       r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?|"
                       r"[a-z]+(?:logy|logist|ics|iatry|iatrician|"
                       r"ist|surgery|surgeon|medicine|physician))$", re.I)
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
    start, end = pm(start), pm(end)
    return f"{start}-{end}" if start < end else None  # "evening 9 to 1" is no evening: ask, never 21:00-13:00


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


# Spoken profiles: "the doctor's name is Ramesh Kumar", "name of the doctor is Dr Priya".
_NAMED_DOCTOR = re.compile(r"\b(?:(?:doctor|dr)(?:'?s)?\s+name|name\s+of\s+(?:the\s+)?(?:new\s+)?(?:doctor|dr)|"
                           r"(?:his|her)\s+name)\s+(?:is|will be|should be)\s+(?:dr\.?\s+)?([A-Za-z]+(?:\s+[A-Za-z]+)?)",
                           re.I)
# A value said outright ("his specialization is dermatologist", "department from ENT to Skin");
# a mere mention is never read as one.
_SAID_AS = r"\s+(?:from\s+[A-Za-z&\- ]+?\s+)?(?:to|as|is now|is|will be|should be|:)\s+(?:an?\s+|the\s+)?"
_STATED = {
    "specialization": re.compile(r"\b(?:speciali[sz]ation|specialty|speciality)" + _SAID_AS + r"([A-Za-z][A-Za-z&\- ]*?)"
                                 r"(?=\s*(?:[,.;]|\b(?:and|with|his|her|fee|fees|on|in|department|dept)\b|$))", re.I),
    "department": re.compile(r"\b(?:department|dept)" + _SAID_AS + r"([A-Za-z][A-Za-z&\- ]*?)"
                             r"(?=\s*(?:[,.;]|\b(?:and|with|his|her|fee|fees|on|department|speciali[sz]ation)\b|$))",
                             re.I),
}


def _doctor_name(raw: str) -> str:
    words = []
    for w in raw.split():
        if _NOT_NAME.match(w):
            break
        words.append(w.capitalize() if w.islower() else w)
    return "Dr " + " ".join(words) if words else ""


def resolve_new_doctor(text: str) -> Optional[str]:
    """The new doctor's name as typed, after "add" / "new doctor" ("Dr Kiran Kumar") or "the doctor's name
    is ..."; 4..60 chars."""
    m = _NEW_DOCTOR.search(text or "")
    if m:
        name = " ".join(m.group(1).split())
    elif m := (_NEW_DOCTOR_BARE.search(text or "") or _NAMED_DOCTOR.search(text or "")):
        name = _doctor_name(m.group(1))
    else:
        return None
    return name if 4 <= len(name) <= 60 else None


def resolve_stated(text: str, field: str) -> Optional[str]:
    """A doctor's specialization / department only when said as "<field> is X"; 2..60 chars, else None."""
    rx = _STATED.get(field)
    m = rx.search(text or "") if rx else None
    value = " ".join(m.group(1).split()) if m else ""
    return value[:1].upper() + value[1:] if 2 <= len(value) <= 60 else None


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


def _bare_window(m: re.Match) -> Optional[str]:
    """One window said without a session word, read as clinic hours. With am/pm it is taken as said.
    Without: crossing noon ("9 to 1", "11 to 2") or starting 8-11 is the morning; starting at 12 is
    afternoon; starting 1-7 is the evening ("6 to 8" = 18:00-20:00) unless it ends at 11 or 12
    ("7 to 11", "6 to 12" = morning)."""
    seg = m.group(0)
    if _MERIDIEM.search(seg):
        return resolve_time_window(seg)
    h1, h2 = int(m.group(1)), int(m.group(4))
    if h2 < h1 or 8 <= h1 <= 12 or h2 >= 11:
        return resolve_time_window(seg)
    return resolve_session_window(seg, "evening")


def resolve_bare_windows(text: str) -> dict:
    """{"morning": "HH:MM-HH:MM", "evening": ...} for hours said with no session word ("9 to 1",
    "9 to 1 and 6 to 8"). {} when a session word is present (resolve_window_after reads those), when a
    window can't be read, or when two windows land in the same session: then the form keeps asking."""
    t = text or ""
    if _MORNING.search(t) or _EVENING.search(t):
        return {}
    out: dict = {}
    for m in _WINDOW.finditer(t):
        window = _bare_window(m)
        session = "morning" if window and window < "12:00" else "evening"
        if window is None or session in out:
            return {}
        out[session] = window
    return out


# ── KK-P4-12: staff login hand-off pre-fill ──

_STAFF_NAME = re.compile(r"\b(?i:staff|login|account)\s+(?:(?i:for)\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")
_STAFF_ROLES = (("front desk", "FRONT_DESK"), ("receptionist", "RECEPTIONIST"), ("appointment manager",
                "APPOINTMENT_MANAGER"), ("branch manager", "BRANCH_MANAGER"), ("pharmacy", "PHARMACY_OPERATOR"),
                ("lab", "LAB_OPERATOR"), ("cashier", "CASHIER"), ("phlebotomist", "PHLEBOTOMIST"), ("doctor", "DOCTOR"),
                ("nurse", "NURSE"))


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


# ── Voice-first bedside / counter entry: vitals, beds, counter payments ──

_IS = r"\s*(?:is|was|of|at|=|:|-)?\s*"
# (field, pattern, low, high): the same bounds as VitalsIn / ObservationIn, so a mis-heard number is asked, not sent.
_VITAL_RX = (
    ("pulse_bpm", re.compile(r"\b(?:pulse(?:\s+rate)?|heart\s+rate|hr|pr)" + _IS + r"(\d{2,3})\b", re.I), 20, 250),
    ("resp_rate", re.compile(r"\b(?:resp(?:iratory)?\s+rate|respiration|rr|breathing\s+rate)" + _IS + r"(\d{1,2})\b",
                             re.I), 4, 80),
    ("spo2_pct", re.compile(r"\b(?:spo2|sp\s*o2|o2\s+sat(?:uration)?|sats?|saturation|oxygen(?:\s+saturation)?)" + _IS
                            + r"(\d{2,3})\b", re.I), 50, 100),
    ("weight_kg", re.compile(r"\b(?:weight|wt)" + _IS + r"(\d{1,3}(?:\.\d{1,2})?)", re.I), 0.5, 400),
    ("height_cm", re.compile(r"\b(?:height|ht)" + _IS + r"(\d{2,3}(?:\.\d)?)", re.I), 30, 250),
    ("blood_glucose_mg_dl", re.compile(r"\b(?:blood\s+sugar|sugar|glucose|grbs|rbs|cbg)" + _IS + r"(\d{2,4})\b", re.I),
     10, 1500),
    ("pain_score", re.compile(r"\bpain(?:\s+score)?" + _IS + r"(\d{1,2})\b(?!\s*(?:days?|hours?))", re.I), 0, 10),
)
_BP = re.compile(r"\b(?:bp|b\.p\.?|blood\s+pressure)" + _IS + r"(\d{2,3})\s*(?:/|over|by|upon|\\)\s*(\d{2,3})\b", re.I)
_TEMP = re.compile(r"\b(?:temp(?:erature)?|fever)" + _IS + r"(\d{2,3}(?:\.\d{1,2})?)\s*"
                   r"(°\s*[cf]\b|degrees?\s*(?:celsius|centigrade|fahrenheit|[cf]\b)?|[cf]\b)?", re.I)
_WEIGHT_UNIT = re.compile(r"\b(\d{1,3}(?:\.\d{1,2})?)\s*(?:kg|kgs|kilos?|kilograms?)\b", re.I)
_VITAL_WORDS = {"bp", "blood", "pressure", "pulse", "rate", "heart", "hr", "pr", "temp", "temperature", "fever",
                "spo", "o", "sat", "sats", "saturation", "oxygen", "weight", "wt", "height", "ht", "kg", "kgs",
                "kilos", "cm", "sugar", "glucose", "grbs", "rbs", "cbg", "pain", "score", "resp", "respiratory",
                "respiration", "rr", "breathing", "over", "by", "upon", "degrees", "degree", "celsius", "fahrenheit",
                "f", "c", "is", "was", "of", "at", "vitals", "vital", "record", "enter", "save", "add", "note", "for",
                "patient", "please", "and", "with", "signs", "check", "update", "his", "her", "the", "a", "in",
                "bed", "token", "number", "no", "ward", "ki", "ka", "ke", "ko", "ni", "lo", "chey", "cheyyi", "karo",
                "chart", "today", "now", "nurse", "observations", "observation", "s"}


def resolve_vitals(text: str) -> Optional[dict]:
    """Vital signs said in one breath ("BP 120 over 80, pulse 88, temp 99.5, SpO2 97, weight 62 kg") as the
    endpoint's field names. A value outside the endpoint's bounds is dropped (the form asks again); a
    temperature from 90 up, or said in °F, is Fahrenheit and converted. None when nothing is read."""
    t = text or ""
    out: dict = {}
    m = _BP.search(t)
    if m:
        sys_, dia = int(m.group(1)), int(m.group(2))
        if 50 <= sys_ <= 300 and 20 <= dia <= 200 and sys_ > dia:
            out["bp_systolic"], out["bp_diastolic"] = sys_, dia
    m = _TEMP.search(t)
    if m:
        value, unit = Decimal(m.group(1)), (m.group(2) or "").lower()
        fahrenheit = "f" in unit or ("c" not in unit.replace("degree", "") and value >= 80)  # 98.6 is never °C
        value = ((value - 32) * 5 / 9 if fahrenheit else value).quantize(Decimal("0.1"))
        if 30 <= value <= 45:
            out["temperature_c"] = str(value)
    for field, rx, low, high in _VITAL_RX:
        m = rx.search(t)
        if m and low <= float(m.group(1)) <= high:
            out[field] = m.group(1) if "." in m.group(1) else int(m.group(1))
    if "weight_kg" not in out:
        m = _WEIGHT_UNIT.search(t)
        if m and 0.5 <= float(m.group(1)) <= 400:
            out["weight_kg"] = m.group(1) if "." in m.group(1) else int(m.group(1))
    return out or None


def _without_vitals(text: str) -> str:
    t = text or ""
    for rx in (_BP, _TEMP, _WEIGHT_UNIT, *(rx for _, rx, _, _ in _VITAL_RX)):
        t = rx.sub(" ", t)
    return t


_BED = re.compile(r"\bbed\s*(?:no\.?|number|num|#)?\s*([a-z]{0,4}[\s-]?\d{1,4}[a-z]?)\b", re.I)
_TO_BED = re.compile(r"\b(?:to|into|in|ki|ko|ku)\s+bed\s*(?:no\.?|number|num|#)?\s*([a-z]{0,4}[\s-]?\d{1,4}[a-z]?)\b",
                     re.I)


def _bed_code(raw: str) -> str:
    return re.sub(r"\s+", "-", raw.strip()).upper()


# Bed codes are unique only inside a ward: "bed 5 in ICU" carries the ward as "5@icu".
_WARD_HINT = re.compile(r"^\s*(?:,\s*)?(?:in|of|at)\s+(?:the\s+)?([a-z][a-z0-9 -]{0,24}?)\s+ward\b"
                        r"|^\s*(?:,\s*)?(?:in\s+(?:the\s+)?)?(icu|nicu|picu|hdu|ccu|sicu|micu)\b", re.I)


def _with_ward(code: str, after: str) -> str:
    m = _WARD_HINT.search(after or "")
    return f"{code}@{(m.group(1) or m.group(2)).lower().strip()}" if m else code


# the destination with its ward ("to bed 15 in ICU"): neither is part of the patient's name
_TO_BED_AND_WARD = re.compile(_TO_BED.pattern + r"(?:\s*,?\s*(?:in|of|at)\s+(?:the\s+)?[a-z][a-z0-9 -]{0,24}?\s+ward\b"
                              r"|\s+(?:in\s+(?:the\s+)?)?(?:icu|nicu|picu|hdu|ccu|sicu|micu)\b)?", re.I)


def resolve_bed(text: str) -> Optional[str]:
    """The patient's current bed ("bed 12", "bed ICU-3", "bed 5 in ICU" -> "5@icu"), never the bed they are
    moved TO; upper-cased code."""
    t = text or ""
    to = _TO_BED.search(t)
    src = t[:to.start()] + " " * (to.end() - to.start()) + t[to.end():] if to else t
    m = _BED.search(src)
    if not m:
        return None
    end = to.start() if to and to.start() > m.end() else len(t)
    return _with_ward(_bed_code(m.group(1)), t[m.end():end])


def resolve_to_bed(text: str) -> Optional[str]:
    """The bed a patient moves to ("shift Ravi to bed 15", "to bed 3 in the private ward" -> "3@private")."""
    m = _TO_BED.search(text or "")
    return _with_ward(_bed_code(m.group(1)), text[m.end():]) if m else None


_WHY = re.compile(r"\b(?:because|reason|due\s+to|since)\b", re.I)
_MONEY_WORDS = {"refund", "refunds", "void", "cancel", "deposit", "advance", "money", "back", "return", "in", "to",
                "rupee", "give", "complete", "discharge", "finish", "final", "close", "stay", "release", "start",
                "credit", "note", "on", "of", "issue", "bill", "invoice", "ne", "diye", "diya", "di", "rupaye", "rupaiye",
                "kattaru", "ichcharu", "ichaaru", "shift", "shifted", "transfer", "transferred", "patient"}
_STAY_DETAIL = re.compile(r"\b(?:attendant|insurance|insured|tpa|payer|policy|expected|self\s+pay|corporate|"
                          r"government|aarogyasri|ayushman|cashless)\b", re.I)


def resolve_patient_target(text: str) -> Optional[str]:
    """Who the entry is for: "#5" (token), "bed:12" (an inpatient's bed), else the patient's name words
    (lower-cased) left once vitals, beds, amounts and instruction words are removed."""
    m = _TOKEN.search(text or "")
    if m:
        return f"#{int(m.group(1))}"
    bed = resolve_bed(text)
    if bed:
        return f"bed:{bed}"
    t = _WHY.split(_without_vitals(text), maxsplit=1)[0]  # "because it was billed twice" is not the name
    t = _STAY_DETAIL.split(t, maxsplit=1)[0]                # nor "attendant Ramu" / "insurance Star Health"
    t = _AMOUNTS.sub(" ", _PAY_REF.sub(" ", _TO_BED_AND_WARD.sub(" ", t)))
    noise = _QUEUE_NOISE | _VITAL_WORDS | _PAY_WORDS | _MONEY_WORDS
    words = [w for w in re.findall(r"[a-z]+", re.sub(r"'s\b", "", t.lower())) if w not in noise]
    name = " ".join(words)
    return name if 2 <= len(name) <= 40 else None


_PAY_MODES = (("upi", r"\b(?:upi|gpay|google\s*pay|phone\s*pe|phonepe|paytm|bhim|qr)\b"),
              ("card", r"\b(?:card|debit|credit\s+card|swipe|pos)\b"),
              ("cash", r"\b(?:cash|nagadu|naqad|nakad|నగదు|नकद)\b"))
_PAY_REF = re.compile(r"\b(?:utr|rrn|ref(?:erence)?|transaction(?:\s+(?:id|number|no))?|txn(?:\s+id)?)"
                      r"\s*(?:no\.?|number|id|is|:|#)?\s*([a-z0-9]{6,30})\b", re.I)
_PAY_WORDS = {"paid", "pay", "payment", "payments", "received", "receive", "collect", "collected", "record", "cash",
              "upi", "card", "gpay", "google", "phonepe", "phone", "pe", "paytm", "bhim", "qr", "debit", "credit",
              "swipe", "pos", "rupees", "rs", "inr", "utr", "rrn", "ref", "reference", "transaction", "txn", "id",
              "from", "via", "through", "by", "amount", "bill", "invoice", "against", "chesaru", "kattaru",
              "diya", "diye", "di", "nagadu", "naqad", "nakad", "full", "balance", "due"}


def resolve_pay_mode(text: str) -> Optional[str]:
    """"cash" | "upi" | "card" when exactly one is said; None for none or several (the form asks)."""
    found = {mode for mode, rx in _PAY_MODES if re.search(rx, text or "", re.I)}
    return found.pop() if len(found) == 1 else None


def resolve_pay_ref(text: str) -> Optional[str]:
    """The UPI / card transaction reference ("UTR 412345678901"), upper-cased; None when not said."""
    m = _PAY_REF.search(text or "")
    return m.group(1).upper() if m else None


_PAID_NUM = re.compile(r"\b(?:paid|pays|pay|received|receive|collected|collect|got|kattaru|chesaru|diya|diye|di)\s+"
                       r"(?:rs\.?\s*|₹\s*|inr\s*)?(\d[\d,]*)(?![\d,]*\s*%)\b", re.I)
_NUM_MODE = re.compile(r"(?<![\w#])(\d[\d,]*)\s*(?:rupees?\s+|rs\.?\s+)?(?:in\s+|by\s+|via\s+|through\s+)?"
                       r"(?:cash|upi|card|gpay|phonepe|paytm)\b", re.I)


def resolve_paid_amount(text: str) -> Optional[str]:
    """Rupees received, whole: "₹500", "500 rupees", "paid 500", "500 cash"; never a token or UTR number."""
    t = _PAY_REF.sub(" ", _TOKEN.sub(" ", text or ""))
    found = resolve_amount(t)
    if found and found[0] == "amount":
        value = found[1]
    else:
        m = _PAID_NUM.search(t) or _NUM_MODE.search(t)
        if not m:
            return None
        value = Decimal(m.group(1).replace(",", ""))
    return str(int(value)) if value > 0 and value == int(value) else None


_DOCTOR_OFF = re.compile(r"\b(?:remove|delete|deactivate|disable|switch\s+off|turn\s+off|inactive|hatao|hata\s+do|"
                         r"teeseyi|teeseyandi)\b|తొలగించు|हटाओ", re.I)
_DOCTOR_ON = re.compile(r"\b(?:activate|reactivate|enable|switch\s+on|turn\s+on|bring\s+back|restore|active\s+again)\b",
                        re.I)


def resolve_doctor_switch(text: str) -> Optional[str]:
    """"off" to remove / deactivate a doctor, "on" to bring one back; None when both or neither are said."""
    off, on = bool(_DOCTOR_OFF.search(text or "")), bool(_DOCTOR_ON.search(re.sub(r"\bdeactivate\b", " ", text or "",
                                                                                     flags=re.I)))
    return "off" if off and not on else "on" if on and not off else None


# ── Voice-first batch 2: admission, discharge, bed housekeeping, service prices ──

_ADMIT_WHO = re.compile(r"\badmit(?:\s+(?:the\s+)?patient)?\s+(.+?)(?=\s+(?:under|with|to|in|into|for|on|at|bed|dr\.?|"
                        r"doctor|because|reason|due|as|urgent|urgently|emergency|elective)\b|[,.;]|$)", re.I)
_ADMIT_NOISE = {"a", "an", "the", "new", "patient", "mr", "mrs", "ms", "miss", "master", "baby", "of"}


def resolve_admit_patient(text: str) -> Optional[str]:
    """Who is admitted: their phone (10 digits) when said, else the name after "admit" (casing kept)."""
    phone = resolve_phone(text)
    if phone:
        return phone
    m = _ADMIT_WHO.search(text or "")
    words = [w for w in re.findall(r"[A-Za-z]+", m.group(1) if m else "") if w.lower() not in _ADMIT_NOISE]
    name = " ".join(words)
    return name if 2 <= len(name) <= 60 else None


def resolve_admit_bed(text: str) -> Optional[str]:
    """The bed an admission goes to ("to bed 15", "in bed ICU-2", "bed 15")."""
    return resolve_to_bed(text) or resolve_bed(text)


_REASON = re.compile(r"\b(?:for|reason(?:\s+is)?|because(?:\s+of)?|due\s+to|with\s+complaints?\s+of|c/o|complaint)\s+"
                     r"(?!(?:dr\.?|doctor|bed|admission)\b)(.+?)"
                     r"(?=\s+(?:under|to\s+bed|in\s+bed|into\s+bed|on\s+bed|bed|dr\.?|doctor|urgent(?:ly)?|"
                     r"as\s+(?:an?\s+)?(?:emergency|urgent))\b|[,;]|\.(?:\s|$)|$)", re.I)


def resolve_reason(text: str) -> Optional[str]:
    """The reason said in the staff member's own words ("for fever and vomiting", "because the AC is broken"),
    3..300 chars; never composed or summarised."""
    m = _REASON.search(text or "")
    reason = " ".join(m.group(1).split()).strip(" .") if m else ""
    return reason if 3 <= len(reason) <= 300 else None


def resolve_priority(text: str) -> str:
    t = (text or "").lower()
    return "emergency" if re.search(r"\bemergency\b", t) else "urgent" if re.search(r"\burgent(?:ly)?\b", t) \
        else "elective"


_DISCHARGE_TYPES = (("death", r"\b(?:death|died|expired|passed\s+away|deceased|mortality)\b"),
                    ("dama", r"\bdama\b|discharge(?:d)?\s+against\s+(?:medical\s+)?advice"),
                    ("lama", r"\blama\b|left\s+against\s+(?:medical\s+)?advice|against\s+(?:medical\s+)?advice"),
                    ("absconded", r"\babscond(?:ed|ing)?\b|ran\s+away|run\s+away"),
                    ("transfer_out", r"\btransfer(?:red)?\s+out\b|\brefer(?:red)?\s+(?:to|out)\b|another\s+hospital"))


def resolve_discharge_type(text: str) -> str:
    """The discharge type said; "regular" when none is."""
    t = text or ""
    return next((kind for kind, rx in _DISCHARGE_TYPES if re.search(rx, t, re.I)), "regular")


_BED_STATES = (("maintenance", r"\b(?:maintenance|repair|broken|not\s+working|out\s+of\s+order)\b"),
               ("blocked", r"\bblock(?:ed)?\b"),
               ("available", r"\b(?:cleaning\s+(?:is\s+)?(?:done|complete|completed|finished)|cleaned|is\s+clean|"
                             r"now\s+clean|ready|available|free\s+now|make\s+(?:it\s+)?free|unblock|back\s+in\s+service)\b"),
               ("cleaning", r"\b(?:needs?\s+cleaning|for\s+cleaning|to\s+clean|send\s+(?:for\s+)?cleaning|dirty|"
                            r"mark\s+(?:it\s+)?cleaning|cleaning)\b"))


def resolve_bed_state(text: str) -> Optional[str]:
    """The bed status said: available (clean / ready), cleaning, maintenance or blocked; None if unclear."""
    t = text or ""
    return next((state for state, rx in _BED_STATES if re.search(rx, t, re.I)), None)


# ── Voice-first batch 3: refunds, deposits, casualty registration, theatre booking ──

_REFUND_NUM = re.compile(r"\b(?:refund|return|give\s+back|deposit|advance|credit(?:\s+note)?|credit\s+note\s+for)\s+(?:of\s+)?(?:rs\.?\s*|₹\s*|inr\s*)?(\d[\d,]*)"
                         r"(?![\d,]*\s*%)\b", re.I)


def resolve_money(text: str) -> Optional[str]:
    """Whole rupees for a refund / deposit: "₹500", "500 rupees", "refund 500", "deposit 10,000", "5000 cash"."""
    t = _PAY_REF.sub(" ", _TOKEN.sub(" ", text or ""))
    m = _REFUND_NUM.search(t)
    if m and not resolve_amount(t):
        value = Decimal(m.group(1).replace(",", ""))
        return str(int(value)) if value > 0 else None
    return resolve_paid_amount(t)


_TRIAGE = (("red", r"\b(?:red|critical)\b"), ("yellow", r"\b(?:yellow|urgent)\b"), ("green", r"\b(?:green|minor)\b"),
           ("black", r"\bblack\b"))


def resolve_triage(text: str) -> Optional[str]:
    """The triage colour said ("triage red", "critical" = red); None for none or several."""
    found = {c for c, rx in _TRIAGE if re.search(rx, text or "", re.I)}
    return found.pop() if len(found) == 1 else None


_UNKNOWN = re.compile(r"\b(?:unknown|unidentified|unnamed|no\s+name|name\s+not\s+known|john\s+doe)\b", re.I)
_ER_NAME = re.compile(r"\b(?:name(?:\s+is)?|patient(?:\s+name)?|called)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")
_AGE = re.compile(r"\b(?:about|around|approx(?:imately)?|aged?)?\s*(\d{1,3})\s*(?:years?|yrs?|yr|y/o|year\s+old)\b"
                  r"|\b(?:age|aged|about|around)\s+(\d{1,3})\b", re.I)
_GENDER = (("female", r"\b(?:female|woman|lady|girl)\b"), ("male", r"\b(?:male|man|boy|gentleman)\b"))
_ARRIVAL = (("ambulance", r"\bambulance|108\b"), ("police", r"\bpolice\b"), ("transfer", r"\b(?:transfer(?:red)?|referred)\b"))


def resolve_unknown(text: str) -> Optional[str]:
    return "yes" if _UNKNOWN.search(text or "") else None


def resolve_er_name(text: str) -> Optional[str]:
    """An ER patient's name only when said as a name ("patient Ravi Kumar", "name is Ravi"), casing kept."""
    m = _ER_NAME.search(text or "")
    return m.group(1) if m and m.group(1).lower() not in {"unknown", "male", "female", "triage"} else None


def resolve_age(text: str) -> Optional[int]:
    m = _AGE.search(text or "")
    age = int(m.group(1) or m.group(2)) if m else None
    return age if age is not None and 0 <= age <= 130 else None


def resolve_gender(text: str) -> Optional[str]:
    found = {g for g, rx in _GENDER if re.search(rx, text or "", re.I)}
    return found.pop() if len(found) == 1 else None


def resolve_arrival(text: str) -> str:
    return next((mode for mode, rx in _ARRIVAL if re.search(rx, text or "", re.I)), "walk_in")


_COMPLAINT = re.compile(r"\b(?:with|complaints?(?:\s+of)?|c/o|case\s+of|presenting\s+with|came\s+with|for)\s+"
                        r"(?!(?:dr\.?|doctor|bed|ambulance|police|triage)\b)(.+?)"
                        r"(?=\s+(?:triage|by\s+ambulance|in\s+ambulance|brought|bp|pulse|spo2|sats?|temp|"
                        r"under|dr\.?|doctor)\b|[,;]|\.(?:\s|$)|$)", re.I)
_EMERGENCY_HEAD = re.compile(r"^\s*(?:new\s+)?(?:emergency|casualty|er)\s*(?:case|patient|arrival|registration)?\s*"
                             r"[:,-]?\s*(.+?)(?=\s*[,;]|\s+(?:triage|by\s+ambulance|brought|unknown|patient|name)\b|$)",
                             re.I)


def resolve_complaint(text: str) -> Optional[str]:
    """The chief complaint in the staff member's words ("with chest pain", "emergency: road accident, ...")."""
    m = _COMPLAINT.search(text or "") or _EMERGENCY_HEAD.search(text or "")
    found = " ".join(m.group(1).split()).strip(" .") if m else ""
    return found if 2 <= len(found) <= 300 else None


_OT_PROC = re.compile(r"\b(?:book|schedule|fix|plan)\s+(?:an?\s+|the\s+)?(.+?)(?=\s+(?:surgery|operation|for|on|in|at|"
                      r"with|under|tomorrow|today|bed|from|ot|theatre|theater)\b|[,;]|$)", re.I)
_OT_NOISE = {"surgery", "operation", "ot", "theatre", "theater", "a", "an", "the", "case", "slot"}
_THEATRE = re.compile(r"\b(?:ot|theatre|theater|operation\s+theatre)\s*(?:no\.?|number|#)?\s*([a-z]?\d{1,2}[a-z]?|[a-z])\b",
                      re.I)


def resolve_procedure(text: str) -> Optional[str]:
    """The procedure booked ("book knee replacement for bed 12 ..." -> "knee replacement"), as said."""
    m = _OT_PROC.search(text or "")
    words = [w for w in (m.group(1).split() if m else []) if w.lower() not in _OT_NOISE]
    name = " ".join(words)
    return name if 2 <= len(name) <= 200 else None


def resolve_theatre(text: str) -> Optional[str]:
    """The theatre said ("OT 1", "theatre 2"), upper-cased."""
    m = _THEATRE.search(text or "")
    return m.group(1).upper() if m else None


_NOT_INDICATION = re.compile(r"\d|\b(?:tomorrow|today|bed|ot|theatre|theater|am|pm|morning|evening)\b", re.I)


def resolve_indication(text: str) -> Optional[str]:
    """Why the surgery is done, the last "for ..." said ("... for acute appendicitis"); never a patient, bed or time."""
    found = [" ".join(m.group(1).split()).strip(" .") for m in _REASON.finditer(text or "")]
    found = [f for f in found if not _NOT_INDICATION.search(f)]
    return found[-1] if found and 2 <= len(found[-1]) <= 300 else None


# ── Voice-first batch 4: admission details, OT status, staff logins, branches ──

_PAYER = (("government_scheme", r"\b(?:government|govt|scheme|aarogyasri|arogyasri|ayushman|pmjay|cghs|echs|esi)\b"),
          ("insurance", r"\b(?:insurance|insured|tpa|cashless|mediclaim)\b"),
          ("corporate", r"\bcorporate\b"), ("self", r"\b(?:self[\s-]?pay(?:ing)?|cash\s+patient|own\s+pay(?:ment)?)\b"))
_PAYER_NAME = re.compile(r"\b(?:insurance|insurer|tpa|corporate|company|scheme|government\s+scheme)\s+(?:is\s+|with\s+|from\s+)?"
                         r"(?!policy|number|no\b|cashless)([A-Z][A-Za-z&.]*(?:\s+[A-Z][A-Za-z&.]*){0,3})")
_POLICY = re.compile(r"\b(?:policy|card|member\s+id|uhid)\s*(?:no\.?|number|#|is|:)?\s*([A-Za-z0-9/-]{4,60})\b", re.I)
_ATTENDANT = re.compile(r"\battendant(?:\s+is|\s+name\s+is|:)?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")
_RELATION = re.compile(r"\b(son|daughter|wife|husband|mother|father|brother|sister|son-in-law|daughter-in-law|"
                       r"friend|relative|neighbour|neighbor|guardian)\b", re.I)
_DISCHARGE_ON = re.compile(r"\b(?:expected\s+discharge|discharge\s+date|discharge\s+on|going\s+home)\b(.*)$", re.I)


def resolve_admission_patch(text: str, now=None) -> Optional[dict]:
    """The administrative details of a stay said in one breath: payer (+ name, policy), attendant (+ relation,
    phone), expected discharge date. Clinical fields are never read from speech. None when nothing is said."""
    t, out = text or "", {}
    payer = next((p for p, rx in _PAYER if re.search(rx, t, re.I)), None)
    if payer:
        out["payer_type"] = payer
        m = _PAYER_NAME.search(t)
        if m and payer != "self":
            out["payer_name"] = m.group(1).strip()
        m = _POLICY.search(t)
        if m and payer != "self":
            out["policy_number"] = m.group(1).upper()
    m = _ATTENDANT.search(t)
    if m:
        out["attendant_name"] = m.group(1)
        rel = _RELATION.search(t[m.end():] or "") or _RELATION.search(t)
        if rel:
            out["attendant_relation"] = rel.group(1).lower()
        phone = resolve_phone(t)
        if phone:
            out["attendant_phone"] = phone
    m = _DISCHARGE_ON.search(t)
    if m:
        d = resolve_date(m.group(1), now=now)
        if d:
            out["expected_discharge_on"] = d.isoformat()
    return out or None


_OT_ACTIONS = (("cancel", r"\bcancel(?:led)?\b"), ("postpone", r"\b(?:postpone(?:d)?|defer(?:red)?|push\s+(?:it\s+)?to\s+later)\b"),
               ("recovery", r"\b(?:recovery|out\s+of\s+(?:the\s+)?(?:ot|theatre|theater)|surgery\s+over)\b"),
               ("start", r"\b(?:start(?:ed)?|begin|began|in\s+(?:the\s+)?(?:ot|theatre|theater)|wheeled\s+in|"
                         r"shift(?:ed)?\s+to\s+(?:the\s+)?(?:ot|theatre|theater)|taken\s+(?:in)?to\s+(?:the\s+)?(?:ot|theatre))\b"))


def resolve_ot_action(text: str) -> Optional[str]:
    """start | recovery | cancel | postpone, the first one said in that order of weight."""
    return next((a for a, rx in _OT_ACTIONS if re.search(rx, text or "", re.I)), None)


_STAFF_NOISE = {"deactivate", "activate", "reactivate", "disable", "enable", "switch", "off", "on", "turn", "staff",
                "login", "logins", "account", "user", "make", "as", "a", "an", "the", "role", "to", "change", "his",
                "her", "block", "unblock", "remove", "access", "set", "into", "from", "now", "please", "s", "is",
                "bring", "back", "restore", "again", "active", "inactive", "member", "kriya", "new", "give"}


def resolve_staff_target(text: str) -> Optional[str]:
    """Whose login: the words left once instruction words and role names are removed (lower-cased search)."""
    t = (text or "").lower().replace("’", "'")
    for words, _ in _STAFF_ROLES:
        t = re.sub(rf"\b{words}\b", " ", t)
    words = [w for w in re.findall(r"[a-z]+", re.sub(r"'s\b", "", t)) if w not in _STAFF_NOISE]
    name = " ".join(words)
    return name if 2 <= len(name) <= 60 else None


_NEW_BRANCH = re.compile(r"\b(?:add|new|create|open|register)\s+(?:a\s+)?(?:new\s+)?branch\s+(?:at|in|called|named|for)?\s*"
                         r"([A-Za-z][A-Za-z .'-]{1,40}?)(?=\s*(?:[,.;]|$|\b(?:with|phone|address|landmark)\b))"
                         r"|\b(?:add|new|create|open)\s+(?:a\s+)?(?:new\s+)?([A-Za-z][A-Za-z.'-]{1,40})\s+branch\b", re.I)
_RENAME_BRANCH = re.compile(r"\brename\s+(?:the\s+)?(.+?)\s+branch\s+(?:to|as)\s+([A-Za-z][A-Za-z .'-]{1,40}?)\s*[.]?$"
                            r"|\bchange\s+(?:the\s+)?(.+?)\s+branch(?:'s)?\s+name\s+to\s+([A-Za-z][A-Za-z .'-]{1,40}?)\s*[.]?$",
                            re.I)


def resolve_new_branch(text: str) -> Optional[str]:
    """A new branch's locality ("add a branch at Gajuwaka" -> "Gajuwaka"), casing kept."""
    m = _NEW_BRANCH.search(text or "")
    name = " ".join((m.group(1) or m.group(2) or "").split()) if m else ""
    return name if 2 <= len(name) <= 40 and name.lower() not in {"new", "a", "the"} else None


def resolve_branch_rename(text: str) -> Optional[list]:
    """[old branch search words, new name] for "rename Gajuwaka branch to Gajuwaka Main"."""
    m = _RENAME_BRANCH.search(text or "")
    if not m:
        return None
    old, new = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    old, new = " ".join(old.lower().split()), " ".join(new.split())
    return [old, new] if 2 <= len(old) <= 60 and 2 <= len(new) <= 40 else None


_OT_STATUS_NOISE = {"start", "started", "begin", "began", "surgery", "surgeries", "operation", "ot", "theatre", "theater",
                    "shift", "shifted", "shifting", "to", "into", "in", "out", "of", "the", "recovery", "cancel",
                    "cancelled", "postpone", "postponed", "defer", "deferred", "for", "patient", "s", "is", "was",
                    "now", "wheeled", "taken", "over", "today", "tomorrow", "push", "it", "later", "please", "mark"}


def resolve_ot_target(text: str) -> Optional[str]:
    """Whose surgery: "bed:12" or the patient's name words (lower-cased), never the reason or the OT words."""
    t = _WHY.split(text or "", maxsplit=1)[0]
    bed = resolve_bed(t)
    if bed:
        return f"bed:{bed}"
    words = [w for w in re.findall(r"[a-z]+", re.sub(r"'s\b", "", t.lower())) if w not in _OT_STATUS_NOISE]
    name = " ".join(words)
    return name if 2 <= len(name) <= 40 else None


_WHY_SAID = re.compile(r"\b(?:because(?:\s+of)?|due\s+to|reason(?:\s+is)?|since|as\s+the)\s*[:,-]?\s+(.+?)"
                       r"(?=\s*,\s*(?:refund|pay|paid|by|in|via|through|utr)\b|\s*[.]?$)", re.I)


def resolve_why(text: str) -> Optional[str]:
    """A reason only when said as one ("because the test was not done", "reason: duplicate bill"): never the
    words after "for", which name the patient in "void the bill for Ravi Kumar". 3..300 chars."""
    m = _WHY_SAID.search(text or "")
    reason = " ".join(m.group(1).split()).strip(" .") if m else ""
    return reason if 3 <= len(reason) <= 300 else None


# ── Spoken numbers: speech-to-text often writes "six hundred", "one twenty over eighty", "token five" ──

_UNIT_WORDS = {w: i for i, w in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                                          "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
                                          "seventeen", "eighteen", "nineteen"))}
_UNIT_WORDS["oh"] = 0
_TENS_WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
               "eighty": 80, "ninety": 90}
_SCALE_WORDS = {"hundred": 100, "thousand": 1000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
                "crore": 10_000_000, "crores": 10_000_000}
_NUM_WORD = "|".join(sorted((*_UNIT_WORDS, *_TENS_WORDS, *_SCALE_WORDS), key=len, reverse=True))
_NUM_RUN = re.compile(rf"\b(?:(?:a|an)\s+(?=(?:hundred|thousand|lakh|lac|crore)\b))?(?:{_NUM_WORD})(?:(?:[\s-]+(?:and\s+)?)"
                      rf"(?:{_NUM_WORD}))*(?:\s+point(?:\s+(?:{'|'.join(_UNIT_WORDS)}))+)?\b", re.I)
_DIGIT_SCALE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(hundred|thousand|lakhs|lakh|lacs|lac|crores|crore)\b", re.I)
# a lone "five" is a number only next to a word that takes one ("token five", "bed five", "five rupees");
# "add one doctor" / "someone" stay words
_NUM_BEFORE = {"token", "bed", "ot", "theatre", "theater", "room", "ward", "pulse", "bp", "over", "by", "weight", "temp",
               "temperature", "sugar", "spo2", "sats", "from", "to", "till", "until", "at", "price", "fee", "fees",
               "number", "age", "aged", "about", "around", "paid", "refund", "deposit", "advance", "₹", "rs",
               "pain", "rr", "plus", "minus", "past"}
_NUM_AFTER = {"rupees", "rupee", "rs", "kg", "kgs", "kilos", "o'clock", "oclock", "am", "pm", "percent", "per", "years",
              "year", "yrs", "sittings", "days", "hours", "minutes", "mins", "cash", "upi", "card", "to", "over", "by"}


def _words_value(words: list) -> Optional[str]:
    words = [w for w in words if w not in ("and", "a", "an")]
    if "point" in words:
        i = words.index("point")
        whole = _words_value(words[:i]) if i else "0"
        frac = "".join(str(_UNIT_WORDS[w]) for w in words[i + 1:] if w in _UNIT_WORDS and _UNIT_WORDS[w] < 10)
        return f"{whole}.{frac}" if whole is not None and frac else whole
    vals = [("u", _UNIT_WORDS[w]) if w in _UNIT_WORDS else ("t", _TENS_WORDS[w]) if w in _TENS_WORDS
            else ("s", _SCALE_WORDS[w]) for w in words]
    kinds = [k for k, _ in vals]
    if "s" not in kinds:
        digits = [v for k, v in vals if k == "u" and v < 10]
        if len(digits) == len(vals) and len(vals) >= 2:          # "nine eight seven six ..." = digits
            return "".join(str(d) for d in digits)
        if len(vals) >= 2 and vals[0][0] == "u" and 1 <= vals[0][1] <= 9 and (vals[1][0] == "t" or 10 <= vals[1][1] <= 19):
            rest = _words_value(words[1:])                      # "one twenty" = 120, "one forty five" = 145
            return f"{vals[0][1]}{int(rest):02d}" if rest and int(rest) < 100 else None
    total = current = 0
    for k, v in vals:
        if k == "s":
            if v == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * v
                current = 0
        else:
            current += v
    return str(total + current)


def spoken_numbers(text: str) -> str:
    """Number words as digits, so every resolver reads "six hundred" like "600" (English, Hindi, Telugu), and a
    clock said in words as H:MM ("at ten thirty" -> "at 10:30"). Idempotent; only number words change, and a
    lone small number only beside a word that takes one."""
    t = _indic_numbers(text or "")
    t = _DIGIT_SCALE.sub(lambda m: _num(Decimal(m.group(1)) * _SCALE_WORDS[m.group(2).lower()]), t)
    if re.search(rf"\b(?:{_NUM_WORD})\b", t, re.I):
        out, last = [], 0
        for m in _NUM_RUN.finditer(t):
            words = [w.lower() for w in re.findall(r"[A-Za-z]+", m.group(0))]
            before = (re.findall(r"[\w₹']+", t[:m.start()].lower())[-1:] or [""])[0]
            after = (re.findall(r"[\w.']+", t[m.end():].lower())[:1] or [""])[0].rstrip(".,")
            lone = len([w for w in words if w not in ("a", "an", "and")]) == 1 and words[-1] in _UNIT_WORDS \
                and _UNIT_WORDS[words[-1]] <= 10
            if lone and before not in _NUM_BEFORE and after not in _NUM_AFTER:
                continue
            value = _clock_words(words, before, after) or _words_value(words)
            if value is None:
                continue
            out.append(t[last:m.start()] + value)
            last = m.end()
        t = "".join(out) + t[last:]
    return _HALF_PAST.sub(_half_past, t)


def _clock_words(words: list, before: str, after: str) -> Optional[str]:
    """"ten thirty" -> "10:30" when it is a time: said at/from a time, before am/pm/to, or an hour of 10-12
    (no amount is said "twelve thirty"). "BP one twenty" stays 120."""
    if len(words) < 2 or words[0] not in _UNIT_WORDS or not 1 <= _UNIT_WORDS[words[0]] <= 12 or before in _NOT_TIME_BEFORE:
        return None
    rest = [w for w in words[1:] if w != "and"]
    if not rest or rest[0] not in _TENS_WORDS and not 10 <= _UNIT_WORDS.get(rest[0], 0) <= 19 and rest[0] != "oh":
        return None
    minutes = _words_value(rest)
    hour = _UNIT_WORDS[words[0]]
    if minutes is None or not 1 <= int(minutes) <= 59:
        return None
    if before in _TIME_WORD_BEFORE or after in _TIME_WORD_AFTER or hour >= 10:
        return f"{hour}:{int(minutes):02d}"
    return None


def _half_past(m: re.Match) -> str:
    if m.group(3):
        hour = int(m.group(3))
        return f"{(hour - 2) % 12 + 1}:45"
    return f"{int(m.group(2))}:{'30' if m.group(1).lower() == 'half' else '15'}"


# ── Voice-first batch 5: doctor days / slot length, new services, credit notes, corporate companies ──

_SLOT_MIN = re.compile(r"\b(\d{1,3})\s*(?:-\s*)?(?:min|mins|minute|minutes)\b", re.I)
SLOT_LENGTHS = (5, 10, 15, 20, 30, 45, 60)


def resolve_slot_minutes(text: str) -> Optional[int]:
    """A slot length the booking grid supports ("15 minutes each", "slots of 20 min")."""
    m = _SLOT_MIN.search(text or "")
    return int(m.group(1)) if m and int(m.group(1)) in SLOT_LENGTHS else None


_SERVICE_NAME = re.compile(r"\b(?:add|new|create)\s+(?:a\s+)?(?:new\s+)?(?:service|procedure|charge|tariff)\s+"
                           r"(?:called\s+|named\s+|for\s+)?[\"“]?(.+?)[\"”]?(?=\s+(?:at|for|price|priced|costing|of|with|"
                           r"in|under|category|as)\b|\s*[,₹]|\s+rs\b|\s+\d|\s*$)", re.I)
_CATEGORIES = (("radiology", r"\b(?:x[\s-]?ray|scan|ultrasound|usg|ct|mri|radiology|echo|doppler)\b"),
               ("diagnostic", r"\b(?:ecg|eeg|test|diagnostic|holter|tmt|pft|spirometry)\b"),
               ("nursing", r"\b(?:nursing|injection|dressing|iv|catheter|nebuli[sz]ation)\b"),
               ("physiotherapy", r"\bphysio"), ("ambulance", r"\bambulance\b"),
               ("room_service", r"\b(?:room|bed|ward)\s+(?:charges?|rent)\b"), ("consultation", r"\bconsultation\b"))


def resolve_service_name(text: str) -> Optional[str]:
    """A new service's name as said ("add a new service ECG at 300" -> "ECG"), casing kept; 2..120 chars."""
    m = _SERVICE_NAME.search(text or "")
    name = " ".join(m.group(1).split()).strip(" .,") if m else ""
    return name if 2 <= len(name) <= 120 else None


def resolve_service_category(text: str) -> str:
    return next((c for c, rx in _CATEGORIES if re.search(rx, text or "", re.I)), "procedure")


_CN_REASONS = (("billing_error", r"\b(?:billing\s+error|wrong(?:ly)?\s+(?:bill|billed|charged)|charged\s+twice|"
                                 r"double\s+(?:charged|billed)|duplicate|mistake|error)\b"),
               ("cancellation", r"\b(?:cancel(?:led|lation)?|not\s+(?:done|performed|given)|did\s+not\s+happen)\b"),
               ("discount_post_issue", r"\b(?:discount|concession|waive|waived|reduction)\b"),
               ("service_deficiency", r"\b(?:deficien\w*|complain\w*|poor\s+service|bad\s+service|delay(?:ed)?)\b"))


def resolve_credit_reason(text: str) -> str:
    """The GST credit-note reason code for what was said; "other" (with the words kept as notes) otherwise."""
    return next((code for code, rx in _CN_REASONS if re.search(rx, text or "", re.I)), "other")


_COMPANY = re.compile(r"\b(?:add|new|create|register|onboard)\s+(?:a\s+)?(?:new\s+)?(?:corporate\s+)?(?:company|client)\s+"
                      r"(?:called\s+|named\s+)?([A-Za-z0-9][A-Za-z0-9 &.'()-]{1,118}?)\s*[.]?$"
                      r"|\b(?:add|new|create|register|onboard)\s+((?!(?:as|a|an|the|corporate)\b)[A-Z][A-Za-z0-9&.'-]*"
                      r"(?:\s+(?!(?:as|a|an|the|corporate)\b)[A-Z][A-Za-z0-9&.'-]*){0,4})\s+"
                      r"(?:as\s+(?:a\s+)?)?(?:corporate\s+)?(?:company|client)\b", re.I)


def resolve_company(text: str) -> Optional[str]:
    """A new corporate client's name as said, casing kept; 2..120 chars."""
    m = _COMPANY.search(text or "")
    name = " ".join((m.group(1) or m.group(2) or "").split()) if m else ""
    return name if 2 <= len(name) <= 120 else None


_AT_PRICE = re.compile(r"\b(?:at|for|price|priced|costing|cost|of|rate|charges?|rent|tariff)\s+(?:is\s+)?(?:rs\.?\s*|₹\s*|inr\s*)?(\d[\d,]*)\b(?!\s*%)",
                       re.I)


def resolve_service_price(text: str) -> Optional[list]:
    """A new service's price: "₹300", "300 rupees", or a number right after "at" / "for" / "price"."""
    found = resolve_price(text)
    if found:
        return found
    m = _AT_PRICE.search(text or "")
    value = int(m.group(1).replace(",", "")) if m else 0
    return ["amount", str(value)] if value > 0 else None


# ── Hindi / Telugu number words (Indic script, and Latin before a scale word) and clock minutes ──

_IN_UNITS = {
    1: ("एक", "ఒకటి", "ఒక", "ek", "okati"), 2: ("दो", "రెండు", "do", "rendu"), 3: ("तीन", "మూడు", "teen", "moodu", "mudu"),
    4: ("चार", "నాలుగు", "char", "chaar", "naalugu", "nalugu"), 5: ("पाँच", "पांच", "ఐదు", "అయిదు", "paanch", "panch", "aidu"),
    6: ("छह", "छः", "छे", "ఆరు", "chhe", "che", "aaru"), 7: ("सात", "ఏడు", "saat", "edu"),
    8: ("आठ", "ఎనిమిది", "aath", "enimidi"), 9: ("नौ", "తొమ్మిది", "nau", "tommidi"), 10: ("दस", "పది", "das", "padi"),
    11: ("ग्यारह", "పదకొండు"), 12: ("बारह", "పన్నెండు"), 13: ("तेरह", "పదమూడు"), 14: ("चौदह", "పద్నాలుగు"),
    15: ("पंद्रह", "पन्द्रह", "పదిహేను", "pandrah"), 16: ("सोलह", "పదహారు"), 17: ("सत्रह", "పదిహేడు"), 18: ("अठारह", "పద్దెనిమిది"),
    19: ("उन्नीस", "పందొమ్మిది"), 20: ("बीस", "ఇరవై", "bees"), 25: ("पच्चीस", "ఇరవై ఐదు"), 30: ("तीस", "ముప్పై", "tees"),
    40: ("चालीस", "నలభై"), 50: ("पचास", "యాభై", "pachaas", "pachas"), 60: ("साठ", "అరవై"), 70: ("सत्तर", "డెబ్బై"),
    80: ("अस्सी", "ఎనభై"), 90: ("नब्बे", "తొంభై"),
}
_IN_SCALES = {100: ("सौ", "వంద", "వందలు", "వందల", "నూరు", "sau", "so", "vanda", "vandalu", "vandala"),
              1000: ("हज़ार", "हजार", "వెయ్యి", "వేయి", "వేలు", "వేల", "hazaar", "hazar", "hajar", "veyyi", "velu", "vela"),
              100_000: ("लाख", "లక్ష", "లక్షలు", "లక్షల", "laksha", "lakshalu")}
_IN_HALF = {"डेढ़": 1.5, "डेढ": 1.5, "ढाई": 2.5, "dedh": 1.5, "dhai": 2.5, "ఒకటిన్నర": 1.5, "రెండున్నర": 2.5}
_IN_PLUS = {"साढ़े": 0.5, "साढे": 0.5, "sadhe": 0.5, "saade": 0.5, "सवा": 0.25, "sawa": 0.25, "पौने": -0.25, "paune": -0.25}
_IN_WORD = {w: v for v, ws in _IN_UNITS.items() for w in ws if " " not in w}
_IN_SCALE = {w: v for v, ws in _IN_SCALES.items() for w in ws}
# a Latin Hindi / Telugu word ("do", "teen", "das") is a number only right before a scale word ("do hazaar")
_LATIN = {w for w in (*_IN_WORD, *_IN_SCALE, *_IN_HALF, *_IN_PLUS) if w.isascii()}
_IN_CURRENCY = {"रुपये", "रुपए", "रुपया", "రూపాయలు", "రూపాయిలు", "రూపాయల", "rupees", "rs", "rupaye"}
_IN_BEFORE = {"टोकन", "token", "టోకెన్", "बेड", "bed", "బెడ్", "नंबर", "నంబర్"}


def _indic_numbers(text: str) -> str:
    toks, out, i = (text or "").split(" "), [], 0
    clean = [re.sub(r"[.,!?।]+$", "", t).lower() for t in toks]
    while i < len(toks):
        j, total, current, plus, has_scale, used = i, 0.0, 0.0, 0.0, False, []
        while j < len(toks):
            w = clean[j]
            nxt = clean[j + 1] if j + 1 < len(toks) else ""
            if w in _LATIN and w not in _IN_SCALE and nxt not in _IN_SCALE and not (w in _IN_PLUS and nxt in _IN_WORD):
                break
            if w in _IN_PLUS:
                plus += _IN_PLUS[w]
            elif w in _IN_HALF:
                current += _IN_HALF[w]
            elif w in _IN_WORD:
                current += _IN_WORD[w]
            elif w in _IN_SCALE and (current or plus or j == i):
                scale = _IN_SCALE[w]
                has_scale = True
                value = (current or 1) + plus
                if scale == 100:
                    current, plus = value * 100, 0.0
                else:
                    total, current, plus = total + value * scale, 0.0, 0.0
            else:
                break
            used.append(j)
            j += 1
        if not used:
            out.append(toks[i])
            i += 1
            continue
        value = total + current + plus
        lone = not has_scale and len(used) == 1 and value <= 10
        prev = clean[i - 1] if i else ""
        after = clean[j] if j < len(toks) else ""
        if (lone and prev not in _IN_BEFORE and after not in _IN_CURRENCY) or value != int(value):
            out.extend(toks[i:j])
        else:
            tail = toks[j - 1][len(clean[j - 1]):] if toks[j - 1].lower().startswith(clean[j - 1]) else ""
            out.append(str(int(value)) + tail)
        i = j
    return " ".join(out)


_HALF_PAST = re.compile(r"\b(half|quarter)\s+past\s+(\d{1,2})\b|\bquarter\s+to\s+(\d{1,2})\b", re.I)
_TIME_WORD_BEFORE = {"at", "from", "since", "around", "till", "until"}
_TIME_WORD_AFTER = {"am", "pm", "a.m.", "p.m.", "o'clock", "oclock", "to", "till", "until", "morning", "evening",
                    "tomorrow", "today"}
_NOT_TIME_BEFORE = {"bp", "pulse", "sugar", "over", "by", "weight", "temp", "temperature", "spo2", "sats", "price", "fee",
                    "fees", "paid", "refund", "deposit", "₹", "rs", "token", "bed", "rr", "rate"}


# ── Voice-first batch 6: IPD setup (wards, beds, bed tariffs) ──

_WARD_TYPES = (("nicu", r"\bnicu\b"), ("picu", r"\bpicu\b"), ("icu", r"\b(?:icu|intensive\s+care)\b"), ("hdu", r"\bhdu\b"),
               ("semi_private", r"\bsemi[\s-]?private\b"), ("private", r"\b(?:private|deluxe|suite)\b"),
               ("maternity", r"\b(?:maternity|labour|labor|obstetric|post[\s-]?natal)\b"),
               ("isolation", r"\bisolation\b"), ("daycare", r"\bday\s?care\b"), ("emergency", r"\b(?:emergency|casualty)\b"),
               ("general", r"\bgeneral\b"))
_NEW_WARD = re.compile(r"\b(?:add|new|create|open)\s+(?:a\s+)?(?:new\s+)?ward\s+(?:called\s+|named\s+)?(.+?)"
                       r"(?=\s*(?:,|\.|$|\b(?:of\s+type|type|as|for|with)\b))"
                       r"|\b(?:add|new|create|open)\s+(?:a\s+)?(?:new\s+)?(.+?)\s+ward\b", re.I)


def resolve_ward_type(text: str) -> str:
    return next((w for w, rx in _WARD_TYPES if re.search(rx, text or "", re.I)), "general")


def resolve_new_ward(text: str) -> Optional[str]:
    """A new ward's name as said ("add a new ward Maternity B" -> "Maternity B"), casing kept."""
    m = _NEW_WARD.search(text or "")
    name = " ".join((m.group(1) or m.group(2) or "").split()).strip(" .,") if m else ""
    return name if 1 <= len(name) <= 80 and name.lower() not in {"a", "new", "the"} else None


_BED_RANGE = re.compile(r"\bbeds?\s+(?:no\.?\s+|numbers?\s+)?([a-z]{0,4}-?)(\d{1,4})\s*(?:to|-|–|till|through)\s*"
                        r"(?:[a-z]{0,4}-?)?(\d{1,4})\b", re.I)
_BED_COUNT = re.compile(r"\b(\d{1,3})\s+(?:new\s+|more\s+)?(?:[a-z-]+\s+){0,2}?beds\b", re.I)
_TO_WARD = re.compile(r"\b(?:to|in|into|for)\s+(?:the\s+)?([a-z][a-z0-9 -]{0,24}?)\s+ward\b|\b(?:to|in|into)\s+(?:the\s+)?"
                      r"(icu|nicu|picu|hdu|ccu)\b", re.I)


def resolve_bed_plan(text: str) -> Optional[dict]:
    """New beds said as a range ("beds 101 to 110", "beds G1 to G10") or a count ("10 beds")."""
    t = text or ""
    m = _BED_RANGE.search(t)
    if m:
        lo, hi = int(m.group(2)), int(m.group(3))
        return {"prefix": m.group(1).upper(), "from": lo, "to": hi} if lo <= hi and hi - lo < 200 else None
    m = _BED_COUNT.search(t)
    return {"count": int(m.group(1))} if m and 1 <= int(m.group(1)) <= 200 else None


def resolve_ward_term(text: str) -> Optional[str]:
    """Which ward ("to the general ward", "in ICU"), lower-cased search words."""
    m = _TO_WARD.search(text or "")
    return " ".join((m.group(1) or m.group(2)).lower().split()) if m else None


_TARIFF_CAT = re.compile(r"\b(general|semi[\s-]?private|private|icu|nicu|picu|hdu|deluxe|suite|isolation|daycare|"
                         r"maternity|twin[\s-]?sharing|sharing|economy)\b(?=.*\b(?:beds?|tariffs?|rooms?|charges?|rent)\b)", re.I)


def resolve_bed_category(text: str) -> Optional[str]:
    """The bed category a tariff is for ("ICU bed tariff" -> "icu", "semi private room" -> "semi_private")."""
    m = _TARIFF_CAT.search(text or "")
    return re.sub(r"[\s-]+", "_", m.group(1).lower()) if m else None


# ── Voice-first batch 7: OPD / IPD settings, insurance payers, claims ──

def resolve_opd_change(text: str) -> Optional[dict]:
    """OPD desk settings said in words. Never the UPI payee or GSTIN (a typo there moves money / tax)."""
    t, out = (text or "").lower(), {}
    if re.search(r"\bvitals?\b", t):
        if re.search(r"\b(?:skip|optional|not\s+(?:required|needed|mandatory)|no\s+need|off|don'?t\s+need)\b", t):
            out["vitals_required"] = False
        elif re.search(r"\b(?:mandatory|compulsory|required|must|always)\b", t):
            out["vitals_required"] = True
    if re.search(r"\b(?:bill(?:ing)?|pay|payment)\b", t) and re.search(r"\bconsult", t):
        if re.search(r"\b(?:after|post)\s+(?:the\s+)?consult", t):
            out["billing_after_consult"] = True
        elif re.search(r"\b(?:before|pre)\s+(?:the\s+)?consult|\b(?:pay|bill)\s+first\b", t):
            out["billing_after_consult"] = False
    if re.search(r"\bauto(?:matic(?:ally)?)?[\s-]?open", t) and re.search(r"\bshift", t):
        state = resolve_on_off(re.sub(r"\bopen\b", " ", t))
        if state:
            out["auto_open_shift"] = state == "on"
    m = re.search(r"\bafter\s+check[\s-]?in\b.*?\b(vitals|waiting|registered|registration)\b", t)
    if m:
        out["after_checkin_stage"] = {"vitals": "vitals_pending", "registration": "registered"}.get(m.group(1), m.group(1))
    if re.search(r"\b(?:accept|payment\s+modes?|take\s+payments?|allow)\b", t):
        modes = [m for m, rx in _PAY_MODES if re.search(rx, t, re.I)]
        if modes:
            out["payment_modes"] = [m for m in ("cash", "upi", "card") if m in modes]
    return out or None


_ROOM = re.compile(r"\b(?:room|cabin|chamber|consultation\s+room|opd)\s*(?:no\.?|number|#)?\s*([A-Za-z]?\d{1,3}[A-Za-z]?|[A-Z])\b",
                   re.I)


def resolve_room(text: str) -> Optional[str]:
    m = _ROOM.search(text or "")
    return f"Room {m.group(1).upper()}" if m else None


def resolve_ipd_change(text: str) -> Optional[dict]:
    """IPD settings said in words: the discharge cut-off hour and how long a bed is held."""
    t, out = (text or "").lower(), {}
    m = re.search(r"\bdischarge\s+cut[\s-]?off\b.*?\b(\d{1,2})(?::00)?\s*(am|pm|noon)?\b", t)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(2) in ("pm", "noon") else 0) if m.group(2) else int(m.group(1))
        if m.group(2) == "noon":
            hour = 12
        if 0 <= hour <= 23:
            out["discharge_cutoff_hour"] = hour
    m = re.search(r"\b(?:hold|reserve|reservation|keep)\b.*?\b(\d{1,4})\s*(min|mins|minutes|hours?|hrs?)\b", t)
    if m:
        minutes = int(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
        if 5 <= minutes <= 1440:
            out["reservation_minutes"] = minutes
    return out or None


_PAYER_KIND = (("government_scheme", r"\b(?:government|govt|scheme)\b"), ("tpa", r"\btpa\b"),
               ("corporate", r"\bcorporate\b"), ("private_insurance", r"\b(?:insurer|insurance|insurance\s+company)\b"))
_PAYER_NEW = re.compile(r"\b(?:add|new|create|register|onboard)\s+(?:a\s+)?(?:new\s+)?(?:tpa|insurer|insurance(?:\s+company)?|"
                        r"payer|government\s+scheme|scheme|corporate\s+payer)\s+(?:called\s+|named\s+)?"
                        r"([A-Za-z0-9][A-Za-z0-9 &.'()-]{1,100}?)\s*[.]?$", re.I)


def resolve_payer_kind(text: str) -> str:
    return next((k for k, rx in _PAYER_KIND if re.search(rx, text or "", re.I)), "private_insurance")


def resolve_payer_name(text: str) -> Optional[str]:
    m = _PAYER_NEW.search(text or "")
    name = " ".join(m.group(1).split()) if m else ""
    return name if 2 <= len(name) <= 150 else None


_CLAIM_NO = re.compile(r"\bclaim\s*(?:no\.?|number|#)?\s*([A-Z]{2,6}-?\d[\w-]*)", re.I)


def resolve_claim_target(text: str) -> Optional[str]:
    """"claim:CLM-0012" when the claim number is said, else the patient's bed or name like any inpatient."""
    m = _CLAIM_NO.search(text or "")
    if m:
        return f"claim:{m.group(1).upper()}"
    m = re.search(r"\b(?!(?:on|for|of|in|to|with|at|about|withdraw|mark|cancel|close|settle|record|update|query)\b)([A-Za-z]+(?:\s+[A-Z][a-z]+)?)'s\s+(?:insurance\s+)?claim\b",
                  text or "")
    if m and m.group(1).lower() not in {"the", "patient", "insurer", "tpa"}:
        return m.group(1).lower()
    t = re.sub(r"\b(?:claim|claims|settlement|settled|tds|utr|neft|received|query|queried|raised|process|processing|"
               r"withdraw|withdrawn|mark|insurer|insurance|tpa|star|health|from|of|the|for|in|on|as|is)\b", " ",
               _WHY.split(text or "", maxsplit=1)[0].split(":")[0], flags=re.I)
    return resolve_patient_target(t)


_TDS = re.compile(r"\btds\s+(?:of\s+|is\s+|deducted\s+)?(?:rs\.?\s*|₹\s*)?(\d[\d,]*)", re.I)
_UTR = re.compile(r"\b(?:utr|neft|rtgs|ref(?:erence)?|transaction(?:\s+id)?)\s*(?:no\.?|number|id|is|:|#)?\s*([a-z0-9]{4,40})\b",
                  re.I)


def resolve_tds(text: str) -> Optional[str]:
    m = _TDS.search(text or "")
    return str(int(m.group(1).replace(",", ""))) if m else None


def resolve_utr(text: str) -> Optional[str]:
    m = _UTR.search(text or "")
    return m.group(1).upper() if m else None


def resolve_received(text: str) -> Optional[str]:
    """Rupees the insurer paid ("paid 45,000", "received 45000", "settled 45,000"), never the TDS or UTR."""
    t = _UTR.sub(" ", _TDS.sub(" ", _CLAIM_NO.sub(" ", text or "")))
    m = re.search(r"\b(?:paid|received|settled|credited|sent|transferred)\s+(?:rs\.?\s*|₹\s*)?(\d[\d,]*)", t, re.I)
    if m:
        return str(int(m.group(1).replace(",", "")))
    found = resolve_amount(t)
    return str(int(found[1])) if found and found[0] == "amount" else None


_CLAIM_ACTIONS = (("withdraw", r"\bwithdraw(?:n)?\b"), ("query", r"\b(?:query|queried|queries|asked\s+for|wants|needs)\b"),
                  ("in_process", r"\b(?:in\s+process|processing|under\s+process|in\s+progress)\b"))


def resolve_claim_action(text: str) -> Optional[str]:
    return next((a for a, rx in _CLAIM_ACTIONS if re.search(rx, text or "", re.I)), None)


def resolve_claim_note(text: str) -> Optional[str]:
    """The insurer's query or the withdraw reason in the staff member's words: after "because" / a colon /
    "asked for"."""
    why = resolve_why(text)
    if why:
        return why
    m = re.search(r"(?::\s*|\b(?:asked\s+for|wants|needs|query(?:\s+is)?|queried\s+for)\s+)(.{3,1000}?)\s*[.]?$", text or "",
                  re.I)
    return " ".join(m.group(1).split()) if m else None


# ── Voice-first batch 8: IPD discounts, packages, insurance pre-authorisations ──

_DISC_NUM = re.compile(r"\b(?:discount|concession|waive|waiver|reduce|reduction|less)\s+(?:of\s+)?(?:rs\.?\s*|₹\s*)?"
                       r"(\d[\d,]*)\b(?!\s*%)|\b(\d[\d,]*)\s*(?:rupees?\s+|rs\.?\s+)?(?:discount|concession|off)\b", re.I)


def resolve_discount_amount(text: str) -> Optional[str]:
    """Whole rupees of a discount ("discount of 2000", "2000 rupees off", "₹2,000 concession"); never a percent."""
    found = resolve_amount(text)
    if found and found[0] == "amount":
        return str(int(found[1]))
    m = _DISC_NUM.search(text or "")
    return str(int((m.group(1) or m.group(2)).replace(",", ""))) if m else None


def resolve_decision(text: str) -> Optional[str]:
    t = text or ""
    if re.search(r"\b(?:reject|rejected|deny|denied|decline|declined|refuse|refused)\b", t, re.I):
        return "rejected"
    if re.search(r"\b(?:approve|approved|sanction|sanctioned|accept|ok)\b", t, re.I):
        return "approved"
    return "cancelled" if re.search(r"\b(?:cancel|withdraw)\b", t, re.I) else None


_PACKAGE = re.compile(r"\b(?:on|to|under|with|in|into|apply|put)\s+(?:the\s+|a\s+)?([a-z0-9][a-z0-9 &/-]{1,80}?)\s+package\b"
                      r"|\bpackage\s+([a-z0-9][a-z0-9 &/-]{1,80}?)\s*(?:to|for|on)\b", re.I)


def resolve_package_term(text: str) -> Optional[str]:
    m = _PACKAGE.search(text or "")
    words = [w for w in ((m.group(1) or m.group(2)) if m else "").lower().split()
             if w not in {"bed", "the", "a", "patient", "stay", "his", "her", "on", "to", "under", "with", "in", "into",
                          "put", "apply"} and not w.isdigit()]
    term = " ".join(words)
    return term if 2 <= len(term) <= 80 else None


_PREAUTH_NO = re.compile(r"\b(?:pre[\s-]?auth(?:ori[sz]ation)?|pa)\s*(?:no\.?|number|#)?\s*([A-Z]{2,6}-?\d[\w-]*)", re.I)


def resolve_preauth_target(text: str) -> Optional[str]:
    """"preauth:PA-0007" when the number is said, else the inpatient's bed or name (as for a claim)."""
    m = _PREAUTH_NO.search(text or "")
    if m:
        return f"preauth:{m.group(1).upper()}"
    m = re.search(r"\b(?!(?:on|for|of|in|to|with|at|about|submit|approve|reject|mark)\b)([A-Za-z]+(?:\s+[A-Z][a-z]+)?)'s\s+"
                  r"pre[\s-]?auth", text or "")
    if m:
        return m.group(1).lower()
    t = re.sub(r"\b(?:pre[\s-]?auth(?:ori[sz]ation)?|approved|approval|approve|reject(?:ed)?|query|queried|raised|submit|"
               r"submitted|enhancement|request(?:ed)?|denied|deny|ref(?:erence)?|insurer|insurance|tpa|star|health|for|"
               r"of|the|on|has|have|is|as)\b", " ", _WHY.split(text or "", maxsplit=1)[0].split(":")[0], flags=re.I)
    return resolve_patient_target(t)


_APPROVED = re.compile(r"\bapproved\s+(?:a\s+total\s+of\s+|total\s+|for\s+)?(?:rs\.?\s*|₹\s*)?(\d[\d,]*)\b", re.I)
_APPROVAL_REF = re.compile(r"\b(?:approval\s+(?:ref(?:erence)?|no\.?|number|code|id|letter)|ref(?:erence)?\s*(?:no\.?|number)?|"
                           r"auth(?:ori[sz]ation)?\s+(?:no\.?|number|code))\s*(?:is|:|#)?\s*([A-Za-z0-9/-]{3,40})\b", re.I)
_ENHANCE = re.compile(r"\benhancement\s+(?:of\s+|for\s+)?(?:rs\.?\s*|₹\s*)?(\d[\d,]*)\b|\b(\d[\d,]*)\s*(?:rupees?\s+)?"
                      r"(?:more|extra|additional)\b", re.I)


def resolve_approved_amount(text: str) -> Optional[str]:
    t = _APPROVAL_REF.sub(" ", text or "")
    m = _APPROVED.search(t)
    if m:
        return str(int(m.group(1).replace(",", "")))
    found = resolve_amount(t)
    return str(int(found[1])) if found and found[0] == "amount" else None


def resolve_approval_ref(text: str) -> Optional[str]:
    m = _APPROVAL_REF.search(text or "")
    return m.group(1).upper() if m else None


def resolve_enhancement(text: str) -> Optional[str]:
    m = _ENHANCE.search(text or "")
    return str(int((m.group(1) or m.group(2)).replace(",", ""))) if m else None


_PREAUTH_REPLY = (("enhancement_denied", r"\benhancement\s+(?:denied|rejected|refused|declined)\b"),
                  ("rejected", r"\b(?:reject(?:ed)?|denied|declined|refused)\b"),
                  ("query_raised", r"\b(?:query|queried|queries|asked\s+for|wants|needs)\b"))


def resolve_preauth_reply(text: str) -> Optional[str]:
    return next((d for d, rx in _PREAUTH_REPLY if re.search(rx, text or "", re.I)), None)


_STAY_NOUNS = r"(?:discount|concession|package|bill|stay|deposit|pre[\s-]?auth|claim)"


def resolve_stay_target(text: str) -> Optional[str]:
    """Whose stay a finance step is for: "bed:12", "Sita's discount" -> "sita", "... to / for Sita" -> "sita"."""
    t = _WHY.split(text or "", maxsplit=1)[0]
    bed = resolve_admit_bed(t)   # one bed here: "discount to bed 12" names the stay, not a move
    if bed:
        return f"bed:{bed}"
    m = re.search(r"\b(?!(?:on|for|of|in|to|with|at|about|approve|reject|cancel|apply|put|give)\b)"
                  r"([A-Za-z]+(?:\s+[A-Z][a-z]+)?)'s\s+" + _STAY_NOUNS, t)
    if m:
        return m.group(1).lower()
    m = re.search(r"\b(?:to|for)\s+(?!(?:the|a|bed|patient|him|her)\b)([A-Za-z]+(?:\s+[A-Z][a-z]+)?)"
                  r"(?=\s*(?:[,.]|$|\d|\b(?:by|from|under|with|because|reference|ref|letter|po|for|in|on|at|of|"
                  r"approval|pre)\b))", t.strip())
    if m:
        return m.group(1).lower()
    words = [w for w in re.findall(r"[a-z]+", re.sub(r"'s\b", "", t.lower()))
             if w not in _QUEUE_NOISE | _MONEY_WORDS | {"discount", "concession", "package", "approve", "approved", "reject",
                                                       "rejected", "apply", "put", "give", "off", "rupees", "of", "the"}]
    name = " ".join(words)
    return name if 2 <= len(name) <= 40 else None


def resolve_preauth_action(text: str) -> Optional[str]:
    t = text or ""
    if re.search(r"\benhancement\b|\b(?:more|extra|additional)\b", t, re.I):
        return "request_enhancement"
    return "submit" if re.search(r"\b(?:submit|send|file)\b", t, re.I) else None


# ── Voice-first batch 9: package create, credit authorisation, new pre-auth, disallowances ──

_PKG_NEW = re.compile(r"\b(?:create|add|new|make)\s+(?:a\s+)?(?:new\s+)?(?:ipd\s+)?package\s+(?:called\s+|named\s+|for\s+)?"
                      r"([A-Za-z0-9][A-Za-z0-9 &/'-]{1,80}?)(?=\s+(?:at|for|of|price|priced|costing|covering|covers|with|"
                      r"includes?|including|up\s+to)\b|\s*[,₹]|\s+rs\b|\s+\d|\s*$)", re.I)
_PKG_CATS = (("bed", r"\b(?:bed|room|stay)\b"), ("nursing", r"\bnursing\b"), ("doctor_visit", r"\b(?:doctor|visits?|consult\w*)\b"),
             ("procedure", r"\b(?:procedure|surgery|operation|ot)\b"), ("investigation", r"\b(?:investigations?|tests?|lab|scans?)\b"),
             ("pharmacy", r"\b(?:pharmacy|medicines?|drugs?)\b"), ("consumable", r"\bconsumables?\b"))
_PKG_DAYS = re.compile(r"\b(?:up\s+to|for|max(?:imum)?)\s+(\d{1,3})\s*days?\b|\b(\d{1,3})[\s-]days?\b", re.I)


def resolve_package_name(text: str) -> Optional[str]:
    m = _PKG_NEW.search(text or "")
    name = " ".join(m.group(1).split()).strip(" .,") if m else ""
    return name if 2 <= len(name) <= 200 else None


def resolve_package_cover(text: str) -> Optional[list]:
    """What a new package covers, from words after "covering" / "includes" (bed, nursing, doctor visits ...)."""
    m = re.search(r"\b(?:covering|covers|includes?|including|with)\b(.*)$", text or "", re.I)
    part = m.group(1) if m else ""
    found = [c for c, rx in _PKG_CATS if re.search(rx, part, re.I)]
    return found or None


def resolve_package_days(text: str) -> Optional[int]:
    m = _PKG_DAYS.search(text or "")
    days = int(m.group(1) or m.group(2)) if m else None
    return days if days and 1 <= days <= 365 else None


_CREDIT_FROM = re.compile(r"\b(?:from|by|payer|company)\s+([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,3})")
_REF_WORD = re.compile(r"\b(?:ref(?:erence)?|po|purchase\s+order|letter|authori[sz]ation|approval)\s*(?:no\.?|number|#|is|:)?\s*"
                       r"([A-Za-z0-9/-]{2,60})\b", re.I)


def resolve_credit_payer(text: str) -> Optional[str]:
    t = _REF_WORD.sub(" ", text or "")
    for m in _CREDIT_FROM.finditer(t):
        name = m.group(1).strip()
        if name.lower().split()[0] not in {"bed", "the", "patient", "ref", "reference", "po"}:
            return name
    return None


def resolve_credit_ref(text: str) -> Optional[str]:
    m = _REF_WORD.search(text or "")
    return m.group(1).upper() if m and m.group(1).lower() not in {"of", "for", "from", "number"} else None


def resolve_credit_amount(text: str) -> Optional[str]:
    t = _REF_WORD.sub(" ", text or "")
    found = resolve_amount(t)
    if found and found[0] == "amount":
        return str(int(found[1]))
    m = re.search(r"\bcredit\s+(?:of\s+|for\s+|limit\s+(?:of\s+)?)?(?:rs\.?\s*|₹\s*)?(\d[\d,]*)\b", t, re.I)
    return str(int(m.group(1).replace(",", ""))) if m else None


def resolve_preauth_amount(text: str) -> Optional[str]:
    """Rupees requested from the insurer ("pre-auth of 60,000", "60000 pre-auth", "₹60,000")."""
    found = resolve_amount(text)
    if found and found[0] == "amount":
        return str(int(found[1]))
    m = re.search(r"\bpre[\s-]?auth(?:ori[sz]ation)?\s+(?:of|for)\s+(?:rs\.?\s*|₹\s*)?(\d[\d,]*)\b"
                  r"|\b(\d[\d,]*)\s*(?:rupees?\s+)?pre[\s-]?auth", text or "", re.I)
    if m:
        return str(int((m.group(1) or m.group(2)).replace(",", "")))
    # a bare four-figure-or-more number that is not a bed, day count or phone ("pre-auth for Sita 45000")
    t = _BED.sub(" ", _PHONE_RUN.sub(" ", text or ""))
    m = re.search(r"(?<![\d.])(\d{1,3}(?:,\d{2,3})+|\d{4,7})(?![\d,]*\s*(?:days?|nights?|%))", t)
    return str(int(m.group(1).replace(",", ""))) if m else None


def resolve_stay_days(text: str) -> Optional[int]:
    m = re.search(r"\b(\d{1,3})\s*(?:days?|nights?)(?:\s+stay)?\b", text or "", re.I)
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 365 else None


def resolve_payer_term(text: str) -> Optional[str]:
    """An insurer named in passing ("... under Star Health", "with the Medi Assist policy"), lower-cased."""
    m = re.search(r"\b(?:under|with|from|via|through|on)\s+(?:the\s+)?([A-Z][A-Za-z&.'-]*(?:\s+[A-Z][A-Za-z&.'-]*){0,3})"
                  r"(?:\s+(?:policy|insurance|tpa|card))?\b", text or "")
    return m.group(1).lower() if m and m.group(1).lower() not in {"bed", "dr", "doctor"} else None


_DISALLOW = (("room_rent_proportionate", r"\broom\s+rent"), ("copay", r"\bco[\s-]?pay"),
             ("non_payable_item", r"\b(?:non[\s-]?payable|consumables?|not\s+payable)\b"),
             ("policy_limit", r"\b(?:sub[\s-]?limit|policy\s+limit|limit|capping|capped)\b"),
             ("excess_over_tariff", r"\b(?:tariff|excess|over\s+the\s+rate|package\s+rate)\b"),
             ("documentation_incomplete", r"\b(?:documents?|documentation|paperwork|papers)\b"),
             ("not_medically_necessary", r"\b(?:not\s+(?:medically\s+)?necessary|unnecessary|not\s+justified)\b"),
             ("repudiated", r"\b(?:repudiated|repudiation)\b"))


def resolve_disallow_code(text: str) -> str:
    return next((c for c, rx in _DISALLOW if re.search(rx, text or "", re.I)), "other")


def resolve_disallowed(text: str) -> Optional[str]:
    t = _CLAIM_NO.sub(" ", text or "")
    m = re.search(r"\b(?:disallowed|deducted|cut|rejected|denied|not\s+paid)\s+(?:rs\.?\s*|₹\s*)?(\d[\d,]*)\b"
                  r"|\b(\d[\d,]*)\s*(?:rupees?\s+)?(?:disallowed|deducted|cut|not\s+paid)\b", t, re.I)
    if m:
        return str(int((m.group(1) or m.group(2)).replace(",", "")))
    found = resolve_amount(t)
    return str(int(found[1])) if found and found[0] == "amount" else None
