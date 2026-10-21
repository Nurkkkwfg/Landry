"""Разовый перенос данных из старой SQLite-базы в Firestore.

Запуск (из папки проекта, с заполненным .env — BOT_TOKEN не нужен, нужен FIREBASE_CREDENTIALS[_FILE]):
    python migrate_sqlite.py путь/к/laundry.db

Безопасно запускать повторно: записи с теми же номерами перезаписываются, счётчик заказов не уменьшается.
"""
import asyncio
import sqlite3
import sys

import db
from config import FIREBASE_CREDENTIALS, FIREBASE_CREDENTIALS_FILE
from db import now


async def main(path: str) -> None:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    ts = now()
    users = []
    for r in con.execute("SELECT * FROM users"):
        users.append(
            {
                "id": r["id"],
                "username": r["username"],
                "full_name": r["full_name"] or "",
                "role": r["role"],
                "approved": bool(r["approved"]),
                "on_shift": bool(r["on_shift"]),
                "lat": r["lat"],
                "lon": r["lon"],
                "shift_at": ts if r["on_shift"] else None,
            }
        )
    cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
    orders = []
    for r in con.execute("SELECT * FROM orders"):
        orders.append(
            {
                "id": r["id"],
                "name": r["name"],
                "phone": r["phone"],
                "address": r["address"],
                "status": r["status"],
                "lat": r["lat"],
                "lon": r["lon"],
                "measurements": r["measurements"],
                "courier_id": r["courier_id"] if "courier_id" in cols else None,
                "created_at": ts,
                "status_at": ts,
                "updated_at": ts,
                "history": [{"status": r["status"], "at": ts, "by": None, "by_name": "", "note": "импорт из SQLite"}],
            }
        )
    con.close()
    await db.store.init(FIREBASE_CREDENTIALS, FIREBASE_CREDENTIALS_FILE)
    try:
        await db.store.import_docs(users, orders)
    finally:
        await db.store.close()
    print(f"Готово: сотрудников {len(users)}, заказов {len(orders)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Использование: python migrate_sqlite.py путь/к/laundry.db")
    asyncio.run(main(sys.argv[1]))
