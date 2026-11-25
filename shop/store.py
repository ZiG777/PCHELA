import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, path: str, currency: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                referrer INTEGER REFERENCES users(id),
                balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
                referral_balance INTEGER NOT NULL DEFAULT 0 CHECK (referral_balance >= 0)
            );
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id),
                name TEXT NOT NULL, description TEXT NOT NULL, price INTEGER NOT NULL CHECK (price > 0)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY, buyer INTEGER NOT NULL REFERENCES users(id),
                username TEXT, product TEXT NOT NULL, quantity INTEGER NOT NULL,
                total INTEGER NOT NULL, reward INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', purchase_key TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS deposits (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                provider TEXT NOT NULL, amount INTEGER NOT NULL,
                external_id TEXT, url TEXT, checkout_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
                next_poll INTEGER NOT NULL DEFAULT 0,
                UNIQUE (provider, external_id)
            );
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                username TEXT, amount INTEGER NOT NULL, destination TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, recipient INTEGER NOT NULL,
                message TEXT NOT NULL, parse_mode TEXT, next_attempt INTEGER NOT NULL DEFAULT 0
            );
        """)
        with self.write():
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('currency', ?)", (currency,))
            if self.one("SELECT value FROM settings WHERE name = 'currency'")["value"] != currency:
                raise ValueError("Валюта базы отличается от SHOP_CURRENCY. Укажите другую DATABASE_PATH.")

    @contextmanager
    def write(self):
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            yield

    def one(self, query, *args):
        return self.db.execute(query, args).fetchone()

    def all(self, query, *args):
        return self.db.execute(query, args).fetchall()

    def register(self, user_id, referrer=None):
        with self.write():
            if referrer == user_id or not self.one("SELECT id FROM users WHERE id = ?", referrer):
                referrer = None
            created = self.db.execute("INSERT OR IGNORE INTO users (id, referrer) VALUES (?, ?)",
                                      (user_id, referrer)).rowcount == 1
            return self.one("SELECT * FROM users WHERE id = ?", user_id), created

    def notify(self, recipients, message, parse_mode=None):
        self.db.executemany("INSERT INTO outbox (recipient, message, parse_mode) VALUES (?, ?, ?)",
                            ((recipient, message, parse_mode) for recipient in recipients))

    def update(self, query, *args):
        with self.write():
            return self.db.execute(query, args).lastrowid

    def purchase(self, buyer, username, quote, admins, notice):
        with self.write():
            prior = self.one("SELECT * FROM orders WHERE purchase_key = ? AND buyer = ?", quote["key"], buyer)
            if prior:
                return prior
            product = self.one("SELECT * FROM products WHERE id = ?", quote["product_id"])
            if not product or any(product[field] != quote[field] for field in ("name", "description", "price")):
                raise ValueError("Товар изменился. Выберите его заново в каталоге.")
            quantity = quote["quantity"]
            total = product["price"] * quantity
            debit = self.db.execute("UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?",
                                    (total, buyer, total))
            if not debit.rowcount:
                raise ValueError("Недостаточно средств. Пополните баланс и снова выберите товар.")
            # Keep the reward column compatible with databases created before deposit rewards.
            order_id = self.db.execute("""
                INSERT INTO orders (buyer, username, product, quantity, total, reward, purchase_key)
                VALUES (?, ?, ?, ?, ?, 0, ?)
            """, (buyer, username, product["name"], quantity, total, quote["key"])).lastrowid
            order = self.one("SELECT * FROM orders WHERE id = ?", order_id)
            self.notify(admins, notice(order), "HTML")
            return order

    def complete_order(self, order_id):
        with self.write():
            order = self.one("SELECT * FROM orders WHERE id = ? AND status = 'pending'", order_id)
            if not order:
                raise ValueError("Этот заказ уже выдан или не найден.")
            self.db.execute("UPDATE orders SET status = 'issued' WHERE id = ?", (order_id,))
            self.notify([order["buyer"]], f"Заказ №{order_id} отмечен как выданный.")

    def open_withdrawal(self, user_id, username, amount, destination, admins, notice):
        with self.write():
            debit = self.db.execute("""
                UPDATE users SET referral_balance = referral_balance - ?
                WHERE id = ? AND referral_balance >= ?
            """, (amount, user_id, amount))
            if not debit.rowcount:
                raise ValueError("Недостаточно средств на реферальном балансе.")
            request_id = self.db.execute("""
                INSERT INTO withdrawals (user_id, username, amount, destination) VALUES (?, ?, ?, ?)
            """, (user_id, username, amount, destination)).lastrowid
            request = self.one("SELECT * FROM withdrawals WHERE id = ?", request_id)
            self.notify(admins, notice(request), "HTML")
            return request_id

    def resolve_withdrawal(self, request_id, paid):
        with self.write():
            request = self.one("SELECT * FROM withdrawals WHERE id = ? AND status = 'pending'", request_id)
            if not request:
                raise ValueError("Эта заявка уже обработана или не найдена.")
            self.db.execute("UPDATE withdrawals SET status = ? WHERE id = ?",
                            ("paid" if paid else "rejected", request_id))
            if not paid:
                self.db.execute("UPDATE users SET referral_balance = referral_balance + ? WHERE id = ?",
                                (request["amount"], request["user_id"]))
            status = "выплачена" if paid else "отклонена, сумма возвращена на реферальный баланс"
            self.notify([request["user_id"]], f"Заявка на вывод №{request_id} {status}.")

    def create_deposit(self, user_id, provider, amount):
        deposit_id = uuid4().hex
        self.update("INSERT INTO deposits (id, user_id, provider, amount) VALUES (?, ?, ?, ?)",
                    deposit_id, user_id, provider, amount)
        return deposit_id

    def credit(self, deposit_id, external_id, referral_percent, notice):
        with self.write():
            deposit = self.one("SELECT * FROM deposits WHERE id = ? AND status = 'pending'", deposit_id)
            if not deposit or deposit["external_id"] not in (None, external_id):
                return False
            if self.one("SELECT id FROM deposits WHERE provider = ? AND external_id = ? AND id != ?",
                        deposit["provider"], external_id, deposit_id):
                return False
            self.db.execute("UPDATE deposits SET status = 'paid', external_id = ? WHERE id = ?",
                            (external_id, deposit_id))
            self.db.execute("UPDATE users SET balance = balance + ? WHERE id = ?",
                            (deposit["amount"], deposit["user_id"]))
            reward = deposit["amount"] * referral_percent // 100
            if reward:
                self.db.execute("""
                    UPDATE users SET referral_balance = referral_balance + ?
                    WHERE id = (SELECT referrer FROM users WHERE id = ?)
                """, (reward, deposit["user_id"]))
            self.notify([deposit["user_id"]], notice(deposit["amount"]))
            return True

    def poll_batch(self, providers):
        if not providers:
            return []
        placeholders = ",".join("?" for _ in providers)
        return self.all(f"""
            SELECT * FROM deposits WHERE provider IN ({placeholders}) AND status = 'pending'
            AND external_id IS NOT NULL AND next_poll <= ? ORDER BY next_poll LIMIT 20
        """, *providers, int(time.time()))
