"""
QR-авторизація Telethon — без SMS коду.
Відскануй QR своїм Telegram додатком.
"""
import asyncio
import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

import os
from dotenv import load_dotenv
load_dotenv()
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

async def main():
    client = TelegramClient("session_reader", API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"✅ Вже залогінений як {me.first_name} (@{me.username})")
        await client.disconnect()
        return

    print("Генеруємо QR код...")
    try:
        qr_login = await client.qr_login()

        # Зберігаємо QR як PNG
        img = qrcode.make(qr_login.url)
        img_path = "qr_login.png"
        img.save(img_path)
        print(f"✅ QR збережено: {img_path}")

        # Відкриваємо автоматично
        import subprocess
        subprocess.Popen(["open", img_path])

        print("\n📱 Відкрий Telegram → Налаштування → Пристрої → Підключити пристрій → відскануй QR")
        print("Очікуємо сканування...")

        try:
            await qr_login.wait(timeout=120)
        except SessionPasswordNeededError:
            password = input("\nВведи 2FA пароль: ")
            await client.sign_in(password=password)

        me = await client.get_me()
        print(f"\n✅ Успішно! Залогінений як: {me.first_name} (@{me.username})")

    except Exception as e:
        print(f"❌ Помилка: {e}")
    finally:
        await client.disconnect()

asyncio.run(main())
