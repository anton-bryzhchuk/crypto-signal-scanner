"""
Crash detector — сканує топ монети на різкі рухи ціни за 24h.
Алерт якщо падіння або ріст > THRESHOLD_PCT.
"""
import aiohttp
from db.storage import is_seen, mark_seen

THRESHOLD_PCT = 45.0   # % — поріг для алерту (падіння або ріст)

TOP_SYMBOLS = [
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX",
    "DOT", "MATIC", "LINK", "UNI", "ATOM", "LTC", "INJ",
    "ARB", "OP", "SUI", "APT", "TIA", "WIF", "PEPE", "BONK",
    "JTO", "PYTH", "SEI", "NEAR", "FTM", "IMX", "BLUR",
]


async def _get_binance_tickers() -> list[dict]:
    url = "https://api.binance.com/api/v3/ticker/24hr"
    results = []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
        for item in data:
            sym = item["symbol"]
            if not sym.endswith("USDT"):
                continue
            base = sym.replace("USDT", "")
            if base not in TOP_SYMBOLS:
                continue
            results.append({
                "symbol":     base,
                "exchange":   "Binance",
                "change_pct": float(item["priceChangePercent"]),
                "price_now":  float(item["lastPrice"]),
                "price_open": float(item["openPrice"]),
                "price_high": float(item["highPrice"]),
                "price_low":  float(item["lowPrice"]),
                "volume_usd": float(item["quoteVolume"]),  # об'єм в USDT
            })
    except Exception:
        pass
    return results


async def scan_rugs() -> list[dict]:
    all_data = await _get_binance_tickers()
    alerts = []

    for item in all_data:
        change = item["change_pct"]
        if abs(change) < THRESHOLD_PCT:
            continue

        direction = "down" if change < 0 else "up"
        key = f"crash_{item['symbol']}_{direction}_{int(abs(change) // 10) * 10}"
        if is_seen(key):
            continue
        mark_seen(key)

        abs_change = abs(change)
        is_crash = change < 0

        if abs_change >= 70:
            emoji = "💀"
        elif abs_change >= 55:
            emoji = "🔴" if is_crash else "🚀"
        else:
            emoji = "⚠️" if is_crash else "📈"

        alerts.append({
            "type":       "crash" if is_crash else "pump",
            "symbol":     item["symbol"],
            "exchange":   item["exchange"],
            "change_pct": change,
            "price_now":  item["price_now"],
            "price_open": item["price_open"],
            "price_high": item["price_high"],
            "price_low":  item["price_low"],
            "volume_usd": item["volume_usd"],
            "emoji":      emoji,
            "urgency":    "high" if abs_change >= 55 else "medium",
            "summary": (
                f"{item['symbol']} {'впав' if is_crash else 'виріс'} "
                f"на {abs_change:.1f}% за 24h "
                f"(${item['price_open']:.4f} → ${item['price_now']:.4f})"
            ),
        })

    alerts.sort(key=lambda x: x["change_pct"])
    return alerts
