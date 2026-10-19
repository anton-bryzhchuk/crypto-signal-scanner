import re
import anthropic
import json
from config import ANTHROPIC_API_KEY, MIN_SCORE
from ai.keywords import has_keyword
from db import storage

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """Ти — фільтр крипто-сигналів. Твоя задача: оцінити повідомлення і вирішити чи варто його надіслати трейдеру.

НАДСИЛАЙ ТІЛЬКИ якщо є щось конкретне:
- Арбітраж: конкретний спред між монетами/біржами з цифрами
- Funding: конкретна монета з аномальним funding rate
- Великий рух ціни + причина (не просто "BTC впав")
- Хак/exploit + постраждала сума
- Rug pull або скам з деталями
- Великі ліквідації з сумою
- Кит-рух з розміром позиції і сумою
- Регуляторні новини що реально впливають (SEC, заборона, арешт)
- Делістинг з великої біржі

ІГНОРУЙ:
- Загальні міркування без цифр і конкретики
- Рекламу, рефералки, "підписуйся"
- "скоро щось буде" без деталей
- Думки і прогнози без підстав
- Те що вже є в ціні (старі новини)
- Повторювані сигнали (той самий токен вже надсилався)
- Лістинги нових невідомих токенів

Відповідай ТІЛЬКИ JSON:
{
  "score": 1-10,
  "send": true/false,
  "category": "arbitrage|funding|hack|rug|liquidation|whale|regulation|crash|other",
  "summary": "одне речення по суті з цифрами якщо є",
  "coins": ["BTC", "ETH"],
  "urgency": "high|medium|low"
}

score >= """ + str(MIN_SCORE) + """ → send: true"""

# ── Стоп-слова (миттєво відкидаємо без Claude) ─────────────────────────────
STOP_PATTERNS = [
    # Реклама і запрошення
    r"підпис(уйся|ись|итися)", r"join\s+(us|our|channel)", r"subscribe",
    r"реферал", r"referral", r"промокод", r"promo.?code",
    r"купи (зараз|сьогодні|токен)", r"buy\s+now",
    r"розіграш", r"giveaway", r"airdrop\s+claim",
    r"t\.me/\+", r"t\.me/[a-z0-9_]+\?start=",  # реф посилання

    # Порожній контент
    r"^(добр(ий|е|ого|ої)\s+(ранок|день|вечір|ніч))",
    r"^(привіт|хай|дарова|ку|го|hi|hey|hello)\W*$",
    r"^(дякую|спасибо|thanks|thank you|👍|🙏)\W*$",
    r"^(так|ні|yes|no|ок|ok|ладно)\W*$",
    r"^[😂🤣😁😄🔥💎🚀]{1,5}$",  # тільки емодзі

    # Мотиваційний спам
    r"ходл(и|ить|ити)", r"to the moon", r"держим(о|ся)",
    r"все буде (добре|гаразд)", r"тримаємо(сь)?",
    r"в теорії", r"на думку", r"як ви думаєте",

    # Питання без цінності
    r"^(хто|що|як|де|коли|чому|який)\s+\w+\??$",
    r"^(коли|when)\s+(памп|moon|pump|dump)\??$",
]

_stop_re = re.compile("|".join(STOP_PATTERNS), re.IGNORECASE | re.UNICODE)

# ── Сигнальні патерни (збільшують шанс пройти до Claude) ──────────────────
SIGNAL_PATTERNS = [
    # Числа зі знаком %
    r"[+\-]\s*\d+\.?\d*\s*%",
    # Суми в доларах
    r"\$\s*\d+[kmb]?\b", r"\d+[kmb]?\s*(usd|usdt|usdc)\b",
    # Назви бірж
    r"\b(binance|bybit|okx|mexc|gate|kucoin|huobi|kraken|coinbase|hyperliquid|bingx|phemex)\b",
    # Слова-сигнали
    r"\b(спред|spread|арбітраж|arbitrage|лонг|шорт|long|short|ліквідац|liquidat)\b",
    r"\b(хак|hack|exploit|drain|rug|скам|scam|вкрали|зламали)\b",
    r"\b(кит|whale|великий\s+переказ|large\s+transfer)\b",
    r"\b(делістинг|delist|SEC|CFTC|санкц|ban|заборон)\b",
    # Конкретні суми (мільйони)
    r"\d+\s*(млн|мільйон|million|млрд|billion|M\b|B\b)",
    # Відсоток funding
    r"\d+\.?\d*\s*%\s*(funding|фандінг|ставк)",
]

_signal_re = re.compile("|".join(SIGNAL_PATTERNS), re.IGNORECASE | re.UNICODE)

_negative_examples: list[str] = []

# Статистика для моніторингу ефективності
_stats = {"total": 0, "pre_blocked": 0, "keyword_pass": 0, "claude_pass": 0, "claude_block": 0}


def get_stats() -> dict:
    return dict(_stats)


def pre_filter(text: str) -> tuple[bool, str]:
    """
    Повертає (pass, reason).
    True = передаємо далі (до Claude або keyword hit).
    False = відкидаємо без Claude.
    """
    # 1. Занадто короткий текст
    if len(text.strip()) < 40:
        return False, "short"

    # 2. Стоп-патерни — явний спам/чат
    if _stop_re.search(text):
        return False, "stop_pattern"

    # 3. Занадто мало унікальних слів (flood)
    words = re.findall(r"\w+", text.lower())
    if len(words) > 5 and len(set(words)) / len(words) < 0.4:
        return False, "low_entropy"

    # 4. Якщо немає жодного сигнального патерну — скіпаємо
    if not _signal_re.search(text):
        return False, "no_signal"

    return True, "ok"


def filter_message(text: str, source: str = "", is_priority_user: bool = False, bypass_prefilter: bool = False) -> dict:
    _stats["total"] += 1

    # ── Блокуємо спам-джерела одразу ───────────────────────────────────
    if source and storage.is_spam_source(source):
        _stats["pre_blocked"] += 1
        return {"score": 0, "send": False, "category": "other",
                "summary": "spam_source: заблоковано", "coins": [], "urgency": "low"}

    # ── Крок 1: ключові слова ───────────────────────────────────────────
    found, keyword = has_keyword(text)
    if found:
        # Cooldown: те саме джерело + ключове слово — не більше разу на 60 хв
        cooldown_key = f"kw:{source}:{keyword}"
        if storage.is_cooldown(cooldown_key, minutes=60):
            _stats["pre_blocked"] += 1
            return {"score": 0, "send": False, "category": "other",
                    "summary": "cooldown: вже надсилалось", "coins": [], "urgency": "low"}

        _stats["keyword_pass"] += 1
        result = _ask_claude(text, source, is_priority_user)
        result["keyword_hit"] = keyword
        # Keyword hit — відправляємо якщо Claude дає score≥5 або якщо не вдалось запитати
        if result.get("_error") or not result.get("summary"):
            result["summary"] = text[:200]
        if result.get("score", 0) >= 5 or result.get("_error"):
            result["send"] = True
            if result.get("score", 0) < 6:
                result["score"] = 6
            storage.set_cooldown(cooldown_key)
        else:
            result["send"] = False
        return result

    # ── Крок 2: pre-filter (безкоштовно) ───────────────────────────────
    if not bypass_prefilter:
        passed, reason = pre_filter(text)
        if not passed:
            _stats["pre_blocked"] += 1
            return {"score": 0, "send": False, "category": "other",
                    "summary": f"pre-filter: {reason}", "coins": [], "urgency": "low"}

    # ── Крок 3: Claude (тільки якщо пройшов pre-filter) ────────────────
    result = _ask_claude(text, source, is_priority_user)
    if result.get("send"):
        _stats["claude_pass"] += 1
    else:
        _stats["claude_block"] += 1
    return result


def _ask_claude(text: str, source: str, is_priority_user: bool) -> dict:
    extra = ""
    if is_priority_user:
        extra = "\n[PRIORITY_USER: підвищ score на 1 якщо є хоч щось корисне]"
    if _negative_examples:
        sample = _negative_examples[-3:]
        extra += f"\n[ПОГАНІ ПРИКЛАДИ — подібне не надсилати]: {'; '.join(sample)}"

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            system=SYSTEM_PROMPT + extra,
            messages=[{
                "role": "user",
                "content": f"Джерело: {source}\n\nПовідомлення:\n{text[:2000]}"
            }]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        err = str(e)
        # Якщо немає кредитів — позначаємо особливо, не відправляємо
        no_credits = "credit balance" in err or "billing" in err.lower()
        return {
            "score": 0,
            "send": False,
            "category": "other",
            "summary": text[:200],   # оригінальний текст, не помилка
            "coins": [],
            "urgency": "low",
            "_error": "no_credits" if no_credits else err,
        }


def add_negative_feedback(text: str, source: str = ""):
    short = text[:100]
    if short not in _negative_examples:
        _negative_examples.append(short)
        if len(_negative_examples) > 20:
            _negative_examples.pop(0)
    # Зберігаємо джерело в БД — після 2+ спамів автоблок
    if source:
        storage.add_spam_source(source)
