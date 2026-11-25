import asyncio
import logging
import time
from decimal import Decimal, DecimalException
from urllib.parse import urlsplit

from aiohttp import ClientError
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LabeledPrice

log = logging.getLogger(__name__)
CRYPTOBOT = "CryptoBot"
XROCKET = "xRocket"
STARS = "Telegram Stars"


class PaymentError(Exception):
    pass


class Payments:
    def __init__(self, config, store, bot, http):
        self.config = config
        self.store = store
        self.bot = bot
        self.http = http
        crypto_host = "testnet-pay.crypt.bot" if config.testnet else "pay.crypt.bot"
        self.crypto_url = f"https://{crypto_host}/api"

    @property
    def providers(self):
        if self.config.currency == "XTR":
            return [STARS]
        return [name for name, token in ((CRYPTOBOT, self.config.cryptobot), (XROCKET, self.config.xrocket)) if token]

    async def request(self, provider, method, path, **kwargs):
        if provider == CRYPTOBOT:
            base = self.crypto_url
            headers = {"Crypto-Pay-API-Token": self.config.cryptobot}
            flag, data = "ok", "result"
        else:
            base = "https://pay.xrocket.tg"
            headers = {"Rocket-Pay-Key": self.config.xrocket}
            flag, data = "success", "data"
        try:
            async with self.http.request(method, f"{base}/{path}", headers=headers,
                                         allow_redirects=False, **kwargs) as response:
                body = await response.json()
                if not 200 <= response.status < 300 or body.get(flag) is not True or data not in body:
                    raise PaymentError(f"{provider}: API не принял запрос ({response.status})")
                return body[data]
        except (ClientError, TimeoutError, ValueError, AttributeError):
            raise PaymentError(f"{provider}: ошибка ответа API") from None

    async def create(self, user_id, provider, amount):
        if provider not in self.providers:
            raise ValueError("Этот способ пополнения недоступен.")
        deposit_id = self.store.create_deposit(user_id, provider, amount)
        description = "Пополнение баланса магазина"
        if provider == STARS:
            url = await self.bot.create_invoice_link(
                title="Пополнение баланса", description=description, payload=deposit_id,
                currency="XTR", prices=[LabeledPrice(label="Баланс магазина", amount=amount)],
            )
            external_id = None
        elif provider == CRYPTOBOT:
            invoice = await self.request(provider, "POST", "createInvoice", json={
                "asset": "USDT", "amount": f"{Decimal(amount) / 100:.2f}", "payload": deposit_id,
                "description": description, "expires_in": 1800, "allow_comments": False,
            })
            url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
            external_id = str(invoice["invoice_id"])
        else:
            invoice = await self.request(provider, "POST", "tg-invoices", json={
                "amount": amount / 100, "currency": "USDT", "payload": deposit_id,
                "description": description, "expiredIn": 1800, "numPayments": 1, "commentsEnabled": False,
            })
            url, external_id = invoice["link"], str(invoice["id"])
        parsed = urlsplit(url or "")
        if parsed.scheme != "https" or parsed.hostname != "t.me":
            raise PaymentError("Платёжная система вернула неизвестную ссылку.")
        self.store.update("UPDATE deposits SET external_id = ?, url = ? WHERE id = ?", external_id, url, deposit_id)
        return url

    def stars_match(self, payer, payload, currency, amount):
        deposit = self.store.one("""
            SELECT * FROM deposits WHERE id = ? AND user_id = ? AND provider = ?
            AND status = 'pending' AND url IS NOT NULL
        """, payload, payer, STARS)
        return bool(deposit and currency == "XTR" and amount == deposit["amount"])

    def reserve_stars(self, payer, payload, currency, amount, checkout_id):
        if currency != "XTR":
            return False
        with self.store.write():
            reserved = self.store.db.execute("""
                UPDATE deposits SET checkout_id = ? WHERE id = ? AND user_id = ? AND provider = ?
                AND amount = ? AND status = 'pending' AND url IS NOT NULL
                AND (checkout_id IS NULL OR checkout_id = ?)
            """, (checkout_id, payload, payer, STARS, amount, checkout_id))
            return bool(reserved.rowcount)

    def credit(self, deposit_id, charge_id):
        return self.store.credit(deposit_id, charge_id, self.config.referral_percent,
                                 lambda amount: f"Баланс пополнен на {self.config.money(amount)}.")

    async def check_crypto(self, deposit):
        provider, external_id = deposit["provider"], deposit["external_id"]
        if provider == CRYPTOBOT:
            result = await self.request(provider, "GET", "getInvoices", params={"invoice_ids": external_id})
            if not result["items"]:
                return
            invoice = result["items"][0]
            invoice_id, currency = invoice["invoice_id"], invoice["asset"]
        else:
            invoice = await self.request(provider, "GET", f"tg-invoices/{external_id}")
            invoice_id, currency = invoice["id"], invoice["currency"]

        try:
            amount = Decimal(str(invoice["amount"]))
        except DecimalException:
            raise PaymentError(f"{provider}: некорректная сумма счёта") from None
        if (str(invoice_id) != external_id or currency != "USDT"
                or amount != Decimal(deposit["amount"]) / 100 or invoice.get("payload") != deposit["id"]):
            raise PaymentError(f"{provider}: параметры счёта не совпадают")
        if invoice["status"] == "paid":
            self.credit(deposit["id"], external_id)
        elif invoice["status"] in {"expired", "cancelled"}:
            self.store.update("UPDATE deposits SET status = 'expired' WHERE id = ? AND status = 'pending'", deposit["id"])

    async def watch(self):
        providers = [provider for provider in self.providers if provider != STARS]
        while True:
            for deposit in self.store.poll_batch(providers):
                self.store.update("UPDATE deposits SET next_poll = ? WHERE id = ?",
                                  int(time.time()) + 30, deposit["id"])
                try:
                    await self.check_crypto(deposit)
                except (PaymentError, KeyError, TypeError, ValueError) as error:
                    log.warning("Счёт %s не проверен (%s)", deposit["id"], type(error).__name__)
            await asyncio.sleep(3)


async def deliver_notifications(store, bot):
    while True:
        batch = store.all("SELECT * FROM outbox WHERE next_attempt <= ? ORDER BY id LIMIT 50", int(time.time()))
        for item in batch:
            try:
                await bot.send_message(item["recipient"], item["message"], parse_mode=item["parse_mode"],
                                       link_preview_options={"is_disabled": True})
            except TelegramForbiddenError:
                log.warning("Telegram запретил доставку сообщения %s получателю %s", item["id"], item["recipient"])
                store.update("DELETE FROM outbox WHERE id = ?", item["id"])
            except TelegramRetryAfter as error:
                await asyncio.sleep(error.retry_after)
                break
            except Exception as error:
                log.warning("Сообщение %s отложено (%s)", item["id"], type(error).__name__)
                store.update("UPDATE outbox SET next_attempt = ? WHERE id = ?", int(time.time()) + 60, item["id"])
            else:
                store.update("DELETE FROM outbox WHERE id = ?", item["id"])
        await asyncio.sleep(2)
