import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerPreCheckoutQuery, CreateInvoiceLink, SendMessage, SendPhoto
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, Update

from shop.config import Config
from shop.payments import CRYPTOBOT, STARS, XROCKET, PaymentError, Payments
from shop.store import Store
from shop.ui import LOGO, Shop


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, CreateInvoiceLink):
            return "https://t.me/$test-invoice"
        return True

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError
        yield


class ShopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = Config("unused", (8,), "https://t.me/admin_user", "https://t.me/support_user",
                             5, ":memory:", "XTR", "", "", False)
        self.store = Store(":memory:", "XTR")
        self.addCleanup(self.store.db.close)
        self.telegram = FakeTelegram()
        self.bot = Bot("123:test", session=self.telegram)
        self.payments = Payments(self.config, self.store, self.bot, None)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(Shop(self.config, self.store, self.payments, "test_shop_bot").router)
        self.update_id = 0

    async def feed(self, payload):
        self.update_id += 1
        await self.dispatcher.feed_update(self.bot, Update.model_validate({"update_id": self.update_id, **payload}))
        return self.telegram.calls[-1]

    @staticmethod
    def telegram_user(user, username):
        return {"id": user, "is_bot": False, "first_name": "User", "username": username}

    async def chat(self, user, text=None, username=None, **fields):
        message = {"message_id": self.update_id + 1, "date": 1, "chat": {"id": user, "type": "private"},
                   "from": self.telegram_user(user, username), **fields}
        if text is not None:
            message["text"] = text
        return await self.feed({"message": message})

    async def test_catalog_purchase_issue_and_referral_bonus(self):
        await self.chat(1, "/start")
        await self.chat(2, "/start ref_1")
        await self.chat(2, "/start ref_8")
        await self.chat(8, "/start")
        await self.chat(8, "🛠 Админ-панель")
        await self.chat(8, "📁 Добавить категорию")
        await self.chat(8, "Игры")
        await self.chat(8, "➕ Добавить товар")
        await self.chat(8, "📁 Игры")
        await self.chat(8, "Товар")
        await self.chat(8, "Описание товара")
        await self.chat(8, "50")
        await self.chat(8, "✏️ Редактировать товар")
        await self.chat(8, "🛒 Игры / Товар")
        await self.chat(8, "💰 Цена")
        await self.chat(8, "100")

        await self.chat(2, "🛍 Каталог")
        await self.chat(2, "📁 Игры")
        await self.chat(2, "🛒 Товар")
        await self.chat(2, "2")
        call = await self.chat(2, "🛒 Оплатить с баланса")
        self.assertIn("Недостаточно средств", call.text)
        self.assertFalse(self.store.all("SELECT id FROM orders"))
        deposit = self.store.create_deposit(2, STARS, 200)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 1")["balance"], 0)
        self.payments.credit(deposit, "test-credit")
        referral = await self.chat(1, "🤝 Реферальная программа")
        self.assertIn("на внутренний баланс", referral.text)
        self.assertNotIn("вывод", referral.text)
        self.assertEqual([row[0].text for row in referral.reply_markup.keyboard], ["👤 Кабинет", "🏠 Главное меню"])
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 1")["balance"], 10)
        receipt = await self.chat(2, "🛒 Оплатить с баланса")
        self.assertIsInstance(receipt.reply_markup, ReplyKeyboardMarkup)
        self.assertIn("Заказ №1", receipt.text)
        self.assertIn(self.config.admin_url, receipt.text)
        await self.chat(2, "🛒 Оплатить с баланса")
        await self.chat(2, "✅ Выдано")
        order = self.store.one("SELECT * FROM orders")
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["total"], 200)
        admin_notice = self.store.one("SELECT message FROM outbox WHERE recipient = 8")["message"]
        self.assertIn("tg://user?id=2", admin_notice)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 2")["balance"], 0)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 1")["balance"], 10)

        self.store.create_deposit(2, STARS, 300)
        home = await self.chat(2, "🏠 Главное меню")
        home_buttons = [button.text for row in home.reply_markup.keyboard for button in row]
        self.assertIn("👤 Кабинет", home_buttons)
        self.assertNotIn("🤝 Реферальная программа", home_buttons)
        profile = await self.chat(2, "👤 Кабинет")
        self.assertIn("Telegram ID · <code>2</code>", profile.text)
        self.assertIn("Баланс · <b>0 ⭐</b>", profile.text)
        self.assertIn("Пополнено за всё время · <b>200 ⭐</b>", profile.text)
        self.assertIn("Заказов · 1", profile.text)
        self.assertIn("Потрачено · 200 ⭐", profile.text)
        referral_button = profile.reply_markup.keyboard[0][1].text
        self.assertEqual(referral_button, "🤝 Реферальная программа")
        referral_screen = await self.chat(2, referral_button)
        self.assertIn("https://t.me/test_shop_bot?start=ref_2", referral_screen.text)

        # A pending order from the old scheme may still have a reward recorded.
        self.store.update("UPDATE orders SET reward = 10 WHERE id = 1")
        await self.chat(8, "📦 Заказы к выдаче")
        await self.chat(8, "📦 Заказ №1 · Товар")
        await self.chat(8, "✅ Выдано")
        with self.assertRaises(ValueError):
            self.store.complete_order(1)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 1")["balance"], 10)

        await self.chat(1, "💸 Запросить вывод")
        self.assertFalse(self.store.all("SELECT id FROM withdrawals"))
        self.store.update("UPDATE products SET price = 10 WHERE id = 1")
        await self.chat(1, "🛍 Каталог")
        await self.chat(1, "📁 Игры")
        await self.chat(1, "🛒 Товар")
        await self.chat(1, "1")
        receipt = await self.chat(1, "🛒 Оплатить с баланса")
        self.assertIn("Заказ №2", receipt.text)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 1")["balance"], 0)

    async def test_stars_invoice_checks_owner_amount_and_duplicate_delivery(self):
        await self.chat(1, "/start")
        await self.chat(2, "/start ref_1")
        await self.chat(2, "👤 Кабинет")
        await self.chat(2, "➕ Пополнить баланс")
        amount_prompt = await self.chat(2, f"⭐ {STARS}")
        self.assertEqual([row[0].text for row in amount_prompt.reply_markup.keyboard],
                         ["💰 Баланс", "🏠 Главное меню"])
        response = await self.chat(2, "110")
        self.assertIsInstance(response.reply_markup, InlineKeyboardMarkup)
        button = response.reply_markup.inline_keyboard[0][0]
        self.assertEqual(button.url, "https://t.me/$test-invoice")
        self.assertEqual(button.text, "💳 Оплатить 110 ⭐")
        self.assertNotIn(button.url, response.text)
        invoice = next(call for call in self.telegram.calls if isinstance(call, CreateInvoiceLink))
        fields = {"currency": "XTR", "total_amount": 110, "invoice_payload": invoice.payload}
        for payer, query_id, amount, expected in (
            (3, "wrong-owner", 110, False), (2, "wrong-amount", 10, False),
            (2, "good", 110, True), (2, "duplicate", 110, False),
        ):
            answer = await self.feed({"pre_checkout_query": {
                **fields, "id": query_id, "from": self.telegram_user(payer, None), "total_amount": amount,
            }})
            self.assertIsInstance(answer, AnswerPreCheckoutQuery)
            self.assertEqual(answer.ok, expected)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 1")["balance"], 0)
        payment = {**fields, "telegram_payment_charge_id": "charge", "provider_payment_charge_id": ""}
        await self.chat(2, successful_payment=payment)
        await self.chat(2, successful_payment=payment)
        result = await self.chat(2, "💰 Баланс")
        self.assertIn("Доступно <b>110 ⭐</b>", result.text)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 1")["balance"], 5)
        self.assertEqual(self.store.one("SELECT count(*) AS total FROM outbox")["total"], 1)

    def test_migrates_existing_referral_balance_only_once(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "shop.sqlite3")
            old = Store(path, "XTR")
            old.register(1)
            old.update("UPDATE users SET balance = 20, referral_balance = 15 WHERE id = 1")
            old.db.close()
            for _ in range(2):
                migrated = Store(path, "XTR")
                try:
                    user = migrated.one("SELECT * FROM users WHERE id = 1")
                    self.assertEqual(user["balance"], 35)
                    self.assertEqual(user["referral_balance"], 0)
                finally:
                    migrated.db.close()

    async def test_admin_can_return_an_old_withdrawal_to_internal_balance(self):
        self.store.register(2)
        self.store.update("INSERT INTO withdrawals (user_id, amount, destination) VALUES (2, 10, '@recipient')")
        await self.chat(8, "🛠 Админ-панель")
        await self.chat(8, "📋 Старые заявки на вывод")
        await self.chat(8, "📋 Заявка №1 · @recipient")
        result = await self.chat(8, "❌ Вернуть на баланс")
        self.assertNotIn("📋 Старые заявки на вывод", [button.text for row in result.reply_markup.keyboard for button in row])
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 2")["balance"], 10)
        with self.assertRaises(ValueError):
            self.store.resolve_withdrawal(1, paid=False)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 2")["balance"], 10)

    async def test_wrong_crypto_invoice_amount_is_rejected_and_valid_invoice_credited_once(self):
        self.store.register(1)
        self.store.register(2, referrer=1)
        for provider in (CRYPTOBOT, XROCKET):
            deposit_id = self.store.create_deposit(2, provider, 123)
            self.store.update("UPDATE deposits SET external_id = '1' WHERE id = ?", deposit_id)
            deposit = self.store.one("SELECT * FROM deposits WHERE id = ?", deposit_id)
            invoice = {"id": 1, "invoice_id": 1, "status": "paid", "amount": "2.23",
                       "currency": "USDT", "asset": "USDT", "payload": deposit_id}
            response = {"items": [invoice]} if provider == CRYPTOBOT else invoice
            self.payments.request = AsyncMock(return_value=response)
            with self.assertRaisesRegex(PaymentError, "параметры счёта не совпадают"):
                await self.payments.check_crypto(deposit)
            invoice["amount"] = "1.23"
            await self.payments.check_crypto(deposit)
            await self.payments.check_crypto(deposit)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 2")["balance"], 246)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 1")["balance"], 12)

    async def test_create_crypto_invoices_preserves_provider_ids_and_amount(self):
        store = Store(":memory:", "USDT")
        self.addCleanup(store.db.close)
        store.register(2)
        config = replace(self.config, currency="USDT", cryptobot="test", xrocket="test")
        http = MagicMock()
        payments = Payments(config, store, self.bot, http)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(Shop(config, store, payments, "test_shop_bot").router)
        link = "https://t.me/test_pay?start=invoice"
        for provider, label, host, path, status, wrapper in (
            (CRYPTOBOT, "💎 CryptoBot", "pay.crypt.bot", "api/createInvoice", 200,
             {"ok": True, "result": {"invoice_id": 123, "bot_invoice_url": link}}),
            (XROCKET, "🚀 xRocket", "pay.xrocket.tg", "tg-invoices", 201,
             {"success": True, "data": {"id": 123, "link": link}}),
        ):
            response = http.request.return_value.__aenter__.return_value
            response.status = status
            response.json = AsyncMock(return_value=wrapper)
            await self.chat(2, "👤 Кабинет")
            await self.chat(2, "➕ Пополнить баланс")
            await self.chat(2, label)
            invoice = await self.chat(2, "1,23")
            button = invoice.reply_markup.inline_keyboard[0][0]
            self.assertEqual(button.text, "💳 Оплатить 1.23 USDT")
            self.assertEqual(button.url, link)
            self.assertNotIn(link, invoice.text)
            args, kwargs = http.request.call_args
            self.assertEqual(args, ("POST", f"https://{host}/{path}"))
            self.assertEqual(float(kwargs["json"]["amount"]), 1.23)
            deposit = store.one("SELECT * FROM deposits WHERE provider = ?", provider)
            self.assertEqual(deposit["id"], kwargs["json"]["payload"])
            self.assertEqual(deposit["external_id"], "123")

    async def test_changed_price_requires_new_quote(self):
        self.store.register(2)
        self.store.update("UPDATE users SET balance = 100 WHERE id = 2")
        self.store.update("INSERT INTO categories (name) VALUES ('Игры')")
        self.store.update("INSERT INTO products (category_id, name, description, price) VALUES (1, 'Товар', 'Текст', 10)")
        await self.chat(2, "🛍 Каталог")
        await self.chat(2, "📁 Игры")
        await self.chat(2, "🛒 Товар")
        await self.chat(2, "1")
        self.store.update("UPDATE products SET price = 50 WHERE id = 1")
        reply = await self.chat(2, "🛒 Оплатить с баланса")
        self.assertIn("Товар изменился", reply.text)
        self.assertEqual(self.store.one("SELECT balance FROM users WHERE id = 2")["balance"], 100)

    async def test_admin_actions_are_inaccessible_to_customers(self):
        for message in ("Админ-панель", "Добавить категорию", "Редактировать категорию",
                        "Фальшивая категория"):
            result = await self.chat(2, message)
            self.assertIsInstance(result, SendMessage)
            self.assertNotIn("🛠 Админ-панель", [button.text for row in result.reply_markup.keyboard for button in row])
        self.assertFalse(self.store.all("SELECT id FROM categories"))

    async def test_new_user_gets_welcome_photo_and_main_keyboard_once(self):
        first = await self.chat(2, "/start")
        self.assertIsInstance(first, SendPhoto)
        self.assertEqual(first.photo.path, LOGO)
        self.assertTrue(LOGO.is_file())
        self.assertIn("Привет! Ты в PCHELA.", first.caption)
        self.assertEqual(first.reply_markup.keyboard[0][0].text, "🛍 Каталог")
        again = await self.chat(2, "/start")
        self.assertIsInstance(again, SendMessage)
        self.assertIsInstance(again.reply_markup, ReplyKeyboardMarkup)

    async def test_rename_category_and_show_names_in_two_columns(self):
        for name in ("Игры", "Сервисы", "Подписки"):
            self.store.update("INSERT INTO categories (name) VALUES (?)", name)
        self.store.update("INSERT INTO products (category_id, name, description, price) VALUES (1, 'Товар', 'Текст', 10)")

        await self.chat(8, "🛠 Админ-панель")
        await self.chat(8, "✏️ Редактировать категорию")
        await self.chat(8, "📁 Игры")
        duplicate = await self.chat(8, "Сервисы")
        self.assertIn("таким названием", duplicate.text)
        saved = await self.chat(8, "Аккаунты")
        self.assertIn("Категория переименована.", saved.text)

        catalog = await self.chat(2, "🛍 Каталог")
        self.assertEqual([[button.text for button in row] for row in catalog.reply_markup.keyboard],
                         [["📁 Аккаунты", "📁 Сервисы"], ["📁 Подписки"], ["🏠 Главное меню"]])
        products = await self.chat(2, "📁 Аккаунты")
        self.assertEqual(products.reply_markup.keyboard[0][0].text, "🛒 Товар")

    async def test_products_with_the_same_name_have_distinct_buttons(self):
        self.store.update("INSERT INTO categories (name) VALUES ('Игры')")
        self.store.update("INSERT INTO products (category_id, name, description, price) VALUES (1, 'Аккаунт', 'Первый', 10)")
        self.store.update("INSERT INTO products (category_id, name, description, price) VALUES (1, 'Аккаунт', 'Второй', 20)")
        await self.chat(2, "🛍 Каталог")
        products = await self.chat(2, "📁 Игры")
        first, second = [row[0].text for row in products.reply_markup.keyboard[:2]]
        self.assertEqual(first, "🛒 Аккаунт · 1")
        self.assertEqual(second, "🛒 Аккаунт · 2")
        card = await self.chat(2, second)
        self.assertIn("Второй", card.text)
