"""Kriya Ketu deterministic router: L1 navigation, L2 triggers, L3 form completion. Zero tokens.

Order (architecture.md §3): clinical screen -> negation -> hypothetical ->
how-to help -> a page name with only navigation words -> action triggers ->
nothing matched ("none": the LLM fallback slot, KK-P1-07).

It never guesses: navigation needs exactly one page and no other words; an
action needs a unique best trigger match; an ambiguous doctor or a missing
required slot becomes a clarify form. A question about a write is answered with
help, never executed. Negated and hypothetical requests never route to a write.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.assistant import resolve
from app.assistant.help import HANDOFFS, HELP, HelpEntry
from app.assistant.registry import PAGE_SYNONYMS, PAGES, REGISTRY, ActionSpec, SlotSpec
from app.services.clinical_firewall import is_sex_determination_request, screen_message
from app.voice.dates import IST, has_any, last_index
from app.voice.nlu_rules import W, is_question

# W["DENY"] minus plain "no" / "is not" words, which also open corrections ("no, I meant Friday").
_NOT_NEGATION = {"no", "nope", "not needed", "kaadu", "కాదు", "ledu", "లేదు", "=నో"}
NEGATION = tuple(w for w in W["DENY"] if w not in _NOT_NEGATION) + (
    "do not",
    # "Dr Rao is not on leave" must never preview the opposite (plain "not" stays out: it opens corrections).
    "not on leave", "isn't on leave", "not be on leave", "chutti nahi", "chutti nahin", "selavu ledu",
    "సెలవు లేదు", "छुट्टी नहीं", "छुट्टी पर नहीं")
HYPOTHETICAL = ("what if", "what happens if", "what would happen if", "suppose", "supposing", "in case",
                "agar", "अगर", "okavela", "ఒకవేళ")
_ONLY_DOES = re.compile(r"\bonly\b.*\b(?:does|do)\b", re.I)
HOW_TO = ("how do i", "how do we", "how to", "how can i", "how should i", "ela", "ఎలా", "kaise", "कैसे")
# Staff-side additions to the patient firewall: Kriya never diagnoses, prescribes or tells the sex of a foetus.
CLINICAL = ("diagnose", "diagnosis", "prescribe", "prescription for", "which tablet", "which medicine",
            "what medicine", "dosage", "dose of", "boy or girl", "ladka ya ladki", "ladka hai ya ladki",
            "sex of the baby", "gender of the baby")
OPEN_VERBS = ("open", "go to", "goto", "take me to", "navigate to", "kholo", "kholiye", "khol", "teruvu",
              "teravandi", "le chalo", "खोलो", "खोलिए", "ओपन", "తెరువు", "తెరవండి", "ఓపెన్")
# Words that may surround a page name in a pure navigation request; anything else means "not just nav".
NAV_WORDS = OPEN_VERBS + (
    "go", "to", "show", "show me", "take", "me", "navigate", "the", "page", "tab", "screen", "section",
    "please", "pls", "plz", "kindly", "a", "my", "now", "karo", "kar", "karein", "do", "dikhao", "dikhaiye",
    "chalo", "cheyyi", "cheyandi", "cheyyandi", "chey", "chupinchu", "chupinchandi", "choopinchu", "ki", "ko",
    "ka", "ke", "ni", "lo", "दिखाओ", "दिखाइए", "करो", "पेज", "चलो", "చేయండి", "చేయి", "చూపించు",
    "చూపించండి", "పేజ్", "పేజీ", "=కి", "=ని", "=ను", "=లో", "=को", "=का", "=के", "=की")
# A read (R0) never answers a request to change something: "cancel today's appointments" must not
# quietly list them. Whole words only for ASCII, so "cancelled", "booked", "sent" stay read words.
WRITE_VERBS = ("cancel", "delete", "remove", "add", "create", "change", "update", "edit", "set", "send", "resend",
               "book", "block", "mark", "assign", "confirm", "reject", "refund", "approve", "dismiss", "register",
               "reschedule", "enable", "disable", "turn on", "turn off", "activate", "deactivate", "call next",
               "reset", "clear", "stop", "pause", "resume", "resolve", "check in", "checkin",
               # IPD: imperative forms only, so "discharge list" / "pending discharges" stay reads
               "transfer", "admit the", "discharge all", "discharge the", "discharge karo", "discharge cheyyi",
               "डिस्चार्ज करो",
               "increase", "decrease", "reduce", "raise", "penchu", "tagginchu", "badhao", "ghatao",
               "hatao", "hata do", "badlo", "badal do", "bhejo", "bhej do", "jodo", "jod do", "radd",
               "raddu", "pampu", "pampandi", "marchu", "marchandi", "teeseyi", "teeseyandi",
               "రద్దు", "పంపు", "పంపండి", "మార్చు", "మార్చండి", "తీసేయి", "తొలగించు",
               "रद्द", "भेजो", "भेजें", "बदलो", "बदलें", "हटाओ", "हटाएं", "जोड़ो", "जोड़ें")
CLARIFY_MINUTES = 10
MAX_DOCTOR_CHOICES = 30
# Small talk: a message made ONLY of these (plus filler) is answered, never routed. "good morning" is not
# here: it is the morning-briefing trigger.
SMALL_TALK = {
    "greeting": ("hi", "hii", "hello", "helo", "hey", "hai", "namaste", "namaskar", "namaskaram", "namasthe",
                 "నమస్తే", "నమస్కారం", "హలో", "नमस्ते", "नमस्कार", "हैलो"),
    "thanks": ("thanks", "thank you", "thank u", "thanku", "thx", "dhanyavad", "dhanyavaad", "dhanyavadalu",
               "shukriya", "ధన్యవాదాలు", "धन्यवाद", "शुक्रिया"),
    "capabilities": ("help", "what can you do", "what can u do", "what do you do", "who are you", "what are you",
                     "how can you help", "menu"),
}
_CHAT_FILLER = ("kriya ketu", "kriya", "ketu", "ok", "okay", "please", "pls", "sir", "madam", "ji", "garu",
                "me", "so", "and", "very much", "a lot")
# "Hi, open leads" / "hello kriya, show today's appointments": the greeting is dropped and the rest routed.
_GREETING_PREFIX = re.compile(r"^\s*(?:hi+|hello|helo|hey|hai|namaste|namaskar(?:am)?)\b[\s,.!:-]*"
                              r"(?:(?:kriya\s*)?ketu|kriya)?\b[\s,.!:-]*", re.I)
_CHAT_FINDERS = sorted(((kind, w) for kind, words in SMALL_TALK.items() for w in words),
                       key=lambda x: -len(x[1]))


def _now() -> datetime:
    return datetime.now(IST)


def _hits(t: str, trigger: str) -> bool:
    """A trigger matches when present; "a+b" needs every part ("cancel+leave": both words, any order)."""
    return all(has_any(t, (_norm(part),)) for part in trigger.split("+"))


@dataclass(frozen=True)
class Route:
    kind: str                         # nav | help | action | clarify | rejected | none
    page: Optional[str] = None
    action_id: Optional[str] = None
    params: dict = field(default_factory=dict)
    error_code: Optional[str] = None  # negated | hypothetical | clinical | not_available | unsupported
    missing: tuple = ()
    candidates: tuple = ()            # e.g. the doctors an ambiguous name could mean
    candidate_actions: tuple = ()
    help: Optional[HelpEntry] = None


def _norm(text: str) -> str:
    t = (text or "").lower().replace("’", "'").replace("‌", "").replace("‍", "")
    # Keep the Indic blocks whole: their vowel signs are not \w, so a \W strip would cut words apart.
    t = re.sub(r"[^\w\s&'ऀ-ൿ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _finder(word: str) -> re.Pattern:
    if word.startswith("="):  # whole token, any script
        return re.compile(rf"(?:^|(?<=\s)){re.escape(word[1:])}(?=$|\s)")
    if word.isascii():
        return re.compile(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])")
    return re.compile(re.escape(word))  # Indic: suffixes attach, so substring


_PAGE_NAMES = sorted({(_norm(n), page) for page, label in PAGES.items()
                      for n in (page, label, *PAGE_SYNONYMS.get(page, ()))}, key=lambda x: -len(x[0]))
_PAGE_FINDERS = [(_finder(n), page) for n, page in _PAGE_NAMES if n]
_NAV_FINDERS = [_finder(w if w.startswith("=") else _norm(w)) for w in sorted(NAV_WORDS, key=len, reverse=True)]


def small_talk(text: str) -> Optional[str]:
    """"greeting" | "thanks" | "capabilities" when the message is only that (and filler), else None.
    Greeting + help ("hi, what can you do") is "capabilities"; anything else in it means a real request."""
    t = _norm(text)
    found = set()
    for kind, word in _CHAT_FINDERS:
        rx = _finder(word)
        if rx.search(t):
            found.add(kind)
            t = rx.sub(" ", t)
    for word in sorted(_CHAT_FILLER, key=len, reverse=True):
        t = _finder(word).sub(" ", t)
    if not found or t.strip():
        return None
    return "capabilities" if "capabilities" in found else "thanks" if "thanks" in found else "greeting"


def starts_new_request(text: str) -> bool:
    """A sentence with a command verb of its own ("change Dr Anitha's fee to 600") is a new request,
    never a typed answer to the form above it, even when it contains a value the form is missing."""
    return has_any(_norm(text), WRITE_VERBS)


def _page_spans(t: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for rx, page in _PAGE_FINDERS:  # longest names first; never overlap a longer match
        for m in rx.finditer(t):
            if all(m.end() <= s or m.start() >= e for s, e, _ in spans):
                spans.append((m.start(), m.end(), page))
    return sorted(spans)


def pages_mentioned(text: str) -> list[str]:
    seen: list[str] = []
    for _, _, page in _page_spans(_norm(text)):
        if page not in seen:
            seen.append(page)
    return seen


def _nav_page(t: str) -> Optional[str]:
    """The page, if the text is ONLY a page name plus navigation words."""
    spans = _page_spans(t)
    if len({p for _, _, p in spans}) != 1:
        return None
    rest = t
    for s, e, _ in reversed(spans):
        rest = rest[:s] + " " + rest[e:]
    for rx in _NAV_FINDERS:
        rest = rx.sub(" ", rest)
    return spans[0][2] if not rest.strip() else None


def _help(t: str, visible: Optional[set], page: Optional[str] = None) -> Optional[HelpEntry]:
    entries = [e for e in HELP if (visible is None or e.page in visible) and (page is None or e.page == page)]
    scored = [(last_index(t, e.keywords), e) for e in entries]
    scored = [(i, e) for i, e in scored if i >= 0] or ([(0, entries[0])] if page and entries else [])
    return max(scored, key=lambda x: x[0])[1] if scored else None  # the head noun comes last


def find_help(text: str, visible: Optional[set] = None, page: Optional[str] = None) -> Optional[HelpEntry]:
    return _help(_norm(text), visible, page)


def _resolve_slot(slot: SlotSpec, text: str, doctors: Optional[list], now: Optional[datetime]):
    """(value or None, candidates for the clarify form)."""
    if slot.kind == "date":
        d = resolve.resolve_date(text, now=now)
        return (d.isoformat() if d else None), ()
    if slot.kind == "date_end":  # only a stated range has an end; never asked for
        r = resolve.resolve_range(text, now=now)
        return (r[1].isoformat() if r else None), ()
    if slot.kind == "leave_type":  # a named session = half day; none = full day
        return resolve.resolve_leave_type(text), ()
    if slot.kind == "amount":
        found = resolve.resolve_amount(text)
        return ([found[0], str(found[1])] if found else None), ()  # JSON-safe for assistant_runs.params
    if slot.kind == "term":
        return resolve.resolve_term(text), ()
    if slot.kind == "booking":
        return resolve.resolve_booking(text), ()
    if slot.kind == "lead":      # who, ignoring the quoted / after-colon message
        return resolve.resolve_lead(text), ()
    if slot.kind == "message":   # verbatim staff text only; never composed
        return resolve.resolve_message(text), ()
    if slot.kind == "holiday_name":  # optional; the preview uses "Clinic closed" when none is given
        return resolve.resolve_holiday_name(text), ()
    if slot.kind == "on_off":
        return resolve.resolve_on_off(text), ()
    if slot.kind == "segment":
        return resolve.resolve_segment(text), ()
    if slot.kind == "lead_days":
        return resolve.resolve_lead_days(text), ()
    if slot.kind == "interest_term":
        return resolve.resolve_interest_term(text), ()
    if slot.kind == "yesno":  # consent: only ever answered on the form, never read from a sentence (REG-03)
        return None, ()
    if slot.kind == "person_name":
        return resolve.resolve_person_name(text), ()
    if slot.kind == "phone":
        return resolve.resolve_phone(text), ()
    if slot.kind == "new_person":
        return resolve.resolve_new_person(text), ()
    if slot.kind == "visit_type":
        return resolve.resolve_visit_type(text), ()
    if slot.kind == "queue_target":
        return resolve.resolve_queue_target(text), ()
    if slot.kind == "stage":
        return resolve.resolve_stage(text), ()
    if slot.kind == "lab_test":
        return resolve.resolve_lab_test(text), ()
    if slot.kind == "price_term":
        return resolve.resolve_price_term(text), ()
    if slot.kind == "treatment":
        return resolve.resolve_treatment(text), ()
    if slot.kind == "staff_name":
        return resolve.resolve_staff_name(text), ()
    if slot.kind == "staff_role":
        return resolve.resolve_staff_role(text), ()
    if slot.kind == "session":
        return resolve.resolve_session(text), ()
    if slot.kind == "session_window":   # evening hours without am/pm are afternoon hours
        return resolve.resolve_session_window(text, resolve.resolve_session(text) or "morning"), ()
    if slot.kind in ("morning_window", "evening_window"):
        session = slot.kind.split("_")[0]
        return resolve.resolve_window_after(text, session) or resolve.resolve_bare_windows(text).get(session), ()
    if slot.kind == "days":
        return resolve.resolve_days(text), ()
    if slot.kind == "new_doctor":
        return resolve.resolve_new_doctor(text), ()
    if slot.kind == "text_field":       # only ever answered on the form
        return None, ()
    if slot.kind == "branch":
        return resolve.resolve_branch(text), ()
    if slot.kind == "assign_session":
        return resolve.resolve_assign_session(text), ()
    if slot.kind == "treatment_scope":
        return resolve.resolve_treatment_scope(text), ()
    if slot.kind == "show_hide":
        return resolve.resolve_show_hide(text), ()
    if slot.kind == "patient_for":
        return resolve.resolve_patient_for(text), ()
    if slot.kind == "sitting_patient":
        return resolve.resolve_sitting_patient(text), ()
    if slot.kind == "plan_treatment":
        return resolve.resolve_plan_treatment(text), ()
    if slot.kind == "sittings":
        return resolve.resolve_sittings(text), ()
    if slot.kind == "clock":
        return resolve.resolve_clock(text), ()
    if slot.kind in ("test_name", "quoted"):   # stored as typed: quotes only, else the form asks
        return resolve.resolve_quoted(text), ()
    if slot.kind == "verbatim":    # what the receptionist will say: only ever typed on the form
        return None, ()
    if slot.kind == "about":
        return resolve.resolve_about(text), ()
    if slot.kind == "lex_target":
        return resolve.resolve_lexicon_target(text), ()
    if slot.kind == "voice_setting":
        return resolve.resolve_voice_setting(text), ()
    if slot.kind == "visit_patient":
        return resolve.resolve_visit_patient(text), ()
    if slot.kind == "phleb_name":
        return resolve.resolve_phleb_name(text), ()
    if slot.kind == "price":
        return resolve.resolve_price(text), ()
    if slot.kind == "percent":
        return resolve.resolve_percent_change(text), ()
    if slot.kind == "round_to":
        return resolve.resolve_round(text), ()
    if slot.kind == "lab_scope":
        return resolve.resolve_lab_scope(text), ()
    if slot.kind == "time_window":
        return resolve.resolve_time_window(text), ()
    if slot.kind == "doctor":
        found = resolve.resolve_doctor(text, doctors or [])
        if len(found) == 1:
            return found[0]["name"], ()  # the row's exact name, as the endpoints expect
        # Ambiguous: the matches. No match: the whole list (a picker, never a guess).
        offer = found or [d for d in doctors or [] if d.get("id") and d.get("name")][:MAX_DOCTOR_CHOICES]
        return None, tuple({"id": d["id"], "name": d["name"]} for d in offer)
    raise ValueError(f"unknown slot kind {slot.kind!r}")


def _names_other(t: str, spec: ActionSpec) -> bool:
    """The sentence is about another object ("register doctor Srinivas" is not a patient registration).
    `unless` holds phrases and compiled patterns (matched on the normalised text)."""
    phrases = tuple(_norm(u) for u in spec.unless if isinstance(u, str))
    return (bool(phrases) and has_any(t, phrases)) or any(u.search(t) for u in spec.unless if isinstance(u, re.Pattern))


def _match_action(text: str, t: str, actions: dict, ui_page: Optional[str], doctors, now,
                  visible: Optional[set]) -> Route:
    scored = []
    writing = has_any(t, WRITE_VERBS)
    for spec in actions.values():
        if (writing and spec.risk == "R0") or _names_other(t, spec):
            continue
        # Each matched trigger counts its words: "raddu+selavu" (2) outweighs a bare "selavu" (1).
        hits = sum(len(_norm(trig.replace("+", " ")).split()) for trig in spec.triggers if _hits(t, trig))
        if hits:
            scored.append((hits, spec))
    if not scored:
        return Route("none")
    best = max(h for h, _ in scored)
    top: list[ActionSpec] = [s for h, s in scored if h == best]
    if len(top) > 1:
        on_page = [s for s in top if ui_page and s.page == ui_page]
        if len(on_page) != 1:
            return Route("clarify", missing=("action",), candidate_actions=tuple(s.id for s in top))
        top = on_page
    return fill(top[0], text, doctors=doctors, now=now, question=asks(_instruction(text, top[0])), visible=visible)


_VERBATIM = {"message": True, "quoted": False, "test_name": False}  # slot kind -> a colon also starts it


def _instruction(text: str, spec: ActionSpec) -> str:
    """D-35: a question inside the staff member's own words ('message Ravi "are you coming?"') does not
    make the instruction a question."""
    colons = [_VERBATIM[s.kind] for s in spec.slots if s.kind in _VERBATIM]
    return resolve.without_verbatim(text, any(colons)) if colons else text


def asks(text: str) -> bool:
    """A question, not a request. "only Dr Meera does PRP" states a mapping: its "does" is not a question
    word there (KK-P4-10)."""
    return is_question(_ONLY_DOES.sub(" ", text))


def fill(spec: ActionSpec, text: str, *, doctors=None, now=None, question: bool = False,
         visible: Optional[set] = None, given: Optional[dict] = None) -> Route:
    """Resolve one known action's slots from text (rules, the LLM fallback and clarify replies share
    this). `given` holds slots already answered on a clarify form; they are not re-parsed."""
    now = now or _now()
    if (spec.risk == "R0" and has_any(_norm(text), WRITE_VERBS)) or (given is None and _names_other(_norm(text), spec)):
        return Route("none")  # also guards the LLM fallback and clarify replies, which come through here
    if spec.risk == "H" and visible is not None and spec.handoff.page not in visible:
        return Route("rejected", page=spec.handoff.page, action_id=spec.id, error_code="not_available")  # T-29
    if spec.risk != "R0" and question:
        return Route("help", page=spec.page, action_id=spec.id, help=_help(_norm(text), visible, spec.page))
    params, missing, candidates = {}, [], ()
    for slot in spec.slots:
        if given and slot.name in given:
            params[slot.name] = given[slot.name]
            continue
        value, cands = _resolve_slot(slot, text, doctors, now)
        if value is not None:
            params[slot.name] = value
        elif slot.required:
            missing.append(slot.name)
            candidates = candidates or cands
    if spec.one_of and not any(params.get(n) for n in spec.one_of):
        missing.extend(spec.one_of)  # e.g. a new doctor's morning or evening hours: the form asks both
    if missing:
        return Route("clarify", action_id=spec.id, params=params, missing=tuple(missing), candidates=candidates)
    return Route("action", action_id=spec.id, params=params)


# Slot kinds a typed follow-up ("mornings 9 to 12", "₹600", "Mon-Sat") may answer: each needs concrete
# evidence in the text. Free-word kinds (a booking, a search term) would read any sentence as an answer.
FOLLOW_UP_KINDS = frozenset({"date", "amount", "price", "percent", "days", "session", "session_window",
                             "morning_window", "evening_window", "time_window", "clock", "phone", "sittings",
                             "new_doctor", "doctor"})


def follow_up(spec: ActionSpec, missing, text: str, given: dict, *, doctors=None, now=None) -> dict:
    """Answers to an open form's `missing` slots read from a typed reply; {} when it answers none."""
    out = {}
    for slot in spec.slots:
        if slot.name not in missing or slot.kind not in FOLLOW_UP_KINDS:
            continue
        if slot.kind == "session_window":  # "5 to 8" after "Dr Rao's evening": the session already said
            session = resolve.resolve_session(text) or given.get("session") or "morning"
            value = resolve.resolve_session_window(text, session)
        else:
            value, _ = _resolve_slot(slot, text, doctors, now or _now())
        if value is not None:
            out[slot.name] = value
    return out


def route(text: str, *, visible_pages: Optional[list] = None, ui_page: Optional[str] = None,
          actions: Optional[dict] = None, doctors: Optional[list] = None,
          now: Optional[datetime] = None) -> Route:
    """Route one staff instruction. `actions` = the actions this user may run (default: registry);
    `visible_pages` = the sidebar pages the user can see (None = unknown, navigation allowed)."""
    actions = {k: s for k, s in (REGISTRY if actions is None else actions).items() if s.triggers}
    visible = None if visible_pages is None else set(visible_pages)
    now = now or _now()
    t = _norm(text)
    if not t:
        return Route("none")
    if screen_message(text)[0] or is_sex_determination_request(text) or has_any(t, CLINICAL):
        return Route("rejected", error_code="clinical")
    if has_any(t, NEGATION):
        return Route("rejected", error_code="negated")
    if has_any(t, HYPOTHETICAL):
        return Route("rejected", error_code="hypothetical")
    chat = small_talk(text)
    if chat:  # the router answers it (what this user can do); nothing is routed or executed
        return Route("help", error_code=chat)
    rest = _GREETING_PREFIX.sub("", text, count=1)
    if rest.strip() and rest != text:
        return route(rest, visible_pages=visible_pages, ui_page=ui_page, actions=actions, doctors=doctors, now=now)
    if has_any(t, HOW_TO):
        entry = _help(t, visible)
        return Route("help", page=entry.page, help=entry) if entry else Route("rejected", error_code="unsupported")
    buttons = [b for trig, b in HANDOFFS if _hits(t, trig)]
    if buttons:  # done on the page by a person (KK-P4-09); the card opens it (the current page's, if several)
        entries = [e for e in HELP if e.button in buttons]
        on_page = next((e for e in entries if e.page == ui_page), None)
        # else the entry whose keyword matched longest ("treatment description" beats "description")
        entry = on_page or max(entries, key=lambda e: max((len(k) for k in e.keywords if has_any(t, (k,))), default=0))
        if visible is not None and entry.page not in visible:
            return _nav(entry.page, visible)  # the page's own "not available" refusal
        return Route("help", page=entry.page, help=entry)

    page = _nav_page(t)
    if page:  # only a page name and navigation words ("chutti", "show leads"): open it, whatever matches
        return _nav(page, visible)
    matched = _match_action(text, t, actions, ui_page, doctors, now, visible)
    if matched.kind != "none":
        return matched
    return _nav(page, visible) if page else Route("none")


def wants_doctors(text: str) -> bool:
    """Whether an action with a doctor slot matches, so the caller should read the doctor list."""
    t = _norm(text)
    return any(any(_hits(t, trig) for trig in s.triggers)
               for s in REGISTRY.values() if any(sl.kind == "doctor" for sl in s.slots))


def _nav(page: str, visible: Optional[set]) -> Route:
    if visible is not None and page not in visible:
        return Route("rejected", page=page, error_code="not_available")
    return Route("nav", page=page)


def match_nav(text: str) -> Optional[str]:
    """Page for a pure navigation request, else None (kept for callers that only need L1)."""
    r = route(text, actions={})
    return r.page if r.kind == "nav" else None
