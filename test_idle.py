import asyncio
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.abspath("."))
from app.services.conversation import conversation_manager
from app.services.whatsapp import whatsapp_service
from app.database import supabase

async def mock_send_text(phone: str, message: str) -> bool:
    print(f"\nBOT TEXT: {message}")
    return True

async def mock_send_interactive_buttons(phone: str, body: str, buttons: list, header: str = None) -> bool:
    hdr = f"[{header}]\n" if header else ""
    print(f"\nBOT BUTTONS: {hdr}{body} | {[b['title'] for b in buttons]}")
    return True

whatsapp_service.send_text = mock_send_text
whatsapp_service.send_interactive_buttons = mock_send_interactive_buttons

PHONE = "+910000000002"

async def reset_db():
    supabase.table("conversations").delete().eq("phone", PHONE).execute()
    supabase.table("patients").delete().eq("phone", PHONE).execute()
    print("DB Reset for test")

async def run_flow():
    print("================== STARTING IDLE TEST ==================")
    await reset_db()
    
    messages = [
        ("Hi", "text", None, None), # Should show language picker
        ("Hi", "text", None, None), # Second Hi should STILL show language picker, NOT hindi!
    ]
    
    for idx, (text, msg_type, msg_id, interactive) in enumerate(messages):
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
            
    print("\nFLOW IDLE FINISHED.")

if __name__ == "__main__":
    asyncio.run(run_flow())
