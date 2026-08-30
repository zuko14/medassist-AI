"""Patient Match Service — Safety gate preventing diagnostic report misrouting.

This service verifies the scraped patient identity against the clinic's internal
`patients` records before automated WhatsApp delivery.

Safety Design:
- Diagnostic centers frequently serve walk-in patients who may not pre-exist in
  MediAssist's `patients` table. "No match" is valid (MocDoc is the source of record).
- "Conflicting match" (e.g. shared family phone with a different patient name)
  triggers NEEDS_REVIEW to prevent sending sensitive medical reports to the wrong individual.
- Missing or malformed phone numbers fail-closed into NEEDS_REVIEW.
"""

import difflib
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from app.database import supabase
from app.utils.validators import validate_phone, normalize_phone
from app.database import sb  # T5.1: off-loop query execution

logger = logging.getLogger(__name__)

# Common medical / honorific prefixes to strip for robust name matching
HONORIFIC_PREFIXES = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "master", "baby", "of",
    "smt", "sri", "shri", "kumari", "kum", "late", "b/o", "c/o", "d/o", "s/o", "w/o"
}


def normalize_name(name: str) -> str:
    """Normalize patient name by removing honorifics, punctuation, and extra whitespace."""
    if not name:
        return ""
    # Remove punctuation
    cleaned = re.sub(r"[^\w\s]", " ", name.lower())
    words = [w.strip() for w in cleaned.split() if w.strip()]
    filtered = [w for w in words if w not in HONORIFIC_PREFIXES]
    return " ".join(filtered) if filtered else " ".join(words)


def compute_name_similarity(name1: str, name2: str) -> float:
    """Calculate similarity score between two patient names in [0.0, 1.0]."""
    n1 = normalize_name(name1)
    n2 = normalize_name(name2)
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0

    # Sequence matcher ratio
    seq_ratio = difflib.SequenceMatcher(None, n1, n2).ratio()

    # Token sort / set ratio for reordered names (e.g. "Varalakshmi C" vs "C Varalakshmi")
    words1 = sorted(n1.split())
    words2 = sorted(n2.split())
    sorted_ratio = difflib.SequenceMatcher(None, " ".join(words1), " ".join(words2)).ratio()

    # Substring / subset check
    set1, set2 = set(words1), set(words2)
    intersection = set1.intersection(set2)
    jaccard = len(intersection) / max(len(set1.union(set2)), 1)

    return max(seq_ratio, sorted_ratio, jaccard)


@dataclass
class MatchResult:
    status: str  # "matched" | "needs_review"
    is_safe_to_send: bool
    match_source: str  # "patients_table" | "moc_doc_only" | "unnamed_record" | "conflict" | "missing_phone" | "manual"
    match_confidence: float
    matched_patient_id: Optional[str] = None
    normalized_phone: Optional[str] = None
    patient_name: str = ""
    review_reason: Optional[str] = None
    existing_records: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "is_safe_to_send": self.is_safe_to_send,
            "match_source": self.match_source,
            "match_confidence": round(self.match_confidence, 2),
            "matched_patient_id": self.matched_patient_id,
            "normalized_phone": self.normalized_phone,
            "patient_name": self.patient_name,
            "review_reason": self.review_reason,
            "existing_records_count": len(self.existing_records),
        }


class PatientMatchService:
    """Evaluates scraped report identity against clinic patient records."""

    def __init__(self, similarity_threshold: float = 0.75):
        self.similarity_threshold = similarity_threshold

    async def match(
        self,
        clinic_id: str,
        scraped_name: str,
        scraped_phone: Optional[str],
        branch_id: Optional[str] = None,
    ) -> MatchResult:
        """Perform patient verification gate.

        Returns MatchResult with safety decision.
        """
        # Step 1: Validate phone
        if not scraped_phone:
            return MatchResult(
                status="needs_review",
                is_safe_to_send=False,
                match_source="missing_phone",
                match_confidence=0.0,
                patient_name=scraped_name or "",
                review_reason="No phone number provided in report metadata",
            )

        norm_phone = normalize_phone(scraped_phone)
        if not validate_phone(norm_phone):
            return MatchResult(
                status="needs_review",
                is_safe_to_send=False,
                match_source="missing_phone",
                match_confidence=0.0,
                patient_name=scraped_name or "",
                review_reason=f"Invalid phone number format: {scraped_phone}",
            )

        # Step 2: Query clinic patients by phone
        try:
            query = (
                supabase.table("patients")
                .select("id, name, phone, clinic_id")
                .eq("clinic_id", clinic_id)
                .eq("phone", norm_phone)
            )
            res = await sb(query)
            records = res.data if isinstance(res.data, list) else []
        except Exception as e:
            logger.error(f"Failed to query patients for match (failing closed): {e}")
            return MatchResult(
                status="needs_review",
                is_safe_to_send=False,
                match_source="database_error",
                match_confidence=0.0,
                matched_patient_id=None,
                normalized_phone=norm_phone,
                patient_name=scraped_name or "",
                review_reason=f"Database query error during patient lookup: {e}",
                existing_records=[],
            )

        # Step 3: Evaluate matching rules
        if not records:
            # No existing patient in MediAssist DB -> Standard diagnostic center walk-in
            return MatchResult(
                status="matched",
                is_safe_to_send=True,
                match_source="moc_doc_only",
                match_confidence=1.0,
                matched_patient_id=None,
                normalized_phone=norm_phone,
                patient_name=scraped_name,
                review_reason=None,
                existing_records=[],
            )

        # A record whose name is NULL/blank (created by the bot before the
        # patient ever gave a name) carries no identity to compare against.
        # Scoring it yields 0.00 and blocks the report forever — while a phone
        # with NO record at all sends on confidence 1.0 above. Treat a nameless
        # record as what it is: less information than no record, not more risk.
        named_records = [r for r in records if normalize_name(r.get("name") or "")]

        if not named_records:
            return MatchResult(
                status="matched",
                is_safe_to_send=True,
                match_source="unnamed_record",
                match_confidence=1.0,
                matched_patient_id=records[0].get("id") if len(records) == 1 else None,
                normalized_phone=norm_phone,
                patient_name=scraped_name,
                review_reason=None,
                existing_records=records,
            )

        records = named_records

        if len(records) == 1:
            db_patient = records[0]
            db_name = db_patient.get("name") or ""
            score = compute_name_similarity(scraped_name, db_name)

            if score >= self.similarity_threshold:
                return MatchResult(
                    status="matched",
                    is_safe_to_send=True,
                    match_source="patients_table",
                    match_confidence=score,
                    matched_patient_id=db_patient.get("id"),
                    normalized_phone=norm_phone,
                    patient_name=scraped_name,
                    review_reason=None,
                    existing_records=records,
                )
            else:
                # Name mismatch on single phone record -> Conflict
                return MatchResult(
                    status="needs_review",
                    is_safe_to_send=False,
                    match_source="conflict",
                    match_confidence=score,
                    matched_patient_id=db_patient.get("id"),
                    normalized_phone=norm_phone,
                    patient_name=scraped_name,
                    review_reason=(
                        f"Name conflict on {norm_phone}: scraped name '{scraped_name}' "
                        f"does not match existing patient '{db_name}' (similarity {score:.2f} < {self.similarity_threshold})"
                    ),
                    existing_records=records,
                )

        # Multiple patient records for the same phone number
        # Check if any record matches strongly
        best_score = 0.0
        best_match = None
        for rec in records:
            s = compute_name_similarity(scraped_name, rec.get("name") or "")
            if s > best_score:
                best_score = s
                best_match = rec

        if best_score >= self.similarity_threshold and best_match:
            return MatchResult(
                status="matched",
                is_safe_to_send=True,
                match_source="patients_table",
                match_confidence=best_score,
                matched_patient_id=best_match.get("id"),
                normalized_phone=norm_phone,
                patient_name=scraped_name,
                review_reason=None,
                existing_records=records,
            )
        else:
            names_summary = ", ".join(f"'{r.get('name')}'" for r in records[:3])
            return MatchResult(
                status="needs_review",
                is_safe_to_send=False,
                match_source="conflict",
                match_confidence=best_score,
                matched_patient_id=None,
                normalized_phone=norm_phone,
                patient_name=scraped_name,
                review_reason=(
                    f"Ambiguous match on {norm_phone}: scraped name '{scraped_name}' "
                    f"does not match any of {len(records)} existing patients ({names_summary})"
                ),
                existing_records=records,
            )


patient_match_service = PatientMatchService()
