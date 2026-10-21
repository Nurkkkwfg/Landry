"""Резервные копии: выгрузка данных Firestore в сжатый JSON и восстановление из него (с подтверждением)."""
import asyncio
import gzip
import io
import json
import logging
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

import db as dbm
from common import ADMIN, ROLE_CACHE, STATUS, Role, safe_send
from config import ADMIN_IDS, BISHKEK

log = logging.getLogger(__name__)
router = Router(name="backup")
router.message.filter(Role(ADMIN))
router.callback_query.filter(Role(ADMIN))

BACKUP_DAYS = 60  # обычная копия: сотрудники + активные + заказы, тронутые за N дней. «/backup full» — всё.
MAX_FILE = 20 * 1024 * 1024  # лимит скачивания файлов ботом
PENDING: dict[int, dict] = {}  # админ -> загруженный и проверенный файл, ждёт подтверждения


def _enc(o):
    if isinstance(o, datetime):
        return {"__dt__": o.isoformat()}
    raise TypeError(f"{type(o)} is not JSON serializable")


def _dec(d):
    if len(d) == 1 and "__dt__" in d:
        return datetime.fromisoformat(d["__dt__"])
    return d


async def make_backup(days: int | None) -> tuple[bytes, dict]:
    payload = await dbm.store.export_all(days)
    raw = json.dumps(payload, default=_enc, ensure_ascii=False).encode()
    return await asyncio.to_thread(gzip.compress, raw), payload


async def send_backup(bot: Bot, chat_ids, days: int | None = BACKUP_DAYS) -> None:
    data, payload = await make_backup(days)
    name = f"laundry-{datetime.now(BISHKEK):%Y%m%d-%H%M}{'-full' if not days else ''}.json.gz"
    scope = "все данные" if not days else f"сотрудники, активные заказы и всё за {days} дней"
    cap = (
        f"💾 Копия данных ({scope}): заказов {len(payload['orders'])}, сотрудников {len(payload['users'])}.\n"
        "Восстановление: отправьте файл с подписью /restore"
    )
    for cid in chat_ids:
        try:
            await bot.send_document(cid, BufferedInputFile(data, filename=name), caption=cap)
        except Exception as e:
            log.warning("backup to %s failed: %s", cid, e)


async def daily_backup(bot: Bot) -> None:
    """Каждую ночь в 03:00 (Бишкек) шлёт копию данных всем админам."""
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
async def backup(m: Message, bot: Bot, command: CommandObject | None = None):
    full = bool(command and command.args and "full" in command.args.lower())
    wait = await m.answer("⏳ Готовлю копию…")
    await send_backup(bot, [m.from_user.id], None if full else BACKUP_DAYS)
    await wait.delete()


def parse_backup(raw: bytes) -> dict:
    """Проверка файла: возвращает payload или бросает ValueError."""
    try:
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        p = json.loads(raw.decode(), object_hook=_dec)
    except Exception:
        raise ValueError("не удалось прочитать файл")
    if not isinstance(p, dict) or p.get("version") != 1:
        raise ValueError("это не копия этого бота")
    users, orders = p.get("users"), p.get("orders")
    if not isinstance(users, list) or not isinstance(orders, list):
        raise ValueError("в файле нет списков users/orders")
    for u in users:
        if not isinstance(u, dict) or not isinstance(u.get("id"), int):
            raise ValueError("некорректная запись сотрудника")
    for o in orders:
        if not isinstance(o, dict) or not isinstance(o.get("id"), int) or o.get("status") not in STATUS:
            raise ValueError("некорректная запись заказа")
        for k in ("name", "phone", "address"):
            if not isinstance(o.get(k), str):
                raise ValueError(f"у заказа №{o['id']} нет поля {k}")
    return p


@router.message(F.document, F.caption.startswith("/restore"))
async def restore(m: Message, bot: Bot):
    if (m.document.file_size or 0) > MAX_FILE:
        await m.answer("❌ Файл слишком большой (лимит 20 МБ)")
        return
    buf = io.BytesIO()
    await bot.download(m.document, destination=buf)
    try:
        p = await asyncio.to_thread(parse_backup, buf.getvalue())
    except ValueError as e:
        await m.answer(f"❌ {e}")
        return
    PENDING[m.from_user.id] = p
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[Btn(text="✅ Восстановить", callback_data="rs:ok"), Btn(text="Отмена", callback_data="rs:no")]]
    )
    await m.answer(
        f"📦 Копия от {p.get('created', '?')[:16]}\n"
        f"Сотрудников: {len(p['users'])}, заказов: {len(p['orders'])}\n\n"
        "⚠️ Записи с теми же номерами/ID будут <b>перезаписаны</b>, остальные данные не тронутся. "
        "Перед восстановлением пришлю копию текущих данных.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "rs:no")
async def restore_no(c: CallbackQuery):
    PENDING.pop(c.from_user.id, None)
    await c.message.edit_text("Восстановление отменено")
    await c.answer()


@router.callback_query(F.data == "rs:ok")
async def restore_ok(c: CallbackQuery, bot: Bot):
    p = PENDING.pop(c.from_user.id, None)
    if p is None:
        await c.answer("Файл устарел — отправьте его заново", show_alert=True)
        return
    await c.answer()
    await c.message.edit_text("⏳ Сохраняю текущие данные и восстанавливаю…")
    await send_backup(bot, [c.from_user.id], None)  # страховочная копия текущего состояния
    await dbm.store.import_docs(p["users"], p["orders"], int(p.get("counter") or 0))
    ROLE_CACHE.clear()
    await c.message.edit_text(f"✅ Восстановлено: сотрудников {len(p['users'])}, заказов {len(p['orders'])}")
