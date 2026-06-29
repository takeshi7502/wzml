import asyncio
from asyncio import Lock

from pyrogram import raw
from pyrogram.errors import AuthBytesInvalid

from .auth import get_auth_key
from .session import Session


class MtprotoPool:
    def __init__(self, clients):
        if isinstance(clients, dict):
            self._client_map = dict(clients)
            self._client_order = list(clients.keys())
        else:
            self._client_map = {i: c for i, c in enumerate(clients)}
            self._client_order = list(self._client_map.keys())
        self._sessions = {}
        self._locks = {}
        self._closed = False

    def _resolve_key(self, client_key):
        if client_key in self._client_map:
            return client_key
        if isinstance(client_key, int) and self._client_order:
            return self._client_order[client_key % len(self._client_order)]
        raise KeyError(f"Client key {client_key} not found")

    def _lock(self, client_key, dc_id):
        key = (client_key, dc_id)
        if key not in self._locks:
            self._locks[key] = Lock()
        return self._locks[key]

    async def get_session(self, client_key, dc_id, is_media=True, mode=3):
        ck = self._resolve_key(client_key)
        cache_key = (ck, dc_id)
        s = self._sessions.get(cache_key)
        if s and s.is_connected.is_set() and not s._closed:
            return s
        async with self._lock(ck, dc_id):
            s = self._sessions.get(cache_key)
            if s and s.is_connected.is_set() and not s._closed:
                return s
            if s:
                try:
                    await s.stop()
                except Exception:
                    pass
            client = self._client_map[ck]
            ak, is_cross = await get_auth_key(client, dc_id)
            s = Session(
                client, dc_id, ak, await client.storage.test_mode(), is_media=is_media
            )
            await s.start(mode=mode)
            if is_cross:
                for attempt in range(6):
                    try:
                        e = await client.invoke(
                            raw.functions.auth.ExportAuthorization(dc_id=dc_id)
                        )
                        await s.invoke(
                            raw.functions.auth.ImportAuthorization(
                                id=e.id, bytes=e.bytes
                            )
                        )
                        break
                    except AuthBytesInvalid:
                        await asyncio.sleep(1)
                else:
                    await s.stop()
                    raise RuntimeError(f"Auth export/import failed for DC {dc_id}")
            self._sessions[cache_key] = s
        return s

    async def execute(self, fn, client_key=0, dc_id=None, **kwargs):
        ck = self._resolve_key(client_key)
        client = self._client_map[ck]
        if dc_id is None:
            dc_id = await client.storage.dc_id()
        session = await self.get_session(ck, dc_id)
        return await fn(session, client, **kwargs)

    async def drop_session(self, client_key, dc_id):
        ck = self._resolve_key(client_key)
        cache_key = (ck, dc_id)
        s = self._sessions.pop(cache_key, None)
        if s:
            try:
                await s.stop()
            except Exception:
                pass

    async def stop(self):
        self._closed = True
        for s in self._sessions.values():
            try:
                await s.stop()
            except Exception:
                pass
        self._sessions.clear()
