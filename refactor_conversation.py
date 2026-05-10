import re

def refactor_conversation():
    with open('app/services/conversation.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Update def handle_message
    content = content.replace(
        'async def handle_message(\n        self,\n        phone: str,',
        'async def handle_message(\n        self,\n        clinic: dict,\n        phone: str,'
    )

    # 2. Add clinic_id extraction
    content = content.replace(
        '        # Guard 1: Duplicate webhook delivery\n        session = await get_or_create_conversation(phone)',
        '        clinic_id = clinic["id"]\n        # Guard 1: Duplicate webhook delivery\n        session = await get_or_create_conversation(clinic_id, phone)'
    )

    # Replace update_conversation, get_patient_by_phone, create_patient calls inside handle_message
    # Actually, it's easier to regex all database calls
    
    db_methods = [
        'get_or_create_conversation',
        'update_conversation',
        'get_patient_by_phone',
        'create_patient',
        'update_patient',
        'get_conversation',
        'get_doctors',
        'get_doctor_by_name',
        'get_available_slots',
        'find_next_available_date',
        'book_appointment',
        'cancel_appointment',
        'get_patient_appointments',
        'log_analytics_event',
        'delete_patient_data'
    ]

    for method in db_methods:
        # Replace method(phone...) with method(clinic["id"], phone...)
        content = re.sub(rf'\b{method}\(phone', f'{method}(clinic["id"], phone', content)
        content = re.sub(rf'\b{method}\(appointment_id\)', f'{method}(clinic["id"], appointment_id)', content)
        content = re.sub(rf'\b{method}\(\)', f'{method}(clinic["id"])', content)
        content = re.sub(rf'\b{method}\(\n', f'{method}(clinic["id"],\n', content)

    # Fix specific database calls that don't start with 'phone'
    content = re.sub(r'get_doctors\((.*?)\)', r'get_doctors(clinic["id"], \1)', content)
    content = content.replace('get_doctors(clinic["id"], clinic["id"],', 'get_doctors(clinic["id"],') # fix double
    
    content = re.sub(r'get_doctor_by_name\((.*?)\)', r'get_doctor_by_name(clinic["id"], \1)', content)
    content = content.replace('get_doctor_by_name(clinic["id"], clinic["id"],', 'get_doctor_by_name(clinic["id"],') # fix double

    content = re.sub(r'get_available_slots\((.*?)\)', r'get_available_slots(clinic["id"], \1)', content)
    content = content.replace('get_available_slots(clinic["id"], clinic["id"],', 'get_available_slots(clinic["id"],') # fix double

    content = re.sub(r'find_next_available_date\((.*?)\)', r'find_next_available_date(clinic["id"], \1)', content)
    content = content.replace('find_next_available_date(clinic["id"], clinic["id"],', 'find_next_available_date(clinic["id"],') # fix double
    
    content = re.sub(r'book_appointment\((.*?)\)', r'book_appointment(clinic["id"], \1)', content)
    content = content.replace('book_appointment(clinic["id"], clinic["id"],', 'book_appointment(clinic["id"],') # fix double

    # Replace self.whatsapp.send_* calls
    wa_methods = [
        'send_text',
        'send_template',
        'send_interactive_buttons',
        'send_interactive_list',
        'send_location',
        'mark_as_read'
    ]
    for method in wa_methods:
        content = re.sub(rf'\bself\.whatsapp\.{method}\(phone', f'self.whatsapp.{method}(clinic, phone', content)
        content = re.sub(rf'\bself\.whatsapp\.{method}\(message_id\)', f'self.whatsapp.{method}(clinic, message_id)', content)

    # Now all internal methods: _process_state, _handle_idle, _send_main_menu, etc.
    # Needs to pass `clinic` around.
    methods_to_update = re.findall(r'def (_[a-zA-Z0-9_]+)\(\s*self,\s*phone: str', content)
    methods_to_update.append('update_state')
    methods_to_update.append('get_patient_language')
    methods_to_update = list(set(methods_to_update))

    for method in methods_to_update:
        # Update method definition
        content = re.sub(rf'def {method}\(\s*self,\s*phone: str', f'def {method}(self, clinic: dict, phone: str', content)
        
        # Update method calls
        content = re.sub(rf'\bself\.{method}\(phone', f'self.{method}(clinic, phone', content)

    # Fix get_lang
    content = content.replace('async def get_lang(phone: str) -> str:', 'async def get_lang(clinic: dict, phone: str) -> str:')
    content = content.replace('get_lang(phone)', 'get_lang(clinic, phone)')
    content = content.replace('.eq("phone", phone)', '.eq("clinic_id", clinic["id"]).eq("phone", phone)')

    # Fix get_session typo
    content = content.replace('get_session(clinic["id"], phone)', 'get_conversation(clinic["id"], phone)')
    content = content.replace('from app.database import get_session', 'from app.database import get_conversation')

    # Fix Settings
    content = content.replace('settings.hospital_phone', 'clinic["whatsapp_number"]')
    content = content.replace('settings.hospital_name', 'clinic["name"]')

    with open('app/services/conversation.py', 'w', encoding='utf-8') as f:
        f.write(content)

    print("Refactor completed")

if __name__ == "__main__":
    refactor_conversation()
