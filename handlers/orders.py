from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

import geo
from common import (
    ACTIVE, ADMIN, CANCELLABLE, DISPATCHER, MENU, STATUS, Role, card, esc, history_text, norm_phone, notify,
    order_changed, retire_cards, safe_send, spawn, status_event,
)
from db import Order

router = Router(name="orders")
router.message.filter(Role(ADMIN, DISPATCHER))
router.callback_query.filter(Role(ADMIN, DISPATCHER))

PAGE = 15
NOT_MENU = ~F.text.in_(MENU)


class New(StatesGroup):
    contact = State()
    name = State()


class Addr(StatesGroup):
    query = State()  # диспетчер пишет адрес
    pick = State()  # выбирает найденный вариант
    detail = State()  # подъезд / этаж / кв.


class Edit(StatesGroup):
    value = State()


class Find(StatesGroup):
    q = State()


def kb_order(o: Order) -> InlineKeyboardMarkup:
    rows = []
    if o.status in ACTIVE:
        rows.append([Btn(text="🗺 2ГИС", url=geo.map_url(o.address, o.lat, o.lon))])
        btns = [Btn(text="✏️ Изменить", callback_data=f"oe:{o.id}")]
        if o.status in CANCELLABLE:
            btns.append(Btn(text="❌ Отменить", callback_data=f"oc:{o.id}"))
        rows.append(btns)
    rows.append([Btn(text="📜 История", callback_data=f"oh:{o.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show(target: Message, o: Order, prefix: str = ""):
    await target.answer(prefix + card(o, f" · {STATUS[o.status]}"), reply_markup=kb_order(o))


async def create(m: Message, db, bot: Bot, who, name: str, phone: str, address: str, lat=None, lon=None):
    o = await db.create_order(name, phone, address, lat, lon, actor_id=who.id, actor_name=who.full_name)
    spawn(notify(bot, o.id))  # карточка курьерам на смене — в фоне
    await show(m, o, "✅ Создан\n")


# ---------- создание ----------
@router.message(F.text == "➕ Новая заявка")
async def new_order(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(New.contact)
    await m.answer("📞 Номер клиента\n(или сразу 3 строки: имя / номер / адрес)")


@router.message(New.contact, NOT_MENU)
async def got_contact(m: Message, state: FSMContext, db, bot: Bot):
    text = (m.text or "").strip()
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if len(lines) >= 3:
        i = next((i for i, x in enumerate(lines) if norm_phone(x)), None)
        if i is not None:
            rest = lines[:i] + lines[i + 1 :]
            await state.update_data(name=rest[0], phone=norm_phone(lines[i]), oid=None)
            await search_address(m, state, ", ".join(rest[1:]))
            return
    phone = norm_phone(text)
    if not phone:
        await m.answer("Неверный номер. Пример: 0700123456")
        return
    last = await db.last_by_phone(phone)
    await state.update_data(phone=phone, oid=None)
    if last:
        await state.update_data(name=last.name, address=last.address, lat=last.lat, lon=last.lon)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [Btn(text=f"↩️ {last.name} · {last.address}"[:60], callback_data="no:same")],
                [Btn(text="✍️ Другие данные", callback_data="no:new")],
            ]
        )
        await m.answer("Клиент уже есть:", reply_markup=kb)
        return
    await state.set_state(New.name)
    await m.answer("👤 Имя клиента")


@router.callback_query(New.contact, F.data == "no:same")
async def same(c: CallbackQuery, state: FSMContext, db, bot: Bot):
    d = await state.get_data()
    await state.clear()
    await c.message.edit_reply_markup()
    await c.answer()
    await create(c.message, db, bot, c.from_user, d["name"], d["phone"], d["address"], d.get("lat"), d.get("lon"))


@router.callback_query(New.contact, F.data == "no:new")
async def other(c: CallbackQuery, state: FSMContext):
    await state.set_state(New.name)
    await c.message.edit_reply_markup()
    await c.message.answer("👤 Имя клиента")
    await c.answer()


@router.message(New.name, NOT_MENU)
async def got_name(m: Message, state: FSMContext):
    if not (m.text or "").strip():
        await m.answer("Введите имя текстом")
        return
    await state.update_data(name=m.text.strip())
    await ask_address(m, state)


# ---------- адрес: поиск -> выбор точного варианта -> детали ----------
async def ask_address(m: Message, state: FSMContext):
    await state.set_state(Addr.query)
    await m.answer("📍 Адрес: улица и дом\n(например: Токтогула 125)")


async def search_address(m: Message, state: FSMContext, text: str):
    wait = await m.answer("🔎 Ищу адрес…")
    cands = await geo.search(text)
    await state.update_data(q=text, cands=cands)
    await state.set_state(Addr.pick)
    rows = [[Btn(text=f"📍 {c['label']}"[:60], callback_data=f"ap:{i}")] for i, c in enumerate(cands)]
    rows.append([Btn(text="✍️ Оставить как написал", callback_data="ap:t")])
    head = (
        "Выберите точный адрес (или напишите уточнённый):"
        if cands
        else "😕 Не нашёл. Напишите адрес иначе, отправьте точку на карте (📎 → Геопозиция) или оставьте как написано:"
    )
    await wait.edit_text(head, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def ask_detail(m: Message, state: FSMContext, label: str, lat, lon):
    await state.update_data(label=label, lat=lat, lon=lon)
    await state.set_state(Addr.detail)
    kb = InlineKeyboardMarkup(inline_keyboard=[[Btn(text="Пропустить", callback_data="ad:skip")]])
    await m.answer(f"📌 <b>{esc(label)}</b>\n🏢 Подъезд / этаж / кв. (например: п.2 эт.5 кв.20)", reply_markup=kb)


async def finish_address(m: Message, state: FSMContext, db, bot: Bot, who, detail: str | None):
    d = await state.get_data()
    await state.clear()
    address = d["label"] + (f", {detail}" if detail else "")
    if d.get("oid"):
        o = await db.update_fields(
            d["oid"], {"address": address, "lat": d.get("lat"), "lon": d.get("lon")},
            who.id, who.full_name, "изменён адрес",
        )
        if o is None:
            await m.answer("Заказ не найден или уже закрыт — изменить нельзя")
            return
        await show(m, o, "✅ Адрес изменён\n")
        spawn(order_changed(bot, o))
    else:
        await create(m, db, bot, who, d["name"], d["phone"], address, d.get("lat"), d.get("lon"))


@router.message(Addr.query, NOT_MENU)
async def addr_query(m: Message, state: FSMContext):
    if not (m.text or "").strip():
        await m.answer("Введите адрес текстом")
        return
    await search_address(m, state, m.text.strip())


@router.message(Addr.pick, NOT_MENU, F.text)
async def addr_retype(m: Message, state: FSMContext):
    await search_address(m, state, m.text.strip())


@router.message(Addr.pick, F.location)
async def addr_location(m: Message, state: FSMContext):
    d = await state.get_data()
    await ask_detail(m, state, d["q"], m.location.latitude, m.location.longitude)


@router.callback_query(Addr.pick, F.data.startswith("ap:"))
async def addr_pick(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    k = c.data.split(":")[1]
    await c.answer()
    await c.message.edit_reply_markup()
    if k == "t":
        await ask_detail(c.message, state, d["q"], None, None)
        return
    cands = d.get("cands") or []
    i = int(k)
    if i >= len(cands):
        return
    await ask_detail(c.message, state, cands[i]["label"], cands[i]["lat"], cands[i]["lon"])


@router.message(Addr.detail, NOT_MENU)
async def addr_detail(m: Message, state: FSMContext, db, bot: Bot):
    t = (m.text or "").strip()
    await finish_address(m, state, db, bot, m.from_user, None if t in {"", "-", "—"} else t)


@router.callback_query(Addr.detail, F.data == "ad:skip")
async def addr_skip(c: CallbackQuery, state: FSMContext, db, bot: Bot):
    await c.answer()
    await c.message.edit_reply_markup()
    await finish_address(c.message, state, db, bot, c.from_user, None)


# ---------- список / поиск ----------
def row(o: Order) -> list[Btn]:
    return [Btn(text=f"№{o.id} {STATUS[o.status][:1]} {o.name[:14]} · {o.address[:22]}", callback_data=f"od:{o.id}")]


async def list_view(db, hist: bool, offset: int):
    if hist:
        total = await db.archive_count()
        orders = await db.archive_page(offset, PAGE)
    else:
        allo = sorted(await db.list_active(), key=lambda o: -o.id)
        total = len(allo)
        orders = allo[offset : offset + PAGE]
    h = "1" if hist else "0"
    rows = [row(o) for o in orders]
    nav = []
    if offset > 0:
        nav.append(Btn(text="◀️", callback_data=f"ol:{h}:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(Btn(text="▶️", callback_data=f"ol:{h}:{offset + PAGE}"))
    if nav:
        rows.append(nav)
    rows.append([Btn(text="📋 Активные" if hist else "🗄 Архив", callback_data=f"ol:{'0' if hist else '1'}:0")])
    title = f"{'🗄 Архив' if hist else '📋 Активные'}: {total}"
    return title, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "📋 Заказы")
async def orders_list(m: Message, state: FSMContext, db):
    await state.clear()
    title, kb = await list_view(db, False, 0)
    await m.answer(title, reply_markup=kb)


@router.callback_query(F.data.startswith("ol:"))
async def orders_page(c: CallbackQuery, db):
    _, h, off = c.data.split(":")
    title, kb = await list_view(db, h == "1", int(off))
    await c.message.edit_text(title, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("od:"))
async def order_open(c: CallbackQuery, db):
    o = await db.get_order(int(c.data.split(":")[1]))
    await c.answer()
    if o:
        await show(c.message, o)


@router.callback_query(F.data.startswith("oh:"))
async def order_history(c: CallbackQuery, db):
    o = await db.get_order(int(c.data.split(":")[1]))
    await c.answer()
    if o:
        await c.message.answer(history_text(o))


@router.message(F.text == "🔎 Поиск")
async def find_start(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Find.q)
    await m.answer("Номер заказа, телефон или имя:")


async def search(m: Message, db, q: str):
    found = await db.find_orders(q)
    if not found:
        await m.answer("Ничего не найдено")
    elif len(found) == 1:
        await show(m, found[0])
    else:
        await m.answer("Найдено:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[row(o) for o in found]))


@router.message(Find.q, NOT_MENU)
async def find_do(m: Message, state: FSMContext, db):
    await state.clear()
    await search(m, db, m.text or "")


@router.message(StateFilter(None), F.text.regexp(r"^#?\d{1,6}$"))
async def quick_number(m: Message, db):
    await search(m, db, m.text)


# ---------- отмена ----------
@router.callback_query(F.data.startswith("oc:"))
async def cancel_ask(c: CallbackQuery):
    oid = c.data.split(":")[1]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[Btn(text="Да, отменить", callback_data=f"ok:{oid}"), Btn(text="Нет", callback_data=f"on:{oid}")]]
    )
    await c.message.edit_reply_markup(reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("on:"))
async def cancel_no(c: CallbackQuery, db):
    o = await db.get_order(int(c.data.split(":")[1]))
    await c.message.edit_reply_markup(reply_markup=kb_order(o) if o else None)
    await c.answer()


async def after_cancel(bot: Bot, o: Order, actor) -> None:
    if o.prev_status in ("new", "ready"):  # убрать карточки у курьеров
        await retire_cards(bot, o.id, f"❌ <b>№{o.id}</b> отменён")
    if o.courier_id and o.prev_status in ("picked", "delivering"):  # курьер, у которого заказ на руках
        hint = "Не сдавайте его в цех." if o.prev_status == "picked" else "Не доставляйте клиенту."
        await safe_send(bot, o.courier_id, f"❌ Заказ <b>№{o.id}</b> ({esc(o.name)}) отменён диспетчером. {hint}")
    await status_event(bot, o, actor.full_name, exclude=actor.id)


@router.callback_query(F.data.startswith("ok:"))
async def cancel_do(c: CallbackQuery, db, bot: Bot):
    oid = int(c.data.split(":")[1])
    o = await db.transition(oid, CANCELLABLE, "cancelled", c.from_user.id, c.from_user.full_name)
    if o is None:
        cur = await db.get_order(oid)
        msg = "Заказ в цехе — отменить нельзя" if cur and cur.status == "shop" else "Уже нельзя отменить"
        await c.answer(msg, show_alert=True)
        if cur:
            await c.message.edit_reply_markup(reply_markup=kb_order(cur))
        return
    await c.message.edit_text(card(o, f" · {STATUS['cancelled']}"), reply_markup=kb_order(o))
    await c.answer("Отменён")
    spawn(after_cancel(bot, o, c.from_user))


# ---------- изменение ----------
FIELDS = {"n": ("name", "👤 Имя"), "p": ("phone", "📞 Номер"), "a": ("address", "📍 Адрес")}


@router.callback_query(F.data.startswith("oe:"))
async def edit_menu(c: CallbackQuery):
    oid = c.data.split(":")[1]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[Btn(text=label, callback_data=f"of:{oid}:{k}") for k, (_, label) in FIELDS.items()]]
    )
    await c.message.edit_reply_markup(reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("of:"))
async def edit_field(c: CallbackQuery, state: FSMContext, db):
    _, oid, k = c.data.split(":")
    o = await db.get_order(int(oid))
    if o is None or o.status not in ACTIVE:
        await c.answer("Заказ уже закрыт — изменить нельзя", show_alert=True)
        return
    await state.update_data(oid=int(oid), k=k)
    await c.answer()
    if k == "a":
        await ask_address(c.message, state)
        return
    await state.set_state(Edit.value)
    await c.message.answer(f"Новое значение: {FIELDS[k][1]}")


@router.message(Edit.value, NOT_MENU)
async def edit_save(m: Message, state: FSMContext, db, bot: Bot):
    d = await state.get_data()
    value = (m.text or "").strip()
    field, label = FIELDS[d["k"]]
    if field == "phone":
        value = norm_phone(value)
        if not value:
            await m.answer("Неверный номер. Пример: 0700123456")
            return
    if not value:
        await m.answer("Введите текстом")
        return
    await state.clear()
    o = await db.update_fields(d["oid"], {field: value}, m.from_user.id, m.from_user.full_name, f"изменено: {label}")
    if o is None:
        await m.answer("Заказ не найден или уже закрыт — изменить нельзя")
        return
    await show(m, o, "✅ Изменено\n")
    spawn(order_changed(bot, o))  # курьер увидит правку
