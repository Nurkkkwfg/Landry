import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message
from sqlalchemy import select

from common import ADMIN, ROLE_CACHE, ROLE_NAMES, Role, esc, menu
from config import ADMIN_IDS
from db import User

log = logging.getLogger(__name__)
router = Router(name="access")
admin_router = Router(name="admin")
admin_router.message.filter(Role(ADMIN))
admin_router.callback_query.filter(Role(ADMIN))
fallback = Router(name="fallback")


def role_buttons(uid: int, prefix: str = "st:set") -> list[list[Btn]]:
    return [[Btn(text=name, callback_data=f"{prefix}:{uid}:{r}")] for r, name in ROLE_NAMES.items()]


@router.message(CommandStart())
async def start(m: Message, db, bot: Bot, state: FSMContext, role: str | None):
    await state.clear()
    if role:
        await m.answer("Меню 👇", reply_markup=menu(role))
        return
    p = m.from_user
    if await db.get(User, p.id):
        await m.answer("⏳ Заявка уже отправлена, ждите одобрения.")
        return
    db.add(User(id=p.id, username=p.username, full_name=p.full_name))
    await db.commit()
    kb = InlineKeyboardMarkup(
        inline_keyboard=role_buttons(p.id) + [[Btn(text="🚫 Отклонить", callback_data=f"st:del:{p.id}")]]
    )
    sent = 0
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(
                aid, f"🔔 Заявка: <b>{esc(p.full_name)}</b> @{esc(p.username or '—')} (ID {p.id})", reply_markup=kb
            )
            sent += 1
        except Exception as e:
            log.warning("admin %s unreachable: %s", aid, e)
    await m.answer("✅ Заявка отправлена администратору." if sent else "⚠️ Не удалось связаться с админом.")


@router.message(Command("id"))
async def my_id(m: Message):
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>")


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext, role: str | None):
    await state.clear()
    await m.answer("Отменено.", reply_markup=menu(role) if role else None)


# ---------- сотрудники (только админ) ----------
@admin_router.message(F.text == "👥 Сотрудники")
async def staff(m: Message, db, state: FSMContext):
    await state.clear()
    users = (await db.scalars(select(User).order_by(User.approved, User.full_name))).all()
    users = [u for u in users if u.id not in ADMIN_IDS]
    if not users:
        await m.answer("Сотрудников пока нет.")
        return
    rows = [
        [Btn(text=f"{ROLE_NAMES.get(u.role, '⏳ Ждёт') if u.approved else '⏳ Ждёт'} · {u.full_name}"[:60],
             callback_data=f"st:open:{u.id}")]
        for u in users[:60]
    ]
    await m.answer("👥 Сотрудники:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.startswith("st:open:"))
async def staff_open(c: CallbackQuery, db):
    uid = int(c.data.split(":")[2])
    u = await db.get(User, uid)
    if not u:
        await c.answer("Не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=role_buttons(uid) + [[Btn(text="⛔ Удалить / отключить", callback_data=f"st:del:{uid}")]]
    )
    await c.message.edit_text(f"<b>{esc(u.full_name)}</b> (ID {uid}) — выберите роль:", reply_markup=kb)
    await c.answer()


@admin_router.callback_query(F.data.startswith("st:set:"))
async def staff_set(c: CallbackQuery, db, bot: Bot):
    _, _, uid, r = c.data.split(":")
    uid = int(uid)
    u = await db.get(User, uid)
    if not u or r not in ROLE_NAMES:
        await c.answer("Не найден", show_alert=True)
        return
    u.role, u.approved = r, True
    if r != "courier":
        u.on_shift = False
    await db.commit()
    ROLE_CACHE.pop(uid, None)
    await c.message.edit_text(f"✅ {esc(u.full_name)} → {ROLE_NAMES[r]}")
    await c.answer("Готово")
    try:
        await bot.send_message(uid, f"✅ Доступ открыт: {ROLE_NAMES[r]}", reply_markup=menu(r))
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("st:del:"))
async def staff_del(c: CallbackQuery, db, bot: Bot):
    uid = int(c.data.split(":")[2])
    u = await db.get(User, uid)
    if u:
        name = u.full_name
        await db.delete(u)
        await db.commit()
        ROLE_CACHE.pop(uid, None)
        await c.message.edit_text(f"🚫 {esc(name)} — доступ закрыт")
        try:
            await bot.send_message(uid, "Доступ закрыт.")
        except Exception:
            pass
    await c.answer()


# ---------- прочее ----------
@fallback.message()
async def unknown_message(m: Message, role: str | None):
    if role is None:
        await m.answer("Нет доступа. Нажмите /start, чтобы отправить заявку.")
    else:
        await m.answer("Используйте кнопки меню 👇", reply_markup=menu(role))


@fallback.callback_query()
async def stale(c: CallbackQuery):
    await c.answer("Кнопка устарела", show_alert=True)
