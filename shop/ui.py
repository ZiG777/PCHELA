import logging
import re
import sqlite3
from collections import Counter
from html import escape
from pathlib import Path
from uuid import uuid4

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, LinkPreviewOptions, ReplyKeyboardMarkup,
)

from .payments import CRYPTOBOT, STARS, XROCKET, PaymentError

log = logging.getLogger(__name__)
LOGO = Path(__file__).with_name("logo.jpg")
WELCOME = (
    "🐝 <b>Привет! Ты в PCHELA.</b>\n\n"
    "Загляни в <b>Каталог</b>, чтобы выбрать товар. "
    "В <b>Кабинете</b> можно пополнить баланс, посмотреть заказы и пригласить друзей.\n\n"
    "Если нужна помощь, нажми <b>Поддержка</b>."
)
HOME = "Главное меню"
ADMIN = "Админ-панель"
CATALOG = "Каталог"
BALANCE = "Баланс"
PROFILE = "Кабинет"
REFERRAL = "Реферальная программа"
ORDERS = "Мои заказы"
SUPPORT = "Поддержка"
ADD_CATEGORY = "Добавить категорию"
EDIT_CATEGORY = "Редактировать категорию"
ADD_PRODUCT = "Добавить товар"
EDIT_PRODUCT = "Редактировать товар"
PENDING_ORDERS = "Заказы к выдаче"
WITHDRAWALS = "Старые заявки на вывод"
TOP_UP = "Пополнить баланс"
PREVIOUS = "← Предыдущая"
NEXT = "Следующая →"
PAGE_SIZE = 8
BUY = "Оплатить с баланса"
ISSUE = "Выдано"
APPROVE = "Уже выплачено"
REJECT = "Вернуть на баланс"

BUTTON_ICONS = {
    HOME: "🏠", ADMIN: "🛠", CATALOG: "🛍", BALANCE: "💰", PROFILE: "👤",
    REFERRAL: "🤝", ORDERS: "📦", SUPPORT: "💬",
    ADD_CATEGORY: "📁", EDIT_CATEGORY: "✏️", ADD_PRODUCT: "➕", EDIT_PRODUCT: "✏️",
    PENDING_ORDERS: "📦", WITHDRAWALS: "📋", TOP_UP: "➕",
    BUY: "🛒", ISSUE: "✅", APPROVE: "✅", REJECT: "❌",
    CRYPTOBOT: "💎", XROCKET: "🚀", STARS: "⭐",
    "Название": "🏷", "Описание": "📝", "Цена": "💰",
}
BUTTON_LABELS = {name: f"{icon} {name}" for name, icon in BUTTON_ICONS.items()}
BUTTON_ACTIONS = {label: name for name, label in BUTTON_LABELS.items()}
BUTTON_STYLES = {TOP_UP: "primary", BUY: "success", ISSUE: "success", APPROVE: "success", REJECT: "danger"}

CATEGORY_QUERY = "SELECT id, name AS label FROM categories ORDER BY id"
CATEGORY_LISTS = {"catalog", "new_product_category", "edit_category"}
PRODUCT_LISTS = {"products", "edit_product"}
CATEGORY_INPUTS = {"new_category", "rename_category"}

LISTS = {
    "catalog": ("🛍 Каталог", CATEGORY_QUERY, "Категория"),
    "new_product_category": ("Выберите категорию нового товара", CATEGORY_QUERY, "Категория"),
    "edit_category": ("Выберите категорию для редактирования", CATEGORY_QUERY, "Категория"),
    "products": ("🛒 Товары категории", """
        SELECT id, name AS label FROM products WHERE category_id = ? ORDER BY id
    """, "Товар"),
    "my_orders": ("📦 Мои заказы", """
        SELECT id, product AS label FROM orders WHERE buyer = ? ORDER BY id DESC
    """, "Заказ"),
    "edit_product": ("Выберите товар для редактирования", """
        SELECT products.id, categories.name || ' / ' || products.name AS label FROM products
        JOIN categories ON categories.id = products.category_id ORDER BY products.id
    """, "Товар"),
    "pending_orders": ("Заказы к выдаче", """
        SELECT id, product AS label FROM orders WHERE status = 'pending' ORDER BY id
    """, "Заказ"),
    "withdrawals": ("Старые заявки на вывод", """
        SELECT id, destination AS label FROM withdrawals WHERE status = 'pending' ORDER BY id
    """, "Заявка"),
}

ADMIN_SCREENS = {
    *CATEGORY_INPUTS, "edit_category", "new_product_category",
    "new_product_name", "new_product_description", "new_product_price",
    "edit_product", "edit_field", "edit_value", "pending_orders", "issue_order", "withdrawals", "resolve_withdrawal",
}
LIST_ICONS = {"Заказ": "📦", "Заявка": "📋"}


def keyboard(rows):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_LABELS.get(name, name), style=BUTTON_STYLES.get(name)) for name in row]
            for row in rows
        ],
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

    async def show(self, message, state, screen, text, buttons=(), *, button_rows=(), parse_mode=None, **data):
        await state.set_data({"screen": screen, **data})
        rows = [*button_rows, *([name] for name in buttons), [HOME]]
        await self.say(message, text, rows, parse_mode=parse_mode)

    async def home(self, message, state, *, welcome=False):
        await state.clear()
        rows = [[CATALOG, BALANCE], [ORDERS, PROFILE], [SUPPORT]]
        if message.from_user.id in self.config.admins:
            rows.append([ADMIN])
        if welcome:
            try:
                await message.answer_photo(FSInputFile(LOGO), caption=WELCOME,
                                           parse_mode="HTML", reply_markup=keyboard(rows))
                return
            except (OSError, TelegramAPIError) as error:
                log.warning("Не удалось отправить логотип (%s)", type(error).__name__)
            await self.say(message, WELCOME, rows, parse_mode="HTML")
        else:
            await self.say(message, "Выберите раздел в нижнем меню.", rows)

    async def start(self, message, state, command):
        args = command.args or ""
        referrer = int(args[4:]) if re.fullmatch(r"ref_[0-9]{1,18}", args) else None
        _, created = self.store.register(message.from_user.id, referrer)
        await self.home(message, state, welcome=created)

    async def support(self, message, state):
        self.store.register(message.from_user.id)
        await self.show(message, state, "support",
                        f"💬 <b>Поддержка</b>\n\n<a href=\"{escape(self.config.support_url)}\">Написать в Telegram</a>",
                        parse_mode="HTML")

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
        visible = rows[:PAGE_SIZE]
        columns = 1
        if screen in CATEGORY_LISTS:
            columns = 2
            labels = {f"📁 {row['label']}": row["id"] for row in visible}
        elif screen in PRODUCT_LISTS:
            labels = self.product_labels(visible)
        else:
            labels = {
                f"{LIST_ICONS[noun]} {noun} №{row['id']} · {' '.join(row['label'].split())[:44]}": row["id"]
                for row in visible
            }
        names = list(labels)
        button_rows = [names[index:index + columns] for index in range(0, len(names), columns)]
        navigation = {}
        if page:
            navigation[PREVIOUS] = page - 1
        if len(rows) > PAGE_SIZE:
            navigation[NEXT] = page + 1
        heading = f"<b>{title}</b>"
        await self.show(message, state, screen, heading if labels else f"{heading}\n\nЗдесь пока пусто.",
                        navigation, button_rows=button_rows, parse_mode="HTML",
                        labels=labels, navigation=navigation, params=list(params))

    @staticmethod
    def product_labels(products):
        names = [f"🛒 {' '.join(product['label'].split())[:44]}" for product in products]
        counts = Counter(names)
        labels = {}
        for name, product in zip(names, products):
            product_id = product["id"]
            label = name
            if counts[name] > 1:
                label = f"{name} · {product_id}"
                while label in counts or label in labels:
                    label += " ·"
            labels[label] = product_id
        return labels

    def order_message(self, order, admin=False):
        status = "✅ Выдан" if order["status"] == "issued" else "⏳ Ожидает выдачи"
        info = (f"📦 <b>Заказ №{order['id']}</b>\n{status}\n\n"
                f"{escape(order['product'])} × {order['quantity']}\n"
                f"Оплачено <b>{self.config.money(order['total'])}</b>")
        if admin:
            info += f"\n\nПокупатель {contact(order['username'], order['buyer'])}"
            if not order["username"]:
                info += "\nУ покупателя нет username. Ему отправлена ссылка на администратора."
            return info + f"\nID покупателя {order['buyer']}"
        if not order["username"] and order["status"] == "pending":
            return info + (f"\n\n<a href=\"{escape(self.config.admin_url)}\">Напишите администратору</a>"
                           " и сообщите номер заказа.")
        return info

    def withdrawal_message(self, request):
        return (f"💸 <b>Заявка на вывод №{request['id']}</b>\n\n"
                f"Сумма <b>{self.config.money(request['amount'])}</b>\n"
                f"Контакт {contact(request['username'], request['user_id'])}\n"
                f"Реквизиты {escape(request['destination'])}")

    async def profile(self, message, state, user):
        deposited = self.store.one("""
            SELECT COALESCE(SUM(amount), 0) AS total FROM deposits WHERE user_id = ? AND status = 'paid'
        """, user["id"])["total"]
        orders = self.store.one("""
            SELECT COUNT(*) AS count, COALESCE(SUM(total), 0) AS spent FROM orders WHERE buyer = ?
        """, user["id"])
        await self.show(
            message, state, "profile",
            f"👤 <b>Личный кабинет</b>\n\n"
            f"🆔 Telegram ID · <code>{user['id']}</code>\n"
            f"💰 Баланс · <b>{self.config.money(user['balance'])}</b>\n"
            f"💳 Пополнено за всё время · <b>{self.config.money(deposited)}</b>\n\n"
            f"📦 Заказов · {orders['count']}\n"
            f"🧾 Потрачено · {self.config.money(orders['spent'])}",
            button_rows=[[ORDERS, REFERRAL], [TOP_UP]], parse_mode="HTML",
        )

    async def referral(self, message, state, user):
        count = self.store.one("SELECT count(*) AS count FROM users WHERE referrer = ?", user["id"])["count"]
        await self.show(message, state, "referral",
                        f"🤝 <b>Реферальная программа</b>\n\n"
                        f"Приглашено <b>{count}</b>\n"
                        f"Бонус <b>{self.config.referral_percent}%</b> от подтверждённых пополнений приглашённых "
                        "сразу зачисляется на внутренний баланс и доступен для покупок.\n\n"
                        f"Ваша ссылка\nhttps://t.me/{escape(self.bot_username)}?start=ref_{user['id']}",
                        [PROFILE], parse_mode="HTML")

    async def admin(self, message, state, text="Админ-панель"):
        rows = [[ADD_CATEGORY, EDIT_CATEGORY], [ADD_PRODUCT, EDIT_PRODUCT], [PENDING_ORDERS]]
        if self.store.one("SELECT 1 FROM withdrawals WHERE status = 'pending' LIMIT 1"):
            rows[-1].append(WITHDRAWALS)
        await self.show(message, state, "admin", f"🛠 <b>{escape(text)}</b>", button_rows=rows, parse_mode="HTML")

    async def message(self, message, state):
        user, _ = self.store.register(message.from_user.id)
        text = message.text.strip()
        action = BUTTON_ACTIONS.get(text, text)
        data = await state.get_data()
        screen = data.get("screen")
        is_admin = user["id"] in self.config.admins
        try:
            if action == HOME or (screen in ADMIN_SCREENS and not is_admin):
                await self.home(message, state)
            elif screen in CATEGORY_INPUTS:
                await self.admin_input(message, state, screen, data, text)
            elif action == SUPPORT:
                await self.support(message, state)
            elif action == CATALOG:
                await self.listing(message, state, "catalog")
            elif action == BALANCE:
                await self.show(message, state, "balance",
                                f"💰 <b>Баланс</b>\n\nДоступно <b>{self.config.money(user['balance'])}</b>",
                                [TOP_UP], parse_mode="HTML")
            elif action == PROFILE:
                await self.profile(message, state, user)
            elif action == REFERRAL:
                await self.referral(message, state, user)
            elif action == ORDERS:
                await self.listing(message, state, "my_orders", user["id"])
            elif is_admin and action == ADMIN:
                await self.admin(message, state)
            elif screen in LISTS:
                await self.choose(message, state, screen, data, text)
            elif screen in ADMIN_SCREENS or (screen == "admin" and is_admin):
                value = action if screen in {"admin", "edit_field", "issue_order", "resolve_withdrawal"} else text
                await self.admin_input(message, state, screen, data, value)
            else:
                value = action if screen in {"balance", "profile", "provider", "purchase"} else text
                await self.customer_input(message, state, screen, data, value, user)
        except ValueError as error:
            await self.say(message, str(error))
        except sqlite3.IntegrityError:
            await self.say(message, "Не удалось сохранить запись. Проверьте, нет ли категории с таким названием.")
        except PaymentError as error:
            log.error("Ошибка платежа. %s", error)
            await self.say(message, f"Платёжная система сейчас не смогла обработать запрос.\n{self.config.support_url}")
        except (TelegramAPIError, KeyError, TypeError) as error:
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
                                f"🛒 <b>{escape(product['name'])}</b>\n\n{escape(product['description'])}\n\n"
                                f"Цена за единицу <b>{self.config.money(product['price'])}</b>\n\n"
                                "Выберите количество или введите число от 1 до 999.",
                                button_rows=[["1", "2"], ["5", "10"]], parse_mode="HTML", product=dict(product))
            else:
                await self.show(message, state, "edit_field",
                                f"{product['name']}\n{product['description']}\n{self.config.money(product['price'])}",
                                ["Название", "Описание", "Цена"], item=item)
        elif screen == "my_orders":
            order = self.store.one("SELECT * FROM orders WHERE id = ? AND buyer = ?", item, message.from_user.id)
            await self.show(message, state, "receipt", self.order_message(order), [ORDERS], parse_mode="HTML")
        elif screen == "new_product_category":
            await self.show(message, state, "new_product_name", "Введите название товара.", category=item)
        elif screen == "edit_category":
            category = self.store.one("SELECT name FROM categories WHERE id = ?", item)
            await self.show(message, state, "rename_category",
                            f"Категория «{category['name']}». Введите новое название.", item=item)
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
                            f"🧾 <b>Подтверждение заказа</b>\n\n{escape(product['name'])} × {quantity}\n\n"
                            f"К оплате <b>{total}</b>\nВаш баланс {self.config.money(user['balance'])}",
                            [BUY, BALANCE], parse_mode="HTML", quote=quote)
        elif screen == "purchase" and text == BUY:
            order = self.store.purchase(user["id"], message.from_user.username, data["quote"],
                                        self.config.admins,
                                        lambda order: self.order_message(order, admin=True))
            await self.show(message, state, "receipt", self.order_message(order), [ORDERS], parse_mode="HTML")
        elif screen in {"balance", "profile"} and text == TOP_UP:
            await self.show(message, state, "provider", "💳 <b>Пополнение баланса</b>\n\nВыберите способ оплаты.",
                            self.payments.providers, parse_mode="HTML")
        elif screen == "provider" and text in self.payments.providers:
            await self.show(message, state, "deposit",
                            f"<b>{BUTTON_LABELS[text]}</b>\n\nВведите сумму пополнения в {self.config.currency}.",
                            [BALANCE], parse_mode="HTML", provider=text)
        elif screen == "deposit":
            amount = self.config.parse_amount(text)
            provider = data["provider"]
            url = await self.payments.create(user["id"], provider, amount)
            total = self.config.money(amount)
            await state.set_data({"screen": "invoice"})
            await message.answer(
                f"💳 <b>Счёт на пополнение</b>\n\n"
                f"Способ оплаты · {provider}\nСумма · <b>{total}</b>\n\n"
                "Баланс обновится после подтверждения платежа.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"💳 Оплатить {total}", url=url, style="success")],
                ]),
            )
        elif screen is None:
            await self.home(message, state)
        else:
            raise ValueError("Выберите действие в нижнем меню.")

    async def admin_input(self, message, state, screen, data, text):
        if screen == "admin":
            destinations = {EDIT_CATEGORY: "edit_category", ADD_PRODUCT: "new_product_category", EDIT_PRODUCT: "edit_product",
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
        elif screen == "rename_category":
            self.validate_text(text, 80)
            self.store.update("UPDATE categories SET name = ? WHERE id = ?", text, data["item"])
            await self.admin(message, state, "Категория переименована.")
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
