"""Kriya Ketu intent accuracy on the staff corpus (tests/ketu_corpus.py).

    python scripts/assistant_intent_eval.py              # rules only: free, offline, deterministic
    python scripts/assistant_intent_eval.py --show       # also list every miss
    OPENROUTER_API_KEY=... python scripts/assistant_intent_eval.py --live
                                                         # + the real LLM fallback on every rule miss

Each sentence is routed exactly as /admin/assistant routes it: the zero-token rules first; with --live, a
rule miss ("none" or a "which did you mean?" tie) goes through the same prompt, schema and re-resolution
(app/assistant/llm.py + routers/assistant._from_llm) the server uses. Reported per set:
  correct  the intended action / page / refusal
  ask      a "which did you mean?" choice (safe, but one extra tap)
  none     not understood (the server answers "I can't do that yet")
  WRONG    a different action: the number that must stay at zero
--live costs a few rupees per run (one call per rule miss). Never run it in CI.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import ketu_corpus  # noqa: E402
from app.assistant import llm, rules  # noqa: E402
from app.assistant.registry import PAGES, REGISTRY  # noqa: E402
from app.voice.dates import IST  # noqa: E402

NOW = datetime(2026, 11, 2, 10, 0, tzinfo=IST)
SETS = ("DEV", "DEV2", "DEV3", "DEV4", "HELDOUT")


def outcome(r: rules.Route) -> str:
    if r.kind == "nav":
        return f"nav:{r.page}"
    if r.kind == "help" and r.error_code in rules.SMALL_TALK:
        return "chat"
    if r.kind == "help":
        return "help"
    if r.kind == "rejected":
        return f"reject:{r.error_code}"
    if r.kind == "clarify" and r.candidate_actions:
        return "ask:" + "|".join(r.candidate_actions)
    if r.kind in ("action", "clarify"):
        return r.action_id
    return "none"


def route(text: str) -> str:
    """rules.route plus the router's tie step (every module switched on), as /admin/assistant does it."""
    docs = ketu_corpus.DOCTORS if rules.wants_doctors(text) else None
    r = rules.route(text, doctors=docs, now=NOW)
    if r.kind == "clarify" and r.candidate_actions:
        live = rules.resolve_tie(text, r.candidate_actions)
        if len(live) == 1:
            r = rules.fill(REGISTRY[live[0]], text, doctors=docs, now=NOW, question=rules.asks(text))
    return outcome(r)


async def via_llm(text: str) -> str:
    """The server's fallback for one sentence: one strict-schema call, then the same deterministic fill."""
    from app.config import settings
    from app.routers.assistant import RequestIn, _from_llm
    from app.services.ai_gateway import call_ai_gateway
    import uuid
    ids, pages = list(REGISTRY), list(PAGES)
    try:
        data = await call_ai_gateway(
            [{"role": "system", "content": llm._system_prompt(ids, pages)},
             {"role": "user", "content": f"Instruction:\n<<<\n{text}\n>>>"}],
            task_type="assistant_intent", primary_model=settings.assistant_llm_model,
            fallback_model=settings.assistant_llm_fallback_model, timeout=settings.assistant_llm_timeout_seconds,
            max_tokens=settings.assistant_llm_max_tokens, temperature=0,
            response_format={"type": "json_schema", "json_schema": {"name": "ketu_intent", "strict": True,
                                                                    "schema": llm.build_schema(ids, pages)}},
            max_attempts=1, provider_prefs={"require_parameters": True})
        intent = llm.Intent.model_validate(json.loads(data["choices"][0]["message"]["content"]))
    except Exception as e:  # an unusable answer is a miss, exactly as on the server
        return f"none({type(e).__name__})"
    if intent.action_id is not None and intent.action_id not in ids:
        return "none(invalid)"
    body = RequestIn(client_request_id=uuid.uuid4(), text=text)
    r = _from_llm(intent, body, None, ketu_corpus.DOCTORS)
    return outcome(r)


def tally(name: str, results: list) -> dict:
    c = {"correct": 0, "ask": 0, "none": 0, "wrong": 0}
    misses = []
    for expected, text, got in results:
        want = expected if isinstance(expected, tuple) else (expected,)
        if got in want:
            c["correct"] += 1
            continue
        kind = "ask" if got.startswith("ask:") else "none" if got.startswith("none") else "wrong"
        c[kind] += 1
        misses.append((kind, want[0], got, text))
    n = len(results)
    print(f"{name:8} n={n:4}  correct {c['correct'] / n:6.1%}   ask {c['ask']:3}   none {c['none']:3}   "
          f"WRONG {c['wrong']:3}")
    return {"n": n, **c, "misses": misses}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="send rule misses to the real LLM fallback (costs money)")
    ap.add_argument("--show", action="store_true", help="list every miss")
    args = ap.parse_args()
    if args.live:
        from app.config import settings
        if not settings.openrouter_api_key:
            print("OPENROUTER_API_KEY is not set")
            return 2
    print("Rules only (zero tokens):" if not args.live else "Rules + live LLM fallback:")
    for name in SETS:
        results = []
        for expected, text in getattr(ketu_corpus, name):
            got = route(text)
            if args.live and (got == "none" or got.startswith("ask:")):
                got = await via_llm(text)
            results.append((expected, text, got))
        out = tally(name, results)
        if args.show:
            for kind, want, got, text in out["misses"]:
                print(f"    {kind:5} want {want:28} got {got[:44]:44} | {text}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
