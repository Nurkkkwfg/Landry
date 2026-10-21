import re

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

from common import LAUNDRY, MENU, STATUS, Role, card, notify, spawn, status_event
from db import Order

router = Router(name="laundry")
router.message.filter(Role(LAUNDRY))
router.callback_query.filter(Role(LAUNDRY))
NOT_MENU = ~F.text.in_(MENU)


class S(StatesGroup):
    find = State()
    carpets = State()
    curtains = State()


def kb(o: Order) -> InlineKeyboardMarkup | None:
    if o.status != "shop":
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                Btn(text="📐 Замеры", callback_data=f"lm:{o.id}"),
                Btn(text="✅ Готово", callback_data=f"lr:{o.id}"),
            ]
        ]
    )


async def show(m: Message, o: Order):
    await m.answer(card(o, f" · {STATUS[o.status]}"), reply_markup=kb(o))


@router.message(F.text == "🧼 В цехе")
async def in_work(m: Message, state: FSMContext, db):
    await state.clear()
    orders = sorted(await db.by_status(("shop",)), key=lambda o: o.id)
    if not orders:
        await m.answer("📭 В прачечной пусто")
        return
    rows = [[Btn(text=f"№{o.id} · {o.name[:20]}", callback_data=f"lo:{o.id}")] for o in orders[:60]]
    await m.answer(f"🧼 В прачечной: {len(orders)}", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("lo:"))
async def open_(c: CallbackQuery, db):
    o = await db.get_order(int(c.data.split(":")[1]))
    await c.answer()
    if o:
        await show(c.message, o)


@router.message(F.text == "🔎 Найти №")
async def ask(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(S.find)
    await m.answer("Номер заказа:")


async def find(m: Message, db):
    q = (m.text or "").strip().lstrip("#")
    o = await db.get_order(int(q)) if q.isdigit() and len(q) <= 9 else None
    if o is None:
        await m.answer("Заказ не найден")
    else:
        await show(m, o)


@router.message(S.find, NOT_MENU)
async def find_state(m: Message, state: FSMContext, db):
    await state.clear()
    await find(m, db)


@router.message(StateFilter(None), F.text.regexp(r"^#?\d{1,9}$"))
async def find_quick(m: Message, db):
    await find(m, db)


# ---------- замеры: ковры (размеры по строкам) -> шторы (штук) ----------
NONE_WORDS = {"0", "-", "—", "нет", "no"}


def parse_carpets(text: str) -> list[str] | None:
    """'1 ковер 3х4' / '2) 4*5' / '3 на 2.5' -> ['3×4', ...]; None — если строка не разобрана."""
    if text.strip().lower() in NONE_WORDS:
        return []
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        nums = re.findall(r"\d+(?:[.,]\d+)?", line)
        if len(nums) < 2:
            return None
        a, b = (x.replace(",", ".") for x in nums[-2:])  # «1 ковер 3х4» -> берём последние два числа
        out.append(f"{a}×{b}")
    return out


def build(carpets: list[str], curtains: int) -> str:
    parts = []
    if carpets:
        parts.append(f"Ковров: {len(carpets)}\n" + "\n".join(f"{i}) {c} м" for i, c in enumerate(carpets, 1)))
    if curtains:
        parts.append(f"Шторы: {curtains} шт.")
    return "\n".join(parts) or "Ковров и штор нет"


@router.callback_query(F.data.startswith("lm:"))
async def measure_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.carpets)
    await state.update_data(oid=int(c.data.split(":")[1]))
    await c.message.answer(
        "🧺 Размеры ковров — каждый с новой строки:\n<code>3х4\n4х5\n4х5</code>\nНет ковров — напишите 0"
    )
    await c.answer()


@router.message(S.carpets, NOT_MENU)
async def got_carpets(m: Message, state: FSMContext):
    carpets = parse_carpets(m.text or "")
    if carpets is None:
        await m.answer("Не понял. В каждой строке два числа, например: 3х4")
        return
    await state.update_data(carpets=carpets)
    await state.set_state(S.curtains)
    await m.answer("🪟 Сколько штор? (число, нет — 0)")


@router.message(S.curtains, NOT_MENU)
async def got_curtains(m: Message, state: FSMContext, db):
    t = (m.text or "").strip().lower()
    if t in NONE_WORDS:
        n = 0
    else:
        mm = re.search(r"\d+", t)
        if not mm:
            await m.answer("Введите число, например 2")
            return
        n = int(mm.group())
    d = await state.get_data()
    await state.clear()
    o = await db.set_measurements(d["oid"], build(d["carpets"], n), m.from_user.id, m.from_user.full_name)
    if o is None:  # раньше здесь был молчаливый «успех»
        await m.answer("⚠️ Замеры НЕ сохранены: статус заказа изменился (он уже не в цехе).")
        cur = await db.get_order(d["oid"])
        if cur:
            await show(m, cur)
        return
    await m.answer("✅ Замеры сохранены")
    await show(m, o)


@router.callback_query(F.data.startswith("lr:"))
async def ready(c: CallbackQuery, db, bot: Bot):
    oid = int(c.data.split(":")[1])
    o = await db.transition(oid, ("shop",), "ready", c.from_user.id, c.from_user.full_name)
    if o is None:
        await c.answer("Статус уже изменён", show_alert=True)
        return
    await c.message.edit_text(card(o, f" · {STATUS['ready']}"))
    await c.answer("Готово")
    spawn(notify(bot, oid))  # курьерам на смене — «взять на доставку»
    spawn(status_event(bot, o, c.from_user.full_name))
