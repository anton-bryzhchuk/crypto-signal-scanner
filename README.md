# Crypto Signal Scanner

A 24/7 crypto **market-data monitoring and alerting** tool. It continuously polls
public exchange and on-chain APIs, reads selected Telegram channels, scores what
it finds with an LLM, and pushes concise alerts to a private Telegram bot. It is a
**read-only data-collection tool** — it never places trades or moves funds.

## What it does

The scanner runs several independent collectors on separate intervals:

| Collector | Source(s) | What it flags |
|-----------|-----------|---------------|
| **Funding** | Binance, OKX, MEXC funding-rate endpoints | Perp funding rates past a configurable threshold |
| **Arbitrage** | Binance, Bybit, OKX ticker endpoints | Cross-exchange price spreads on the same asset |
| **Dump / rug** | Binance tickers, DexScreener | Sharp price drops inside a rolling time window |
| **DEX whale** | DexScreener boosts/search | Large/boosted DEX token activity |
| **On-chain** | Etherscan V2, Solscan | Notable wallet / VC-wallet movements |
| **Telegram reader** | User-joined channels (read-only) | New posts from monitored channels |

Raw hits are passed through an **Anthropic Claude** filter that assigns a 1–10
relevance score; only items at or above `MIN_SCORE` are sent, which keeps the
alert stream signal-dense. A periodic digest summarises activity.

## Architecture

```
 ┌──────────────────────────────────────────────┐
 │                Exchange & chain APIs           │
 │  Binance · Bybit · OKX · MEXC · DexScreener    │
 │           Etherscan V2 · Solscan               │
 └───────────────┬────────────────────────────────┘
                 │  (async polling, read-only)
                 ▼
        ┌───────────────────┐      ┌───────────────────┐
        │   scanner/*.py     │      │   reader/          │
        │ funding, arbitrage │      │  Telegram channels │
        │ rugs, dex_whale,   │      │  (Telethon)        │
        │ onchain            │      └─────────┬──────────┘
        └─────────┬──────────┘                │
                  └──────────────┬────────────┘
                                 ▼
                        ┌──────────────────┐
                        │  ai/filter.py     │  Claude scores
                        │  (score 1–10)     │  each candidate
                        └─────────┬─────────┘
                                  ▼
                   ┌───────────────────────────┐
                   │  db/storage.py (SQLite)    │  dedupe + state
                   └─────────────┬──────────────┘
                                 ▼
                      ┌────────────────────┐
                      │  bot/notifier.py    │  aiogram
                      │  → Telegram alerts  │
                      └────────────────────┘
```

Each loop is an independent `asyncio` task in `main.py`, so a slow or failing
source never blocks the others.

## Tech stack

- **Python 3.11**, `asyncio`
- **Telethon** (read Telegram channels) · **aiogram** (alert bot)
- **aiohttp** (async HTTP to exchange / chain APIs)
- **anthropic** (LLM scoring/filtering)
- **SQLite** for dedupe and state
- Runs 24/7 on a small Linux VPS under `systemd`

## Setup

```bash
git clone https://github.com/mesut101010/crypto-signal-scanner.git
cd crypto-signal-scanner
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # then fill in your own keys
python auth_qr.py        # one-time Telegram login (scan the QR)
python main.py
```

Edit the `MONITORED_CHATS` list in `config.py` (or use the bot's `/addchat`
command). Run `python list_chats.py` to print the IDs of channels you've joined.

### Running 24/7 on a VPS

A minimal `systemd` unit keeps it alive and restarts on failure:

```ini
[Unit]
Description=Crypto Signal Scanner
After=network-online.target

[Service]
WorkingDirectory=/opt/crypto-signal-scanner
ExecStart=/opt/crypto-signal-scanner/venv/bin/python main.py
Restart=always
EnvironmentFile=/opt/crypto-signal-scanner/.env

[Install]
WantedBy=multi-user.target
```

## Example output

Sanitized sample of what lands in the Telegram bot (illustrative data):

```
🔔 FUNDING  SKYAIUSDT
   OKX funding +0.082%  (threshold 0.05%)  ·  score 8/10
   Elevated perp funding — crowded long positioning.

🔺 ARBITRAGE  NEARUSDT
   Binance 2.4310  vs  Bybit 2.4489   spread 0.73%   ·  score 7/10

⚠️ DUMP  EXAMPLEUSDT
   -41% in 18 min  ·  24h vol $12.4M  ·  score 9/10
```

## What I learned

- Structuring a long-running `asyncio` service as independent, individually
  fault-tolerant loops.
- Normalising inconsistent REST schemas from five+ exchanges into one model.
- Keeping all credentials in environment variables and out of source control.
- Using an LLM as a *filter* to cut noise, with a tunable score gate.

---

*Built with AI-assisted development (Claude Code).*
