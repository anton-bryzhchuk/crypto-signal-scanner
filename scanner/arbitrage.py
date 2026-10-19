import asyncio
import aiohttp
from config import ARB_THRESHOLD

SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK", "INJ", "ARB"]


async def _get_binance_prices() -> dict[str, float]:
    url = "https://api.binance.com/api/v3/ticker/price"
    prices = {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
        for item in data:
            sym = item["symbol"]
            if sym.endswith("USDT"):
                base = sym[:-4]
                if base in SYMBOLS:
                    prices[base] = float(item["price"])
    except Exception:
        pass
    return prices


async def _get_bybit_prices() -> dict[str, float]:
    url = "https://api.bybit.com/v5/market/tickers?category=spot"
    prices = {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
        for item in data.get("result", {}).get("list", []):
            sym = item["symbol"]
            if sym.endswith("USDT"):
                base = sym[:-4]
                if base in SYMBOLS:
                    prices[base] = float(item["lastPrice"])
    except Exception:
        pass
    return prices


async def _get_okx_prices() -> dict[str, float]:
    url = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
    prices = {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
        for item in data.get("data", []):
            inst = item.get("instId", "")
            if inst.endswith("-USDT"):
                base = inst[:-5]
                if base in SYMBOLS:
                    prices[base] = float(item["last"])
    except Exception:
        pass
    return prices


async def scan_arbitrage() -> list[dict]:
    """Шукає цінові розбіжності між біржами (spot/spot)."""
    binance, bybit, okx = await asyncio.gather(
        _get_binance_prices(),
        _get_bybit_prices(),
        _get_okx_prices(),
    )

    all_prices: dict[str, dict[str, float]] = {}
    for sym in SYMBOLS:
        entry = {}
        if sym in binance:
            entry["Binance"] = binance[sym]
        if sym in bybit:
            entry["Bybit"] = bybit[sym]
        if sym in okx:
            entry["OKX"] = okx[sym]
        if len(entry) >= 2:
            all_prices[sym] = entry

    alerts = []
    for sym, prices in all_prices.items():
        exchanges = list(prices.items())
        for i in range(len(exchanges)):
            for j in range(i + 1, len(exchanges)):
                ex_a, p_a = exchanges[i]
                ex_b, p_b = exchanges[j]
                spread_pct = abs(p_a - p_b) / min(p_a, p_b) * 100

                if spread_pct < ARB_THRESHOLD:
                    continue

                buy_ex = ex_a if p_a < p_b else ex_b
                sell_ex = ex_b if p_a < p_b else ex_a
                buy_price = min(p_a, p_b)
                sell_price = max(p_a, p_b)

                alerts.append({
                    "type": "arbitrage",
                    "symbol": sym,
                    "buy_exchange": buy_ex,
                    "sell_exchange": sell_ex,
                    "buy_price": buy_price,
                    "sell_price": sell_price,
                    "spread_pct": spread_pct,
                    "urgency": "high" if spread_pct > 0.8 else "medium",
                    "summary": (
                        f"{sym}: купи на {buy_ex} ${buy_price:.4f}, "
                        f"продай на {sell_ex} ${sell_price:.4f} → спред {spread_pct:.3f}%"
                    ),
                })

    alerts.sort(key=lambda x: x["spread_pct"], reverse=True)
    return alerts[:5]  # топ 5 щоб не спамити
