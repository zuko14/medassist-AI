import asyncio
import os
from dotenv import load_dotenv
load_dotenv('.env')

from app.services.whatsapp import whatsapp_service
from app.database import supabase

async def test():
    clinic = supabase.table('clinics').select('*').eq('id', 'f13ea1b8-ec12-4d15-82a8-82668b74bd29').single().execute().data
    print(await whatsapp_service.send_text(clinic, '+919160342929', 'Test from script'))

asyncio.run(test())
