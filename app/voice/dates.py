"""Spoken date / time normalisation (te / hi / en, native script and romanised).

Deterministic on purpose: "repu", "kal", "ellundi" must resolve identically
whether or not an LLM is reachable. All dates are IST (AGENTS.md rule 8).
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

MORNING, AFTERNOON, EVENING = "MORNING", "AFTERNOON", "EVENING"


def today_ist(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(IST)).astimezone(IST).date()


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


_TOKEN_EDGE = r"\s,.!?।"


def _pattern(w: str):
    """Matching rule per word:
    * ASCII -> whole word ("ent" must not match "appointment");
    * "=word" -> whole token, any script (short Indic words: "जी" must not
      match inside "कार्डियोलॉजी");
    * other Indic -> substring, because case suffixes attach (రేపటికి, कलको)."""
    if w.startswith("="):
        return re.compile(rf"(?:^|(?<=[{_TOKEN_EDGE}])){re.escape(w[1:])}(?=$|[{_TOKEN_EDGE}])")
    if w.isascii():
        return re.compile(rf"(?<![a-z]){re.escape(w)}(?![a-z])")
    return None


def has_any(text: str, words) -> bool:
    return first_index(text, words) >= 0


def first_index(text: str, words) -> int:
    """Position of the earliest match of any word, -1 if none."""
    best = -1
    for w in words:
        p = _pattern(w)
        if p is not None:
            m = p.search(text)
            i = m.start() if m else -1
        else:
            i = text.find(w)
        if i >= 0 and (best < 0 or i < best):
            best = i
    return best


def last_index(text: str, words) -> int:
    best = -1
    for w in words:
        p = _pattern(w)
        if p is not None:
            for m in p.finditer(text):
                best = max(best, m.start())
        else:
            best = max(best, text.rfind(w))
    return best


_DAY_AFTER = ("day after tomorrow", "ellundi", "ఎల్లుండి", "parso", "parson", "परसों", "परसो")
_TOMORROW = ("tomorrow", "tmrw", "tommorow", "tomorow", "repu", "repe", "రేపు", "రేపే", "రేపటి",
             "kal", "कल")
_TODAY = ("today", "ivala", "ivvala", "ee roju", "eeroju", "ఈరోజు", "ఈ రోజు", "ఇవాళ", "ఇవ్వాళ",
          "aaj", "आज")

_WEEKDAYS = {
    0: ("monday", "somavaram", "సోమవారం", "somvar", "somwar", "सोमवार"),
    1: ("tuesday", "mangalavaram", "మంగళవారం", "mangalvar", "mangalwar", "मंगलवार"),
    2: ("wednesday", "budhavaram", "బుధవారం", "budhvar", "budhwar", "बुधवार"),
    3: ("thursday", "guruvaram", "గురువారం", "guruvar", "guruwar", "गुरुवार"),
    4: ("friday", "shukravaram", "sukravaram", "శుక్రవారం", "shukravar", "shukrawar", "शुक्रवार"),
    5: ("saturday", "shanivaram", "sanivaram", "శనివారం", "shanivar", "shaniwar", "शनिवार"),
    6: ("sunday", "aadivaram", "adivaram", "ఆదివారం", "ravivar", "raviwar", "itvaar", "रविवार"),
}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Spoken variants matter: CALL-20261011-3F59FF said "సాయంకాలం 5 గంటలకి" and was asked
# "morning or evening?" because only the written form సాయంత్రం was listed.
PERIOD_WORDS = {
    MORNING: ("morning", "udayam", "ఉదయం", "ఉదయా", "poddunna", "podduna", "పొద్దున", "పొద్దుట", "subah", "subha",
              "सुबह"),
    AFTERNOON: ("afternoon", "madhyahnam", "madhyanam", "మధ్యాహ్నం", "మధ్యాహ్న", "మధ్యాన్నం", "dopahar",
                "dopehar", "दोपहर"),
    EVENING: ("evening", "saayantram", "sayantram", "sayankalam", "saayankalam", "సాయంత్రం", "సాయంత్రము",
              "సాయంకాలం", "సాయంకాల", "సాయంత్ర", "shaam", "sham", "शाम", "night", "raatri", "రాత్రి", "=रात", "रात्रि"),
}


def parse_date(text: str, now: Optional[datetime] = None) -> Optional[date]:
    """The date the caller named, or None. Never returns a past date.
    When several date words occur, the LAST one wins ("tomorrow... no, Friday")."""
    t = norm(text)
    if not t:
        return None
    today = today_ist(now)
    candidates = []  # (position, date)

    def add(words, d):
        i = last_index(t, words)
        if i >= 0:
            candidates.append((i, d))

    add(_DAY_AFTER, today + timedelta(days=2))
    # "day after tomorrow" contains "tomorrow": only count tomorrow outside it.
    if first_index(t, _DAY_AFTER) < 0:
        add(_TOMORROW, today + timedelta(days=1))
    add(_TODAY, today)
    for wd, words in _WEEKDAYS.items():
        delta = (wd - today.weekday()) % 7
        if delta == 0 and has_any(t, ("next",)):
            delta = 7
        add(words, today + timedelta(days=delta))

    m = None
    for m in re.finditer(r"(?<!\d)(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?(?!\d)", t):
        pass
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        year += 2000 if year < 100 else 0
        try:
            d = date(year, month, day)
            if not m.group(3) and d < today:
                d = date(year + 1, month, day)
            if d >= today:
                candidates.append((m.start(), d))
        except ValueError:
            pass

    for pat, dg, mg in ((r"(?<![\d:])(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)", 1, 2),
                        (r"(?<![a-z])([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?![\d:])", 2, 1)):
        for mm in re.finditer(pat, t):
            if mm.group(mg) in _MONTHS:
                day, month = int(mm.group(dg)), _MONTHS[mm.group(mg)]
                for year in (today.year, today.year + 1):
                    try:
                        d = date(year, month, day)
                    except ValueError:
                        break
                    if d >= today:
                        candidates.append((mm.start(), d))
                        break

    if not candidates:
        for mm in re.finditer(r"(?<![\d:.])(\d{1,2})(?:st|nd|rd|th)(?![a-z\d])", t):
            day = int(mm.group(1))
            for k in range(2):
                y = today.year + (today.month - 1 + k) // 12
                mo = (today.month - 1 + k) % 12 + 1
                try:
                    d = date(y, mo, day)
                except ValueError:
                    continue
                if d >= today:
                    candidates.append((mm.start(), d))
                    break

    return max(candidates, key=lambda c: c[0])[1] if candidates else None


def parse_time_period(text: str) -> Optional[str]:
    """MORNING / AFTERNOON / EVENING; the LAST one mentioned wins
    ("morning... no, evening" means evening)."""
    t = norm(text)
    found = [(p, last_index(t, words)) for p, words in PERIOD_WORDS.items()]
    found = [(p, i) for p, i in found if i >= 0]
    return max(found, key=lambda x: x[1])[0] if found else None


_CLOCK = re.compile(
    r"(?<![\d/\-.:])(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?![\d/\-]|(?:st|nd|rd|th)(?![a-z]))"
)


# "5 o'clock" / "5 గంటలకి" / "5 बजे": a number said with one of these IS a time, with or
# without a period word. Specific suffixes only: "2 గంటలు అయింది" (waited 2 hours) is not.
_CLOCK_MARKERS = ("o'clock", "o’clock", "o clock", "oclock", "గంటలకి", "గంటలకు", "గంటకి", "గంటకు",
                  "గంటల సమయ", "gantalaki", "gantalaku", "gantaki", "gantaku", "baje", "बजे", "बजकर",
                  # English said by a Telugu/Hindi caller, written in their script by STT
                  "ఓ క్లాక్", "ఓక్లాక్", "ఓ'క్లాక్", "ओ क्लॉक", "ओक्लॉक")
_MARKER_AFTER = re.compile(r"\s*(?:" + "|".join(map(re.escape, _CLOCK_MARKERS)) + ")")
# "5 పీఎం" / "5 पीएम" -> "5 pm".
_INDIC_AMPM = re.compile(r"(\d)\s*(?:(పీ|పి|पी)|(ఏ|ఎ|ए))\.?\s*(?:ఎం|एम)(?!\w)")
# Hours said as words (Telugu STT often writes "ఐదు గంటలకి", not "5 గంటలకి"), including
# English numbers in Telugu/Hindi letters ("సిక్స్ ఓ క్లాక్", live call 2026-10).
_HOUR_WORDS = {
    1: ("one", "ఒకటి", "ఒంటి", "एक", "వన్"), 2: ("two", "రెండు", "दो", "టూ"), 3: ("three", "మూడు", "तीन", "త్రీ"),
    4: ("four", "నాలుగు", "चार", "ఫోర్"), 5: ("five", "ఐదు", "అయిదు", "पाँच", "पांच", "ఫైవ్", "फाइव"),
    6: ("six", "ఆరు", "छह", "छः", "సిక్స్", "सिक्स"), 7: ("seven", "ఏడు", "सात", "సెవెన్", "सेवन"),
    8: ("eight", "ఎనిమిది", "आठ", "ఎయిట్", "एट"), 9: ("nine", "తొమ్మిది", "नौ", "నైన్", "नाइन"),
    10: ("ten", "పది", "दस", "టెన్", "टेन"), 11: ("eleven", "పదకొండు", "ग्यारह", "ఎలెవెన్", "इलेवन"),
    12: ("twelve", "పన్నెండు", "बारह", "ట్వెల్వ్", "ट्वेल्व"),
}
_WORD_HOUR = {w: h for h, ws in _HOUR_WORDS.items() for w in ws}
_WORDS_ALT = "|".join(re.escape(w) for w in sorted(_WORD_HOUR, key=len, reverse=True))
# "ఐదున్నర గంటలకి" = 5:30, "साढ़े पाँच बजे" = 5:30, "six pm" = 18:00.
_WORD_CLOCK = re.compile(r"(?:(साढ़े|sadhe|saadhe|saade)\s+)?(?<!\S)(" + _WORDS_ALT + r")(న్నర|nnara)?(?="
                         + _MARKER_AFTER.pattern + r"|\s*[ap]\.?m\b)")
# Colloquial Telugu "ఐదింటికి" / "నాలుగింటికి" ("at five"): the word's stem + ింటికి.
_INTIKI = {ws[:-1] + "ింటికి": h for h, group in _HOUR_WORDS.items() for ws in group
           if ws[-1:] in ("ు", "ి") and not ws.isascii()}
_INTIKI_RE = re.compile(r"(?<!\S)(" + "|".join(sorted(_INTIKI, key=len, reverse=True)) + ")")


def _spoken_hours_to_digits(t: str) -> str:
    t = _INDIC_AMPM.sub(lambda m: f"{m.group(1)} {'pm' if m.group(2) else 'am'}", t)
    t = _INTIKI_RE.sub(lambda m: f"{_INTIKI[m.group(1)]} గంటలకి", t)
    return _WORD_CLOCK.sub(lambda m: f"{_WORD_HOUR[m.group(2)]}"
                                     + (":30" if m.group(1) or m.group(3) else ""), t)


def parse_clock_time(text: str) -> Optional[str]:
    """'10:30', '10.30', '4 pm', 'saayantram 5', "5 o'clock", 'ఐదు గంటలకి' -> 'HH:MM' (24h),
    else None. A bare number with no minutes, am/pm, period word or o'clock marker is NOT a
    time (it is usually an option number: "second one", "2")."""
    t = _spoken_hours_to_digits(norm(text))
    period = parse_time_period(t)
    for m in _CLOCK.finditer(t):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) or "").replace(".", "")
        if not (m.group(2) or ampm or period or _MARKER_AFTER.match(t, m.end())):
            continue
        if hour > 23 or minute > 59:
            continue
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        elif not ampm and period in (AFTERNOON, EVENING) and hour < 12:
            hour += 12
        elif not ampm and period is None and 1 <= hour <= 7:
            hour += 12  # "5:30" at a clinic means 17:30, not 05:30
        return f"{hour:02d}:{minute:02d}"
    return None


def slot_period(hhmm: str) -> str:
    h = int(str(hhmm)[:2])
    return MORNING if h < 12 else AFTERNOON if h < 16 else EVENING
