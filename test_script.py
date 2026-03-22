import asyncio
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.abspath("."))
from app.services.conversation import conversation_manager
from app.services.whatsapp import whatsapp_service
from app.database import supabase

# Mock WhatsApp sends
async def mock_send_text(phone: str, message: str) -> bool:
    print(f"\nBOT TEXT: {message}")
    return True

async def mock_send_interactive_buttons(phone: str, body: str, buttons: list, header: str = None) -> bool:
    hdr = f"[{header}]\n" if header else ""
    print(f"\nBOT BUTTONS: {hdr}{body} | {[b['title'] for b in buttons]}")
    return True

async def mock_send_interactive_list(phone: str, body: str, button_text: str, sections: list, header: str = None) -> bool:
    hdr = f"[{header}]\n" if header else ""
    print(f"\nBOT LIST: {hdr}{body} | btn: {button_text} | sections: {[row['title'] for row in sections[0]['rows']]}")
    return True

whatsapp_service.send_text = mock_send_text
whatsapp_service.send_interactive_buttons = mock_send_interactive_buttons
whatsapp_service.send_interactive_list = mock_send_interactive_list

PHONE = "+917981945956"

en_flow = [
    ("Hi", "text", None, None),
    ("English", "interactive", None, {"id": "lang_en"}),
    ("Yes", "interactive", None, {"id": "consent_yes"}),
    ("Book Appointment", "interactive", None, {"id": "menu_book"}),
    ("John Smith", "text", None, None),
    ("Fever", "text", None, None),
    ("Yes", "interactive", None, {"id": "suggest_yes"}),
    ("doc_idx", "interactive", None, {"id": "doc_none"}), # dynamically set below
    ("today", "interactive", None, {"id": "date_2026-03-30"}),
    ("time", "interactive", None, {"id": "slot_09:00"}),
    ("Confirm", "interactive", None, {"id": "confirm_yes"}),
]

hi_flow = [
    ("Hi", "text", None, None),
    ("हिंदी", "interactive", None, {"id": "lang_hi"}),
    ("हाँ", "interactive", None, {"id": "consent_yes"}),
    ("Book Appointment", "interactive", None, {"id": "menu_book"}),
    ("राज शर्मा", "text", None, None),
    ("बुखार", "text", None, None),
    ("हाँ", "interactive", None, {"id": "suggest_yes"}),
    ("doc_idx", "interactive", None, {"id": "doc_none"}),
    ("today", "interactive", None, {"id": "date_2026-03-30"}),
    ("time", "interactive", None, {"id": "slot_09:30"}),
    ("Confirm", "interactive", None, {"id": "confirm_yes"}),
]

te_flow = [
    ("Hi", "text", None, None),
    ("తెలుగు", "interactive", None, {"id": "lang_te"}),
    ("అవును", "interactive", None, {"id": "consent_yes"}),
    ("Book Appointment", "interactive", None, {"id": "menu_book"}),
    ("Chaitanya Kumar", "text", None, None),
    ("జ్వరం", "text", None, None),
    ("అవును", "interactive", None, {"id": "suggest_yes"}),
    ("doc_idx", "interactive", None, {"id": "doc_none"}),
    ("today", "interactive", None, {"id": "date_2026-03-30"}),
    ("time", "interactive", None, {"id": "slot_10:00"}),
    ("నిర్ధారించు", "interactive", None, {"id": "confirm_yes"}),
]

async def reset_db():
    supabase.table("conversations").delete().eq("phone", PHONE).execute()
    supabase.table("patients").delete().eq("phone", PHONE).execute()
    print("DB Reset for test")

async def run_flow(name, messages):
    print(f"\n{'='*50}\nSTARTING FLOW: {name}\n{'='*50}")
    await reset_db()
    
    for idx, (text, msg_type, msg_id, interactive) in enumerate(messages):
        # Dynamically inject doctor ID for General Medicine
        if text == "doc_idx":
            # fetch a doctor from DB mapped to General Medicine
            docs = supabase.table("doctors").select("id").eq("department", "General Medicine").execute()
            if docs.data:
                doc_id = docs.data[0]["id"]
                interactive["id"] = f"doc_{doc_id}"
                text = "Doctor Selected"
        
        print(f"\n--- USER: {text} ---")
        try:
            await conversation_manager.handle_message(
                phone=PHONE,
                message=text,
                message_type=msg_type,
                message_id=msg_id,
                interactive_data=interactive
            )
        except Exception as e:
            print(f"Error handling '{text}': {e}")
            import traceback
            traceback.print_exc()
            
    print(f"\nFLOW {name} FINISHED.")

async def main():
    await run_flow("ENGLISH", en_flow)
    await run_flow("HINDI", hi_flow)
    await run_flow("TELUGU", te_flow)

if __name__ == "__main__":
    asyncio.run(main())
