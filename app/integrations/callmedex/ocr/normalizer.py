"""Lab Test Name Normalizer (Phase 5 Implementation)."""

import re
import logging
from typing import Tuple

logger = logging.getLogger(__name__)

# Dictionary mapping variant regex patterns to (Canonical Code, Display Name)
LAB_TEST_CANONICAL_MAPPING = [
    (r"(?i)\b(hb|hemoglobin|haemoglobin|hgb)\b", ("HB", "Hemoglobin")),
    (r"(?i)\b(wbc|total\s+leukocyte\s+count|wbc\s+count|white\s+blood\s+cell)\b", ("WBC", "White Blood Cell Count")),
    (r"(?i)\b(rbc|red\s+blood\s+cell\s+count|rbc\s+count)\b", ("RBC", "Red Blood Cell Count")),
    (r"(?i)\b(platelets|platelet\s+count|plt)\b", ("PLATELETS", "Platelet Count")),
    (r"(?i)\b(glucose|fasting\s+blood\s+sugar|fbs|random\s+blood\s+sugar|rbs)\b", ("GLUCOSE", "Glucose")),
    (r"(?i)\b(creatinine|serum\s+creatinine)\b", ("CREATININE", "Creatinine")),
    (r"(?i)\b(mantoux|mantoux\s+test)\b", ("MANTOUX", "Mantoux Test")),
    (r"(?i)\b(tsh|thyroid\s+stimulating\s+hormone)\b", ("TSH", "Thyroid Stimulating Hormone")),
    (r"(?i)\b(alt|sgpt|alanine\s+aminotransferase)\b", ("ALT", "ALT (SGPT)")),
    (r"(?i)\b(ast|sgot|aspartate\s+aminotransferase)\b", ("AST", "AST (SGOT)")),
]


def normalize_lab_test_name(raw_name: str) -> Tuple[str, str]:
    """Normalize raw laboratory test name to (Canonical Code, Canonical Display Name).

    Examples:
    - 'Hb' -> ('HB', 'Hemoglobin')
    - 'HEMOGLOBIN' -> ('HB', 'Hemoglobin')
    - 'Haemoglobin' -> ('HB', 'Hemoglobin')
    - 'Total Leukocyte Count' -> ('WBC', 'White Blood Cell Count')
    """
    clean_name = raw_name.strip()
    for pattern, (canonical_code, display_name) in LAB_TEST_CANONICAL_MAPPING:
        if re.search(pattern, clean_name):
            return canonical_code, display_name

    # Fallback to sanitized uppercase code if unmapped
    fallback_code = re.sub(r"[^A-Z0-9]", "_", clean_name.upper())[:15]
    return fallback_code, clean_name
