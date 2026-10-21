import os
from datetime import timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

BISHKEK = timezone(timedelta(hours=6))  # без перехода на летнее время

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Firebase: JSON сервисного аккаунта строкой ИЛИ путь к файлу
FIREBASE_CREDENTIALS = os.getenv("FIREBASE_CREDENTIALS", "").strip()
FIREBASE_CREDENTIALS_FILE = (
    os.getenv("FIREBASE_CREDENTIALS_FILE") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or ""
).strip()


def _ids(raw: str) -> frozenset[int]:
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return frozenset(out)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))

# необязательно: ключ 2ГИС API (platform.2gis.ru) — самый точный поиск адресов
DGIS_KEY = os.getenv("DGIS_KEY", "").strip()

# Напоминания: через сколько минут в статусе заказ считается «зависшим»
REMIND_AFTER_MIN = {
    s: _int(f"REMIND_{s.upper()}_MIN", d)
    for s, d in (("new", 30), ("picked", 180), ("shop", 2880), ("ready", 720), ("delivering", 180))
}
REMIND_REPEAT_MIN = _int("REMIND_REPEAT_MIN", 120)  # не чаще, чем раз в N минут по одному заказу
REMIND_FROM_HOUR = _int("REMIND_FROM_HOUR", 8)  # напоминания только в рабочие часы (Бишкек)
REMIND_TO_HOUR = _int("REMIND_TO_HOUR", 22)
SHIFT_MAX_HOURS = _int("SHIFT_MAX_HOURS", 14)  # авто-завершение смены курьера
