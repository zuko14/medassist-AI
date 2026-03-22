import asyncio
from app.database import supabase

async def reset_db():
    print("Clearing conversations...")
    try:
        supabase.table("conversations").delete().neq("phone", "0").execute()
    except Exception as e:
        print(e)
    print("Clearing patients...")
    try:
        supabase.table("patients").delete().neq("phone", "0").execute()
    except Exception as e:
        print(e)
    print("Done")

if __name__ == "__main__":
    asyncio.run(reset_db())
