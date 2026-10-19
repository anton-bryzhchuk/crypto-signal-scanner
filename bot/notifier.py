import logging
import subprocess
from collections import deque
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from db import storage
from config import BOT_TOKEN, OWNER_CHAT_ID

logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

_reader_ref = None

# Rate limiter: максимум 15 TG-алертів на годину
_alert_times: deque = deque(maxlen=100)
MAX_ALERTS_PER_HOUR = 15


def set_reader(reader):
    global _reader_ref
    _reader_ref = reader


def _is_rate_limited() -> bool:
    now = datetime.now(timezone.utc).timestamp()
    hour_ago = now - 3600
    recent = [t for t in _alert_times if t > hour_ago]
    return len(recent) >= MAX_ALERTS_PER_HOUR


def _track_alert():
    _alert_times.append(datetime.now(timezone.utc).timestamp())


CATEGORY_EMOJI = {
    "arbitrage":  "⚡",
    "funding":    "💰",
    "listing":    "🚀",
    "hack":       "🔴",
    "rug":        "💀",
    "liquidation":"💥",
    "whale":      "🐋",
    "regulation": "⚖️",
    "crash":      "📉",
    "other":      "💬",
}

CATEGORY_LABEL = {
    "other": "Chat",
}

URGENCY_EMOJI = {"high": "🔥", "medium": "⚠️", "low": ""}


def _alert_kb(alert_id: int) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👍 Корисно",   callback_data=f"fb:1:{alert_id}")
    kb.button(text="👎 Спам",      callback_data=f"fb:-1:{alert_id}")
    kb.button(text="✅ Прочитав",  callback_data=f"dismiss:{alert_id}")
    kb.button(text="📄 Деталі",    callback_data=f"detail:{alert_id}")
    kb.adjust(2, 2)
    return kb.as_markup()


def _link_kb(links: list[tuple[str, str]]) -> types.InlineKeyboardMarkup:
    """Клавіатура з самих лише URL-кнопок (без фідбеку) — для dump/pump алертів."""
    kb = InlineKeyboardBuilder()
    for label, url in links:
        if url:
            kb.button(text=label, url=url)
    kb.adjust(2)
    return kb.as_markup()


async def send_tg_alert(alert: dict):
    if _is_rate_limited():
        logger.warning("Rate limit: пропускаємо TG алерт")
        return
    _track_alert()

    cat     = alert.get("category", "other")
    urgency = alert.get("urgency", "low")
    score   = alert.get("score", 0)
    coins   = alert.get("coins", [])
    source  = alert.get("source", "")
    summary = alert.get("summary", "")
    kw_hit  = alert.get("keyword_hit", "")

    coin_str = " ".join(f"#{c}" for c in coins) if coins else ""
    kw_str   = f"\n🔑 <i>ключове слово: {kw_hit}</i>" if kw_hit else ""

    is_vip = alert.get("is_vip", False)
    vip_name = alert.get("vip_name", "VIP")
    vip_badge = f"👑 <b>{vip_name}</b>  " if is_vip else ""
    msg_link = alert.get("msg_link", "")
    link_str = f'\n🔗 <a href="{msg_link}">Відкрити в Telegram</a>' if msg_link else ""

    cat_label = CATEGORY_LABEL.get(cat, cat.upper())
    text = (
        f"{vip_badge}{URGENCY_EMOJI.get(urgency, '')} {CATEGORY_EMOJI.get(cat, '📌')} "
        f"<b>{cat_label}</b>  score:{score}/10\n\n"
        f"{summary}{kw_str}\n\n"
        f"<i>📍 {source}</i>{link_str}\n"
        f"{coin_str}"
    )

    await bot.send_message(
        OWNER_CHAT_ID, text,
        parse_mode="HTML",
        reply_markup=_alert_kb(alert["alert_id"])
    )


async def send_funding_alert(alert: dict):
    alert_id   = storage.save_alert("funding", alert["summary"], 0,
                                    f"{alert['long_exchange']}/{alert['short_exchange']}")
    risk_emoji = "🔴 РИЗИК" if alert["risk"] == "high" else "🟢"
    warn_text  = ("\n\n" + "\n".join(alert["warnings"])) if alert["warnings"] else ""

    # ── Заголовок ──────────────────────────────────────────────────────
    header = (
        f"🔔 <b>Δ {alert['spread']:.3f}%  #{alert['symbol']}  за {alert['mins_left']} хв</b>\n"
        f"Спред: <b>{alert['spread']:.3f}%</b>   {risk_emoji}\n"
    )

    # ── Краща пара ─────────────────────────────────────────────────────
    pair_rows = ""
    for ex, rate, avg24, label in [
        (alert["long_exchange"],  alert["long_rate"],  None, "📗 ЛОНГ "),
        (alert["short_exchange"], alert["short_rate"], None, "📕 ШОРТ "),
    ]:
        history = [r for r in alert["all_rates"] if r["exchange"] == ex]
        avg = history[0]["avg24"] if history else rate
        pair_rows += (
            f"{label}<b>{ex}</b>\n"
            f"   Fund: <code>{rate:+.4f}%</code>   avg24: <code>{avg:+.4f}%</code>\n"
        )

    # ── Таблиця всіх бірж ──────────────────────────────────────────────
    # Exch       Fund     avg24   Trend
    col = "{:<10} {:>8}  {:>8}  {}"
    table_header = col.format("Біржа", "Fund%", "avg24%", "")
    divider      = "─" * 36
    rows = []
    for r in alert["all_rates"]:
        is_long  = r["exchange"] == alert["long_exchange"]
        is_short = r["exchange"] == alert["short_exchange"]
        marker   = " ◄L" if is_long else (" ◄S" if is_short else "")
        rows.append(col.format(
            r["exchange"][:10],
            f"{r['rate']:+.4f}",
            f"{r['avg24']:+.4f}",
            r.get("trend", "→") + marker
        ))

    table = f"<code>{table_header}\n{divider}\n" + "\n".join(rows) + "</code>"

    text = (
        f"{header}\n"
        f"{pair_rows}\n"
        f"{table}"
        f"{warn_text}"
    )
    await bot.send_message(
        OWNER_CHAT_ID, text,
        parse_mode="HTML",
        reply_markup=_alert_kb(alert_id)
    )


async def send_arb_alert(alert: dict):
    summary  = (
        f"{alert['symbol']}: купи {alert['buy_exchange']} ${alert['buy_price']:.4f} "
        f"→ продай {alert['sell_exchange']} ${alert['sell_price']:.4f} "
        f"спред {alert['spread_pct']:.3f}%"
    )
    alert_id = storage.save_alert(
        "arbitrage", summary, 0,
        f"{alert['buy_exchange']}-{alert['sell_exchange']}"
    )

    text = (
        f"⚡ <b>АРБІТРАЖ</b>  #{alert['symbol']}\n\n"
        f"🟢 Купи:   {alert['buy_exchange']}  <b>${alert['buy_price']:.4f}</b>\n"
        f"🔴 Продай: {alert['sell_exchange']}  <b>${alert['sell_price']:.4f}</b>\n"
        f"Спред: <b>{alert['spread_pct']:.3f}%</b>\n\n"
        f"<i>⚠️ Враховуй комісії (~0.1% кожна сторона)</i>"
    )
    await bot.send_message(
        OWNER_CHAT_ID, text,
        parse_mode="HTML",
        reply_markup=_alert_kb(alert_id)
    )


def _fmt_price(p: float) -> str:
    """Форматує ціну: малі числа більше знаків після коми"""
    if p >= 100:
        return f"{p:.2f}"
    elif p >= 1:
        return f"{p:.4f}"
    else:
        return f"{p:.6f}"


def _fmt_volume(v: float) -> str:
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    elif v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    else:
        return f"${v/1_000:.1f}K"


async def send_whale_alert(alert: dict):
    """Whale алерт: DEX великі угоди або on-chain переводи."""
    alert_type = alert.get("type", "")
    symbol     = alert.get("symbol", "?")
    direction  = alert.get("direction", alert.get("side", "?"))
    urgency    = alert.get("urgency", "medium")
    summary    = alert.get("summary", "")

    dir_emoji = "🔴" if direction in ("SELL", "SELL") else ("🟢" if direction == "BUY" else "🔀")
    urg_emoji = "🚨" if urgency == "high" else "⚠️"

    def _fmt_usd(v: float) -> str:
        if v >= 1_000_000:
            return f"${v/1_000_000:.2f}M"
        return f"${v/1_000:.0f}K"

    if alert_type == "dex_whale":
        vol   = alert.get("vol5m_usd", 0)
        buys  = alert.get("buys5m", 0)
        sells = alert.get("sells5m", 0)
        liq   = alert.get("liquidity", 0)
        url   = alert.get("url", "")
        impact = (vol / liq * 100) if liq > 0 else 0

        link_str = f'  <a href="{url}">📊 Chart</a>' if url else ""
        text = (
            f"{urg_emoji} <b>DEX</b>  ${symbol}  {dir_emoji} <b>{direction}</b>\n"
            f"{_fmt_usd(vol)} за 5хв  •  {sells}↓ / {buys}↑\n"
            f"Ліквід: {_fmt_usd(liq)}  •  impact ~{impact:.0f}%"
            f"{link_str}"
        )

    elif alert_type == "dca_pattern":
        trades    = alert.get("trades", 0)
        total_usd = alert.get("total_usd", 0)
        action    = "зливає" if direction == "SELL" else "набирає"
        text = (
            f"🚨 <b>DCA</b>  ${symbol}  {dir_emoji} <b>{direction}</b>\n"
            f"x{trades} угод  •  {_fmt_usd(total_usd)} за 2h\n"
            f"<i>Кит системно {action} позицію</i>"
        )

    elif alert_type == "onchain_whale":
        usd_val = alert.get("usd_value", 0)
        amount  = alert.get("amount", 0)
        label   = alert.get("label", "")
        tx_hash = alert.get("tx_hash", "")
        dca     = alert.get("dca")

        tx_link  = f"https://etherscan.io/tx/{tx_hash}"
        link_str = f'  <a href="{tx_link}">🔗 TX</a>' if tx_hash else ""
        dca_str  = f"\n⚠️ DCA x{dca['trades']}  •  {_fmt_usd(dca['total_usd'])} за 2h" if dca else ""

        amount_str = f"{amount/1_000:.0f}K" if amount >= 1000 else f"{amount:.0f}"
        text = (
            f"{urg_emoji} <b>ON-CHAIN</b>  ${symbol}  {dir_emoji} {label}\n"
            f"{_fmt_usd(usd_val)}  ({amount_str} {symbol})"
            f"{link_str}"
            f"{dca_str}"
        )
    elif alert_type == "vc_whale":
        vc_name  = alert.get("vc_name", "VC")
        usd_val  = alert.get("usd_value", 0)
        amount   = alert.get("amount", 0)
        label    = alert.get("label", "")
        tx_hash  = alert.get("tx_hash", "")
        dca      = alert.get("dca")

        tx_link  = f"https://etherscan.io/tx/{tx_hash}"
        link_str = f'  <a href="{tx_link}">🔗 TX</a>' if tx_hash else ""
        dca_str  = f"\n⚠️ DCA x{dca['trades']}  •  {_fmt_usd(dca['total_usd'])} за 2h" if dca else ""
        amount_str = f"{amount/1_000_000:.2f}M" if amount >= 1_000_000 else (
            f"{amount/1_000:.0f}K" if amount >= 1000 else f"{amount:.0f}"
        )

        text = (
            f"🏦 <b>{vc_name}</b>  {dir_emoji} <b>{direction}</b>  ${symbol}\n"
            f"{_fmt_usd(usd_val)}  ({amount_str} {symbol})  {label}"
            f"{link_str}"
            f"{dca_str}"
        )

    elif alert_type in ("dump", "pump"):
        await _send_move_alert(alert_type, symbol, alert)
        return

    else:
        text = f"🐋 <b>WHALE</b>  ${symbol}\n{summary}"

    alert_id = storage.save_alert("whale", summary, 0, f"whale:{symbol}")
    await bot.send_message(
        OWNER_CHAT_ID, text,
        parse_mode="HTML",
        reply_markup=_alert_kb(alert_id),
        disable_web_page_preview=True
    )


async def _send_move_alert(alert_type: str, symbol: str, alert: dict):
    """
    Короткий формат для dump/pump алерту: тільки суть + корисні лінки,
    без кнопок фідбеку — це терміновий сигнал, не матеріал для навчання фільтру.
    """
    move_pct   = alert.get("move_pct", 0)
    price_now  = alert.get("price", 0)
    high24     = alert.get("high24", 0)
    low24      = alert.get("low24", 0)
    elapsed_min = alert.get("elapsed_min", 0)
    vol24h     = alert.get("vol24h", 0)
    chain      = alert.get("chain", "")
    url        = alert.get("url", "")
    token_arkham   = alert.get("arkham", "")
    token_explorer = alert.get("explorer", "")
    wallets        = alert.get("wallets", [])
    wallets_label  = alert.get("wallets_label", "гаманці")

    is_dump = alert_type == "dump"
    dot     = "🔴" if is_dump else "🟢"
    sign    = "-" if is_dump else "+"

    text = (
        f"{dot} <b>${symbol}</b>  ({chain})\n"
        f"Зміна: <b>{sign}{move_pct:.2f}%</b>\n\n"
        f"MAX: <code>{high24:.6g}</code>\n"
        f"MIN: <code>{low24:.6g}</code>\n\n"
        f"<i>Зараз: ${price_now:.6g}</i>\n\n"
        f"⏱ {elapsed_min:.1f} хв\n"
        f"🌊 Обʼєм 24h: {_fmt_volume(vol24h)}"
    )

    links: list[tuple[str, str]] = []
    if url:
        links.append(("📊 Chart", url))
    if token_arkham:
        links.append(("🔗 Arkham", token_arkham))
    if token_explorer:
        links.append(("🧭 Explorer", token_explorer))
    if wallets:
        top = wallets[0]
        wlabel = "🕵️ Продавець" if wallets_label == "продавці" else "🕵️ Покупець"
        links.append((wlabel, top["arkham"]))

    storage.save_alert("whale", alert.get("summary", ""), 0, f"whale:{symbol}")
    await bot.send_message(
        OWNER_CHAT_ID, text,
        parse_mode="HTML",
        reply_markup=_link_kb(links),
        disable_web_page_preview=True
    )


async def send_rug_alert(alert: dict):
    alert_id = storage.save_alert("crash", alert["summary"], 0, alert["exchange"])
    change   = alert["change_pct"]
    is_crash = change < 0
    dot      = "🔴" if is_crash else "🟢"
    sign     = "▼" if is_crash else "▲"

    text = (
        f"{dot} <b>${alert['symbol']}</b>\n"
        f"Зміна: <b>{sign}{abs(change):.2f}%</b> за 24h\n\n"
        f"┌ MAX: <code>${_fmt_price(alert['price_high'])}</code>\n"
        f"└ MIN: <code>${_fmt_price(alert['price_low'])}</code>\n\n"
        f"24h тому:  <code>${_fmt_price(alert['price_open'])}</code>\n"
        f"Зараз:     <code>${_fmt_price(alert['price_now'])}</code>\n\n"
        f"📊 Обʼєм 24h: <b>{_fmt_volume(alert['volume_usd'])}</b>\n"
        f"🏦 {alert['exchange']}\n\n"
        f"<i>{'⚠️ Rug pull, хак або корекція?' if is_crash else '⚠️ Памп, новина або маніпуляція?'}</i>"
    )
    await bot.send_message(
        OWNER_CHAT_ID, text,
        parse_mode="HTML",
        reply_markup=_alert_kb(alert_id)
    )


# ── Callbacks ──────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("fb:"))
async def handle_feedback(call: types.CallbackQuery):
    _, value, alert_id = call.data.split(":")
    storage.update_feedback(int(alert_id), int(value))
    if int(value) == -1 and _reader_ref:
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT content, source FROM alerts WHERE id=?", (alert_id,)
            ).fetchone()
            if row:
                _reader_ref.add_negative(row["content"], source=row["source"] or "")
    emoji = "👍" if int(value) == 1 else "👎"
    await call.answer(f"{emoji} Записав, фільтр вчиться")
    # Прибираємо клавіатуру після фідбеку
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("dismiss:"))
async def handle_dismiss(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("✅ Прибрано")


@dp.callback_query(F.data.startswith("detail:"))
async def handle_detail(call: types.CallbackQuery):
    _, alert_id = call.data.split(":")
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT content, source FROM alerts WHERE id=?", (alert_id,)
        ).fetchone()
    if row:
        await call.message.answer(
            f"<b>Повний текст:</b>\n{row['content'][:3000]}\n\n<i>{row['source']}</i>",
            parse_mode="HTML"
        )
    await call.answer()


# ── Команди ────────────────────────────────────────────────────────────────

@dp.message(Command("addchat"))
async def cmd_addchat(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.answer("Використання: /addchat @username або -100xxxxxxxxx")
        return
    storage.add_chat(args[1].strip())
    await msg.answer(f"✅ Додано: {args[1].strip()}\nНадішли /restart щоб почати читати.")


@dp.message(Command("removechat"))
async def cmd_removechat(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.answer("Використання: /removechat @username")
        return
    storage.remove_chat(args[1].strip())
    await msg.answer("✅ Видалено. Надішли /restart щоб застосувати.")


@dp.message(Command("addvip"))
async def cmd_addvip(msg: types.Message):
    """Додати VIP-юзера — надсилається майже завжди"""
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    args = msg.text.split(maxsplit=2)
    if len(args) < 2:
        await msg.answer("Використання: /addvip user_id [нотатка]\nПриклад: /addvip -1945843537 Vik crypto")
        return
    user_id = args[1].strip()
    note = args[2] if len(args) > 2 else ""
    if _reader_ref:
        await _reader_ref.add_vip_user(user_id, note=note)
    else:
        storage.add_vip_user(user_id, note)
    await msg.answer(f"👑 VIP додано: {user_id} {note}\nПерезапустити не потрібно — активно одразу.")


@dp.message(Command("removevip"))
async def cmd_removevip(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        return
    storage.remove_vip_user(args[1].strip())
    await msg.answer(f"✅ VIP видалено: {args[1].strip()}")


@dp.message(Command("addviptopic"))
async def cmd_addviptopic(msg: types.Message):
    """Додати VIP-топік — всі повідомлення з нього форвардяться без фільтрів"""
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    args = msg.text.split(maxsplit=3)
    if len(args) < 3:
        await msg.answer(
            "Використання: /addviptopic chat_id topic_id [нотатка]\n"
            "Приклад: /addviptopic -1001234567890 123 Community calls\n\n"
            "Щоб дізнатись topic_id: /listtopics -100xxxxxxxxx"
        )
        return
    chat_id = args[1].strip()
    try:
        topic_id = int(args[2].strip())
    except ValueError:
        await msg.answer("❌ topic_id має бути числом")
        return
    note = args[3] if len(args) > 3 else ""
    storage.add_vip_topic(chat_id, topic_id, note)
    if _reader_ref:
        _reader_ref._load_vip_topics()
    await msg.answer(
        f"📢 VIP-топік додано!\n"
        f"Чат: <code>{chat_id}</code>\n"
        f"Топік ID: <code>{topic_id}</code>  {note}\n\n"
        f"Всі повідомлення з цього топіку будуть форвардитись без фільтрів.",
        parse_mode="HTML"
    )


@dp.message(Command("removeviptopic"))
async def cmd_removeviptopic(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    args = msg.text.split(maxsplit=2)
    if len(args) < 3:
        await msg.answer("Використання: /removeviptopic chat_id topic_id")
        return
    try:
        storage.remove_vip_topic(args[1].strip(), int(args[2].strip()))
        if _reader_ref:
            _reader_ref._load_vip_topics()
        await msg.answer("✅ VIP-топік видалено")
    except Exception as e:
        await msg.answer(f"❌ {e}")


@dp.message(Command("adduser"))
async def cmd_adduser(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    args = msg.text.split(maxsplit=2)
    if len(args) < 2:
        await msg.answer("Використання: /adduser @username [нотатка]")
        return
    user_id = args[1].strip()
    note = args[2] if len(args) > 2 else ""
    if _reader_ref:
        await _reader_ref.add_priority_user(user_id, note=note)
    await msg.answer(f"⭐ Priority user додано: {user_id}")


@dp.message(Command("removeuser"))
async def cmd_removeuser(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        return
    storage.remove_priority_user(args[1].strip())
    await msg.answer("✅ Видалено з priority list")


@dp.message(Command("list"))
async def cmd_list(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    chats = storage.get_chats()
    users = storage.get_priority_users()
    text  = "<b>📡 Моніторяться чати:</b>\n"
    text += "\n".join(f"  • {c['chat_id']}" for c in chats) or "  (порожньо)"
    text += "\n\n<b>⭐ Priority users:</b>\n"
    text += "\n".join(f"  {u['user_id']} {u['note']}" for u in users) or "  (порожньо)"
    await msg.answer(text, parse_mode="HTML")


@dp.message(Command("keywords"))
async def cmd_keywords(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    from ai.keywords import KEYWORDS
    chunks = [KEYWORDS[i:i+20] for i in range(0, len(KEYWORDS), 20)]
    for chunk in chunks:
        await msg.answer(
            "<b>🔑 Ключові слова:</b>\n" + "\n".join(f"  • {k}" for k in chunk),
            parse_mode="HTML"
        )


@dp.message(Command("digest"))
async def cmd_digest(msg: types.Message):
    """Отримати дайджест прямо зараз"""
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    await msg.answer("⏳ Генерую дайджест...")
    from bot.digest import build_digest
    text = await build_digest()
    await msg.answer(text, parse_mode="HTML")


@dp.message(Command("restart"))
async def cmd_restart(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    await msg.answer("♻️ Перезапускаю бота...")
    try:
        subprocess.Popen(["systemctl", "restart", "cryptobot"])
    except Exception as e:
        await msg.answer(f"❌ Помилка: {e}")


@dp.message(Command("filterstats"))
async def cmd_filterstats(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    from ai.filter import get_stats
    s = get_stats()
    total = s["total"] or 1
    blocked_pct = s["pre_blocked"] / total * 100
    kw_pct      = s["keyword_pass"] / total * 100
    claude_pct  = (s["claude_pass"] + s["claude_block"]) / total * 100
    saved       = s["pre_blocked"] / total * 100

    await msg.answer(
        f"<b>🔬 Статистика фільтру</b>\n\n"
        f"📨 Всього повідомлень: <b>{total}</b>\n\n"
        f"⚡ Ключове слово (форс): <b>{s['keyword_pass']}</b> ({kw_pct:.0f}%)\n"
        f"🚫 Pre-filter заблокував: <b>{s['pre_blocked']}</b> ({blocked_pct:.0f}%)\n"
        f"🤖 До Claude дійшло: <b>{s['claude_pass'] + s['claude_block']}</b> ({claude_pct:.0f}%)\n"
        f"   ✅ Claude пропустив: <b>{s['claude_pass']}</b>\n"
        f"   ❌ Claude заблокував: <b>{s['claude_block']}</b>\n\n"
        f"💰 Зекономлено API: <b>{saved:.0f}%</b> запитів",
        parse_mode="HTML"
    )


@dp.message(Command("status"))
async def cmd_status(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    chats = storage.get_chats()
    now_hour = [t for t in _alert_times
                if t > datetime.now(timezone.utc).timestamp() - 3600]
    with storage.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE date(created_at) = date('now')"
        ).fetchone()[0]
        good  = conn.execute("SELECT COUNT(*) FROM alerts WHERE feedback=1").fetchone()[0]
        bad   = conn.execute("SELECT COUNT(*) FROM alerts WHERE feedback=-1").fetchone()[0]
    text = (
        f"<b>📊 Статус бота</b>\n\n"
        f"📡 Чатів: {len(chats)}\n"
        f"📨 Алертів за цю годину: {len(now_hour)}/{MAX_ALERTS_PER_HOUR}\n"
        f"📨 Алертів сьогодні: {today}\n"
        f"📨 Алертів всього: {total}\n"
        f"👍 Корисних: {good}\n"
        f"👎 Спаму: {bad}\n"
    )
    await msg.answer(text, parse_mode="HTML")


@dp.message(Command("listtopics"))
async def cmd_listtopics(msg: types.Message):
    """Показує всі топіки форум-групи"""
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        await msg.answer("Використання: /listtopics -100xxxxxxxxx")
        return
    if not _reader_ref:
        await msg.answer("❌ Reader не запущений")
        return
    await msg.answer("⏳ Отримую топіки...")
    topics = await _reader_ref.get_forum_topics(args[1].strip())
    if not topics:
        await msg.answer("❌ Не вдалось отримати топіки (або це не форум-група)")
        return
    lines = "\n".join(f"  <code>{t['id']}</code>  {t['title']}" for t in topics)
    await msg.answer(
        f"<b>📋 Топіки групи:</b>\n\n{lines}\n\n"
        f"Щоб встановити фільтр:\n"
        f"<code>/settopics {args[1].strip()} ID1,ID2</code>",
        parse_mode="HTML"
    )


@dp.message(Command("settopics"))
async def cmd_settopics(msg: types.Message):
    """Встановлює які топіки моніторити для групи"""
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    args = msg.text.split(maxsplit=2)
    if len(args) < 3:
        await msg.answer(
            "Використання: /settopics -100xxxxxxxxx ID1,ID2\n"
            "Приклад: /settopics -1001234567890 2,5\n"
            "Щоб зняти фільтр (моніторити всі): /settopics -100xxx all"
        )
        return
    chat_id = args[1].strip()
    topic_str = "" if args[2].strip().lower() == "all" else args[2].strip()
    storage.set_chat_topics(chat_id, topic_str)
    if topic_str:
        await msg.answer(f"✅ Фільтр встановлено: топіки {topic_str}\nНадішли /restart щоб застосувати.")
    else:
        await msg.answer(f"✅ Фільтр знятий — моніторяться всі топіки\nНадішли /restart щоб застосувати.")


@dp.message(Command("id"))
async def cmd_id(msg: types.Message):
    """Перешли повідомлення з чату → отримаєш його ID"""
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    if msg.forward_from_chat:
        chat = msg.forward_from_chat
        await msg.answer(
            f"<b>Чат знайдено:</b>\n"
            f"Назва: <b>{chat.title}</b>\n"
            f"ID: <code>{chat.id}</code>\n"
            f"Тип: {chat.type}\n\n"
            f"Додати: <code>/addchat {chat.id}</code>",
            parse_mode="HTML"
        )
    elif msg.forward_from:
        user = msg.forward_from
        name = f"@{user.username}" if user.username else f"{user.first_name}"
        await msg.answer(
            f"Це повідомлення від <b>юзера</b> {name} (id: <code>{user.id}</code>)\n\n"
            f"Щоб отримати ID <b>чату</b> — перешли повідомлення від самого каналу/групи "
            f"(не від юзера в групі, а системне або від адміна каналу)",
            parse_mode="HTML"
        )
    else:
        await msg.answer(
            "Перешли сюди будь-яке повідомлення з чату/каналу який хочеш додати.\n\n"
            "⚠️ Якщо пересилаєш з групи — вибирай повідомлення від самої групи (закріплені, системні), а не від юзера."
        )


@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    if msg.from_user.id != OWNER_CHAT_ID:
        return
    await msg.answer(
        "<b>🤖 Команди бота:</b>\n\n"
        "<b>Чати:</b>\n"
        "/addchat @username — додати канал/групу\n"
        "/removechat @username — видалити\n"
        "/list — показати всі чати\n"
        "/listtopics -100xxx — показати топіки форум-групи\n"
        "/settopics -100xxx ID1,ID2 — моніторити тільки ці топіки\n\n"
        "<b>Users:</b>\n"
        "/addvip user_id [нотатка] — VIP (майже завжди надсилається)\n"
        "/removevip user_id — видалити VIP\n"
        "/addviptopic chat_id topic_id [нотатка] — VIP топік (без фільтрів)\n"
        "/removeviptopic chat_id topic_id — видалити VIP топік\n"
        "/adduser @username [нотатка] — priority user (score +1)\n"
        "/removeuser @username — видалити\n\n"
        "<b>Фільтр:</b>\n"
        "/keywords — список ключових слів\n\n"
        "<b>Система:</b>\n"
        "/digest — дайджест прямо зараз\n"
        "/restart — перезапустити бота\n"
        "/status — статистика алертів\n"
        "/help — ця довідка\n\n"
        "<b>Кнопки на алертах:</b>\n"
        "👍 Корисно / 👎 Спам — навчають фільтр\n"
        "✅ Прочитав — видаляє повідомлення\n"
        "📄 Деталі — повний текст",
        parse_mode="HTML"
    )
