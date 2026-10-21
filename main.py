import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ErrorEvent

import db
import jobs
from common import Ctx, spawn
from config import ADMIN_IDS, BOT_TOKEN, FIREBASE_CREDENTIALS, FIREBASE_CREDENTIALS_FILE
from handlers import access, backup, courier, laundry, orders
from storage import FirestoreStorage

log = logging.getLogger("bot")


async def on_error(event: ErrorEvent) -> bool:
    log.exception("update failed", exc_info=event.exception)
    upd = event.update
    try:
        if upd.callback_query:
            await upd.callback_query.answer("❌ Ошибка, попробуйте ещё раз", show_alert=True)
        elif upd.message:
            await upd.message.answer("❌ Ошибка, попробуйте ещё раз (/start)")
    except Exception:
        pass
    return True


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=FirestoreStorage(db.store))
    ctx = Ctx()
    for obs in (dp.message, dp.edited_message, dp.callback_query):
        obs.outer_middleware(ctx)
    for r in (access.router, access.admin_router, backup.router, orders.router, courier.router, laundry.router, access.fallback):
        dp.include_router(r)
    dp.errors.register(on_error)
    return dp


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN or not ADMIN_IDS:
        sys.exit("Заполните .env: BOT_TOKEN и ADMIN_IDS (см. .env.example)")
    if not (FIREBASE_CREDENTIALS or FIREBASE_CREDENTIALS_FILE):
        sys.exit("Не задан FIREBASE_CREDENTIALS (или FIREBASE_CREDENTIALS_FILE) — см. FIREBASE_SETUP.md")
    try:
        await db.store.init(FIREBASE_CREDENTIALS, FIREBASE_CREDENTIALS_FILE)
    except Exception as e:
        sys.exit(f"Не удалось подключиться к Firestore: {e}")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Бот @%s запущен", (await bot.get_me()).username)
        spawn(backup.daily_backup(bot))
        spawn(jobs.reminders_loop(bot))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await db.store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
