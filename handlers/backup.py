import asyncio
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

import db
from common import ADMIN, Role
from config import ADMIN_IDS, DATABASE_URL

log = logging.getLogger(__name__)
router = Router(name="backup")
router.message.filter(Role(ADMIN))
BISHKEK = timezone(timedelta(hours=6))  # без перехода на летнее время


def db_path() -> str | None:
    return DATABASE_URL.split(":///", 1)[1] if "sqlite" in DATABASE_URL else None


def _copy(src: str, dst: str) -> None:
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)  # консистентная копия даже при работающем боте
    finally:
        b.close()
        a.close()


def _valid(path: str) -> bool:
    try:
        con = sqlite3.connect(path)
        try:
            con.execute("SELECT count(*) FROM orders").fetchone()
            con.execute("SELECT count(*) FROM users").fetchone()
            return True
        finally:
            con.close()
    except sqlite3.Error:
        return False


async def send_backup(bot: Bot, chat_ids) -> bool:
    path = db_path()
    if not path or not os.path.exists(path):
        return False
    tmp = os.path.join(tempfile.gettempdir(), f"laundry-{datetime.now(BISHKEK):%Y%m%d-%H%M}.db")
    await asyncio.to_thread(_copy, path, tmp)
    try:
        for cid in chat_ids:
            try:
                await bot.send_document(cid, FSInputFile(tmp), caption="💾 Копия базы. Восстановление: отправьте файл с подписью /restore")
            except Exception as e:
                log.warning("backup to %s failed: %s", cid, e)
    finally:
        os.remove(tmp)
    return True


async def daily_backup(bot: Bot) -> None:
    """Каждую ночь в 03:00 (Бишкек) шлёт копию базы всем админам."""
    while True:
        now = datetime.now(BISHKEK)
        nxt = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await send_backup(bot, ADMIN_IDS)
        except Exception:
            log.exception("daily backup failed")


@router.message(Command("backup"))
@router.message(F.text == "💾 Бэкап")
async def backup(m: Message, bot: Bot):
    if not await send_backup(bot, [m.from_user.id]):
        await m.answer("База не в файле SQLite — копия недоступна")


@router.message(F.document, F.caption.startswith("/restore"))
async def restore(m: Message, bot: Bot):
    path = db_path()
    if not path:
        await m.answer("Восстановление работает только для SQLite")
        return
    tmp = path + ".restore"
    await bot.download(m.document, destination=tmp)
    if not await asyncio.to_thread(_valid, tmp):
        os.remove(tmp)
        await m.answer("❌ Это не файл базы этого бота")
        return
    await m.answer("✅ База загружена. Бот перезапускается…")
    await db.close_db()
    for ext in ("-wal", "-shm"):
        if os.path.exists(path + ext):
            os.remove(path + ext)
    os.replace(tmp, path)
    os._exit(0)  # Render / Docker поднимут бота заново; локально — запустите снова
