import os
from dotenv import load_dotenv

load_dotenv()

TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_PHONE = os.environ["TG_PHONE"]

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY", "")

MIN_SCORE = int(os.getenv("MIN_SCORE", "7"))
FUNDING_THRESHOLD = float(os.getenv("FUNDING_THRESHOLD", "0.05"))
ARB_THRESHOLD = float(os.getenv("ARB_THRESHOLD", "0.3"))

# Різкий злив токена: падіння ціни на DUMP_DROP_PCT% і більше за DUMP_WINDOW_MIN хвилин
DUMP_DROP_PCT = float(os.getenv("DUMP_DROP_PCT", "35"))
DUMP_WINDOW_MIN = int(os.getenv("DUMP_WINDOW_MIN", "30"))

# Список чатів/каналів для моніторингу (username або -100xxxxxxxxx)
# Редагуй цей список вручну або через /addchat команду в боті
MONITORED_CHATS: list[str] = [
    # "@example_channel",
    # "-1001234567890",
]

# Юзери за якими стежимо (user_id або @username)
# Це власники/адміни чатів — їх повідомлення пріоритет
PRIORITY_USERS: list[str] = [
    # "@trader_example",
]
