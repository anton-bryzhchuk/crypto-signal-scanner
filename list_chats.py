"""
Показує всі групи та канали де ти є учасником — з їх ID
"""
import asyncio
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

import os
from dotenv import load_dotenv
load_dotenv()
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

async def main():
    client = TelegramClient("session_reader", API_ID, API_HASH)
    await client.connect()

    print(f"\n{'Назва':<45} {'ID':<22} Тип")
    print("-" * 78)

    async for dialog in client.iter_dialogs():
        e = dialog.entity
        if isinstance(e, (Channel, Chat)):
            kind = "канал" if getattr(e, "broadcast", False) else "група"
            # Telethon повертає ID без -100 префікса — додаємо для addchat
            full_id = int(f"-100{e.id}") if isinstance(e, Channel) else -e.id
            print(f"{dialog.name[:44]:<45} {full_id:<22} {kind}")

    await client.disconnect()

asyncio.run(main())
