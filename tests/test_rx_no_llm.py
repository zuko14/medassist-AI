"""Pharmacy OS zero-LLM guard (docs/pharmacy-os/ADR.md ADR-RX-06).

1. Every RX service/router module (app/services/rx*.py, app/routers/rx*.py) imports
   nothing from the AI, voice or assistant packages: item mapping, substitution,
   quantities, refills and stock answers never depend on a model.
2. AI, voice, patient-chat and Kriya Ketu code never names an rx_* table: they reach
   pharmacy data only through the /admin/rx routes (same permission, entitlement, audit).
"""

import ast
import pathlib
import re

from app.tenancy import TENANT_OWNED_TABLES

BANNED = ("app.services.ai_", "app.services.faq_engine", "app.services.hybrid_search",
          "app.services.report_summarizer", "app.voice", "app.assistant",
          "openai", "groq", "anthropic", "google.generativeai")
AI_PATHS = ("app/services/ai_*.py", "app/services/conversation.py", "app/voice/**/*.py", "app/assistant/**/*.py")
RX_TABLES = sorted(t for t in TENANT_OWNED_TABLES if t.startswith("rx_"))
RX_TABLE = re.compile(r"\b(" + "|".join(RX_TABLES) + r")\b")


def _rx_files() -> list[pathlib.Path]:
    return sorted([*pathlib.Path("app/services").glob("rx*.py"), *pathlib.Path("app/routers").glob("rx*.py")])


def _imports(path: pathlib.Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def _banned(mods: set[str]) -> list[str]:
    return sorted(m for m in mods
                  if any(m.startswith(b) if b.endswith("_") else (m == b or m.startswith(b + ".")) for b in BANNED))


def test_there_are_rx_modules_to_check():
    names = {p.as_posix() for p in _rx_files()}
    assert {"app/services/rx.py", "app/routers/rx.py"} <= names, names


def test_rx_modules_import_no_ai_voice_or_assistant():
    for path in _rx_files():
        assert not _banned(_imports(path)), f"{path} imports {_banned(_imports(path))}"


def test_ai_voice_chat_and_assistant_never_name_rx_tables():
    paths = sorted({p for pattern in AI_PATHS for p in pathlib.Path(".").glob(pattern)})
    assert paths, "no AI / voice / assistant files found: the guard would check nothing"
    for p in paths:
        hits = sorted(set(RX_TABLE.findall(p.read_text(encoding="utf-8"))))
        assert not hits, f"{p} names pharmacy tables {hits}; go through /admin/rx routes"


def test_rx_tables_are_known():
    assert "rx_stores" in RX_TABLES
    assert RX_TABLE.search("x = 'rx_stores'") and not RX_TABLE.search("rx_access(c) or rx_stores_x")


def test_guard_catches_a_banned_import(tmp_path):
    bad = tmp_path / "rx_bad.py"
    bad.write_text("from app.services.ai_engine import x\nimport app.assistant.registry\nimport openai\n",
                   encoding="utf-8")
    assert _banned(_imports(bad)) == ["app.assistant.registry", "app.services.ai_engine", "app.services.ai_engine.x",
                                      "openai"]
