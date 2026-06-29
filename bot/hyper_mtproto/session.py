import asyncio
import bisect
import logging
import os
from hashlib import sha1
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

import pyrogram
from pyrogram import raw
from pyrogram.errors import (
    RPCError,
    InternalServerError,
    AuthKeyDuplicated,
    FloodWait,
    FloodPremiumWait,
    ServiceUnavailable,
    BadMsgNotification,
    SecurityCheckMismatch,
)
from pyrogram.raw.all import layer
from pyrogram.raw.core import TLObject, MsgContainer, Int, FutureSalts

from .connection import Connection
from .crypto import mtproto
from .msg_factory import MsgFactory
from .msg_id import MsgId

log = logging.getLogger(__name__)


class Result:
    def __init__(self):
        self.value = None
        self.event = asyncio.Event()


class Session:
    START_TIMEOUT = 1
    WAIT_TIMEOUT = 15
    SLEEP_THRESHOLD = 10
    MAX_RETRIES = 5
    ACKS_THRESHOLD = 8
    PING_INTERVAL = 5
    STORED_MSG_IDS_MAX_SIZE = 1000 * 2

    TRANSPORT_ERRORS = {
        404: "auth key not found",
        429: "transport flood",
        444: "invalid DC",
    }

    CUR_ALWD_INNR_QRYS = (
        raw.functions.InvokeWithoutUpdates,
        raw.functions.InvokeWithTakeout,
        raw.functions.InvokeWithBusinessConnection,
    )

    def __init__(
        self,
        client: "pyrogram.Client",
        dc_id: int,
        auth_key: bytes,
        test_mode: bool,
        is_media: bool = False,
        is_cdn: bool = False,
    ):
        self.client = client
        self.dc_id = dc_id
        self.auth_key = auth_key
        self.test_mode = test_mode
        self.is_media = is_media
        self.is_cdn = is_cdn

        self.connection = None

        self.auth_key_id = sha1(auth_key).digest()[-8:]

        self.session_id = os.urandom(8)
        self.msg_factory = MsgFactory()

        self.salt = 0

        self.pending_acks = set()

        self.results = {}

        self.stored_msg_ids = []

        self.ping_task = None
        self.ping_task_event = asyncio.Event()

        self.network_task = None

        self.is_connected = asyncio.Event()

        self._closed = False

        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"hmtp-{dc_id}"
        )

    async def start(self, mode=3):
        self._closed = False
        self.mode = mode
        self.session_id = os.urandom(8)
        self.salt = 0
        self.msg_factory = MsgFactory()
        self.stored_msg_ids = []
        self.pending_acks = set()

        while True:
            self.connection = Connection(
                self.dc_id,
                self.test_mode,
                self.client.ipv6,
                self.client.proxy,
                self.is_media,
                mode=mode,
            )

            try:
                await self.connection.connect()

                self.network_task = self.client.loop.create_task(self.network_worker())

                await self.send(
                    raw.functions.Ping(ping_id=0), timeout=self.START_TIMEOUT
                )

                if not self.is_cdn:
                    await self.send(
                        raw.functions.InvokeWithLayer(
                            layer=layer,
                            query=raw.functions.InitConnection(
                                api_id=await self.client.storage.api_id(),
                                app_version=self.client.app_version,
                                device_model=self.client.device_model,
                                system_version=self.client.system_version,
                                system_lang_code=self.client.lang_code,
                                lang_code=self.client.lang_code,
                                lang_pack="",
                                query=raw.functions.help.GetConfig(),
                                proxy=raw.types.InputClientProxy(
                                    address=self.client._un_docu_gnihts[0],
                                    port=self.client._un_docu_gnihts[1],
                                )
                                if len(self.client._un_docu_gnihts) == 3
                                else None,
                                params=self.client._un_docu_gnihts[2]
                                if len(self.client._un_docu_gnihts) == 3
                                else None,
                            ),
                        ),
                        timeout=self.START_TIMEOUT,
                    )

                self.ping_task = self.client.loop.create_task(self.ping_worker())

                log.info(f"Session initialized: Layer {layer}")
                log.info(
                    f"Device: {self.client.device_model} - {self.client.app_version}"
                )
                log.info(
                    f"System: {self.client.system_version} ({self.client.lang_code.upper()})"
                )

            except AuthKeyDuplicated as e:
                await self.stop()
                raise e
            except (OSError, TimeoutError, RPCError):
                await self.stop()
            except Exception as e:
                await self.stop()
                raise e
            else:
                break

        self.is_connected.set()

        log.info("Session started")

    async def stop(self):
        self._closed = True
        self.is_connected.clear()

        self.ping_task_event.set()

        if self.ping_task is not None:
            await self.ping_task

        self.ping_task_event.clear()

        self.connection.close()

        if self.network_task:
            await self.network_task

        for i in self.results.values():
            i.event.set()

        self.results.clear()
        self.stored_msg_ids.clear()
        self.pending_acks.clear()

        if not self.is_media and callable(self.client.disconnect_handler):
            try:
                await self.client.disconnect_handler(self.client)
            except Exception as e:
                log.error(e, exc_info=True)

        log.info("Session stopped")

    async def restart(self):
        await self.stop()
        await self.start()

    async def handle_packet(self, packet):
        try:
            data = await self.client.loop.run_in_executor(
                self._executor,
                mtproto.unpack,
                BytesIO(packet),
                self.session_id,
                self.auth_key,
                self.auth_key_id,
            )
        except SecurityCheckMismatch:
            return

        messages = data.body.messages if isinstance(data.body, MsgContainer) else [data]

        log.debug("Received:")
        log.debug(data)

        for msg in messages:
            if msg.seq_no % 2 != 0:
                if msg.msg_id in self.pending_acks:
                    continue
                else:
                    self.pending_acks.add(msg.msg_id)

            try:
                if len(self.stored_msg_ids) > Session.STORED_MSG_IDS_MAX_SIZE:
                    del self.stored_msg_ids[: Session.STORED_MSG_IDS_MAX_SIZE // 2]

                if self.stored_msg_ids:
                    if msg.msg_id < self.stored_msg_ids[0]:
                        raise SecurityCheckMismatch(
                            "The msg_id is lower than all the stored values"
                        )

                    if msg.msg_id in self.stored_msg_ids:
                        raise SecurityCheckMismatch(
                            "The msg_id is equal to any of the stored values"
                        )

                    time_diff = (msg.msg_id - MsgId()) / 2**32

                    if time_diff > 30:
                        raise SecurityCheckMismatch(
                            "The msg_id belongs to over 30 seconds in the future. "
                            "Most likely the client time has to be synchronized."
                        )

                    if time_diff < -300:
                        raise SecurityCheckMismatch(
                            "The msg_id belongs to over 300 seconds in the past. "
                            "Most likely the client time has to be synchronized."
                        )
            except SecurityCheckMismatch as e:
                log.info("Discarding packet: %s", e)
                return
            else:
                bisect.insort(self.stored_msg_ids, msg.msg_id)

            if isinstance(
                msg.body, (raw.types.MsgDetailedInfo, raw.types.MsgNewDetailedInfo)
            ):
                self.pending_acks.add(msg.body.answer_msg_id)
                continue

            if isinstance(msg.body, raw.types.NewSessionCreated):
                continue

            msg_id = None

            if isinstance(
                msg.body, (raw.types.BadMsgNotification, raw.types.BadServerSalt)
            ):
                msg_id = msg.body.bad_msg_id
            elif isinstance(msg.body, (FutureSalts, raw.types.RpcResult)):
                msg_id = msg.body.req_msg_id
            elif isinstance(msg.body, raw.types.Pong):
                msg_id = msg.body.msg_id
            else:
                if self.client is not None:
                    self.client.loop.create_task(self.client.handle_updates(msg.body))

            if msg_id in self.results:
                self.results[msg_id].value = getattr(msg.body, "result", msg.body)
                self.results[msg_id].event.set()

        if len(self.pending_acks) >= self.ACKS_THRESHOLD:
            log.debug(f"Send {len(self.pending_acks)} acks")

            try:
                await self.send(
                    raw.types.MsgsAck(msg_ids=list(self.pending_acks)), False
                )
            except (OSError, TimeoutError):
                pass
            else:
                self.pending_acks.clear()

    async def ping_worker(self):
        log.info("PingTask started")

        while True:
            try:
                await asyncio.wait_for(self.ping_task_event.wait(), self.PING_INTERVAL)
            except asyncio.TimeoutError:
                pass
            else:
                break

            try:
                await self.send(
                    raw.functions.PingDelayDisconnect(
                        ping_id=0, disconnect_delay=self.WAIT_TIMEOUT + 10
                    ),
                    False,
                )
            except (OSError, TimeoutError, RPCError):
                pass

        log.info("PingTask stopped")

    async def network_worker(self):
        log.info("NetworkTask started")

        while True:
            packet = await self.connection.recv()

            if packet is None or len(packet) == 4:
                if packet:
                    error_code = Int.read(BytesIO(packet))
                    error_desc = Session.TRANSPORT_ERRORS.get(
                        abs(error_code), "unknown"
                    )
                    log.warning(f'Server sent "{error_code}" ({error_desc})')

                if self.is_connected.is_set():
                    self.client.loop.create_task(self.restart())

                break

            self.client.loop.create_task(self.handle_packet(packet))

        log.info("NetworkTask stopped")

    async def send(
        self, data: TLObject, wait_response: bool = True, timeout: float = WAIT_TIMEOUT
    ):
        message = self.msg_factory(data)
        msg_id = message.msg_id

        if wait_response:
            self.results[msg_id] = Result()

        log.debug("Sent:")
        log.debug(message)

        payload = await self.client.loop.run_in_executor(
            self._executor,
            mtproto.pack,
            message,
            self.salt,
            self.session_id,
            self.auth_key,
            self.auth_key_id,
        )

        try:
            await self.connection.send(payload)
        except OSError as e:
            self.results.pop(msg_id, None)
            self.is_connected.clear()
            raise e

        if wait_response:
            try:
                await asyncio.wait_for(self.results[msg_id].event.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            finally:
                result = self.results.pop(msg_id).value

            if result is None:
                raise TimeoutError
            elif isinstance(result, raw.types.RpcError):
                if isinstance(data, Session.CUR_ALWD_INNR_QRYS):
                    data = data.query

                RPCError.raise_it(result, type(data))
            elif isinstance(result, raw.types.BadMsgNotification):
                raise BadMsgNotification(result.error_code)
            elif isinstance(result, raw.types.BadServerSalt):
                self.salt = result.new_server_salt
                return await self.send(data, wait_response, timeout)
            else:
                return result

    async def invoke(
        self,
        query: TLObject,
        retries: int = MAX_RETRIES,
        timeout: float = WAIT_TIMEOUT,
        sleep_threshold: float = SLEEP_THRESHOLD,
    ):
        sleep_threshold = max(sleep_threshold, self.client.sleep_threshold)

        try:
            await asyncio.wait_for(self.is_connected.wait(), self.WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            pass

        if isinstance(query, Session.CUR_ALWD_INNR_QRYS):
            inner_query = query.query
        else:
            inner_query = query

        query_name = ".".join(inner_query.QUALNAME.split(".")[1:])

        while True:
            try:
                return await self.send(query, timeout=timeout)
            except (FloodWait, FloodPremiumWait) as e:
                amount = e.value

                if amount > sleep_threshold >= 0:
                    raise

                log.warning(
                    f"[{self.client.name}] Waiting for {amount} seconds before continuing "
                    f'(required by "{query_name}")'
                )

                await asyncio.sleep(amount)
            except (
                OSError,
                TimeoutError,
                InternalServerError,
                ServiceUnavailable,
            ) as e:
                if retries == 0 or (
                    isinstance(e, InternalServerError)
                    and getattr(e, "code", 0) == 500
                    and (e.ID or e.NAME)
                    in [
                        "HISTORY_GET_FAILED",
                        "PERSISTENT_TIMESTAMP_OUTDATED",
                    ]
                ):
                    raise e from None
                (log.warning if retries < 2 else log.info)(
                    f'[{Session.MAX_RETRIES - retries + 1}] Retrying "{query_name}" due to {str(e) or repr(e)}'
                )

                await asyncio.sleep(0.5)

                return await self.invoke(query, retries - 1, timeout)
        raise TimeoutError("Exceeded maximum number of retries")
