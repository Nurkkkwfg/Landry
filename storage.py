"""FSM-хранилище aiogram в Firestore: диалоги (создание заявки, замеры…) переживают перезапуск бота."""
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from google.api_core.exceptions import NotFound

from db import Store, now


class FirestoreStorage(BaseStorage):
    def __init__(self, store: Store):
        self.store = store

    def _ref(self, key: StorageKey):
        parts = (key.bot_id, key.chat_id, key.user_id, key.thread_id or 0, key.destiny)
        return self.store.fsm.document("_".join(str(p) for p in parts))

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self._ref(key).set({"state": value, "updated_at": now()}, merge=True)

    async def get_state(self, key: StorageKey) -> str | None:
        snap = await self._ref(key).get()
        return (snap.to_dict() or {}).get("state") if snap.exists else None

    async def set_data(self, key: StorageKey, data) -> None:
        ref = self._ref(key)
        payload = {"data": dict(data), "updated_at": now()}
        try:
            await ref.update(payload)  # update целиком заменяет поле data
        except NotFound:
            await ref.set(payload)

    async def get_data(self, key: StorageKey) -> dict:
        snap = await self._ref(key).get()
        if not snap.exists:
            return {}
        return dict((snap.to_dict() or {}).get("data") or {})

    async def close(self) -> None:
        pass
