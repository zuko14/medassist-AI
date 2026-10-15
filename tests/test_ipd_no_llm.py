"""IPD zero-LLM guard (docs/ipd-os/ADR.md ADR-IPD-08).

1. IPD service/router import nothing from the AI, voice or assistant packages:
   clinical state never depends on a model.
2. AI, voice, patient-chat and Kriya Ketu code never name an IPD clinical table:
   the assistant reaches IPD only through the HTTP routes (same permission,
   entitlement and audit path as a human).
"""

import ast
import pathlib

BANNED = ("app.services.ai_engine", "app.services.ai_gateway", "app.services.faq_engine",
          "app.services.hybrid_search", "app.services.report_summarizer", "app.voice", "app.assistant")
IPD_FILES = ("app/services/ipd.py", "app/routers/ipd.py")
CLINICAL_TABLES = ("ipd_observations", "ipd_notes", "ipd_orders", "ipd_med_administrations",
                   "ipd_discharge_summaries", "ipd_admissions")


def _imports(path: pathlib.Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def test_ipd_modules_import_no_ai_voice_or_assistant():
    for rel in IPD_FILES:
        bad = [m for m in _imports(pathlib.Path(rel)) if any(m == b or m.startswith(b + ".") for b in BANNED)]
        assert not bad, f"{rel} imports {bad}"


def test_ai_voice_chat_and_assistant_never_touch_ipd_tables():
    paths = [*pathlib.Path("app/services").glob("ai_*.py"), pathlib.Path("app/services/conversation.py"),
             *pathlib.Path("app/voice").rglob("*.py"), *pathlib.Path("app/assistant").rglob("*.py")]
    for p in paths:
        text = p.read_text(encoding="utf-8")
        hits = [t for t in CLINICAL_TABLES if t in text]
        assert not hits, f"{p} names IPD tables {hits}; go through /admin/ipd routes"
