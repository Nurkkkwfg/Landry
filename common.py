import asyncio
import html
import logging
import re

from aiogram import BaseMiddleware, Bot
from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from sqlalchemy import select

import db
import geo
from config import ADMIN_IDS
from db import Order, User

log = logging.getLogger(__name__)

ADMIN, DISPATCHER, COURIER, LAUNDRY = "admin", "dispatcher", "courier", "laundry"
ROLE_NAMES = {DISPATCHER: "📋 Диспетчер", COURIER: "🚚 Курьер", LAUNDRY: "🧼 Цех"}
STATUS = {
    "new": "🆕 Новый",
    "picked": "🚗 У водителя",
    "shop": "🏭 В прачечной",
    "ready": "✅ Готов к доставке",
    "delivering": "🚗 У водителя (везёт клиенту)",
    "delivered": "📦 Завершён",
    "cancelled": "❌ Отменён",
}
ACTIVE = ("new", "picked", "shop", "ready", "delivering")
# тексты кнопок меню: во время ввода они не должны считаться данными
MENU = frozenset(
    ["📍 Геопозиция", "📋 Задачи", "🟢 Начать смену", "🔴 Завершить смену", "🏭 Сдать в цех",
     "🧼 В цехе", "🔎 Найти №", "➕ Новая заявка", "📋 Заказы", "🔎 Поиск", "👥 Сотрудники", "💾 Бэкап"]
)

# role -> {user_id: role|None}; сбрасывается при изменении сотрудника
ROLE_CACHE: dict[int, str | None] = {}


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def norm_phone(raw: str) -> str | None:
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 9:
        d = "996" + d
    elif len(d) == 10 and d.startswith("0"):
        d = "996" + d[1:]
    return "+" + d if 11 <= len(d) <= 15 else None


# ---------- фоновые задачи ----------
_tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)

    def _done(task: asyncio.Task):
        _tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("background task failed", exc_info=task.exception())

    t.add_done_callback(_done)


# ---------- доступ ----------
class Ctx(BaseMiddleware):
    """Открывает сессию БД и определяет роль (с кэшем в памяти)."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        async with db.SessionLocal() as session:
            data["db"] = session
            role = None
            if user:
                if user.id in ADMIN_IDS:
                    role = ADMIN
                elif user.id in ROLE_CACHE:
                    role = ROLE_CACHE[user.id]
                else:
                    rec = await session.get(User, user.id)
                    role = rec.role if rec and rec.approved else None
                    ROLE_CACHE[user.id] = role
            data["role"] = role
            return await handler(event, data)


class Role(BaseFilter):
    def __init__(self, *roles: str):
        self.roles = frozenset(roles)

    async def __call__(self, event, role: str | None = None) -> bool:
        return role in self.roles


def menu(role: str) -> ReplyKeyboardMarkup:
    if role == COURIER:
        rows = [
            [KeyboardButton(text="📍 Геопозиция", request_location=True), KeyboardButton(text="📋 Задачи")],
            [KeyboardButton(text="🏭 Сдать в цех")],
            [KeyboardButton(text="🟢 Начать смену"), KeyboardButton(text="🔴 Завершить смену")],
        ]
    elif role == LAUNDRY:
        rows = [[KeyboardButton(text="🧼 В цехе"), KeyboardButton(text="🔎 Найти №")]]
    else:
        rows = [
            [KeyboardButton(text="➕ Новая заявка"), KeyboardButton(text="📋 Заказы")],
            [KeyboardButton(text="🔎 Поиск")],
        ]
        if role == ADMIN:
            rows[1] += [KeyboardButton(text="👥 Сотрудники"), KeyboardButton(text="💾 Бэкап")]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ---------- карточка ----------
def card(o: Order, head: str = "") -> str:
    t = f"<b>№{o.id}</b>{head}\n👤 {esc(o.name)}\n📞 {esc(o.phone)}\n📍 {esc(o.address)}"
    if o.measurements:
        t += f"\n📐 {esc(o.measurements)}"
    return t


def task_view(o: Order, lat: float | None = None, lon: float | None = None):
    """Карточка задачи курьера: текст + кнопки."""
    label, btn, cb = {
        "new": ("🧺 Забрать", "✅ Забрал", f"c:p:{o.id}"),
        "ready": ("🚚 Взять на доставку", "🚚 Взял", f"c:l:{o.id}"),
        "delivering": ("📦 Доставить", "✅ Доставил", f"c:d:{o.id}"),
    }[o.status]
    head = f" · {label}"
    if None not in (lat, lon, o.lat, o.lon):
        head += f" · {geo.km(lat, lon, o.lat, o.lon):.1f} км"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [Btn(text="🗺 Открыть в 2ГИС", url=geo.map_url(o.address, o.lat, o.lon))],
            [Btn(text=btn, callback_data=cb)],
        ]
    )
    return card(o, head), kb


async def notify_couriers(bot: Bot, session, o: Order) -> None:
    """Карточка задачи всем курьерам на смене."""
    couriers = (
        await session.scalars(
            select(User).where(User.role == COURIER, User.approved.is_(True), User.on_shift.is_(True))
        )
    ).all()

    async def send(c: User):
        text, kb = task_view(o, c.lat, c.lon)
        await bot.send_message(c.id, text, reply_markup=kb)

    results = await asyncio.gather(*(send(c) for c in couriers), return_exceptions=True)
    for c, r in zip(couriers, results):
        if isinstance(r, Exception):
            log.warning("notify courier %s: %s", c.id, r)


async def notify(bot: Bot, order_id: int) -> None:
    """Фон: карточка задачи всем курьерам на смене."""
    async with db.SessionLocal() as s:
        o = await s.get(Order, order_id)
        if o is not None and o.status in ("new", "ready"):
            await notify_couriers(bot, s, o)
