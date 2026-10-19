"""
Щоденний дайджест о 9:30 за Берліном.
Показує токени з рухом >35% за 24h та ліквідністю >$500K.
Дані з DexScreener — Claude не потрібен.
"""
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone, timedelta
from config import OWNER_CHAT_ID

logger = logging.getLogger(__name__)

BERLIN = timezone(timedelta(hours=2))
DIGEST_HOUR   = 9
DIGEST_MINUTE = 30

MIN_CHANGE_PCT = 35.0
MIN_LIQUIDITY  = 500_000


async def _get_movers() -> tuple[list[dict], list[dict]]:
    """Повертає (pumps, dumps) — токени з рухом >35% за 24h."""
    try:
        url = "https://api.dexscreener.com/token-boosts/top/v1"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return [], []
                data = await r.json()

        tokens = data if isinstance(data, list) else []
        addresses = [t.get("tokenAddress") for t in tokens[:150] if t.get("tokenAddress")]

        pumps, dumps = [], []

        for i in range(0, len(addresses), 10):
            batch = addresses[i:i+10]
            batch_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}"
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(batch_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status != 200:
                            continue
                        batch_data = await r.json()
            except Exception:
                continue

            seen = set()
            for pair in (batch_data.get("pairs") or []):
                symbol = pair.get("baseToken", {}).get("symbol", "?")
                if symbol in seen:
                    continue

                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                if liq < MIN_LIQUIDITY:
                    continue

                change = float((pair.get("priceChange") or {}).get("h24") or 0)
                if abs(change) < MIN_CHANGE_PCT:
                    continue

                price = float(pair.get("priceUsd") or 0)

                seen.add(symbol)
                entry = {
                    "symbol": symbol,
                    "change": change,
                    "price":  price,
                    "liq":    liq,
                }
                if change > 0:
                    pumps.append(entry)
                else:
                    dumps.append(entry)

            await asyncio.sleep(0.2)

        pumps.sort(key=lambda x: x["change"], reverse=True)
        dumps.sort(key=lambda x: x["change"])
        return pumps[:10], dumps[:10]

    except Exception as e:
        logger.error(f"digest movers error: {e}")
        return [], []


def _fmt_liq(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    return f"${v/1_000:.0f}K"


def _fmt_price(p: float) -> str:
    if p >= 1:
        return f"${p:.3f}"
    elif p >= 0.001:
        return f"${p:.5f}"
    else:
        return f"${p:.8f}"


async def build_digest() -> str:
    pumps, dumps = await _get_movers()
    now = datetime.now(BERLIN).strftime("%d.%m.%Y")

    if not pumps and not dumps:
        return f"📋 <b>Дайджест {now}</b>\n\nНемає токенів з рухом >35% за 24h (ліквід >$500K)"

    lines = [f"📋 <b>Дайджест {now}  •  рух >35% за 24h</b>\n"]

    if pumps:
        lines.append("🟢 <b>ЗРОСТАННЯ</b>")
        for t in pumps:
            lines.append(
                f"  <b>${t['symbol']}</b>  +{t['change']:.0f}%  "
                f"{_fmt_price(t['price'])}  ліквід {_fmt_liq(t['liq'])}"
            )

    if dumps:
        if pumps:
            lines.append("")
        lines.append("🔴 <b>ПАДІННЯ</b>")
        for t in dumps:
            lines.append(
                f"  <b>${t['symbol']}</b>  {t['change']:.0f}%  "
                f"{_fmt_price(t['price'])}  ліквід {_fmt_liq(t['liq'])}"
            )

    return "\n".join(lines)


async def digest_loop(bot):
    logger.info("Digest loop запущено")
    sent_today = None

    while True:
        now = datetime.now(BERLIN)
        today = now.date()

        if (
            now.hour == DIGEST_HOUR
            and now.minute == DIGEST_MINUTE
            and sent_today != today
        ):
            try:
                logger.info("Відправляємо дайджест...")
                text = await build_digest()
                await bot.send_message(OWNER_CHAT_ID, text, parse_mode="HTML",
                                       disable_web_page_preview=True)
                sent_today = today
            except Exception as e:
                logger.error(f"Digest error: {e}")

        await asyncio.sleep(60)
