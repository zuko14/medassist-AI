"""Kriya's own confidence policy. Model confidence is an input, never the decision.

final = min(asr, nlu)          (conservative: the weakest link decides)
final <  clarify_below         -> treated as "not understood" (focused clarification, 3 strikes -> human)
final >= clarify_below         -> the dialog proceeds; every transactional step
                                  STILL requires an explicit spoken "yes" to a
                                  read-back (dialog.py), whatever the confidence.
LLM fallback is consulted only when the rules found nothing and the utterance
has real words in it.

Thresholds come from settings (VOICE_CONF_CLARIFY_BELOW) and are tuned from the
evaluation reports, not guessed.
"""

from dataclasses import replace

from .dates import norm
from .nlu_rules import NLUResult

POLICY_VERSION = "policy-2026.10.07"

# Sounds that carry no request. Telugu "ఆ" and Hindi "हाँ" are NOT here: they mean "yes".
FILLERS = frozenset({"umm", "um", "hmm", "hm", "uh", "ah", "aaa", "huh", "hello", "halo", "hallo", "helo",
                     "oh", "ohh", "ooh", "హలో", "హలో హలో", "హ్మ్", "ఉమ్", "అ", "ఓ", "ఓహ్", "हेलो", "हैलो",
                     "हम्म", "अ", "ओह"})


def is_filler(text: str) -> bool:
    """'Hello?', 'hmm', 'హలో హలో': the caller is checking the line, not asking anything."""
    words = [w.strip(".,!?।") for w in norm(text).split()]
    words = [w for w in words if w]
    return bool(words) and all(w in FILLERS for w in words)


def is_noise(text: str) -> bool:
    """A filler, or a short transcript in a script the call is not in and Kriya does not
    speak: STT turning line noise into one Bengali word ("দহ।", CALL-20261010-33DEF1).
    Neither is a misunderstanding of the caller."""
    from .nlu_rules import SUPPORTED_LANGS, script_language
    if is_filler(text):
        return True
    code = script_language(text or "")
    return bool(code) and code not in SUPPORTED_LANGS and len(norm(text).split()) <= 2


def asr_confidence(text: str) -> float:
    t = norm(text)
    words = [w.strip(".,!?") for w in t.split()]
    words = [w for w in words if w]
    if not words or len(t) < 2:
        return 0.0
    if all(w in FILLERS for w in words):
        return 0.3
    return 1.0


def needs_llm(nlu: NLUResult, text: str) -> bool:
    return nlu.confidence == 0.0 and len(norm(text).split()) >= 2


def gate(nlu: NLUResult, asr_conf: float, clarify_below: float) -> NLUResult:
    final = min(asr_conf, nlu.confidence)
    if final < clarify_below:
        return NLUResult([], {}, final, "none")
    return replace(nlu, confidence=final)
