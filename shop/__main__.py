import asyncio
import logging

from aiohttp import ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from .config import Config
from .payments import Payments, deliver_notifications
from .store import Store
from .ui import Shop


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
    asyncio.run(main())
