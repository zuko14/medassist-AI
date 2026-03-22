import re
import codecs

with codecs.open(r'c:\Users\chait\OneDrive\Desktop\hospital-bot\app\services\conversation.py', 'r', 'utf-8') as f:
    text = f.read()

# Fix Bug 3: SKIP_KEYWORDS
old_skip = r'''        # Allow skip
        if message.lower() in ["skip", "no symptoms", "don't know", "none", "नहीं", "తెలియదు"]:'''
new_skip = r'''        SKIP_KEYWORDS = [
            "no", "no no", "none", "nothing", "skip", "nope",
            "no symptoms", "dont know", "don't know", "not sure", "idk",
            "నహీ", "నో", "లేదు", "ఏమీ లేదు", "తెలియదు", "వదిలేయి",
            "नहीं", "कुछ नहीं", "पता नहीं", "छोड़ो"
        ]

        # Allow skip
        if message.lower().strip() in SKIP_KEYWORDS:'''
text = text.replace(old_skip, new_skip)

# Fix Bug 2: None from map_symptom_to_department
old_symptom = r'''        # Store suggestion in context
        context["suggested_department"] = symptom_result["suggested_department"]'''
new_symptom = r'''        if symptom_result is None:
            await self.whatsapp.send_text(
                phone,
                "Could you describe your symptoms? Example: fever, chest pain, tooth pain, back pain"
            )
            return

        # Store suggestion in context
        context["suggested_department"] = symptom_result["suggested_department"]'''
text = text.replace(old_symptom, new_symptom)

# Fix Bug 5: Data consent (idle)
old_consent_idle = r'''        # Check if data consent is collected
        if patient.get("data_consent") is None:
            await self.whatsapp.send_text(phone, get_message("consent_request", lang))
            await update_conversation(phone, {"state": "awaiting_consent"})
            return'''
new_consent_idle = r'''        # Check if data consent is collected
        if patient.get("data_consent") is None:
            consent_msgs = {
                "en": "To book appointments, I need to save your name and contact details as per our privacy policy. Reply YES to continue or NO to proceed without saving your data.",
                "hi": "अपॉइंटमेंट बुक करने के लिए, मुझे आपका नाम और संपर्क विवरण सहेजना होगा। जारी रखने के लिए YES या बिना डेटा सहेजे NO टाइप करें।",
                "te": "అపాయింట్మెంట్ బుక్ చేయడానికి, మీ పేరు మరియు సంప్రదింపు వివరాలు సేవ్ చేయాలి. కొనసాగించడానికి YES లేదా NO అని టైప్ చేయండి."
            }
            await self.whatsapp.send_text(phone, consent_msgs.get(lang, consent_msgs["en"]))
            await self.update_state(phone, "awaiting_consent")
            return'''
text = text.replace(old_consent_idle, new_consent_idle)

# Fix Bug 5: Data consent (selecting language)
old_consent_lang = r'''        # Check data consent - proceed to consent, NOT language picker again
        if patient.get("data_consent") is None:
            await self.whatsapp.send_text(phone, get_message("consent_request", selected))
            await update_conversation(phone, {"state": "awaiting_consent"})
        else:'''
new_consent_lang = r'''        # Check data consent - proceed to consent, NOT language picker again
        if patient.get("data_consent") is None:
            consent_msgs = {
                "en": "To book appointments, I need to save your name and contact details as per our privacy policy. Reply YES to continue or NO to proceed without saving your data.",
                "hi": "अपॉइंटमेंट बुक करने के लिए, मुझे आपका नाम और संपर्क विवरण सहेजना होगा। जारी रखने के लिए YES या बिना डेटा सहेजे NO टाइप करें।",
                "te": "అపాయింట్మెంట్ బుక్ చేయడానికి, మీ పేరు మరియు సంప్రదింపు వివరాలు సేవ్ చేయాలి. కొనసాగించడానికి YES లేదా NO అని టైప్ చేయండి."
            }
            await self.whatsapp.send_text(phone, consent_msgs.get(selected, consent_msgs["en"]))
            await self.update_state(phone, "awaiting_consent")
            return
        else:'''
text = text.replace(old_consent_lang, new_consent_lang)

# Replace all simple state changes to use update_state (Bug 10)
text = re.sub(
    r'await update_conversation\(phone,\s*\{"state":\s*("[^"]+")\}\)',
    r'await self.update_state(phone, \1)',
    text
)
# Bug 10 context updates
text = re.sub(
    r'await update_conversation\(phone,\s*\{"state":\s*("[^"]+"),\s*"context":\s*(context|merged_context)\}\)',
    r'await self.update_state(phone, \1, \2)',
    text
)
text = re.sub(
    r'await update_conversation\(phone,\s*\{"state":\s*("[^"]+"),\s*"context":\s*\{\}\}\)',
    r'await self.update_state(phone, \1, {})',
    text
)
# For multiline {"state": ..., "context": ...}
text = re.sub(
    r'await update_conversation\(phone, \{\s*"state":\s*("[^"]+"),\s*"context":\s*(context|merged_context|\{\})\s*\}\)',
    r'await self.update_state(phone, \1, \2)',
    text
)

# Bug 6: Junk messages after booking
old_booking_confirm = r'''                await log_analytics_event(phone, "appointment_booked", department=context.get("department"))

                # Reset to main menu
                await update_conversation(phone, {"state": "main_menu", "context": {}})
                await self._send_main_menu(phone, lang)'''

new_booking_confirm = r'''                await log_analytics_event(phone, "appointment_booked", department=context.get("department"))

                import asyncio
                await asyncio.sleep(2)
                instructions = {
                    "en": f"Please arrive 10 minutes early for your {context.get('department')} appointment.",
                    "hi": f"कृपया अपने {context.get('department')} अपॉइंटमेंट से 10 मिनट पहले पहुंचें।",
                    "te": f"దయచేసి మీ {context.get('department')} అపాయింట్‌మెంట్‌కు 10 నిమిషాల ముందు చేరుకోండి."
                }.get(lang, f"Please arrive 10 minutes early for your {context.get('department')} appointment.")
                await self.whatsapp.send_text(phone, instructions)

                await self.whatsapp.send_interactive_buttons(
                    phone,
                    body="What would you like to do?",
                    buttons=[
                        {"id": "menu_book", "title": "📅 Book Another"},
                        {"id": "main_menu", "title": "🏠 Main Menu"}
                    ]
                )
                await self.update_state(phone, "main_menu", {})'''
text = text.replace(old_booking_confirm, new_booking_confirm)
text = text.replace('await self.update_state(phone, "main_menu", {})', 'await self.update_state(phone, "main_menu")')


# Bug 12: Appointment summary before confirmation
old_show_booking = r'''    async def _show_booking_confirmation(self, phone: str, context: dict, lang: str) -> None:
        """Show booking confirmation summary."""
        from datetime import datetime

        date_display = datetime.strptime(context["appointment_date"], "%Y-%m-%d").strftime("%d %b %Y")

        await self.whatsapp.send_interactive_buttons(
            phone,
            body=get_message(
                "confirm_booking",
                lang,
                name=context.get("booking_name", "Patient"),
                doctor=context["doctor_name"],
                department=context.get("department", ""),
                date=date_display,
                time=context["appointment_time"]
            ),
            buttons=[
                {"id": "confirm_yes", "title": "Confirm" if lang == "en" else ("पुष्टि" if lang == "hi" else "నిర్ధారించు")},
                {"id": "confirm_no", "title": "Edit" if lang == "en" else ("संपादन" if lang == "hi" else "మార్చు")}
            ]
        )

        await self.update_state(phone, "confirming_booking", context)'''

new_show_booking = r'''    async def _show_booking_confirmation(self, phone: str, context: dict, lang: str) -> None:
        """Show booking confirmation summary."""
        from datetime import datetime

        date_display = datetime.strptime(context["appointment_date"], "%Y-%m-%d").strftime("%d %b %Y")
        day_name_en = datetime.strptime(context["appointment_date"], "%Y-%m-%d").strftime("%A")
        fee = context.get("doctor", {}).get("consultation_fee", 0)

        summary_body_en = f"""📋 Please confirm your appointment:

👤 Patient: {context.get("booking_name", "Patient")}
👨⚕️ Doctor: {context.get("doctor_name")}
🏥 Department: {context.get("department", "")}
📅 Date: {date_display} ({day_name_en})
⏰ Time: {context.get("appointment_time")}
💰 Fee: ₹{fee}
📍 MediAssist Hospital

Is this correct?"""

        summary_body_hi = f"""📋 कृपया अपने अपॉइंटमेंट की पुष्टि करें:

👤 मरीज: {context.get("booking_name", "Patient")}
👨⚕️ डॉक्टर: {context.get("doctor_name")}
🏥 विभाग: {context.get("department", "")}
📅 तारीख: {date_display} ({day_name_en})
⏰ समय: {context.get("appointment_time")}
💰 फीस: ₹{fee}
📍 MediAssist Hospital

क्या यह सही है?"""

        summary_body_te = f"""📋 దయచేసి మీ అపాయింట్‌మెంట్‌ని నిర్ధారించండి:

👤 రోగి: {context.get("booking_name", "Patient")}
👨⚕️ డాక్టర్: {context.get("doctor_name")}
🏥 విభాగం: {context.get("department", "")}
📅 తేదీ: {date_display} ({day_name_en})
⏰ సమయం: {context.get("appointment_time")}
💰 ఫీజు: ₹{fee}
📍 MediAssist Hospital

ఇది సరైనదేనా?"""

        summary_bodies = {
            "en": summary_body_en,
            "hi": summary_body_hi,
            "te": summary_body_te
        }

        await self.whatsapp.send_interactive_buttons(
            phone,
            body=summary_bodies.get(lang, summary_body_en),
            buttons=[
                {"id": "confirm_yes", "title": "✅ Confirm" if lang == "en" else ("✅ पुष्टि" if lang == "hi" else "✅ నిర్ధారించు")},
                {"id": "confirm_no", "title": "✏️ Edit" if lang == "en" else ("✏️ संपादन" if lang == "hi" else "✏️ మార్చు")}
            ]
        )

        await self.update_state(phone, "confirming_booking", context)'''
text = text.replace(old_show_booking, new_show_booking)

# Edit logic in confirming booking
old_edit_booking = r'''        else:
            # Edit booking - go back to doctor selection
            await self._show_doctor_list(phone, context.get("department", "General Medicine"), context, lang)'''

new_edit_booking = r'''        elif intent == "edit_booking" or intent == "reject_suggestion":
            await self.whatsapp.send_interactive_buttons(
                phone,
                body="What would you like to change?" if lang == "en" else ("आप क्या बदलना चाहेंगे?" if lang == "hi" else "మీరు ఏమి మార్చాలనుకుంటున్నారు?"),
                buttons=[
                    {"id": "edit_doctor", "title": "👨⚕️ Doctor" if lang == "en" else ("👨⚕️ डॉक्टर" if lang == "hi" else "👨⚕️ డాక్టర్")},
                    {"id": "edit_date", "title": "📅 Date" if lang == "en" else ("📅 तारीख" if lang == "hi" else "📅 తేదీ")},
                    {"id": "edit_time", "title": "⏰ Time" if lang == "en" else ("⏰ समय" if lang == "hi" else "⏰ సమయం")}
                ]
            )
        elif intent == "edit_doctor":
            await self._show_doctor_list(phone, context.get("department", "General Medicine"), context, lang)
        elif intent == "edit_date":
            await self._show_date_picker(phone, context, lang)
            await self.update_state(phone, "selecting_date", context)
        elif intent == "edit_time":
            slots, _ = await get_available_slots(context.get("doctor_name"), context.get("appointment_date"))
            if slots:
                await self._show_slot_list(phone, slots, context, lang)
            else:
                await self._suggest_other_doctors(phone, context, lang)'''
text = text.replace(old_edit_booking, new_edit_booking)

with codecs.open(r'c:\Users\chait\OneDrive\Desktop\hospital-bot\app\services\conversation.py', 'w', 'utf-8') as f:
    f.write(text)
