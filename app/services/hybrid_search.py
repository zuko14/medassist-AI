"""Deterministic Multilingual & Synonym Catalogue Search for Kriya AI.

Provides zero-LLM, zero-embedding patient catalogue search with:
1. Exact whole-word and whole-phrase matching with boundary safety.
2. Translations and abbreviations of the SAME test (no symptom->test mapping).
3. Typo tolerance using Python standard library `difflib`.
4. Fully deterministic, non-raising, sub-millisecond execution.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Set

# Hindi -> English translations & exact clinical abbreviations of the SAME test
HINDI_MEDICAL_MAP: Dict[str, List[str]] = {
    "शुगर": ["sugar", "glucose", "hba1c", "fbs", "ppbs", "fasting blood sugar"],
    "सुगर": ["sugar", "glucose", "hba1c", "fbs", "ppbs", "fasting blood sugar"],
    "ग्लूकोज": ["glucose", "sugar", "fbs", "ppbs", "hba1c"],
    "मधुमेह": ["diabetes", "glucose", "hba1c", "sugar"],
    "थायराइड": ["thyroid", "tsh", "t3", "t4", "thyroid profile"],
    "थाइराइड": ["thyroid", "tsh", "t3", "t4", "thyroid profile"],
    "खून": ["blood", "cbc", "hemoglobin", "haemoglobin"],
    "रक्त": ["blood", "cbc", "hemoglobin", "haemoglobin"],
    "पेशाब": ["urine", "routine urine", "urinalysis"],
    "मूत्र": ["urine", "routine urine", "urinalysis"],
    "गुर्दा": ["kidney", "kft", "renal", "creatinine", "urea"],
    "किडनी": ["kidney", "kft", "renal", "creatinine", "urea"],
    "लिवर": ["liver", "lft", "hepatic", "bilirubin"],
    "जिगर": ["liver", "lft", "hepatic", "bilirubin"],
    "कोलेस्ट्रॉल": ["cholesterol", "lipid", "triglycerides", "lipid profile"],
    "कोलेस्ट्रोल": ["cholesterol", "lipid", "triglycerides", "lipid profile"],
    "विटामिन": ["vitamin", "vit d", "vit b12"],
    "कैल्शियम": ["calcium", "serum calcium"],
    "मलेरिया": ["malaria", "malaria antigen", "mp smear"],
    "टाइफाइड": ["typhoid", "widal", "typhidot"],
    "गर्भावस्था": ["pregnancy", "beta hcg", "hcg", "upt"],
    "प्रेग्नेंसी": ["pregnancy", "beta hcg", "hcg", "upt"],
    "ब्लड": ["blood", "cbc", "hemoglobin"],
    "टेस्ट": [],
    "जांच": [],
}

# Telugu -> English translations & exact clinical abbreviations of the SAME test
TELUGU_MEDICAL_MAP: Dict[str, List[str]] = {
    "షుగర్": ["sugar", "glucose", "hba1c", "fbs", "ppbs", "fasting blood sugar"],
    "గ్లూకోజ్": ["glucose", "sugar", "fbs", "ppbs", "hba1c"],
    "చక్కెర": ["sugar", "glucose", "hba1c", "diabetes"],
    "మధుమేహం": ["diabetes", "glucose", "hba1c", "sugar"],
    "థైరాయిడ్": ["thyroid", "tsh", "t3", "t4", "thyroid profile"],
    "రక్తం": ["blood", "cbc", "hemoglobin", "haemoglobin"],
    "మూత్రం": ["urine", "routine urine", "urinalysis"],
    "కిడ్నీ": ["kidney", "kft", "renal", "creatinine", "urea"],
    "మూత్రపిండం": ["kidney", "kft", "renal", "creatinine", "urea"],
    "కాలేయం": ["liver", "lft", "hepatic", "bilirubin"],
    "లివర్": ["liver", "lft", "hepatic", "bilirubin"],
    "కొలెస్ట్రాల్": ["cholesterol", "lipid", "triglycerides", "lipid profile"],
    "విటమిన్": ["vitamin", "vit d", "vit b12"],
    "కాల్షియం": ["calcium", "serum calcium"],
    "మలేరియా": ["malaria", "malaria antigen", "mp smear"],
    "టైఫాయిడ్": ["typhoid", "widal"],
    "గర్భధారణ": ["pregnancy", "beta hcg", "hcg", "upt"],
    "పరీక్ష": [],
}

# English medical abbreviations, clinical terms, and exact synonyms
ENGLISH_SYNONYM_MAP: Dict[str, List[str]] = {
    "sugar": ["glucose", "hba1c", "fbs", "ppbs", "fasting blood sugar"],
    "glucose": ["sugar", "fbs", "ppbs", "hba1c", "fasting blood sugar"],
    "diabetes": ["glucose", "hba1c", "sugar", "fbs", "ppbs"],
    "cbc": ["complete blood count", "hemogram"],
    "complete blood count": ["cbc", "hemogram"],
    "kft": ["kidney function test", "renal function test", "rft"],
    "rft": ["renal function test", "kidney function test", "kft"],
    "kidney function test": ["kft", "rft", "renal function test"],
    "renal function test": ["rft", "kft", "kidney function test"],
    "lft": ["liver function test", "hepatic function", "hepatic profile"],
    "liver function test": ["lft", "hepatic function", "hepatic profile"],
    "lipid": ["cholesterol", "triglycerides", "lipid profile"],
    "lipid profile": ["cholesterol", "lipid", "triglycerides"],
    "cholesterol": ["lipid", "triglycerides", "lipid profile"],
    "thyroid": ["tsh", "thyroid profile", "t3", "t4"],
    "thyroid profile": ["tsh", "thyroid", "t3", "t4"],
    "tsh": ["thyroid", "thyroid profile", "thyroid stimulating hormone"],
    "creatinine": ["serum creatinine"],
    "urea": ["blood urea"],
    "hemoglobin": ["haemoglobin", "hb"],
    "haemoglobin": ["hemoglobin", "hb"],
    "hb": ["hemoglobin", "haemoglobin"],
    "platelet": ["platelet count"],
    "typhoid": ["widal", "typhidot"],
    "malaria": ["malaria antigen", "mp smear"],
    "pregnancy": ["beta hcg", "hcg", "upt"],
    "hcg": ["pregnancy", "beta hcg"],
    "crp": ["c-reactive protein"],
    "esr": ["erythrocyte sedimentation rate"],
    "vitamin": ["vit d", "vit b12", "vitamin d", "vitamin b12"],
    "calcium": ["serum calcium", "total calcium"],
    "usg": ["ultrasound", "sonography"],
    "ultrasound": ["usg", "sonography"],
    "xray": ["x-ray", "radiography"],
    "x-ray": ["xray", "radiography"],
    "ecg": ["electrocardiogram", "ekg"],
}

# Common noise words to strip from query before processing
STOP_WORDS = {"test", "tests", "checkup", "package", "profile", "investigation", "panel", "for", "in", "of", "and"}


def _tokenize(text: str) -> List[str]:
    """Extract lowercase alphanumeric words and non-ASCII unicode tokens."""
    return [w for w in re.split(r"[^\w\u0900-\u097F\u0C00-\u0C7F]+", text.lower()) if w]


def get_synonym_expansions(query: str) -> Set[str]:
    """Retrieve all translated/expanded keywords for a given search query."""
    tokens = _tokenize(query)
    expansions: Set[str] = set()

    for token in tokens:
        if token in STOP_WORDS:
            continue
        if token in HINDI_MEDICAL_MAP:
            expansions.update(HINDI_MEDICAL_MAP[token])
        if token in TELUGU_MEDICAL_MAP:
            expansions.update(TELUGU_MEDICAL_MAP[token])
        if token in ENGLISH_SYNONYM_MAP:
            expansions.update(ENGLISH_SYNONYM_MAP[token])

    norm_query = query.strip().lower()
    if norm_query in HINDI_MEDICAL_MAP:
        expansions.update(HINDI_MEDICAL_MAP[norm_query])
    if norm_query in TELUGU_MEDICAL_MAP:
        expansions.update(TELUGU_MEDICAL_MAP[norm_query])
    if norm_query in ENGLISH_SYNONYM_MAP:
        expansions.update(ENGLISH_SYNONYM_MAP[norm_query])

    return {e.lower().strip() for e in expansions if e.strip()}


def _matches_candidate_term(candidate: str, test_name: str, test_desc: str, test_tokens: Set[str]) -> bool:
    """Check whether candidate matches as a whole word or whole phrase."""
    cand = candidate.strip().lower()
    if not cand:
        return False

    full_text = f"{test_name} {test_desc}".lower()

    if " " in cand:
        # Multi-word phrase matching with word boundaries
        words = cand.split()
        pattern = r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"
        return bool(re.search(pattern, full_text, re.IGNORECASE))

    # Single-token term: match whole words ONLY (never substring match)
    return cand in test_tokens


def multilingual_synonym_search(tests: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Deterministic Tier 2 & Tier 3 search for lab tests.

    Called ONLY when Tier 1 (exact substring match) yields zero results.
    Never uses an LLM or embeddings on the patient path.
    """
    if not query or not tests:
        return []

    tokens = [t for t in _tokenize(query) if t not in STOP_WORDS]
    if not tokens:
        tokens = _tokenize(query)
    if not tokens:
        return []

    # Pre-tokenize all tests once for fast, whole-token matching
    test_token_cache: List[Tuple[Dict[str, Any], str, str, Set[str]]] = []
    for t in tests:
        t_name = (t.get("name") or "").lower()
        t_desc = (t.get("description") or "").lower()
        t_tokens = set(_tokenize(f"{t_name} {t_desc}"))
        test_token_cache.append((t, t_name, t_desc, t_tokens))

    # ═══════════════════════════════════════════════════════════════════════════
    # Tier 2: Synonym & Multilingual Expansion (Conjunctive: all tokens satisfied)
    # ═══════════════════════════════════════════════════════════════════════════
    token_candidates: List[Set[str]] = []
    has_any_expansion = False
    for tok in tokens:
        exp = get_synonym_expansions(tok)
        if exp:
            has_any_expansion = True
        cands = {tok} | exp
        token_candidates.append(cands)

    if has_any_expansion:
        matched: List[Dict[str, Any]] = []
        for t, t_name, t_desc, t_tokens in test_token_cache:
            # Every query token must have at least one candidate satisfied as whole word / phrase
            if all(
                any(_matches_candidate_term(c, t_name, t_desc, t_tokens) for c in cands)
                for cands in token_candidates
            ):
                matched.append(t)

        if matched:
            matched.sort(
                key=lambda t: (
                    len(t.get("name") or ""),
                    (t.get("name") or "").lower(),
                )
            )
            return matched

    # ═══════════════════════════════════════════════════════════════════════════
    # Tier 3: Typo Tolerance via difflib (Conjunctive)
    # ═══════════════════════════════════════════════════════════════════════════
    catalogue_tokens: Dict[str, Set[str]] = {}
    for t, t_name, _, _ in test_token_cache:
        for tok in _tokenize(t_name):
            if len(tok) >= 3:
                catalogue_tokens.setdefault(tok, set()).add(t_name)

    vocab = list(catalogue_tokens.keys())
    per_token_matches: List[Set[str]] = []

    for q_tok in tokens:
        matching_names_for_token: Set[str] = set()
        if q_tok in catalogue_tokens:
            matching_names_for_token.update(catalogue_tokens[q_tok])
        elif len(q_tok) >= 3:
            close_words = difflib.get_close_matches(q_tok, vocab, n=2, cutoff=0.75)
            for w in close_words:
                matching_names_for_token.update(catalogue_tokens[w])
        if not matching_names_for_token:
            return []
        per_token_matches.append(matching_names_for_token)

    if per_token_matches:
        common_names = set.intersection(*per_token_matches)
        if common_names:
            fuzzy_matched = [
                t for t in tests if (t.get("name") or "").lower() in common_names
            ]
            fuzzy_matched.sort(
                key=lambda t: (
                    len(t.get("name") or ""),
                    (t.get("name") or "").lower(),
                )
            )
            return fuzzy_matched

    return []
