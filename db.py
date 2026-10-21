"""Хранилище на Firestore.

Коллекции:
  users/{telegram_id}   — сотрудники
  orders/{номер}        — заказы (история статусов лежит внутри документа, поле history)
  meta/counters         — счётчик номеров заказов (1, 2, 3… без повторов)
  fsm/{ключ}            — состояния диалогов (чтобы переживали перезапуск)

Все запросы — только по равенству либо по одному полю, поэтому составные индексы создавать не нужно.
"""
import inspect
import json
import logging
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone

from google.api_core.exceptions import NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import service_account

log = logging.getLogger(__name__)

KEEP = object()  # «не менять courier_id»
ACTIVE = ("new", "picked", "shop", "ready", "delivering")
TERMINAL = ("delivered", "cancelled")


def now() -> datetime:
    return datetime.now(timezone.utc)


def _f(name: str, op: str, value) -> FieldFilter:
    return FieldFilter(name, op, value)


def _idx(name=None, phone=None) -> dict:
    """Служебные поля для поиска (Firestore не умеет «содержит»)."""
    out = {}
    if name is not None:
        out["name_lc"] = str(name).strip().lower()
    if phone is not None:
        out["phone9"] = re.sub(r"\D", "", str(phone))[-9:]
    return out


@dataclass
class User:
    id: int
    username: str | None = None
    full_name: str = ""
    role: str | None = None
    approved: bool = False
    on_shift: bool = False
    lat: float | None = None
    lon: float | None = None
    shift_at: datetime | None = None


@dataclass
class Order:
    id: int
    name: str = ""
    phone: str = ""
    address: str = ""
    # new -> picked (у водителя) -> shop (в прачечной) -> ready -> delivering (у водителя) -> delivered | cancelled
    status: str = "new"
    lat: float | None = None
    lon: float | None = None
    measurements: str | None = None
    courier_id: int | None = None
    created_at: datetime | None = None
    status_at: datetime | None = None
    reminded_at: datetime | None = None
    cards: list = field(default_factory=list)  # карточки задач, разосланные курьерам: [{c: chat, m: message}]
    history: list = field(default_factory=list)  # [{status, at, by, by_name, note}]
    prev_status: str | None = None  # не хранится: заполняется после transition()


def _build(cls, doc_id, d: dict):
    names = {f.name for f in fields(cls)}
    kw = {k: v for k, v in (d or {}).items() if k in names and k != "id"}
    return cls(id=int(doc_id), **kw)


async def _all(query) -> list:
    return [s async for s in query.stream()]


def _normalize_order(oid: int, d: dict) -> dict:
    d = dict(d)
    d.pop("id", None)
    d.update(_idx(d.get("name", ""), d.get("phone", "")))
    d["active"] = d.get("status") in ACTIVE
    if d["active"]:
        d.pop("arch", None)
    else:
        d["arch"] = oid  # поле только у завершённых — по нему идёт архив без составных индексов
    d.setdefault("cards", [])
    d.setdefault("history", [])
    d.setdefault("reminded_at", None)
    ts = now()
    for k in ("created_at", "status_at", "updated_at"):
        d.setdefault(k, ts)
    return d


class Store:
    def __init__(self):
        self.client: firestore.AsyncClient | None = None

    # ---------- подключение ----------
    async def init(self, creds_json: str = "", creds_file: str = "") -> None:
        if creds_json:
            info = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(info)
            self.client = firestore.AsyncClient(project=info["project_id"], credentials=creds)
        elif creds_file:
            creds = service_account.Credentials.from_service_account_file(creds_file)
            self.client = firestore.AsyncClient(project=creds.project_id, credentials=creds)
        else:
            self.client = firestore.AsyncClient()  # GOOGLE_APPLICATION_CREDENTIALS / окружение GCP
        await self.client.collection("meta").document("counters").get()  # проверка доступа сразу при старте

    async def close(self) -> None:
        if self.client is not None:
            try:
                r = self.client.close()
                if inspect.isawaitable(r):
                    await r
            except Exception:
                pass

    @property
    def users(self):
        return self.client.collection("users")

    @property
    def orders(self):
        return self.client.collection("orders")

    @property
    def fsm(self):
        return self.client.collection("fsm")

    @property
    def counter(self):
        return self.client.collection("meta").document("counters")

    # ---------- сотрудники ----------
    async def get_user(self, uid: int) -> User | None:
        s = await self.users.document(str(uid)).get()
        return _build(User, s.id, s.to_dict()) if s.exists else None

    async def create_user(self, u: User) -> None:
        data = {f.name: getattr(u, f.name) for f in fields(User) if f.name != "id"}
        await self.users.document(str(u.id)).set(data)

    async def update_user(self, uid: int, **changes) -> bool:
        try:
            await self.users.document(str(uid)).update(changes)
            return True
        except NotFound:
            return False

    async def delete_user(self, uid: int) -> None:
        await self.users.document(str(uid)).delete()

    async def list_users(self) -> list[User]:
        return [_build(User, s.id, s.to_dict()) for s in await _all(self.users)]

    async def users_by_role(self, role: str) -> list[User]:
        q = self.users.where(filter=_f("role", "==", role)).where(filter=_f("approved", "==", True))
        return [_build(User, s.id, s.to_dict()) for s in await _all(q)]

    async def couriers_on_shift(self) -> list[User]:
        q = (
            self.users.where(filter=_f("role", "==", "courier"))
            .where(filter=_f("approved", "==", True))
            .where(filter=_f("on_shift", "==", True))
        )
        return [_build(User, s.id, s.to_dict()) for s in await _all(q)]

    async def stale_shifts(self, before: datetime) -> list[User]:
        users = await self.couriers_on_shift()
        return [u for u in users if u.shift_at is None or u.shift_at < before]

    # ---------- заказы: чтение ----------
    async def get_order(self, oid: int) -> Order | None:
        s = await self.orders.document(str(oid)).get()
        return _build(Order, s.id, s.to_dict()) if s.exists else None

    async def list_active(self) -> list[Order]:
        q = self.orders.where(filter=_f("active", "==", True))
        return sorted((_build(Order, s.id, s.to_dict()) for s in await _all(q)), key=lambda o: o.id)

    async def by_status(self, statuses: tuple) -> list[Order]:
        q = self.orders.where(filter=_f("status", "in", list(statuses)))
        return [_build(Order, s.id, s.to_dict()) for s in await _all(q)]

    async def tasks_for(self, uid: int) -> list[Order]:
        """Свободные задачи (new/ready) + доставки этого курьера."""
        free = await self.by_status(("new", "ready"))
        q = self.orders.where(filter=_f("status", "==", "delivering")).where(filter=_f("courier_id", "==", uid))
        return free + [_build(Order, s.id, s.to_dict()) for s in await _all(q)]

    async def held(self, uid: int) -> list[Order]:
        q = self.orders.where(filter=_f("status", "==", "picked")).where(filter=_f("courier_id", "==", uid))
        return sorted((_build(Order, s.id, s.to_dict()) for s in await _all(q)), key=lambda o: o.id)

    async def last_by_phone(self, phone: str) -> Order | None:
        q = self.orders.where(filter=_f("phone", "==", phone))
        found = [_build(Order, s.id, s.to_dict()) for s in await _all(q)]
        return max(found, key=lambda o: o.id) if found else None

    async def archive_count(self) -> int:
        res = await self.orders.where(filter=_f("arch", ">", 0)).count().get()
        return int(res[0][0].value)

    async def archive_page(self, offset: int, limit: int) -> list[Order]:
        q = (
            self.orders.where(filter=_f("arch", ">", 0))
            .order_by("arch", direction=firestore.Query.DESCENDING)
            .offset(offset)
            .limit(limit)
        )
        return [_build(Order, s.id, s.to_dict()) for s in await _all(q)]

    async def find_orders(self, q: str) -> list[Order]:
        q = q.strip().lstrip("#")
        digits = re.sub(r"\D", "", q)
        if q.isdigit() and len(q) <= 6:
            o = await self.get_order(int(q))
            return [o] if o else []
        found: dict[int, Order] = {}
        if len(digits) >= 4:
            if len(digits) >= 9:  # полный номер: точное совпадение по последним 9 цифрам
                qs = self.orders.where(filter=_f("phone9", "==", digits[-9:]))
            else:  # часть номера: по началу (без ведущего 0)
                p = digits.lstrip("0") or digits
                qs = self.orders.where(filter=_f("phone9", ">=", p)).where(filter=_f("phone9", "<", p + "\uf8ff"))
            docs = await _all(qs.limit(50))
            extra = []
        else:
            ql = q.lower()
            if not ql:
                return []
            qs = self.orders.where(filter=_f("name_lc", ">=", ql)).where(filter=_f("name_lc", "<", ql + "\uf8ff"))
            docs = await _all(qs.limit(50))
            extra = [o for o in await self.list_active() if ql in o.name.lower()]  # «содержит» — среди активных
        for s in docs:
            found[int(s.id)] = _build(Order, s.id, s.to_dict())
        for o in extra:
            found[o.id] = o
        return sorted(found.values(), key=lambda o: -o.id)[:10]

    # ---------- заказы: запись ----------
    async def create_order(self, name, phone, address, lat, lon, actor_id=None, actor_name="") -> Order:
        """Номер берётся из счётчика в транзакции: без дублей и пропусков, даже при одновременных заявках."""

        @firestore.async_transactional
        async def run(tx):
            snap = await self.counter.get(transaction=tx)
            n = int((snap.to_dict() or {}).get("orders", 0)) + 1
            ts = now()
            entry = {"status": "new", "at": ts, "by": actor_id, "by_name": actor_name, "note": "создан"}
            data = {
                "name": name.strip(),
                "phone": phone,
                "address": address.strip(),
                "status": "new",
                "lat": lat,
                "lon": lon,
                "measurements": None,
                "courier_id": None,
                "created_at": ts,
                "status_at": ts,
                "updated_at": ts,
                "reminded_at": None,
                "cards": [],
                "history": [entry],
                "active": True,
                **_idx(name, phone),
            }
            tx.set(self.counter, {"orders": n}, merge=True)
            tx.set(self.orders.document(str(n)), data)
            return n, data

        n, data = await run(self.client.transaction())
        return _build(Order, n, data)

    async def transition(
        self, oid: int, src: tuple, dst: str, actor_id, actor_name: str = "", *, require_courier=None,
        set_courier=KEEP, note: str | None = None,
    ) -> Order | None:
        """Атомарная смена статуса. None — если заказ уже в другом статусе / у другого курьера.

        Именно эта проверка внутри транзакции не даёт двум курьерам взять один заказ."""
        ref = self.orders.document(str(oid))

        @firestore.async_transactional
        async def run(tx):
            snap = await ref.get(transaction=tx)
            if not snap.exists:
                return None
            d = snap.to_dict()
            if d.get("status") not in src:
                return None
            if require_courier is not None and d.get("courier_id") != require_courier:
                return None
            ts = now()
            entry = {"status": dst, "at": ts, "by": actor_id, "by_name": actor_name, "note": note}
            upd = {"status": dst, "status_at": ts, "updated_at": ts, "reminded_at": None}
            if set_courier is not KEEP:
                upd["courier_id"] = set_courier
            if dst in TERMINAL:
                upd["active"] = False
                upd["arch"] = oid
            tx.update(ref, {**upd, "history": firestore.ArrayUnion([entry])})
            prev = d["status"]
            d.update(upd)
            d["history"] = list(d.get("history") or []) + [entry]
            d["_prev"] = prev
            return d

        d = await run(self.client.transaction())
        if d is None:
            return None
        prev = d.pop("_prev")
        o = _build(Order, oid, d)
        o.prev_status = prev
        return o

    async def update_fields(self, oid: int, changes: dict, actor_id, actor_name: str, note: str) -> Order | None:
        """Правка данных заказа — только пока он активен."""
        ref = self.orders.document(str(oid))

        @firestore.async_transactional
        async def run(tx):
            snap = await ref.get(transaction=tx)
            if not snap.exists:
                return None
            d = snap.to_dict()
            if d.get("status") not in ACTIVE:
                return None
            ts = now()
            entry = {"status": d["status"], "at": ts, "by": actor_id, "by_name": actor_name, "note": note}
            upd = {**changes, **_idx(changes.get("name"), changes.get("phone")), "updated_at": ts}
            tx.update(ref, {**upd, "history": firestore.ArrayUnion([entry])})
            d.update(upd)
            d["history"] = list(d.get("history") or []) + [entry]
            return d

        d = await run(self.client.transaction())
        return _build(Order, oid, d) if d else None

    async def set_measurements(self, oid: int, text: str, actor_id, actor_name: str) -> Order | None:
        """Замеры сохраняются только если заказ ещё в цехе; иначе None (а не молчаливый успех)."""
        ref = self.orders.document(str(oid))

        @firestore.async_transactional
        async def run(tx):
            snap = await ref.get(transaction=tx)
            if not snap.exists:
                return None
            d = snap.to_dict()
            if d.get("status") != "shop":
                return None
            ts = now()
            entry = {"status": "shop", "at": ts, "by": actor_id, "by_name": actor_name, "note": "замеры"}
            upd = {"measurements": text, "updated_at": ts}
            tx.update(ref, {**upd, "history": firestore.ArrayUnion([entry])})
            d.update(upd)
            d["history"] = list(d.get("history") or []) + [entry]
            return d

        d = await run(self.client.transaction())
        return _build(Order, oid, d) if d else None

    async def add_cards(self, oid: int, cards: list) -> None:
        try:
            await self.orders.document(str(oid)).update({"cards": firestore.ArrayUnion(cards)})
        except NotFound:
            pass

    async def pop_cards(self, oid: int) -> list:
        ref = self.orders.document(str(oid))

        @firestore.async_transactional
        async def run(tx):
            snap = await ref.get(transaction=tx)
            if not snap.exists:
                return []
            cards = (snap.to_dict() or {}).get("cards") or []
            if cards:
                tx.update(ref, {"cards": []})
            return cards

        return await run(self.client.transaction())

    async def touch_reminded(self, ids: list[int]) -> None:
        ts = now()
        for i in range(0, len(ids), 400):
            batch = self.client.batch()
            for oid in ids[i : i + 400]:
                batch.update(self.orders.document(str(oid)), {"reminded_at": ts})
            await batch.commit()

    # ---------- бэкап / восстановление ----------
    async def export_all(self, days: int | None = None) -> dict:
        users = [{"id": int(s.id), **s.to_dict()} for s in await _all(self.users)]
        orders: dict[str, dict] = {}
        if days:
            q = self.orders.where(filter=_f("updated_at", ">=", now() - timedelta(days=days)))
            for s in await _all(q):
                orders[s.id] = {"id": int(s.id), **s.to_dict()}
            for s in await _all(self.orders.where(filter=_f("active", "==", True))):  # активные — всегда
                orders[s.id] = {"id": int(s.id), **s.to_dict()}
        else:
            for s in await _all(self.orders):
                orders[s.id] = {"id": int(s.id), **s.to_dict()}
        snap = await self.counter.get()
        return {
            "version": 1,
            "created": now().isoformat(),
            "days": days,
            "counter": int((snap.to_dict() or {}).get("orders", 0)) if snap.exists else 0,
            "users": users,
            "orders": list(orders.values()),
        }

    async def import_docs(self, users: list[dict], orders: list[dict], counter: int = 0) -> None:
        """Загрузка (upsert): документы с совпадающими номерами перезаписываются, остальные не трогаются."""
        ops: list[tuple] = []
        for u in users:
            d = {k: v for k, v in u.items() if k != "id"}
            ops.append((self.users.document(str(int(u["id"]))), d))
        top = counter
        for o in orders:
            oid = int(o["id"])
            top = max(top, oid)
            ops.append((self.orders.document(str(oid)), _normalize_order(oid, o)))
        for i in range(0, len(ops), 400):
            batch = self.client.batch()
            for ref, data in ops[i : i + 400]:
                batch.set(ref, data)
            await batch.commit()
        snap = await self.counter.get()
        cur = int((snap.to_dict() or {}).get("orders", 0)) if snap.exists else 0
        if top > cur:
            await self.counter.set({"orders": top}, merge=True)

    async def cleanup_fsm(self, older: timedelta) -> int:
        q = self.fsm.where(filter=_f("updated_at", "<", now() - older)).limit(300)
        n = 0
        for s in await _all(q):
            await s.reference.delete()
            n += 1
        return n


store = Store()
