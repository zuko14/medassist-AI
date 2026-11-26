"""Continuous Care — voice check-in results (ADR-CC-16; docs/continuous-care/CHANNELS.md §5).

The voice receptionist runs the deterministic care script (app/voice/dialog.py care branch) and keeps the answers in
its session state; this module turns them into observations after the call. Only answers the person confirmed are
stored: a spoken adherence yes/no (answer to a closed question), or a reading that was read back and confirmed ('voice_speech_confirmed'). Never an
unconfirmed number. Zero-LLM: no model is called here.
"""

import logging
from typing import Optional

from app.database import sb, scoped_query
from app.services import cc
from app.services.cc import _rows

logger = logging.getLogger(__name__)
MEASURE_LABEL = {"fbg": "fasting sugar", "ppbg": "after-meal sugar", "rbg": "sugar", "bp": "BP", "weight": "weight"}


async def due_items(clinic: dict, enrollment: dict) -> list:
    """What the call should ask: today's adherence times and reading items of the active plan."""
    plan = await cc.current_plan(clinic["id"], enrollment["id"])
    if not plan:
        return []
    act = (_rows(await sb(scoped_query("cc_protocol_activations", clinic["id"], "params").eq("id", enrollment["activation_id"])
                          .limit(1))) or [{}])[0]
    hypo = (act.get("params") or {}).get("hypo_mgdl")
    out = []
    for it in plan["items"]:
        if it["prompt_mode"] == "adherence":
            for t in (it.get("schedule") or {}).get("times") or []:
                label = "morning" if t < "12:00" else "afternoon" if t < "17:00" else "evening"
                if not any(o.get("time") == label for o in out if o["kind"] == "adherence"):
                    out.append({"id": it["id"], "kind": "adherence", "time": label})
        elif it["prompt_mode"] == "reading" and it.get("measure_code") in MEASURE_LABEL:
            out.append({"id": it["id"], "kind": "reading", "measure": it["measure_code"],
                        "measure_label": MEASURE_LABEL[it["measure_code"]],
                        "hypo_below": hypo if it["measure_code"] in ("fbg", "ppbg", "rbg") else None})
    return out[:6]


async def record_call(clinic: dict, call: dict, state: Optional[dict]) -> int:
    """After a care_checkin call: store confirmed answers, evaluate, reset the unanswered streak."""
    care = ((call.get("handoff_packet") or {}).get("care") or {})
    enrollment_id = care.get("enrollment_id")
    if not enrollment_id or not state:
        return 0
    cid = clinic["id"]
    e = (_rows(await sb(scoped_query("cc_enrollments", cid).eq("id", enrollment_id).limit(1))) or [None])[0]
    if not e or e["status"] != "active":
        return 0
    answers = []
    for o in state.get("outcomes") or []:
        if o.get("wf") == "CARE":
            answers += o.get("answers") or []
    n = 0
    for a in answers:
        if a.get("value") is None:
            continue
        if a["kind"] == "adherence":
            row = await cc.record_observation(cid, e["id"], "adherence", code=a["value"], source="voice_speech_confirmed",
                                              message_id=f"voice:{call['id']}:{a.get('item')}:adh")
        else:
            v = str(a["value"])
            v1, v2 = (float(v.split("/")[0]), float(v.split("/")[1])) if "/" in v else (float(v), None)
            row = await cc.record_observation(cid, e["id"], a.get("measure") or "rbg", value=v1, value2=v2,
                                              source="voice_speech_confirmed",
                                              message_id=f"voice:{call['id']}:{a.get('item')}:read")
        n += row is not None
    if n:
        from app.database import supabase
        await sb(supabase.table("cc_enrollments").update({"unanswered_streak": 0}).eq("clinic_id", cid).eq("id", e["id"]))
        await cc.evaluate_enrollment(clinic, e)
    return n
