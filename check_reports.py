"""Quick diagnostic: check what's in the lab_reports table."""
import sys
from app.database import supabase


def mask_phone(phone: str) -> str:
    """Mask a phone number, showing only the last 4 digits."""
    if not phone or len(phone) <= 4:
        return "****"
    return "*" * (len(phone) - 4) + phone[-4:]


# Get ALL records (no raw PII printed)
all_reports = supabase.table("lab_reports").select(
    "id, patient_phone, report_name, status, ai_summary, has_abnormal_values, uploaded_at"
).order("uploaded_at", desc=True).execute()

print(f"\n=== ALL LAB REPORTS ({len(all_reports.data)}) ===")
for r in all_reports.data:
    print(
        f"  Phone: {mask_phone(r['patient_phone'])} | "
        f"Report: {r['report_name']} | Status: {r['status']} | "
        f"AI: {'YES' if r.get('ai_summary') else 'NO'} | "
        f"Abnormal: {r.get('has_abnormal_values')} | "
        f"Uploaded: {r.get('uploaded_at')}"
    )

# Check specific phone — must be passed as a CLI argument
if len(sys.argv) < 2:
    print("\nUsage: python check_reports.py <phone_number>")
    print("  Skipping per-patient lookup (no phone number provided).")
    sys.exit(0)

phone = sys.argv[1]

print(f"\n=== REPORTS FOR {mask_phone(phone)} (status=sent) ===")
sent = supabase.table("lab_reports").select("id, report_name, status").eq(
    "patient_phone", phone
).eq("status", "sent").execute()
print(f"  Found: {len(sent.data)} records")
for r in sent.data:
    print(f"  ID: {r['id']} | Report: {r['report_name']} | Status: {r['status']}")

print(f"\n=== REPORTS FOR {mask_phone(phone)} (any status) ===")
any_status = supabase.table("lab_reports").select("id, report_name, status").eq(
    "patient_phone", phone
).execute()
print(f"  Found: {len(any_status.data)} records")
for r in any_status.data:
    print(f"  ID: {r['id']} | Report: {r['report_name']} | Status: {r['status']}")
