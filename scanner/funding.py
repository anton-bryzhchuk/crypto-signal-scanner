"""
Funding Rate Arbitrage Scanner.

Стратегія:
  Знаходимо пари бірж де ставки РІЗНІ:
    Біржа A: ставка дуже негативна (−) → ЛОНГ → отримуємо виплату
    Біржа B: ставка нейтральна або позитивна → ШОРТ → платимо мало
  Прибуток = різниця ставок між біржами.

Алерт тільки якщо:
  1. До виплати ≤ 15 хвилин
  2. Різниця ставок ≥ MIN_SPREAD_PCT
  3. Ставка стабільна останні 2 години
"""

import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from db.storage import save_funding_rate, get_funding_history, is_funding_seen, upsert_funding_seen

MIN_SPREAD_PCT       = 0.08
ALERT_BEFORE_MINUTES = 15
PAYOUT_HOURS         = [0, 8, 16]
FLIPPER_EXCHANGES    = {"KuCoin", "BingX", "Gate"}

TOP_SYMBOLS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
    "DOT", "MATIC", "LINK", "UNI", "ATOM", "LTC", "INJ",
    "ARB", "OP", "SUI", "APT", "WIF", "PEPE", "TIA", "NEAR",
]


def minutes_to_payout() -> int:
    now = datetime.now(timezone.utc)
    candidates = []
    for h in PAYOUT_HOURS:
        payout = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if payout <= now:
            payout += timedelta(days=1)
        candidates.append((payout - now).total_seconds() / 60)
    return int(min(candidates))


def is_rate_stable(exchange: str, symbol: str, current_rate: float) -> tuple[bool, str]:
    history = get_funding_history(exchange, symbol, hours=2)
    if len(history) < 2:
        return True, ""
    signs = [r > 0 for r in history]
    if len(set(signs)) > 1:
        return False, f"{exchange}: міняв знак за 2h"
    avg = sum(history) / len(history)
    if avg != 0 and abs(current_rate / avg) > 3:
        return False, f"{exchange}: різка зміна перед виплатою"
    return True, ""


def rate_trend(exchange: str, symbol: str, current: float) -> str:
    """↑ ↓ → залежно від тренду"""
    history = get_funding_history(exchange, symbol, hours=4)
    if len(history) < 2:
        return "→"
    avg_old = sum(history[2:]) / len(history[2:]) if len(history) > 2 else history[-1]
    if current > avg_old + 0.005:
        return "↑"
    if current < avg_old - 0.005:
        return "↓"
    return "→"


async def _fetch(session, url, timeout=10):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            return await r.json()
    except Exception:
        return None


async def get_all_funding() -> list[dict]:
    results = []
    async with aiohttp.ClientSession() as s:
        tasks = {
            "binance": _fetch(s, "https://fapi.binance.com/fapi/v1/premiumIndex"),
            "bybit":   _fetch(s, "https://api.bybit.com/v5/market/tickers?category=linear"),
            "okx":     _fetch(s, "https://www.okx.com/api/v5/public/funding-rate?instType=SWAP"),
            "mexc":    _fetch(s, "https://contract.mexc.com/api/v1/contract/funding_rate"),
        }
        fetched = dict(zip(tasks.keys(), await asyncio.gather(*tasks.values())))

    # Binance
    if fetched.get("binance"):
        for item in fetched["binance"]:
            sym = item["symbol"].replace("USDT", "").replace("BUSD", "")
            if sym in TOP_SYMBOLS:
                results.append({"exchange": "Binance", "symbol": sym,
                                 "rate": float(item["lastFundingRate"]) * 100})

    # Bybit
    if fetched.get("bybit"):
        for item in fetched["bybit"].get("result", {}).get("list", []):
            sym = item["symbol"].replace("USDT", "").replace("PERP", "")
            if sym in TOP_SYMBOLS and item.get("fundingRate"):
                results.append({"exchange": "Bybit", "symbol": sym,
                                 "rate": float(item["fundingRate"]) * 100})

    # OKX
    if fetched.get("okx"):
        for item in fetched["okx"].get("data", []):
            sym = item.get("instId", "").replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
            if sym in TOP_SYMBOLS and item.get("fundingRate"):
                results.append({"exchange": "OKX", "symbol": sym,
                                 "rate": float(item["fundingRate"]) * 100})

    # MEXC
    if fetched.get("mexc") and isinstance(fetched["mexc"], dict):
        for item in fetched["mexc"].get("data", []):
            sym = item.get("symbol", "").replace("_USDT", "")
            if sym in TOP_SYMBOLS and item.get("fundingRate"):
                results.append({"exchange": "MEXC", "symbol": sym,
                                 "rate": float(item["fundingRate"]) * 100})

    return results


async def scan_funding() -> list[dict]:
    mins_left = minutes_to_payout()

    all_rates = await get_all_funding()

    # Зберігаємо в історію
    for item in all_rates:
        save_funding_rate(item["exchange"], item["symbol"], item["rate"])

    if mins_left > ALERT_BEFORE_MINUTES:
        return []

    # Групуємо по символу
    by_symbol: dict[str, list[dict]] = {}
    for item in all_rates:
        by_symbol.setdefault(item["symbol"], []).append(item)

    alerts = []

    for symbol, rates in by_symbol.items():
        if len(rates) < 2:
            continue

        rates_sorted = sorted(rates, key=lambda x: x["rate"])
        lowest  = rates_sorted[0]
        highest = rates_sorted[-1]
        spread  = highest["rate"] - lowest["rate"]

        if spread < MIN_SPREAD_PCT:
            continue

        key = f"farb_{symbol}_{lowest['exchange']}_{highest['exchange']}"
        if is_funding_seen(key, spread):
            continue
        upsert_funding_seen(key, spread)

        # Стабільність і тренд
        stable_low,  warn_low  = is_rate_stable(lowest["exchange"],  symbol, lowest["rate"])
        stable_high, warn_high = is_rate_stable(highest["exchange"], symbol, highest["rate"])

        warnings = []
        for ex in [lowest["exchange"], highest["exchange"]]:
            if ex in FLIPPER_EXCHANGES:
                warnings.append(f"⚠️ {ex} часто фліпає ставку перед виплатою")
        if warn_low:  warnings.append(warn_low)
        if warn_high: warnings.append(warn_high)

        # Тренди для всіх бірж
        for r in rates_sorted:
            r["trend"] = rate_trend(r["exchange"], symbol, r["rate"])
            history = get_funding_history(r["exchange"], symbol, hours=24)
            r["avg24"] = sum(history) / len(history) if history else r["rate"]

        risk = "high" if (not stable_low or not stable_high or
                          lowest["exchange"] in FLIPPER_EXCHANGES or
                          highest["exchange"] in FLIPPER_EXCHANGES) else "low"

        alerts.append({
            "type":           "funding_arb",
            "symbol":         symbol,
            "long_exchange":  lowest["exchange"],
            "long_rate":      lowest["rate"],
            "short_exchange": highest["exchange"],
            "short_rate":     highest["rate"],
            "spread":         spread,
            "mins_left":      mins_left,
            "warnings":       warnings,
            "risk":           risk,
            "all_rates":      rates_sorted,   # всі біржі для таблиці
            "summary": (
                f"{symbol}: ЛОНГ {lowest['exchange']} {lowest['rate']:+.4f}% / "
                f"ШОРТ {highest['exchange']} {highest['rate']:+.4f}% | "
                f"різниця {spread:.4f}% | до виплати {mins_left}хв"
            ),
        })

    alerts.sort(key=lambda x: x["spread"], reverse=True)
    return alerts[:3]
