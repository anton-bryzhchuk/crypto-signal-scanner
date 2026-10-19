"""
On-chain scanner — великі переводи токенів через Etherscan API.
Логіка:
  - Моніторимо топ ERC-20 токени за капіталізацією
  - Якщо трансфер > $500K на DEX/біржу або з холодного гаманця → алерт
  - Відслідковуємо DCA: той самий гаманець продає/купує 3+ рази за 2h
"""
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from config import ETHERSCAN_API_KEY
from db.storage import is_seen, mark_seen

logger = logging.getLogger(__name__)

WHALE_USD       = 500_000    # мінімальна сума трансферу

STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "FRAX",
    "LUSD", "USDD", "GUSD", "PYUSD", "CUSD", "EURC", "USD1",
    "USDS", "CRVUSD", "SUSD", "MUSD", "USDX", "HUSD", "USDJ",
}
DCA_MIN_TRADES  = 3          # угод для DCA патерну
DCA_WINDOW_MIN  = 120        # хвилин для DCA

ETHERSCAN_BASE  = "https://api.etherscan.io/v2/api"

# ── Відомі VC / Whale гаманці ─────────────────────────────────────────────
# Будь-який великий рух з цих адрес → миттєвий алерт незалежно від токена
KNOWN_VC_WALLETS = {
    # Jump Trading
    "0xf584f8728b874a6a5c7a8d4d387c9aae9172d621": "Jump Trading",
    "0x9507c04b10486547584c37bcbd931b2a4fee9a41": "Jump Trading 2",
    "0x875d2e2e8f5c39b22cb97af22c40ccdfb9e08e30": "Jump Trading 3",
    # Wintermute (market maker)
    "0xdbf5e9c5206d0db70a90108bf936da60221dc080": "Wintermute",
    "0x56178a0d5f301baf6cf3e1cd53d9863437345bf9": "Wintermute 2",
    # a16z
    "0x05e793ce0c6027323ac150f6d45c2344d28b6019": "a16z",
    "0x66b870ddf78c975af5cd8edc6de25eca81791de1": "a16z 2",
    # Paradigm
    "0x159f95587e98fc33d5a2db39540e6d0f4c4ef546": "Paradigm",
    # Alameda/FTX (все ще активні адреси в процесі банкрутства)
    "0xfbd961d21845061fbc4bf5bb0e2e0050e000b7b9": "Alameda Research",
    # Binance Labs
    "0x7c66550c9c730b6fdd4c03bc2e73c5462c5f7cc3": "Binance Labs",
    # DWF Labs
    "0x3685f2abd9dfae2c3fa23e7e85c0de88b47e1b38": "DWF Labs",
    # Animoca Brands
    "0xc882b111a75c0c657fc507c04fbfcd2cc984f071": "Animoca Brands",
}

# ── Відомі DEX/біржові адреси (deposit/withdraw) ───────────────────────────
KNOWN_DEX = {
    "0x1111111254fb6c44bac0bed2854e76f90643097d": "1inch",
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3",
    "0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f": "SushiSwap",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap UniversalRouter",
    "0x2c55d51804cf5b436ba5af37bd7b8f5f4b6b7b7b": "Curve",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x Exchange",
    # Binance hot wallets
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance",
    # Bybit
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit",
    # OKX
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
}

# Топ токени для моніторингу (contract address → symbol)
# Оновлюється динамічно через DexScreener
_token_watchlist: dict[str, str] = {
    # Humanity Protocol H
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",  # placeholder
}

# DCA трекер: {wallet: [(timestamp, direction, usd), ...]}
_dca_tracker: dict[str, list] = defaultdict(list)


def _is_dex(address: str) -> str:
    """Повертає назву DEX якщо адреса відома, інакше ''."""
    return KNOWN_DEX.get(address.lower(), "")


async def _get_top_erc20_tokens() -> list[dict]:
    """Отримуємо топ токени через DexScreener щоб мати актуальний список."""
    try:
        url = "https://api.dexscreener.com/token-boosts/top/v1"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        tokens = []
        for item in (data if isinstance(data, list) else []):
            if item.get("chainId") == "ethereum":
                tokens.append({
                    "address": item.get("tokenAddress", "").lower(),
                    "symbol":  item.get("description", "?")[:10],
                })
        return tokens[:50]
    except Exception:
        return []


async def _get_large_transfers(token_address: str, symbol: str) -> list[dict]:
    """Отримуємо великі трансфери токена за останні 10 хвилин."""
    if not ETHERSCAN_API_KEY:
        return []
    try:
        params = {
            "chainid": "1",
            "module": "account",
            "action": "tokentx",
            "contractaddress": token_address,
            "page": "1",
            "offset": "50",
            "sort": "desc",
            "apikey": ETHERSCAN_API_KEY,
        }
        async with aiohttp.ClientSession() as s:
            async with s.get(ETHERSCAN_BASE, params=params,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()

        if data.get("status") != "1":
            return []

        results = []
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=10)

        for tx in data.get("result", []):
            ts = datetime.fromtimestamp(int(tx.get("timeStamp", 0)), tz=timezone.utc)
            if ts < cutoff:
                break

            decimals  = int(tx.get("tokenDecimal", 18))
            raw_value = int(tx.get("value", 0))
            amount    = raw_value / (10 ** decimals)

            # Потрібна ціна в USD — беремо з DexScreener окремо
            # Поки що зберігаємо в токенах, USD вирахуємо нижче
            results.append({
                "hash":     tx.get("hash", ""),
                "from":     tx.get("from", "").lower(),
                "to":       tx.get("to", "").lower(),
                "amount":   amount,
                "symbol":   tx.get("tokenSymbol", symbol),
                "ts":       ts,
                "decimals": decimals,
            })
        return results
    except Exception as e:
        logger.debug(f"etherscan tokentx error for {symbol}: {e}")
        return []


async def _get_token_price_usd(token_address: str) -> float:
    """Отримуємо поточну ціну токена в USD через DexScreener."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
        pairs = data.get("pairs") or []
        if pairs:
            return float(pairs[0].get("priceUsd") or 0)
    except Exception:
        pass
    return 0.0


def _check_dca_pattern(wallet: str, direction: str,
                       usd: float, ts: datetime) -> dict | None:
    """Перевіряємо DCA патерн для гаманця."""
    history = _dca_tracker[wallet]
    cutoff  = ts - timedelta(minutes=DCA_WINDOW_MIN)

    # Чистимо старі записи
    history = [h for h in history if h[0] >= cutoff]
    history.append((ts, direction, usd))
    _dca_tracker[wallet] = history

    # Рахуємо угоди в одному напрямку
    same_dir = [h for h in history if h[1] == direction]
    if len(same_dir) >= DCA_MIN_TRADES:
        total_usd = sum(h[2] for h in same_dir)
        return {
            "wallet":     wallet,
            "direction":  direction,
            "trades":     len(same_dir),
            "total_usd":  total_usd,
            "window_min": DCA_WINDOW_MIN,
        }
    return None


async def scan_vc_wallets() -> list[dict]:
    """
    Окремо моніторимо відомі VC/whale гаманці.
    Будь-який трансфер > $100K → алерт (нижчий поріг ніж для звичайних токенів).
    """
    if not ETHERSCAN_API_KEY:
        return []

    alerts = []
    VC_THRESHOLD = 100_000  # нижчий поріг для відомих гаманців

    for address, vc_name in KNOWN_VC_WALLETS.items():
        try:
            params = {
                "chainid": "1",
                "module": "account",
                "action": "tokentx",
                "address": address,
                "page": "1",
                "offset": "20",
                "sort": "desc",
                "apikey": ETHERSCAN_API_KEY,
            }
            async with aiohttp.ClientSession() as s:
                async with s.get(ETHERSCAN_BASE, params=params,
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()

            if data.get("status") != "1":
                continue

            now    = datetime.now(timezone.utc)
            cutoff = now - timedelta(minutes=15)

            for tx in data.get("result", []):
                ts = datetime.fromtimestamp(int(tx.get("timeStamp", 0)), tz=timezone.utc)
                if ts < cutoff:
                    break

                decimals = int(tx.get("tokenDecimal", 18))
                amount   = int(tx.get("value", 0)) / (10 ** decimals)
                symbol   = tx.get("tokenSymbol", "?")

                # Пропускаємо стейблкоїни
                if symbol.upper() in STABLECOINS:
                    continue

                tx_from  = tx.get("from", "").lower()
                tx_to    = tx.get("to", "").lower()

                # Отримуємо ціну
                token_addr = tx.get("contractAddress", "").lower()
                price = await _get_token_price_usd(token_addr)
                if price <= 0:
                    continue

                usd_value = amount * price
                if usd_value < VC_THRESHOLD:
                    continue

                # Визначаємо напрямок
                is_sender   = tx_from == address.lower()
                to_dex_name = _is_dex(tx_to)

                if is_sender and to_dex_name:
                    direction = "SELL"
                    label     = f"→ {to_dex_name}"
                elif is_sender:
                    direction = "TRANSFER OUT"
                    label     = f"→ {tx_to[:8]}..."
                else:
                    direction = "RECEIVE"
                    label     = f"← {tx_from[:8]}..."

                key = f"vc:{tx.get('hash', '')}"
                if is_seen(key):
                    continue
                mark_seen(key)

                # DCA перевірка
                dca = _check_dca_pattern(address, direction, usd_value, ts)

                urgency = "high" if direction in ("SELL", "TRANSFER OUT") else "medium"
                alerts.append({
                    "type":      "vc_whale",
                    "vc_name":   vc_name,
                    "symbol":    symbol,
                    "direction": direction,
                    "usd_value": usd_value,
                    "amount":    amount,
                    "price":     price,
                    "label":     label,
                    "tx_hash":   tx.get("hash", ""),
                    "chain":     "ETH",
                    "urgency":   urgency,
                    "dca":       dca,
                    "summary": (
                        f"{vc_name} {direction} ${symbol}: "
                        f"${usd_value:,.0f} {label}"
                        + (f" | DCA x{dca['trades']}" if dca else "")
                    ),
                })

        except Exception as e:
            logger.debug(f"vc scan error {vc_name}: {e}")
        await asyncio.sleep(0.3)  # rate limit

    return alerts


async def scan_onchain() -> list[dict]:
    """Головна функція сканера."""
    if not ETHERSCAN_API_KEY:
        logger.warning("ETHERSCAN_API_KEY не встановлено")
        return []

    tokens  = await _get_top_erc20_tokens()
    alerts  = []

    for token in tokens[:30]:  # обмеження щоб не перевищити rate limit
        addr   = token["address"]
        symbol = token["symbol"]

        # Пропускаємо стейблкоїни
        if symbol.upper() in STABLECOINS:
            continue

        txns = await _get_large_transfers(addr, symbol)
        if not txns:
            continue

        # Ціна токена для конвертації в USD
        price = await _get_token_price_usd(addr)
        if price <= 0:
            continue

        for tx in txns:
            usd_value = tx["amount"] * price
            if usd_value < WHALE_USD:
                continue

            from_dex = _is_dex(tx["from"])
            to_dex   = _is_dex(tx["to"])

            # Визначаємо напрямок
            if to_dex:
                direction = "SELL"   # переводить на DEX = готується продавати
                label     = f"→ {to_dex}"
            elif from_dex:
                direction = "BUY"    # виводить з DEX = купив
                label     = f"← {from_dex}"
            else:
                direction = "TRANSFER"
                label     = f"{tx['from'][:6]}...→{tx['to'][:6]}..."

            key = f"onchain:{tx['hash']}"
            if is_seen(key):
                continue
            mark_seen(key)

            # Перевіряємо DCA патерн
            dca = _check_dca_pattern(tx["from"], direction, usd_value, tx["ts"])

            alert = {
                "type":      "onchain_whale",
                "symbol":    tx["symbol"],
                "direction": direction,
                "usd_value": usd_value,
                "amount":    tx["amount"],
                "price":     price,
                "from":      tx["from"],
                "to":        tx["to"],
                "label":     label,
                "tx_hash":   tx["hash"],
                "chain":     "ETH",
                "urgency":   "high" if usd_value >= 1_000_000 else "medium",
                "dca":       dca,
                "summary": (
                    f"${tx['symbol']} on-chain: ${usd_value:,.0f} {direction} {label} "
                    + (f"| DCA: {dca['trades']} угод ${dca['total_usd']:,.0f} total" if dca else "")
                ),
            }
            alerts.append(alert)

            # Окремий алерт для DCA патерну
            if dca:
                dca_key = f"dca:{tx['from']}:{direction}:{dca['trades']}"
                if not is_seen(dca_key):
                    mark_seen(dca_key)
                    alerts.append({
                        "type":      "dca_pattern",
                        "symbol":    tx["symbol"],
                        "direction": direction,
                        "wallet":    tx["from"],
                        "trades":    dca["trades"],
                        "total_usd": dca["total_usd"],
                        "urgency":   "high",
                        "summary": (
                            f"⚠️ DCA {direction}: гаманець {tx['from'][:8]}... "
                            f"зробив {dca['trades']} {direction}-угод за {DCA_WINDOW_MIN}хв, "
                            f"загалом ${dca['total_usd']:,.0f}"
                        ),
                    })

    return alerts
