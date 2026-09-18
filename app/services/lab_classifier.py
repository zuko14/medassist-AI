"""Suggest a service type for an unfiled diagnostic test, from its name.

Deterministic keyword rules, not an LLM: a centre files 1,392 tests with one
click, the same name always lands in the same heading, and nothing is sent to
a third party. The admin sees the proposal before anything is saved, and only
UNFILED tests are ever touched -- a heading a person chose is never replaced.

The labels are the panel's own starter headings (LAB_CATEGORY_SUGGESTIONS in
admin/index.html), so auto-filed and hand-filed tests share one menu.
"""

import re

PATHOLOGY = "Lab Tests (Pathology)"
PACKAGES = "Health Packages"
RADIOLOGY = "Radiology & Imaging"
SCANS = "Scans (CT / MRI)"
CARDIAC = "Cardiac & Special Tests"

#: First match wins, so the order is part of the rules: "PET-CT Whole Body"
#: is a scan, not a package, and "USG Guided FNAC" is imaging.
_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (SCANS, re.compile(
        r"\b(?:mri|mra|mrcp|ct|cect|ncct|hrct|pet|cbct)\b|ct[\s-]*scan|cone\s*beam|angiogra",
        re.IGNORECASE)),
    (PACKAGES, re.compile(
        r"packages?\b|\bpkg\b|check[\s-]*up|health\s*check|wellness|full\s*body|master\s*health"
        r"|executive\s*health|screening\s*(?:package|profile)",
        re.IGNORECASE)),
    (RADIOLOGY, re.compile(
        r"x[\s-]*ray|\bxr\b|ultrasound|ultrasonogra|\busg\b|sonogra|doppler|mammogra|\bdexa\b"
        r"|\bbmd\b|\bopg\b|\bhsg\b|fluoroscop|barium|\bivp\b|bone\s*densit"
        # Plain-film X-rays are often listed by their view alone:
        # "HAND WITH WRIST LAT VIEW", "LEG WITH ANKLE AP/LAT VIEW".
        r"|\bviews?\b|\b(?:ap|pa)\s*/\s*lat\b",
        re.IGNORECASE)),
    (CARDIAC, re.compile(
        r"\b(?:ecg|ekg|echo|2d\s*echo|tmt|eeg|emg|ncv|pft)\b|echocardio|treadmill|holter"
        r"|spirometr|audiometr|endoscop|colonoscop|\babpm\b",
        re.IGNORECASE)),
)


def suggest_service_type(name: str) -> str:
    """The heading a test most likely belongs under. Pathology by default:
    a diagnostic catalogue is overwhelmingly blood and urine tests."""
    text = name or ""
    for label, pattern in _RULES:
        if pattern.search(text):
            return label
    return PATHOLOGY


def classify_unfiled(tests: list[dict]) -> dict[str, list[dict]]:
    """Proposed heading -> the unfiled tests that would go under it.

    Filed tests (any non-blank category) are skipped entirely.
    """
    proposal: dict[str, list[dict]] = {}
    for t in tests:
        if (t.get("category") or "").strip():
            continue
        proposal.setdefault(suggest_service_type(t.get("name") or ""), []).append(t)
    return proposal


if __name__ == "__main__":
    cases = {
        "MRI BRAIN (PLAIN)": SCANS, "CT CHEST": SCANS, "PET-CT WHOLE BODY": SCANS,
        "HRCT THORAX": SCANS, "Master Health Checkup": PACKAGES, "Full Body Package": PACKAGES,
        "CHEST X-RAY PA VIEW": RADIOLOGY, "USG WHOLE ABDOMEN": RADIOLOGY, "Mammography": RADIOLOGY,
        "ECG": CARDIAC, "2D ECHO": CARDIAC, "TMT": CARDIAC, "COMPLETE BLOOD COUNT": PATHOLOGY,
        "THYROID PROFILE (T3 T4 TSH)": PATHOLOGY, "PROTEIN, TOTAL": PATHOLOGY,
        "ECHINOCOCCUS ANTIBODY": PATHOLOGY, "CHIKUNGUNYA IGM": PATHOLOGY, "PETECHIAE": PATHOLOGY,
        "hand AP/LAT view": RADIOLOGY, "ULTRASONOGRAM - SCROTUM": RADIOLOGY,
        "BMD - SPINE AND HIP": RADIOLOGY, "PERIPHERAL SMEAR STUDY": PATHOLOGY,
        "HEPATITIS B PROFILE": PATHOLOGY, "COAGULATION SCREEN": PATHOLOGY,
    }
    for n, want in cases.items():
        assert suggest_service_type(n) == want, (n, suggest_service_type(n))
    print("ok")
