"""Continuous Care zero-LLM guard (ADR-CC-02/03; docs/continuous-care/TEST-PLAN.md CG3).

1. Every CC module (app/services/cc*.py, app/services/cc_protocols/*.py, app/routers/cc.py) and the CC-L1 crisis
   detector import nothing from the AI, voice-NLU or assistant packages — walking every import, including the lazy
   ones inside functions.
2. AI, voice, patient-chat and Kriya Ketu code never names a cc_* table: they reach care data only through
   cc_hooks / cc_voice (zero-LLM services) or the /admin/cc routes.
"""

import pathlib
import re

from app.tenancy import TENANT_OWNED_TABLES
from tests.test_rx_no_llm import _banned, _imports

AI_PATHS = ("app/services/ai_*.py", "app/services/conversation.py", "app/voice/**/*.py", "app/assistant/**/*.py")
CC_TABLES = sorted(t for t in TENANT_OWNED_TABLES if t.startswith("cc_"))
CC_TABLE = re.compile(r"\b(" + "|".join(CC_TABLES) + r")\b")


def _cc_files() -> list[pathlib.Path]:
    return sorted([*pathlib.Path("app/services").glob("cc*.py"), *pathlib.Path("app/services/cc_protocols").glob("*.py"),
                   pathlib.Path("app/routers/cc.py"), pathlib.Path("app/services/crisis.py")])


def test_there_are_cc_modules_to_check():
    names = {p.as_posix() for p in _cc_files()}
    assert {"app/services/cc.py", "app/services/cc_hooks.py", "app/services/cc_voice.py", "app/routers/cc.py",
            "app/services/cc_protocols/base.py", "app/services/cc_protocols/t2dm.py"} <= names, names
    assert len([n for n in names if n.startswith("app/services/cc_protocols/")]) >= 13


#: The one voice module CC may use: the outbound job queue (enqueue a care_checkin call). It is plain scheduling code;
#: the guard below proves it imports no model, NLU or assistant code itself.
VOICE_QUEUE = {"app.voice", "app.voice.outbound"}


def test_cc_modules_import_no_ai_voice_or_assistant():
    for path in _cc_files():
        bad = [m for m in _banned(_imports(path)) if m not in VOICE_QUEUE]
        assert not bad, f"{path} imports {bad}"


def test_the_voice_queue_cc_uses_imports_no_model_or_nlu():
    mods = _imports(pathlib.Path("app/voice/outbound.py"))
    assert not [m for m in _banned(mods) if not m.startswith("app.voice")]
    assert not [m for m in mods if re.search(r"nlu|dialog|llm|ai_engine|asr|tts", m)], mods


def test_protocol_packs_import_only_the_engine_and_stdlib():
    for path in pathlib.Path("app/services/cc_protocols").glob("*.py"):
        local = {q.stem for q in path.parent.glob("*.py")}
        mods = {m for m in _imports(path) if m.split(".")[0] not in local and not m.startswith("app.services.cc_protocols")}
        assert mods <= {"math", "datetime", "datetime.timedelta", "datetime.date", "hashlib", "json", "dataclasses",
                        "dataclasses.asdict", "dataclasses.dataclass", "dataclasses.field", "typing", "types",
                        "types.SimpleNamespace", "typing.Callable", "typing.Iterable", "typing.Literal",
                        "typing.Mapping", "typing.Optional", "math"} | {m for m in mods if m.startswith("typing.")}, \
            (path, mods)


def test_ai_voice_chat_and_assistant_never_name_cc_tables():
    assert len(CC_TABLES) == 18
    paths = sorted({p for pattern in AI_PATHS for p in pathlib.Path(".").glob(pattern)})
    assert paths
    for p in paths:
        hits = sorted(set(CC_TABLE.findall(p.read_text(encoding="utf-8"))))
        assert not hits, f"{p} names care tables {hits}; go through cc_hooks / cc_voice / /admin/cc"


def test_guard_catches_a_lazy_banned_import(tmp_path):
    bad = tmp_path / "cc_bad.py"
    bad.write_text("def f():\n    from app.services.ai_engine import detect_intent\n    import app.voice.nlu\n",
                   encoding="utf-8")
    assert _banned(_imports(bad)) == ["app.services.ai_engine", "app.services.ai_engine.detect_intent", "app.voice.nlu"]
