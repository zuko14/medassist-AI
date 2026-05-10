import re

def fix_whatsapp_calls():
    with open('app/services/conversation.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # Find self.whatsapp.send_*(phone, 
    # Or self.whatsapp.send_*(\n    phone, 
    # Or self.whatsapp.mark_as_read(message_id)

    # We can just look for self.whatsapp.send_TEXT or send_something
    methods = [
        'send_text', 'send_template', 'send_interactive_buttons', 'send_interactive_list', 'send_location'
    ]

    for m in methods:
        # Match self.whatsapp.send_*(<spaces>phone
        content = re.sub(rf'(self\.whatsapp\.{m}\(\s*)phone', r'\1clinic, phone', content)

    # mark_as_read
    content = re.sub(r'(self\.whatsapp\.mark_as_read\(\s*)message_id', r'\1clinic, message_id', content)

    # Also fix any other self.whatsapp.send_*(... phone ...)?
    # Wait, the previous script changed `self.whatsapp.send_text(phone` to `self.whatsapp.send_text(clinic, phone`
    # That might result in `self.whatsapp.send_text(clinic, clinic, phone` if I run it again.
    # Let's clean up `clinic, clinic, phone` just in case
    content = content.replace('(clinic, clinic, phone', '(clinic, phone')
    content = content.replace('(clinic, clinic, message_id', '(clinic, message_id')

    # Fix get_session typo if still there
    content = content.replace('get_session(clinic["id"], phone)', 'get_conversation(clinic["id"], phone)')

    # Check for send_interactive_buttons
    content = content.replace('self.whatsapp.send_interactive_buttons(clinic, phone,', 'self.whatsapp.send_interactive_buttons(clinic, phone,')

    with open('app/services/conversation.py', 'w', encoding='utf-8') as f:
        f.write(content)
        
    print("Fixed whatsapp calls in conversation.py")

if __name__ == "__main__":
    fix_whatsapp_calls()
