"""Kriya Ketu staff assistant — /admin/assistant (architecture.md section 2.4).

Tenant and actor come from the session only (enforce_clinic_access; bodies are
extra="forbid"). A run is visible only to the user who made it: anyone else
gets 404. Corporate-viewer and phlebotomist logins never reach these routes
(verify_credentials confines them to their own path allow-lists).

Routing is zero-token (app/assistant/rules.py); only a miss or a tie costs one
structured LLM call (app/assistant/llm.py). A matched action passes the policy
pre-check (app/assistant/policy.py); R0 runs at once, R1-R3 get a read-only
preview and a plan hash, and only a confirm whose recomputed hash still matches
claims the run (CAS) and executes it through the real /admin endpoints as the
caller (app/assistant/executor.py).
"""

import asyncio
import dataclasses
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.assistant import executor, llm, policy, resolve, rules, store, stt
from app.assistant.executor import Call, CallerAuth
from app.assistant.registry import PAGES, REGISTRY, VARIANTS, read_only_post
from app.config import settings
from app.routers.admin import AdminUser, enforce_clinic_access, verify_credentials
from app.services.ai_gateway import check_admin_spend_cap, record_ai_usage_bg
from app.services.tenant import get_clinic_by_id, kriya_ketu_enabled
from app.utils.security import PersistentRateLimiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/assistant", tags=["assistant"])

_request_limiter = PersistentRateLimiter(max_attempts=120, window_seconds=3600)  # T-18: per user
_stt_limiter = PersistentRateLimiter(max_attempts=60, window_seconds=3600)  # T-18 / B.5: per user
DISPATCH_APP = None  # the app actions are dispatched into; None = app.main.app (tests point it at a fake)
STT_PROVIDER = None  # None = stt.SarvamRest(); tests point it at stt.FakeStt


class UiContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: str = Field(default="", max_length=40)
    # The sidebar pages this user can see. Navigation grants nothing (each page's API checks
    # permissions), so the client's own view is the source of truth for "not available".
    visible_pages: Optional[list[Annotated[str, Field(max_length=40)]]] = Field(default=None, max_length=64)


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: uuid.UUID
    text: str = Field(min_length=1, max_length=1000)
    input_mode: Literal["text", "voice"] = "text"
    transcript_original: Optional[str] = Field(default=None, max_length=1000)
    language: Optional[str] = Field(default=None, max_length=10)
    ui_context: UiContext = Field(default_factory=UiContext)
    reply_to_run_id: Optional[uuid.UUID] = None
    slot_answers: Optional[dict] = None


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_hash: str = Field(min_length=1, max_length=128)
    typed_count: Optional[int] = Field(default=None, ge=0, le=10000)
    ack_duplicate: bool = False


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error_code": code, "message": message})


def run_out(row: dict) -> dict:
    result = row.get("result") or {}
    return {
        "run_id": row["id"],
        "state": row["state"],
        "action_id": row.get("action_id"),
        "route_layer": row.get("route_layer"),
        "message": result.get("message", ""),
        "cards": result.get("cards", []),
        "steps": row.get("steps") or [],
        "plan_hash": row.get("plan_hash"),
        "expires_at": row.get("expires_at"),
        "navigate": result.get("navigate"),
        "error_code": row.get("error_code"),
    }


async def _scope(user: AdminUser, clinic_id: str) -> tuple[str, dict]:
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not settings.assistant_enabled or not kriya_ketu_enabled(clinic):
        raise _err(403, "assistant_disabled", "Kriya Ketu is not enabled for this clinic.")
    return scope, clinic


async def _rate_limited(scope: str, username: str) -> bool:
    return await asyncio.to_thread(_request_limiter.check_and_record, f"assistant:{scope}:{username}")


async def _stt_rate_limited(scope: str, username: str) -> bool:
    return await asyncio.to_thread(_stt_limiter.check_and_record, f"assistant-stt:{scope}:{username}")


_REJECTED = {
    "negated": "Okay, I won't do that.",
    "hypothetical": "I won't change anything for a \"what if\". Say it as an instruction when you're ready.",
    "clinical": "Kriya Ketu doesn't give medical advice, diagnoses or prescriptions. Doctors decide that "
                "in the Consultation page.",
    "not_available": "That page isn't available for your account.",
    "unsupported": "I can't do that yet. Try \"open appointments\" or \"go to leads\".",
    "multi": "Please send one request at a time.",
    "too_many": "That is more items than Kriya Ketu changes in one go. Please narrow it down.",
    "unavailable": "Kriya couldn't prepare that right now. Please try again.",
}
_ACK_DEFAULT = "I understand this may be sent twice"


def _text(message: str, card: str = "text", **extra) -> dict:
    return {"message": message, "cards": [{"type": card, "text": message, **extra}], "navigate": None}


def _fields(r: rules.Route, layer: str) -> dict:
    """Run fields for a routed request. `layer` is the layer that produced a match."""
    if r.kind == "nav":
        result = _text(f"Opening {PAGES[r.page]}.")
        result["navigate"] = r.page
        return {"to": "COMPLETED_VERIFIED", "action_id": "nav.open", "route_layer": "nav" if layer == "rules" else layer,
                "params": {"page": r.page}, "result": result}
    if r.kind == "help":
        answer = r.help.answer if r.help else f"You can do this on the {PAGES.get(r.page, 'right')} page."
        return {"to": "COMPLETED_VERIFIED", "action_id": "help.howto", "route_layer": layer,
                "params": {"page": r.page}, "result": _text(answer, "help", page=r.page)}
    if r.kind == "clarify":
        expires = (datetime.now(timezone.utc) + timedelta(minutes=rules.CLARIFY_MINUTES)).isoformat()
        slots = {s.name: s for s in REGISTRY[r.action_id].slots} if r.action_id in REGISTRY else {}
        card = {"type": "clarify", "missing": list(r.missing), "candidates": list(r.candidates),
                "fields": [{"name": m, "kind": "action" if m == "action" else slots[m].kind if m in slots else "text",
                            **({"label": slots[m].label} if m in slots and slots[m].label else {}),
                            **({"optional": True} if m in slots and not slots[m].required else {})}
                           for m in r.missing],
                "actions": [{"id": a, "summary": REGISTRY[a].summary if a in REGISTRY else a}
                            for a in r.candidate_actions]}
        # Name the form, so staff see at once which task Kriya understood ("Add a doctor", not a patient form).
        message = (f"{REGISTRY[r.action_id].summary}: I need a little more detail." if r.action_id in REGISTRY
                   else "Which of these did you mean?" if r.candidate_actions else "I need a little more detail.")
        return {"to": "NEEDS_CLARIFICATION", "action_id": r.action_id, "route_layer": "form" if layer == "rules" else layer,
                "params": r.params, "expires_at": expires,
                "result": {"message": message, "cards": [card], "navigate": None}}
    if r.kind == "action":
        return {"to": "AUTHORIZED", "action_id": r.action_id, "route_layer": layer, "params": r.params}
    code = r.error_code if r.kind == "rejected" else "unsupported"
    return {"to": "REJECTED", "action_id": r.action_id, "route_layer": layer if r.kind == "rejected" else "none",
            "error_code": code, "result": _text(_REJECTED[code], "help" if code == "unsupported" else "error")}


async def _doctors(auth: CallerAuth, scope: str, run_id: str) -> Optional[list]:
    """The clinic's doctors as the caller sees them (GET /admin/doctors), for the doctor slot."""
    out = await executor.dispatch(Call("GET", "/admin/doctors"), auth, scope, run_id, app=DISPATCH_APP)
    if out.status != "ok" or not isinstance(out.data, list):
        return None
    return [{"id": str(d["id"]), "name": d["name"], **{k: d.get(k) for k in ("morning_slots", "evening_slots")}}
            for d in out.data if isinstance(d, dict) and d.get("id") and d.get("name")]


def _from_llm(intent: llm.Intent, body: RequestIn, visible, doctors=None) -> rules.Route:
    """Re-resolve the model's choice with the same deterministic code the rules use."""
    if intent.kind in ("negated", "hypothetical"):
        return rules.Route("rejected", error_code=intent.kind)
    if intent.kind == "multi":
        return rules.Route("rejected", error_code="multi")
    if intent.kind == "unsupported" or intent.action_id is None:
        return rules.Route("none")
    if intent.action_id == "nav.open":
        return rules.Route("nav", page=intent.slots.page) if intent.slots.page else rules.Route("none")
    if intent.action_id == "help.howto":
        entry = rules.find_help(body.text, visible, intent.slots.page)
        return rules.Route("help", page=entry.page, help=entry) if entry else rules.Route("none")
    surface = " ".join(v for v in (intent.slots.doctor_text, intent.slots.date_text) if v)
    return rules.fill(REGISTRY[intent.action_id], f"{surface} {body.text}".strip(), doctors=doctors,
                      question=intent.kind == "question", visible=visible)


def _variant(r: rules.Route, clinic: dict) -> rules.Route:
    """Same request, the clinic's own endpoint: a check-in is the OPD arrive where OPD is live (KK-FR-061)."""
    alt = VARIANTS.get(r.action_id or "")
    if alt and not _on(REGISTRY[r.action_id], clinic) and _on(REGISTRY[alt], clinic):
        return dataclasses.replace(r, action_id=alt)
    return r


def _on(spec, clinic: dict) -> bool:
    return spec.feature is None or spec.feature(clinic)


async def _route(body: RequestIn, scope: str, user: AdminUser, clinic: dict, auth: CallerAuth,
                 run_id: str) -> dict:
    """Zero-token rules first; one LLM classification only on a miss or a tie (ADR-002)."""
    visible = body.ui_context.visible_pages
    doctors = await _doctors(auth, scope, run_id) if rules.wants_doctors(body.text) else None
    r = _variant(rules.route(body.text, visible_pages=visible, ui_page=body.ui_context.page or None,
                             doctors=doctors), clinic)
    if r.kind == "clarify" and r.candidate_actions:  # a tie: an action switched off here is no candidate
        live = [a for a in r.candidate_actions if _on(REGISTRY[a], clinic)]
        if len(live) == 1:
            r = _variant(rules.fill(REGISTRY[live[0]], body.text, doctors=doctors, question=rules.asks(body.text),
                                    visible=None if visible is None else set(visible)), clinic)
    if not (r.kind == "none" or (r.kind == "clarify" and r.candidate_actions)):
        return _fields(r, "rules")
    if r.kind == "none" and not rules.asks(body.text):
        continued = await _follow_up(body, scope, user, clinic, auth, run_id)
        if continued is not None:
            return continued

    out = await llm.classify(body.text, clinic_id=scope, username=user.username,
                             action_ids=policy.permitted_actions(user, clinic), visible_pages=visible)
    spent = {"llm_tokens": out.tokens, "llm_cost_paise": out.cost_paise}
    if out.status != "ok":
        fields = _fields(r, "rules") if r.kind == "clarify" else _fields(rules.Route("none"), "none")
        if out.status == "invalid":
            fields["route_layer"] = "llm"
        if out.status == "budget" and user.role in ("clinic_admin", "super_admin"):
            note = fields["result"]["message"] + " (This month's AI budget is used up; simple commands still work.)"
            fields["result"] = _text(note, "help")
        return {**fields, **spent}
    spec = REGISTRY.get(out.intent.action_id or "")
    if doctors is None and spec is not None and any(sl.kind == "doctor" for sl in spec.slots):
        doctors = await _doctors(auth, scope, run_id)
    fields = _fields(_variant(_from_llm(out.intent, body, set(visible) if visible is not None else None, doctors),
                              clinic), "llm")
    if fields["to"] == "NEEDS_CLARIFICATION" and out.intent.question_for_user:
        fields["result"]["message"] = out.intent.question_for_user  # plain text; the widget escapes it
    return {**fields, **spent}


async def _follow_up(body: RequestIn, scope: str, user: AdminUser, clinic: dict, auth: CallerAuth,
                     run_id: str) -> Optional[dict]:
    """A typed reply to the form just above it ("mornings 9 to 12" under "Add a doctor") continues that
    request instead of starting a new one: its answers carry over to this run and the old form closes.
    Only the user's latest earlier run, only while its form is open, only slots the reply states with
    concrete evidence (rules.FOLLOW_UP_KINDS). None = not a reply; the caller routes the text as usual."""
    prev = next((r for r in await store.list_runs(scope, user.username, False, 2) if r["id"] != run_id), None)
    if not prev or prev["state"] != "NEEDS_CLARIFICATION":
        return None
    spec = REGISTRY.get(prev.get("action_id") or "")
    card = next((c for c in (prev.get("result") or {}).get("cards") or [] if c.get("type") == "clarify"), {})
    missing = card.get("missing") or []
    if spec is None or not missing or "action" in missing:
        return None
    given = dict(prev.get("params") or {})
    asks_doctor = any(s.kind == "doctor" and s.name in missing for s in spec.slots)
    doctors = await _doctors(auth, scope, run_id) if asks_doctor else None
    answers = rules.follow_up(spec, missing, body.text, given, doctors=doctors)
    offered = {c.get("name") for c in card.get("candidates") or []}
    for s in spec.slots:  # a doctor answer must be one the form offered, as on the form itself
        if s.kind == "doctor" and s.name in answers and offered and answers[s.name] not in offered:
            del answers[s.name]
    if not answers:
        return None
    if not await store.transition(scope, user.username, prev["id"], ("NEEDS_CLARIFICATION",), "CANCELLED",
                                  error_code="continued"):
        return None  # answered or expired meanwhile: route the text on its own
    visible = body.ui_context.visible_pages
    r = rules.fill(spec, prev["input_text"], doctors=doctors, given={**given, **answers},
                   visible=set(visible) if visible is not None else None)
    return _fields(_variant(r, clinic), "form")


def _answers_to_params(spec, card: dict, answers: dict, params: dict) -> dict:
    """Validate clarify-form answers against what the form offered; raise ValueError on anything else.
    A doctor answer must be one of the candidate ids shown; a date must be ISO; nothing is guessed."""
    out = dict(params)
    candidates = {c.get("id"): c.get("name") for c in card.get("candidates") or []}
    for slot in spec.slots:
        if slot.name not in answers:
            continue
        value = answers[slot.name]
        if not slot.required and isinstance(value, str) and not value.strip():
            continue  # an optional field left blank ("Evening hours" for a mornings-only doctor)
        if slot.kind == "doctor":
            if value not in candidates:
                raise ValueError("doctor not offered")
            out[slot.name] = candidates[value]
        elif slot.kind == "date":
            out[slot.name] = date.fromisoformat(str(value)).isoformat()
        elif slot.kind == "amount":
            found = resolve.resolve_amount(str(value))
            if not found:
                raise ValueError("amount not understood")
            out[slot.name] = [found[0], str(found[1])]
        elif slot.kind in ("booking", "lead"):  # typed ref / phone / name: the same resolver as the text
            found = resolve.resolve_booking(str(value)[:100])
            if not found:
                raise ValueError("booking not understood")
            out[slot.name] = found
        elif slot.kind == "yesno":  # consent questions: exactly "yes" or "no", nothing inferred
            if value not in ("yes", "no"):
                raise ValueError("yes or no")
            out[slot.name] = value
        elif slot.kind in ("person_name", "phone", "stage", "queue_target"):
            resolver = {"person_name": resolve.resolve_person_name, "phone": resolve.resolve_phone,
                        "stage": resolve.resolve_stage, "queue_target": resolve.resolve_queue_target}[slot.kind]
            found = resolver(str(value)[:100])
            if not found:
                raise ValueError(f"{slot.kind} not understood")
            out[slot.name] = found
        elif slot.kind == "on_off":
            found = resolve.resolve_on_off(str(value)[:40])
            if not found:
                raise ValueError("on or off")
            out[slot.name] = found
        elif slot.kind in ("patient_for", "sitting_patient"):  # a patient's name as typed, casing kept
            name = " ".join(str(value).split())
            if not (2 <= len(name) <= 100 and re.fullmatch(r"[A-Za-z][A-Za-z .']*", name)):
                raise ValueError("name not understood")
            out[slot.name] = name
        elif slot.kind in ("session", "assign_session"):
            found = resolve.resolve_session(str(value)[:40]) or (
                "both" if slot.kind == "assign_session" and str(value).strip().lower() == "both" else None)
            if not found:
                raise ValueError("morning or evening")
            out[slot.name] = found
        elif slot.kind in ("session_window", "morning_window", "evening_window"):
            session = slot.kind.split("_")[0] if slot.kind != "session_window" else (out.get("session") or "morning")
            found = resolve.resolve_session_window(str(value)[:40], session)
            if not found:
                raise ValueError("hours not understood")
            out[slot.name] = found
        elif slot.kind == "days":
            found = resolve.resolve_days(str(value)[:60])
            if not found:
                raise ValueError("days not understood")
            out[slot.name] = found
        elif slot.kind in ("new_doctor", "text_field"):  # stored as typed
            text = " ".join(str(value).split())
            if not (2 <= len(text) <= 60 and re.fullmatch(r"[A-Za-z][A-Za-z .&()'/-]*", text)):
                raise ValueError(f"{slot.kind} not understood")
            out[slot.name] = text
        elif slot.kind == "branch":
            found = " ".join(str(value).lower().split())[:60]
            if len(found) < 2:
                raise ValueError("branch not understood")
            out[slot.name] = found
        elif slot.kind in ("clock", "show_hide"):
            found = (resolve.resolve_clock if slot.kind == "clock" else resolve.resolve_show_hide)(str(value)[:40])
            if not found:
                raise ValueError(f"{slot.kind} not understood")
            out[slot.name] = found
        elif slot.kind in ("lab_test", "lab_scope", "treatment", "treatment_scope", "plan_treatment", "about",
                           "lex_target", "visit_patient", "phleb_name"):  # search words
            found = " ".join(str(value).lower().split())[:60]
            if len(found) < 2:
                raise ValueError(f"{slot.kind} not understood")
            out[slot.name] = found
        elif slot.kind in ("test_name", "quoted"):  # a new test's name / a caller phrase, stored as typed
            text = " ".join(str(value).split())
            if not 1 <= len(text) <= 100:
                raise ValueError("name length")
            out[slot.name] = text
        elif slot.kind == "verbatim":  # a receptionist question / answer, as typed (the preview checks lengths)
            text = " ".join(str(value).split())
            if not 2 <= len(text) <= 600:
                raise ValueError("text length")
            out[slot.name] = text
        elif slot.kind == "price":  # on the form a bare number is the price in rupees
            v = str(value).strip()[:40]
            found = resolve.resolve_price(v) or (
                ["amount", v.replace(",", "")] if re.fullmatch(r"[1-9][\d,]{0,9}", v) else None)
            if not found:
                raise ValueError("price not understood")
            out[slot.name] = found
        elif slot.kind == "percent":  # "+10" / "-5" (sign required) or "increase 10%"
            v = str(value).strip()[:40]
            m = re.fullmatch(r"([+-])\s*(\d{1,3}(?:\.\d{1,2})?)\s*%?", v)
            signed = (m.group(1).replace("+", "") + m.group(2)) if m and float(m.group(2)) > 0 else None
            found = signed or resolve.resolve_percent_change(v)
            if not found:
                raise ValueError("percent not understood")
            out[slot.name] = found
        elif slot.kind == "time_window":
            found = resolve.resolve_time_window(str(value)[:40])
            if not found:
                raise ValueError("hours not understood")
            out[slot.name] = found
        elif slot.kind == "voice_setting":  # "lead call limit 50": the same resolver as the text
            found = resolve.resolve_voice_setting(str(value)[:120])
            if not found:
                raise ValueError("setting not understood")
            out[slot.name] = found
        elif slot.kind == "message":  # the staff member's own words, sent as typed
            text = str(value).strip()
            if not 1 <= len(text) <= 1000:
                raise ValueError("message length")
            out[slot.name] = text
        else:
            raise ValueError(f"slot {slot.name} cannot be answered on a form")
    if spec.one_of and not any(out.get(n) for n in spec.one_of):
        raise ValueError("one of the hours is needed")
    missing = [s.name for s in spec.slots if s.required and s.name not in out]
    if missing and missing == [m for m in card.get("missing") or [] if m != "action"]:
        raise ValueError("nothing new answered")
    return out


async def _reply(body: RequestIn, request: Request, scope: str, user: AdminUser, clinic: dict) -> dict:
    """A clarify-form answer continues the SAME run: NEEDS_CLARIFICATION -> RECEIVED (CAS), then the
    usual path. Retries and expired forms return the run as it is; nothing is re-executed."""
    rid = str(body.reply_to_run_id)
    row = await store.get_run(scope, user.username, rid)
    if not row:
        raise _err(404, "run_not_found", "That request was not found.")
    if row["state"] != "NEEDS_CLARIFICATION":
        return row
    card = next((c for c in (row.get("result") or {}).get("cards") or [] if c.get("type") == "clarify"), {})
    answers = body.slot_answers or {}
    action_id = row.get("action_id")
    if "action" in (card.get("missing") or []):
        offered = [a.get("id") if isinstance(a, dict) else a for a in card.get("actions") or []]
        if answers.get("action") not in offered:
            raise _err(422, "invalid_answer", "Please pick one of the offered options.")
        action_id = answers["action"]
    spec = REGISTRY.get(action_id or "")
    if spec is None:
        raise _err(422, "invalid_answer", "Please pick one of the offered options.")
    try:
        given = _answers_to_params(spec, card, answers, row.get("params") or {}) if action_id == row.get("action_id")             else dict(row.get("params") or {})
    except (TypeError, ValueError):
        raise _err(422, "invalid_answer", "Please check the details and try again.")
    claimed = await store.transition(scope, user.username, rid, ("NEEDS_CLARIFICATION",), "RECEIVED",
                                     action_id=action_id)
    if not claimed:
        return await store.get_run(scope, user.username, rid) or row
    visible = body.ui_context.visible_pages
    fields = _fields(rules.fill(spec, row["input_text"], given=given,
                                visible=set(visible) if visible is not None else None), "form")
    to_state = fields.pop("to")
    final = await store.transition(scope, user.username, rid, ("RECEIVED",), to_state, **fields) or claimed
    if final["state"] == "AUTHORIZED":
        final = await _run_action(final, spec, user, clinic, CallerAuth.from_request(request), scope)
    return final


def _reader(auth: CallerAuth, scope: str, run_id: str):
    async def read(call: Call):
        if call.method.upper() != "GET" and not read_only_post(call):  # the bulk dry run writes nothing
            raise ValueError("previews only read")
        return await executor.dispatch(call, auth, scope, run_id, app=DISPATCH_APP)
    return read


def _preview_fields(spec, preview: dict, plan_hash: str) -> dict:
    typed = spec.risk == "R3"
    card = {"type": "preview-typed" if typed else "preview", "title": preview["title"],
            "bullets": preview["bullets"], "rows": preview["rows"], "warnings": preview["warnings"],
            "affected": preview["affected"], "requires_ack": bool(preview.get("requires_ack")),
            "ack_text": preview.get("ack_text") or _ACK_DEFAULT}
    if typed:
        card["typed_count"] = preview["affected"]
    expires = (datetime.now(timezone.utc) + timedelta(minutes=policy.PREVIEW_MINUTES)).isoformat()
    return {"plan_hash": plan_hash, "expires_at": expires, "preview": preview,
            "result": {"message": preview["title"], "cards": [card], "navigate": None}}


def _result(spec, row: dict, outcomes: list, params: dict) -> dict:
    """Cards for a finished run. A tick only for COMPLETED_VERIFIED (UX §4.3)."""
    state, steps = row["state"], row.get("steps") or []
    if state == "COMPLETED_VERIFIED":
        if spec.present and spec.risk == "R0":
            try:
                return spec.present(outcomes, params)
            except Exception as e:  # a card bug must not lose a finished read; the page has the data
                logger.error(f"ASSISTANT_PRESENT_FAILED run={row['id']} action={spec.id}: {type(e).__name__}")
                return _text(f"{spec.summary}: open the page to see it.", "help", page=spec.page)
        evidence = [s["evidence"] for s in steps if s.get("evidence")]
        return {"message": f"{spec.summary}: done and verified.", "navigate": None,
                "cards": [{"type": "result-ok", "title": spec.summary, "evidence": evidence, "page": spec.page}]}
    if state in ("UNKNOWN_OUTCOME", "NEEDS_HUMAN"):
        msg = "Kriya couldn't confirm the result. Please check the page before trying again."
        return {"message": msg, "navigate": None, "cards": [{"type": "result-unknown", "text": msg, "page": spec.page}]}
    if state == "PARTIALLY_COMPLETED":
        msg = "Some steps were completed and are still in effect; a later step failed."
        return {"message": msg, "navigate": None, "cards": [{"type": "result-partial", "text": msg, "steps": steps}]}
    failed = next((o for o in reversed(outcomes) if o.status != "ok"), None)
    return _text(failed.message if failed else "Kriya couldn't do that.", "error")


async def _execute(spec, row: dict, steps: list, auth: CallerAuth, params: dict) -> dict:
    final, outcomes = await executor.execute(row, steps, auth, app=DISPATCH_APP)
    saved = await store.transition(final["clinic_id"], final["username"], final["id"], (final["state"],),
                                   final["state"], result=_result(spec, final, outcomes, params))
    return saved or final


def _handoff_result(spec, params: dict) -> dict:
    """The hand-off card: the page, its button and the fields the widget pre-fills (never a password)."""
    h = spec.handoff
    labels = {s.name: s.label or s.name.replace("_", " ").capitalize() for s in spec.slots}
    fields = [{"id": el, "label": labels.get(p, p), "value": str(params[p])[:120]} for el, p in h.fields if params.get(p)]
    text = (f"This needs your click on the {PAGES.get(h.page, h.page)} page"
            + (f": press {h.button}." if h.button else ".") + (f" {h.note}" if h.note else ""))
    card = {"type": "handoff", "page": h.page, "button": h.button, "text": text, "fields": fields}
    return {"message": text, "cards": [card], "navigate": None}


async def _run_action(row: dict, spec, user: AdminUser, clinic: dict, auth: CallerAuth, scope: str) -> dict:
    """AUTHORIZED run: R0 executes now; R1-R3 get a preview to confirm."""
    tr = store.transition
    decision = policy.precheck(spec, user, clinic)
    if not decision.allowed:
        return await tr(scope, user.username, row["id"], ("AUTHORIZED",), "REJECTED", error_code=decision.reason,
                        result=_text(decision.message, "error")) or row
    params = row.get("params") or {}
    if spec.risk == "H":  # KK-FR-025 / SEC-18: a person presses the page's own button; nothing is executed
        return await tr(scope, user.username, row["id"], ("AUTHORIZED",), "COMPLETED_VERIFIED",
                        result=_handoff_result(spec, params)) or row
    if spec.risk == "R0":
        claimed = await tr(scope, user.username, row["id"], ("AUTHORIZED",), "EXECUTING")
        return await _execute(spec, claimed, spec.call(params), auth, params) if claimed else row
    try:
        _, preview, plan_hash = await policy.build_plan(spec, params, _reader(auth, scope, row["id"]))
    except policy.EmptyPlan as e:  # nothing to change: say why (the message is ours, never exception text)
        return await tr(scope, user.username, row["id"], ("AUTHORIZED",), "REJECTED", error_code="nothing_to_do",
                        result=_text(str(e), "help", page=spec.page)) or row
    except policy.PlanError:
        return await tr(scope, user.username, row["id"], ("AUTHORIZED",), "REJECTED", error_code="too_many",
                        result=_text(_REJECTED["too_many"], "error")) or row
    except Exception as e:
        logger.error(f"ASSISTANT_PREVIEW_FAILED run={row['id']} action={spec.id}: {type(e).__name__}")
        return await tr(scope, user.username, row["id"], ("AUTHORIZED",), "REJECTED", error_code="unavailable",
                        result=_text(_REJECTED["unavailable"], "error")) or row
    return await tr(scope, user.username, row["id"], ("AUTHORIZED",), "NEEDS_CONFIRMATION",
                    **_preview_fields(spec, preview, plan_hash)) or row


@router.post("/requests")
async def create_request(body: RequestIn, request: Request, clinic_id: str = "default",
                         user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id)
    if await _rate_limited(scope, user.username):
        raise _err(429, "rate_limited", "Too many requests. Please wait a few minutes.")
    if body.reply_to_run_id:
        return run_out(await _reply(body, request, scope, user, clinic))

    started = time.monotonic()
    row, created = await store.create_run(scope, user, {
        "client_request_id": str(body.client_request_id),
        "input_mode": body.input_mode,
        "input_text": body.text,
        "transcript_original": body.transcript_original,
        "language": body.language,
    }, request.client.host if request.client else "unknown")
    if not created:
        return run_out(row)

    routed = await _route(body, scope, user, clinic, CallerAuth.from_request(request), row["id"])
    spec = REGISTRY.get(routed.get("action_id") or "")
    if routed["to"] == "NEEDS_CLARIFICATION" and spec is not None:
        # Never ask for details (e.g. a patient's consent) for an action this user can't run here.
        decision = policy.precheck(spec, user, clinic)
        if not decision.allowed:
            routed.update(to="REJECTED", error_code=decision.reason, expires_at=None,
                          result=_text(decision.message, "error"))
    to_state = routed.pop("to")
    routed["route_ms"] = int((time.monotonic() - started) * 1000)
    final = await store.transition(scope, user.username, row["id"], ("RECEIVED",), to_state, **routed)
    final = final or await store.get_run(scope, user.username, row["id"]) or row
    if final["state"] == "AUTHORIZED":
        final = await _run_action(final, REGISTRY[final["action_id"]], user, clinic,
                                  CallerAuth.from_request(request), scope)
    logger.info(f"ASSISTANT_RUN run={final['id']} layer={final.get('route_layer')} "
                f"action={final.get('action_id')} state={final['state']} ms={routed['route_ms']}")
    return run_out(final)


@router.get("/runs")
async def list_runs(clinic_id: str = "default", active: bool = False,
                    limit: int = Query(default=30, ge=1, le=100),
                    user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id)
    return [run_out(r) for r in await store.list_runs(scope, user.username, active, limit)]


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: uuid.UUID, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id)
    if not await store.get_run(scope, user.username, str(run_id)):
        raise _err(404, "run_not_found", "That request was not found.")
    row = await store.transition(scope, user.username, str(run_id), store.EXPIRING_STATES, "CANCELLED")
    if not row:
        raise _err(409, "not_cancellable", "This request can no longer be cancelled.")
    return run_out(row)


@router.post("/runs/{run_id}/confirm")
async def confirm_run(run_id: uuid.UUID, body: ConfirmIn, request: Request, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    """NEEDS_CONFIRMATION -> EXECUTING only if: same user, not expired (lazy), sent hash == stored hash ==
    a freshly recomputed hash, R3 typed count matches, duplicate acknowledged when required; CAS on state."""
    scope, clinic = await _scope(user, clinic_id)
    rid = str(run_id)
    row = await store.get_run(scope, user.username, rid)
    if not row:
        raise _err(404, "run_not_found", "That request was not found.")
    spec = REGISTRY.get(row.get("action_id") or "")
    if row["state"] != "NEEDS_CONFIRMATION" or spec is None:
        raise _err(409, "not_confirmable", "This request has nothing waiting for confirmation.")
    if body.plan_hash != row.get("plan_hash"):
        raise _err(409, "stale_plan", "This preview is out of date. Please review the latest one.")

    decision = policy.precheck(spec, user, clinic)
    if not decision.allowed:
        rejected = await store.transition(scope, user.username, rid, ("NEEDS_CONFIRMATION",), "REJECTED",
                                          error_code=decision.reason, result=_text(decision.message, "error"))
        return run_out(rejected or row)

    auth = CallerAuth.from_request(request)
    params = row.get("params") or {}
    try:
        steps, preview, plan_hash = await policy.build_plan(spec, params, _reader(auth, scope, rid))
    except policy.EmptyPlan as e:  # changed since the preview: nothing left to do
        done = await store.transition(scope, user.username, rid, ("NEEDS_CONFIRMATION",), "REJECTED",
                                      error_code="nothing_to_do", result=_text(str(e), "help", page=spec.page))
        return run_out(done or row)
    except Exception as e:
        logger.error(f"ASSISTANT_CONFIRM_PREVIEW_FAILED run={rid} action={spec.id}: {type(e).__name__}")
        raise _err(409, "unavailable", _REJECTED["unavailable"])
    if plan_hash != row["plan_hash"]:
        changed = await store.transition(scope, user.username, rid, ("NEEDS_CONFIRMATION",), "NEEDS_CONFIRMATION",
                                         error_code="plan_changed", **_preview_fields(spec, preview, plan_hash))
        return run_out(changed or row)
    if spec.risk == "R3" and body.typed_count != preview["affected"]:
        raise _err(422, "typed_count_mismatch", f"Type {preview['affected']} to confirm.")
    if preview.get("requires_ack") and not body.ack_duplicate:
        raise _err(422, "ack_required", f"Please tick \"{preview.get('ack_text') or _ACK_DEFAULT}\" to confirm.")

    claimed = await store.transition(scope, user.username, rid, ("NEEDS_CONFIRMATION",), "EXECUTING", error_code=None)
    if not claimed:
        raise _err(409, "not_confirmable", "This request was already handled.")
    return run_out(await _execute(spec, claimed, steps, auth, params))


_STT_DOWN = "Voice service unavailable. Type your request."


async def _read_capped(request: Request, limit: int) -> Optional[bytes]:
    """The body in memory, or None as soon as it passes `limit` (the rest is never buffered)."""
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            return None
    return bytes(buf)


@router.post("/transcribe")
async def transcribe(request: Request, clinic_id: str = "default",
                     language: Literal["auto", "te", "hi", "en", "mixed"] = "auto",
                     duration_ms: int = Query(default=stt.MAX_MS, ge=0, le=stt.MAX_MS),
                     user: AdminUser = Depends(verify_credentials)):
    """Voice -> editable text (ADR-003, speech spec B.2 step 4). The body is the raw MediaRecorder blob,
    not multipart: multipart uploads over 1 MB spool to a temp file, and audio never touches disk (B.5).
    The text goes back to the composer only; nothing here is stored and the transcript is never logged.
    `duration_ms` is the browser's recording time (cost estimate only); absent = billed at the 30 s cap."""
    scope, _ = await _scope(user, clinic_id)
    if not settings.assistant_stt_enabled:
        raise _err(403, "stt_disabled", "Voice input is not enabled.")
    if not settings.sarvam_api_key:
        raise _err(503, "stt_unavailable", _STT_DOWN)
    mime = stt.audio_mime(request.headers.get("content-type"))
    if not mime:
        raise _err(415, "unsupported_audio", "Voice isn't supported in this browser. Type your request.")
    if await _stt_rate_limited(scope, user.username):
        raise _err(429, "rate_limited", "Too many voice requests. Please type, or wait a few minutes.")
    audio = await _read_capped(request, stt.MAX_BYTES)
    if audio is None:
        raise _err(413, "audio_too_large", "That recording is too long. Keep it under 30 seconds.")
    if not audio:
        raise _err(422, "empty_audio", "Recording failed. Try again.")
    if not stt.looks_like(mime, audio):
        raise _err(415, "unsupported_audio", "Voice isn't supported in this browser. Type your request.")
    exceeded, _, _ = await check_admin_spend_cap(scope)
    if exceeded:
        raise _err(503, "stt_budget", "This month's AI budget is used up. Type your request.")

    size, started = len(audio), time.monotonic()
    language_code, mode = stt.LANGUAGES[language]
    provider = STT_PROVIDER or stt.SarvamRest()
    try:
        heard = await provider.transcribe(audio, mime, language_code, mode)
    except Exception as e:
        code = e.code if isinstance(e, stt.SttError) else "failed"
        del audio
        record_ai_usage_bg(clinic_id=scope, task_type="assistant_stt", provider="sarvam",
                           model=settings.assistant_stt_model, prompt_tokens=0, completion_tokens=0,
                           total_tokens=0, cost_paise=0, success=False, error_message=code)
        logger.warning(f"ASSISTANT_STT status={code} error={type(e).__name__} mime={mime} bytes={size} "
                       f"audio_ms={duration_ms} ms={int((time.monotonic() - started) * 1000)}")
        if code == "timeout":
            raise _err(504, "stt_timeout", _STT_DOWN)
        raise _err(502, "stt_failed", _STT_DOWN)
    del audio  # held in request memory only (B.5)
    record_ai_usage_bg(clinic_id=scope, task_type="assistant_stt", provider="sarvam",
                       model=settings.assistant_stt_model, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                       cost_paise=stt.cost_paise(duration_ms, settings.voice_stt_paise_per_minute), success=True)
    logger.info(f"ASSISTANT_STT status=ok lang={heard.language} chars={len(heard.text)} mime={mime} bytes={size} "
                f"audio_ms={duration_ms} ms={int((time.monotonic() - started) * 1000)}")
    return {"text": heard.text, "language": heard.language, "duration_ms": duration_ms}
