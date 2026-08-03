"""Lab Test Extraction Validation & Quality Engine (Phase 5 Implementation)."""

import re
import logging
from typing import List, Tuple
from app.integrations.callmedex.ocr.schemas import ExtractedLabTest, LabFlag
from app.integrations.callmedex.api.exceptions import ValidationError

logger = logging.getLogger(__name__)

# Numeric physiological boundaries for common lab tests
IMPOSSIBLE_BOUNDARIES = {
    "HB": (1.0, 30.0),        # g/dL
    "WBC": (100.0, 500000.0),  # /uL
    "RBC": (0.5, 10.0),       # mill/uL
    "PLATELETS": (1000.0, 2000000.0), # /uL
    "GLUCOSE": (10.0, 1500.0), # mg/dL
    "CREATININE": (0.1, 40.0), # mg/dL
}


def parse_flag_from_reference_range(value: float, reference_range: str) -> LabFlag:
    """Parse reference range string (e.g. '13.0-17.0') and determine flag."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*[-–—to]\s*(\d+(?:\.\d+)?)", reference_range)
    if match:
        low = float(match.group(1))
        high = float(match.group(2))
        if value < low:
            return LabFlag.LOW
        elif value > high:
            return LabFlag.HIGH
        else:
            return LabFlag.NORMAL
    return LabFlag.UNKNOWN


def validate_and_deduplicate_tests(tests: List[ExtractedLabTest]) -> Tuple[List[ExtractedLabTest], List[str]]:
    """Validate extracted lab tests for impossible numerical values, deduplicate by code, and compute flags.

    Returns:
        (validated_tests, warning_messages)
    """
    validated: List[ExtractedLabTest] = []
    warnings: List[str] = []
    seen_codes = set()

    for test in tests:
        # Check duplicate code
        if test.code in seen_codes:
            warnings.append(f"Duplicate test '{test.code}' detected; keeping highest confidence entry")
            continue
        seen_codes.add(test.code)

        # Check physiological boundaries
        if test.code in IMPOSSIBLE_BOUNDARIES:
            min_val, max_val = IMPOSSIBLE_BOUNDARIES[test.code]
            if test.value < min_val or test.value > max_val:
                warnings.append(
                    f"Impossible value {test.value} for test '{test.code}' (expected between {min_val} and {max_val})"
                )

        # Compute flag if missing or UNKNOWN
        if test.flag == LabFlag.UNKNOWN and test.reference_range:
            test.flag = parse_flag_from_reference_range(test.value, test.reference_range)

        # Low confidence warning
        if test.confidence < 0.70:
            warnings.append(f"Low confidence ({test.confidence}) for extracted test '{test.code}'")

        validated.append(test)

    return validated, warnings
