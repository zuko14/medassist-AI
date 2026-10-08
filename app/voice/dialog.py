"""Deterministic dialog engine: the receptionist's state machine.

The LLM never decides a transition. Each turn the engine receives an
NLUResult (rules or validated LLM), updates the task's slots, and either asks
the ONE next question, or calls a typed tool and reports the tool's VERIFIED
result. Speech is a list of Say(key, params) rendered by responses.realize().

State is a plain dict (JSON-serialisable) persisted in voice_calls.dialog
after every turn, so a call survives a process restart and the admin panel
can show exactly where a conversation stands.
"""

from dataclasses import asdict, dataclass, field
from typing import Optional, Protocol

from .intents import WORKFLOW_OF
from .nlu_rules import SUPPORTED_LANGS, NLUResult, is_question, looks_english, script_language
from .policy import is_filler

DIALOG_VERSION = "dialog-2026.10.08b"
MAX_FAILURES = 3
MAX_IDLE = 2          # "hello?" / "hmm" turns answered patiently before they count as misses


@dataclass
class Say:
    key: str
    params: dict = field(default_factory=dict)


@dataclass
class TurnOutput:
    says: list = field(default_factory=list)
    control: str = "continue"          # continue | end | transfer   (internal: handoff_pending)
    handoff_reason: Optional[str] = None


class Tools(Protocol):
    """Typed, tenant-scoped operations. Real implementation: app/voice/tools.py.
    Read methods return None when the underlying system failed (=> degraded
    mode, never a guess). Write methods return a dict whose "status" is the
    VERIFIED outcome (read-back), never the HTTP result of the write."""

    async def find_doctors(self, department: Optional[str], doctor_ids: Optional[list]) -> Optional[list]: ...
    async def find_slots(self, doctors: list, date: str, period: Optional[str], clock: Optional[str]) -> Optional[list]: ...
    async def next_available(self, doctors: list, after_date: str, period: Optional[str]) -> Optional[list]: ...
    async def caller_profile(self) -> Optional[dict]: ...
    async def book(self, option: dict, patient_name: str, relation: str) -> dict: ...
    async def upcoming_appointments(self) -> Optional[list]: ...
    async def cancel(self, appointment: dict) -> dict: ...
    async def reports(self) -> Optional[list]: ...
    async def resend_report(self, report: dict) -> dict: ...
    async def doctor_fees(self, department: Optional[str], doctor_ids: Optional[list]) -> Optional[list]: ...
    async def lab_tests(self, query: str) -> Optional[list]: ...
    async def book_lab(self, test: dict, date: str, patient_name: str) -> dict: ...
    async def info(self, topic: Optional[str]) -> Optional[str]: ...
    async def queue_status(self) -> Optional[dict]: ...
    async def create_callback(self, reason: str) -> dict: ...
    async def handoff(self, reason: str) -> dict: ...
    async def answer_question(self, question: str, lang: str, entities: dict, focus: dict) -> Optional[dict]: ...


def new_state(lang: str) -> dict:
    return {"lang": lang, "task": None, "queue": [], "expect": None, "failures": 0,
            "lang_votes": {}, "outcomes": [], "last_says": [], "last_question": None,
            "turns": 0, "offer": None}


_SLOT_KEYS = ("department", "specialty", "doctor_ids", "date", "time_period", "clock_time",
              "relation", "patient_name")
_REPLAN_KEYS = ("department", "doctor_ids", "date", "time_period", "clock_time")
_ANSWER_KEYS = frozenset(_SLOT_KEYS) | {"option_index", "booking_ref", "info_topic"}
_EMPTY = NLUResult([], {}, 1.0)


class DialogEngine:
    def __init__(self, tools: Tools, clinic: dict):
        """clinic: {"hospital", "assistant", "emergency" (or None), "hold_minutes"}."""
        self.tools = tools
        self.clinic = clinic

    # ---- entry points ----

    def greeting(self, state: dict, outbound: bool = False, interest: Optional[str] = None,
                 name: Optional[str] = None, slots: Optional[dict] = None) -> TurnOutput:
        if outbound:
            # A "yes" goes straight to the department the lead asked about (no re-asking).
            state["offer"] = {"wf": "BOOKING", "slots": dict(slots or {})}
            state["expect"] = "offer"
            state["outbound"] = True
            say = Say("greeting_outbound", {**self._c(), "interest": interest, "name": name,
                                            "pitch": self.clinic.get("pitch") or {}})
            state["last_question"] = asdict(say)
            out = TurnOutput([say])
        else:
            out = TurnOutput([Say("greeting", self._c())])
        return self._finish(state, out)

    async def emergency(self, state: dict) -> TurnOutput:
        out = TurnOutput([Say("emergency", {"emergency": self.clinic.get("emergency")})])
        await self._handoff(state, out, "emergency", announce=False)
        return self._finish(state, out)

    def clinical(self, state: dict) -> TurnOutput:
        state["offer"] = {"wf": "BOOKING", "slots": {}}
        state["expect"] = "offer"
        return self._finish(state, TurnOutput([Say("clinical_refusal")]))

    def observe_language(self, state: dict, text: str) -> Optional[Say]:
        """Implicit switch: two consecutive turns clearly in another supported language.
        A single code-mixed turn never flips the conversation language."""
        code = script_language(text) or ("en-IN" if looks_english(text) else None)
        if not code or code == state["lang"]:
            state["lang_votes"] = {}
            return None
        if code not in SUPPORTED_LANGS:
            if not state.get("told_languages"):
                state["told_languages"] = True
                return Say("unsupported_language")
            return None
        votes = state["lang_votes"]
        votes[code] = votes.get(code, 0) + 1
        if votes[code] >= 2 or (state.get("turns", 0) == 0 and len(text.split()) >= 2):
            state["lang"], state["lang_votes"] = code, {}
            return Say("language_switched")
        return None

    async def silence(self, state: dict, count: int) -> TurnOutput:
        """count = consecutive no-input timeouts in a row (1 = reprompt, 2 = end)."""
        if count >= 2:
            state["outcomes"].append({"wf": "CALL", "outcome": "caller_silent"})
            return self._finish(state, TurnOutput([Say("goodbye_silence")], control="end"))
        return TurnOutput([Say("reprompt_silence")] + self._reprompt(state))

    async def turn(self, state: dict, nlu: NLUResult, text: str) -> TurnOutput:
        out = await self._turn(state, nlu, text)
        if out.control == "handoff_pending":
            out.control = "continue"
            await self._handoff(state, out, out.handoff_reason or "repeated_misunderstanding")
        return self._finish(state, out)

    async def _turn(self, state: dict, nlu: NLUResult, text: str) -> TurnOutput:
        state["turns"] += 1
        out = TurnOutput()
        intents, ents = nlu.intents, nlu.entities
        biz = [i for i in nlu.business_intents if i != "ASK_IF_AI"]
        expect = state.get("expect")

        if nlu.language_request:
            state["lang"] = nlu.language_request
            out.says.append(Say("language_switched"))
            if not biz:
                out.says.extend(self._reprompt(state))
                return out
        if "REPEAT" in intents and not biz:
            out.says = [Say(**s) for s in state["last_says"]] or [Say("didnt_catch")]
            return out
        if "ASK_IF_AI" in intents:
            out.says.append(Say("ai_disclosure", self._c()))
            if not biz:
                out.says.extend(self._reprompt(state))
                return out
        # "Hello?" / "hmm" / a bare "okay" with nothing asked: the caller is checking the line.
        # Answer patiently instead of counting a misunderstanding (production calls were being
        # transferred within seconds because three "hello"s counted as three misses).
        opening_ack = not state.get("task") and not expect and intents and set(intents) <= {"AFFIRM"}
        if (not biz and not (ents.keys() & _ANSWER_KEYS) and not nlu.language_request
                and (is_filler(text) or opening_ack)):
            state["idle"] = state.get("idle", 0) + 1
            if state["idle"] <= MAX_IDLE:
                if expect:
                    out.says.extend(self._reprompt(state) or [Say("listening")])
                else:
                    out.says.append(Say("listening" if state["idle"] == 1 else "clarify"))
                return out
        else:
            state["idle"] = 0

        if "HUMAN_AGENT_REQUEST" in biz:
            # First plain request at the start of a call: offer to do it ourselves, once.
            # Asking again (or after any misunderstanding / mid-task) transfers immediately.
            if (biz == ["HUMAN_AGENT_REQUEST"] and not state.get("human_offered") and not state.get("task")
                    and not state.get("misses_total")):
                state["human_offered"] = True
                state["offer"] = {"wf": "HUMAN", "reason": "caller_requested_human"}
                self._ask(state, out, "offer", Say("offer_self_help"))
                return out
            await self._handoff(state, out, "caller_requested_human")
            return out
        if "GOODBYE" in intents and not biz and expect not in ("confirm", "option", "patient_name"):
            out.says.append(Say("farewell"))
            out.control = "end"
            return out

        # A question about the hospital ("what services do you have?", "what treatments does she
        # do?"), asked at any point: answer it from the clinic's records, then carry on exactly
        # where the call was. Never a miss, never a handoff on its own.
        if "KNOWLEDGE_QUESTION" in biz or ("DOCTOR_INFORMATION" in biz and is_question(text)):
            biz = [b for b in biz if b not in ("KNOWLEDGE_QUESTION", "DOCTOR_INFORMATION")]
            # Only question: answer, then resume. Question + request ("...and book me for
            # tomorrow"): answer, then the request is handled below as usual.
            await self._answer_question(state, out, text, ents, follow_up=not biz)
            if not biz:
                return out

        if expect == "offer" and not biz:
            offer = state.get("offer") or {}
            state["offer"] = None
            if "AFFIRM" in intents and offer:
                if offer.get("wf") == "HUMAN":
                    await self._handoff(state, out, offer.get("reason", "offered"))
                    return out
                state["task"] = {"wf": offer["wf"], "intent": "OFFER", "slots": dict(offer.get("slots") or {})}
                state["expect"] = None
                return await self._run(state, out, nlu, text)
            if "DENY" in intents:
                if offer.get("wf") == "HUMAN":       # "no, don't transfer" -> they want us to help
                    state["expect"] = None
                    out.says.append(Say("how_can_help"))
                    return out
                if state.get("outbound") and not state.get("lead_declined_told"):
                    # A lead who is not booking today still learns they can book on WhatsApp.
                    state["lead_declined_told"] = True
                    out.says.append(Say("lead_declined", {"wa_number": self.clinic.get("whatsapp")}))
                return self._anything_else(state, out)
            if not (ents.keys() & set(_SLOT_KEYS)):
                state["offer"] = offer
                return self._miss(state, out, self._offer_say(offer))
            state["task"] = {"wf": offer.get("wf", "BOOKING"), "intent": "OFFER",
                             "slots": dict(offer.get("slots") or {})}
            state["expect"] = None
            return await self._run(state, out, nlu, text)

        if expect == "anything_else" and not biz:
            if "DENY" in intents or "GOODBYE" in intents:
                out.says.append(Say("farewell"))
                out.control = "end"
                return out
            if "AFFIRM" in intents and not ents:
                state["expect"] = None
                out.says.append(Say("how_can_help"))
                return out

        if biz:
            todo = []  # one (workflow, intent) per workflow, in the order spoken
            for b in biz:
                wf = WORKFLOW_OF.get(b, "UNSUPPORTED")
                if wf not in [w for w, _ in todo]:
                    todo.append((wf, b))
            active = (state.get("task") or {}).get("wf")
            if not (active and [w for w, _ in todo] == [active]):
                first, first_intent = todo[0]
                if active and first != active:
                    state["outcomes"].append({"wf": active, "outcome": "abandoned"})
                if first != active:
                    state["task"] = {"wf": first, "intent": first_intent, "slots": {}}
                    state["expect"] = None
                state["queue"] = [{"wf": w, "intent": i} for w, i in todo[1:] if w != first]

        if not state.get("task"):
            if any(ents.get(k) for k in ("department", "doctor_ids", "specialty", "date", "time_period",
                                         "clock_time")):
                # "Today" / "tomorrow evening" as an opening line at a hospital means an appointment.
                state["task"] = {"wf": "BOOKING", "intent": "BOOK_APPOINTMENT", "slots": {}}
            else:
                return self._miss(state, out, Say("clarify" if state["failures"] == 0 else "didnt_catch"))

        return await self._run(state, out, nlu, text)

    # ---- orchestration ----

    async def _run(self, state: dict, out: TurnOutput, nlu: NLUResult, text: str) -> TurnOutput:
        first = True
        while state.get("task"):
            task = state["task"]
            handler = getattr(self, "_wf_" + task["wf"].lower())
            done = await handler(state, out, nlu if first else _EMPTY, text if first else "")
            first = False
            if out.control != "continue":
                state["task"], state["queue"] = None, []
                break
            if not done:
                break
            cur = state.get("task") or task
            state["outcomes"].append({"wf": cur["wf"], "outcome": cur.get("outcome", "done")})
            state["task"] = None
            if state["queue"] and not state.get("expect"):
                nxt = state["queue"].pop(0)
                state["task"] = {"wf": nxt["wf"], "intent": nxt["intent"], "slots": {}}
                out.says.append(Say("next_task"))
        if out.control == "continue" and not state.get("task") and not state.get("expect"):
            self._anything_else(state, out)
        return out

    async def _answer_question(self, state: dict, out: TurnOutput, text: str, ents: dict,
                               follow_up: bool = True) -> None:
        task = state.get("task") or {}
        slots = task.get("slots") or {}
        sel = slots.get("selected") or {}
        focus = {k: v for k, v in (("doctor_name", sel.get("doctor_name")),
                                   ("department", slots.get("department") or sel.get("department"))) if v}
        ask = getattr(self.tools, "answer_question", None)
        try:
            res = await ask(text, state["lang"], dict(ents), focus) if ask else None
        except Exception:
            res = None
        state["failures"], state["idle"] = 0, 0          # understood: not a miss
        answered = bool(res and res.get("text"))
        out.says.append(Say("kb_answer", {"text": res["text"]}) if answered else Say("kb_unknown"))
        state["outcomes"].append({"wf": "KNOWLEDGE", "outcome": "answered" if answered else "not_answered"})
        if not follow_up:
            return
        if state.get("expect"):
            out.says.extend(self._reprompt(state))      # back to the question that was pending
        elif not state.get("task"):
            dept = ents.get("department") or focus.get("department")
            self._offer(state, out, {"wf": "BOOKING", "slots": {"department": dept} if dept else {}})

    def _finish(self, state: dict, out: TurnOutput) -> TurnOutput:
        state["last_says"] = [asdict(s) for s in out.says]
        return out

    def _anything_else(self, state: dict, out: TurnOutput) -> TurnOutput:
        state["expect"] = "anything_else"
        out.says.append(Say("anything_else"))
        return out

    def _miss(self, state: dict, out: TurnOutput, say: Say) -> TurnOutput:
        """A turn that did not move the conversation forward. Three in a row => human."""
        state["failures"] = state.get("failures", 0) + 1
        state["misses_total"] = state.get("misses_total", 0) + 1
        if state["failures"] >= MAX_FAILURES:
            out.handoff_reason = "repeated_misunderstanding"
            out.control = "handoff_pending"
            return out
        out.says.append(say)
        return out

    @staticmethod
    def _progress(state: dict) -> None:
        state["failures"] = 0

    @staticmethod
    def _reprompt(state: dict) -> list:
        q = state.get("last_question")
        return [Say(**q)] if q and state.get("expect") else []

    def _ask(self, state: dict, out: TurnOutput, expect: str, say: Say) -> bool:
        state["expect"] = expect
        state["last_question"] = asdict(say)
        out.says.append(say)
        return False

    def _offer(self, state: dict, out: TurnOutput, offer: dict) -> None:
        state["offer"] = offer
        self._ask(state, out, "offer", self._offer_say(offer))

    @staticmethod
    def _offer_say(offer: dict) -> Say:
        return Say({"BOOKING": "offer_booking", "LAB_BOOKING": "offer_lab_booking",
                    "HUMAN": "offer_human", "CALLBACK": "offer_callback"}.get(offer.get("wf"), "offer_booking"))

    async def _handoff(self, state: dict, out: TurnOutput, reason: str, announce: bool = True) -> TurnOutput:
        res = await self.tools.handoff(reason) or {}
        state["task"], state["queue"], state["expect"], state["offer"] = None, [], None, None
        out.handoff_reason = reason
        if res.get("mode") == "transfer":
            if announce:
                out.says.append(Say("handoff_transfer"))
            out.control = "transfer"
        else:
            out.says.append(Say("handoff_callback"))
            out.control = "end"
        state["outcomes"].append({"wf": "HUMAN", "outcome": "handoff_" + (res.get("mode") or "callback")})
        return out

    async def _degraded(self, state: dict, out: TurnOutput, task: dict, key: str = "system_degraded") -> bool:
        out.says.append(Say(key))
        await self.tools.create_callback("system_unavailable")
        task["outcome"] = "system_unavailable"
        return True

    def _c(self) -> dict:
        return {"hospital": self.clinic.get("hospital", ""), "assistant": self.clinic.get("assistant", "Kriya")}

    @staticmethod
    def _merge(slots: dict, ents: dict) -> bool:
        """Copy entities into slots. True when a search parameter changed — the
        caller corrected something ("No, evening") and options must be re-planned."""
        changed = False
        for k in _SLOT_KEYS:
            if k in ents and ents[k] is not None and slots.get(k) != ents[k]:
                slots[k] = ents[k]
                if k in _REPLAN_KEYS:
                    changed = True
        if "doctor_ids" in ents:
            slots.pop("doctors", None)
        if "department" in ents and "doctor_ids" not in ents and changed:
            slots.pop("doctor_ids", None)
            slots.pop("doctors", None)
        if "clock_time" in ents and "time_period" not in ents:
            slots.pop("time_period", None)
        if "time_period" in ents and "clock_time" not in ents:
            slots.pop("clock_time", None)
        if changed:
            for k in ("options", "selected", "consented"):
                slots.pop(k, None)
        return changed

    # ---- workflows (return True when the task is finished) ----

    async def _wf_booking(self, state, out, nlu, text) -> bool:
        task = state["task"]
        s = task["slots"]
        ents, intents = nlu.entities, nlu.intents
        expect = state.get("expect")
        if s.get("booking"):
            return True
        changed = self._merge(s, ents)
        if changed or ents.get("patient_name"):
            self._progress(state)

        if expect == "doctor_choice" and not changed:
            idx = ents.get("option_index")
            choices = s.get("doctor_choices") or []
            if idx is not None and idx < len(choices):
                s["doctor_ids"], s["doctors"] = [choices[idx]["id"]], [choices[idx]]
                self._progress(state)
            else:
                self._miss(state, out, Say("ask_which_doctor", self._two_doctors(choices)))
                return False
        elif expect == "option" and s.get("options") and not changed:
            opts = s["options"]
            idx = ents.get("option_index")
            if idx is None and ents.get("clock_time"):
                idx = next((i for i, o in enumerate(opts) if o["time"] == ents["clock_time"]), None)
            if idx is not None and idx < len(opts):
                s["selected"] = opts[idx]
                self._progress(state)
            elif "DENY" in intents:
                s.pop("options", None)
                return self._ask(state, out, "change", Say("ask_change"))
            else:
                self._miss(state, out, self._present(s))
                return False
        elif expect == "confirm" and s.get("selected") and not changed:
            if "AFFIRM" in intents:
                s["consented"] = True
                self._progress(state)
            elif "DENY" in intents:
                for k in ("options", "selected", "consented"):
                    s.pop(k, None)
                return self._ask(state, out, "change", Say("ask_change"))
            else:
                self._miss(state, out, self._confirm_say(s) if s.get("patient_name") else self._present(s))
                return False
        elif expect == "patient_name" and not s.get("patient_name"):
            self._miss(state, out, Say("ask_patient_name"))
            return False
        state["expect"] = None

        if not s.get("department") and not s.get("doctor_ids"):
            if s.get("specialty"):
                out.says.append(Say("no_doctor_for_specialty", {"specialty": s["specialty"]}))
                task["outcome"] = "no_doctor"
                self._offer(state, out, {"wf": "HUMAN", "reason": "specialty_not_offered"})
                return True
            return self._ask(state, out, "specialty", Say("ask_specialty"))

        if "doctors" not in s:
            docs = await self.tools.find_doctors(s.get("department"), s.get("doctor_ids"))
            if docs is None:
                return await self._degraded(state, out, task, "booking_system_down")
            if not docs:
                out.says.append(Say("no_doctor_for_specialty",
                                    {"specialty": s.get("specialty") or s.get("department") or ""}))
                task["outcome"] = "no_doctor"
                return True
            s["doctors"] = docs
            if len(s.get("doctor_ids") or []) > 1 and len(docs) > 1:
                s["doctor_choices"] = docs[:2]
                return self._ask(state, out, "doctor_choice", Say("ask_which_doctor", self._two_doctors(docs)))

        if not s.get("date"):
            return self._ask(state, out, "date", Say("ask_date"))
        if not s.get("time_period") and not s.get("clock_time"):
            return self._ask(state, out, "time_period", Say("ask_time_period"))

        if not s.get("selected"):
            if not s.get("options"):
                opts = await self.tools.find_slots(s["doctors"], s["date"], s.get("time_period"), s.get("clock_time"))
                if opts is None:
                    return await self._degraded(state, out, task, "booking_system_down")
                if not opts:
                    out.says.append(Say("no_slots_on_date", {"date": s["date"]}))
                    alt = await self.tools.next_available(s["doctors"], s["date"], s.get("time_period"))
                    if alt is None:
                        return await self._degraded(state, out, task, "booking_system_down")
                    if not alt:
                        out.says.append(Say("no_slots_any"))
                        task["outcome"] = "no_slots"
                        self._offer(state, out, {"wf": "CALLBACK"})
                        return True
                    s["date"] = alt[0]["date"]
                    opts = alt
                if s.get("clock_time"):
                    exact = [o for o in opts if o["time"] == s["clock_time"]]
                    opts = exact[:1] or opts
                s["options"] = opts[:2]
            if len(s["options"]) == 1:
                s["selected"] = s["options"][0]
                return self._ask(state, out, "confirm", self._present(s))
            return self._ask(state, out, "option", self._present(s))

        if not s.get("patient_name"):
            if (s.get("relation") or "SELF") == "SELF":
                prof = await self.tools.caller_profile()
                if prof and prof.get("name"):
                    s["patient_name"] = prof["name"]
            if not s.get("patient_name"):
                return self._ask(state, out, "patient_name", Say("ask_patient_name"))

        if not s.get("consented"):
            return self._ask(state, out, "confirm", self._confirm_say(s))

        res = await self.tools.book(s["selected"], s["patient_name"], s.get("relation") or "SELF")
        return await self._booking_result(state, out, task, res)

    async def _booking_result(self, state, out, task, res: dict) -> bool:
        s = task["slots"]
        res = res or {}
        status, verified = res.get("status"), bool(res.get("verified"))
        if status == "unavailable" and task["wf"] == "LAB_BOOKING":
            for k in ("date", "consented", "asked"):
                s.pop(k, None)
            return self._ask(state, out, "date", Say("lab_day_unavailable"))
        if status in ("slot_taken", "unavailable"):
            if s.get("retries", 0) < 1 and task["wf"] == "BOOKING":
                s["retries"] = s.get("retries", 0) + 1
                for k in ("options", "selected", "consented"):
                    s.pop(k, None)
                out.says.append(Say("slot_taken"))
                return await self._wf_booking(state, out, _EMPTY, "")
            out.says.append(Say("no_slots_any"))
            task["outcome"] = "slot_unavailable"
            self._offer(state, out, {"wf": "CALLBACK"})
            return True
        if status == "confirmed" and verified:
            s["booking"] = res
            out.says.append(Say("booked_confirmed", {"ref": res.get("booking_ref"),
                                                     "whatsapp_sent": res.get("whatsapp_sent")}))
            task["outcome"] = "booked"
        elif status == "payment_pending" and verified:
            s["booking"] = res
            hold = res.get("hold_minutes") or self.clinic.get("hold_minutes", 10)
            if res.get("link_sent"):
                out.says.append(Say("booked_payment_pending", {"hold": hold, "amount": res.get("amount_paise")}))
                task["outcome"] = "payment_link_sent"
            else:
                out.says.append(Say("payment_link_not_sent", {"hold": hold}))
                await self.tools.create_callback("payment_link_not_delivered")
                task["outcome"] = "held_link_not_delivered"
        elif status in ("confirmed", "payment_pending"):
            out.says.append(Say("booking_unverified"))
            await self.tools.create_callback("booking_unverified")
            task["outcome"] = "unverified"
            return True
        else:
            return await self._degraded(state, out, task, "booking_system_down")

        old = s.get("reschedule_of")
        if old:
            if status == "confirmed":
                c = await self.tools.cancel(old) or {}
                if c.get("status") in ("cancelled", "refunded") and c.get("verified"):
                    out.says.append(Say("old_cancelled"))
                    task["outcome"] = "rescheduled"
                else:
                    await self.tools.create_callback("reschedule_old_not_cancelled")
                    out.says.append(Say("reschedule_old_kept"))
            else:
                await self.tools.create_callback("reschedule_old_pending_payment")
                out.says.append(Say("reschedule_old_kept"))
        return True

    async def _pick_appointment(self, state, out, task, nlu) -> Optional[dict]:
        """Shared by cancel / reschedule. The chosen appointment, or None when
        this turn ended with a question (task['done'] False) or an answer (True)."""
        s = task["slots"]
        if "appts" not in s:
            appts = await self.tools.upcoming_appointments()
            if appts is None:
                await self._degraded(state, out, task)
                task["done"] = True
                return None
            if not appts:
                out.says.append(Say("no_upcoming"))
                task["outcome"], task["done"] = "nothing_to_change", True
                return None
            s["appts"] = appts[:3]
            ref = nlu.entities.get("booking_ref")
            match = [a for a in s["appts"] if ref and a.get("booking_ref") == ref]
            if match or len(s["appts"]) == 1:
                s["selected"] = (match or s["appts"])[0]
            else:
                self._ask(state, out, "option", Say("ask_which_appointment", {"appts": s["appts"]}))
                return None
        if not s.get("selected"):
            idx = nlu.entities.get("option_index")
            if idx is not None and idx < len(s["appts"]):
                s["selected"] = s["appts"][idx]
                self._progress(state)
            else:
                self._miss(state, out, Say("ask_which_appointment", {"appts": s["appts"]}))
                state["expect"] = "option"
                return None
        return s["selected"]

    async def _wf_cancel(self, state, out, nlu, text) -> bool:
        task = state["task"]
        s = task["slots"]
        expect = state.get("expect")
        state["expect"] = None
        appt = await self._pick_appointment(state, out, task, nlu)
        if appt is None:
            return bool(task.get("done"))
        if not s.get("consented"):
            if expect == "confirm" and s.get("asked"):
                if "AFFIRM" in nlu.intents:
                    s["consented"] = True
                elif "DENY" in nlu.intents:
                    task["outcome"] = "cancel_declined"
                    return True
                else:
                    self._miss(state, out, Say("confirm_cancel", {"appt": appt}))
                    state["expect"] = "confirm"
                    return False
            else:
                s["asked"] = True
                return self._ask(state, out, "confirm", Say("confirm_cancel", {"appt": appt}))
        res = await self.tools.cancel(appt) or {}
        key = {"refunded": "cancelled_refunded", "cancelled": "cancelled", "cancelled_late": "cancelled_late",
               "refund_failed": "cancelled_refund_failed"}.get(res.get("status"))
        if not key or not res.get("verified"):
            out.says.append(Say("cancel_failed"))
            await self.tools.create_callback("cancel_failed")
            task["outcome"] = "cancel_failed"
            return True
        out.says.append(Say(key))
        task["outcome"] = res["status"]
        return True

    async def _wf_reschedule(self, state, out, nlu, text) -> bool:
        task = state["task"]
        state["expect"] = None
        appt = await self._pick_appointment(state, out, task, nlu)
        if appt is None:
            return bool(task.get("done"))
        if appt.get("paid") or not appt.get("doctor_id"):
            out.says.append(Say("reschedule_paid_handoff"))
            await self._handoff(state, out, "reschedule_needs_staff", announce=True)
            return True
        state["task"] = {"wf": "BOOKING", "intent": "RESCHEDULE_APPOINTMENT",
                         "slots": {"doctor_ids": [appt["doctor_id"]], "reschedule_of": appt}}
        keep = {k: v for k, v in nlu.entities.items() if k in ("date", "time_period", "clock_time")}
        return await self._wf_booking(state, out, NLUResult([], keep, 1.0), "")

    async def _wf_status(self, state, out, nlu, text) -> bool:
        task = state["task"]
        appts = await self.tools.upcoming_appointments()
        if appts is None:
            return await self._degraded(state, out, task)
        if not appts:
            out.says.append(Say("no_upcoming"))
            task["outcome"] = "none"
            return True
        for a in appts[:2]:
            out.says.append(Say("upcoming_one", {"appt": a}))
        task["outcome"] = "answered"
        return True

    async def _wf_report(self, state, out, nlu, text) -> bool:
        task = state["task"]
        s = task["slots"]
        expect = state.get("expect")
        state["expect"] = None
        if "report" not in s:
            reps = await self.tools.reports()
            if reps is None:
                return await self._degraded(state, out, task)
            if not reps:
                out.says.append(Say("report_none"))
                task["outcome"] = "none"
                return True
            s["report"] = reps[0]
        r = s["report"]
        test = r.get("test_name") or ""
        if r.get("status") != "ready":
            out.says.append(Say("report_not_ready", {"test": test}))
            task["outcome"] = "not_ready"
            return True
        if task.get("intent") not in ("REPORT_DELIVERY", "REPORT_REDELIVERY"):
            if expect == "confirm" and s.get("offered"):
                if "DENY" in nlu.intents:
                    task["outcome"] = "status_told"
                    return True
                if "AFFIRM" not in nlu.intents:
                    self._miss(state, out, Say("report_ready_offer", {"test": test}))
                    state["expect"] = "confirm"
                    return False
            else:
                s["offered"] = True
                return self._ask(state, out, "confirm", Say("report_ready_offer", {"test": test}))
        res = await self.tools.resend_report(r) or {}
        if res.get("status") == "sent":
            out.says.append(Say("report_resent"))
            task["outcome"] = "report_sent"
        else:
            out.says.append(Say("report_resend_failed"))
            await self.tools.create_callback("report_resend_failed")
            task["outcome"] = "report_send_failed"
        return True

    async def _wf_info(self, state, out, nlu, text) -> bool:
        task = state["task"]
        topic = nlu.entities.get("info_topic") or task["slots"].get("topic")
        if not topic:
            # "I need some information": ask which, rather than reading every FAQ aloud.
            if state.get("expect") == "info_topic":
                self._miss(state, out, Say("ask_info_topic"))
                state["expect"] = "info_topic"
                return False
            return self._ask(state, out, "info_topic", Say("ask_info_topic"))
        state["expect"] = None
        answer = await self.tools.info(topic)
        if answer:
            out.says.append(Say("info_answer", {"text": answer}))
            task["outcome"] = "answered"
            return True
        out.says.append(Say("info_unavailable"))
        task["outcome"] = "unavailable"
        state["offer"], state["expect"] = {"wf": "HUMAN", "reason": "info_unavailable"}, "offer"
        return True

    async def _wf_fees(self, state, out, nlu, text) -> bool:
        task = state["task"]
        s = task["slots"]
        self._merge(s, nlu.entities)
        state["expect"] = None
        if not s.get("department") and not s.get("doctor_ids"):
            if s.get("specialty"):
                out.says.append(Say("no_doctor_for_specialty", {"specialty": s["specialty"]}))
                task["outcome"] = "no_doctor"
                return True
            return self._ask(state, out, "specialty", Say("ask_specialty"))
        rows = await self.tools.doctor_fees(s.get("department"), s.get("doctor_ids"))
        if rows is None:
            return await self._degraded(state, out, task)
        rows = [r for r in rows if r.get("fee_paise")]
        if not rows:
            out.says.append(Say("info_unavailable"))
            state["offer"], state["expect"] = {"wf": "HUMAN", "reason": "fee_unavailable"}, "offer"
            task["outcome"] = "unavailable"
            return True
        for r in rows[:2]:
            out.says.append(Say("fee_answer", {"doctor": r["name"], "fee": r["fee_paise"]}))
        task["outcome"] = "answered"
        self._offer(state, out, {"wf": "BOOKING",
                                 "slots": {k: s[k] for k in ("department", "doctor_ids") if s.get(k)}})
        return True

    async def _wf_lab_price(self, state, out, nlu, text) -> bool:
        task = state["task"]
        tests = await self.tools.lab_tests(text)
        if tests is None:
            return await self._degraded(state, out, task)
        tests = [t for t in tests if t.get("price_paise")]
        if not tests:
            out.says.append(Say("lab_not_found"))
            task["outcome"] = "not_found"
            return True
        for t in tests[:2]:
            out.says.append(Say("lab_price", {"test": t["name"], "price": t["price_paise"]}))
        task["outcome"] = "answered"
        self._offer(state, out, {"wf": "LAB_BOOKING", "slots": {"test": tests[0]}})
        return True

    async def _wf_lab_booking(self, state, out, nlu, text) -> bool:
        task = state["task"]
        s = task["slots"]
        ents, intents = nlu.entities, nlu.intents
        expect = state.get("expect")
        state["expect"] = None
        if s.get("booking"):
            return True
        if ents.get("date") and ents["date"] != s.get("date"):
            s["date"] = ents["date"]
            s.pop("consented", None)
        if ents.get("patient_name"):
            s["patient_name"] = ents["patient_name"]
        if not s.get("test"):
            tests = await self.tools.lab_tests(text) if text else []
            if tests is None:
                return await self._degraded(state, out, task, "booking_system_down")
            tests = [t for t in tests if t.get("price_paise")]
            if not tests:
                if expect == "lab_test":
                    self._miss(state, out, Say("ask_lab_test"))
                    state["expect"] = "lab_test"
                    return False
                return self._ask(state, out, "lab_test", Say("ask_lab_test"))
            s["test"] = tests[0]
        if not s.get("date"):
            return self._ask(state, out, "date", Say("ask_collection_date"))
        if not s.get("patient_name"):
            prof = await self.tools.caller_profile()
            if prof and prof.get("name") and (ents.get("relation") or "SELF") == "SELF":
                s["patient_name"] = prof["name"]
            else:
                return self._ask(state, out, "patient_name", Say("ask_patient_name"))
        if not s.get("consented"):
            if expect == "confirm" and s.get("asked"):
                if "AFFIRM" in intents:
                    s["consented"] = True
                elif "DENY" in intents:
                    task["outcome"] = "declined"
                    return True
            if not s.get("consented"):
                s["asked"] = True
                return self._ask(state, out, "confirm", Say("confirm_lab", {
                    "test": s["test"]["name"], "date": s["date"], "patient": s["patient_name"],
                    "fee": s["test"].get("price_paise")}))
        res = await self.tools.book_lab(s["test"], s["date"], s["patient_name"])
        return await self._booking_result(state, out, task, res)

    async def _wf_queue(self, state, out, nlu, text) -> bool:
        task = state["task"]
        q = await self.tools.queue_status()
        if not q:
            out.says.append(Say("no_queue"))
            task["outcome"] = "none"
        else:
            out.says.append(Say("queue_status", {"token": q["token"], "ahead": q["ahead"]}))
            task["outcome"] = "answered"
        return True

    async def _wf_callback(self, state, out, nlu, text) -> bool:
        task = state["task"]
        await self.tools.create_callback(task.get("intent") or "callback_request")
        out.says.append(Say("callback_created"))
        task["outcome"] = "callback_requested"
        return True

    async def _wf_human(self, state, out, nlu, text) -> bool:
        await self._handoff(state, out, "caller_requested_human")
        return True

    async def _wf_unsupported(self, state, out, nlu, text) -> bool:
        intent = state["task"].get("intent")
        out.says.append(Say("unsupported_intent"))
        await self._handoff(state, out, f"unsupported_intent:{intent}")
        return True

    # ---- speech helpers ----

    @staticmethod
    def _two_doctors(docs: list) -> dict:
        return {"d1": docs[0]["name"], "d2": docs[1]["name"] if len(docs) > 1 else docs[0]["name"]}

    @staticmethod
    def _present(s: dict) -> Say:
        opts = s["options"]
        if len(opts) == 1:
            o = opts[0]
            return Say("present_one", {"date": o["date"], "time": o["time"], "doctor": o["doctor_name"],
                                       "fee": o.get("fee_paise")})
        a, b = opts[0], opts[1]
        return Say("present_two", {"date": a["date"], "d1": a["doctor_name"], "t1": a["time"],
                                   "d2": b["doctor_name"], "t2": b["time"]})

    @staticmethod
    def _confirm_say(s: dict) -> Say:
        o = s["selected"]
        return Say("confirm_booking", {"patient": s.get("patient_name") or "", "doctor": o["doctor_name"],
                                       "date": o["date"], "time": o["time"], "fee": o.get("fee_paise")})
