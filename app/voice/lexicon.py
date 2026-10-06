"""Healthcare terminology layer: canonical specialties, multilingual synonyms,
Indic -> Latin romanisation for name matching, and tenant overrides.

Canonical specialty ids are Kriya's own vocabulary. A clinic's real department
names (doctors.department, free text) are matched to them at runtime by
`resolve_department`, so "Cardiology", "Cardiologist" and "Heart Care" all work
without per-clinic setup. A clinic adds anything else through
voice_lexicon_entries (kind='specialty_synonym' / 'doctor_alias' / 'test_alias' /
'pronunciation').
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Optional

from .dates import first_index, norm

LEXICON_VERSION = "lexicon-2026.10.06"

# canonical -> spoken labels, caller synonyms, department-name stems clinics use
SPECIALTIES: dict = {
    "CARDIOLOGY": {
        "label": {"en": "cardiology", "te": "గుండె డాక్టర్", "hi": "दिल के डॉक्टर"},
        "synonyms": ("heart", "cardio", "cardiology", "cardiologist", "cardiac", "gunde", "gundey",
                     "గుండె", "హార్ట్", "కార్డియో", "కార్డియాలజీ", "కార్డియాలజిస్ట్",
                     "dil", "दिल", "हृदय", "हार्ट", "कार्डियो", "कार्डियोलॉजी", "कार्डियोलॉजिस्ट"),
        "departments": ("cardio", "heart", "cardiac"),
    },
    "ORTHOPEDICS": {
        "label": {"en": "orthopaedics", "te": "ఎముకల డాక్టర్", "hi": "हड्डी के डॉक्टर"},
        "synonyms": ("ortho", "orthopedic", "orthopaedic", "orthopedics", "orthopaedics", "bone", "bones",
                     "joint", "joints", "knee", "emuka", "emukala", "ఎముక", "ఎముకల", "ఆర్థో", "కీళ్ల",
                     "haddi", "हड्डी", "ऑर्थो", "जोड़"),
        "departments": ("ortho", "bone", "joint"),
    },
    "PEDIATRICS": {
        "label": {"en": "children's doctor", "te": "పిల్లల డాక్టర్", "hi": "बच्चों के डॉक्टर"},
        "synonyms": ("child", "children", "kids", "kid", "baby", "pediatric", "paediatric", "pediatrician",
                     "paediatrician", "pediatrics", "paediatrics", "pillala", "పిల్లల", "పిల్లలు",
                     "పీడియాట్రిక్", "bacche", "bachche", "bachon", "बच्चों", "बच्चे", "बाल रोग"),
        "departments": ("pediatric", "paediatric", "child", "neonat"),
    },
    "GYNECOLOGY": {
        "label": {"en": "gynaecology", "te": "గైనకాలజీ డాక్టర్", "hi": "स्त्री रोग डॉक्टर"},
        "synonyms": ("gynec", "gynaec", "gyno", "gynecologist", "gynaecologist", "gynecology", "gynaecology",
                     "lady doctor", "pregnancy", "obstetrician", "గైనకాలజీ", "గైనిక్", "ప్రసూతి", "గర్భం",
                     "garbham", "स्त्री रोग", "गायनी", "गर्भ", "प्रेगनेंसी"),
        "departments": ("gyn", "obst", "women", "maternity"),
    },
    "DERMATOLOGY": {
        "label": {"en": "skin doctor", "te": "చర్మ వైద్యులు", "hi": "त्वचा के डॉक्टर"},
        "synonyms": ("skin", "derma", "dermatology", "dermatologist", "hair fall", "charma", "charmam",
                     "చర్మ", "స్కిన్", "त्वचा", "चर्म", "स्किन"),
        "departments": ("derma", "skin", "cosmet"),
    },
    "ENT": {
        "label": {"en": "ENT", "te": "ENT (చెవి ముక్కు గొంతు)", "hi": "ENT (नाक कान गला)"},
        "synonyms": ("ent", "ear", "nose", "throat", "chevi", "mukku", "gonthu", "చెవి", "ముక్కు", "గొంతు",
                     "kaan", "naak", "gala", "कान", "नाक", "गला"),
        "departments": ("ent", "otorhino", "ear"),
    },
    "OPHTHALMOLOGY": {
        "label": {"en": "eye doctor", "te": "కంటి డాక్టర్", "hi": "आँखों के डॉक्टर"},
        "synonyms": ("eye", "eyes", "ophthalmology", "ophthalmologist", "vision", "kanti", "kallu", "kalla",
                     "కంటి", "కళ్ళ", "కన్ను", "aankh", "aankhon", "आंख", "आँख", "नेत्र"),
        "departments": ("ophthal", "eye", "vision"),
    },
    "DENTAL": {
        "label": {"en": "dentist", "te": "పళ్ళ డాక్టర్", "hi": "दाँतों के डॉक्टर"},
        "synonyms": ("dental", "dentist", "teeth", "tooth", "pallu", "palla", "పళ్ళ", "పన్ను", "పంటి",
                     "daant", "dant", "दांत", "दाँत"),
        "departments": ("dent", "tooth", "oral"),
    },
    "NEUROLOGY": {
        "label": {"en": "neurology", "te": "నరాల డాక్టర్", "hi": "न्यूरो डॉक्टर"},
        "synonyms": ("neuro", "neurology", "neurologist", "brain", "nerve", "nerves", "naraala", "narala",
                     "నరాల", "మెదడు", "న్యూరో", "न्यूरो", "दिमाग", "नस"),
        "departments": ("neuro", "brain"),
    },
    "GASTROENTEROLOGY": {
        "label": {"en": "gastroenterology", "te": "కడుపు సంబంధిత డాక్టర్", "hi": "पेट के डॉक्टर"},
        "synonyms": ("gastro", "gastroenterology", "gastroenterologist", "stomach", "liver", "kadupu",
                     "కడుపు", "గ్యాస్ట్రో", "లివర్", "पेट", "गैस्ट्रो", "लिवर"),
        "departments": ("gastro", "liver", "hepat", "digest"),
    },
    "GENERAL_MEDICINE": {
        "label": {"en": "general physician", "te": "జనరల్ ఫిజీషియన్", "hi": "जनरल फिजिशियन"},
        "synonyms": ("general physician", "general medicine", "physician", "general doctor", "fever",
                     "jwaram", "జ్వరం", "జనరల్", "bukhar", "बुखार", "जनरल"),
        "departments": ("general", "medicine", "physician", "internal"),
    },
    "PULMONOLOGY": {
        "label": {"en": "lung specialist", "te": "ఊపిరితిత్తుల డాక్టర్", "hi": "फेफड़ों के डॉक्टर"},
        "synonyms": ("lungs", "lung", "pulmonology", "pulmonologist", "asthma", "ఊపిరితిత్తుల", "ఆస్తమా",
                     "फेफड़े", "दमा"),
        "departments": ("pulmo", "chest", "respir", "lung"),
    },
    "NEPHROLOGY": {
        "label": {"en": "kidney specialist", "te": "కిడ్నీ డాక్టర్", "hi": "किडनी के डॉक्टर"},
        "synonyms": ("kidney", "kidneys", "nephro", "nephrology", "nephrologist", "dialysis", "కిడ్నీ",
                     "మూత్రపిండ", "किडनी", "गुर्दा", "गुर्दे"),
        "departments": ("nephro", "kidney", "renal"),
    },
    "UROLOGY": {
        "label": {"en": "urology", "te": "యూరాలజీ డాక్టర్", "hi": "यूरोलॉजी डॉक्टर"},
        "synonyms": ("urology", "urologist", "prostate", "యూరాలజీ", "यूरोलॉजी"),
        "departments": ("uro",),
    },
    "ENDOCRINOLOGY": {
        "label": {"en": "diabetes and thyroid specialist", "te": "షుగర్, థైరాయిడ్ డాక్టర్", "hi": "शुगर, थायराइड डॉक्टर"},
        "synonyms": ("diabetes", "diabetologist", "endocrinology", "endocrinologist", "thyroid", "sugar doctor",
                     "షుగర్ డాక్టర్", "థైరాయిడ్", "डायबिटीज", "शुगर डॉक्टर", "थायराइड"),
        "departments": ("endocr", "diabet", "thyroid"),
    },
    "PSYCHIATRY": {
        "label": {"en": "psychiatry", "te": "మానసిక వైద్యులు", "hi": "मनोचिकित्सक"},
        "synonyms": ("psychiatry", "psychiatrist", "psychologist", "mental health", "మానసిక", "मानसिक"),
        "departments": ("psych", "mental"),
    },
    "ONCOLOGY": {
        "label": {"en": "oncology", "te": "క్యాన్సర్ వైద్యులు", "hi": "कैंसर विशेषज्ञ"},
        "synonyms": ("oncology", "oncologist", "cancer", "క్యాన్సర్", "कैंसर"),
        "departments": ("onco", "cancer"),
    },
    "GENERAL_SURGERY": {
        "label": {"en": "general surgeon", "te": "సర్జన్", "hi": "सर्जन"},
        "synonyms": ("surgeon", "surgery", "general surgeon", "సర్జన్", "सर्जन"),
        "departments": ("surg",),
    },
    "PHYSIOTHERAPY": {
        "label": {"en": "physiotherapy", "te": "ఫిజియోథెరపీ", "hi": "फिजियोथेरेपी"},
        "synonyms": ("physio", "physiotherapy", "physiotherapist", "ఫిజియో", "फिजियो"),
        "departments": ("physio", "rehab"),
    },
}


def _ascii_pattern(word: str):
    # Whole word plus the commonest romanised case suffixes ("gundeki", "orthoku").
    return re.compile(rf"(?<![a-z]){re.escape(word)}(?:ki|ku|lo|ke|ko|ka|ni|na)?(?![a-z])")


_ASCII_SYNONYMS = [
    (canon, _ascii_pattern(syn))
    for canon, spec in SPECIALTIES.items() for syn in spec["synonyms"] if syn.isascii()
]


def find_specialties(text: str) -> list:
    """Canonical specialties mentioned, in order of first mention, de-duplicated."""
    t = norm(text)
    hits: dict = {}
    for canon, pat in _ASCII_SYNONYMS:
        m = pat.search(t)
        if m and (canon not in hits or m.start() < hits[canon]):
            hits[canon] = m.start()
    for canon, spec in SPECIALTIES.items():
        for syn in spec["synonyms"]:
            if not syn.isascii():
                i = t.find(syn)
                if i >= 0 and (canon not in hits or i < hits[canon]):
                    hits[canon] = i
    return [c for c, _ in sorted(hits.items(), key=lambda kv: kv[1])]


def resolve_department(canonical: str, clinic_departments: Iterable[str]) -> Optional[str]:
    """The clinic's own department name for a canonical specialty, or None."""
    spec = SPECIALTIES.get(canonical)
    if not spec:
        return None
    for d in clinic_departments:
        if d and any(stem in d.lower() for stem in spec["departments"]):
            return d
    return None


def tenant_department_for(text: str, tenant_entries: Iterable[dict], clinic_departments: Iterable[str]) -> Optional[str]:
    """A clinic-defined phrase ("cardiac OPD", "గుండె విభాగం") -> its department."""
    t = norm(text)
    valid = {norm(d): d for d in clinic_departments if d}
    best = None
    for e in tenant_entries:
        if e.get("kind") != "specialty_synonym" or not e.get("is_active", True):
            continue
        phrase = norm(e.get("phrase", ""))
        target = valid.get(norm(e.get("canonical", "")))
        if phrase and target and first_index(t, (phrase,)) >= 0:
            if best is None or len(phrase) > best[0]:
                best = (len(phrase), target)
    return best[1] if best else None


# ---- Indic -> Latin romanisation (for matching names only, never for speech) ----

_TE_CONS = dict(zip("కఖగఘఙచఛజఝఞటఠడఢణతథదధనపఫబభమయరఱలళవశషసహ",
                    ["k", "kh", "g", "gh", "n", "ch", "chh", "j", "jh", "n", "t", "th", "d", "dh", "n",
                     "t", "th", "d", "dh", "n", "p", "ph", "b", "bh", "m", "y", "r", "r", "l", "l", "v",
                     "s", "sh", "s", "h"]))
_TE_VOW = dict(zip("అఆఇఈఉఊఋఎఏఐఒఓఔ", ["a", "a", "i", "i", "u", "u", "ru", "e", "e", "ai", "o", "o", "au"]))
_TE_SIGN = dict(zip("ాిీుూృెేైొోౌ", ["a", "i", "i", "u", "u", "ru", "e", "e", "ai", "o", "o", "au"]))
_HI_CONS = dict(zip("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसह",
                    ["k", "kh", "g", "gh", "n", "ch", "chh", "j", "jh", "n", "t", "th", "d", "dh", "n",
                     "t", "th", "d", "dh", "n", "p", "ph", "b", "bh", "m", "y", "r", "l", "v", "sh", "sh",
                     "s", "h"]))
_HI_VOW = dict(zip("अआइईउऊऋएऐओऔ", ["a", "a", "i", "i", "u", "u", "ri", "e", "ai", "o", "au"]))
_HI_SIGN = dict(zip("ािीुूृेैोौ", ["a", "i", "i", "u", "u", "ri", "e", "ai", "o", "au"]))
_VIRAMAS = {"్", "्"}
_NASAL = {"ం": "m", "ं": "n", "ँ": "n"}


def romanize(text: str) -> str:
    """Rough, deterministic romanisation of Telugu / Devanagari. Long vowels
    collapse ("aa" -> "a") so it compares well with English spellings."""
    out = []
    chars = list(text)
    for i, ch in enumerate(chars):
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        cons = _TE_CONS.get(ch) or _HI_CONS.get(ch)
        if cons:
            out.append(cons)
            if nxt in _VIRAMAS or nxt in _TE_SIGN or nxt in _HI_SIGN or nxt == "़":
                continue
            next_is_letter = nxt in _TE_CONS or nxt in _HI_CONS or nxt in _NASAL
            if ch in _HI_CONS and not next_is_letter:
                continue  # Hindi schwa deletion at word end: "निवास" -> nivas
            out.append("a")
            continue
        if ch in _TE_VOW or ch in _HI_VOW:
            out.append(_TE_VOW.get(ch) or _HI_VOW[ch])
        elif ch in _TE_SIGN or ch in _HI_SIGN:
            out.append(_TE_SIGN.get(ch) or _HI_SIGN[ch])
        elif ch in _NASAL:
            out.append(_NASAL[ch])
        elif ch in _VIRAMAS or ch == "़" or unicodedata.category(ch) == "Mn":
            continue
        else:
            out.append(ch)
    s = "".join(out).lower()
    return re.sub(r"([aeiou])\1+", r"\1", s)


_NAME_NOISE = {"dr", "doctor", "doc", "garu", "gaaru", "sir", "madam", "maam", "with", "appointment",
               "kavali", "kaavali", "chahiye", "available", "unnara", "book", "the", "tomorrow", "today",
               "repu", "please", "see", "meet", "want", "need", "dactar", "daktar", "gari", "kosam",
               "evening", "morning", "slot", "time", "naku", "naaku", "mujhe", "hai", "kya"}


def _name_tokens(text: str) -> list:
    t = romanize(norm(text))
    t = re.sub(r"[^a-z ]", " ", t)
    return [w for w in t.split() if len(w) >= 4 and w not in _NAME_NOISE]


def match_doctors(text: str, doctors: list, tenant_entries: Iterable[dict] = ()) -> list:
    """Doctors the caller named. Empty when no name is clearly present;
    several when the name is ambiguous (two "Srinivas") - the dialog asks."""
    t = norm(text)
    by_name = {norm(d.get("name", "")): d for d in doctors if d.get("name")}
    for e in tenant_entries:
        if e.get("kind") == "doctor_alias" and e.get("is_active", True):
            phrase = norm(e.get("phrase", ""))
            target = by_name.get(norm(e.get("canonical", "")))
            if phrase and target and first_index(t, (phrase,)) >= 0:
                return [target]
    words = _name_tokens(text)
    if not words:
        return []
    scored = []
    for d in doctors:
        dtoks = _name_tokens(d.get("name", ""))
        if not dtoks:
            continue
        best = max(SequenceMatcher(None, w, n).ratio() for w in words for n in dtoks)
        if best >= 0.8:
            scored.append((best, d))
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    top = scored[0][0]
    return [d for s, d in scored if s >= top - 0.05]


def apply_pronunciations(text: str, tenant_entries: Iterable[dict]) -> str:
    """Replace configured spellings with how they should be spoken (TTS input only)."""
    out = text
    entries = sorted(
        (e for e in tenant_entries if e.get("kind") == "pronunciation" and e.get("is_active", True)),
        key=lambda e: -len(e.get("phrase", "")),
    )
    for e in entries:
        phrase, spoken = e.get("phrase") or "", e.get("canonical") or ""
        if phrase and spoken:
            out = re.sub(re.escape(phrase), spoken, out, flags=re.IGNORECASE)
    return out
