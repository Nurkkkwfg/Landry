from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

import geo
from common import COURIER, Role, esc, menu, notify_dispatchers, retire_cards, spawn, status_event, task_view
from db import Order, User, now

router = Router(name="courier")
for _obs in (router.message, router.edited_message, router.callback_query):
    _obs.filter(Role(COURIER))

NO_PROFILE = "⚠️ Профиль не найден (доступ мог быть закрыт). Нажмите /start"


async def show_handover(m: Message, orders: list[Order]):
    lines = "\n".join(f"№{o.id} · {esc(o.name)}" for o in orders)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[Btn(text=f"🏭 Сдать всё в цех ({len(orders)})", callback_data="c:sa")]]
    )
    await m.answer(f"🚗 <b>У вас на руках:</b>\n{lines}", reply_markup=kb)


async def show_tasks(m: Message, db, u: User | None):
    if u is None:  # пользователя удалили, а роль ещё жила в кэше
        await m.answer(NO_PROFILE)
        return
    orders = await db.tasks_for(u.id)
    mine = await db.held(u.id)
    if not orders and not mine:
        await m.answer("📭 Задач нет")
        return
    if u.lat is not None:
        orders.sort(
            key=lambda o: (o.lat is None, geo.km(u.lat, u.lon, o.lat, o.lon) if o.lat is not None else 0, o.id)
        )
    else:
        orders.sort(key=lambda o: o.id)
        if orders:
            await m.answer("ℹ️ Отправьте геопозицию — отсортирую от ближних к дальним")
    if mine:
        await show_handover(m, mine)
    for o in orders:
        text, kb = task_view(o, u.lat, u.lon)
        await m.answer(text, reply_markup=kb)


@router.message(F.text == "🟢 Начать смену")
async def shift_on(m: Message, db):
    if not await db.update_user(m.from_user.id, on_shift=True, lat=None, lon=None, shift_at=now()):
        await m.answer(NO_PROFILE)
        return
    await m.answer("🟢 Смена начата. Нажмите «📍 Геопозиция».", reply_markup=menu(COURIER))


@router.message(F.text == "🔴 Завершить смену")
async def shift_off(m: Message, db):
    if not await db.update_user(m.from_user.id, on_shift=False, lat=None, lon=None):
        await m.answer(NO_PROFILE)
        return
    text = "🔴 Смена завершена"
    mine = await db.held(m.from_user.id)
    if mine:
        text += f"\n⚠️ У вас на руках заказов: {len(mine)} — сдайте в цех («🏭 Сдать в цех»)"
    await m.answer(text, reply_markup=menu(COURIER))


@router.message(F.location)
async def location(m: Message, db):
    u = await db.get_user(m.from_user.id)
    if u is None:
        await m.answer(NO_PROFILE)
        return
    if not u.on_shift:  # геопозиция больше не включает смену сама
        await m.answer("🔴 Смена не начата. Нажмите «🟢 Начать смену», затем отправьте геопозицию.")
        return
    await db.update_user(u.id, lat=m.location.latitude, lon=m.location.longitude)
    u.lat, u.lon = m.location.latitude, m.location.longitude
    await show_tasks(m, db, u)


@router.edited_message(F.location)  # живая геопозиция: тихо обновляем
async def live_location(m: Message, db):
    u = await db.get_user(m.from_user.id)
    if u and u.on_shift:
        await db.update_user(u.id, lat=m.location.latitude, lon=m.location.longitude)


@router.message(F.text == "📋 Задачи")
async def tasks(m: Message, db):
    await show_tasks(m, db, await db.get_user(m.from_user.id))


@router.message(F.text == "🏭 Сдать в цех")
async def handover(m: Message, db):
    mine = await db.held(m.from_user.id)
    if not mine:
        await m.answer("📭 У вас нет заказов для сдачи")
    else:
        await show_handover(m, mine)


async def move(c: CallbackQuery, db, bot: Bot, src: str, dst: str, **kw) -> Order | None:
    oid = int(c.data.split(":")[2])
    o = await db.transition(oid, (src,), dst, c.from_user.id, c.from_user.full_name, **kw)
    if o is None:
        await c.answer("Уже выполнено, отменено или у другого курьера", show_alert=True)
        try:
            await c.message.edit_reply_markup()
        except Exception:
            pass
        return None
    await c.answer("Готово")
    if src in ("new", "ready"):  # у остальных курьеров карточка больше не висит
        spawn(retire_cards(bot, oid, f"🚫 <b>№{oid}</b> — уже взят другим курьером",
                           keep=(c.message.chat.id, c.message.message_id)))
    spawn(status_event(bot, o, c.from_user.full_name))
    return o


@router.callback_query(F.data.startswith("c:p:"))
async def picked(c: CallbackQuery, db, bot: Bot):
    o = await move(c, db, bot, "new", "picked", set_courier=c.from_user.id)
    if o:
        await c.message.edit_text(f"<b>№{o.id}</b> 🚗 У вас — сдайте в прачечную («🏭 Сдать в цех»)")


@router.callback_query(F.data.startswith("c:l:"))
async def load(c: CallbackQuery, db, bot: Bot):
    o = await move(c, db, bot, "ready", "delivering", set_courier=c.from_user.id)
    if o:
        u = await db.get_user(c.from_user.id)
        text, kb = task_view(o, u.lat if u else None, u.lon if u else None)
        await c.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("c:d:"))
async def delivered(c: CallbackQuery, db, bot: Bot):
    o = await move(c, db, bot, "delivering", "delivered", require_courier=c.from_user.id)
    if o:
        await c.message.edit_text(f"<b>№{o.id}</b> 📦 Завершён")


@router.callback_query(F.data == "c:sa")
async def handover_all(c: CallbackQuery, db, bot: Bot):
    uid, name = c.from_user.id, c.from_user.full_name
    done = []
    for o in await db.held(uid):
        r = await db.transition(o.id, ("picked",), "shop", uid, name, require_courier=uid, set_courier=None)
        if r:
            done.append(r)
    await c.message.edit_text(f"🏭 Сдано в прачечную: {len(done)}" if done else "Нечего сдавать")
    await c.answer("Готово")
    if done:
        spawn(notify_dispatchers(bot, f"🔔 🏭 В прачечной ({esc(name)}): " + ", ".join(f"№{o.id}" for o in done)))
