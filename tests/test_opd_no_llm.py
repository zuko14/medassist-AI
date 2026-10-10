"""AST Guard for OPD Zero-LLM Clinical Safety (Phase 1.3).

Verifies:
1. opd_clinical.py, opd_pdf.py, opd.py, opd_billing.py (if present), routers/opd.py
   import nothing from ai_engine, ai_gateway, faq_engine, hybrid_search, report_summarizer, app.voice.
2. No module under app/services/ai_*, app/voice/, or conversation.py contains the strings
   'opd_encounters' or 'opd_prescription'.
"""

import ast
import os
import pathlib
import pytest

BANNED_IMPORTS = {
    "ai_engine",
    "ai_gateway",
    "faq_engine",
    "hybrid_search",
    "report_summarizer",
    "app.voice",
    "voice",
}

OPD_SOURCE_FILES = [
    "app/services/opd.py",
    "app/services/opd_clinical.py",
    "app/services/opd_pdf.py",
    "app/routers/opd.py",
]
if os.path.exists("app/services/opd_billing.py"):
    OPD_SOURCE_FILES.append("app/services/opd_billing.py")


def _get_imported_names(tree: ast.AST) -> set[str]:
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    return imported


def test_opd_modules_import_no_ai_or_voice():
    """Verify OPD service and router files do not import any AI or voice modules."""
    for rel_path in OPD_SOURCE_FILES:
        path = pathlib.Path(rel_path)
        assert path.exists(), f"Source file {rel_path} must exist"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules = _get_imported_names(tree)

        violations = []
        for mod in imported_modules:
            for banned in BANNED_IMPORTS:
                if mod == banned or mod.startswith(banned + "."):
                    violations.append(mod)

        assert not violations, f"{rel_path} has prohibited AI/Voice imports: {violations}"


def test_no_opd_leakage_into_ai_or_voice_modules():
    """Verify AI, voice, and conversation modules do not reference clinical OPD tables."""
    forbidden_strings = ["opd_encounters", "opd_prescription"]

    paths_to_check = []
    services_dir = pathlib.Path("app/services")
    for f in services_dir.glob("ai_*.py"):
        paths_to_check.append(f)
    if (services_dir / "conversation.py").exists():
        paths_to_check.append(services_dir / "conversation.py")

    voice_dir = pathlib.Path("app/voice")
    if voice_dir.exists():
        for f in voice_dir.rglob("*.py"):
            paths_to_check.append(f)

    for p in paths_to_check:
        content = p.read_text(encoding="utf-8")
        for bad_str in forbidden_strings:
            assert bad_str not in content, f"{p} unexpectedly contains '{bad_str}'"
