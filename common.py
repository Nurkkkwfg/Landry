import asyncio
import html
import logging
import re
import time

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

import db
import geo
from config import ADMIN_IDS, BISHKEK
from db import ACTIVE, Order

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
# отменять можно всё, кроме заказа в цехе (там уже идёт работа с вещами клиента)
CANCELLABLE = ("new", "picked", "ready", "delivering")
# тексты кнопок меню: во время ввода они не должны считаться данными
MENU = frozenset(
    ["📍 Геопозиция", "📋 Задачи", "🟢 Начать смену", "🔴 Завершить смену", "🏭 Сдать в цех",
     "🧼 В цехе", "🔎 Найти №", "➕ Новая заявка", "📋 Заказы", "🔎 Поиск", "👥 Сотрудники", "💾 Бэкап"]
)

# user_id -> (role|None, время записи); сбрасывается при изменении сотрудника и по таймауту
ROLE_CACHE: dict[int, tuple[str | None, float]] = {}
ROLE_TTL = 300


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


# ---------- отправка с повторами ----------
_sem = asyncio.Semaphore(10)  # не упираемся в лимиты Telegram при рассылке


async def safe_send(bot: Bot, chat_id: int, text: str, retries: int = 4, **kw):
    """send_message с повторами: flood-wait и сетевые сбои не теряют сообщение.
    Возвращает Message или None (если пользователь заблокировал бота и т.п.)."""
    for attempt in range(retries):
        try:
            async with _sem:
                return await bot.send_message(chat_id, text, **kw)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.warning("send to %s failed: %s", chat_id, e)
            return None
        except (TelegramNetworkError, TelegramServerError) as e:
            log.warning("send to %s: %s (попытка %s)", chat_id, e, attempt + 1)
            await asyncio.sleep(2**attempt)
    log.error("send to %s: не удалось после %s попыток", chat_id, retries)
    return None


# ---------- доступ ----------
class Ctx(BaseMiddleware):
    """Подкладывает хранилище (db) и роль пользователя (с кэшем в памяти)."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        role = None
        if user:
            if user.id in ADMIN_IDS:
                role = ADMIN
            else:
                hit = ROLE_CACHE.get(user.id)
                if hit and time.monotonic() - hit[1] < ROLE_TTL:
                    role = hit[0]
                else:
                    rec = await db.store.get_user(user.id)
                    role = rec.role if rec and rec.approved else None
                    ROLE_CACHE[user.id] = (role, time.monotonic())
        data["db"] = db.store
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


def history_text(o: Order) -> str:
    lines = [f"📜 <b>История №{o.id}</b>"]
    for h in (o.history or [])[-40:]:
        at = h.get("at")
        t = at.astimezone(BISHKEK).strftime("%d.%m %H:%M") if at else "—"
        line = f"{t} · {STATUS.get(h.get('status'), h.get('status'))}"
        if h.get("by_name"):
            line += f" · {esc(h['by_name'])}"
        if h.get("note"):
            line += f" ({esc(h['note'])})"
        lines.append(line)
    return "\n".join(lines)


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


# ---------- рассылки ----------
async def dispatcher_ids() -> set[int]:
    """Диспетчеры; если их нет — админы."""
    ids = {u.id for u in await db.store.users_by_role(DISPATCHER)}
    return ids or set(ADMIN_IDS)


async def notify_dispatchers(bot: Bot, text: str, exclude: int | None = None) -> None:
    ids = await dispatcher_ids() - {exclude}
    await asyncio.gather(*(safe_send(bot, i, text) for i in ids))


async def status_event(bot: Bot, o: Order, actor_name: str, exclude: int | None = None) -> None:
    """Уведомление диспетчерам о смене статуса."""
    text = f"🔔 <b>№{o.id}</b> · {esc(o.name)} — {STATUS[o.status]}\n👤 {esc(actor_name)}"
    await notify_dispatchers(bot, text, exclude)


async def notify_couriers(bot: Bot, o: Order) -> None:
    """Карточка задачи всем курьерам на смене. Запоминаем сообщения, чтобы убрать их, когда заказ возьмут."""
    couriers = await db.store.couriers_on_shift()

    async def send(c):
        text, kb = task_view(o, c.lat, c.lon)
        msg = await safe_send(bot, c.id, text, reply_markup=kb)
        return {"c": c.id, "m": msg.message_id} if msg else None

    cards = [r for r in await asyncio.gather(*(send(c) for c in couriers)) if r]
    if not cards:
        return
    await db.store.add_cards(o.id, cards)
    fresh = await db.store.get_order(o.id)  # успели взять/отменить, пока рассылали?
    if fresh is None or fresh.status != o.status:
        await retire_cards(bot, o.id, f"🚫 <b>№{o.id}</b> — уже неактуально")


async def notify(bot: Bot, order_id: int) -> None:
    """Фон: карточка задачи всем курьерам на смене."""
    o = await db.store.get_order(order_id)
    if o is not None and o.status in ("new", "ready"):
        await notify_couriers(bot, o)


async def retire_cards(bot: Bot, oid: int, text: str, keep: tuple[int, int] | None = None) -> None:
    """Заменяет разосланные карточки задачи текстом (без кнопок). keep=(chat, message) — не трогать."""
    cards = await db.store.pop_cards(oid)

    async def edit(cd):
        if keep and (cd["c"], cd["m"]) == keep:
            return
        try:
            async with _sem:
                await bot.edit_message_text(text, chat_id=cd["c"], message_id=cd["m"])
        except Exception:
            pass

    await asyncio.gather(*(edit(cd) for cd in cards))


async def order_changed(bot: Bot, o: Order) -> None:
    """Заказ отредактировали: курьеры узнают об этом."""
    if o.status in ("new", "ready"):
        await retire_cards(bot, o.id, f"✏️ <b>№{o.id}</b> изменён — смотрите новую карточку")
        await notify(bot, o.id)
    elif o.courier_id:
        if o.status == "delivering":
            text, kb = task_view(o)
            await safe_send(bot, o.courier_id, "✏️ Заказ изменён:\n" + text, reply_markup=kb)
        else:
            await safe_send(bot, o.courier_id, "✏️ Заказ изменён:\n" + card(o))
