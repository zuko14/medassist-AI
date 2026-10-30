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
IPD_FILES = ("app/services/ipd.py", "app/routers/ipd.py", "app/services/ipd_finance.py",
             # G1 orders (migration 115): diagnostic orders and worklist are deterministic too
             "app/services/orders.py", "app/routers/orders.py",
             # G2 LIS (migration 118): range selection, flagging and sign-off are deterministic
             "app/services/lis.py", "app/routers/lis.py",
             # G9 clinical documents (migration 119): rendered from stored rows only
             "app/services/clinical_documents.py", "app/routers/documents.py",
             # G12 masters (migration 120): ICD-10 search is deterministic SQL
             "app/services/masters.py", "app/routers/masters.py",
             # G7 emergency (migration 121): triage, MLC, disposition, merge
             "app/services/emergency.py", "app/routers/emergency.py",
             # G8 operation theatre (migration 122): scheduling, WHO checklist, anaesthesia, completion
             "app/services/ot.py", "app/routers/ot.py",
             # G13 radiology (migration 123): modality / obstetric rules, PCPNDT firewall, sign-off
             "app/services/radiology.py", "app/routers/radiology.py",
             # G6 insurance / TPA (migration 124): split, settlement and ageing are deterministic money
             "app/services/insurance.py", "app/routers/insurance.py")
CLINICAL_TABLES = ("ipd_observations", "ipd_notes", "ipd_orders", "ipd_med_administrations",
                   "ipd_discharge_summaries", "ipd_admissions", "opd_orders", "lab_results", "lab_result_values",
                   "lab_test_parameters", "clinical_consents", "clinical_certificates",
                   "emergency_visits", "emergency_mlc_records",
                   "ot_surgeries", "ot_who_checklists", "ot_anaesthesia_records",
                   "radiology_reports", "pcpndt_form_f_records",
                   "ipd_pre_authorizations", "ipd_insurance_claims", "ipd_claim_transactions")


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
