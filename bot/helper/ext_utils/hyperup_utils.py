import asyncio
import functools
import inspect
import io
import math
import os
from hashlib import md5
from math import ceil
from mimetypes import guess_type
from os import path as ospath
from pathlib import PurePath
from re import search as research

from pyrogram import StopTransmission, raw, utils
from pyrogram.errors import (
    AuthBytesInvalid,
    FilePartMissing,
    FloodPremiumWait,
    FloodWait,
)

from ... import LOGGER
from ...hyper_mtproto.auth import get_auth_key
from ...hyper_mtproto.session import Session as HyperSession
from ..telegram_helper.tg_transfer import MB, HypertgTransfer

KB = 1024
PART_SIZE = 512 * KB


class HypertgUpload(HypertgTransfer):
    def __init__(self, obj):
        super().__init__(obj)
        self._up_file = ""
        self._up_size = 0

    @staticmethod
    def _parse_missing_part(exc):
        val = getattr(exc, "value", None)
        if isinstance(val, int):
            return val
        m = research(r"Part (\d+)", str(exc))
        if m:
            return int(m.group(1))
        LOGGER.warning(f"HypertgUL parse_part fallback=0 exc={exc}")
        return 0

    def _build_media(
        self, input_file, mime_type, media_type, attributes, thumb_file=None
    ):
        if media_type == "photo":
            return raw.types.InputMediaUploadedPhoto(file=input_file)
        if media_type == "video":
            mime_type = "video/mp4"
        elif media_type == "audio":
            if mime_type == "audio/ogg":
                mime_type = "audio/opus"
            else:
                mime_type = "audio/mpeg"
        else:
            mime_type = "application/octet-stream"
        return raw.types.InputMediaUploadedDocument(
            file=input_file,
            thumb=thumb_file,
            mime_type=mime_type,
            attributes=attributes or [],
            force_file=media_type == "document",
        )

    async def _save_file(
        self,
        client,
        dc_id,
        file_path,
        file_id=None,
        file_part=0,
        progress=None,
        progress_args=(),
    ):
        if file_path is None:
            return None

        async def worker(session):
            while True:
                data = await queue.get()
                if data is None:
                    return
                try:
                    await session.invoke(data)
                except Exception as e:
                    LOGGER.error(e)

        part_size = 512 * 1024

        if isinstance(file_path, (str, PurePath)):
            fp = open(file_path, "rb")
        elif isinstance(file_path, io.IOBase):
            fp = file_path
        else:
            raise ValueError(
                "Invalid file. Expected a file path as string or a binary (not text) file pointer"
            )

        file_name = getattr(fp, "name", "file.jpg")
        fp.seek(0, os.SEEK_END)
        file_size = fp.tell()
        fp.seek(0)

        if file_size == 0:
            raise ValueError("File size equals to 0 B")

        file_total_parts = int(math.ceil(file_size / part_size))
        is_big = file_size > 10 * 1024 * 1024
        pool_size = 3 if is_big else 1
        workers_count = 4 if is_big else 1
        is_missing_part = file_id is not None
        file_id = file_id or client.rnd_id()
        md5_sum = md5() if not is_big and not is_missing_part else None

        ak, is_cross = await get_auth_key(client, dc_id)
        test_mode = await client.storage.test_mode()
        pool = [
            HyperSession(client, dc_id, ak, test_mode, is_media=True)
            for _ in range(pool_size)
        ]
        workers = [
            asyncio.create_task(worker(s)) for s in pool for _ in range(workers_count)
        ]
        queue = asyncio.Queue(16)

        try:
            for s in pool:
                await s.start(mode=3)

            if is_cross:
                for s in pool:
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
                        for ss in pool:
                            await ss.stop()
                        raise RuntimeError(f"Auth export/import failed for DC {dc_id}")

            fp.seek(part_size * file_part)

            while True:
                if self._listener.is_cancelled:
                    raise StopTransmission()
                chunk = fp.read(part_size)

                if not chunk:
                    if not is_big and not is_missing_part:
                        md5_sum = "".join(
                            [hex(i)[2:].zfill(2) for i in md5_sum.digest()]
                        )
                    break

                if is_big:
                    rpc = raw.functions.upload.SaveBigFilePart(
                        file_id=file_id,
                        file_part=file_part,
                        file_total_parts=file_total_parts,
                        bytes=chunk,
                    )
                else:
                    rpc = raw.functions.upload.SaveFilePart(
                        file_id=file_id, file_part=file_part, bytes=chunk
                    )

                await queue.put(rpc)

                if is_missing_part:
                    return

                if not is_big and not is_missing_part:
                    md5_sum.update(chunk)

                file_part += 1

                if progress:
                    func = functools.partial(
                        progress,
                        min(file_part * part_size, file_size),
                        file_size,
                        *progress_args,
                    )

                    if inspect.iscoroutinefunction(progress):
                        await func()
                    else:
                        await asyncio.get_event_loop().run_in_executor(None, func)
        except StopTransmission:
            raise
        except Exception as e:
            LOGGER.error(e, exc_info=True)
        else:
            if is_big:
                return raw.types.InputFileBig(
                    id=file_id,
                    parts=file_total_parts,
                    name=file_name,
                )
            else:
                return raw.types.InputFile(
                    id=file_id,
                    parts=file_total_parts,
                    name=file_name,
                    md5_checksum=md5_sum,
                )
        finally:
            for _ in workers:
                await queue.put(None)

            await asyncio.gather(*workers)

            for s in pool:
                await s.stop()
            if isinstance(file_path, (str, PurePath)):
                fp.close()

    async def _upload_file(self, client, file_path):
        import time as _time

        _t0 = _time.monotonic()
        file_size = ospath.getsize(file_path)
        file_total_parts = ceil(file_size / PART_SIZE)
        dc_id = await client.storage.dc_id()
        file_name = ospath.basename(file_path)
        self._obj._processed_bytes = 0

        def _progress(current, total):
            self._obj._processed_bytes = current
            part = current // PART_SIZE
            if part > 0 and part % max(1, file_total_parts // 10) == 0:
                elapsed = _time.monotonic() - _t0
                mb_done = current / MB
                mb_total = file_size / MB
                LOGGER.info(
                    f"HypertgUL {file_name} "
                    f"part={part}/{file_total_parts} "
                    f"MB={mb_done:.0f}/{mb_total:.0f} "
                    f"speed={mb_done / elapsed:.1f}MB/s"
                )

        try:
            result = await self._save_file(client, dc_id, file_path, progress=_progress)
            self._obj._processed_bytes = file_size
            elapsed = _time.monotonic() - _t0
            LOGGER.info(
                f"HypertgUL done {file_name} "
                f"elapsed={elapsed:.1f}s "
                f"speed={file_size / MB / elapsed:.1f}MB/s"
            )
            return result
        except StopTransmission:
            LOGGER.warning(f"HypertgUL upload cancelled {file_name}")
            raise
        except Exception as e:
            LOGGER.error(f"HypertgUL upload fail: {type(e).__name__}: {e}")
            raise

    async def _upload_thumb(self, client, file_path):
        dc_id = await client.storage.dc_id()
        return await self._save_file(client, dc_id, file_path)

    async def _reupload_part(self, client, file_path, input_file, part_num):
        offset = part_num * PART_SIZE
        fp = open(file_path, "rb")
        try:
            fp.seek(offset)
            chunk = fp.read(PART_SIZE)
            if not chunk:
                LOGGER.warning(f"HypertgUL reupload part={part_num} empty")
                return
            dc_id = await client.storage.dc_id()
            session = await self._mk_session(client, dc_id, mode=3)
            if isinstance(input_file, raw.types.InputFileBig):
                rpc = raw.functions.upload.SaveBigFilePart(
                    file_id=input_file.id,
                    file_part=part_num,
                    file_total_parts=input_file.parts,
                    bytes=chunk,
                )
            else:
                rpc = raw.functions.upload.SaveFilePart(
                    file_id=input_file.id,
                    file_part=part_num,
                    bytes=chunk,
                )
            await session.invoke(rpc)
        except Exception as e:
            LOGGER.error(
                f"HypertgUL reupload part={part_num} fail: {type(e).__name__}: {e}"
            )
        finally:
            fp.close()

    async def upload(
        self,
        target_client,
        target_chat_id,
        file_path,
        dump_chat_id,
        media_type,
        attributes,
        thumb_path=None,
        caption="",
        reply_to_message_id=None,
    ):
        self._cancel.clear()
        self._obj._processed_bytes = 0
        self._up_file = ospath.basename(file_path)
        self._up_size = ospath.getsize(file_path)

        input_file = await self._upload_file(target_client, file_path)

        thumb_file = None
        if thumb_path and ospath.exists(thumb_path) and ospath.getsize(thumb_path) > 0:
            thumb_file = await self._upload_thumb(target_client, thumb_path)

        mime_type = self._mime(file_path)
        input_media = self._build_media(
            input_file, mime_type, media_type, attributes, thumb_file
        )

        peer = await target_client.resolve_peer(target_chat_id)

        parsed = await utils.parse_text_entities(
            target_client, caption or "", None, None
        )

        rpc = raw.functions.messages.SendMedia(
            peer=peer,
            media=input_media,
            random_id=target_client.rnd_id(),
            reply_to=raw.types.InputReplyToMessage(reply_to_msg_id=reply_to_message_id)
            if reply_to_message_id
            else None,
            **parsed,
            silent=True,
        )

        r_updates = None
        send_retries = 0
        missing_fixed = 0
        while True:
            try:
                r_updates = await target_client.invoke(rpc)
                break
            except FilePartMissing as e:
                part = self._parse_missing_part(e)
                missing_fixed += 1
                LOGGER.warning(
                    f"HypertgUL SendMedia missing part {part} "
                    f"(fixed {missing_fixed}) {self._up_file}"
                )
                await self._reupload_part(target_client, file_path, input_file, part)
                send_retries += 1
                if send_retries >= 100:
                    raise RuntimeError(
                        f"SendMedia exhausted after fixing {missing_fixed} missing parts"
                    )
            except (FloodWait, FloodPremiumWait) as e:
                val = e.value if hasattr(e, "value") else 5
                send_retries += 1
                LOGGER.warning(
                    f"HypertgUL SendMedia flood {val}s "
                    f"(retry {send_retries}) {self._up_file}"
                )
                if send_retries >= 10:
                    raise
                await asyncio.sleep(val + 1)
            except Exception as e:
                send_retries += 1
                LOGGER.error(
                    f"HypertgUL SendMedia {type(e).__name__}: {e} "
                    f"(retry {send_retries}) {self._up_file}"
                )
                if send_retries >= 10:
                    raise
        msg_id = None
        for u in r_updates.updates:
            if isinstance(
                u,
                (
                    raw.types.UpdateNewMessage,
                    raw.types.UpdateNewChannelMessage,
                    raw.types.UpdateNewScheduledMessage,
                ),
            ):
                msg_id = u.message.id
                break
        if msg_id is None:
            LOGGER.error("HypertgUL no UpdateNewMessage in response")
            raise ValueError("No UpdateNewMessage in SendMedia response")

        msg = await target_client.get_messages(
            chat_id=target_chat_id, message_ids=msg_id
        )
        return msg

    async def cancel(self):
        await super().cancel()

    @staticmethod
    def _mime(path):
        m, _ = guess_type(path)
        return m or "application/octet-stream"
