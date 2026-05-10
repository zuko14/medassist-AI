import os
import re

def fix_scheduler():
    with open('app/services/scheduler.py', 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Add get_clinic_by_id import
    content = content.replace('from app.templates.whatsapp_templates import TEMPLATES', 'from app.templates.whatsapp_templates import TEMPLATES\nfrom app.services.tenant import get_clinic_by_id')

    # Fix send_24h_reminders
    content = content.replace('for appt in appointments.data:\n                try:', 'for appt in appointments.data:\n                try:\n                    clinic = await get_clinic_by_id(appt.get("clinic_id", "default"))')
    content = content.replace('await whatsapp_service.send_template(\n                        appt["patient_phone"],', 'await whatsapp_service.send_template(\n                        clinic,\n                        appt["patient_phone"],')
    
    # Fix send_2h_reminders
    content = content.replace('if appt_time[:5] <= in_2h[:5]:\n                    try:', 'if appt_time[:5] <= in_2h[:5]:\n                    try:\n                        clinic = await get_clinic_by_id(appt.get("clinic_id", "default"))')
    
    # Fix send_followups
    content = content.replace('for appt in appointments.data:\n                try:\n                    components', 'for appt in appointments.data:\n                try:\n                    clinic = await get_clinic_by_id(appt.get("clinic_id", "default"))\n                    components')
    
    # Fix check_doctor_leaves
    content = content.replace('for appt in affected.data:\n                    try:\n                        # Cancel appointment', 'for appt in affected.data:\n                    try:\n                        clinic = await get_clinic_by_id(appt.get("clinic_id", "default"))\n                        # Cancel appointment')
    
    with open('app/services/scheduler.py', 'w', encoding='utf-8') as f:
        f.write(content)

def fix_lab_reports():
    with open('app/services/lab_reports.py', 'r', encoding='utf-8') as f:
        content = f.read()
    
    content = content.replace('from app.services.whatsapp import whatsapp_service', 'from app.services.whatsapp import whatsapp_service\nfrom app.services.tenant import get_clinic_by_id')
    
    # upload_and_send doesn't have clinic right now!
    content = content.replace('async def upload_and_send(\n        self,', 'async def upload_and_send(\n        self,\n        clinic_id: str,')
    content = content.replace('            "patient_phone": patient_phone,', '            "clinic_id": clinic_id,\n            "patient_phone": patient_phone,')
    content = content.replace('await self._send_report_notification(result.data[0])', 'clinic = await get_clinic_by_id(clinic_id)\n            await self._send_report_notification(clinic, result.data[0])')
    
    # resend_report
    content = content.replace('await self._send_report_notification(report)', 'clinic = await get_clinic_by_id(report.get("clinic_id", "default"))\n            await self._send_report_notification(clinic, report)')
    
    # _send_report_notification
    content = content.replace('async def _send_report_notification(self, report_data: dict) -> bool:', 'async def _send_report_notification(self, clinic: dict, report_data: dict) -> bool:')
    content = content.replace('await whatsapp_service.send_template(\n            report_data["patient_phone"],', 'await whatsapp_service.send_template(\n            clinic,\n            report_data["patient_phone"],')
    
    # get_all_reports
    content = content.replace('async def get_all_reports(self) -> list:', 'async def get_all_reports(self, clinic_id: str = "default") -> list:')
    content = content.replace('query = supabase.table("lab_reports").select("*")', 'query = supabase.table("lab_reports").select("*").eq("clinic_id", clinic_id)')
    
    with open('app/services/lab_reports.py', 'w', encoding='utf-8') as f:
        f.write(content)

def fix_prescriptions():
    with open('app/services/prescriptions.py', 'r', encoding='utf-8') as f:
        content = f.read()

    content = content.replace('from app.services.whatsapp import whatsapp_service', 'from app.services.whatsapp import whatsapp_service\nfrom app.services.tenant import get_clinic_by_id')

    # add_prescription
    content = content.replace('async def add_prescription(\n        self,', 'async def add_prescription(\n        self,\n        clinic_id: str,')
    content = content.replace('            "patient_phone": patient_phone,', '            "clinic_id": clinic_id,\n            "patient_phone": patient_phone,')
    content = content.replace('await self._send_prescription_notification(result.data[0])', 'clinic = await get_clinic_by_id(clinic_id)\n            await self._send_prescription_notification(clinic, result.data[0])')

    # _send_prescription_notification
    content = content.replace('async def _send_prescription_notification(self, rx_data: dict) -> bool:', 'async def _send_prescription_notification(self, clinic: dict, rx_data: dict) -> bool:')
    content = content.replace('await whatsapp_service.send_template(\n            rx_data["patient_phone"],', 'await whatsapp_service.send_template(\n            clinic,\n            rx_data["patient_phone"],')
    
    # send_due_reminders
    content = content.replace('for rx in active_rx.data:\n            try:\n                times = rx["reminder_times"]', 'for rx in active_rx.data:\n            try:\n                times = rx["reminder_times"]\n                clinic = await get_clinic_by_id(rx.get("clinic_id", "default"))')
    content = content.replace('await whatsapp_service.send_template(\n                            rx["patient_phone"],', 'await whatsapp_service.send_template(\n                            clinic,\n                            rx["patient_phone"],')
    
    # get_all_prescriptions
    content = content.replace('async def get_all_prescriptions(self, active_only: bool = False) -> list:', 'async def get_all_prescriptions(self, clinic_id: str = "default", active_only: bool = False) -> list:')
    content = content.replace('query = supabase.table("prescriptions").select("*")', 'query = supabase.table("prescriptions").select("*").eq("clinic_id", clinic_id)')
    
    # deactivate_prescription
    content = content.replace('async def deactivate_prescription(self, rx_id: str) -> bool:', 'async def deactivate_prescription(self, clinic_id: str, rx_id: str) -> bool:')
    content = content.replace('.eq("id", rx_id).execute()', '.eq("clinic_id", clinic_id).eq("id", rx_id).execute()')

    with open('app/services/prescriptions.py', 'w', encoding='utf-8') as f:
        f.write(content)


def run():
    if os.path.exists('app/services/scheduler.py'): fix_scheduler()
    if os.path.exists('app/services/lab_reports.py'): fix_lab_reports()
    if os.path.exists('app/services/prescriptions.py'): fix_prescriptions()
    print("Fixed scheduler, lab_reports, prescriptions")

if __name__ == "__main__":
    run()
