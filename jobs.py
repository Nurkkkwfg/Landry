"""Фоновые задачи: напоминания о зависших заказах, авто-завершение смен, чистка старых диалогов."""
import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot

import db
from common import STATUS, dispatcher_ids, esc, safe_send
from config import BISHKEK, REMIND_AFTER_MIN, REMIND_FROM_HOUR, REMIND_REPEAT_MIN, REMIND_TO_HOUR, SHIFT_MAX_HOURS
from db import now

log = logging.getLogger(__name__)
TICK = 600  # проверка раз в 10 минут


def human(td: timedelta) -> str:
    minutes = int(td.total_seconds() // 60)
    d, rem = divmod(minutes, 1440)
    h, m = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} д")
    if h:
        parts.append(f"{h} ч")
    if m and not d:
        parts.append(f"{m} мин")
    return " ".join(parts) or "0 мин"


async def end_stale_shifts(bot: Bot) -> None:
    cutoff = now() - timedelta(hours=SHIFT_MAX_HOURS)
    for u in await db.store.stale_shifts(cutoff):
        await db.store.update_user(u.id, on_shift=False, lat=None, lon=None)
        text = f"🔴 Смена завершена автоматически (прошло больше {SHIFT_MAX_HOURS} ч). Чтобы продолжить — «🟢 Начать смену»."
        held = await db.store.held(u.id)
        if held:
            text += f"\n⚠️ У вас на руках заказов: {len(held)} — сдайте в цех."
        await safe_send(bot, u.id, text)


async def send_reminders(bot: Bot) -> None:
    if not (REMIND_FROM_HOUR <= datetime.now(BISHKEK).hour < REMIND_TO_HOUR):
        return
    ts = now()
    overdue = []
    for o in await db.store.list_active():
        limit = REMIND_AFTER_MIN.get(o.status)
        if not limit or not o.status_at:
            continue
        age = ts - o.status_at
        if age < timedelta(minutes=limit):
            continue
        if o.reminded_at and ts - o.reminded_at < timedelta(minutes=REMIND_REPEAT_MIN):
            continue
        overdue.append((o, age))
    if not overdue:
        return
    overdue.sort(key=lambda x: -x[1].total_seconds())
    lines = [f"№{o.id} · {esc(o.name)} — {STATUS[o.status]}, {human(age)}" for o, age in overdue[:25]]
    if len(overdue) > 25:
        lines.append(f"…и ещё {len(overdue) - 25}")
    text = "⏰ <b>Зависшие заказы:</b>\n" + "\n".join(lines)
    sent = await asyncio.gather(*(safe_send(bot, i, text) for i in await dispatcher_ids()))
    # курьеру — про заказы, которые у него на руках
    for o, age in overdue:
        if o.status in ("picked", "delivering") and o.courier_id:
            await safe_send(bot, o.courier_id, f"⏰ Заказ <b>№{o.id}</b> у вас уже {human(age)} — {STATUS[o.status]}")
    if any(sent):
        await db.store.touch_reminded([o.id for o, _ in overdue])


async def reminders_loop(bot: Bot) -> None:
    await asyncio.sleep(60)
    n = 0
    while True:
        try:
            await end_stale_shifts(bot)
            await send_reminders(bot)
            if n % 36 == 0:  # раз в ~6 часов
                await db.store.cleanup_fsm(timedelta(days=3))
        except Exception:
            log.exception("jobs tick failed")
        n += 1
        await asyncio.sleep(TICK)
