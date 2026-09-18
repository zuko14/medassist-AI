"""Catalogue Clean-Up Analyzer for Kriya AI.

Scans a clinic's diagnostic catalogue for:
1. Exact and acronym duplicate entries within the same branch.
   - Exact match after normalizing case, punctuation and spacing.
   - Acronym expansion equals other full name (CBC = Complete Blood Count).
   - Zero-similarity threshold (no difflib 0.88 rule).
   - Strict exclusions: numbers, views (AP/PA/LAT), antibody classes (IgG/IgM),
     laterality (left/right), single letter diffs, across branches.
   - Union-Find grouping with O(n) hash bucket lookup.
2. Suspicious prices (<= 0 or < Rs 10).
3. Missing clinical fasting preparation instructions for fasting investigations.
4. Missing service type categories.

All operations are non-blocking and safe for asyncio.to_thread execution.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

# Tests that clinically require fasting
FASTING_CLINICAL_TRIGGERS: List[Tuple[str, str]] = [
    ("fasting blood sugar", "8-10 hours overnight fasting required. Water is permitted."),
    ("fasting blood glucose", "8-10 hours overnight fasting required. Water is permitted."),
    ("fbs", "8-10 hours overnight fasting required. Water is permitted."),
    ("lipid profile", "10-12 hours overnight fasting required. Water is permitted."),
    ("fasting lipid", "10-12 hours overnight fasting required. Water is permitted."),
    ("fasting insulin", "8-10 hours fasting required. Water is permitted."),
    ("ultrasound whole abdomen", "6 hours fasting required prior to scan. Full bladder recommended."),
    ("usg whole abdomen", "6 hours fasting required prior to scan. Full bladder recommended."),
    ("usg abdomen", "6 hours fasting required prior to scan. Full bladder recommended."),
]

# Medical acronym expansions for exact equivalence matching
ACRONYM_MAP: Dict[str, str] = {
    "cbc": "complete blood count",
    "lft": "liver function test",
    "kft": "kidney function test",
    "rft": "renal function test",
    "tsh": "thyroid stimulating hormone",
    "fbs": "fasting blood sugar",
    "ppbs": "post prandial blood sugar",
    "usg": "ultrasound",
    "ecg": "electrocardiogram",
    "ekg": "electrocardiogram",
    "hba1c": "glycated hemoglobin",
    "esr": "erythrocyte sedimentation rate",
    "crp": "c reactive protein",
}


class UnionFind:
    """Disjoint Set Union (DSU) with path compression for exact duplicate grouping."""

    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, i: str) -> str:
        if i not in self.parent:
            self.parent[i] = i
            return i
        if self.parent[i] != i:
            self.parent[i] = self.find(self.parent[i])
        return self.parent[i]

    def union(self, i: str, j: str) -> None:
        root_i = self.find(i)
        root_j = self.find(j)
        if root_i != root_j:
            self.parent[root_i] = root_j


def _normalize_name(name: str) -> str:
    """Normalize test name by lowercase, stripping punctuation and redundant spacing."""
    n = (name or "").lower().strip()
    n = re.sub(r"[^\w\s]", " ", n)
    return " ".join(n.split())


def _has_exclusion_difference(name_a: str, name_b: str) -> bool:
    """Check whether two candidate names must NEVER be paired.

    Exclusions:
    1. Any number difference (e.g. Vitamin B12 vs B1, 2D vs 3D, T3 vs T4).
    2. Antibody class difference (IgG, IgM, IgA, IgE).
    3. View difference (AP, PA, LAT, OBL).
    4. Laterality difference (left, right, bilateral).
    5. Single letter difference on equal-length alphanumeric strings.
    """
    # 1. Any number difference
    nums_a = re.findall(r"\d+", name_a)
    nums_b = re.findall(r"\d+", name_b)
    if nums_a != nums_b:
        return True

    words_a = set(re.findall(r"\b[a-zA-Z0-9]+\b", name_a.lower()))
    words_b = set(re.findall(r"\b[a-zA-Z0-9]+\b", name_b.lower()))

    # 2. Antibody class difference
    antibodies = {"igg", "igm", "iga", "ige"}
    if (words_a & antibodies) != (words_b & antibodies):
        return True

    # 3. View difference
    views = {"ap", "pa", "lat", "lateral", "obl", "oblique", "axial"}
    if (words_a & views) != (words_b & views):
        return True

    # 4. Laterality difference
    laterality = {"left", "right", "bilateral", "lt", "rt"}
    if (words_a & laterality) != (words_b & laterality):
        return True

    # 5. Single letter difference on equal length words (e.g. HBsAg vs HBeAg)
    alpha_a = "".join(re.findall(r"[a-zA-Z0-9]", name_a.lower()))
    alpha_b = "".join(re.findall(r"[a-zA-Z0-9]", name_b.lower()))
    if len(alpha_a) == len(alpha_b) and len(alpha_a) > 2:
        diff_count = sum(1 for ca, cb in zip(alpha_a, alpha_b) if ca != cb)
        if diff_count == 1:
            return True

    return False


def analyze_catalogue_quality(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Perform deterministic O(n) rule-based analysis on a clinic's catalogue.

    Returns structured suggestions for duplicate removal, price fixes, and
    fasting/prep instructions.
    """
    duplicates: List[Dict[str, Any]] = []
    suspicious_prices: List[Dict[str, Any]] = []
    missing_prep: List[Dict[str, Any]] = []
    uncategorized: List[Dict[str, Any]] = []

    n_rows = len(rows)

    # 1. Price analysis
    for r in rows:
        t_id = str(r.get("id"))
        t_name = str(r.get("name") or "Unnamed Test")
        price_paise = r.get("price_paise") or 0

        if price_paise <= 0:
            suspicious_prices.append({
                "id": t_id,
                "name": t_name,
                "price_paise": price_paise,
                "price_rupees": price_paise / 100.0,
                "reason": "Price is zero or negative. Patient cannot book without a valid price.",
                "suggested_action": "Set a valid catalogue price",
            })
        elif price_paise < 1000:  # < Rs 10
            suspicious_prices.append({
                "id": t_id,
                "name": t_name,
                "price_paise": price_paise,
                "price_rupees": price_paise / 100.0,
                "reason": f"Unusually low price (Rs {price_paise/100:.2f}). Possible data entry typo.",
                "suggested_action": "Verify price in rupees",
            })

    # 2. Fasting & Prep Instructions analysis (checks prep_instructions)
    for r in rows:
        t_id = str(r.get("id"))
        t_name = str(r.get("name") or "")
        norm = _normalize_name(t_name)
        fasting_req = bool(r.get("fasting_required"))
        prep = (r.get("prep_instructions") or "").strip()

        for trigger, clinical_prep in FASTING_CLINICAL_TRIGGERS:
            if trigger in norm:
                if not fasting_req or not prep:
                    missing_prep.append({
                        "id": t_id,
                        "name": t_name,
                        "current_fasting_required": fasting_req,
                        "current_prep_instructions": prep,
                        "suggested_fasting_required": True,
                        "suggested_prep_instructions": clinical_prep,
                        "suggested_preparation": clinical_prep,  # UI backward compat
                        "reason": f"Matches clinical fasting trigger '{trigger}'. Needs fasting and preparation instructions for patient safety.",
                    })
                break

    # 3. Category analysis
    for r in rows:
        t_id = str(r.get("id"))
        t_name = str(r.get("name") or "")
        cat = (r.get("category") or "").strip()
        if not cat or cat.lower() in ("general", "uncategorized", "other"):
            uncategorized.append({
                "id": t_id,
                "name": t_name,
                "current_category": cat or "None",
                "suggested_action": "Assign to Pathology, Health Packages, Radiology, or Cardiology",
            })

    # 4. Duplicate Detection (O(n) with Union-Find grouping)
    # Rules:
    # - Same branch_id only (two branches never pair)
    # - Exact normalized match OR acronym expansion equals other full name
    # - Filtered by _has_exclusion_difference
    branch_buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    for r in rows:
        branch_key = str(r.get("branch_id") or "")
        name = str(r.get("name") or "")
        norm = _normalize_name(name)
        if not norm:
            continue

        # Acronym equivalence key
        canon_key = ACRONYM_MAP.get(norm, norm)
        bucket_key = (branch_key, canon_key)
        branch_buckets.setdefault(bucket_key, []).append(r)

    uf = UnionFind()
    for bucket in branch_buckets.values():
        if len(bucket) < 2:
            continue
        first = bucket[0]
        first_id = str(first.get("id"))
        for other in bucket[1:]:
            other_id = str(other.get("id"))
            if not _has_exclusion_difference(str(first.get("name") or ""), str(other.get("name") or "")):
                uf.union(first_id, other_id)

    # Collect grouped items
    row_map = {str(r.get("id")): r for r in rows}
    groups_by_root: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        r_id = str(r.get("id"))
        root = uf.find(r_id)
        groups_by_root.setdefault(root, []).append(r)

    for grp_rows in groups_by_root.values():
        if len(grp_rows) < 2:
            continue
        # Canonical selection: has prep/desc, price > 0, oldest
        grp_rows.sort(
            key=lambda r: (
                bool(r.get("prep_instructions")),
                bool(r.get("description")),
                (r.get("price_paise") or 0) > 0,
                str(r.get("created_at") or ""),
            ),
            reverse=True,
        )
        canonical = grp_rows[0]
        dupes = grp_rows[1:]

        duplicates.append({
            "canonical_id": str(canonical.get("id")),
            "canonical_name": str(canonical.get("name")),
            "canonical_price_rupees": (canonical.get("price_paise") or 0) / 100.0,
            "duplicate_ids": [str(d.get("id")) for d in dupes],
            "duplicate_names": [str(d.get("name")) for d in dupes],
            "duplicate_details": [
                {
                    "id": str(d.get("id")),
                    "name": str(d.get("name")),
                    "price_rupees": (d.get("price_paise") or 0) / 100.0,
                }
                for d in dupes
            ],
            "reason": "Redundant catalogue entries detected for the same investigation at the same branch.",
        })

    return {
        "summary": {
            "total_tests_analyzed": n_rows,
            "duplicate_groups_count": len(duplicates),
            "suspicious_prices_count": len(suspicious_prices),
            "missing_prep_count": len(missing_prep),
            "uncategorized_count": len(uncategorized),
            "total_suggestions_count": len(duplicates) + len(suspicious_prices) + len(missing_prep),
        },
        "duplicates": duplicates,
        "suspicious_prices": suspicious_prices,
        "missing_prep": missing_prep,
        "uncategorized": uncategorized,
    }
