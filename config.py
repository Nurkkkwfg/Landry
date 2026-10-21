import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./laundry.db").strip()


def _ids(raw: str) -> frozenset[int]:
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return frozenset(out)


ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))

# необязательно: ключ 2ГИС API (platform.2gis.ru) — самый точный поиск адресов
DGIS_KEY = os.getenv("DGIS_KEY", "").strip()
