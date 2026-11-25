import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    admins: tuple[int, ...]
    admin_url: str
    support_url: str
    referral_percent: int
    database: str
    currency: str
    cryptobot: str
    xrocket: str
    testnet: bool

    @classmethod
    def load(cls):
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        admins = tuple(dict.fromkeys(int(value) for value in os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else ()
        admin_url = os.getenv("ADMIN_URL", "")
        support_url = os.getenv("SUPPORT_URL", "")
        referral_percent = int(os.getenv("REFERRAL_PERCENT", "5"))
        currency = os.getenv("SHOP_CURRENCY", "XTR").upper()
        cryptobot = os.getenv("CRYPTOBOT_TOKEN", "").strip()
        xrocket = os.getenv("XROCKET_TOKEN", "").strip()
        if not token or not admins or min(admins) <= 0:
            raise ValueError("Укажите BOT_TOKEN и ADMIN_IDS (числовые ID через запятую).")
        for name, url in (("ADMIN_URL", admin_url), ("SUPPORT_URL", support_url)):
            if not re.fullmatch(r"https://t\.me/[A-Za-z0-9_]{5,32}", url):
                raise ValueError(f"{name} должен содержать ссылку https://t.me/username.")
        if not 0 <= referral_percent <= 100:
            raise ValueError("REFERRAL_PERCENT должен быть от 0 до 100.")
        if currency not in {"XTR", "USDT"} or (currency == "USDT" and not (cryptobot or xrocket)):
            raise ValueError("Укажите SHOP_CURRENCY=XTR или настройте хотя бы один провайдер для USDT.")
        return cls(token, admins, admin_url, support_url, referral_percent,
                   os.getenv("DATABASE_PATH", "data/shop.sqlite3"), currency, cryptobot, xrocket,
                   os.getenv("CRYPTOBOT_TESTNET", "false").lower() == "true")

    def money(self, amount: int) -> str:
        if self.currency == "XTR":
            return f"{amount} ⭐"
        whole, cents = divmod(amount, 100)
        return f"{whole}.{cents:02d} USDT"

    def parse_amount(self, value: str) -> int:
        pattern = r"[0-9]{1,6}" if self.currency == "XTR" else r"[0-9]{1,6}(?:[.,][0-9]{1,2})?"
        if not re.fullmatch(pattern, value):
            raise ValueError("Введите сумму целым числом." if self.currency == "XTR"
                             else "Введите сумму с точностью до двух знаков после запятой.")
        whole, _, fraction = value.replace(",", ".").partition(".")
        amount = int(whole) if self.currency == "XTR" else int(whole) * 100 + int(fraction.ljust(2, "0"))
        if amount <= 0:
            raise ValueError("Сумма должна быть больше нуля.")
        return amount
