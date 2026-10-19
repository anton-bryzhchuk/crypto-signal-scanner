import asyncio
import logging
from db import storage
from reader.tg_reader import TGReader
from scanner.funding import scan_funding
from scanner.arbitrage import scan_arbitrage
from scanner.rugs import scan_rugs
from scanner.dex_whale import scan_dex_whales
from scanner.onchain import scan_onchain, scan_vc_wallets
from bot.notifier import bot, dp, set_reader, send_tg_alert, send_funding_alert, send_arb_alert, send_rug_alert, send_whale_alert
from bot.digest import digest_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

FUNDING_INTERVAL   = 3 * 60    # кожні 3 хвилини
ARB_INTERVAL       = 60        # кожну хвилину
RUG_INTERVAL       = 10 * 60   # кожні 10 хвилин
DEX_WHALE_INTERVAL = 2 * 60    # кожні 2 хвилини
ONCHAIN_INTERVAL   = 3 * 60    # кожні 3 хвилини
VC_WALLET_INTERVAL = 5 * 60    # кожні 5 хвилин


async def funding_loop():
    while True:
        try:
            alerts = await scan_funding()
            for alert in alerts:
                logger.info(f"Funding alert: {alert['summary']}")
                await send_funding_alert(alert)
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Funding loop error: {e}")
        await asyncio.sleep(FUNDING_INTERVAL)


async def arb_loop():
    while True:
        try:
            alerts = await scan_arbitrage()
            for alert in alerts:
                logger.info(f"Arb alert: {alert['summary']}")
                await send_arb_alert(alert)
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Arb loop error: {e}")
        await asyncio.sleep(ARB_INTERVAL)


async def rug_loop():
    while True:
        try:
            alerts = await scan_rugs()
            for alert in alerts:
                logger.info(f"Rug alert: {alert['summary']}")
                await send_rug_alert(alert)
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Rug loop error: {e}")
        await asyncio.sleep(RUG_INTERVAL)


async def dex_whale_loop():
    await asyncio.sleep(15)  # даємо боту запуститись
    while True:
        try:
            alerts = await scan_dex_whales()
            for alert in alerts:
                logger.info(f"DEX Whale: {alert['summary']}")
                await send_whale_alert(alert)
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"DEX whale loop error: {e}")
        await asyncio.sleep(DEX_WHALE_INTERVAL)


async def onchain_loop():
    await asyncio.sleep(30)
    while True:
        try:
            alerts = await scan_onchain()
            for alert in alerts:
                logger.info(f"Onchain: {alert['summary']}")
                await send_whale_alert(alert)
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Onchain loop error: {e}")
        await asyncio.sleep(ONCHAIN_INTERVAL)


async def vc_wallet_loop():
    await asyncio.sleep(45)
    while True:
        try:
            alerts = await scan_vc_wallets()
            for alert in alerts:
                logger.info(f"VC wallet: {alert['summary']}")
                await send_whale_alert(alert)
                await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"VC wallet loop error: {e}")
        await asyncio.sleep(VC_WALLET_INTERVAL)


async def main():
    storage.init_db()
    logger.info("БД ініціалізовано")

    reader = TGReader(on_alert_callback=send_tg_alert)
    set_reader(reader)

    await reader.start()
    logger.info("TG Reader запущено")

    await asyncio.gather(
        dp.start_polling(bot, handle_signals=False),
        funding_loop(),
        arb_loop(),
        rug_loop(),
        dex_whale_loop(),
        onchain_loop(),
        vc_wallet_loop(),
        digest_loop(bot),
        reader.client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
