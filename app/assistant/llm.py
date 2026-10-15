"""Kriya Ketu L4 fallback: ONE structured-output OpenRouter call (openrouter-and-speech-spec.md Part A).

Called only when the zero-token rules found nothing (or a tie). The model can only
NAME an action from an enum of the actions this user may run, plus surface slot
strings; no tools, no second turn, no history, no record data. Its output is
re-validated here and re-resolved by the same rules/resolvers, so policy and
confirmation still apply. Every failure is a status, never an exception: the
caller answers deterministically.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.assistant.registry import PAGES, REGISTRY
from app.config import settings
from app.services.ai_gateway import SpendCapExceededError, calculate_cost_paise, call_ai_gateway
from app.utils.security import PersistentRateLimiter, _compiled_patterns, strip_injection_markers

logger = logging.getLogger(__name__)

KINDS = ("command", "question", "hypothetical", "negated", "unsupported", "multi")
SLOT_FIELDS = ("doctor_text", "date_text", "end_date_text", "leave_type", "patient_text", "booking_ref", "page",
               "message_text")
MIN_WORDS = 3
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# The patient-chat injection patterns, minus "show/list ... patient|appointment|record": listing those is
# a staff member's normal job here. (sanitize_user_input is not called: it logs an 80-char preview of the
# text, and assistant input text must never be logged, KK-NFR-OBS-01.)
_INJECTION = [p for p in _compiled_patterns if "patient|appointment" not in p.pattern]
_limiter = PersistentRateLimiter(max_attempts=settings.assistant_llm_rate_per_hour, window_seconds=3600)


class Slots(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doctor_text: Optional[str] = Field(default=None, max_length=80)
    date_text: Optional[str] = Field(default=None, max_length=40)
    end_date_text: Optional[str] = Field(default=None, max_length=40)
    leave_type: Optional[Literal["full", "half_morning", "half_evening"]] = None
    patient_text: Optional[str] = Field(default=None, max_length=80)
    booking_ref: Optional[str] = Field(default=None, max_length=20)
    page: Optional[str] = Field(default=None, max_length=40)
    message_text: Optional[str] = Field(default=None, max_length=500)


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["command", "question", "hypothetical", "negated", "unsupported", "multi"]
    action_id: Optional[str]
    slots: Slots
    question_for_user: Optional[str] = Field(max_length=160)


@dataclass(frozen=True)
class LlmOutcome:
    # ok | disabled | too_short | suspicious | rate_limited | budget | unavailable | invalid
    status: str
    intent: Optional[Intent] = None
    tokens: int = 0
    cost_paise: int = 0


def build_schema(action_ids: list, pages: list) -> dict:
    def text(n):
        return {"type": ["string", "null"], "maxLength": n}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["kind", "action_id", "slots", "question_for_user"],
        "properties": {
            "kind": {"enum": list(KINDS)},
            "action_id": {"enum": [*action_ids, None]},
            "slots": {"type": "object", "additionalProperties": False, "properties": {
                "doctor_text": text(80), "date_text": text(40), "end_date_text": text(40),
                "leave_type": {"enum": ["full", "half_morning", "half_evening", None]},
                "patient_text": text(80), "booking_ref": text(20),
                "page": {"enum": [*pages, None]}, "message_text": text(500)}},
            "question_for_user": text(160),
        },
    }


def _system_prompt(action_ids: list, pages: list) -> str:
    actions = "\n".join(f"- {a}: {REGISTRY[a].summary if a in REGISTRY else a}" for a in action_ids)
    page_list = ", ".join(f"{p} ({PAGES.get(p, p)})" for p in pages)
    return (
        "You classify one instruction from a hospital staff member into exactly one action id from the "
        "list, or null. Reply only with JSON matching the schema.\n"
        "Rules: never invent action ids; never output database ids; copy dates and names as the user wrote "
        "them; kind=negated if the user says NOT to do something; kind=hypothetical for \"what if\"; "
        "kind=multi if they ask for several different things; kind=unsupported if nothing fits. "
        "You never give medical advice.\n"
        f"Actions:\n{actions}\nPages: {page_list}"
    )


async def _rate_limited(clinic_id: str, username: str) -> bool:
    return await asyncio.to_thread(_limiter.check_and_record, f"assistant-llm:{clinic_id}:{username}")


async def classify(text: str, *, clinic_id: str, username: str, action_ids, visible_pages=None) -> LlmOutcome:
    """One classification call, or a status explaining why none was made / used."""
    if not settings.assistant_llm_enabled:
        return LlmOutcome("disabled")
    if len((text or "").split()) < MIN_WORDS:
        return LlmOutcome("too_short")
    cleaned = strip_injection_markers(text)
    if any(p.search(cleaned) for p in _INJECTION):
        logger.warning(f"ASSISTANT_LLM_SKIPPED reason=suspicious user={username}")
        return LlmOutcome("suspicious")
    if await _rate_limited(clinic_id, username):
        return LlmOutcome("rate_limited")

    action_ids = list(action_ids)
    pages = list(visible_pages) if visible_pages is not None else list(PAGES)
    schema = build_schema(action_ids, pages)
    try:
        data = await call_ai_gateway(
            [{"role": "system", "content": _system_prompt(action_ids, pages)},
             {"role": "user", "content": f"Instruction:\n<<<\n{cleaned}\n>>>"}],
            task_type="assistant_intent",
            clinic_id=clinic_id,
            primary_model=settings.assistant_llm_model,
            fallback_model=settings.assistant_llm_fallback_model,
            timeout=settings.assistant_llm_timeout_seconds,
            max_tokens=settings.assistant_llm_max_tokens,
            temperature=0,
            response_format={"type": "json_schema",
                             "json_schema": {"name": "ketu_intent", "strict": True, "schema": schema}},
            max_attempts=1,
            provider_prefs={"require_parameters": True},
        )
    except SpendCapExceededError:
        return LlmOutcome("budget")
    except Exception as e:
        logger.warning(f"ASSISTANT_LLM_UNAVAILABLE error={type(e).__name__}")
        return LlmOutcome("unavailable")

    usage = data.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)
    cost = calculate_cost_paise(usage, tokens)
    try:
        content = data["choices"][0]["message"]["content"]
        intent = Intent.model_validate(json.loads(content))
    except (KeyError, IndexError, TypeError, ValueError, ValidationError):
        return LlmOutcome("invalid", tokens=tokens, cost_paise=cost)
    # Never trust the provider to have enforced the enum.
    if intent.action_id is not None and intent.action_id not in action_ids:
        return LlmOutcome("invalid", tokens=tokens, cost_paise=cost)
    if intent.slots.page is not None and intent.slots.page not in pages:
        return LlmOutcome("invalid", tokens=tokens, cost_paise=cost)
    # The model never supplies ids: drop any slot carrying something that looks like one.
    clean = {k: (None if isinstance(v, str) and _UUID.search(v) else v) for k, v in intent.slots.model_dump().items()}
    intent = intent.model_copy(update={"slots": Slots(**clean)})
    return LlmOutcome("ok", intent, tokens, cost)
