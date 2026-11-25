import logging
import re
import sqlite3
from html import escape
from uuid import uuid4

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import KeyboardButton, LinkPreviewOptions, ReplyKeyboardMarkup

from .payments import PaymentError

log = logging.getLogger(__name__)
HOME = "Главное меню"
ADMIN = "Админ-панель"
CATALOG = "Каталог"
BALANCE = "Баланс"
REFERRAL = "Реферальная программа"
ORDERS = "Мои заказы"
SUPPORT = "Поддержка"
ADD_CATEGORY = "Добавить категорию"
ADD_PRODUCT = "Добавить товар"
EDIT_PRODUCT = "Редактировать товар"
PENDING_ORDERS = "Заказы к выдаче"
WITHDRAWALS = "Заявки на вывод"
TOP_UP = "Пополнить баланс"
WITHDRAW = "Запросить вывод"
PREVIOUS = "← Предыдущая"
NEXT = "Следующая →"
PAGE_SIZE = 8
BUY = "Оплатить с баланса"
ISSUE = "Выдано"
APPROVE = "Выплата выполнена"
REJECT = "Отклонить заявку"

LISTS = {
    "catalog": ("Категории", "SELECT id, name AS label FROM categories ORDER BY id", "Категория"),
    "products": ("Товары категории", """
        SELECT id, name AS label FROM products WHERE category_id = ? ORDER BY id
    """, "Товар"),
    "my_orders": ("Мои заказы", """
        SELECT id, product AS label FROM orders WHERE buyer = ? ORDER BY id DESC
    """, "Заказ"),
    "new_product_category": ("Выберите категорию нового товара", """
        SELECT id, name AS label FROM categories ORDER BY id
    """, "Категория"),
    "edit_product": ("Выберите товар для редактирования", """
        SELECT products.id, categories.name || ' / ' || products.name AS label FROM products
        JOIN categories ON categories.id = products.category_id ORDER BY products.id
    """, "Товар"),
    "pending_orders": ("Заказы к выдаче", """
        SELECT id, product AS label FROM orders WHERE status = 'pending' ORDER BY id
    """, "Заказ"),
    "withdrawals": ("Заявки на вывод", """
        SELECT id, destination AS label FROM withdrawals WHERE status = 'pending' ORDER BY id
    """, "Заявка"),
}

ADMIN_SCREENS = {
    "new_category", "new_product_category", "new_product_name", "new_product_description", "new_product_price",
    "edit_product", "edit_field", "edit_value", "pending_orders", "issue_order", "withdrawals", "resolve_withdrawal",
}


def keyboard(rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=name) for name in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def contact(username, user_id):
    if username:
        name = escape(username)
        return f'<a href="https://t.me/{name}">@{name}</a>'
    return f'<a href="tg://user?id={user_id}">Открыть профиль покупателя</a>'


class Shop:
    def __init__(self, config, store, payments, bot_username):
        self.config = config
        self.store = store
        self.payments = payments
        self.bot_username = bot_username
        self.router = Router()
        self.router.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user)
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.support, Command("paysupport"))
        self.router.message.register(self.paid, F.successful_payment)
        self.router.message.register(self.message, F.text)
        self.router.pre_checkout_query.register(self.pre_checkout)

    async def say(self, message, text, rows=None, *, parse_mode=None):
        await message.answer(text, reply_markup=keyboard(rows) if rows else None,
                             parse_mode=parse_mode,
                             link_preview_options=LinkPreviewOptions(is_disabled=True))

    async def show(self, message, state, screen, text, buttons=(), *, parse_mode=None, **data):
        await state.set_data({"screen": screen, **data})
        await self.say(message, text, [[name] for name in buttons] + [[HOME]], parse_mode=parse_mode)

    async def home(self, message, state, text="Выберите раздел в нижнем меню."):
        await state.clear()
        rows = [[CATALOG, BALANCE], [ORDERS, REFERRAL], [SUPPORT]]
        if message.from_user.id in self.config.admins:
            rows.append([ADMIN])
        await self.say(message, text, rows)

    async def start(self, message, state, command):
        args = command.args or ""
        referrer = int(args[4:]) if re.fullmatch(r"ref_[0-9]{1,18}", args) else None
        self.store.register(message.from_user.id, referrer)
        await self.home(message, state, "Добро пожаловать в магазин.")

    async def support(self, message, state):
        self.store.register(message.from_user.id)
        await self.show(message, state, "support", f"Поддержка и вопросы по платежам\n{self.config.support_url}")

    async def pre_checkout(self, query):
        valid = self.payments.reserve_stars(query.from_user.id, query.invoice_payload, query.currency,
                                            query.total_amount, query.id)
        await query.answer(ok=valid, error_message=None if valid else "Создайте новый счёт в разделе «Баланс».")

    async def paid(self, message):
        payment = message.successful_payment
        if self.payments.stars_match(message.from_user.id, payment.invoice_payload, payment.currency,
                                     payment.total_amount):
            self.payments.credit(payment.invoice_payload, payment.telegram_payment_charge_id)
        else:
            previous = self.store.one("SELECT status, external_id FROM deposits WHERE id = ?", payment.invoice_payload)
            if not previous or previous["status"] != "paid" or previous["external_id"] != payment.telegram_payment_charge_id:
                log.error("Не удалось сопоставить платёж Stars со счётом %s", payment.invoice_payload)
                await self.say(message, f"Обратитесь в поддержку по поводу платежа.\n{self.config.support_url}")

    async def listing(self, message, state, screen, *params, page=0):
        title, query, noun = LISTS[screen]
        rows = self.store.all(f"{query} LIMIT ? OFFSET ?", *params, PAGE_SIZE + 1, page * PAGE_SIZE)
        if page and not rows:
            return await self.listing(message, state, screen, *params, page=page - 1)
        labels = {
            f"{noun} №{row['id']} · {' '.join(row['label'].split())[:44]}": row["id"]
            for row in rows[:PAGE_SIZE]
        }
        navigation = {}
        if page:
            navigation[PREVIOUS] = page - 1
        if len(rows) > PAGE_SIZE:
            navigation[NEXT] = page + 1
        await self.show(message, state, screen, title if labels else f"{title}\nЗдесь пока пусто.",
                        [*labels, *navigation], labels=labels, navigation=navigation, params=list(params))

    def order_message(self, order, admin=False):
        status = "выдан" if order["status"] == "issued" else "ожидает выдачи"
        info = (f"Заказ №{order['id']} · {status}\n"
                f"{order['product']} × {order['quantity']}\n"
                f"Оплачено {self.config.money(order['total'])}")
        if admin:
            info = escape(info) + f"\nПокупатель {contact(order['username'], order['buyer'])}"
            if not order["username"]:
                info += "\nУ покупателя нет username. Ему отправлена ссылка на администратора."
            return info + f"\nID покупателя {order['buyer']}"
        if not order["username"] and order["status"] == "pending":
            return info + f"\nНапишите администратору и сообщите номер заказа.\n{self.config.admin_url}"
        return info

    def withdrawal_message(self, request):
        return (f"Заявка на вывод №{request['id']}\n"
                f"Сумма {self.config.money(request['amount'])}\n"
                f"Контакт {contact(request['username'], request['user_id'])}\n"
                f"Реквизиты {escape(request['destination'])}")

    async def referral(self, message, state, user):
        count = self.store.one("SELECT count(*) AS count FROM users WHERE referrer = ?", user["id"])["count"]
        await self.show(message, state, "referral",
                        f"Ваша ссылка\nhttps://t.me/{self.bot_username}?start=ref_{user['id']}\n\n"
                        f"Приглашено {count}\n"
                        f"Вознаграждение {self.config.referral_percent}% от подтверждённых пополнений приглашённых пользователей\n"
                        f"Доступно к выводу {self.config.money(user['referral_balance'])}", [WITHDRAW])

    async def admin(self, message, state, text="Админ-панель"):
        await self.show(message, state, "admin", text,
                        [ADD_CATEGORY, ADD_PRODUCT, EDIT_PRODUCT, PENDING_ORDERS, WITHDRAWALS])

    async def message(self, message, state):
        user = self.store.register(message.from_user.id)
        text = message.text.strip()
        data = await state.get_data()
        screen = data.get("screen")
        is_admin = user["id"] in self.config.admins
        try:
            if text == HOME or (screen in ADMIN_SCREENS and not is_admin):
                await self.home(message, state)
            elif text == SUPPORT:
                await self.support(message, state)
            elif text == CATALOG:
                await self.listing(message, state, "catalog")
            elif text == BALANCE:
                await self.show(message, state, "balance", f"Баланс {self.config.money(user['balance'])}", [TOP_UP])
            elif text == REFERRAL:
                await self.referral(message, state, user)
            elif text == ORDERS:
                await self.listing(message, state, "my_orders", user["id"])
            elif is_admin and text == ADMIN:
                await self.admin(message, state)
            elif screen in LISTS:
                await self.choose(message, state, screen, data, text)
            elif screen in ADMIN_SCREENS or (screen == "admin" and is_admin):
                await self.admin_input(message, state, screen, data, text)
            else:
                await self.customer_input(message, state, screen, data, text, user)
        except ValueError as error:
            await self.say(message, str(error))
        except sqlite3.IntegrityError:
            await self.say(message, "Не удалось сохранить запись. Проверьте, нет ли категории с таким названием.")
        except (PaymentError, TelegramAPIError, KeyError, TypeError) as error:
            log.error("Не удалось обработать действие %s (%s)", screen, type(error).__name__)
            await self.say(message, f"Действие временно недоступно.\n{self.config.support_url}")

    async def choose(self, message, state, screen, data, text):
        if text in data["navigation"]:
            return await self.listing(message, state, screen, *data["params"], page=data["navigation"][text])
        item = data["labels"].get(text)
        if not item:
            raise ValueError("Выберите пункт в нижнем меню.")
        if screen == "catalog":
            await self.listing(message, state, "products", item)
        elif screen in {"products", "edit_product"}:
            product = self.store.one("SELECT * FROM products WHERE id = ?", item)
            if screen == "products":
                await self.show(message, state, "quantity",
                                f"{product['name']}\n\n{product['description']}\n\n"
                                f"Цена за единицу {self.config.money(product['price'])}\n"
                                "Выберите количество или введите число от 1 до 999.",
                                ["1", "2", "5", "10"], product=dict(product))
            else:
                await self.show(message, state, "edit_field",
                                f"{product['name']}\n{product['description']}\n{self.config.money(product['price'])}",
                                ["Название", "Описание", "Цена"], item=item)
        elif screen == "my_orders":
            order = self.store.one("SELECT * FROM orders WHERE id = ? AND buyer = ?", item, message.from_user.id)
            await self.show(message, state, "receipt", self.order_message(order), [ORDERS])
        elif screen == "new_product_category":
            await self.show(message, state, "new_product_name", "Введите название товара.", category=item)
        elif screen == "pending_orders":
            order = self.store.one("SELECT * FROM orders WHERE id = ?", item)
            buttons = [ISSUE] if order["status"] == "pending" else []
            await self.show(message, state, "issue_order", self.order_message(order, admin=True), buttons,
                            parse_mode="HTML", item=item)
        elif screen == "withdrawals":
            request = self.store.one("SELECT * FROM withdrawals WHERE id = ?", item)
            buttons = [APPROVE, REJECT] if request["status"] == "pending" else []
            await self.show(message, state, "resolve_withdrawal",
                            self.withdrawal_message(request) + f"\nСтатус {self.withdrawal_status(request['status'])}",
                            buttons, parse_mode="HTML", item=item)

    @staticmethod
    def withdrawal_status(status):
        return {"pending": "ожидает обработки", "paid": "выплачена", "rejected": "отклонена"}[status]

    async def customer_input(self, message, state, screen, data, text, user):
        if screen == "quantity":
            if not re.fullmatch(r"[0-9]{1,3}", text) or not int(text):
                raise ValueError("Введите количество от 1 до 999.")
            quantity = int(text)
            product = data["product"]
            quote = {**product, "product_id": product["id"], "quantity": quantity, "key": uuid4().hex}
            total = self.config.money(quantity * product["price"])
            await self.show(message, state, "purchase",
                            f"{product['name']} × {quantity}\nСумма заказа {total}\n"
                            f"Ваш баланс {self.config.money(user['balance'])}", [BUY, BALANCE], quote=quote)
        elif screen == "purchase" and text == BUY:
            order = self.store.purchase(user["id"], message.from_user.username, data["quote"],
                                        self.config.admins,
                                        lambda order: self.order_message(order, admin=True))
            await self.show(message, state, "receipt", self.order_message(order), [ORDERS])
        elif screen == "balance" and text == TOP_UP:
            await self.show(message, state, "provider", "Выберите способ оплаты.", self.payments.providers)
        elif screen == "provider" and text in self.payments.providers:
            await self.show(message, state, "deposit", f"Введите сумму пополнения ({self.config.currency}).", provider=text)
        elif screen == "deposit":
            amount = self.config.parse_amount(text)
            url = await self.payments.create(user["id"], data["provider"], amount)
            await self.show(message, state, "invoice",
                            f"Счёт на {self.config.money(amount)}\n{url}\n"
                            "После подтверждения оплаты баланс обновится автоматически.", [BALANCE])
        elif screen == "referral" and text == WITHDRAW:
            if not user["referral_balance"]:
                raise ValueError("На реферальном балансе пока нет средств.")
            await self.show(message, state, "withdraw_amount", "Введите сумму вывода.")
        elif screen == "withdraw_amount":
            amount = self.config.parse_amount(text)
            if amount > user["referral_balance"]:
                raise ValueError("Недостаточно средств на реферальном балансе.")
            await self.show(message, state, "withdraw_destination",
                            "Укажите реквизиты для выплаты или контакт для согласования способа перевода.", amount=amount)
        elif screen == "withdraw_destination":
            if not text or len(text) > 300:
                raise ValueError("Введите реквизиты длиной до 300 символов.")
            request_id = self.store.open_withdrawal(user["id"], message.from_user.username, data["amount"],
                                                    text, self.config.admins, self.withdrawal_message)
            await self.show(message, state, "withdraw_receipt",
                            f"Заявка №{request_id} отправлена администратору.", [REFERRAL])
        elif screen is None:
            await self.home(message, state)
        else:
            raise ValueError("Выберите действие в нижнем меню.")

    async def admin_input(self, message, state, screen, data, text):
        if screen == "admin":
            destinations = {ADD_PRODUCT: "new_product_category", EDIT_PRODUCT: "edit_product",
                            PENDING_ORDERS: "pending_orders", WITHDRAWALS: "withdrawals"}
            if text == ADD_CATEGORY:
                await self.show(message, state, "new_category", "Введите название категории.")
            elif text in destinations:
                await self.listing(message, state, destinations[text])
            else:
                raise ValueError("Выберите действие в нижнем меню.")
        elif screen == "new_category":
            self.validate_text(text, 80)
            self.store.update("INSERT INTO categories (name) VALUES (?)", text)
            await self.admin(message, state, "Категория добавлена.")
        elif screen == "new_product_name":
            self.validate_text(text, 100)
            await self.show(message, state, "new_product_description", "Введите описание товара.",
                            category=data["category"], name=text)
        elif screen == "new_product_description":
            self.validate_text(text, 2000)
            await self.show(message, state, "new_product_price", f"Введите цену ({self.config.currency}).",
                            category=data["category"], name=data["name"], description=text)
        elif screen == "new_product_price":
            self.store.update("INSERT INTO products (category_id, name, description, price) VALUES (?, ?, ?, ?)",
                              data["category"], data["name"], data["description"], self.config.parse_amount(text))
            await self.admin(message, state, "Товар добавлен.")
        elif screen == "edit_field":
            fields = {"Название": "name", "Описание": "description", "Цена": "price"}
            if text not in fields:
                raise ValueError("Выберите поле в нижнем меню.")
            await self.show(message, state, "edit_value", f"Введите новое значение поля «{text}».",
                            item=data["item"], field=fields[text])
        elif screen == "edit_value":
            field = data["field"]
            if field == "price":
                value = self.config.parse_amount(text)
            else:
                self.validate_text(text, {"name": 100, "description": 2000}[field])
                value = text
            self.store.update(f"UPDATE products SET {field} = ? WHERE id = ?", value, data["item"])
            await self.admin(message, state, "Товар обновлён.")
        elif screen == "issue_order" and text == ISSUE:
            self.store.complete_order(data["item"])
            await self.admin(message, state, f"Заказ №{data['item']} отмечен как выданный.")
        elif screen == "resolve_withdrawal" and text in (APPROVE, REJECT):
            self.store.resolve_withdrawal(data["item"], text == APPROVE)
            await self.admin(message, state, f"Заявка №{data['item']} обработана.")
        else:
            raise ValueError("Выберите действие в нижнем меню.")

    @staticmethod
    def validate_text(text, limit):
        if not text or len(text) > limit:
            raise ValueError(f"Введите текст длиной от 1 до {limit} символов.")
