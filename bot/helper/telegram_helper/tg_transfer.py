from asyncio import Event, gather, sleep

from pyrogram import raw, utils
from pyrogram.connection import Connection
from pyrogram.errors import AuthBytesInvalid
from pyrogram.file_id import FileType, ThumbnailSource

from ... import LOGGER
from ...core.tg_client import TgClient
from ...hyper_mtproto import MtprotoPool
from ...hyper_mtproto.auth import get_auth_key
from ...hyper_mtproto.session import Session as HyperSession

_orig_connection_close = Connection.close


def _safe_connection_close(self):
    if self.protocol is not None:
        return _orig_connection_close(self)


Connection.close = _safe_connection_close

MB = 1024 * 1024


class HypertgTransfer:
    def __init__(self, obj):
        self._obj = obj
        self._listener = obj._listener
        self.clients = dict(TgClient.helper_bots)
        self.work_loads = dict(TgClient.helper_loads)
        self.client_ids = list(self.clients.keys())
        if TgClient.helper_users:
            for no, client in TgClient.helper_users.items():
                self.clients[-no] = client
                self.client_ids.append(-no)
            for no, load in TgClient.helper_user_loads.items():
                self.work_loads[-no] = load
        if TgClient.user and all(c is not TgClient.user for c in self.clients.values()):
            key = -(len(TgClient.helper_users) + 1)
            self.clients[key] = TgClient.user
            self.client_ids.append(key)
            self.work_loads[key] = 0
        self.num_clients = len(self.clients)
        self._pool = MtprotoPool(self.clients)
        self._cancel = Event()
        self._tasks = []
        LOGGER.info(
            f"HypertgTransfer init clients={self.num_clients} "
            f"loads={dict(TgClient.helper_loads)}"
        )

    def _client_idx(self, client):
        for i, c in self.clients.items():
            if c is client:
                return i
        return None

    async def _mk_session(self, client, dc_id, mode=3):
        idx = self._client_idx(client)
        if idx is not None:
            return await self._pool.get_session(idx, dc_id, is_media=True, mode=mode)
        ak, is_cross = await get_auth_key(client, dc_id)
        s = HyperSession(
            client, dc_id, ak, await client.storage.test_mode(), is_media=True
        )
        await s.start(mode=mode)
        if is_cross:
            for attempt in range(6):
                try:
                    e = await client.invoke(
                        raw.functions.auth.ExportAuthorization(dc_id=dc_id)
                    )
                    await s.invoke(
                        raw.functions.auth.ImportAuthorization(id=e.id, bytes=e.bytes)
                    )
                    break
                except AuthBytesInvalid:
                    await sleep(1)
            else:
                await s.stop()
                raise AuthBytesInvalid
        return s

    async def _get_session(self, idx, dc_id, force=False):
        if force:
            await self._pool.drop_session(idx, dc_id)
        return await self._pool.get_session(idx, dc_id, is_media=True, mode=3)

    async def _warmup(self, indices, dc_id):
        async def _w(i):
            try:
                await self._pool.get_session(i, dc_id)
            except Exception as e:
                LOGGER.warning(f"HypertgTransfer warmup fail client {i}: {e}")

        await gather(*[_w(i) for i in indices])

    async def _close_all(self):
        await self._pool.stop()

    @staticmethod
    def _location(fid):
        ft = fid.file_type
        if ft == FileType.CHAT_PHOTO:
            if fid.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=fid.chat_id, access_hash=fid.chat_access_hash
                )
            elif fid.chat_access_hash == 0:
                peer = raw.types.InputPeerChat(chat_id=-fid.chat_id)
            else:
                peer = raw.types.InputPeerChannel(
                    channel_id=utils.get_channel_id(fid.chat_id),
                    access_hash=fid.chat_access_hash,
                )
            loc = raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=fid.volume_id,
                local_id=fid.local_id,
                big=fid.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
            return loc
        if ft == FileType.PHOTO:
            loc = raw.types.InputPhotoFileLocation(
                id=fid.media_id,
                access_hash=fid.access_hash,
                file_reference=fid.file_reference,
                thumb_size=fid.thumbnail_size,
            )
            return loc
        loc = raw.types.InputDocumentFileLocation(
            id=fid.media_id,
            access_hash=fid.access_hash,
            file_reference=fid.file_reference,
            thumb_size=fid.thumbnail_size,
        )
        return loc

    async def cancel(self):
        self._cancel.set()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await gather(*self._tasks, return_exceptions=True)
        await self._close_all()
