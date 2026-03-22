import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from app.database import supabase

doctors_data = [
    {
        "name": "Dr. Priya Sharma", "specialization": "General Physician", "department": "General Medicine",
        "experience_years": 8, "qualifications": "MBBS, MD General Medicine",
        "rating": 4.7, "consultation_fee": 300, "is_active": True,
        "available_days": "Mon,Tue,Wed,Thu,Fri,Sat",
        "morning_slots": ["09:00","09:30","10:00","10:30","11:00","11:30"],
        "evening_slots": ["17:00","17:30","18:00","18:30"]
    },
    {
        "name": "Dr. Arjun Reddy", "specialization": "Cardiologist", "department": "Cardiology",
        "experience_years": 14, "qualifications": "MBBS, MD, DM Cardiology",
        "rating": 4.8, "consultation_fee": 800, "is_active": True,
        "available_days": "Mon,Tue,Wed,Thu,Fri",
        "morning_slots": ["09:00","09:30","10:00","10:30","11:00"],
        "evening_slots": ["17:00","17:30","18:00"]
    },
    {
        "name": "Dr. Meena Patel", "specialization": "Dentist", "department": "Dental",
        "experience_years": 6, "qualifications": "BDS, MDS Oral Surgery",
        "rating": 4.6, "consultation_fee": 500, "is_active": True,
        "available_days": "Mon,Tue,Wed,Thu,Fri,Sat",
        "morning_slots": ["09:00","09:30","10:00","10:30","11:00","11:30"],
        "evening_slots": ["17:00","17:30","18:00","18:30"]
    },
    {
        "name": "Dr. Suresh Kumar", "specialization": "Orthopedic Surgeon", "department": "Orthopedics",
        "experience_years": 12, "qualifications": "MBBS, MS Orthopedics",
        "rating": 4.7, "consultation_fee": 700, "is_active": True,
        "available_days": "Mon,Tue,Wed,Thu,Fri",
        "morning_slots": ["09:00","09:30","10:00","10:30","11:00"],
        "evening_slots": ["17:00","17:30","18:00"]
    },
    {
        "name": "Dr. Anita Singh", "specialization": "Gynecologist", "department": "Gynecology",
        "experience_years": 10, "qualifications": "MBBS, MD Obstetrics and Gynecology",
        "rating": 4.9, "consultation_fee": 600, "is_active": True,
        "available_days": "Mon,Tue,Wed,Thu,Fri,Sat",
        "morning_slots": ["09:00","09:30","10:00","10:30","11:00","11:30"],
        "evening_slots": ["17:00","17:30","18:00","18:30"]
    },
    {
        "name": "Dr. Ravi Nair", "specialization": "Pediatrician", "department": "Pediatrics",
        "experience_years": 9, "qualifications": "MBBS, MD Pediatrics",
        "rating": 4.8, "consultation_fee": 400, "is_active": True,
        "available_days": "Mon,Tue,Wed,Thu,Fri,Sat",
        "morning_slots": ["09:00","09:30","10:00","10:30","11:00","11:30"],
        "evening_slots": ["17:00","17:30","18:00","18:30"]
    }
]

async def patch():
    try:
        res1 = supabase.table("doctors").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
        print("Deleted existing doctors.")
        res2 = supabase.table("doctors").insert(doctors_data).execute()
        print(f"Inserted {len(res2.data)} doctors successfully.")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(patch())
