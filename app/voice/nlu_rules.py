"""Deterministic understanding layer: intents, entities, language requests.

This runs on EVERY turn before any LLM. It is the layer the reliability
benchmark measures first, and it is what keeps the receptionist working when
the LLM is slow or down. The LLM (nlu_llm.py) is consulted only when this
layer finds nothing usable, and its output is validated against the same
tenant data before use.
"""

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from .dates import first_index, has_any, norm, parse_clock_time, parse_date, parse_time_period
from .lexicon import find_specialties, match_doctors, resolve_department, tenant_department_for

NLU_RULES_VERSION = "nlu-rules-2026.10.07"

SUPPORTED_LANGS = ("te-IN", "hi-IN", "en-IN")
DIALOG_ACTS = ("AFFIRM", "DENY", "GOODBYE", "REPEAT", "LANGUAGE_CHANGE")


@dataclass
class NluContext:
    doctors: list = field(default_factory=list)          # [{id, name, department, ...}]
    departments: list = field(default_factory=list)      # clinic's own department names
    tenant_entries: list = field(default_factory=list)   # voice_lexicon_entries rows
    now: Optional[datetime] = None


@dataclass
class NLUResult:
    intents: list
    entities: dict
    confidence: float
    source: str = "rules"            # rules | llm | none
    language_request: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def business_intents(self) -> list:
        return [i for i in self.intents if i not in DIALOG_ACTS]


# ---- word lists (ASCII = whole word; Indic = substring) ----

W = {
    "BOOK": ("appointment", "appointments", "book", "booking", "అపాయింట్మెంట్", "అపాయింట్‌మెంట్",
             "బుక్", "अपॉइंटमेंट", "अपोइंटमेंट", "बुक", "एपॉइंटमेंट"),
    "WANT": ("kavali", "kaavali", "కావాలి", "chahiye", "chaiye", "चाहिए", "need", "needs", "want", "wants", "dikhana",
             "dikhaana", "consult", "kalavali", "కలవాలి", "milna", "मिलना", "दिखाना"),
    "AVAIL": ("available", "availability", "unnara", "unnaru", "undara", "ఉన్నారా", "అందుబాటులో",
              "milenge", "slots", "slot", "free", "मिलेंगे", "उपलब्ध", "స్లాట్", "ఖాళీ", "khali",
              "खाली", "स्लॉट"),
    "DOCTOR": ("doctor", "doctors", "dr", "daktar", "డాక్టర్", "డాక్టరు", "వైద్యు", "डॉक्टर", "डाक्टर"),
    "INFO": ("information", "info", "details", "samacharam", "సమాచారం", "వివరాలు", "జానకారీ",
             "jankari", "jaankari", "जानकारी", "डिटेल"),
    "CANCEL": ("cancel", "cancellation", "రద్దు", "క్యాన్సిల్", "radd", "रद्द", "कैंसिल"),
    "RESCHEDULE": ("reschedule", "postpone", "prepone", "marchandi", "మార్చండి", "మార్చాలి",
                   "badal", "badalna", "बदल", "बदलना"),
    "STATUS": ("my appointment", "appointment status", "naa appointment", "నా అపాయింట్మెంట్",
               "mera appointment", "मेरा अपॉइंटमेंट", "when is my"),
    "REPORT": ("report", "reports", "result", "results", "రిపోర్ట్", "రిపోర్టు", "రిజల్ట్", "रिपोर्ट"),
    "SEND": ("send", "whatsapp", "pampu", "pampandi", "pampinchandi", "పంపండి", "పంపించండి", "పంపు",
             "bhej", "bhejo", "bhejiye", "भेज", "भेजो", "forward", "వాట్సాప్", "व्हाट्सएप"),
    "LAB": ("test", "tests", "scan", "xray", "x-ray", "ultrasound", "ecg", "cbc", "lipid", "lab",
            "టెస్ట్", "పరీక్ష", "స్కాన్", "टेस्ट", "जांच", "जाँच", "स्कैन"),
    "FEES": ("fee", "fees", "charges", "charge", "cost", "price", "rate", "ఫీజు", "ఎంత", "entha",
             "kitna", "kitne", "कितना", "कितने", "फीस"),
    "LOCATION": ("address", "location", "where", "directions", "route", "map", "ఎక్కడ", "అడ్రస్",
                 "ekkada", "kahan", "kaha", "कहाँ", "कहां", "पता"),
    "HOURS": ("timing", "timings", "open", "hours", "closing", "టైమింగ్", "సమయాలు",
              "kab tak", "खुला", "समय"),
    "CONTACT": ("phone number", "contact number", "contact", "landline"),
    "QUEUE": ("token", "queue", "my turn", "waiting", "ఎంత సేపు", "kitni der", "कितनी देर"),
    "PAYMENT": ("payment", "paid", "pay", "link", "చెల్లింపు", "పేమెంట్", "पेमेंट", "भुगतान"),
    "REFUND": ("refund", "రీఫండ్", "रिफंड", "paisa wapas", "पैसा वापस"),
    "HUMAN": ("human", "receptionist", "reception", "person", "staff", "operator", "someone", "manishi",
              "మనిషి", "రిసెప్షన్", "इंसान", "किसी से बात", "रिसेप्शन"),
    "CALLBACK": ("call back", "callback", "call me back", "call me later", "తర్వాత కాల్", "baad mein call", "बाद में कॉल",
                 "busy", "బిజీ", "बिज़ी", "बिजी"),
    "COMPLAINT": ("complaint", "complain", "ఫిర్యాదు", "शिकायत"),
    "ASK_IF_AI": ("are you a robot", "are you ai", "are you an ai", "are you a bot", "are you human",
                  "are you a machine", "robot aa", "robot ha", "రోబోట్", "रोबोट", "मशीन हो"),
    "AFFIRM": ("yes", "yeah", "yep", "ok", "okay", "sure", "haa", "haan", "han", "avunu", "అవును", "సరే",
               "sare", "=హా", "=हाँ", "=हां", "=जी", "theek hai", "ठीक", "confirm", "cheyyandi", "cheyandi",
               "చేయండి", "kar do", "kardo", "karo", "pakka", "correct", "right",
               "ఓకే", "ఓకై", "అలాగే", "alage", "=ఔను", "కరెక్ట్", "తప్పకుండా", "చెయ్యండి", "బుక్ చేయండి",
               "ओके", "ज़रूर", "जरूर", "बिल्कुल", "alright", "go ahead", "please do", "of course"),
    "DENY": ("no", "nope", "vaddu", "వద్దు", "kaadu", "కాదు", "ledu", "లేదు", "nahi", "nahin", "नहीं",
             "mat", "don't", "dont", "వద్దండి", "అక్కర్లేదు", "=నో", "=मत", "नको", "not needed"),
    "GOODBYE": ("bye", "goodbye", "thank you", "thanks", "dhanyavadalu", "ధన్యవాదాలు", "shukriya",
                "धन्यवाद", "that's all", "thats all", "anthe", "అంతే", "bas", "=बस", "=బై", "=बाय", "థాంక్స్",
                "थैंक्स"),
    "REPEAT": ("repeat", "malli cheppandi", "మళ్ళీ చెప్పండి", "phir se", "dobara", "दोबारा", "फिर से",
               "pardon", "come again"),
}

# Yes only when it is (nearly) the whole reply: "ఆ" is also "that" ("ఆ డాక్టర్"), "హా" a filler.
SHORT_AFFIRM = frozenset({"ఆ", "ఆఁ", "ఆ ఆ", "హా", "హాఁ", "హాం", "హా అండి", "ఆ అండి", "aa", "haa", "ha",
                          "ఓకే అండి", "ఓకే సార్", "సరే అండి", "సరే సార్", "सही", "हाँ जी", "जी हाँ",
                          "ఆ చెప్పండి", "హా చెప్పండి", "ఆ ఓకే", "హా ఓకే", "ఆ సరే", "హా సరే", "ఆ చేయండి",
                          "హా చేయండి", "aa cheppandi", "haa cheppandi", "हाँ बताइए", "हाँ बोलिए"})

FIRST = ("first", "1st", "modati", "modatidi", "మొదటి", "మొదటిది", "pehla", "pehle", "पहला", "पहले")
SECOND = ("second", "2nd", "rendo", "rendava", "రెండో", "రెండవ", "doosra", "dusra", "दूसरा", "दूसरे")
THIRD = ("third", "3rd", "mudo", "moodo", "మూడో", "మూడవ", "teesra", "tisra", "तीसरा", "तीसरे")

RELATIONS = {
    "MOTHER": ("mother", "mom", "amma", "అమ్మ", "maa", "mummy", "=माँ", "=मां", "मम्मी"),
    "FATHER": ("father", "dad", "nanna", "నాన్న", "papa", "पापा", "पिताजी", "पिता"),
    "SPOUSE": ("wife", "husband", "bharya", "భార్య", "bharta", "భర్త", "pati", "patni", "पति", "पत्नी"),
    "CHILD": ("son", "daughter", "koduku", "కొడుకు", "kuthuru", "కూతురు", "beta", "beti", "बेटा", "बेटी",
              "my child", "my baby"),
    "OTHER": ("friend", "relative", "brother", "sister", "anna", "akka", "thammudu", "chelli", "bhai",
              "behen", "भाई", "बहन"),
}

LANG_NAMES = {
    "te-IN": ("telugu", "తెలుగు", "तेलुगु"),
    "hi-IN": ("hindi", "హిందీ", "हिंदी", "हिन्दी"),
    "en-IN": ("english", "ఇంగ్లీష్", "ఇంగ్లిష్", "इंग्लिश", "अंग्रेजी", "angrezi"),
}
_SWITCH_CUES = ("lo", "mein", "me", "please", "speak", "talk", "matladandi", "maatladandi", "cheppandi",
                "bolo", "boliye", "baat", "మాట్లాడండి", "చెప్పండి", "లో", "में", "बोलिए", "बात")

_SCRIPTS = (("te-IN", 0x0C00, 0x0C7F), ("hi-IN", 0x0900, 0x097F), ("ta-IN", 0x0B80, 0x0BFF),
            ("kn-IN", 0x0C80, 0x0CFF), ("ml-IN", 0x0D00, 0x0D7F), ("bn-IN", 0x0980, 0x09FF),
            ("gu-IN", 0x0A80, 0x0AFF), ("pa-IN", 0x0A00, 0x0A7F), ("od-IN", 0x0B00, 0x0B7F))

ROMAN_INDIC_MARKERS = ("kavali", "kaavali", "cheyyandi", "cheyandi", "naaku", "naku", "nenu", "undi",
                       "unnara", "chahiye", "hai", "mujhe", "kar", "karo", "dijiye", "nahi", "haan", "ledu",
                       "vaddu", "kaadu", "avunu", "repu", "ellundi", "ivala", "kal", "aaj", "garu", "andi",
                       "ji", "lo", "ki", "ka", "ke", "mein", "chesi", "pampandi")

_BOOKING_REF = re.compile(r"\b([A-Z]{2,8}-?[A-Z0-9]{4,10})\b")


def language_request(text: str) -> Optional[str]:
    """'Telugu lo cheppandi' / 'Speak in English' / 'हिंदी में' -> language code."""
    t = norm(text)
    for code, names in LANG_NAMES.items():
        if has_any(t, names) and (has_any(t, _SWITCH_CUES) or len(t.split()) <= 2):
            return code
    return None


def script_language(text: str) -> Optional[str]:
    """Language by Unicode script when >50% of letters are one Indic script."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return None
    for code, lo, hi in _SCRIPTS:
        if sum(1 for c in letters if lo <= ord(c) <= hi) / len(letters) > 0.5:
            return code
    return None


def looks_english(text: str) -> bool:
    t = norm(text)
    return bool(t) and t.isascii() and len(t.split()) >= 3 and not has_any(t, ROMAN_INDIC_MARKERS)


def _option_index(t: str) -> Optional[int]:
    if has_any(t, THIRD):
        return 2
    if has_any(t, SECOND):
        return 1
    if has_any(t, FIRST):
        return 0
    m = re.fullmatch(r"\s*(?:option\s*)?([123])\s*", t)
    return int(m.group(1)) - 1 if m else None


_NAME_FILLERS = ("my name is", "name is", "naa peru", "na peru", "నా పేరు", "peru", "పేరు", "mera naam",
                 "मेरा नाम", "naam", "नाम", "hai", "है", "andi", "అండి", "ji", "जी", "patient name",
                 "her name is", "his name is", "amma peru", "nanna peru")


def extract_name(text: str) -> Optional[str]:
    t = " " + norm(text) + " "
    for f in sorted(_NAME_FILLERS, key=len, reverse=True):
        t = t.replace(f" {f} ", " ")
    t = re.sub(r"[^\w\s.]", " ", t)
    words = [w for w in t.split() if not w.isdigit()]
    if not 1 <= len(words) <= 4:
        return None
    name = " ".join(words).strip(" .")
    return name.title() if name.isascii() else name


def understand(text: str, ctx: NluContext, expect: Optional[str] = None) -> NLUResult:
    t = norm(text)
    ents: dict = {}
    if not t:
        return NLUResult([], {}, 0.0, "none")

    # ---- entities ----
    d = parse_date(t, ctx.now)
    if d:
        ents["date"] = d.isoformat()
    period = parse_time_period(t)
    if period:
        ents["time_period"] = period
    clock = parse_clock_time(t)
    if clock:
        ents["clock_time"] = clock

    # While a patient name is expected, a name like "Lakshmi Devi" must not be
    # read as Dr. Lakshmi Prasanna (or a specialty) and re-plan the booking.
    naming = expect == "patient_name"
    dept = None if naming else tenant_department_for(t, ctx.tenant_entries, ctx.departments)
    specs = [] if naming else find_specialties(t)
    if specs:
        ents["specialty"] = specs[0]
        dept = dept or resolve_department(specs[0], ctx.departments)
    if dept:
        ents["department"] = dept
    docs = [] if naming else match_doctors(text, ctx.doctors, ctx.tenant_entries)
    if docs:
        ents["doctor_ids"] = [x["id"] for x in docs]

    for rel, words in RELATIONS.items():
        if has_any(t, words):
            ents["relation"] = rel
            break
    if expect == "option":
        idx = _option_index(t)
        if idx is not None:
            ents["option_index"] = idx
    if expect == "patient_name":
        name = extract_name(text)
        if name:
            ents["patient_name"] = name
    m = _BOOKING_REF.search(text.upper())
    if m and any(ch.isdigit() for ch in m.group(1)):
        ents["booking_ref"] = m.group(1)

    lang_req = language_request(t)

    # ---- intents, ordered by where they are said ----
    pos: dict = {}

    def hit(intent, key):
        i = first_index(t, W[key])
        if i >= 0 and (intent not in pos or i < pos[intent]):
            pos[intent] = i

    has_report = has_any(t, W["REPORT"])
    has_lab = has_any(t, W["LAB"])
    if has_any(t, W["ASK_IF_AI"]):
        hit("ASK_IF_AI", "ASK_IF_AI")
    elif has_any(t, W["HUMAN"]):
        hit("HUMAN_AGENT_REQUEST", "HUMAN")
    hit("CALLBACK_REQUEST", "CALLBACK")
    hit("COMPLAINT", "COMPLAINT")
    hit("CANCEL_APPOINTMENT", "CANCEL")
    hit("RESCHEDULE_APPOINTMENT", "RESCHEDULE")
    if has_report:
        hit("REPORT_DELIVERY" if has_any(t, W["SEND"]) else "REPORT_STATUS", "REPORT")
    if has_lab and not has_report:
        hit("LAB_TEST_BOOKING" if has_any(t, W["BOOK"]) else "LAB_TEST_SEARCH", "LAB")
    if has_any(t, W["REFUND"]):
        hit("REFUND_STATUS", "REFUND")
    elif has_any(t, W["PAYMENT"]):
        hit("PAYMENT_STATUS", "PAYMENT")
    if has_any(t, W["QUEUE"]):
        hit("QUEUE_STATUS", "QUEUE")
    if has_any(t, W["FEES"]) and not has_lab:
        hit("FEES", "FEES")
    for key, intent, topic in (("LOCATION", "LOCATION", "location"),
                               ("HOURS", "HOSPITAL_INFORMATION", "hours"),
                               ("CONTACT", "CONTACT_INFORMATION", "contact")):
        if has_any(t, W[key]):
            hit(intent, key)
            ents.setdefault("info_topic", topic)

    transactional = {"CANCEL_APPOINTMENT", "RESCHEDULE_APPOINTMENT", "LAB_TEST_BOOKING"}
    says_book = has_any(t, ("book", "బుక్", "बुक"))
    if has_any(t, W["STATUS"]) and not (pos.keys() & transactional) and not says_book:
        hit("APPOINTMENT_STATUS", "STATUS")
    elif has_any(t, W["BOOK"]) and not (pos.keys() & transactional):
        hit("BOOK_APPOINTMENT", "BOOK")
    elif (specs or docs or dept) and not (pos.keys() & transactional) and "FEES" not in pos:
        if has_any(t, W["AVAIL"]):
            hit("DOCTOR_AVAILABILITY", "AVAIL")
        elif has_any(t, W["WANT"]) or d:
            pos["BOOK_APPOINTMENT"] = first_index(t, W["WANT"]) if has_any(t, W["WANT"]) else 0

    # Nothing business-like yet: "are doctor slots free?", "naaku doctor kavali" -> booking;
    # "I need other information" -> hospital information (the dialog asks which).
    if not pos:
        if has_any(t, W["AVAIL"]) or (has_any(t, W["DOCTOR"]) and has_any(t, W["WANT"])):
            pos["BOOK_APPOINTMENT"] = max(0, first_index(t, W["AVAIL"] + W["DOCTOR"]))
        elif has_any(t, W["INFO"]):
            pos["GENERAL_INFORMATION"] = first_index(t, W["INFO"])

    for act in ("AFFIRM", "DENY", "GOODBYE", "REPEAT"):
        if has_any(t, W[act]):
            pos.setdefault(act, first_index(t, W[act]))
    if " ".join(re.sub(r"[.,!?।;:'\"-]", " ", t).split()) in SHORT_AFFIRM:  # not \W: it eats Indic vowel signs
        pos.setdefault("AFFIRM", 0)
    if lang_req:
        pos["LANGUAGE_CHANGE"] = 0
    if expect == "confirm" and "AFFIRM" in pos:
        pos.pop("BOOK_APPOINTMENT", None)  # "yes, book it" confirms; it is not a new booking
    if "AFFIRM" in pos and "DENY" in pos:
        pos.pop("AFFIRM" if pos["AFFIRM"] < pos["DENY"] else "DENY")  # the later act wins

    intents = [k for k, _ in sorted(pos.items(), key=lambda kv: kv[1])]
    business = [i for i in intents if i not in DIALOG_ACTS]
    if business or lang_req:
        conf = 0.95 if len(business) <= 1 else 0.9
    elif intents or any(k in ents for k in ("date", "time_period", "clock_time", "option_index",
                                             "patient_name", "department", "doctor_ids", "specialty")):
        conf = 0.95 if expect else 0.7
    else:
        conf = 0.0
    if "specialty" in ents and "department" not in ents and ctx.departments:
        conf = min(conf, 0.8)  # a specialty this clinic may not have
    return NLUResult(intents, ents, conf, "rules" if conf > 0 else "none", lang_req)
