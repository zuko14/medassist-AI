import sys
import os

# Ensure app is in path
sys.path.append(os.path.dirname(__file__))

from app.database import supabase

def update_fees():
    updates = [
        ("Dr. Priya Sharma", 300),
        ("Dr. Arjun Reddy", 800),
        ("Dr. Meena Patel", 500),
        ("Dr. Suresh Kumar", 700),
        ("Dr. Anita Singh", 600),
        ("Dr. Ravi Nair", 400),
    ]
    for doc, fee in updates:
        supabase.table("doctors").update({"consultation_fee": fee}).eq("name", doc).execute()
        print(f"Updated {doc} to {fee}")

def reset_test_data():
    phone = "+917981945956"
    
    # Needs to delete appointments first because of foreign key maybe?
    # Better yet, just delete by phone
    try:
        supabase.table("appointments").delete().eq("patient_phone", phone).execute()
        print(f"Deleted appointments for {phone}")
    except Exception as e:
        print(f"Failed to delete appointments: {e}")
        
    try:
        supabase.table("conversations").delete().eq("phone", phone).execute()
        print(f"Deleted conversations for {phone}")
    except Exception as e:
        print(f"Failed to delete conversations: {e}")
        
    try:
        supabase.table("patients").delete().eq("phone", phone).execute()
        print(f"Deleted patients for {phone}")
    except Exception as e:
        print(f"Failed to delete patients: {e}")

if __name__ == "__main__":
    update_fees()
    reset_test_data()
