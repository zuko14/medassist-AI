"""Synthetic understanding benchmark for the voice receptionist.

    python scripts/voice_eval.py            # writes docs/voice/eval-reports/<date>-synthetic.md
    python scripts/voice_eval.py --check    # exit 1 if any gate is below threshold

WHAT THIS MEASURES (and what it does not): deterministic understanding
(app/voice/nlu_rules.py) on >= 10,000 generated text utterances across
Telugu (script + romanised), Hindi (script + romanised), English and code-mix.
It is the SYNTHETIC benchmark of STATUS gate G-EVAL-SYN. It says nothing about
speech recognition, noise, accents or real callers: those are the integration,
shadow and production benchmarks, reported separately (spec section 77).
Failures are listed, never dropped.
"""

import argparse
import itertools
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.voice.dates import IST  # noqa: E402
from app.voice.nlu_rules import NluContext, understand  # noqa: E402

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=IST)  # fixed: results are reproducible
TODAY = NOW.date()
DEPTS = ["Cardiology", "Orthopedics", "Pediatrics", "Gynecology", "Dermatology", "ENT",
         "Ophthalmology", "Dental", "General Medicine"]
CTX = NluContext(doctors=[{"id": "d1", "name": "Dr. Srinivas Rao", "department": "Cardiology"}],
                 departments=DEPTS, now=NOW)

SPEC = {  # canonical -> spoken forms per language family
    "CARDIOLOGY": {"te": ["గుండె", "హార్ట్", "కార్డియాలజీ"], "rte": ["gunde", "heart", "cardiology"],
                   "hi": ["दिल", "हार्ट", "कार्डियोलॉजी"], "rhi": ["dil", "heart", "cardio"],
                   "en": ["heart", "cardiology", "cardiologist"]},
    "ORTHOPEDICS": {"te": ["ఎముకల", "ఆర్థో"], "rte": ["emukala", "ortho", "bone"], "hi": ["हड्डी", "ऑर्थो"],
                    "rhi": ["haddi", "ortho"], "en": ["bone", "orthopedic", "ortho"]},
    "PEDIATRICS": {"te": ["పిల్లల"], "rte": ["pillala", "child"], "hi": ["बच्चों"], "rhi": ["bacche", "child"],
                   "en": ["child", "pediatrician", "kids"]},
    "DERMATOLOGY": {"te": ["చర్మ", "స్కిన్"], "rte": ["skin", "charma"], "hi": ["त्वचा", "स्किन"],
                    "rhi": ["skin"], "en": ["skin", "dermatologist"]},
    "OPHTHALMOLOGY": {"te": ["కంటి"], "rte": ["kanti", "eye"], "hi": ["आंख"], "rhi": ["aankh", "eye"],
                      "en": ["eye", "ophthalmologist"]},
    "DENTAL": {"te": ["పళ్ళ"], "rte": ["pallu", "dental"], "hi": ["दांत"], "rhi": ["daant", "dental"],
               "en": ["dental", "dentist", "teeth"]},
}
DATES = {"te": [("రేపు", 1), ("ఎల్లుండి", 2), ("ఈ రోజు", 0)], "rte": [("repu", 1), ("ellundi", 2), ("ivala", 0)],
         "hi": [("कल", 1), ("परसों", 2), ("आज", 0)], "rhi": [("kal", 1), ("parso", 2), ("aaj", 0)],
         "en": [("tomorrow", 1), ("day after tomorrow", 2), ("today", 0)]}
PERIODS = {"te": [("ఉదయం", "MORNING"), ("సాయంత్రం", "EVENING")], "rte": [("udayam", "MORNING"), ("saayantram", "EVENING")],
           "hi": [("सुबह", "MORNING"), ("शाम", "EVENING")], "rhi": [("subah", "MORNING"), ("shaam", "EVENING")],
           "en": [("morning", "MORNING"), ("evening", "EVENING")]}
BOOK = {"te": ["నాకు {d} {p} {s} డాక్టర్ అపాయింట్మెంట్ కావాలి", "{d} {p} {s} డాక్టర్ దగ్గర అపాయింట్మెంట్ బుక్ చేయండి"],
        "rte": ["naaku {d} {p} {s} doctor appointment kavali", "{d} {p} {s} doctor ki appointment book cheyyandi",
                "naaku {d} {p} {s} doctor kavali"],
        "hi": ["मुझे {d} {p} {s} डॉक्टर का अपॉइंटमेंट चाहिए", "{d} {p} {s} डॉक्टर का अपॉइंटमेंट बुक कर दीजिए"],
        "rhi": ["mujhe {d} {p} {s} doctor ka appointment chahiye", "{d} {p} {s} doctor ka appointment book kar do"],
        "en": ["I need a {s} doctor {d} {p}", "book an appointment with a {s} doctor {d} {p}",
               "can I see a {s} specialist {d} {p}"]}
OTHER = [  # (lang family, text, expected first business intent)
    ("en", "I want to cancel my appointment", "CANCEL_APPOINTMENT"), ("rte", "naa appointment cancel cheyyandi", "CANCEL_APPOINTMENT"),
    ("hi", "मेरा अपॉइंटमेंट कैंसिल कर दो", "CANCEL_APPOINTMENT"), ("rhi", "mera appointment cancel kar do", "CANCEL_APPOINTMENT"),
    ("te", "నా అపాయింట్మెంట్ రద్దు చేయండి", "CANCEL_APPOINTMENT"),
    ("en", "is my report ready", "REPORT_STATUS"), ("rte", "naa report ready ayyinda", "REPORT_STATUS"),
    ("hi", "मेरी रिपोर्ट आ गई क्या", "REPORT_STATUS"), ("te", "నా రిపోర్ట్ వచ్చిందా", "REPORT_STATUS"),
    ("en", "send my report on whatsapp", "REPORT_DELIVERY"), ("rte", "report whatsapp lo pampandi", "REPORT_DELIVERY"),
    ("hi", "रिपोर्ट व्हाट्सएप पर भेज दो", "REPORT_DELIVERY"),
    ("en", "I want to talk to the receptionist", "HUMAN_AGENT_REQUEST"), ("rte", "manishi tho matladali", "HUMAN_AGENT_REQUEST"),
    ("hi", "किसी से बात करनी है", "HUMAN_AGENT_REQUEST"), ("te", "రిసెప్షన్ కి కనెక్ట్ చేయండి", "HUMAN_AGENT_REQUEST"),
    ("en", "where is the hospital", "LOCATION"), ("rte", "hospital address ekkada", "LOCATION"),
    ("hi", "अस्पताल कहाँ है", "LOCATION"), ("te", "హాస్పిటల్ ఎక్కడ ఉంది", "LOCATION"),
    ("en", "what are your timings", "HOSPITAL_INFORMATION"), ("en", "what is the cardiology doctor fee", "FEES"),
    ("rte", "cardiology doctor fees entha", "FEES"), ("hi", "डॉक्टर की फीस कितनी है", "FEES"),
    ("en", "how much is the CBC test", "LAB_TEST_SEARCH"), ("rte", "thyroid test price entha", "LAB_TEST_SEARCH"),
    ("hi", "मुझे कल ब्लड टेस्ट बुक करना है", "LAB_TEST_BOOKING"), ("en", "I want to reschedule my appointment", "RESCHEDULE_APPOINTMENT"),
    ("en", "when is my appointment", "APPOINTMENT_STATUS"), ("en", "please call me back later", "CALLBACK_REQUEST"),
    ("en", "I want to make a complaint", "COMPLAINT"), ("en", "are you a robot", "ASK_IF_AI"),
]


def cases():
    out = []
    for fam, tpls in BOOK.items():
        for canon, forms in SPEC.items():
            for tpl, s, (d, off), (p, per) in itertools.product(tpls, forms[fam], DATES[fam], PERIODS[fam]):
                out.append({"lang": fam, "text": tpl.format(d=d, p=p, s=s), "intent": "BOOK_APPOINTMENT",
                            "specialty": canon, "date": (TODAY + timedelta(days=off)).isoformat(), "period": per})
    for fam, text, intent in OTHER:
        out.append({"lang": fam, "text": text, "intent": intent})
    # politeness / filler variations multiply every case (real callers pad sentences)
    pads = ["", "hello ", "andi ", "please ", "ji "]
    tails = ["", " andi", " please", " sir"]
    return [dict(c, text=(pad + c["text"] + tail).strip()) for c in out for pad in pads for tail in tails]


def run():
    rows, by_lang, by_intent, fails = cases(), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), []
    for c in rows:
        r = understand(c["text"], CTX)
        ok = bool(r.business_intents) and r.business_intents[0] == c["intent"]
        if ok and c["intent"] == "BOOK_APPOINTMENT":
            e = r.entities
            ok = e.get("specialty") == c["specialty"] and e.get("date") == c["date"] and e.get("time_period") == c["period"]
        for bucket, key in ((by_lang, c["lang"]), (by_intent, c["intent"])):
            bucket[key][0] += ok
            bucket[key][1] += 1
        if not ok:
            fails.append((c, r.business_intents, r.entities))
    return rows, by_lang, by_intent, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--threshold", type=float, default=98.0)
    args = ap.parse_args()
    rows, by_lang, by_intent, fails = run()
    total_ok = sum(v[0] for v in by_lang.values())
    pct = lambda v: round(100 * v[0] / v[1], 2)
    overall = round(100 * total_ok / len(rows), 2)
    lines = [f"# Voice synthetic understanding benchmark — {date.today().isoformat()}", "",
             "Deterministic NLU only, text input, fixed date 2026-10-06. NOT a speech, noise or real-caller result.", "",
             f"**Cases:** {len(rows)}  **Correct:** {total_ok}  **Accuracy:** {overall}%  **Gate:** >= {args.threshold}% per language", "",
             "| Language family | Correct | Total | Accuracy |", "|---|---|---|---|"]
    lines += [f"| {k} | {v[0]} | {v[1]} | {pct(v)}% |" for k, v in sorted(by_lang.items())]
    lines += ["", "| Intent | Correct | Total | Accuracy |", "|---|---|---|---|"]
    lines += [f"| {k} | {v[0]} | {v[1]} | {pct(v)}% |" for k, v in sorted(by_intent.items())]
    lines += ["", f"## Failures ({len(fails)}, first 100 shown)", ""]
    lines += [f"- `{c['lang']}` {c['text']!r} -> {got} {ents}" for c, got, ents in fails[:100]]
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "voice", "eval-reports")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{date.today().isoformat()}-synthetic.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{len(rows)} cases, {overall}% overall -> {path}")
    for k, v in sorted(by_lang.items()):
        print(f"  {k}: {pct(v)}%")
    if args.check and any(pct(v) < args.threshold for v in by_lang.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
