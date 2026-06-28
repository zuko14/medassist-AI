"""FAQ Engine for common hospital queries."""

import logging
from typing import Optional

from app.config import settings
from app.templates.whatsapp_templates import get_message

logger = logging.getLogger(__name__)


# FAQ Database
FAQ_DATABASE = {
    "en": {
        "visiting_hours": "Our visiting hours are from 4:00 PM to 7:00 PM daily. ICU visiting hours may vary.",
        "parking": "We have ample parking space available at {hospital_name}. Parking is free for the first 2 hours.",
        "insurance": "We accept all major health insurance providers. Please bring your insurance card and ID proof.",
        "payment": "We accept cash, credit/debit cards, UPI, and insurance. Payment can be made at the billing counter.",
        "emergency": "For emergencies, please call {hospital_emergency_number} immediately or visit our 24/7 emergency ward.",
        "address": "We are located at: {hospital_address}. Landmark: {hospital_landmark}",
        "contact": "You can reach us at: {hospital_phone}. Emergency: {hospital_emergency_number}",
        "website": "Visit our website: {hospital_website}",
        "departments": "Our departments include: General Medicine, Cardiology, Dental, Orthopedics, Gynecology, Pediatrics, Dermatology, Ophthalmology, and ENT.",
        "lab_hours": "Our diagnostic lab is open from 7:00 AM to 8:00 PM, Monday to Saturday. Sunday: 8:00 AM to 2:00 PM.",
        "pharmacy": "Our 24-hour pharmacy is located on the ground floor. We stock all essential medicines.",
        "admission": "For admission, please visit our admission desk with your doctor's recommendation letter and ID proof.",
        "discharge": "Discharge process typically takes 2-3 hours after doctor's approval. Please settle bills at the counter.",
        "reports": "Lab reports are usually available within 24 hours. You can collect them from the lab or access online.",
    },
    "hi": {
        "visiting_hours": "हमारे मिलने का समय दोपहर 4:00 बजे से शाम 7:00 बजे तक है। ICU के लिए समय अलग हो सकता है।",
        "parking": "{hospital_name} में पर्याप्त पार्किंग स्थल उपलब्ध है। पहले 2 घंटे पार्किंग निःशुल्क है।",
        "insurance": "हम सभी प्रमुख स्वास्थ्य बीमा प्रदाताओं को स्वीकार करते हैं। कृपया अपना बीमा कार्ड और पहचान पत्र लाएं।",
        "payment": "हम नकद, क्रेडिट/डेबिट कार्ड, UPI और बीमा स्वीकार करते हैं। भुगतान बिलिंग काउंटर पर किया जा सकता है।",
        "emergency": "आपातकाल के लिए, कृपया तुरंत {hospital_emergency_number} पर कॉल करें या हमारे 24/7 आपातकाल वार्ड में जाएं।",
        "address": "हम यहां स्थित हैं: {hospital_address}. लैंडमार्क: {hospital_landmark}",
        "contact": "आप हमसे संपर्क कर सकते हैं: {hospital_phone}. आपातकाल: {hospital_emergency_number}",
        "departments": "हमारे विभाग: सामान्य चिकित्सा, हृदय रोग, दंत चिकित्सा, हड्डी रोग, स्त्री रोग, बाल रोग, त्वचा रोग, नेत्र रोग, और कान-नाक-गला।",
    },
    "te": {
        "visiting_hours": "మా సందర్శకుల సమయం రోజువారీ మధ్యాహ్నం 4:00 నుండి సాయంత్రం 7:00 వరకు. ICU సమయాలు మారవచ్చు.",
        "parking": "{hospital_name}లో పుష్కలంగా పార్కింగ్ స్థలం ఉంది. మొదటి 2 గంటలు ఉచితం.",
        "insurance": "మేజర్ ఆరోగ్య బీమా ప్రొవైడర్లను అన్నింటినీ అంగీకరిస్తాం. దయచేసి మీ బీమా కార్డ్ మరియు ID ప్రూఫ్ తీసుకురండి.",
        "emergency": "అత్యవసర పరిస్థితుల కోసం, వెంటనే {hospital_emergency_number} కు కాల్ చేయండి లేదా మా 24/7 అత్యవసర వార్డ్‌కు వెళ్లండి.",
        "address": "మనం ఇక్కడ ఉన్నాం: {hospital_address}. ల్యాండ్‌మార్క్: {hospital_landmark}",
        "departments": "మా విభాగాలు: జనరల్ మెడిసిన్, కార్డియాలజీ, దంతచికిత్స, ఆర్థోపెడిక్స్, గైనకాలజీ, పీడియాట్రిక్స్, డెర్మటాలజీ, ఆఫ్తాల్మాలజీ, మరియు ENT.",
    }
}

# FAQ Keywords for matching
FAQ_KEYWORDS = {
    "en": {
        "visiting_hours": ["visiting hours", "visit time", "when can I visit", "meeting hours"],
        "parking": ["parking", "where to park", "car parking", "vehicle"],
        "insurance": ["insurance", "cashless", "health insurance", "TPA", "claim"],
        "payment": ["payment", "pay", "billing", "bill", "charges", "fees", "cost"],
        "emergency": ["emergency", "urgent", "critical", "ambulance"],
        "address": ["address", "location", "where are you", "how to reach", "directions"],
        "contact": ["contact", "phone", "number", "call", "reach you"],
        "website": ["website", "online", "portal", "web"],
        "departments": ["departments", "specialities", "services", "what do you have"],
        "lab_hours": ["lab hours", "test timing", "diagnostic", "blood test time"],
        "pharmacy": ["pharmacy", "medicine", "drug store", "medical store"],
        "admission": ["admission", "admit", "hospitalization", "get admitted"],
        "discharge": ["discharge", "release", "leave hospital", "checkout"],
        "reports": ["reports", "test results", "lab results", "medical report"],
    },
    "hi": {
        "visiting_hours": ["मिलने का समय", "विजिटिंग आवर्स", "कब मिल सकते"],
        "parking": ["पार्किंग", "गाड़ी", "वाहन"],
        "insurance": ["बीमा", "इंश्योरेंस", "क्लेम"],
        "payment": ["भुगतान", "बिल", "शुल्क", "कीमत"],
        "emergency": ["आपातकाल", "एमरजेंसी", "एम्बुलेंस"],
        "address": ["पता", "लोकेशन", "कहां है", "पहुंचना"],
        "contact": ["संपर्क", "फोन", "नंबर", "कॉल"],
    },
    "te": {
        "visiting_hours": ["సందర్శకుల సమయం", "విజిటింగ్ అవర్స్", "ఎప్పుడు కలవచ్చు"],
        "parking": ["పార్కింగ్", "కారు", "వాహనం"],
        "insurance": ["బీమా", "ఇన్సూరెన్స్", "క్లెయిమ్"],
        "payment": ["చెల్లింపు", "బిల్లు", "ధర", "ఛార్జీలు"],
        "emergency": ["అత్యవసరం", "ఎమర్జెన్సీ", "ఆంబులెన్స్"],
        "address": ["చిరునామా", "లొకేషన్", "ఎక్కడ ఉన్నారు", "ఎలా వెళ్ళాలి"],
        "contact": ["సంప్రదింపు", "ఫోన్", "నంబర్", "కాల్"],
    }
}


class FAQEngine:
    """FAQ Engine for answering common questions.
    
    Tenant Isolation:
      FAQ lookup is scoped per-clinic. Each clinic can define custom FAQ
      overrides in its config JSONB field under "custom_faqs".

      When pgvector semantic search is adopted, the pattern MUST be:
        WHERE clinic_id = $1 ORDER BY embedding <-> query_vec LIMIT 5
      NEVER query embeddings without a clinic_id pre-filter.
    """

    def __init__(self):
        self.faq_db = FAQ_DATABASE
        self.keywords = FAQ_KEYWORDS

    def _get_clinic_overrides(self, clinic: dict, lang: str) -> dict:
        """Extract per-clinic FAQ overrides from clinic config JSONB.
        
        Clinics can add custom FAQs in their config:
          {
            "custom_faqs": {
              "en": {
                "canteen": "Our canteen is open 8 AM to 9 PM on all floors.",
                "wifi": "Free Wi-Fi: Network 'Hospital-Guest', no password needed."
              }
            }
          }
        """
        config = clinic.get("config", {}) or {}
        custom_faqs = config.get("custom_faqs", {})
        return custom_faqs.get(lang, custom_faqs.get("en", {}))

    def _get_clinic_keyword_overrides(self, clinic: dict, lang: str) -> dict:
        """Extract per-clinic keyword overrides from clinic config JSONB.
        
        Clinics can add custom keyword triggers in their config:
          {
            "custom_faq_keywords": {
              "en": {
                "canteen": ["canteen", "food", "cafeteria", "lunch"],
                "wifi": ["wifi", "wi-fi", "internet", "network"]
              }
            }
          }
        """
        config = clinic.get("config", {}) or {}
        custom_kw = config.get("custom_faq_keywords", {})
        return custom_kw.get(lang, custom_kw.get("en", {}))

    def find_answer(self, message: str, clinic: dict, lang: str = "en") -> Optional[str]:
        """Find FAQ answer for a message, merging global + clinic-specific FAQs.
        
        Tenant isolation: FAQs are scoped to the clinic.
        Clinic-specific custom FAQs override global defaults when defined.
        """
        message_lower = message.lower()

        # Get keywords for the language
        lang_keywords = self.keywords.get(lang, self.keywords["en"])

        # Merge with clinic-specific keyword overrides (clinic takes priority)
        clinic_keyword_overrides = self._get_clinic_keyword_overrides(clinic, lang)
        merged_keywords = {**lang_keywords, **clinic_keyword_overrides}

        # Default info if not in clinic
        config = clinic.get("config", {}) or {}
        info = {
            "hospital_name": clinic.get("name", settings.hospital_name),
            "hospital_address": config.get("address", settings.hospital_address),
            "hospital_phone": clinic.get("phone", settings.hospital_phone),
            "hospital_emergency_number": config.get("emergency_number", settings.hospital_emergency_number),
            "hospital_website": config.get("website", settings.hospital_website),
            "hospital_landmark": config.get("landmark", settings.hospital_landmark)
        }

        # Get global FAQs merged with clinic overrides (clinic answers take priority)
        global_faqs = self.faq_db.get(lang, self.faq_db["en"])
        clinic_faq_overrides = self._get_clinic_overrides(clinic, lang)
        merged_faqs = {**global_faqs, **clinic_faq_overrides}

        # Check each FAQ category
        for category, keywords in merged_keywords.items():
            for keyword in keywords:
                if keyword in message_lower:
                    answer = merged_faqs.get(category)
                    if answer:
                        try:
                            return answer.format(**info)
                        except KeyError:
                            return answer  # Return as-is if format vars missing
                    return None

        return None

    def is_faq_query(self, message: str, clinic: dict, lang: str = "en") -> bool:
        """Check if message is a FAQ query."""
        return self.find_answer(message, clinic, lang) is not None

    def get_all_faqs(self, lang: str = "en") -> dict:
        """Get all FAQs for a language."""
        return self.faq_db.get(lang, self.faq_db["en"])


# Global instance
faq_engine = FAQEngine()
