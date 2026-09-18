"""Tests for deterministic multilingual & synonym catalogue search (Feature 5)."""

import pytest
from app.services.hybrid_search import multilingual_synonym_search, get_synonym_expansions
from app.services.conversation import ConversationManager

TEST_CATALOGUE = [
    {"id": "1", "name": "Complete Blood Count (CBC)", "price_paise": 35000, "category": "Pathology"},
    {"id": "2", "name": "Fasting Blood Glucose", "price_paise": 15000, "category": "Pathology"},
    {"id": "3", "name": "HbA1c (Glycated Hemoglobin)", "price_paise": 50000, "category": "Pathology"},
    {"id": "4", "name": "Thyroid Profile Total (T3, T4, TSH)", "price_paise": 65000, "category": "Pathology"},
    {"id": "5", "name": "Lipid Profile", "price_paise": 75000, "category": "Pathology"},
    {"id": "6", "name": "Kidney Function Test (KFT)", "price_paise": 80000, "category": "Pathology"},
    {"id": "7", "name": "Serum Creatinine", "price_paise": 20000, "category": "Pathology"},
    {"id": "8", "name": "Liver Function Test (LFT)", "price_paise": 85000, "category": "Pathology"},
    {"id": "9", "name": "Urine Routine & Microscopy", "price_paise": 18000, "category": "Pathology"},
    {"id": "10", "name": "Dengue NS1 Antigen", "price_paise": 60000, "category": "Pathology"},
]


class TestMultilingualSearch:
    def test_hindi_thyroid_search(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "थायराइड")
        assert len(results) >= 1
        assert any("Thyroid" in r["name"] for r in results)

    def test_hindi_sugar_search(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "शुगर")
        assert len(results) >= 1
        matched_names = [r["name"] for r in results]
        assert "Fasting Blood Glucose" in matched_names or "HbA1c (Glycated Hemoglobin)" in matched_names

    def test_telugu_sugar_search(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "షుగర్")
        assert len(results) >= 1
        matched_names = [r["name"] for r in results]
        assert "Fasting Blood Glucose" in matched_names or "HbA1c (Glycated Hemoglobin)" in matched_names

    def test_telugu_thyroid_search(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "థైరాయిడ్")
        assert len(results) >= 1
        assert any("Thyroid" in r["name"] for r in results)

    def test_english_synonym_sugar_finds_glucose_and_hba1c(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "sugar")
        matched_names = [r["name"] for r in results]
        assert "Fasting Blood Glucose" in matched_names
        assert "HbA1c (Glycated Hemoglobin)" in matched_names

    def test_english_synonym_cholesterol_finds_lipid(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "cholesterol")
        matched_names = [r["name"] for r in results]
        assert "Lipid Profile" in matched_names

    def test_english_synonym_creatinine_finds_creatinine(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "creatinine")
        matched_names = [r["name"] for r in results]
        assert "Serum Creatinine" in matched_names

    def test_english_synonym_kft_finds_kidney_function_test(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "kft")
        matched_names = [r["name"] for r in results]
        assert "Kidney Function Test (KFT)" in matched_names

    def test_typo_tolerance_thryoid(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "thryoid")
        assert len(results) >= 1
        assert any("Thyroid" in r["name"] for r in results)

    def test_typo_tolerance_creatanine(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "creatanine")
        assert len(results) >= 1
        assert any("Creatinine" in r["name"] for r in results)

    def test_garbage_query_returns_empty(self):
        results = multilingual_synonym_search(TEST_CATALOGUE, "xyz123randomgarbage")
        assert results == []

    def test_conversation_manager_tier1_takes_precedence(self):
        # Tier 1 exact match works for "Urine"
        res = ConversationManager._match_lab_tests(TEST_CATALOGUE, "urine")
        assert len(res) == 1
        assert res[0]["name"] == "Urine Routine & Microscopy"

    def test_conversation_manager_falls_back_to_tier2_for_hindi(self):
        res = ConversationManager._match_lab_tests(TEST_CATALOGUE, "थायराइड")
        assert len(res) >= 1
        assert any("Thyroid" in r["name"] for r in res)

    def test_conversation_manager_falls_back_to_tier3_for_typo(self):
        res = ConversationManager._match_lab_tests(TEST_CATALOGUE, "thryoid")
        assert len(res) >= 1
        assert any("Thyroid" in r["name"] for r in res)

    def test_required_negative_cases(self):
        # Tests that must return nothing:
        # 1. मलेरिया vs [LYMPHOCYTE COUNT, COMPLETE BLOOD COUNT]
        cat1 = [
            {"id": "1", "name": "LYMPHOCYTE COUNT"},
            {"id": "2", "name": "COMPLETE BLOOD COUNT"},
        ]
        assert multilingual_synonym_search(cat1, "मलेरिया") == []

        # 2. calcium vs [CT SCAN BRAIN, 2D ECHO CARDIAC]
        cat2 = [
            {"id": "1", "name": "CT SCAN BRAIN"},
            {"id": "2", "name": "2D ECHO CARDIAC"},
        ]
        assert multilingual_synonym_search(cat2, "calcium") == []

        # 3. lft vs [GASTRIN, SERUM SODIUM SALT, FASTING BLOOD SUGAR]
        cat3 = [
            {"id": "1", "name": "GASTRIN"},
            {"id": "2", "name": "SERUM SODIUM SALT"},
            {"id": "3", "name": "FASTING BLOOD SUGAR"},
        ]
        assert multilingual_synonym_search(cat3, "lft") == []

        # 4. hemoglobin vs [HBSAG]
        cat4 = [{"id": "1", "name": "HBSAG"}]
        assert multilingual_synonym_search(cat4, "hemoglobin") == []

    def test_required_positive_cases(self):
        # Tests that must match:
        # 1. sugar → HBA1C and FASTING BLOOD SUGAR
        cat = [
            {"id": "1", "name": "HBA1C"},
            {"id": "2", "name": "FASTING BLOOD SUGAR"},
            {"id": "3", "name": "KIDNEY FUNCTION TEST"},
        ]
        res = multilingual_synonym_search(cat, "sugar")
        names = [r["name"] for r in res]
        assert "HBA1C" in names
        assert "FASTING BLOOD SUGAR" in names
        assert "KIDNEY FUNCTION TEST" not in names

        # 2. थायराइड → TSH
        cat_tsh = [{"id": "1", "name": "TSH"}, {"id": "2", "name": "CBC"}]
        res_tsh = multilingual_synonym_search(cat_tsh, "थायराइड")
        assert len(res_tsh) == 1 and res_tsh[0]["name"] == "TSH"

        # 3. షుగర్ → GLUCOSE
        cat_gl = [{"id": "1", "name": "GLUCOSE"}, {"id": "2", "name": "X-RAY"}]
        res_gl = multilingual_synonym_search(cat_gl, "షుగర్")
        assert len(res_gl) == 1 and res_gl[0]["name"] == "GLUCOSE"

        # 4. typo "thyriod" → THYROID PROFILE
        cat_th = [{"id": "1", "name": "THYROID PROFILE"}]
        res_th = multilingual_synonym_search(cat_th, "thyriod")
        assert len(res_th) == 1 and res_th[0]["name"] == "THYROID PROFILE"
