"""Unit tests for catalogue quality clean-up analyzer (Feature 6)."""

import time
import uuid
import pytest
from app.services.catalogue_cleaner import analyze_catalogue_quality

SAMPLE_CATALOGUE = [
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "Complete Blood Count",
        "price_paise": 35000,
        "fasting_required": False,
        "prep_instructions": None,
        "category": "Pathology",
        "description": "Measures red, white cells and platelets.",
    },
    {
        "id": "22222222-2222-2222-2222-222222222222",
        "name": "CBC",
        "price_paise": 35000,
        "fasting_required": False,
        "prep_instructions": None,
        "category": "General",
        "description": None,
    },
    {
        "id": "33333333-3333-3333-3333-333333333333",
        "name": "Fasting Blood Sugar",
        "price_paise": 15000,
        "fasting_required": False,  # Missing fasting!
        "prep_instructions": None,
        "category": "Pathology",
    },
    {
        "id": "44444444-4444-4444-4444-444444444444",
        "name": "Lipid Profile",
        "price_paise": 0,  # Zero price!
        "fasting_required": True,
        "prep_instructions": "10-12 hours fasting required",
        "category": "Pathology",
    },
    {
        "id": "55555555-5555-5555-5555-555555555555",
        "name": "Random Blood Sugar",
        "price_paise": 500,  # Suspiciously low Rs 5!
        "fasting_required": False,
        "prep_instructions": None,
        "category": "General",
    },
]


class TestCatalogueCleaner:
    def test_detects_acronym_duplicates(self):
        analysis = analyze_catalogue_quality(SAMPLE_CATALOGUE)
        dupes = analysis["duplicates"]
        assert len(dupes) == 1
        d_group = dupes[0]
        assert d_group["canonical_name"] == "Complete Blood Count"
        assert "22222222-2222-2222-2222-222222222222" in d_group["duplicate_ids"]

    def test_detects_zero_and_suspicious_prices(self):
        analysis = analyze_catalogue_quality(SAMPLE_CATALOGUE)
        prices = analysis["suspicious_prices"]
        price_ids = [p["id"] for p in prices]
        assert "44444444-4444-4444-4444-444444444444" in price_ids  # Rs 0
        assert "55555555-5555-5555-5555-555555555555" in price_ids  # Rs 5

    def test_detects_missing_fasting_prep(self):
        analysis = analyze_catalogue_quality(SAMPLE_CATALOGUE)
        missing = analysis["missing_prep"]
        missing_ids = [m["id"] for m in missing]
        assert "33333333-3333-3333-3333-333333333333" in missing_ids
        fbs_item = next(m for m in missing if m["id"] == "33333333-3333-3333-3333-333333333333")
        assert fbs_item["suggested_fasting_required"] is True
        assert "fasting" in fbs_item["suggested_prep_instructions"].lower()

    def test_detects_uncategorized_tests(self):
        analysis = analyze_catalogue_quality(SAMPLE_CATALOGUE)
        uncat = analysis["uncategorized"]
        uncat_ids = [u["id"] for u in uncat]
        assert "22222222-2222-2222-2222-222222222222" in uncat_ids
        assert "55555555-5555-5555-5555-555555555555" in uncat_ids

    def test_strict_exclusions_must_not_be_flagged(self):
        """None of these clinical pairs should EVER be flagged as duplicates:
        - IgG vs IgM
        - Vitamin B12 vs B1
        - Chest X-ray AP vs PA
        - CBC vs 'CBC with ESR'
        - same name at two different branches
        """
        b1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        b2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        tests = [
            {"id": str(uuid.uuid4()), "name": "Dengue IgG Antibody", "price_paise": 50000, "branch_id": b1},
            {"id": str(uuid.uuid4()), "name": "Dengue IgM Antibody", "price_paise": 50000, "branch_id": b1},
            {"id": str(uuid.uuid4()), "name": "Vitamin B12", "price_paise": 80000, "branch_id": b1},
            {"id": str(uuid.uuid4()), "name": "Vitamin B1", "price_paise": 80000, "branch_id": b1},
            {"id": str(uuid.uuid4()), "name": "Chest X-ray AP", "price_paise": 40000, "branch_id": b1},
            {"id": str(uuid.uuid4()), "name": "Chest X-ray PA", "price_paise": 40000, "branch_id": b1},
            {"id": str(uuid.uuid4()), "name": "CBC", "price_paise": 30000, "branch_id": b1},
            {"id": str(uuid.uuid4()), "name": "CBC with ESR", "price_paise": 45000, "branch_id": b1},
            {"id": str(uuid.uuid4()), "name": "Thyroid Profile", "price_paise": 60000, "branch_id": b1},
            {"id": str(uuid.uuid4()), "name": "Thyroid Profile", "price_paise": 60000, "branch_id": b2},
        ]
        res = analyze_catalogue_quality(tests)
        assert res["duplicates"] == [], f"Expected 0 duplicates, got: {res['duplicates']}"

    def test_benchmark_1500_rows_finish_under_2_seconds(self):
        """1,500 catalogue rows must analyze in < 2 seconds."""
        rows = []
        for i in range(1500):
            rows.append({
                "id": str(uuid.uuid4()),
                "name": f"Clinical Investigation Test #{i}",
                "price_paise": 20000 + (i * 10),
                "fasting_required": False,
                "prep_instructions": None,
                "category": "Pathology" if i % 2 == 0 else "General",
                "branch_id": None,
            })
        # Add 1 deliberate exact duplicate
        rows.append({
            "id": str(uuid.uuid4()),
            "name": "Clinical Investigation Test #0",
            "price_paise": 20000,
            "fasting_required": False,
            "prep_instructions": None,
            "category": "Pathology",
            "branch_id": None,
        })

        t0 = time.perf_counter()
        analysis = analyze_catalogue_quality(rows)
        elapsed = time.perf_counter() - t0

        print(f"\n1500 rows analyzed in {elapsed:.4f}s")
        assert elapsed < 2.0, f"Analysis took {elapsed:.4f}s, expected < 2.0s"
        assert len(analysis["duplicates"]) == 1
