import argparse
import asyncio
import logging

from aiohttp import ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import FSInputFile, InputProfilePhotoStatic

from .config import Config
from .payments import XROCKET, PaymentError, Payments, deliver_notifications
from .store import Store
from .ui import LOGO, Shop


async def set_logo():
    config = Config.load()
    async with Bot(config.token, session=AiohttpSession(timeout=20)) as bot:
        await bot.set_my_profile_photo(InputProfilePhotoStatic(photo=FSInputFile(LOGO)))
    print("Аватар PCHELA обновлён.")


async def check_xrocket():
    config = Config.load()
    if not config.xrocket:
        raise SystemExit("XROCKET_TOKEN не задан.")
    async with ClientSession(timeout=ClientTimeout(total=6), trust_env=True) as http:
        payments = Payments(config, None, None, http)
        try:
            await payments.request(XROCKET, "GET", "app/info")
            print("xRocket: ключ принят.")
            currencies = await payments.request(XROCKET, "GET", "currencies/available")
        except PaymentError as error:
            raise SystemExit(str(error)) from None
    rows = currencies.get("results") if isinstance(currencies, dict) else None
    if not isinstance(rows, list):
        raise SystemExit("xRocket: неожиданный формат списка валют.")
    usdt = next((row for row in rows if isinstance(row, dict) and row.get("currency") == "USDT"), None)
    if not usdt:
        raise SystemExit("xRocket: в списке доступных валют отсутствует код USDT.")
    minimum = usdt.get("minInvoice")
    if type(minimum) not in (int, float):
        raise SystemExit("xRocket: API не вернул минимальную сумму счёта USDT.")
    print(f"xRocket: минимальная сумма счёта {minimum} USDT.")


async def main():
    config = Config.load()
    store = Store(config.database, config.currency)
    try:
        async with (
            Bot(config.token, session=AiohttpSession(timeout=8)) as bot,
            ClientSession(timeout=ClientTimeout(total=6), trust_env=True) as http,
        ):
            me = await bot.get_me()
            payments = Payments(config, store, bot, http)
            dispatcher = Dispatcher()
            dispatcher.include_router(Shop(config, store, payments, me.username).router)
            await bot.delete_webhook()
            workers = [
                asyncio.create_task(payments.watch()),
                asyncio.create_task(deliver_notifications(store, bot)),
            ]
            try:
                # Persist payment updates before polling again with a higher offset.
                await dispatcher.start_polling(bot, close_bot_session=False, handle_as_tasks=False)
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
    finally:
        store.db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Telegram-магазин PCHELA")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--set-logo", action="store_true", help="Установить логотип как аватар бота")
    actions.add_argument("--check-xrocket", action="store_true", help="Проверить ключ xRocket и минимум счёта USDT")
    args = parser.parse_args()
    if args.set_logo:
        asyncio.run(set_logo())
    elif args.check_xrocket:
        asyncio.run(check_xrocket())
    else:
        asyncio.run(main())
