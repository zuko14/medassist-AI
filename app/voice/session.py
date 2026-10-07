"""One call, text level: caller utterance in -> sentences to speak out.

Pipeline per turn (fixed order, nothing can skip a step):
  0. takeover / pause flag   -> stop automation, transfer to staff
  1. safety.screen           -> emergency script + transfer | clinical refusal
  2. implicit language vote  -> switch only after 2 consecutive turns
  3. nlu_rules.understand    -> deterministic intents + entities
  4. nlu_llm (only if 3 found nothing) -> validated against tenant data
  5. policy.gate             -> low confidence = "not understood"
  6. dialog.turn             -> one question, or verified tool result
  7. responses.realize       -> the only source of speech; tenant pronunciations applied
Every step is written to voice_call_events; dialog state to voice_calls.dialog.
The audio gateway, the admin Test Console and the eval harness all use this class.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.config import settings

from . import responses as R
from . import store
from .dates import IST, today_ist
from .dialog import DIALOG_VERSION, DialogEngine, new_state
from .intents import TAXONOMY_VERSION
from .lexicon import (LEXICON_VERSION, SPECIALTIES, apply_pronunciations, find_specialties, resolve_department,
                      tenant_department_for)
from .nlu_llm import PROMPT_VERSION, understand_llm
from .nlu_rules import NLU_RULES_VERSION, NluContext, understand
from .policy import POLICY_VERSION, asr_confidence, gate, needs_llm
from .safety import SAFETY_VERSION, screen
from .tools import TOOLS_VERSION, CallContext, KriyaTools

logger = logging.getLogger(__name__)

VERSIONS = {"taxonomy": TAXONOMY_VERSION, "nlu_rules": NLU_RULES_VERSION, "llm_prompt": PROMPT_VERSION,
            "policy": POLICY_VERSION, "dialog": DIALOG_VERSION, "templates": R.TEMPLATES_VERSION,
            "lexicon": LEXICON_VERSION, "safety": SAFETY_VERSION, "tools": TOOLS_VERSION}
QUALITY_VERSION = "quality-v1"
_LABELS = {k: v["label"] for k, v in SPECIALTIES.items()}
_SUCCESS = {"booked", "payment_link_sent", "answered", "report_sent", "cancelled", "refunded", "cancelled_late",
            "rescheduled", "callback_requested", "status_told", "none", "not_ready", "nothing_to_change"}


@dataclass
class Reply:
    texts: list = field(default_factory=list)
    control: str = "continue"           # continue | end | transfer
    handoff_reason: Optional[str] = None


class CallSession:
    def __init__(self, ctx: CallContext, tools=None, dialog_state: Optional[dict] = None,
                 now: Optional[datetime] = None):
        self.ctx = ctx
        self.tools = tools or KriyaTools(ctx)
        self.cfg = store.voice_config(ctx.clinic)
        emergency = self.cfg.get("emergency_number") or (ctx.clinic.get("config") or {}).get("emergency_number")
        self.engine = DialogEngine(self.tools, {
            "hospital": ctx.clinic.get("name") or "", "assistant": self.cfg.get("assistant_name") or "Kriya",
            "emergency": emergency, "hold_minutes": settings.booking_hold_minutes})
        self.state = dialog_state or new_state(self.cfg.get("primary_language") or "te-IN")
        self.now = now
        self._nlu_ctx: Optional[NluContext] = None
        self._lexicon: list = []
        self.llm_tokens = 0
        self.llm_cost_paise = 0
        self.failed_verifications = 0

    # ---- helpers ----

    async def _context(self) -> NluContext:
        if self._nlu_ctx is None:
            from app.database import get_doctors
            try:
                doctors = await get_doctors(self.ctx.clinic_id, branch_id=self.ctx.branch_id)
            except Exception:
                doctors = []
            self._lexicon = await store.load_lexicon(self.ctx.clinic_id)
            self._nlu_ctx = NluContext(
                doctors=[{"id": d["id"], "name": d.get("name") or "", "department": d.get("department") or ""}
                         for d in doctors],
                departments=sorted({d.get("department") for d in doctors if d.get("department")}),
                tenant_entries=self._lexicon, now=self.now)
        return self._nlu_ctx

    def _render(self, out) -> list:
        today = today_ist(self.now)
        texts = []
        for s in out.says:
            try:
                txt = R.realize(s.key, self.state["lang"], today, _LABELS, **s.params)
            except Exception as e:  # a template bug must never kill a live call
                logger.error(f"VOICE_RENDER_FAILED key={s.key}: {e}")
                txt = R.render("system_degraded", self.state["lang"])
            texts.append(apply_pronunciations(txt, self._lexicon))
        return texts

    async def _event(self, kind, name, **kw):
        await store.add_event(self.ctx.clinic_id, self.ctx.call_id, kind, name,
                              correlation_id=self.ctx.correlation_id, **kw)

    async def _emit(self, out) -> Reply:
        texts = self._render(out)
        self.ctx.lang = self.state["lang"]
        for t in texts:
            await self._event("turn_agent", "AGENT_SAID", text=t)
        self.ctx.summary = self.summary()
        fields = {"dialog": self.state, "language": self.state["lang"]}
        biz = [o["wf"] for o in self.state.get("outcomes", [])]
        if self.state.get("task"):
            biz.append(self.state["task"]["wf"])
        if biz:
            fields["primary_intent"] = biz[0]
            fields["intents"] = list(dict.fromkeys(biz))
        await store.update_call(self.ctx.clinic_id, self.ctx.call_id, fields)
        return Reply(texts, out.control, out.handoff_reason)

    def summary(self) -> dict:
        """Handoff packet body: staff must not need to re-ask anything."""
        task = self.state.get("task") or {}
        slots = {k: v for k, v in (task.get("slots") or {}).items()
                 if k in ("department", "specialty", "date", "time_period", "clock_time", "patient_name",
                          "relation", "selected")}
        return {"current_task": task.get("wf"), "collected": slots,
                "completed": self.state.get("outcomes", []),
                "pending": [q["wf"] for q in self.state.get("queue", [])],
                "language": self.state.get("lang")}

    # ---- API ----

    async def start(self, outbound: bool = False, interest: Optional[str] = None) -> Reply:
        await store.update_call(self.ctx.clinic_id, self.ctx.call_id, {"versions": VERSIONS})
        await self._event("system", "CALL_STARTED", status="ok", data={"mode": self.ctx.mode})
        name, slots = None, {}
        if outbound:
            name, slots = await self._lead_context(interest)
        return await self._emit(self.engine.greeting(self.state, outbound=outbound, interest=interest,
                                                     name=name, slots=slots))

    async def _lead_context(self, interest: Optional[str]) -> tuple:
        """(name, booking slots) for a lead call: greet them by name and, when their
        interest is one of this clinic's departments, skip asking it again."""
        name, slots = None, {}
        try:
            prof = await self.tools.caller_profile()
            raw = " ".join(((prof or {}).get("name") or "").split())
            # WhatsApp display names can be emoji or nicknames: speak only a plain 1-3 word name.
            if raw and len(raw) <= 30 and len(raw.split()) <= 3 and all(c.isalpha() or c in " .'" for c in raw):
                name = raw
        except Exception:
            pass
        if interest:
            ctx = await self._context()
            dept = tenant_department_for(interest, ctx.tenant_entries, ctx.departments)
            if not dept:
                specs = find_specialties(interest)
                dept = resolve_department(specs[0], ctx.departments) if specs else None
            if dept:
                slots["department"] = dept
        return name, slots

    async def handle(self, text: str, stt_lang: Optional[str] = None) -> Reply:
        text = (text or "").strip()
        await self._event("turn_user", "CALLER_SAID", text=text, data={"stt_language": stt_lang})

        call = await store.get_call(self.ctx.clinic_id, self.ctx.call_id)
        if call and (call.get("automation_paused") or call.get("takeover_requested_at")):
            await self._event("handoff", "STAFF_TAKEOVER", status="ok")
            out = await self.engine._handoff(self.state, self.engine_out(), "staff_takeover")
            return await self._emit(out)

        verdict = screen(text, self.state["lang"])
        if verdict:
            await self._event("safety", "EMERGENCY" if verdict == "emergency" else "CLINICAL_BLOCKED", status="ok")
            out = await self.engine.emergency(self.state) if verdict == "emergency" else self.engine.clinical(self.state)
            return await self._emit(out)

        switched = self.engine.observe_language(self.state, text)
        ctx = await self._context()
        nlu = understand(text, ctx, self.state.get("expect"))
        if needs_llm(nlu, text):
            nlu, tokens, cost = await understand_llm(text, ctx, self.ctx.clinic_id, self.state.get("expect"))
            self.llm_tokens += tokens
            self.llm_cost_paise += cost
        nlu = gate(nlu, asr_confidence(text), settings.voice_conf_clarify_below)
        await self._event("nlu", "INTENT_DETECTED" if nlu.intents else "NOT_UNDERSTOOD",
                          status="ok" if nlu.confidence else "fail", data=nlu.to_dict(),
                          text=None if nlu.intents else text)  # feeds "top failure phrases"
        out = await self.engine.turn(self.state, nlu, text)
        if switched:
            out.says.insert(0, switched)
        return await self._emit(out)

    async def silence(self, count: int) -> Reply:
        return await self._emit(await self.engine.silence(self.state, count))

    def engine_out(self):
        from .dialog import TurnOutput
        return TurnOutput()

    def quality(self, avg_turn_ms: Optional[int]) -> dict:
        outcomes = [o.get("outcome") for o in self.state.get("outcomes", [])]
        ok = sum(1 for o in outcomes if o in _SUCCESS or o == "handoff_transfer")
        outcome = ok / len(outcomes) if outcomes else 0.0
        turns = max(1, self.state.get("turns", 1))
        clar = 1.0 - min(1.0, self.state.get("misses_total", 0) / turns)
        lat = 1.0 if not avg_turn_ms or avg_turn_ms <= 1500 else max(0.0, 1 - (avg_turn_ms - 1500) / 3000)
        score = round(100 * (0.6 * outcome + 0.2 * clar + 0.2 * lat), 1)
        return {"version": QUALITY_VERSION, "score": score, "outcome": round(outcome, 3),
                "clarity": round(clar, 3), "latency": round(lat, 3), "avg_turn_ms": avg_turn_ms}

    async def finish(self, status: str, telephony_seconds: int = 0, stt_seconds: float = 0.0,
                     tts_chars: int = 0, avg_turn_ms: Optional[int] = None) -> None:
        outcomes = self.state.get("outcomes", [])
        last = outcomes[-1]["outcome"] if outcomes else ("abandoned" if status == "dropped" else None)
        if any(o.get("wf") == "HUMAN" for o in outcomes) and status == "completed":
            status = "handed_off"
        cost = store.usage_cost_paise(stt_seconds, tts_chars, telephony_seconds) + self.llm_cost_paise
        await store.update_call(self.ctx.clinic_id, self.ctx.call_id, {
            "status": status, "outcome": last, "ended_at": datetime.now(IST).isoformat(),
            "telephony_seconds": int(telephony_seconds), "stt_seconds": round(stt_seconds, 2),
            "tts_chars": int(tts_chars), "llm_tokens": self.llm_tokens, "cost_paise": int(cost),
            "quality": self.quality(avg_turn_ms), "dialog": self.state})
        await self._event("system", "CALL_ENDED", status="ok", data={"status": status, "outcome": last})
