from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message
from sqlalchemy import and_, or_, select, update

import geo
from common import COURIER, Role, esc, menu, task_view
from db import Order, User

router = Router(name="courier")
for _obs in (router.message, router.edited_message, router.callback_query):
    _obs.filter(Role(COURIER))


async def held(db, uid: int) -> list[Order]:
    return (
        await db.scalars(select(Order).where(Order.status == "picked", Order.courier_id == uid).order_by(Order.id))
    ).all()


async def show_handover(m: Message, orders: list[Order]):
    lines = "\n".join(f"№{o.id} · {esc(o.name)}" for o in orders)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[Btn(text=f"🏭 Сдать всё в цех ({len(orders)})", callback_data="c:sa")]]
    )
    await m.answer(f"🚗 <b>У вас на руках:</b>\n{lines}", reply_markup=kb)


async def show_tasks(m: Message, db, u: User):
    orders = (
        await db.scalars(
            select(Order).where(
                or_(Order.status.in_(("new", "ready")), and_(Order.status == "delivering", Order.courier_id == u.id))
            )
        )
    ).all()
    mine = await held(db, u.id)
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
    u = await db.get(User, m.from_user.id)
    u.on_shift, u.lat, u.lon = True, None, None
    await db.commit()
    await m.answer("🟢 Смена начата. Нажмите «📍 Геопозиция».", reply_markup=menu(COURIER))


@router.message(F.text == "🔴 Завершить смену")
async def shift_off(m: Message, db):
    u = await db.get(User, m.from_user.id)
    u.on_shift, u.lat, u.lon = False, None, None
    await db.commit()
    text = "🔴 Смена завершена"
    mine = await held(db, u.id)
    if mine:
        text += f"\n⚠️ У вас на руках заказов: {len(mine)} — сдайте в цех («🏭 Сдать в цех»)"
    await m.answer(text, reply_markup=menu(COURIER))


@router.message(F.location)
async def location(m: Message, db):
    u = await db.get(User, m.from_user.id)
    u.lat, u.lon, u.on_shift = m.location.latitude, m.location.longitude, True
    await db.commit()
    await show_tasks(m, db, u)


@router.edited_message(F.location)  # живая геопозиция: тихо обновляем
async def live_location(m: Message, db):
    u = await db.get(User, m.from_user.id)
    if u and u.on_shift:
        u.lat, u.lon = m.location.latitude, m.location.longitude
        await db.commit()


@router.message(F.text == "📋 Задачи")
async def tasks(m: Message, db):
    await show_tasks(m, db, await db.get(User, m.from_user.id))


@router.message(F.text == "🏭 Сдать в цех")
async def handover(m: Message, db):
    mine = await held(db, m.from_user.id)
    if not mine:
        await m.answer("📭 У вас нет заказов для сдачи")
    else:
        await show_handover(m, mine)


async def move(c: CallbackQuery, db, src: str, dst: str, courier_cond, courier_value):
    oid = int(c.data.split(":")[2])
    r = await db.execute(
        update(Order).where(Order.id == oid, Order.status == src, courier_cond(oid)).values(status=dst, courier_id=courier_value)
    )
    await db.commit()
    if r.rowcount != 1:
        await c.answer("Уже выполнено, отменено или у другого курьера", show_alert=True)
        await c.message.edit_reply_markup()
        return None
    await c.answer("Готово")
    return oid


@router.callback_query(F.data.startswith("c:p:"))
async def picked(c: CallbackQuery, db):
    oid = await move(c, db, "new", "picked", lambda i: Order.id == i, c.from_user.id)
    if oid:
        await c.message.edit_text(f"<b>№{oid}</b> 🚗 У вас — сдайте в прачечную («🏭 Сдать в цех»)")


@router.callback_query(F.data.startswith("c:l:"))
async def load(c: CallbackQuery, db):
    oid = await move(c, db, "ready", "delivering", lambda i: Order.id == i, c.from_user.id)
    if oid:
        o, u = await db.get(Order, oid), await db.get(User, c.from_user.id)
        text, kb = task_view(o, u.lat, u.lon)
        await c.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("c:d:"))
async def delivered(c: CallbackQuery, db):
    oid = await move(c, db, "delivering", "delivered", lambda i: Order.courier_id == c.from_user.id, c.from_user.id)
    if oid:
        await c.message.edit_text(f"<b>№{oid}</b> 📦 Завершён")


@router.callback_query(F.data == "c:sa")
async def handover_all(c: CallbackQuery, db):
    r = await db.execute(
        update(Order)
        .where(Order.status == "picked", Order.courier_id == c.from_user.id)
        .values(status="shop", courier_id=None)
    )
    await db.commit()
    await c.message.edit_text(f"🏭 Сдано в прачечную: {r.rowcount}" if r.rowcount else "Нечего сдавать")
    await c.answer("Готово")
