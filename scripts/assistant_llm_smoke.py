"""Live check of the Kriya Ketu LLM fallback against the REAL OpenRouter API (gate G-KK-LLM).

    OPENROUTER_API_KEY=... python scripts/assistant_llm_smoke.py

Sends 30 staff phrases through the exact schema and prompt app/assistant/llm.py
uses (strict json_schema + provider.require_parameters, one attempt) to the
configured ASSISTANT_LLM_MODEL / ASSISTANT_LLM_FALLBACK_MODEL and reports the
valid-JSON count and p95 latency. Pass: >= 29/30 valid and p95 <= 4 s.
Costs a few rupees. Never run in CI. Each call is recorded in ai_usage_ledger
with task_type="assistant_intent" and no clinic.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.assistant import llm  # noqa: E402
from app.assistant.registry import PAGES, REGISTRY  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.ai_gateway import call_ai_gateway  # noqa: E402

PHRASES = [
    "i want to see who messaged us on whatsapp recently", "take me where the doctors' days off are",
    "where do I change the hospital timings", "the screen with today's queue please",
    "need the place where lab prices are kept", "show the page for staff logins",
    "naaku whatsapp lo vachina leads chupinchu", "doctors ki selavulu ekkada pettali",
    "mujhe aaj ki appointments wali screen dikhao", "lab test ke rate kahan badalte hain",
    "రేపటి అపాయింట్మెంట్లు ఎక్కడ చూడాలి", "వాట్సాప్ లీడ్స్ పేజీ చూపించు",
    "आज की कतार वाली स्क्रीन खोलिए", "डॉक्टर की छुट्टी कहाँ डालते हैं",
    "how would I put a public holiday in", "can you bring up the receptionist call logs",
    "where are the report delivery settings", "open whatever shows payments we received",
    "I need the billing counter screen", "where do I add a new branch location",
    "please don't open the staff page", "what if we closed the clinic on friday",
    "open leads and also call all of them", "tell me a joke", "which tablet for fever",
    "take me to the consultation room page", "patient registration counter kholo",
    "naaku report connector chupinchandi", "show the screen with treatment prices",
    "where can I see this month's analytics for the opd",
]


async def main() -> int:
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY is not set")
        return 2
    action_ids, pages = list(REGISTRY), list(PAGES)
    schema = llm.build_schema(action_ids, pages)
    valid, latencies = 0, []
    for text in PHRASES:
        started = time.monotonic()
        try:
            data = await call_ai_gateway(
                [{"role": "system", "content": llm._system_prompt(action_ids, pages)},
                 {"role": "user", "content": f"Instruction:\n<<<\n{text}\n>>>"}],
                task_type="assistant_intent", primary_model=settings.assistant_llm_model,
                fallback_model=settings.assistant_llm_fallback_model,
                timeout=settings.assistant_llm_timeout_seconds, max_tokens=settings.assistant_llm_max_tokens,
                temperature=0, max_attempts=1, provider_prefs={"require_parameters": True},
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "ketu_intent", "strict": True, "schema": schema}})
            intent = llm.Intent.model_validate(json.loads(data["choices"][0]["message"]["content"]))
            ok = intent.action_id in (*action_ids, None) and intent.slots.page in (*pages, None)
            detail = f"{intent.kind} {intent.action_id} page={intent.slots.page} model={data.get('model')}"
        except Exception as e:
            ok, detail = False, f"ERROR {type(e).__name__}: {str(e)[:120]}"
        latencies.append(time.monotonic() - started)
        valid += ok
        print(f"{'OK ' if ok else 'BAD'} {latencies[-1]:5.2f}s  {text}  ->  {detail}")
    p95 = sorted(latencies)[int(0.95 * (len(latencies) - 1))]
    passed = valid >= 29 and p95 <= 4.0
    print(f"\nvalid JSON {valid}/{len(PHRASES)}  p95 {p95:.2f}s  -> G-KK-LLM {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
