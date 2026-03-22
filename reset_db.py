import asyncio
from app.database import supabase

async def reset_db():
    print("Deleting from conversations...")
    supabase.table("conversations").delete().neq("phone", "dummy_non_existent").execute()
    print("Deleting from patients...")
    supabase.table("patients").delete().neq("phone", "dummy_non_existent").execute()
    print("Done")

if __name__ == "__main__":
    asyncio.run(reset_db())
