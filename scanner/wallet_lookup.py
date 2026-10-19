"""
Wallet lookup — хто саме зливає токен.
Коли dump_detector фіксує різке падіння ціни, тут шукаємо гаманці,
які за останні DUMP_WINDOW_MIN хвилин найбільше продали цей токен,
і додаємо лінк на Arkham для ручної перевірки (публічного API в Arkham немає).
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import aiohttp
from config import ETHERSCAN_API_KEY, SOLSCAN_API_KEY

logger = logging.getLogger(__name__)

ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
SOLSCAN_BASE = "https://pro-api.solscan.io/v2.0"

# DexScreener chainId → Etherscan V2 chainid (один ключ працює на всіх EVM-чейнах)
EVM_CHAIN_IDS = {
    "ethereum": 1, "bsc": 56, "polygon": 137, "arbitrum": 42161,
    "optimism": 10, "base": 8453, "avalanche": 43114, "fantom": 250,
}


def arkham_link(address: str) -> str:
    return f"https://intel.arkm.com/explorer/address/{address}"


def explorer_link(chain: str, address: str) -> str:
    if chain == "solana":
        return f"https://solscan.io/account/{address}"
    return f"https://etherscan.io/address/{address}"


async def find_top_wallets(chain: str, token_address: str, window_min: int,
                            direction: str = "sell", limit: int = 3) -> list[dict]:
    """
    Повертає топ гаманців за останні window_min хвилин.
    direction="sell" → хто найбільше відправляв токен (дампери)
    direction="buy"  → хто найбільше отримував токен (покупці/накопичувачі)
    """
    chain = (chain or "").lower()
    if not token_address:
        return []
    if chain == "solana":
        return await _solana_top_wallets(token_address, window_min, direction, limit)
    chain_id = EVM_CHAIN_IDS.get(chain)
    if chain_id and ETHERSCAN_API_KEY:
        return await _evm_top_wallets(chain, chain_id, token_address, window_min, direction, limit)
    return []


# Лишаємо старе ім'я для зворотної сумісності
async def find_top_sellers(chain: str, token_address: str, window_min: int, limit: int = 3) -> list[dict]:
    return await find_top_wallets(chain, token_address, window_min, "sell", limit)


async def _evm_top_wallets(chain: str, chain_id: int, token_address: str, window_min: int,
                            direction: str, limit: int) -> list[dict]:
    try:
        params = {
            "chainid": str(chain_id),
            "module": "account",
            "action": "tokentx",
            "contractaddress": token_address,
            "page": "1",
            "offset": "200",
            "sort": "desc",
            "apikey": ETHERSCAN_API_KEY,
        }
        async with aiohttp.ClientSession() as s:
            async with s.get(ETHERSCAN_BASE, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
        if data.get("status") != "1":
            return []

        field = "from" if direction == "sell" else "to"
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_min)
        totals: dict[str, float] = defaultdict(float)
        for tx in data.get("result", []):
            ts = datetime.fromtimestamp(int(tx.get("timeStamp", 0)), tz=timezone.utc)
            if ts < cutoff:
                break
            decimals = int(tx.get("tokenDecimal", 18))
            amount = int(tx.get("value", 0)) / (10 ** decimals)
            totals[tx.get(field, "").lower()] += amount

        top = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:limit]
        return [
            {
                "wallet": w,
                "amount": amt,
                "arkham": arkham_link(w),
                "explorer": explorer_link(chain, w),
            }
            for w, amt in top if w
        ]
    except Exception as e:
        logger.debug(f"evm top wallets error: {e}")
        return []


async def _solana_top_wallets(token_mint: str, window_min: int, direction: str, limit: int) -> list[dict]:
    if not SOLSCAN_API_KEY:
        return []
    try:
        headers = {"token": SOLSCAN_API_KEY}
        params = {
            "address": token_mint,
            "activity_type[]": "ACTIVITY_SPL_TRANSFER",
            "page": "1",
            "page_size": "100",
            "sort_by": "block_time",
            "sort_order": "desc",
        }
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{SOLSCAN_BASE}/token/transfer", params=params,
                             headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
        items = data.get("data", []) or []

        field = "from_address" if direction == "sell" else "to_address"
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_min)
        totals: dict[str, float] = defaultdict(float)
        for it in items:
            ts = datetime.fromtimestamp(int(it.get("block_time", 0)), tz=timezone.utc)
            if ts < cutoff:
                continue
            addr = it.get(field, "")
            decimals = int(it.get("token_decimals") or it.get("decimals") or 0)
            amount = float(it.get("amount", 0)) / (10 ** decimals) if decimals else float(it.get("amount", 0))
            if addr:
                totals[addr] += amount

        top = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:limit]
        return [
            {
                "wallet": w,
                "amount": amt,
                "arkham": arkham_link(w),
                "explorer": explorer_link("solana", w),
            }
            for w, amt in top if w
        ]
    except Exception as e:
        logger.debug(f"solscan top wallets error: {e}")
        return []
