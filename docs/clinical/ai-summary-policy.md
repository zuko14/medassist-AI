# Kriya AI — Clinical AI Safety & Patient Report Summary Policy (W10.1)

**Policy Owner:** Chief Medical & AI Safety Officer  
**Effective Date:** 2026-08-25  
**Version:** 1.0.0 (Production Safety Baseline)

---

## 1. Executive Decision on Patient Delivery
AI-generated diagnostic summaries delivered to patients via WhatsApp are subject to strict, automated multi-layer clinical safety gates:
1. **Never Deliver Autonomous Diagnoses or Prescriptions:** The AI system is strictly limited to explaining test observations in non-diagnostic, plain-language terminology.
2. **Confidence-Gated Patient Routing:**
   - **Confidence ≥ 0.95:** Delivered to patient with mandatory medical disclaimer.
   - **0.80 ≤ Confidence < 0.95:** Held in `status="needs_review"` for staff validation before patient delivery.
   - **Confidence < 0.80:** Refused (`status="escalated"`); raw signed diagnostic PDF is delivered directly without AI interpretations.
3. **Canonical JSON Only:** AI generation consumes validated OCR canonical schemas (`CanonicalLabReport`), NEVER raw unverified OCR streams.

---

## 2. Mandatory Patient-Facing Medical Disclaimer
Every AI-generated summary delivered to a patient via WhatsApp MUST conclude with the localized statutory disclaimer:

> **English:**  
> *"DISCLAIMER: This automated report summary is generated for informational purposes only. It does not constitute a medical diagnosis, prescription, or clinical recommendation. Please consult your treating healthcare provider for professional medical evaluation."*

> **Hindi (हिंदी):**  
> *"अस्वीकरण: यह स्वचालित रिपोर्ट सारांश केवल सूचनात्मक उद्देश्यों के लिए तैयार किया गया है। यह चिकित्सा निदान या नुस्खा का गठन नहीं करता है।"*

> **Telugu (తెలుగు):**  
> *"గమనిక: ఈ ఆటోమేటెడ్ రిపోర్ట్ సారాంశం సమాచారం కొరకు మాత్రమే సృష్టించబడింది. ఇది వైద్య నిర్ధారణ లేదా చికిత్స సలహా కాదు."*

> **Tamil (தமிழ்):**  
> *"மறுப்பு: இந்த தானியங்கி அறிக்கை சுருக்கம் தகவலுக்காக மட்டுமே உருவாக்கப்பட்டது. இது மருத்துவ நோயறிதல் அல்லது சிகிச்சை பரிந்துரை அல்ல."*

---

## 3. Adversarial Clinical Safety Standards
The AI summarization engine is validated against adversarial attacks and distortions:
1. **Negation Dropping:** "No evidence of fracture" must never be summarized as "Fracture detected".
2. **Unit Shifts:** mg/dL vs g/dL unit mismatches must trigger immediate validation failure.
3. **Abnormal Inversion:** Out-of-range biomarkers (e.g. Glucose > 250 mg/dL) must never be marked normal.
