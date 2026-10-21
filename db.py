import os

from sqlalchemy import BigInteger, Boolean, Float, Integer, String, Text, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str | None] = mapped_column(String(16), index=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    on_shift: Mapped[bool] = mapped_column(Boolean, default=False)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)


class Order(Base):
    __tablename__ = "orders"
    # AUTOINCREMENT: номера 1, 2, 3... и никогда не повторяются
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(32), index=True)
    address: Mapped[str] = mapped_column(Text)
    # new -> picked (у водителя) -> shop (в прачечной) -> ready -> delivering (у водителя) -> delivered | cancelled
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    measurements: Mapped[str | None] = mapped_column(Text)
    courier_id: Mapped[int | None] = mapped_column(BigInteger)  # у кого заказ сейчас на руках


SessionLocal: async_sessionmaker[AsyncSession] | None = None
_engine = None


async def init_db(url: str):
    global SessionLocal, _engine
    if "sqlite" in url and ":///" in url:  # папка для файла базы должна существовать
        folder = os.path.dirname(os.path.abspath(url.split(":///", 1)[1]))
        os.makedirs(folder, exist_ok=True)
    _engine = create_async_engine(url, connect_args={"timeout": 15} if "sqlite" in url else {})
    if "sqlite" in url:

        @event.listens_for(_engine.sync_engine, "connect")
        def _pragmas(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    SessionLocal = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if "sqlite" in url:  # мини-миграция для базы предыдущей версии
            cols = {r[1] for r in (await conn.exec_driver_sql("PRAGMA table_info(orders)")).fetchall()}
            if "courier_id" not in cols:
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN courier_id BIGINT")


async def close_db():
    if _engine is not None:
        await _engine.dispose()
