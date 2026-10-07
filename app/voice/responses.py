"""Everything the receptionist says, per language. The ONLY source of speech.

No LLM writes caller-facing text: the dialog engine emits a speech act
(key + params built from verified tool results) and this module renders it.
That is what makes "never fabricate a booking, a price or a report" a
structural property instead of a prompt instruction.

Style: short spoken sentences, one question per turn, natural spoken Telugu
(not written/formal prose). Every template here must be reviewed by a native
speaker before go-live (STATUS.md gate G-LANG-REVIEW).
"""

import re
from datetime import date
from typing import Optional

TEMPLATES_VERSION = "templates-2026.10.07"

T: dict = {
    "greeting": {
        "te": "నమస్కారం, {hospital}కి ఫోన్ చేసినందుకు ధన్యవాదాలు. నేను {assistant}. మీకు ఎలా సహాయం చేయగలను?",
        "en": "Namaste, thank you for calling {hospital}. This is {assistant}. How can I help you?",
        "hi": "नमस्ते, {hospital} में कॉल करने के लिए धन्यवाद। मैं {assistant} बोल रही हूँ। बताइए, मैं आपकी क्या मदद करूँ?",
    },
    "greeting_outbound": {
        "te": "నమస్కారం{who}, నేను {hospital} నుండి {assistant} మాట్లాడుతున్నాను. {interest_sentence}మీకు అపాయింట్మెంట్ బుక్ చేయడంలో సహాయం చేయమంటారా?",
        "en": "Namaste{who}, this is {assistant} calling from {hospital}. {interest_sentence}Can I help you book an appointment?",
        "hi": "नमस्ते{who}, मैं {hospital} से {assistant} बोल रही हूँ। {interest_sentence}क्या मैं आपकी अपॉइंटमेंट बुक करने में मदद करूँ?",
    },
    "who": {"te": "{name} గారు", "en": "{name}", "hi": "{name} जी"},
    "listening": {
        "te": "చెప్పండి, నేను వింటున్నాను. మీకు ఏం సహాయం కావాలి?",
        "en": "Yes, I'm listening. How can I help you?",
        "hi": "जी, मैं सुन रही हूँ। बताइए, क्या मदद चाहिए?",
    },
    "offer_self_help": {
        "te": "నేనే మీకు అపాయింట్మెంట్ బుక్ చేయగలను, రిపోర్ట్స్, ఫీజులు, టైమింగ్స్ కూడా చెప్పగలను. ఏం కావాలో చెప్పండి. లేదా రిసెప్షన్‌కి కనెక్ట్ చేయమంటారా?",
        "en": "I can book your appointment myself, and help with reports, fees and timings. Tell me what you need, or shall I connect you to reception?",
        "hi": "मैं खुद आपका अपॉइंटमेंट बुक कर सकती हूँ, और रिपोर्ट, फीस, टाइमिंग की जानकारी भी दे सकती हूँ। बताइए क्या चाहिए, या रिसेप्शन से जोड़ दूँ?",
    },
    "ask_info_topic": {
        "te": "తప్పకుండా. ఏ సమాచారం కావాలి? హాస్పిటల్ టైమింగ్స్, అడ్రస్, లేదా డాక్టర్ ఫీజులా?",
        "en": "Sure. What would you like to know: our timings, our address, or doctor fees?",
        "hi": "ज़रूर। क्या जानकारी चाहिए: टाइमिंग, पता, या डॉक्टर की फीस?",
    },
    "interest_sentence": {
        "te": "మీరు ఇటీవల {interest} గురించి అడిగారు. ",
        "en": "You recently asked us about {interest}. ",
        "hi": "आपने हाल ही में {interest} के बारे में पूछा था। ",
    },
    "ask_specialty": {
        "te": "తప్పకుండా. మీకు ఏ డాక్టర్ కావాలి? ఉదాహరణకు గుండె డాక్టర్, పిల్లల డాక్టర్.",
        "en": "Sure. Which doctor or department would you like?",
        "hi": "ज़रूर। आपको किस डॉक्टर या विभाग में दिखाना है?",
    },
    "ask_date": {"te": "ఏ రోజు కావాలి?", "en": "Which day would you like?", "hi": "किस दिन चाहिए?"},
    "ask_time_period": {
        "te": "ఉదయం కావాలా, సాయంత్రం కావాలా?", "en": "Morning or evening?", "hi": "सुबह चाहिए या शाम?",
    },
    "present_one": {
        "te": "{date} {time}కి డాక్టర్ {doctor} గారు అందుబాటులో ఉన్నారు. {fee_sentence}బుక్ చేయమంటారా?",
        "en": "Dr. {doctor} is available {date} at {time}. {fee_sentence}Shall I book it?",
        "hi": "डॉक्टर {doctor} {date} {time} बजे उपलब्ध हैं। {fee_sentence}बुक कर दूँ?",
    },
    "present_two": {
        "te": "{date} రెండు సమయాలు ఉన్నాయి: డాక్టర్ {d1} గారితో {t1}, లేదా డాక్టర్ {d2} గారితో {t2}. ఏది కావాలి?",
        "en": "{date} I have two options: Dr. {d1} at {t1}, or Dr. {d2} at {t2}. Which one would you like?",
        "hi": "{date} दो समय हैं: डॉक्टर {d1} के साथ {t1} बजे, या डॉक्टर {d2} के साथ {t2} बजे। कौन सा चाहिए?",
    },
    "fee_sentence": {"te": "ఫీజు {fee}. ", "en": "The fee is {fee}. ", "hi": "फीस {fee} है। "},
    "ask_which_doctor": {
        "te": "డాక్టర్ {d1} గారు కావాలా, డాక్టర్ {d2} గారు కావాలా?",
        "en": "Do you mean Dr. {d1} or Dr. {d2}?",
        "hi": "डॉक्टर {d1} चाहिए या डॉक्टर {d2}?",
    },
    "ask_patient_name": {
        "te": "పేషెంట్ పేరు చెప్పండి.", "en": "May I have the patient's name?", "hi": "मरीज़ का नाम बताइए।",
    },
    "confirm_booking": {
        "te": "సరే. {patient} గారికి డాక్టర్ {doctor} గారితో {date} {time}కి. {fee_sentence}బుక్ చేయమంటారా?",
        "en": "Okay. {patient} with Dr. {doctor}, {date} at {time}. {fee_sentence}Shall I book it?",
        "hi": "ठीक है। {patient} के लिए डॉक्टर {doctor} के साथ {date} {time} बजे। {fee_sentence}बुक कर दूँ?",
    },
    "ask_change": {
        "te": "సరే, ఏ రోజు లేదా ఏ సమయం కావాలి?", "en": "Okay, which day or time would you prefer?",
        "hi": "ठीक है, कौन सा दिन या समय चाहिए?",
    },
    "booked_confirmed": {
        "te": "మీ అపాయింట్మెంట్ బుక్ అయింది. బుకింగ్ నంబర్ {ref}.{wa_sentence}",
        "en": "Your appointment is booked. Your booking number is {ref}.{wa_sentence}",
        "hi": "आपका अपॉइंटमेंट बुक हो गया है। बुकिंग नंबर {ref} है।{wa_sentence}",
    },
    "wa_sentence": {
        "te": " వివరాలు WhatsAppలో పంపించాను.", "en": " I've sent the details on WhatsApp.",
        "hi": " डिटेल्स WhatsApp पर भेज दी हैं।",
    },
    "booked_payment_pending": {
        "te": "ఈ స్లాట్ {hold} నిమిషాలు మీకోసం ఉంచాను. పేమెంట్ లింక్ WhatsAppలో పంపించాను. {amount} చెల్లించగానే అపాయింట్మెంట్ ఆటోమేటిక్‌గా కన్ఫర్మ్ అవుతుంది.",
        "en": "I've held this slot for you for {hold} minutes and sent a payment link on WhatsApp. As soon as you pay {amount}, your appointment is confirmed automatically.",
        "hi": "मैंने यह स्लॉट {hold} मिनट के लिए होल्ड कर दिया है और पेमेंट लिंक WhatsApp पर भेज दिया है। {amount} भरते ही आपका अपॉइंटमेंट अपने आप कन्फर्म हो जाएगा।",
    },
    "payment_link_not_sent": {
        "te": "స్లాట్ {hold} నిమిషాలు ఉంచాను, కానీ పేమెంట్ లింక్ WhatsAppలో పంపలేకపోయాను. మా రిసెప్షన్ టీమ్ మీకు కాల్ చేస్తారు.",
        "en": "I've held the slot for {hold} minutes, but I couldn't send the payment link on WhatsApp. Our reception team will call you.",
        "hi": "मैंने स्लॉट {hold} मिनट के लिए होल्ड किया है, लेकिन पेमेंट लिंक WhatsApp पर नहीं भेज पाई। हमारी रिसेप्शन टीम आपको कॉल करेगी।",
    },
    "slot_taken": {
        "te": "క్షమించండి, ఆ స్లాట్ ఇప్పుడే వేరేవాళ్ళు బుక్ చేశారు. వేరే సమయం చూస్తాను.",
        "en": "Sorry, that slot was just taken. Let me find another time.",
        "hi": "माफ़ कीजिए, वह स्लॉट अभी-अभी बुक हो गया। मैं दूसरा समय देखती हूँ।",
    },
    "booking_system_down": {
        "te": "క్షమించండి, హాస్పిటల్ బుకింగ్ సిస్టమ్ ఇప్పుడు అందుబాటులో లేదు. మా రిసెప్షన్ టీమ్ మీకు కాల్ చేసి బుక్ చేస్తారు.",
        "en": "Sorry, the hospital booking system is temporarily unavailable. Our reception team will call you back to book it.",
        "hi": "माफ़ कीजिए, अस्पताल का बुकिंग सिस्टम अभी उपलब्ध नहीं है। हमारी रिसेप्शन टीम आपको कॉल करके बुक कर देगी।",
    },
    "booking_unverified": {
        "te": "మీ బుకింగ్‌ను నేను ఇంకా నిర్ధారించలేకపోయాను. మా టీమ్ చెక్ చేసి మీకు కాల్ చేస్తారు.",
        "en": "I couldn't confirm your booking yet. Our team will check and call you.",
        "hi": "मैं अभी आपकी बुकिंग की पुष्टि नहीं कर पाई। हमारी टीम जाँच करके आपको कॉल करेगी।",
    },
    "no_slots_on_date": {
        "te": "{date} స్లాట్లు లేవు.", "en": "There are no slots {date}.", "hi": "{date} कोई स्लॉट नहीं है।",
    },
    "no_slots_any": {
        "te": "క్షమించండి, రాబోయే రెండు వారాల్లో ఖాళీ స్లాట్లు లేవు.",
        "en": "Sorry, there are no free slots in the next two weeks.",
        "hi": "माफ़ कीजिए, अगले दो हफ़्तों में कोई खाली स्लॉट नहीं है।",
    },
    "no_doctor_for_specialty": {
        "te": "క్షమించండి, మా దగ్గర {specialty} అందుబాటులో లేరు.",
        "en": "Sorry, we don't have a {specialty} here.",
        "hi": "माफ़ कीजिए, हमारे यहाँ {specialty} उपलब्ध नहीं हैं।",
    },
    "anything_else": {
        "te": "ఇంకా ఏమైనా సహాయం కావాలా?", "en": "Is there anything else I can help you with?",
        "hi": "क्या मैं और कुछ मदद कर सकती हूँ?",
    },
    "farewell": {
        "te": "ఫోన్ చేసినందుకు ధన్యవాదాలు. జాగ్రత్తగా ఉండండి.", "en": "Thank you for calling. Take care.",
        "hi": "कॉल करने के लिए धन्यवाद। अपना ख्याल रखिए।",
    },
    "didnt_catch": {
        "te": "క్షమించండి, సరిగ్గా వినపడలేదు. మళ్ళీ చెప్పగలరా?",
        "en": "Sorry, I didn't catch that. Could you say it again?",
        "hi": "माफ़ कीजिए, ठीक से सुनाई नहीं दिया। फिर से बताएँगे?",
    },
    "clarify": {
        "te": "మీకు సరిగ్గా సహాయం చేయాలని అడుగుతున్నాను. అపాయింట్మెంట్ కావాలా, రిపోర్ట్ గురించా, లేక వేరే సమాచారం కావాలా?",
        "en": "I want to make sure I understood you correctly. Is this about an appointment, a report, or something else?",
        "hi": "मैं ठीक से समझना चाहती हूँ। क्या यह अपॉइंटमेंट के बारे में है, रिपोर्ट के बारे में, या कुछ और?",
    },
    "handoff_transfer": {
        "te": "సరే, మిమ్మల్ని మా రిసెప్షన్ టీమ్‌కి కనెక్ట్ చేస్తున్నాను. లైన్‌లో ఉండండి.",
        "en": "Sure, I'm connecting you to our reception team. Please stay on the line.",
        "hi": "ठीक है, मैं आपको हमारी रिसेप्शन टीम से जोड़ रही हूँ। कृपया लाइन पर बने रहिए।",
    },
    "handoff_callback": {
        "te": "ప్రస్తుతం మా రిసెప్షన్ అందుబాటులో లేదు. మీ రిక్వెస్ట్ నమోదు చేశాను, త్వరలోనే మీకు కాల్ చేస్తారు.",
        "en": "Our reception team isn't available right now. I've noted your request and they'll call you back soon.",
        "hi": "अभी हमारी रिसेप्शन टीम उपलब्ध नहीं है। मैंने आपकी रिक्वेस्ट नोट कर ली है, वे जल्द ही आपको कॉल करेंगे।",
    },
    "emergency": {
        "te": "ఇది అత్యవసర పరిస్థితిలా ఉంది. వెంటనే 108కి కాల్ చేయండి లేదా దగ్గర్లోని ఎమర్జెన్సీకి వెళ్ళండి.{emergency_sentence}",
        "en": "This sounds like an emergency. Please call 108 right away or go to the nearest emergency room.{emergency_sentence}",
        "hi": "यह इमरजेंसी लग रही है। तुरंत 108 पर कॉल कीजिए या नज़दीकी इमरजेंसी में जाइए।{emergency_sentence}",
    },
    "emergency_sentence": {
        "te": " మా ఎమర్జెన్సీ నంబర్ {emergency}.", "en": " Our emergency number is {emergency}.",
        "hi": " हमारा इमरजेंसी नंबर {emergency} है।",
    },
    "clinical_refusal": {
        "te": "క్షమించండి, మందులు లేదా వైద్య సలహా నేను చెప్పలేను, అది డాక్టర్ గారే చెప్పాలి. డాక్టర్ అపాయింట్మెంట్ బుక్ చేయమంటారా?",
        "en": "I'm sorry, I can't advise on medicines or treatment. Only a doctor can. Shall I book a doctor's appointment for you?",
        "hi": "माफ़ कीजिए, दवा या इलाज की सलाह मैं नहीं दे सकती, वह डॉक्टर ही देंगे। क्या मैं डॉक्टर का अपॉइंटमेंट बुक कर दूँ?",
    },
    "ai_disclosure": {
        "te": "నేను {hospital} తరఫున పనిచేసే ఆటోమేటెడ్ అసిస్టెంట్‌ని. మీకు కావాలంటే ఎప్పుడైనా మా స్టాఫ్‌కి కనెక్ట్ చేస్తాను.",
        "en": "I'm {hospital}'s automated assistant. I can connect you to our staff anytime you like.",
        "hi": "मैं {hospital} की ऑटोमेटेड असिस्टेंट हूँ। आप चाहें तो मैं कभी भी आपको हमारे स्टाफ से जोड़ सकती हूँ।",
    },
    "language_switched": {
        "te": "సరే, తెలుగులో మాట్లాడుతాను.", "en": "Sure, I'll speak in English.",
        "hi": "ठीक है, मैं हिंदी में बात करूँगी।",
    },
    "unsupported_language": {
        "te": "నేను తెలుగు, హిందీ, ఇంగ్లీష్‌లో మాట్లాడగలను. ఏ భాష కావాలి?",
        "en": "I can speak Telugu, Hindi or English. Which would you prefer?",
        "hi": "मैं तेलुगु, हिंदी या अंग्रेज़ी में बात कर सकती हूँ। कौन सी भाषा चाहिए?",
    },
    "no_upcoming": {
        "te": "ఈ నంబర్ మీద రాబోయే అపాయింట్మెంట్లు ఏవీ లేవు.",
        "en": "I don't see any upcoming appointments on this number.",
        "hi": "इस नंबर पर कोई आने वाला अपॉइंटमेंट नहीं दिख रहा।",
    },
    "upcoming_one": {
        "te": "{date} {time}కి {what} అపాయింట్మెంట్ ఉంది.{pay_note}",
        "en": "You have an appointment {what} {date} at {time}.{pay_note}",
        "hi": "{date} {time} बजे {what} आपका अपॉइंटमेंट है।{pay_note}",
    },
    "pay_note": {
        "te": " పేమెంట్ ఇంకా పెండింగ్‌లో ఉంది.", "en": " The payment is still pending.",
        "hi": " पेमेंट अभी बाकी है।",
    },
    "ask_which_appointment": {
        "te": "మీకు {n} అపాయింట్మెంట్లు ఉన్నాయి: {items}. ఏది?",
        "en": "You have {n} appointments: {items}. Which one?",
        "hi": "आपके {n} अपॉइंटमेंट हैं: {items}। कौन सा?",
    },
    "confirm_cancel": {
        "te": "{date} {time}కి {what} అపాయింట్మెంట్ రద్దు చేయమంటారా?",
        "en": "Shall I cancel your appointment {what} {date} at {time}?",
        "hi": "क्या मैं {date} {time} बजे {what} वाला अपॉइंटमेंट कैंसिल कर दूँ?",
    },
    "cancelled": {
        "te": "మీ అపాయింట్మెంట్ రద్దు అయింది.", "en": "Your appointment is cancelled.",
        "hi": "आपका अपॉइंटमेंट कैंसिल हो गया है।",
    },
    "cancelled_refunded": {
        "te": "మీ అపాయింట్మెంట్ రద్దు అయింది. మీ రీఫండ్ ప్రారంభించాము.",
        "en": "Your appointment is cancelled and your refund has been initiated.",
        "hi": "आपका अपॉइंटमेंट कैंसिल हो गया है और रिफंड शुरू कर दिया गया है।",
    },
    "cancelled_refund_failed": {
        "te": "మీ అపాయింట్మెంట్ రద్దు అయింది. రీఫండ్ ఆటోమేటిక్‌గా కాలేదు, మా టీమ్ మీకు కాల్ చేస్తారు.",
        "en": "Your appointment is cancelled. The refund couldn't be processed automatically, so our team will contact you.",
        "hi": "आपका अपॉइंटमेंट कैंसिल हो गया है। रिफंड अपने आप नहीं हो पाया, हमारी टीम आपसे संपर्क करेगी।",
    },
    "cancel_failed": {
        "te": "క్షమించండి, ఇప్పుడు రద్దు చేయలేకపోయాను. మా టీమ్ మీకు కాల్ చేస్తారు.",
        "en": "Sorry, I couldn't cancel it right now. Our team will call you.",
        "hi": "माफ़ कीजिए, अभी कैंसिल नहीं हो पाया। हमारी टीम आपको कॉल करेगी।",
    },
    "reschedule_paid_handoff": {
        "te": "ఈ అపాయింట్మెంట్‌కు పేమెంట్ అయిపోయింది, కాబట్టి మార్చడానికి మా రిసెప్షన్ టీమ్ సహాయం చేస్తారు.",
        "en": "This appointment is already paid, so our reception team will help you change it.",
        "hi": "इस अपॉइंटमेंट का पेमेंट हो चुका है, इसलिए इसे बदलने में हमारी रिसेप्शन टीम मदद करेगी।",
    },
    "rescheduled": {
        "te": "అయిపోయింది. కొత్త అపాయింట్మెంట్ బుక్ అయింది, పాతది రద్దు చేశాను.",
        "en": "Done. Your new appointment is booked and the old one is cancelled.",
        "hi": "हो गया। नया अपॉइंटमेंट बुक हो गया है और पुराना कैंसिल कर दिया है।",
    },
    "reschedule_old_kept": {
        "te": "మీ పాత అపాయింట్మెంట్ ఇంకా ఉంది. కొత్తదానికి పేమెంట్ అయ్యాక మా టీమ్ పాతది రద్దు చేస్తారు.",
        "en": "Your old appointment is still in place. Once the new one is paid, our team will cancel the old one.",
        "hi": "आपका पुराना अपॉइंटमेंट अभी बना हुआ है। नए का पेमेंट होते ही हमारी टीम पुराना कैंसिल कर देगी।",
    },
    "report_none": {
        "te": "ఈ నంబర్ మీద ఇటీవలి రిపోర్ట్లు ఏవీ కనిపించలేదు.",
        "en": "I don't see any recent reports on this number.",
        "hi": "इस नंबर पर कोई हाल की रिपोर्ट नहीं दिख रही।",
    },
    "report_not_ready": {
        "te": "మీ {test} రిపోర్ట్ ఇంకా రెడీ కాలేదు.", "en": "Your {test} report isn't ready yet.",
        "hi": "आपकी {test} रिपोर्ट अभी तैयार नहीं है।",
    },
    "report_ready_offer": {
        "te": "మీ {test} రిపోర్ట్ రెడీగా ఉంది. WhatsAppలో పంపమంటారా?",
        "en": "Your {test} report is ready. Shall I send it to your WhatsApp?",
        "hi": "आपकी {test} रिपोर्ट तैयार है। क्या मैं इसे WhatsApp पर भेज दूँ?",
    },
    "report_resent": {
        "te": "రిపోర్ట్ మీ WhatsAppకి పంపించాను.", "en": "I've sent the report to your WhatsApp.",
        "hi": "मैंने रिपोर्ट आपके WhatsApp पर भेज दी है।",
    },
    "report_resend_failed": {
        "te": "క్షమించండి, ఇప్పుడు రిపోర్ట్ పంపలేకపోయాను. మా టీమ్ మీకు పంపిస్తారు.",
        "en": "Sorry, I couldn't send the report right now. Our team will send it to you.",
        "hi": "माफ़ कीजिए, अभी रिपोर्ट नहीं भेज पाई। हमारी टीम आपको भेज देगी।",
    },
    "info_answer": {"te": "{text}", "en": "{text}", "hi": "{text}"},
    "info_unavailable": {
        "te": "క్షమించండి, ఆ సమాచారం నా దగ్గర లేదు. మా రిసెప్షన్ టీమ్‌కి కనెక్ట్ చేయమంటారా?",
        "en": "Sorry, I don't have that information. Shall I connect you to our reception team?",
        "hi": "माफ़ कीजिए, यह जानकारी मेरे पास नहीं है। क्या मैं आपको रिसेप्शन टीम से जोड़ दूँ?",
    },
    "fee_answer": {
        "te": "డాక్టర్ {doctor} గారి కన్సల్టేషన్ ఫీజు {fee}.", "en": "Dr. {doctor}'s consultation fee is {fee}.",
        "hi": "डॉक्टर {doctor} की कंसल्टेशन फीस {fee} है।",
    },
    "offer_booking": {
        "te": "అపాయింట్మెంట్ బుక్ చేయమంటారా?", "en": "Would you like me to book an appointment?",
        "hi": "क्या मैं अपॉइंटमेंट बुक कर दूँ?",
    },
    "lab_price": {"te": "{test} ధర {price}.", "en": "The {test} costs {price}.", "hi": "{test} की कीमत {price} है।"},
    "lab_not_found": {
        "te": "క్షమించండి, ఆ టెస్ట్ మా లిస్ట్‌లో కనిపించలేదు.", "en": "Sorry, I couldn't find that test in our list.",
        "hi": "माफ़ कीजिए, यह टेस्ट हमारी लिस्ट में नहीं मिला।",
    },
    "offer_lab_booking": {
        "te": "ఈ టెస్ట్ బుక్ చేయమంటారా?", "en": "Shall I book this test for you?",
        "hi": "क्या मैं यह टेस्ट बुक कर दूँ?",
    },
    "ask_lab_test": {"te": "ఏ టెస్ట్ కావాలి?", "en": "Which test would you like?", "hi": "कौन सा टेस्ट चाहिए?"},
    "ask_collection_date": {
        "te": "టెస్ట్ కోసం ఏ రోజు వస్తారు?", "en": "Which day would you like to come in for the test?",
        "hi": "टेस्ट के लिए किस दिन आएँगे?",
    },
    "confirm_lab": {
        "te": "{patient} గారికి {date} {test}. {fee_sentence}బుక్ చేయమంటారా?",
        "en": "{test} for {patient}, {date}. {fee_sentence}Shall I book it?",
        "hi": "{patient} के लिए {date} {test}। {fee_sentence}बुक कर दूँ?",
    },
    "queue_status": {
        "te": "మీ టోకెన్ నంబర్ {token}. మీ ముందు {ahead} మంది ఉన్నారు.",
        "en": "Your token number is {token}. There are {ahead} people ahead of you.",
        "hi": "आपका टोकन नंबर {token} है। आपसे पहले {ahead} लोग हैं।",
    },
    "no_queue": {
        "te": "ఈ రోజు మీకు టోకెన్ కనిపించలేదు.", "en": "I don't see a token for you today.",
        "hi": "आज आपके नाम पर कोई टोकन नहीं दिख रहा।",
    },
    "callback_created": {
        "te": "సరే, మీ రిక్వెస్ట్ నమోదు చేశాను. మా టీమ్ త్వరలోనే మీకు కాల్ చేస్తారు.",
        "en": "Okay, I've noted your request. Our team will call you back soon.",
        "hi": "ठीक है, मैंने आपकी रिक्वेस्ट नोट कर ली है। हमारी टीम जल्द ही आपको कॉल करेगी।",
    },
    "unsupported_intent": {
        "te": "దీనికి మా స్టాఫ్ సహాయం అవసరం.", "en": "Our staff will need to help you with that.",
        "hi": "इसके लिए हमारे स्टाफ की मदद चाहिए।",
    },
    "reprompt_silence": {"te": "హలో, మీరు లైన్‌లో ఉన్నారా?", "en": "Hello, are you still there?",
                         "hi": "हैलो, क्या आप लाइन पर हैं?"},
    "goodbye_silence": {
        "te": "మీ మాట వినపడటం లేదు, కాల్ ముగిస్తున్నాను. మళ్ళీ ఎప్పుడైనా కాల్ చేయండి. ధన్యవాదాలు.",
        "en": "I can't hear you, so I'll end the call now. Please call again anytime. Thank you.",
        "hi": "आपकी आवाज़ नहीं आ रही, इसलिए कॉल खत्म कर रही हूँ। कभी भी फिर से कॉल कीजिए। धन्यवाद।",
    },
    "next_task": {"te": "ఇప్పుడు మీ రెండో రిక్వెస్ట్ చూస్తాను.", "en": "Now, your other request.",
                  "hi": "अब आपकी दूसरी रिक्वेस्ट देखती हूँ।"},
    "lab_day_unavailable": {
        "te": "ఆ రోజు శాంపిల్ కలెక్షన్ లేదు. వేరే ఏ రోజు వస్తారు?",
        "en": "We don't collect samples on that day. Which other day would suit you?",
        "hi": "उस दिन सैंपल कलेक्शन नहीं होता। कौन सा दूसरा दिन ठीक रहेगा?",
    },
    "cancelled_late": {
        "te": "మీ అపాయింట్మెంట్ రద్దు అయింది. రద్దు గడువు దాటినందున రీఫండ్ వర్తించదు.",
        "en": "Your appointment is cancelled. As it was cancelled after the refund window, no refund applies.",
        "hi": "आपका अपॉइंटमेंट कैंसिल हो गया है। रिफंड की समय-सीमा निकल जाने के कारण रिफंड लागू नहीं है।",
    },
    "how_can_help": {"te": "చెప్పండి, ఏం కావాలి?", "en": "Sure, how can I help?", "hi": "जी, बताइए।"},
    "offer_human": {
        "te": "మా రిసెప్షన్ టీమ్‌కి కనెక్ట్ చేయమంటారా?", "en": "Shall I connect you to our reception team?",
        "hi": "क्या मैं आपको रिसेप्शन टीम से जोड़ दूँ?",
    },
    "offer_callback": {
        "te": "మా టీమ్ మీకు కాల్ చేసి సహాయం చేయమంటారా?", "en": "Shall I ask our team to call you back?",
        "hi": "क्या हमारी टीम आपको वापस कॉल करे?",
    },
    "old_cancelled": {
        "te": "పాత అపాయింట్మెంట్ రద్దు చేశాను.", "en": "I've cancelled your old appointment.",
        "hi": "पुराना अपॉइंटमेंट कैंसिल कर दिया है।",
    },
    "system_degraded": {
        "te": "క్షమించండి, సిస్టమ్ ఇప్పుడు స్పందించడం లేదు. మా టీమ్ మీకు కాల్ చేస్తారు.",
        "en": "Sorry, our system isn't responding right now. Our team will call you.",
        "hi": "माफ़ कीजिए, हमारा सिस्टम अभी जवाब नहीं दे रहा। हमारी टीम आपको कॉल करेगी।",
    },
}

_MONTHS = {
    "te": ("జనవరి", "ఫిబ్రవరి", "మార్చి", "ఏప్రిల్", "మే", "జూన్", "జూలై", "ఆగస్టు", "సెప్టెంబర్", "అక్టోబర్", "నవంబర్", "డిసెంబర్"),
    "hi": ("जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"),
}
_DAYS = {
    "te": ("సోమవారం", "మంగళవారం", "బుధవారం", "గురువారం", "శుక్రవారం", "శనివారం", "ఆదివారం"),
    "hi": ("सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}
_REL_DAYS = {"te": ("ఈ రోజు", "రేపు", "ఎల్లుండి"), "hi": ("आज", "कल", "परसों"),
             "en": ("today", "tomorrow", "the day after tomorrow")}
_PERIOD = {"te": ("ఉదయం", "మధ్యాహ్నం", "సాయంత్రం"), "hi": ("सुबह", "दोपहर", "शाम")}


def short(lang: str) -> str:
    """'te-IN' -> 'te'; anything unsupported renders in English."""
    s = (lang or "en").split("-")[0]
    return s if s in ("te", "hi", "en") else "en"


def speak_date(iso: str, lang: str, today: date) -> str:
    lg = short(lang)
    d = date.fromisoformat(iso)
    delta = (d - today).days
    if 0 <= delta <= 2:
        return _REL_DAYS[lg][delta]
    if lg == "en":
        return f"on {_DAYS['en'][d.weekday()]}, {_MONTHS['en'][d.month - 1]} {d.day}"
    return f"{_MONTHS[lg][d.month - 1]} {d.day}, {_DAYS[lg][d.weekday()]}"


def speak_time(hhmm: str, lang: str) -> str:
    h, m = int(str(hhmm)[:2]), int(str(hhmm)[3:5])
    h12 = h % 12 or 12
    clock = f"{h12}:{m:02d}" if m else f"{h12}"
    lg = short(lang)
    if lg == "en":
        return f"{clock} {'AM' if h < 12 else 'PM'}"
    return f"{_PERIOD[lg][0 if h < 12 else 1 if h < 16 else 2]} {clock}"


def speak_money(paise: Optional[int], lang: str) -> Optional[str]:
    if not paise or paise <= 0:
        return None
    rupees = paise // 100 if paise % 100 == 0 else round(paise / 100, 2)
    return {"te": f"{rupees} రూపాయలు", "hi": f"{rupees} रुपये"}.get(short(lang), f"{rupees} rupees")


def speak_doctor(name: str) -> str:
    return re.sub(r"^\s*(dr\.?|doctor)\s+", "", name or "", flags=re.IGNORECASE).strip()


def speak_ref(ref: str) -> str:
    """Booking refs are read character by character: 'KR-7Q2X' -> 'K R 7 Q 2 X'."""
    return " ".join(ch for ch in (ref or "") if ch.isalnum())


class _Blank(dict):
    def __missing__(self, key):
        return ""


def render(key: str, lang: str, **params) -> str:
    """Fill a template. A missing param renders empty rather than crashing a
    live call; test_every_template_renders guards against real omissions."""
    tpl = T[key].get(short(lang)) or T[key]["en"]
    return re.sub(r"\s{2,}", " ", tpl.format_map(_Blank(params))).strip()


def speak_what(appt: dict, lang: str) -> str:
    """'with Dr. X' / 'for the CBC test', in the caller's language."""
    lg = short(lang)
    if appt.get("booking_type") == "lab_test" or not appt.get("doctor_name"):
        test = appt.get("lab_test_name") or ""
        return {"te": f"{test} టెస్ట్", "hi": f"{test} टेस्ट का"}.get(lg, f"for the {test} test")
    doc = speak_doctor(appt["doctor_name"])
    return {"te": f"డాక్టర్ {doc} గారితో", "hi": f"डॉक्टर {doc} के साथ"}.get(lg, f"with Dr. {doc}")


def realize(key: str, lang: str, today: date, specialty_labels: Optional[dict] = None, **p) -> str:
    """Speech act -> sentence. Params arrive raw (ISO dates, HH:MM, paise, dicts)
    and are formatted here so a mid-call language switch re-renders correctly."""
    q = dict(p)
    if q.get("date"):
        q["date"] = speak_date(q["date"], lang, today)
    for k in ("time", "t1", "t2"):
        if q.get(k):
            q[k] = speak_time(q[k], lang)
    for k in ("doctor", "d1", "d2"):
        if q.get(k):
            q[k] = speak_doctor(q[k])
    if q.get("ref"):
        q["ref"] = speak_ref(q["ref"])
    for k in ("fee", "amount", "price"):
        if k in q:
            q[k] = speak_money(q[k], lang) or ""
    # Fragments are joined with explicit spaces (render() strips its output).
    q["fee_sentence"] = render("fee_sentence", lang, fee=q["fee"]) + " " if q.get("fee") else ""
    q["wa_sentence"] = " " + render("wa_sentence", lang) if p.get("whatsapp_sent") else ""
    q["emergency_sentence"] = " " + render("emergency_sentence", lang, emergency=p["emergency"]) if p.get("emergency") else ""
    q["who"] = " " + render("who", lang, name=p["name"]) if p.get("name") else ""
    q["interest_sentence"] = render("interest_sentence", lang, interest=p["interest"]) + " " if p.get("interest") else ""
    if q.get("specialty") and specialty_labels and q["specialty"] in specialty_labels:
        q["specialty"] = specialty_labels[q["specialty"]].get(short(lang)) or specialty_labels[q["specialty"]]["en"]
    appt = p.get("appt")
    if appt:
        q["what"] = speak_what(appt, lang)
        q["date"] = speak_date(str(appt["date"]), lang, today)
        q["time"] = speak_time(str(appt["time"]), lang) if appt.get("time") else ""
        q["pay_note"] = " " + render("pay_note", lang) if appt.get("status") == "pending_payment" else ""
    if p.get("appts"):
        items = []
        for a in p["appts"]:
            when = speak_date(str(a["date"]), lang, today)
            at = speak_time(str(a["time"]), lang) if a.get("time") else ""
            items.append(f"{speak_what(a, lang)} {when} {at}".strip())
        q["items"], q["n"] = ", ".join(items), len(items)
    return render(key, lang, **q)
