"""Diagnostic OS zero-LLM guard (docs/diagnostic-os/ADR.md ADR-DX-10).

1. Every DX service/router module (app/services/dx*.py, app/routers/dx*.py)
   imports nothing from the AI, voice or assistant packages: reference ranges,
   flags, formulas and sign-off never depend on a model.
2. AI, voice, patient-chat and Kriya Ketu code never names a dx_* table: they
   reach DX only through the HTTP routes (same permission, entitlement, audit).
"""

import ast
import pathlib
import re

BANNED = ("app.services.ai_", "app.services.faq_engine", "app.services.hybrid_search",
          "app.services.report_summarizer", "app.voice", "app.assistant",
          "openai", "groq", "anthropic", "google.generativeai")
AI_PATHS = ("app/services/ai_*.py", "app/services/conversation.py", "app/voice/**/*.py", "app/assistant/**/*.py")
DX_TABLE = re.compile(r"\bdx_[a-z][a-z_]*\b")


def _dx_files() -> list[pathlib.Path]:
    return sorted([*pathlib.Path("app/services").glob("dx*.py"), *pathlib.Path("app/routers").glob("dx*.py")])


def _imports(path: pathlib.Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def test_there_are_dx_modules_to_check():
    names = {p.as_posix() for p in _dx_files()}
    assert {"app/services/dx.py", "app/routers/dx.py"} <= names, names


def test_dx_modules_import_no_ai_voice_or_assistant():
    for path in _dx_files():
        bad = [m for m in _imports(path)
               if any(m.startswith(b) if b.endswith("_") else (m == b or m.startswith(b + ".")) for b in BANNED)]
        assert not bad, f"{path} imports {bad}"


def test_ai_voice_chat_and_assistant_never_name_dx_tables():
    paths = sorted({p for pattern in AI_PATHS for p in pathlib.Path(".").glob(pattern)})
    assert paths, "no AI / voice / assistant files found: the guard would check nothing"
    for p in paths:
        hits = sorted(set(DX_TABLE.findall(p.read_text(encoding="utf-8"))))
        assert not hits, f"{p} names DX tables {hits}; go through /admin/dx routes"


def test_guard_catches_a_banned_import(tmp_path):
    bad = tmp_path / "dx_bad.py"
    bad.write_text("from app.services.ai_engine import x\nimport app.voice.tools\n", encoding="utf-8")
    assert {"app.services.ai_engine", "app.voice.tools"} <= _imports(bad)
