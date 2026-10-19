"""
DEX Whale Scanner — відслідковує великі угоди на DEX (> WHALE_THRESHOLD USD).
Джерело: DexScreener API (безкоштовно, без ключа).
Логіка:
  - Кожні 2 хв перевіряємо топ-150 токенів за DEX об'ємом
  - Якщо одна угода > $500K → алерт одразу
  - Якщо той самий гаманець робить 3+ угоди в одну сторону за 2h → DCA алерт
  - Якщо ціна впала/зросла на DUMP_DROP_PCT%+ за DUMP_WINDOW_MIN хв → dump/pump-алерт
    з топ гаманцями-продавцями чи покупцями (scanner/wallet_lookup.py)
"""
import aiohttp
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from db.storage import is_seen, mark_seen
from config import DUMP_DROP_PCT, DUMP_WINDOW_MIN
from scanner.wallet_lookup import find_top_wallets, arkham_link, explorer_link

logger = logging.getLogger(__name__)

WHALE_THRESHOLD = 500_000   # USD — мінімальна сума для whale алерту
MIN_LIQUIDITY   = 500_000   # USD — токени з меншою ліквідністю (шлак/неліквід) пропускаємо

STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "FRAX",
    "LUSD", "USDD", "GUSD", "PYUSD", "CUSD", "EURC", "USD1",
    "USDS", "CRVUSD", "SUSD", "MUSD", "USDX", "HUSD", "USDJ",
}
DCA_MIN_TRADES  = 3         # мінімум угод для DCA патерну
DCA_WINDOW_MIN  = 120       # хвилин — вікно для DCA

# Зберігаємо угоди гаманців: {wallet: [(timestamp, side, usd_amount), ...]}
_wallet_history: dict[str, list] = defaultdict(list)

# Історія цін по кожній парі для dump-детектора: {pair_addr: deque[(timestamp, price)]}
# scan_dex_whales() ганяється раз в 2 хв (DEX_WHALE_INTERVAL в main.py) — цього достатньо
# щоб зловити просадку на DUMP_WINDOW_MIN-хвилинному вікні.
_price_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=30))

# Довша історія цін (до ~30h) для MAX/MIN за останні 24 години в алерті.
# Накопичується поки бот працює — після рестарту вікно почне збиратись заново.
_price_history_24h: dict[str, deque] = defaultdict(lambda: deque(maxlen=900))
PRICE_24H_WINDOW_MIN = 24 * 60

DEXSCREENER_TRENDING = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_PAIRS     = "https://api.dexscreener.com/latest/dex/search?q={symbol}"
DEXSCREENER_TXNS      = "https://api.dexscreener.com/latest/dex/tokens/{address}"


async def _get_top_tokens() -> list[dict]:
    """Топ токени за буст/активністю на DexScreener."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(DEXSCREENER_TRENDING, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        tokens = []
        for item in (data if isinstance(data, list) else data.get("tokens", [])):
            addr = item.get("tokenAddress", "")
            chain = item.get("chainId", "ethereum")
            if addr:
                tokens.append({"address": addr, "chain": chain,
                               "symbol": item.get("description", addr[:8])})
        return tokens[:100]
    except Exception as e:
        logger.debug(f"dexscreener trending error: {e}")
        return []


async def _get_token_pairs(address: str) -> list[dict]:
    """Отримуємо пари і останні угоди для токена."""
    try:
        url = DEXSCREENER_TXNS.format(address=address)
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        return data.get("pairs", []) or []
    except Exception:
        return []


def _parse_recent_large_txns(pair: dict) -> list[dict]:
    """
    Витягуємо великі угоди з пари.
    DexScreener не дає окремих транзакцій, але дає txns за 5m/1h/6h/24h
    і priceUsd, liquidity — оцінюємо розмір через volume.
    """
    results = []
    symbol  = pair.get("baseToken", {}).get("symbol", "?")
    price   = float(pair.get("priceUsd") or 0)
    liq     = float((pair.get("liquidity") or {}).get("usd") or 0)
    vol5m   = float((pair.get("volume") or {}).get("m5") or 0)
    txns5m  = pair.get("txns", {}).get("m5", {})
    buys5m  = int(txns5m.get("buys") or 0)
    sells5m = int(txns5m.get("sells") or 0)

    # Якщо за 5 хв об'єм > threshold — підозріло велика активність
    if vol5m >= WHALE_THRESHOLD:
        total_txns = max(buys5m + sells5m, 1)
        avg_txn    = vol5m / total_txns
        # Якщо мало угод але великий об'єм — схоже на whale
        if total_txns <= 5 or avg_txn >= WHALE_THRESHOLD:
            side = "SELL" if sells5m > buys5m * 2 else ("BUY" if buys5m > sells5m * 2 else "MIX")
            results.append({
                "symbol":    symbol,
                "pair_addr": pair.get("pairAddress", ""),
                "dex":       pair.get("dexId", "unknown"),
                "chain":     pair.get("chainId", ""),
                "side":      side,
                "vol5m_usd": vol5m,
                "buys5m":    buys5m,
                "sells5m":   sells5m,
                "price":     price,
                "liquidity": liq,
                "url":       pair.get("url", ""),
            })
    return results


def _check_price_move(pair_addr: str, price: float, now: datetime) -> dict | None:
    """
    Записує ціну в історію і перевіряє чи змінилась вона на DUMP_DROP_PCT%+
    (в будь-який бік) відносно екстремуму за останні DUMP_WINDOW_MIN хвилин.
    Повертає {"direction": "dump"|"pump", "pct": ..., "ref_price": ..., "ref_ts": ...} або None.
    """
    if price <= 0:
        return None

    hist = _price_history[pair_addr]
    hist.append((now, price))
    cutoff = now - timedelta(minutes=DUMP_WINDOW_MIN)
    while hist and hist[0][0] < cutoff:
        hist.popleft()

    if len(hist) < 2:
        return None

    peak_ts, peak_price     = max(hist, key=lambda h: h[1])
    trough_ts, trough_price = min(hist, key=lambda h: h[1])

    if peak_price > 0:
        drop_pct = (peak_price - price) / peak_price * 100
        if drop_pct >= DUMP_DROP_PCT:
            return {"direction": "dump", "pct": drop_pct, "ref_price": peak_price, "ref_ts": peak_ts}

    if trough_price > 0:
        rise_pct = (price - trough_price) / trough_price * 100
        if rise_pct >= DUMP_DROP_PCT:
            return {"direction": "pump", "pct": rise_pct, "ref_price": trough_price, "ref_ts": trough_ts}

    return None


def _record_24h_price(pair_addr: str, price: float, now: datetime) -> None:
    """Записує ціну в довгу історію (виклик на кожному скані, для кожної пари)."""
    hist = _price_history_24h[pair_addr]
    hist.append((now, price))
    cutoff = now - timedelta(minutes=PRICE_24H_WINDOW_MIN)
    while hist and hist[0][0] < cutoff:
        hist.popleft()


def _get_24h_range(pair_addr: str, fallback_price: float) -> tuple[float, float]:
    """Повертає (max_24h, min_24h) з накопиченої історії."""
    prices = [p for _, p in _price_history_24h.get(pair_addr, [])] or [fallback_price]
    return max(prices), min(prices)


async def _build_move_alert(pair: dict, symbol: str, price: float, move: dict, now: datetime) -> dict:
    chain      = pair.get("chainId", "")
    token_addr = (pair.get("baseToken") or {}).get("address", "")
    liq        = float((pair.get("liquidity") or {}).get("usd") or 0)
    vol24h     = float((pair.get("volume") or {}).get("h24") or 0)
    direction  = move["direction"]                      # "dump" | "pump"
    wallet_dir = "sell" if direction == "dump" else "buy"
    elapsed_min = (now - move["ref_ts"]).total_seconds() / 60

    pair_addr = pair.get("pairAddress", "")
    high24, low24 = _get_24h_range(pair_addr, price)

    wallets = await find_top_wallets(chain, token_addr, DUMP_WINDOW_MIN, wallet_dir)
    label   = "продавці" if direction == "dump" else "покупці"
    wallets_lines = "\n".join(
        f"  • <code>{w['wallet'][:10]}...</code> —{w['amount']:,.0f} токенів  "
        f'<a href="{w["arkham"]}">Arkham</a> · <a href="{w["explorer"]}">Explorer</a>'
        for w in wallets
    )

    verb = "впав" if direction == "dump" else "зріс"
    arrow = f"${move['ref_price']:.6g} → ${price:.6g}"

    return {
        "type":          direction,
        "symbol":        symbol,
        "chain":         chain,
        "move_pct":      move["pct"],
        "ref_price":     move["ref_price"],
        "price":         price,
        "high24":        high24,
        "low24":         low24,
        "elapsed_min":   elapsed_min,
        "liquidity":     liq,
        "vol24h":        vol24h,
        "window_min":    DUMP_WINDOW_MIN,
        "wallets":       wallets,
        "wallets_label": label,
        "wallets_lines": wallets_lines,
        "url":           pair.get("url", ""),
        "arkham":        arkham_link(token_addr) if token_addr else "",
        "explorer":      explorer_link(chain, token_addr) if token_addr else "",
        "urgency":       "high",
        "summary": (
            f"${symbol} {verb} на {move['pct']:.0f}% за {DUMP_WINDOW_MIN}хв ({arrow})"
        ),
    }


async def scan_dex_whales() -> list[dict]:
    """Головна функція — повертає список алертів."""
    tokens  = await _get_top_tokens()
    alerts  = []
    now     = datetime.now(timezone.utc)

    for token in tokens[:80]:  # обмежуємо щоб не перевантажити API
        pairs = await _get_token_pairs(token["address"])
        if not pairs:
            continue

        # Беремо пару з найбільшою ліквідністю
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        top_pair = pairs[0]

        symbol = top_pair.get("baseToken", {}).get("symbol", "?")
        price  = float(top_pair.get("priceUsd") or 0)
        liq_top = float((top_pair.get("liquidity") or {}).get("usd") or 0)

        if liq_top < MIN_LIQUIDITY:
            continue  # шлак / низька ліквідність — пропускаємо повністю

        if symbol.upper() not in STABLECOINS:
            pair_addr = top_pair.get("pairAddress", "")
            _record_24h_price(pair_addr, price, now)
            move = _check_price_move(pair_addr, price, now)
            if move:
                move_key = f"{move['direction']}:{pair_addr}:{int(move['pct'] // 5)}"
                if not is_seen(move_key):
                    mark_seen(move_key)
                    alerts.append(await _build_move_alert(top_pair, symbol, price, move, now))

        txns = _parse_recent_large_txns(top_pair)
        for txn in txns:
            if txn["symbol"].upper() in STABLECOINS:
                continue
            key = f"dex_whale:{txn['pair_addr']}:{txn['side']}:{int(txn['vol5m_usd'] // 100_000)}"
            if is_seen(key):
                continue
            mark_seen(key)

            alerts.append({
                "type":      "dex_whale",
                "symbol":    txn["symbol"],
                "side":      txn["side"],
                "vol5m_usd": txn["vol5m_usd"],
                "buys5m":    txn["buys5m"],
                "sells5m":   txn["sells5m"],
                "price":     txn["price"],
                "liquidity": txn["liquidity"],
                "dex":       txn["dex"],
                "chain":     txn["chain"],
                "url":       txn["url"],
                "urgency":   "high" if txn["vol5m_usd"] >= 1_000_000 else "medium",
                "summary": (
                    f"${txn['symbol']} великий {txn['side']} на DEX {txn['dex']}: "
                    f"${txn['vol5m_usd']:,.0f} за 5 хв "
                    f"({txn['sells5m']}s/{txn['buys5m']}b угод)"
                ),
            })

    return alerts
