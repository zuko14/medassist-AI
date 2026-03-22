import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath("."))
from app.database import supabase

async def patch():
    try:
        # Step 5: Run this SQL to fix all existing wrong records
        print("Resetting all patients.")
        # Although the prompt specified an update first, it then specifies a global delete.
        # We will honor the update log for completeness then wipe.
        res1 = supabase.table("patients").update({"language": None}).eq("language", "hi").eq("visit_count", 0).execute()
        print(f"Updated {len(res1.data)} ghost records.")
        
        # Now wipe tracking states
        res2 = supabase.table("conversations").delete().neq("phone", "NONE").execute()
        res3 = supabase.table("patients").delete().neq("phone", "NONE").execute()
        print(f"Deleted {len(res2.data)} conversations and {len(res3.data)} patients.")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(patch())
