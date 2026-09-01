"""Pre-approved WhatsApp message templates for Meta Business API.

NOTE: All templates must be submitted to Meta for approval before use.
Submit at: business.facebook.com > WhatsApp > Message Templates

Category: UTILITY (not MARKETING) for healthcare use cases.
Language: en (English) - can add hi, te variants later.
"""

from datetime import datetime, timedelta, timezone as _dt_timezone
from typing import Optional

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
                ],
            }
        ],
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
                ],
            }
        ],
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
                ],
            }
        ],
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
                ],
            }
        ],
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
                ],
            }
        ],
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
                ],
            }
        ],
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
                ],
            }
        ],
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
                ],
            }
        ],
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
                ],
            }
        ],
    },
}


# Multilingual message templates for freeform messages
MESSAGES = {
    "en": {
        "welcome": "Welcome to {hospital_name} 🏥\nI'm Kriya AI, your AI scheduling assistant.",
        "disclaimer": "⚠️ Kriya AI is a scheduling assistant. It does not provide medical advice. For emergencies, call {emergency}.",
        "consent_request": "To book your appointment, I need to save your name and contact details as per our privacy policy. Reply YES to continue or NO to proceed without saving.",
        "consent_thanks": "Thank you! Your data will be stored securely.",
        "consent_no_save": "No problem! I'll help you without storing your data.",
        "main_menu": "What would you like to do?",
        "language_select": "Welcome to {hospital_name} 🏥\nनमस्ते | నమస్కారం\n\nPlease select your language:\nअपनी भाषा चुनें | మీ భాష ఎంచుకోండి",
        "ask_name": "Please share the patient's full name.",
        "welcome_back": "Welcome back, {name}!",
        "ask_symptoms": "What symptoms or concerns do you have? (You can skip this by typing 'skip')",
        "suggesting_department": "Based on your concern, our *{department}* team may be able to help.\n\nReason: {reason}\n\nShall I book there?",
        "select_doctor": "Please select a doctor:",
        "select_date": "Please select a date (today or any date in the next 30 days).",
        "select_datetime": "Please select a date & time for your appointment:",
        "select_slot": "Please select a time slot:",
        "confirm_booking": "Please confirm your appointment:\n\n👤 {name}\n👨‍⚕️ {doctor}\n🏥 {department}\n📅 {date}\n🕐 {time}\n\nIs this correct?",
        "booking_confirmed": "✅ Appointment confirmed!\n\nRef: {ref}\n👨‍⚕️ {doctor}\n📅 {date} at {time}\n\nPlease arrive 10 mins early. Reply CANCEL to cancel.",
        "booking_failed": "Sorry, I couldn't book the appointment. Please try again or call us at {phone}.",
        "slot_taken": "That slot was just booked by someone else. Here are the next available times with {doctor}:",
        "doctor_fully_booked": "{doctor} has no available slots in the next 7 days. Here are other {department} doctors:",
        "no_doctors_available": "Our {department} team is fully booked right now. Please call us directly: {phone}",
        "session_timeout": "Your booking session timed out. Here's the main menu to start again.",
        "already_booking": "You're already booking an appointment with {doctor}. Continue that or start a new booking?",
        "emergency": "🚨 MEDICAL EMERGENCY DETECTED. Please call our emergency desk immediately at {emergency} or dial 108.",
        "human_escalation": "Connecting you to our staff. Please call {phone} or wait for a callback.",
        "opt_out_confirm": "Done — you won't get follow-up or health check-in messages from us any more. You'll still get appointment reminders, lab reports and payment updates for anything you book. Message us anytime to re-subscribe.",
        "data_deleted": "Your data has been deleted from our systems.",
        "invalid_input": "I didn't understand that. Please try again or type 'help'.",
        "unsupported_media": "I can only read text messages right now — I can't open photos, voice notes, or files. Please type your message and I'll help you right away. For anything urgent, call {phone}.",
        "thank_you": "Thank you for choosing {hospital_name}. Take care!",
        "change_language": "🌐 Change Language",
        "available_doctors_in": "Available doctors in {dept}:",
        "our_doctors_body": "Choose a doctor to book an appointment directly:",
        "our_services_body": "Here are the services we offer at {hospital_name}:",
        "health_checkin": "Hi {name}, checking in after your visit to Dr. {doctor}. How are you feeling?",
        "health_checkin_concern": "Sorry to hear that. Please call us at {phone} so we can help — don't wait if symptoms are serious.",
        "health_checkin_ok": "Great to hear! Take care, and reach out anytime if that changes.",
        "queue_status_waiting": "🎫 *Live OPD Token Status*\n\nYour Token Number: *#{token}*\nDoctor: *{doctor}*\nCurrently Serving: *#{current}*\nPatients Ahead of You: *{ahead}*\n\nPlease be near the OPD waiting area when your token is close.",
        "queue_status_not_checked_in": "You have an appointment today with *{doctor}*, but have not checked in yet.\n\nPlease visit the reception desk to collect your OPD token number.",
        "queue_your_turn": "🔔 *It's your turn now!*\n\nToken Number: *#{token}*\nDoctor: *{doctor}*\n\nPlease proceed to the consultation room.",
        "queue_status_none": "You don't have a confirmed appointment scheduled for today.\n\nType *book* to schedule an appointment.",
        "cancellation_policy_note": "ℹ️ Cancellation Policy: Free cancellation with full refund is available up to {hours} hours before your slot (before {cutoff}).",
        "cancellation_policy_note_anytime": "ℹ️ Cancellation Policy: Free cancellation with full refund is available any time before your appointment starts (before {cutoff}).",
        "cancellation_cutoff_note": "ℹ️ Cancellation Policy: Please cancel at least {hours} hours before your slot (before {cutoff}) so we can offer it to another patient.",
        "cancellation_cutoff_note_anytime": "ℹ️ Cancellation Policy: You can cancel any time before your appointment starts (before {cutoff}).",
        "refund_receipt": "✅ *Appointment Cancelled & Refund Initiated*\n--------------------------------\nDoctor: {doctor}\nDate: {date}\nRefund Amount: ₹{amount}\nRefund Reference: {refund_id}\nPayment Gateway: Razorpay\nStatus: Refund has been initiated back to your original payment method (UPI / Card). Amount will be credited within 2 to 5 business days.",
        "refund_late_no_refund": "⚠️ Your appointment has been cancelled. As per clinic policy, cancellations made within {hours} hours of the slot are non-refundable.",
        "refund_failed_manual_review": "⚠️ Your appointment has been cancelled. We could not start the refund automatically — our team has been notified and will process it manually. Please contact us at {phone} if you do not hear back within 2 business days.",
        "refund_late_slot_started": "⚠️ Your appointment has been cancelled. As per clinic policy, cancellations made after the appointment start time are non-refundable.",
    },
    "hi": {
        "welcome": "{hospital_name} में आपका स्वागत है 🏥\nमैं Kriya AI हूं, आपका AI सहायक।",
        "disclaimer": "⚠️ Kriya AI एक शेड्यूलिंग सहायक है। यह चिकित्सा सलाह नहीं देता। आपातकाल के लिए, {emergency} पर कॉल करें।",
        "consent_request": "अपॉइंटमेंट बुक करने के लिए, मुझे हमारी गोपनीयता नीति के अनुसार आपका नाम और संपर्क विवरण सहेजने की आवश्यकता है। जारी रखने के लिए YES दर्ज करें या बिना सहेजे आगे बढ़ने के लिए NO।",
        "consent_thanks": "धन्यवाद! आपका डेटा सुरक्षित रूप से संग्रहीत किया जाएगा।",
        "consent_no_save": "कोई बात नहीं! मैं बिना डेटा संग्रहीत किए आपकी मदद करूंगा।",
        "main_menu": "आप क्या करना चाहेंगे?",
        "ask_name": "कृपया मरीज़ का पूरा नाम बताएं।",
        "welcome_back": "वापसी पर स्वागत है, {name}!",
        "ask_symptoms": "आपके क्या लक्षण या चिंताएं हैं? ('skip' टाइप करके इसे छोड़ सकते हैं)",
        "suggesting_department": "आपकी चिंता के आधार पर, हमारी *{department}* टीम मदद कर सकती है।\n\nकारण: {reason}\n\nक्या मैं वहां बुक करूं?",
        "select_doctor": "कृपया एक डॉक्टर चुनें:",
        "select_date": "कृपया एक तारीख चुनें (आज या अगले 30 दिनों में कोई भी तारीख)।",
        "select_datetime": "कृपया अपनी अपॉइंटमेंट के लिए तारीख और समय चुनें:",
        "select_slot": "कृपया एक समय स्लॉट चुनें:",
        "confirm_booking": "कृपया अपनी अपॉइंटमेंट की पुष्टि करें:\n\n👤 {name}\n👨‍⚕️ {doctor}\n🏥 {department}\n📅 {date}\n🕐 {time}\n\nक्या यह सही है?",
        "booking_confirmed": "✅ अपॉइंटमेंट की पुष्टि हो गई!\n\nरेफ: {ref}\n👨‍⚕️ {doctor}\n📅 {date} {time} बजे\n\nकृपया 10 मिनट पहले पहुंचें। रद्द करने के लिए CANCEL लिखें।",
        "booking_failed": "क्षमा करें, मैं अपॉइंटमेंट बुक नहीं कर सका। कृपया फिर से प्रयास करें या हमें {phone} पर कॉल करें।",
        "slot_taken": "वह स्लॉट अभी किसी और ने बुक कर लिया। {doctor} के लिए अगले उपलब्ध समय यहां हैं:",
        "session_timeout": "आपकी बुकिंग सत्र समाप्त हो गया। फिर से शुरू करने के लिए यहां मुख्य मेनू है।",
        "already_booking": "आप पहले से ही {doctor} के साथ अपॉइंटमेंट बुक कर रहे हैं। उसे जारी रखें या नई बुकिंग शुरू करें?",
        "emergency": "🚨 चिकित्सा आपातकाल! कृपया तुरंत हमारे आपातकालीन डेस्क पर {emergency} पर कॉल करें या 108 डायल करें।",
        "human_escalation": "आपको हमारे स्टाफ से जोड़ा जा रहा है। कृपया {phone} पर कॉल करें या कॉलबैक की प्रतीक्षा करें।",
        "opt_out_confirm": "हो गया — अब आपको फॉलो-अप या हेल्थ चेक-इन संदेश नहीं मिलेंगे। आपकी बुकिंग से जुड़े अपॉइंटमेंट रिमाइंडर, लैब रिपोर्ट और भुगतान सूचनाएं मिलती रहेंगी। पुनः सब्सक्राइब करने के लिए कभी भी मैसेज करें।",
        "data_deleted": "आपका डेटा हमारे सिस्टम से हटा दिया गया है।",
        "invalid_input": "मैं इसे समझ नहीं पाया। कृपया फिर से प्रयास करें या 'help' टाइप करें।",
        "unsupported_media": "मैं अभी केवल टेक्स्ट संदेश पढ़ सकता हूँ — फोटो, वॉयस नोट या फाइल नहीं खोल सकता। कृपया अपना संदेश टाइप करें। जरूरी हो तो {phone} पर कॉल करें।",
        "thank_you": "{hospital_name} चुनने के लिए धन्यवाद। स्वस्थ रहें!",
        "change_language": "🌐 भाषा बदलें",
        "available_doctors_in": "{dept} में उपलब्ध डॉक्टर:",
        "our_doctors_body": "सीधे अपॉइंटमेंट बुक करने के लिए डॉक्टर चुनें:",
        "our_services_body": "{hospital_name} में हमारी सेवाएं:",
        "health_checkin": "नमस्ते {name}, डॉ. {doctor} से आपकी मुलाकात के बाद जांच कर रहे हैं। आप कैसा महसूस कर रहे हैं?",
        "health_checkin_concern": "यह सुनकर खेद है। कृपया हमें {phone} पर कॉल करें ताकि हम मदद कर सकें — लक्षण गंभीर होने पर प्रतीक्षा न करें।",
        "health_checkin_ok": "यह सुनकर अच्छा लगा! ध्यान रखें, और कुछ बदलने पर कभी भी संपर्क करें।",
        "queue_status_waiting": "🎫 *ओपीडी टोकन स्थिति*\n\nआपका टोकन नंबर: *#{token}*\nडॉक्टर: *{doctor}*\nवर्तमान टोकन: *#{current}*\nआपसे आगे मरीज: *{ahead}*\n\nकृपया टोकन पास आने पर प्रतीक्षा क्षेत्र में उपस्थित रहें।",
        "queue_your_turn": "🔔 *अब आपकी बारी है!*\n\nटोकन नंबर: *#{token}*\nडॉक्टर: *{doctor}*\n\nकृपया परामर्श कक्ष में जाएं।",
        "queue_status_not_checked_in": "आज आपका *{doctor}* के साथ अपॉइंटमेंट है, लेकिन आपने अभी तक चेक-इन नहीं किया है।\n\nकृपया अपना ओपीडी टोकन लेने के लिए रिसेप्शन पर संपर्क करें।",
        "queue_status_none": "आज के लिए आपका कोई कन्फर्म अपॉइंटमेंट नहीं है।\n\nअपॉइंटमेंट बुक करने के लिए *book* लिखें।",
        "cancellation_policy_note": "ℹ️ रद्दीकरण नीति: आपके स्लॉट से {hours} घंटे पहले ({cutoff} से पहले) तक पूर्ण रिफंड के साथ रद्दीकरण उपलब्ध है।",
        "cancellation_policy_note_anytime": "ℹ️ रद्दीकरण नीति: अपॉइंटमेंट शुरू होने से पहले ({cutoff} से पहले) कभी भी पूर्ण रिफंड के साथ रद्द कर सकते हैं।",
        "cancellation_cutoff_note": "ℹ️ रद्दीकरण नीति: कृपया अपने स्लॉट से कम से कम {hours} घंटे पहले ({cutoff} से पहले) रद्द करें ताकि हम यह स्लॉट किसी और मरीज़ को दे सकें।",
        "cancellation_cutoff_note_anytime": "ℹ️ रद्दीकरण नीति: आप अपॉइंटमेंट शुरू होने से पहले ({cutoff} से पहले) कभी भी रद्द कर सकते हैं।",
        "refund_receipt": "✅ *अपॉइंटमेंट रद्द — रिफंड शुरू किया गया*\n--------------------------------\nडॉक्टर: {doctor}\nदिनांक: {date}\nरिफंड राशि: ₹{amount}\nरिफंड रेफरेंस: {refund_id}\nभुगतान गेटवे: Razorpay\nस्थिति: रिफंड आपके मूल भुगतान माध्यम (UPI / कार्ड) में भेज दिया गया है। राशि 2 से 5 कार्यदिवसों में आपके खाते में जमा हो जाएगी।",
        "refund_late_no_refund": "⚠️ आपका अपॉइंटमेंट रद्द कर दिया गया है। क्लिनिक की नीति के अनुसार, स्लॉट से {hours} घंटे के भीतर किए गए रद्दीकरण पर रिफंड नहीं मिलता।",
        "refund_failed_manual_review": "⚠️ आपका अपॉइंटमेंट रद्द कर दिया गया है। रिफंड स्वचालित रूप से शुरू नहीं हो पाया — हमारी टीम को सूचित कर दिया गया है और इसे मैन्युअल रूप से प्रोसेस किया जाएगा। 2 कार्यदिवसों में जवाब न मिलने पर {phone} पर संपर्क करें।",
        "refund_late_slot_started": "⚠️ आपका अपॉइंटमेंट रद्द कर दिया गया है। क्लिनिक की नीति के अनुसार, अपॉइंटमेंट का समय शुरू होने के बाद किए गए रद्दीकरण पर रिफंड नहीं मिलता।",
    },
    "te": {
        "welcome": "{hospital_name} కు స్వాగతం 🏥\nనేను Kriya AI, మీ AI సహాయకుడిని.",
        "disclaimer": "⚠️ Kriya AI షెడ్యూలింగ్ సహాయకుడు. ఇది వైద్య సలహా ఇవ్వదు. అత్యవసర పరిస్థితుల కోసం, {emergency} కు కాల్ చేయండి.",
        "consent_request": "అపాయింట్‌మెంట్ బుక్ చేయడానికి, మా ప్రైవసీ పాలసీ ప్రకారం మీ పేరు మరియు సంప్రదింపు వివరాలను సేవ్ చేయాలి. కొనసాగించడానికి YES టైప్ చేయండి లేదా సేవ్ చేయకుండా ముందుకు వెళ్లడానికి NO.",
        "consent_thanks": "ధన్యవాదాలు! మీ డేటా సురక్షితంగా నిల్వ చేయబడుతుంది.",
        "consent_no_save": "సమస్య లేదు! నేను డేటా నిల్వ చేయకుండా మీకు సహాయం చేస్తాను.",
        "main_menu": "మీరు ఏమి చేయాలనుకుంటున్నారు?",
        "ask_name": "దయచేసి రోగి పూర్తి పేరును పంచుకోండి.",
        "welcome_back": "తిరిగి వచ్చినందుకు స్వాగతం, {name}!",
        "ask_symptoms": "మీకు ఏ లక్షణాలు లేదా ఆందోళనలు ఉన్నాయి? ('skip' టైప్ చేసి దాటవేయవచ్చు)",
        "suggesting_department": "మీ ఆందోళన ఆధారంగా, మా *{department}* బృందం సహాయం చేయగలదు.\n\nకారణం: {reason}\n\nఅక్కడ బుక్ చేయాలా?",
        "select_doctor": "దయచేసి ఒక డాక్టర్‌ను ఎంచుకోండి:",
        "select_date": "దయచేసి ఒక తేదీని ఎంచుకోండి (ఈరోజు లేదా తదుపరి 30 రోజుల్లో ఏదైనా).",
        "select_datetime": "దయచేసి మీ అపాయింట్‌మెంట్ కోసం తేదీ మరియు సమయాన్ని ఎంచుకోండి:",
        "select_slot": "దయచేసి ఒక సమయ స్లాట్‌ను ఎంచుకోండి:",
        "confirm_booking": "దయచేసి మీ అపాయింట్‌మెంట్‌ను నిర్ధారించండి:\n\n👤 {name}\n👨‍⚕️ {doctor}\n🏥 {department}\n📅 {date}\n🕐 {time}\n\nఇది సరైనదేనా?",
        "booking_confirmed": "✅ అపాయింట్‌మెంట్ నిర్ధారించబడింది!\n\nరిఫ్: {ref}\n👨‍⚕️ {doctor}\n📅 {date} {time}\n\nదయచేసి 10 నిమిషాల ముందు వరుడండి. రద్దు చేయడానికి CANCEL అని రిప్లై చేయండి.",
        "booking_failed": "క్షమించండి, నేను అపాయింట్‌మెంట్ బుక్ చేయలేకపోయాను. దయచేసి మళ్లీ ప్రయత్నించండి లేదా మాకు {phone} కు కాల్ చేయండి.",
        "slot_taken": "ఆ స్లాట్ ఇప్పుడే మరొకరు బుక్ చేసుకున్నారు. {doctor} కు అందుబాటులో ఉన్న తదుపరి సమయాలు:",
        "session_timeout": "మీ బుకింగ్ సెషన్ ముగిసింది. మళ్లీ ప్రారంభించడానికి ఇక్కడ ప్రధాన మెను ఉంది.",
        "already_booking": "మీరు ఇప్పటికే {doctor} తో అపాయింట్‌మెంట్ బుక్ చేస్తున్నారు. దానిని కొనసాగించండి లేదా కొత్త బుకింగ్ ప్రారంభించండి?",
        "emergency": "🚨 వైద్య అత్యవసర పరిస్థితి! దయచేసి వెంటనే మా అత్యవసర డెస్క్‌కు {emergency} కు కాల్ చేయండి లేదా 108 డయల్ చేయండి.",
        "human_escalation": "మీరు మా సిబ్బందికి కనెక్ట్ అవుతున్నారు. దయచేసి {phone} కు కాల్ చేయండి లేదా కాల్‌బ్యాక్ కోసం వేచి ఉండండి.",
        "opt_out_confirm": "పూర్తయింది — ఇకపై ఫాలో-అప్ లేదా ఆరోగ్య చెక్-ఇన్ సందేశాలు రావు. మీరు బుక్ చేసిన వాటికి అపాయింట్‌మెంట్ రిమైండర్లు, లాబ్ రిపోర్ట్లు, చెల్లింపు సమాచారం వస్తాయి. తిరిగి సబ్‌స్క్రైబ్ చేయడానికి ఎప్పుడైనా మెసేజ్ చేయండి.",
        "data_deleted": "మీ డేటా మా సిస్టమ్‌ల నుండి తొలగించబడింది.",
        "invalid_input": "నేను దానిని అర్థం చేసుకోలేకపోయాను. దయచేసి మళ్లీ ప్రయత్నించండి లేదా 'help' టైప్ చేయండి.",
        "unsupported_media": "నేను ఇప్పుడు టెక్స్ట్ సందేశాలను మాత్రమే చదవగలను — ఫోటోలు, వాయిస్ నోట్స్ లేదా ఫైల్స్ తెరవలేను. దయచేసి మీ సందేశాన్ని టైప్ చేయండి. అత్యవసరమైతే {phone} కి కాల్ చేయండి.",
        "thank_you": "{hospital_name} ఎంచుకున్నందుకు ధన్యవాదాలు. జాగ్రత్త!",
        "change_language": "🌐 భాష మార్చు",
        "available_doctors_in": "{dept}లో అందుబాటులో ఉన్న డాక్టర్లు:",
        "our_doctors_body": "నేరుగా అపాయింట్మెంట్ బుక్ చేయడానికి డాక్టర్ను ఎంచుకోండి:",
        "our_services_body": "{hospital_name}లో మా సేవలు:",
        "health_checkin": "నమస్తే {name}, డాక్టర్ {doctor} వద్ద మీ సందర్శన తర్వాత తనిఖీ చేస్తున్నాము. మీరు ఎలా ఫీల్ అవుతున్నారు?",
        "health_checkin_concern": "అది వినడం బాధగా ఉంది. దయచేసి మాకు {phone} కు కాల్ చేయండి — లక్షణాలు తీవ్రంగా ఉంటే వేచి ఉండకండి.",
        "health_checkin_ok": "వినడం సంతోషంగా ఉంది! జాగ్రత్తగా ఉండండి, మార్పు ఉంటే ఎప్పుడైనా సంప్రదించండి.",
        "queue_status_waiting": "🎫 *లైవ్ ఓపీడీ టోకెన్ స్థితి*\n\nమీ టోకెన్ నంబర్: *#{token}*\nడాక్టర్: *{doctor}*\nప్రస్తుతం చూస్తున్న టోకెన్: *#{current}*\nమీ ముందున్న రోగులు: *{ahead}*\n\nదయచేసి మీ టోకెన్ దగ్గరకు వచ్చినప్పుడు వెయిటింగ్ ఏరియాలో ఉండండి.",
        "queue_your_turn": "🔔 *ఇప్పుడు మీ వంతు వచ్చింది!*\n\nటోకెన్ నంబర్: *#{token}*\nడాక్టర్: *{doctor}*\n\nదయచేసి కన్సల్టేషన్ గదికి వెళ్లండి.",
        "queue_status_not_checked_in": "ఈరోజు మీకు *{doctor}* తో అపాయింట్‌మెంట్ ఉంది, కానీ మీరు ఇంకా చెక్-ఇన్ చేయలేదు.\n\nదయచేసి మీ ఓపీడీ టోకెన్ తీసుకోవడానికి రిసెప్షన్ డెస్క్‌ను సంప్రదించండి.",
        "queue_status_none": "ఈరోజు కోసం మీకు ఎటువంటి అపాయింట్‌మెంట్ షెడ్యూల్ చేయబడలేదు.\n\nఅపాయింట్‌మెంట్ బుక్ చేసుకోవడానికి *book* అని టైప్ చేయండి.",
        "cancellation_policy_note": "ℹ️ రద్దు విధానం: మీ స్లాట్‌కు {hours} గంటల ముందు వరకు ({cutoff} లోపు) పూర్తి వాపసుతో రద్దు అందుబాటులో ఉంది.",
        "cancellation_policy_note_anytime": "ℹ️ రద్దు విధానం: అపాయింట్‌మెంట్ ప్రారంభం కాకముందు ({cutoff} లోపు) ఎప్పుడైనా పూర్తి వాపసుతో రద్దు చేసుకోవచ్చు.",
        "cancellation_cutoff_note": "ℹ️ రద్దు విధానం: దయచేసి మీ స్లాట్‌కు కనీసం {hours} గంటల ముందు ({cutoff} లోపు) రద్దు చేయండి, దానివల్ల ఆ స్లాట్ మరొక రోగికి ఇవ్వగలుగుతాము.",
        "cancellation_cutoff_note_anytime": "ℹ️ రద్దు విధానం: అపాయింట్‌మెంట్ ప్రారంభం కాకముందు ({cutoff} లోపు) మీరు ఎప్పుడైనా రద్దు చేసుకోవచ్చు.",
        "refund_receipt": "✅ *అపాయింట్‌మెంట్ రద్దు — రీఫండ్ ప్రారంభమైంది*\n--------------------------------\nడాక్టర్: {doctor}\nతేదీ: {date}\nరీఫండ్ మొత్తం: ₹{amount}\nరీఫండ్ రిఫరెన్స్: {refund_id}\nచెల్లింపు గేట్వే: Razorpay\nస్థితి: రీఫండ్ మీ అసలైన చెల్లింపు పద్ధతికి (UPI / కార్డ్) తిరిగి పంపబడింది. మొత్తం 2 నుండి 5 పని దినాల్లో జమ అవుతుంది.",
        "refund_late_no_refund": "⚠️ మీ అపాయింట్‌మెంట్ రద్దు చేయబడింది. క్లినిక్ విధానం ప్రకారం, స్లాట్‌కు {hours} గంటలలోపు చేసిన రద్దులకు రీఫండ్ ఉండదు.",
        "refund_failed_manual_review": "⚠️ మీ అపాయింట్‌మెంట్ రద్దు చేయబడింది. రీఫండ్ ఆటోమెటిక్‌గా ప్రారంభం కాలేదు — మా బృందానికి సమాచారం అందింది, వారు దీనిని మాన్యువల్‌గా పూర్తి చేస్తారు. 2 పని దినాల్లో సమాధానం రాకపోతే {phone} నకు కాల్ చేయండి.",
        "refund_late_slot_started": "⚠️ మీ అపాయింట్‌మెంట్ రద్దు చేయబడింది. క్లినిక్ విధానం ప్రకారం, అపాయింట్‌మెంట్ ప్రారంభమైన తర్వాత చేసిన రద్దులకు రీఫండ్ ఉండదు.",
    },
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
        **kwargs,
    }

    return template.format(**format_kwargs)


# ── Cancellation & refund policy lines ──────────────────────────────────────
# The window itself is resolved by app.services.tenant.cancellation_window_hours()
# and passed in, so this module keeps its single `settings` dependency and the
# policy has exactly one source of truth.

#: Asia/Kolkata. Every clinic on this platform quotes local time to patients,
#: so a cutoff rendered in UTC would be 5.5 hours wrong on every message.
_IST = _dt_timezone(timedelta(hours=5, minutes=30))


def cancellation_cutoff(
    appointment_date: str, appointment_time: str, window_hours: int
) -> Optional[datetime]:
    """The last IST moment at which a cancellation is still refundable.

    Returns None when the appointment date/time cannot be parsed — callers
    then omit the policy line rather than print a wrong deadline, which is
    worse than printing none.
    """
    try:
        slot = datetime.strptime(
            f"{appointment_date} {(appointment_time or '')[:5]}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=_IST)
    except (ValueError, TypeError):
        return None
    return slot - timedelta(hours=max(0, int(window_hours or 0)))


def cancellation_policy_line(
    lang: str,
    window_hours: int,
    appointment_date: str,
    appointment_time: str,
    refundable: bool = True,
) -> str:
    """One-line cancellation policy for a booking confirmation.

    `refundable` must be False for a booking no money was taken for. Promising
    "full refund" on an unpaid appointment is a promise the clinic cannot keep,
    and the patient reads it as one.

    Returns "" when the cutoff cannot be computed, so callers can append it
    unconditionally.
    """
    cutoff = cancellation_cutoff(appointment_date, appointment_time, window_hours)
    if cutoff is None:
        return ""

    formatted = cutoff.strftime("%d %b %Y, %I:%M %p")
    family = "cancellation_policy_note" if refundable else "cancellation_cutoff_note"
    key = family if window_hours else f"{family}_anytime"
    return get_message(key, lang, hours=window_hours, cutoff=formatted)
