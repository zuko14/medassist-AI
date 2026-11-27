"""Kriya Ketu learning (migration 159): phrases the staff use that the built-in rules did not know.

1. evidence: assistant_runs where the LLM fallback chose the action (route_layer 'llm') AND a person confirmed and
   finished it (state 'COMPLETED_VERIFIED'). Nothing new is collected.
2. distill (suggest(), pure, zero-LLM): per action, the 2–5 word phrases shared by at least MIN_EVIDENCE of those
   sentences, without digits, doctor-name words, negation / hypothetical / clinical words, and not already
   understood by the built-in rules. Computed when the admin opens the list; nothing is stored until a decision.
3. decide (clinic admin): approve -> an 'active' row; dismiss -> never suggested again.
4. use (rules.route, learned=...): an active phrase is an extra trigger for its action, tried ONLY when the built-in
   rules returned nothing and only after the clinical / negation / hypothetical gates, through the same fill, policy,
   preview and confirmation. It can never change what Ketu already understands.
"""
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.assistant import rules
from app.assistant.registry import REGISTRY
from app.database import sb, scoped_query
from app.voice.dates import has_any

logger = logging.getLogger(__name__)

MIN_EVIDENCE = 3      # confirmed instructions that must share the phrase
WINDOW_DAYS = 60      # assistant_runs are purged at 90 days
MAX_SUGGESTIONS = 30
_N = (2, 3, 4, 5)
# Words that carry no instruction on their own; a phrase must hold at least one word outside this set.
_FILLER = frozenset("""a an the to for of on in at by with and or is are was be it this that these those my our his her
their him them me us you your i we please pls kindly can could would will just now today tomorrow yesterday dr doctor
sir madam ji garu hai ka ki ke ko se ne mein lo ni ga ki""".split())
_BLOCKED = rules.NEGATION + rules.HYPOTHETICAL + rules.CLINICAL + rules.MEDICINE_ADVICE


def _ngrams(words: list[str]) -> set[str]:
    out = set()
    for n in _N:
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] in _FILLER or gram[-1] in _FILLER:   # "block his schedule", never "his schedule for"
                continue
            if all(w in _FILLER for w in gram):
                continue
            out.add(" ".join(gram))
    return out


def _known(phrase: str) -> bool:
    """The built-in rules already understand this phrase (as an action, a page or an answer)."""
    return rules.route(phrase, doctors=[]).kind != "none"


def suggest(rows: list[dict], decided: set[tuple[str, str]], doctor_words: set[str]) -> list[dict]:
    """rows: [{"input_text", "action_id"}] from qualifying runs. decided: {(phrase, action_id)} already approved or
    dismissed. Returns [{"phrase", "action_id", "evidence"}], strongest first. Pure: no I/O, no model."""
    by_action: dict[str, list[set[str]]] = defaultdict(list)
    for row in rows:
        action = row.get("action_id")
        if action not in REGISTRY or not REGISTRY[action].triggers:
            continue
        words = [w for w in rules._norm(re.sub(r"\d+", " ", row.get("input_text") or "")).split()
                 if w not in doctor_words]
        by_action[action].append(_ngrams(words))
    out = []
    for action, grams in by_action.items():
        counts: dict[str, int] = defaultdict(int)
        for g in grams:
            for phrase in g:
                counts[phrase] += 1
        strong = sorted(((c, p) for p, c in counts.items() if c >= MIN_EVIDENCE), key=lambda x: (-x[0], -len(x[1])))
        chosen: list[str] = []
        for c, phrase in strong:
            if (phrase, action) in decided or len(phrase) < 3 or len(phrase) > 80:
                continue
            if any(phrase in kept or kept in phrase for kept in chosen):  # keep one of "block schedule" / "block his schedule"
                continue
            if has_any(phrase, _BLOCKED) or _known(phrase):
                continue
            chosen.append(phrase)
            out.append({"phrase": phrase, "action_id": action, "evidence": c})
    out.sort(key=lambda s: -s["evidence"])
    return out[:MAX_SUGGESTIONS]


async def active_phrases(clinic_id: str) -> list[tuple[str, str]]:
    """[(phrase, action_id)] approved for this clinic. Any error -> [] (routing exactly as without learning)."""
    try:
        res = await sb(scoped_query("assistant_learned_phrases", clinic_id).select("phrase, action_id")
                       .eq("status", "active").limit(500))
        return [(r["phrase"], r["action_id"]) for r in (res.data or []) if r.get("action_id") in REGISTRY]
    except Exception as e:
        logger.warning(f"Ketu learned phrases unavailable: {type(e).__name__}")
        return []


async def load_suggestions(clinic_id: str, doctor_names: list[str]) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    runs = await sb(scoped_query("assistant_runs", clinic_id).select("input_text, action_id")
                    .eq("route_layer", "llm").eq("state", "COMPLETED_VERIFIED").gte("created_at", since)
                    .order("created_at", desc=True).limit(2000))
    decided_rows = await sb(scoped_query("assistant_learned_phrases", clinic_id).select("phrase, action_id").limit(2000))
    decided = {(r["phrase"], r["action_id"]) for r in (decided_rows.data or [])}
    doctor_words = {w for n in doctor_names for w in rules._norm(n).split() if w not in ("dr", "doctor")}
    return suggest(runs.data or [], decided, doctor_words)


def needs_doctors(learned: list[tuple[str, str]]) -> bool:
    return any(any(sl.kind == "doctor" for sl in REGISTRY[a].slots) for _, a in learned if a in REGISTRY)


def valid_choice(phrase: str, action_id: str, suggestions: list[dict]) -> Optional[dict]:
    """Only a phrase currently suggested for that action can be approved: the admin cannot inject a trigger."""
    return next((s for s in suggestions if s["phrase"] == phrase and s["action_id"] == action_id), None)
