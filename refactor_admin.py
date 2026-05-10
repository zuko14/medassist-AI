import re

def fix_admin():
    with open('app/routers/admin.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # lab-reports/upload
    content = content.replace('report_type: str = Form("General"),', 'report_type: str = Form("General"),\n    clinic_id: str = Form("default"),')
    content = content.replace('file_bytes=file_bytes,\n            filename=file.filename,', 'clinic_id=clinic_id,\n            file_bytes=file_bytes,\n            filename=file.filename,')

    # lab-reports get
    content = content.replace('async def get_lab_reports(user: str = Depends(verify_credentials)):', 'async def get_lab_reports(clinic_id: str = "default", user: str = Depends(verify_credentials)):')
    content = content.replace('result = await LabReportService().get_all_reports()', 'result = await LabReportService().get_all_reports(clinic_id)')

    # prescriptions/add
    content = content.replace('body: dict,\n    user: str = Depends(verify_credentials),', 'body: dict,\n    clinic_id: str = "default",\n    user: str = Depends(verify_credentials),')
    content = content.replace('result = await PrescriptionService().add_prescription(\n            patient_phone=body["patient_phone"],', 'clinic_id = body.get("clinic_id", clinic_id)\n        result = await PrescriptionService().add_prescription(\n            clinic_id=clinic_id,\n            patient_phone=body["patient_phone"],')

    # prescriptions get
    content = content.replace('active_only: bool = False,\n    user: str = Depends(verify_credentials),', 'active_only: bool = False,\n    clinic_id: str = "default",\n    user: str = Depends(verify_credentials),')
    content = content.replace('result = await PrescriptionService().get_all_prescriptions(active_only)', 'result = await PrescriptionService().get_all_prescriptions(clinic_id, active_only)')

    # prescriptions deactivate
    content = content.replace('prescription_id: str,\n    user: str = Depends(verify_credentials),', 'prescription_id: str,\n    clinic_id: str = "default",\n    user: str = Depends(verify_credentials),')
    content = content.replace('await PrescriptionService().deactivate_prescription(prescription_id)', 'await PrescriptionService().deactivate_prescription(clinic_id, prescription_id)')

    with open('app/routers/admin.py', 'w', encoding='utf-8') as f:
        f.write(content)
        
    print("Fixed admin.py")

if __name__ == "__main__":
    fix_admin()
