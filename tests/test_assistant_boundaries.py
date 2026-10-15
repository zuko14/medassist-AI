"""Kriya Ketu is not the patient bot (ADR-001, architecture.md §6): AST import checks both ways.

- app/assistant/ may reuse only pure voice helpers (dates, lexicon, nlu_rules) and
  the deterministic clinical firewall; never the patient conversation or call
  dialog/session code.
- Nothing outside the assistant's own router and app.main may import app.assistant.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSISTANT = ROOT / "app" / "assistant"
FORBIDDEN_FOR_ASSISTANT = ("app.services.conversation", "app.voice.dialog", "app.voice.session",
                           "app.voice.gateway", "app.voice.tools", "app.routers.webhook")
ALLOWED_IMPORTERS = {ROOT / "app" / "routers" / "assistant.py", ROOT / "app" / "main.py"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def test_assistant_never_imports_the_patient_channels():
    for path in ASSISTANT.glob("*.py"):
        bad = {m for m in _imports(path) if m.startswith(FORBIDDEN_FOR_ASSISTANT)}
        assert not bad, f"{path.name} imports {bad}"


def test_patient_channels_never_import_the_assistant():
    for path in (ROOT / "app").rglob("*.py"):
        if ASSISTANT in path.parents or path in ALLOWED_IMPORTERS:
            continue
        bad = {m for m in _imports(path) if m == "app.assistant" or m.startswith("app.assistant.")}
        assert not bad, f"{path.relative_to(ROOT)} imports {bad}"
