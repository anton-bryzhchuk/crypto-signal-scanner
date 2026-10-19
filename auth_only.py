"""
Авторизація Telethon з підтримкою 2FA і примусовим SMS.
"""
import asyncio
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError

import os
from dotenv import load_dotenv
load_dotenv()
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE = os.environ["TG_PHONE"]

async def main():
    client = TelegramClient("session_reader", API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        print(f"Відправляємо код на {PHONE}...")
        try:
            sent = await client.send_code_request(PHONE, force_sms=True)
            print(f"Код відправлено через: {type(sent.type).__name__}")
        except FloodWaitError as e:
            print(f"❌ Flood wait: треба почекати {e.seconds} секунд ({e.seconds//60} хв)")
            await client.disconnect()
            return
        except Exception as e:
            print(f"❌ Помилка при відправці коду: {e}")
            await client.disconnect()
            return

        code = input("Введи код: ")
        try:
            await client.sign_in(PHONE, code)
        except SessionPasswordNeededError:
            password = input("Введи 2FA пароль: ")
            await client.sign_in(password=password)
        except Exception as e:
            print(f"❌ Помилка входу: {e}")
            await client.disconnect()
            return

    me = await client.get_me()
    print(f"\n✅ Успішно! Залогінений як: {me.first_name} (@{me.username})")
    await client.disconnect()

asyncio.run(main())
