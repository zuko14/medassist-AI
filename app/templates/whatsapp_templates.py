"""Pre-approved WhatsApp message templates for Meta Business API.

NOTE: All templates must be submitted to Meta for approval before use.
Submit at: business.facebook.com > WhatsApp > Message Templates

Category: UTILITY (not MARKETING) for healthcare use cases.
Language: en (English) - can add hi, te variants later.
"""

from app.config import settings


# META_TEMPLATE_APPROVAL_NOTE: Submit these templates to Meta Business Manager
# Template names must match exactly as registered in Meta.
TEMPLATES = {
    "appointment_confirmation": {
        "name": "appointment_confirmation",
        "language": "en",
        "category": "UTILITY",
        "body": "Your appointment with {{1}} ({{2}}) is confirmed for {{3}} at {{4}}. Reply CANCEL to cancel. - {{5}}",
        "components_builder": lambda doctor, dept, date, time, hospital: [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": doctor},
                    {"type": "text", "text": dept},
                    {"type": "text", "text": date},
                    {"type": "text", "text": time},
                    {"type": "text", "text": hospital},
                ]
            }
        ]
    },

    "reminder_24h": {
        "name": "appointment_reminder_24h",
        "language": "en",
        "category": "UTILITY",
        "body": "Reminder: Your appointment with {{1}} is tomorrow at {{2}}. Please arrive 10 mins early. Reply CANCEL if you can't make it.",
        "components_builder": lambda doctor, time: [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": doctor},
                    {"type": "text", "text": time},
                ]
            }
        ]
    },

    "reminder_2h": {
        "name": "appointment_reminder_2h",
        "language": "en",
        "category": "UTILITY",
        "body": "Your appointment at {{1}} is in 2 hours with {{2}}. Reply CANCEL to cancel.",
        "components_builder": lambda hospital, doctor: [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": hospital},
                    {"type": "text", "text": doctor},
                ]
            }
        ]
    },

    "followup_message": {
        "name": "post_appointment_followup",
        "language": "en",
        "category": "UTILITY",
        "body": "Hello {{1}}, we hope you're feeling better after your visit. Would you like to book a follow-up appointment? Reply YES or call us at {{2}}.",
        "components_builder": lambda name, phone: [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": name},
                    {"type": "text", "text": phone},
                ]
            }
        ]
    },

    "opt_out_confirmation": {
        "name": "opt_out_confirmation",
        "language": "en",
        "category": "UTILITY",
        "body": "You've been unsubscribed from {{1}} WhatsApp reminders. Message us anytime to re-subscribe. For urgent help call {{2}}.",
        "components_builder": lambda hospital, emergency_phone: [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": hospital},
                    {"type": "text", "text": emergency_phone},
                ]
            }
        ]
    },

    "data_deletion_confirmation": {
        "name": "data_deletion_confirmation",
        "language": "en",
        "category": "UTILITY",
        "body": "Your data has been deleted from {{1}} systems as requested. Reference: {{2}}. For records, contact {{3}}.",
        "components_builder": lambda hospital, ref, contact: [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": hospital},
                    {"type": "text", "text": ref},
                    {"type": "text", "text": contact},
                ]
            }
        ]
    },

    "emergency_response": {
        "name": "emergency_response_v2",
        "language": "en",
        "category": "UTILITY",
        "body": "⚠️ This sounds urgent. Please call {{1}} (ambulance) immediately or visit our emergency ward. Address: {{2}}",
        "components_builder": lambda emergency_num, address: [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": emergency_num},
                    {"type": "text", "text": address},
                ]
            }
        ]
    },

    "reengagement": {
        "name": "patient_reengagement",
        "language": "en",
        "category": "UTILITY",
        "body": "Hello {{1}}, it's been a while. Your health matters to us. Would you like to schedule a checkup? Message YES to get started.",
        "components_builder": lambda name: [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": name},
                ]
            }
        ]
    },

    "appointment_cancelled_doctor_leave": {
        "name": "appointment_cancelled_doctor_leave",
        "language": "en",
        "category": "UTILITY",
        "body": "We're sorry, your appointment with {{1}} on {{2}} has been cancelled as the doctor is unavailable. Reply REBOOK to reschedule. We apologise for the inconvenience.",
        "components_builder": lambda doctor, date: [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": doctor},
                    {"type": "text", "text": date},
                ]
            }
        ]
    },
}


# Multilingual message templates for freeform messages
MESSAGES = {
    "en": {
        "welcome": "Welcome to {hospital_name} 🏥\nI'm MediAssist, your AI scheduling assistant.",
        "disclaimer": "⚠️ MediAssist is a scheduling assistant. It does not provide medical advice. For emergencies, call {emergency}.",
        "consent_request": "To book your appointment, I need to save your name and contact details as per our privacy policy. Reply YES to continue or NO to proceed without saving.",
        "consent_thanks": "Thank you! Your data will be stored securely.",
        "consent_no_save": "No problem! I'll help you without storing your data.",
        "main_menu": "What would you like to do?",
        "language_select": "Welcome to {hospital_name} 🏥\nनमस्ते | నమస్కారం\n\nPlease select your language:\nअपनी भाषा चुनें | మీ భాష ఎంచుకోండి",
        "ask_name": "Please share the patient's full name.",
        "welcome_back": "Welcome back, {name}! Who is this appointment for?",
        "ask_symptoms": "What symptoms or concerns do you have? (You can skip this by typing 'skip')",
        "suggesting_department": "Based on your concern, our *{department}* team may be able to help.\n\nReason: {reason}\n\nShall I book there?",
        "select_doctor": "Please select a doctor:",
        "select_date": "Please select a date (today or any date in the next 30 days).",
        "select_slot": "Please select a time slot:",
        "confirm_booking": "Please confirm your appointment:\n\n👤 {name}\n👨‍⚕️ {doctor}\n🏥 {department}\n📅 {date}\n🕐 {time}\n\nIs this correct?",
        "booking_confirmed": "✅ Appointment confirmed!\n\nRef: {ref}\n👨‍⚕️ {doctor}\n📅 {date} at {time}\n\nPlease arrive 10 mins early. Reply CANCEL to cancel.",
        "booking_failed": "Sorry, I couldn't book the appointment. Please try again or call us at {phone}.",
        "slot_taken": "That slot was just booked by someone else. Here are the next available times with {doctor}:",
        "doctor_fully_booked": "{doctor} has no available slots in the next 7 days. Here are other {department} doctors:",
        "no_doctors_available": "Our {department} team is fully booked right now. Please call us directly: {phone}",
        "session_timeout": "Your booking session timed out. Here's the main menu to start again.",
        "already_booking": "You're already booking an appointment with {doctor}. Continue that or start a new booking?",
        "emergency": "🚨 This is an emergency. Please call {emergency} immediately or visit our emergency ward.",
        "human_escalation": "Connecting you to our staff. Please call {phone} or wait for a callback.",
        "opt_out_confirm": "You've been unsubscribed. Message us anytime to re-subscribe.",
        "data_deleted": "Your data has been deleted from our systems.",
        "invalid_input": "I didn't understand that. Please try again or type 'help'.",
        "thank_you": "Thank you for choosing {hospital_name}. Take care!",
        "change_language": "🌐 Change Language",
        "available_doctors_in": "Available doctors in {dept}:",
        "our_doctors_body": "Choose a doctor to book an appointment directly:",
        "our_services_body": "Here are the services we offer at {hospital_name}:"
    },
    "hi": {
        "welcome": "{hospital_name} में आपका स्वागत है 🏥\nमैं MediAssist हूं, आपका AI सहायक।",
        "disclaimer": "⚠️ MediAssist एक शेड्यूलिंग सहायक है। यह चिकित्सा सलाह नहीं देता। आपातकाल के लिए, {emergency} पर कॉल करें।",
        "consent_request": "अपॉइंटमेंट बुक करने के लिए, मुझे हमारी गोपनीयता नीति के अनुसार आपका नाम और संपर्क विवरण सहेजने की आवश्यकता है। जारी रखने के लिए YES दर्ज करें या बिना सहेजे आगे बढ़ने के लिए NO।",
        "consent_thanks": "धन्यवाद! आपका डेटा सुरक्षित रूप से संग्रहीत किया जाएगा।",
        "consent_no_save": "कोई बात नहीं! मैं बिना डेटा संग्रहीत किए आपकी मदद करूंगा।",
        "main_menu": "आप क्या करना चाहेंगे?",
        "ask_name": "कृपया मरीज़ का पूरा नाम बताएं।",
        "welcome_back": "वापसी पर स्वागत है, {name}! यह अपॉइंटमेंट किसके लिए है?",
        "ask_symptoms": "आपके क्या लक्षण या चिंताएं हैं? ('skip' टाइप करके इसे छोड़ सकते हैं)",
        "suggesting_department": "आपकी चिंता के आधार पर, हमारी *{department}* टीम मदद कर सकती है।\n\nकारण: {reason}\n\nक्या मैं वहां बुक करूं?",
        "select_doctor": "कृपया एक डॉक्टर चुनें:",
        "select_date": "कृपया एक तारीख चुनें (आज या अगले 30 दिनों में कोई भी तारीख)।",
        "select_slot": "कृपया एक समय स्लॉट चुनें:",
        "confirm_booking": "कृपया अपनी अपॉइंटमेंट की पुष्टि करें:\n\n👤 {name}\n👨‍⚕️ {doctor}\n🏥 {department}\n📅 {date}\n🕐 {time}\n\nक्या यह सही है?",
        "booking_confirmed": "✅ अपॉइंटमेंट की पुष्टि हो गई!\n\nरेफ: {ref}\n👨‍⚕️ {doctor}\n📅 {date} {time} बजे\n\nकृपया 10 मिनट पहले पहुंचें। रद्द करने के लिए CANCEL लिखें।",
        "booking_failed": "क्षमा करें, मैं अपॉइंटमेंट बुक नहीं कर सका। कृपया फिर से प्रयास करें या हमें {phone} पर कॉल करें।",
        "slot_taken": "वह स्लॉट अभी किसी और ने बुक कर लिया। {doctor} के लिए अगले उपलब्ध समय यहां हैं:",
        "session_timeout": "आपकी बुकिंग सत्र समाप्त हो गया। फिर से शुरू करने के लिए यहां मुख्य मेनू है।",
        "already_booking": "आप पहले से ही {doctor} के साथ अपॉइंटमेंट बुक कर रहे हैं। उसे जारी रखें या नई बुकिंग शुरू करें?",
        "emergency": "🚨 यह एक आपातकाल है। कृपया तुरंत {emergency} पर कॉल करें या हमारे आपातकाल वार्ड में जाएं।",
        "human_escalation": "आपको हमारे स्टाफ से जोड़ा जा रहा है। कृपया {phone} पर कॉल करें या कॉलबैक की प्रतीक्षा करें।",
        "opt_out_confirm": "आपने अनसब्सक्राइब कर लिया है। पुनः सब्सक्राइब करने के लिए कभी भी मैसेज करें।",
        "data_deleted": "आपका डेटा हमारे सिस्टम से हटा दिया गया है।",
        "invalid_input": "मैं इसे समझ नहीं पाया। कृपया फिर से प्रयास करें या 'help' टाइप करें।",
        "thank_you": "{hospital_name} चुनने के लिए धन्यवाद। स्वस्थ रहें!",
        "change_language": "🌐 भाषा बदलें",
        "available_doctors_in": "{dept} में उपलब्ध डॉक्टर:",
        "our_doctors_body": "सीधे अपॉइंटमेंट बुक करने के लिए डॉक्टर चुनें:",
        "our_services_body": "{hospital_name} में हमारी सेवाएं:"
    },
    "te": {
        "welcome": "{hospital_name} కు స్వాగతం 🏥\nనేను MediAssist, మీ AI సహాయకుడిని.",
        "disclaimer": "⚠️ MediAssist షెడ్యూలింగ్ సహాయకుడు. ఇది వైద్య సలహా ఇవ్వదు. అత్యవసర పరిస్థితుల కోసం, {emergency} కు కాల్ చేయండి.",
        "consent_request": "అపాయింట్‌మెంట్ బుక్ చేయడానికి, మా ప్రైవసీ పాలసీ ప్రకారం మీ పేరు మరియు సంప్రదింపు వివరాలను సేవ్ చేయాలి. కొనసాగించడానికి YES టైప్ చేయండి లేదా సేవ్ చేయకుండా ముందుకు వెళ్లడానికి NO.",
        "consent_thanks": "ధన్యవాదాలు! మీ డేటా సురక్షితంగా నిల్వ చేయబడుతుంది.",
        "consent_no_save": "సమస్య లేదు! నేను డేటా నిల్వ చేయకుండా మీకు సహాయం చేస్తాను.",
        "main_menu": "మీరు ఏమి చేయాలనుకుంటున్నారు?",
        "ask_name": "దయచేసి రోగి పూర్తి పేరును పంచుకోండి.",
        "welcome_back": "తిరిగి వచ్చినందుకు స్వాగతం, {name}! ఈ అపాయింట్‌మెంట్ ఎవరి కోసం?",
        "ask_symptoms": "మీకు ఏ లక్షణాలు లేదా ఆందోళనలు ఉన్నాయి? ('skip' టైప్ చేసి దాటవేయవచ్చు)",
        "suggesting_department": "మీ ఆందోళన ఆధారంగా, మా *{department}* బృందం సహాయం చేయగలదు.\n\nకారణం: {reason}\n\nఅక్కడ బుక్ చేయాలా?",
        "select_doctor": "దయచేసి ఒక డాక్టర్‌ను ఎంచుకోండి:",
        "select_date": "దయచేసి ఒక తేదీని ఎంచుకోండి (ఈరోజు లేదా తదుపరి 30 రోజుల్లో ఏదైనా).",
        "select_slot": "దయచేసి ఒక సమయ స్లాట్‌ను ఎంచుకోండి:",
        "confirm_booking": "దయచేసి మీ అపాయింట్‌మెంట్‌ను నిర్ధారించండి:\n\n👤 {name}\n👨‍⚕️ {doctor}\n🏥 {department}\n📅 {date}\n🕐 {time}\n\nఇది సరైనదేనా?",
        "booking_confirmed": "✅ అపాయింట్‌మెంట్ నిర్ధారించబడింది!\n\nరిఫ్: {ref}\n👨‍⚕️ {doctor}\n📅 {date} {time}\n\nదయచేసి 10 నిమిషాల ముందు వరుడండి. రద్దు చేయడానికి CANCEL అని రిప్లై చేయండి.",
        "booking_failed": "క్షమించండి, నేను అపాయింట్‌మెంట్ బుక్ చేయలేకపోయాను. దయచేసి మళ్లీ ప్రయత్నించండి లేదా మాకు {phone} కు కాల్ చేయండి.",
        "slot_taken": "ఆ స్లాట్ ఇప్పుడే మరొకరు బుక్ చేసుకున్నారు. {doctor} కు అందుబాటులో ఉన్న తదుపరి సమయాలు:",
        "session_timeout": "మీ బుకింగ్ సెషన్ ముగిసింది. మళ్లీ ప్రారంభించడానికి ఇక్కడ ప్రధాన మెను ఉంది.",
        "already_booking": "మీరు ఇప్పటికే {doctor} తో అపాయింట్‌మెంట్ బుక్ చేస్తున్నారు. దానిని కొనసాగించండి లేదా కొత్త బుకింగ్ ప్రారంభించండి?",
        "emergency": "🚨 ఇది అత్యవసర పరిస్థితి. దయచేసి వెంటనే {emergency} కు కాల్ చేయండి లేదా మా అత్యవసర వార్డ్‌కు వెళ్లండి.",
        "human_escalation": "మీరు మా సిబ్బందికి కనెక్ట్ అవుతున్నారు. దయచేసి {phone} కు కాల్ చేయండి లేదా కాల్‌బ్యాక్ కోసం వేచి ఉండండి.",
        "opt_out_confirm": "మీరు అన్‌సబ్‌స్క్రైబ్ చేసుకున్నారు. తిరిగి సబ్‌స్క్రైబ్ చేయడానికి ఎప్పుడైనా మెసేజ్ చేయండి.",
        "data_deleted": "మీ డేటా మా సిస్టమ్‌ల నుండి తొలగించబడింది.",
        "invalid_input": "నేను దానిని అర్థం చేసుకోలేకపోయాను. దయచేసి మళ్లీ ప్రయత్నించండి లేదా 'help' టైప్ చేయండి.",
        "thank_you": "{hospital_name} ఎంచుకున్నందుకు ధన్యవాదాలు. జాగ్రత్త!",
        "change_language": "🌐 భాష మార్చు",
        "available_doctors_in": "{dept}లో అందుబాటులో ఉన్న డాక్టర్లు:",
        "our_doctors_body": "నేరుగా అపాయింట్మెంట్ బుక్ చేయడానికి డాక్టర్ను ఎంచుకోండి:",
        "our_services_body": "{hospital_name}లో మా సేవలు:"
    }
}


def get_message(key: str, lang: str = "en", **kwargs) -> str:
    """Get a message in the specified language with formatting."""
    # Default to English if language not supported
    if lang not in MESSAGES:
        lang = "en"

    # Get message template
    template = MESSAGES[lang].get(key, MESSAGES["en"].get(key, ""))

    # Add hospital config to kwargs
    format_kwargs = {
        "hospital_name": settings.hospital_name,
        "emergency": settings.hospital_emergency_number,
        "phone": settings.hospital_phone,
        **kwargs
    }

    return template.format(**format_kwargs)
