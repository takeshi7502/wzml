from asyncio import Lock, sleep
from io import BufferedReader
from json import JSONDecodeError
from logging import getLogger
from mimetypes import guess_type
from os import makedirs
from os import path as ospath
from os import walk as oswalk
from pathlib import Path
from shutil import rmtree
from time import time
from uuid import uuid4
from urllib.parse import quote

from aiofiles.os import path as aiopath
from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ContentTypeError
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bot import user_data
from bot.core.config_manager import Config
from bot.core.tg_client import TgClient
from bot.helper.ext_utils.bot_utils import SetInterval, get_size_bytes, sync_to_async

LOGGER = getLogger(__name__)
TELDRIVE_UPLOAD_LOADS = {"main": 0}
TELDRIVE_UPLOAD_CURSOR = 0
TELDRIVE_UPLOAD_LOCK = Lock()


class ProgressFileReader(BufferedReader):
    def __init__(self, filename, read_callback=None, cancel_callback=None):
        super().__init__(open(filename, "rb"))
        self.__read_callback = read_callback
        self.__cancel_callback = cancel_callback
        self.length = Path(filename).stat().st_size

    def read(self, size=None):
        if self.__cancel_callback and self.__cancel_callback():
            raise Exception("Teldrive upload has been cancelled!")
        size = size or (self.length - self.tell())
        chunk = super().read(size)
        if self.__read_callback:
            self.__read_callback(self.tell())
        return chunk


class TeldriveUpload:
    def __init__(self, listener, path):
        self.listener = listener
        self._path = path
        self._updater = None
        self._is_errored = False
        self.__processed_bytes = 0
        self.last_uploaded = 0
        self.total_time = 0
        self.total_files = 0
        self.total_folders = 0
        self.update_interval = 3
        self.is_uploading = True
        self.is_server_processing = False
        self.current_file_size = 0
        self.current_file_name = ""
        self.results = []

        user_dict = user_data.get(self.listener.user_id, {})
        self.api_url = (
            user_dict.get("TELDRIVE_API_URL")
            or Config.TELDRIVE_API_URL
            or ""
        ).rstrip("/")
        if self.api_url.endswith("/api"):
            self.api_url = self.api_url[:-4]
        self.api_key = user_dict.get("TELDRIVE_API_KEY") or Config.TELDRIVE_API_KEY
        self.base_path = user_dict.get("TELDRIVE_PATH") or Config.TELDRIVE_PATH or "/"
        self.share = user_dict.get("TELDRIVE_SHARE", Config.TELDRIVE_SHARE)
        self.channel_id = user_dict.get("TELDRIVE_CHANNEL_ID") or Config.TELDRIVE_CHANNEL_ID
        self.split_size = self._parse_split_size(
            user_dict.get("TELDRIVE_SPLIT_SIZE") or Config.TELDRIVE_SPLIT_SIZE or "500mb"
        )
        self.api_timeout = ClientTimeout(total=120, connect=30, sock_connect=30, sock_read=90)
        self.upload_timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=300)
        self._existing_files_cache = {}
        self._api_session = None
        self._metadata_posts = 0

    @property
    def speed(self):
        try:
            return self.__processed_bytes / self.total_time
        except Exception:
            return 0

    @property
    def processed_bytes(self):
        return self.__processed_bytes

    def __progress_callback(self, current):
        self.__processed_bytes += max(0, current - self.last_uploaded)
        self.last_uploaded = current
        if current >= self.current_file_size:
            self.is_server_processing = True

    async def progress(self):
        self.total_time += self.update_interval

    def status_message(self):
        if self.is_server_processing:
            return "Teldrive is uploading. Pls wait..."
        return ""

    def _api(self, route):
        return f"{self.api_url}/api/{route.lstrip('/')}"

    def _headers(self):
        token = self.api_key.strip()
        if token.startswith("access_token="):
            return {"Cookie": token}
        return {"Cookie": f"access_token={token}"}

    async def _get_session(self):
        if self._api_session is None or self._api_session.closed:
            self._api_session = ClientSession(
                headers=self._headers(), timeout=self.api_timeout
            )
        return self._api_session

    async def _close_session(self):
        if self._api_session and not self._api_session.closed:
            await self._api_session.close()
            self._api_session = None

    def _join_cloud_path(self, *parts):
        clean_parts = [str(part).strip("/") for part in parts if str(part).strip("/")]
        return "/" + "/".join(clean_parts) if clean_parts else "/"

    def _parse_split_size(self, value):
        try:
            size = get_size_bytes(str(value)) if not isinstance(value, int) else value
        except Exception:
            size = 500 * 1024 * 1024
        # Bot API hard limit is 2000 MiB; keep a small margin for safety.
        return max(1, min(int(size), 1990 * 1024 * 1024))

    def _storage_chat_id(self):
        channel_id = str(self.channel_id or "").strip()
        if not channel_id:
            raise ValueError(
                "Bạn chưa set Teldrive Channel ID. Hãy vào /usetting → Uphoster → Teldrive Settings "
                "và điền Channel ID của storage channel, sau đó thêm bot vào channel với quyền gửi file."
            )
        if channel_id.startswith("-") or channel_id.startswith("@"):
            return int(channel_id) if channel_id.lstrip("-").isdigit() else channel_id
        return int(f"-100{channel_id}") if channel_id.isdigit() else channel_id

    def _share_link(self, share_id):
        return f"{self.api_url}/share/{share_id}" if share_id else ""

    def _direct_link(self, file_obj, share_id=""):
        file_id = file_obj.get("id", "") if file_obj else ""
        file_name = file_obj.get("name", "") if file_obj else ""
        if not file_id:
            return ""
        if share_id:
            return (
                f"{self.api_url}/api/shares/{quote(share_id)}/files/"
                f"{quote(file_id)}/{quote(file_name)}"
            )
        return f"{self.api_url}/api/files/{quote(file_id)}/{quote(file_name)}"

    def _human_error(self, error):
        raw = str(error)
        upper = raw.upper()
        if "CHANNEL_INVALID" in upper or "CHANNEL_PRIVATE" in upper or "PEER_ID_INVALID" in upper:
            return (
                "Không truy cập được Teldrive storage channel. "
                f"Hãy kiểm tra Channel ID ({self.channel_id or 'chưa set'}) trong Teldrive Settings "
                "và thêm bot vào channel đó với quyền gửi file. Nếu là channel private, bot bắt buộc phải là member/admin."
            )
        if "CHAT_ADMIN_REQUIRED" in upper or "USER_BANNED_IN_CHANNEL" in upper or "FORBIDDEN" in upper:
            return (
                "Bot chưa đủ quyền trong Teldrive storage channel. "
                "Hãy cấp quyền admin hoặc ít nhất quyền gửi document/file cho bot."
            )
        if "FILE_PARTS_INVALID" in upper or "FILE_PART_INVALID" in upper:
            return "Telegram từ chối file part. Hãy thử giảm Teldrive Split Size xuống 500MB hoặc 100MB rồi upload lại."
        if "BIGGER THAN 2000 MIB" in upper or "FILE_TOO_BIG" in upper:
            return "File/part vượt giới hạn Telegram Bot API 2000MiB. Hãy giảm Teldrive Split Size xuống 1GB, 500MB hoặc 100MB."
        if "FLOOD" in upper:
            return "Telegram đang giới hạn tốc độ upload (FloodWait). Hãy thử lại sau hoặc thêm helper bot để chia tải."
        if "CONNECTION TIMEOUT" in upper or "TIMEOUT" in upper:
            return "Kết nối tới Teldrive API bị timeout. Có thể server Teldrive đang chậm/quá tải, hãy thử lại sau."
        if "TOKEN IS MALFORMED" in upper or "UNAUTHORIZED" in upper or "HTTP 401" in upper:
            return "TELDRIVE_API_KEY/access token không hợp lệ hoặc đã hết hạn. Hãy cập nhật lại API key trong /usetting."
        if "RECORD NOT FOUND" in upper or "HTTP 404" in upper:
            return (
                "Teldrive API báo không tìm thấy record. Thường do channel/path metadata chưa khớp, "
                "storage channel trên web khác TELDRIVE_CHANNEL_ID, hoặc folder đích chưa đồng bộ. "
                "Hãy kiểm tra lại Channel ID và thử upload lại."
            )
        if "HTML PAGE INSTEAD OF JSON" in upper:
            return "TELDRIVE_API_URL không đúng endpoint Teldrive API hoặc bị redirect sang trang web HTML. Hãy kiểm tra lại URL."
        return raw

    async def _parse_response(self, resp, allow_empty=False):
        if allow_empty and resp.status == 204:
            return {}
        try:
            data = await resp.json()
        except (ContentTypeError, JSONDecodeError):
            text = (await resp.text()).strip()
            content_type = resp.headers.get("Content-Type", "unknown")
            if allow_empty and not text and 200 <= resp.status < 300:
                return {}
            if "<html" in text[:500].lower() or "<!doctype html" in text[:500].lower():
                text = "Teldrive returned an HTML page instead of JSON. Check TELDRIVE_API_URL."
            elif len(text) > 500:
                text = f"{text[:500]}... [truncated]"
            data = {"error": f"Non-JSON response ({content_type}): {text}"}
        if resp.status < 200 or resp.status >= 300:
            raise Exception(data.get("error") or f"HTTP {resp.status}: {data}")
        if isinstance(data, dict) and data.get("error"):
            raise Exception(data["error"])
        return data

    async def _mkdir(self, cloud_path):
        session = await self._get_session()
        try:
            async with session.post(
                self._api("files/mkdir"),
                json={"path": cloud_path},
            ) as resp:
                return await self._parse_response(resp, allow_empty=True)
        except Exception as err:
            if "exist" not in str(err).lower() and "duplicate" not in str(err).lower():
                raise
            parent = self._join_cloud_path(*cloud_path.strip("/").split("/")[:-1])
            name = cloud_path.rstrip("/").split("/")[-1]
            return await self._find_file(name, "folder", cloud_path=parent)

    async def _find_file(self, name, file_type, cloud_path=None, parent_id=None):
        params = {
            "name": name,
            "type": file_type,
            "operation": "find",
            "limit": 100,
        }
        if parent_id:
            params["parentId"] = parent_id
        else:
            params["path"] = cloud_path or "/"
        last_error = None
        max_attempts = 6
        for attempt in range(1, max_attempts + 1):
            try:
                session = await self._get_session()
                async with session.get(self._api("files"), params=params) as resp:
                    data = await self._parse_response(resp)
                items = data.get("items") or []
                for item in items:
                    if item.get("name") == name and item.get("type") == file_type:
                        return item
                return items[0] if items else None
            except Exception as err:
                last_error = err
                if self.listener.is_cancelled:
                    raise
                LOGGER.warning(
                    f"Teldrive find retry {attempt}/{max_attempts} for {name}: {err}"
                )
                if attempt < max_attempts:
                    await sleep(min(45, 5 * attempt))
        raise last_error

    async def _list_existing_files_by_parent(self, parent_id):
        if not parent_id:
            return {}
        if parent_id in self._existing_files_cache:
            return self._existing_files_cache[parent_id]

        LOGGER.info(f"Teldrive listing remote files once for parentId: {parent_id}")
        existing = {}
        cursor = ""
        while True:
            params = {
                "operation": "list",
                "parentId": parent_id,
                "limit": 100,
                "sort": "id",
                "order": "asc",
            }
            if cursor:
                params["cursor"] = cursor
            last_error = None
            max_attempts = 6
            for attempt in range(1, max_attempts + 1):
                try:
                    session = await self._get_session()
                    async with session.get(self._api("files"), params=params) as resp:
                        data = await self._parse_response(resp)
                    break
                except Exception as err:
                    last_error = err
                    if self.listener.is_cancelled:
                        raise
                    LOGGER.warning(
                        f"Teldrive list retry {attempt}/{max_attempts} for parentId {parent_id}: {err}"
                    )
                    if attempt < max_attempts:
                        await sleep(min(45, 5 * attempt))
            else:
                raise last_error
            for item in data.get("items") or []:
                if item.get("type") == "file" and item.get("name"):
                    existing[item["name"]] = item
            cursor = (data.get("meta") or {}).get("nextCursor") or ""
            if not cursor:
                break

        self._existing_files_cache[parent_id] = existing
        LOGGER.info(
            f"Teldrive cached {len(existing)} remote files for parentId: {parent_id}"
        )
        return existing

    async def _get_existing_file_from_parent_cache(self, file_path, parent_id):
        if not parent_id:
            return None
        existing = await self._list_existing_files_by_parent(parent_id)
        remote = existing.get(ospath.basename(file_path))
        if not remote:
            return None
        local_size = Path(file_path).stat().st_size
        remote_size = remote.get("size")
        if remote_size is not None and int(remote_size) != local_size:
            return None
        return remote

    async def _metadata_pause(self):
        self._metadata_posts += 1
        if self._metadata_posts % 15 == 0:
            await sleep(2.0)
        elif self._metadata_posts > 3:
            await sleep(0.5)

    async def _post_file_metadata(self, payload, file_path, cloud_path, parent_id=None):
        last_error = None
        max_attempts = 8
        for attempt in range(1, max_attempts + 1):
            try:
                await self._metadata_pause()
                session = await self._get_session()
                async with session.post(self._api("files"), json=payload) as resp:
                    return await self._parse_response(resp)
            except Exception as err:
                last_error = err
                if self.listener.is_cancelled:
                    raise
                LOGGER.warning(
                    f"Teldrive metadata POST retry {attempt}/{max_attempts} for {payload.get('name')}: {err}"
                )
                # After timeout/error, check if backend already created the record.
                existing = None
                try:
                    if parent_id:
                        self._existing_files_cache.pop(parent_id, None)
                        existing = await self._get_existing_file_from_parent_cache(file_path, parent_id)
                    else:
                        existing = await self._find_file(
                            payload.get("name"),
                            "file",
                            cloud_path=cloud_path,
                        )
                except Exception as check_err:
                    LOGGER.warning(
                        f"Teldrive metadata existing-check failed for {payload.get('name')}: {check_err}"
                    )
                if existing:
                    LOGGER.info(f"Teldrive metadata already created after timeout: {payload.get('name')}")
                    return existing
                if attempt < max_attempts:
                    wait_secs = min(60, 5 * attempt)
                    LOGGER.info(f"Teldrive waiting {wait_secs}s before metadata retry...")
                    await sleep(wait_secs)
        raise last_error

    async def _post_folder_metadata(self, payload, folder_name, parent_id=None, parent_path="/"):
        last_error = None
        for attempt in range(1, 5):
            try:
                if self._metadata_posts > 3:
                    await sleep(0.5)
                self._metadata_posts += 1
                session = await self._get_session()
                async with session.post(self._api("files"), json=payload) as resp:
                    return await self._parse_response(resp)
            except Exception as err:
                last_error = err
                if self.listener.is_cancelled:
                    raise
                LOGGER.warning(
                    f"Teldrive folder POST retry {attempt}/4 for {folder_name}: {err}"
                )
                # Check if folder was already created
                try:
                    existing = await self._find_file(
                        folder_name, "folder",
                        parent_id=parent_id, cloud_path=parent_path
                    )
                    if existing:
                        LOGGER.info(f"Teldrive folder already created after timeout: {folder_name}")
                        return existing
                except Exception:
                    pass
                if attempt < 4:
                    wait_secs = min(30, 5 * attempt)
                    LOGGER.info(f"Teldrive waiting {wait_secs}s before folder retry...")
                    await sleep(wait_secs)
        raise last_error

    def _remember_existing_file(self, file_data, parent_id):
        if not parent_id or not file_data or not file_data.get("name"):
            return
        if parent_id in self._existing_files_cache:
            self._existing_files_cache[parent_id][file_data["name"]] = file_data

    async def _delete_file(self, file_id):
        if not file_id:
            return
        session = await self._get_session()
        async with session.post(
            self._api("files/delete"),
            json={"ids": [file_id]},
        ) as resp:
            await self._parse_response(resp, allow_empty=True)

    async def _create_folder(self, folder_name, parent_id=None, parent_path="/"):
        payload = {
            "name": folder_name,
            "type": "folder",
        }
        if parent_id:
            payload["parentId"] = parent_id
        else:
            payload["path"] = parent_path or "/"
        return await self._post_folder_metadata(
            payload, folder_name, parent_id=parent_id, parent_path=parent_path
        )

    async def _create_or_get_folder(self, folder_path):
        parent_path = self._join_cloud_path(*folder_path.strip("/").split("/")[:-1])
        folder_name = folder_path.rstrip("/").split("/")[-1]
        LOGGER.info(f"Teldrive mkdir: {folder_path}")
        try:
            return await self._create_folder(folder_name, parent_path=parent_path)
        except Exception as err:
            if "exist" not in str(err).lower() and "duplicate" not in str(err).lower():
                raise
            folder = await self._find_file(folder_name, "folder", cloud_path=parent_path)
            if folder:
                return folder
            raise

    async def _ensure_folder_path(self, folder_path):
        folder_path = self._join_cloud_path(folder_path)
        if folder_path == "/":
            return None
        current = "/"
        folder = None
        parent_id = None
        for part in folder_path.strip("/").split("/"):
            current = self._join_cloud_path(current, part)
            parent_path = self._join_cloud_path(*current.strip("/").split("/")[:-1])
            folder = await self._create_folder(part, parent_id=parent_id, parent_path=parent_path)
            parent_id = folder.get("id")
        return folder

    @retry(
        wait=wait_exponential(multiplier=2, min=4, max=8),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    async def _upload_part(self, upload_id, file_path):
        if self.listener.is_cancelled:
            return None

        file_name = ospath.basename(file_path)
        part_name = file_name

        session = await self._get_session()
        async with session.get(self._api("uploads/fileId")) as resp:
            await self._parse_response(resp, allow_empty=True)

        params = {
            "partName": part_name,
            "fileName": file_name,
            "partNo": 1,
        }
        if self.channel_id:
            params["channelId"] = self.channel_id

        self.last_uploaded = 0
        self.current_file_size = Path(file_path).stat().st_size
        self.current_file_name = file_name
        self.is_server_processing = False

        headers = self._headers()
        headers["Content-Length"] = str(self.current_file_size)
        headers["Content-Type"] = guess_type(file_path)[0] or "application/octet-stream"

        with ProgressFileReader(
            file_path,
            self.__progress_callback,
            lambda: self.listener.is_cancelled,
        ) as file:
            async with ClientSession(timeout=self.upload_timeout) as session:
                async with session.post(
                    self._api(f"uploads/{upload_id}"),
                    params=params,
                    headers=headers,
                    data=file,
                ) as resp:
                    data = await self._parse_response(resp)
                    self.is_server_processing = False
                    return data

    async def _get_upload_parts(self, upload_id):
        session = await self._get_session()
        async with session.get(self._api(f"uploads/{upload_id}")) as resp:
            return await self._parse_response(resp)

    async def _create_file(self, upload_id, file_path, cloud_path, upload_part):
        remote_parts = await self._get_upload_parts(upload_id)
        if not isinstance(remote_parts, list):
            remote_parts = [upload_part]
        parts = [
            {
                "id": part.get("partId") or part.get("id") or part.get("name"),
                "salt": part.get("salt", ""),
            }
            for part in remote_parts
        ]
        if not parts or not parts[0].get("id"):
            raise Exception(f"Teldrive upload did not return part metadata: {remote_parts}")

        payload = {
            "uploadId": upload_id,
            "name": ospath.basename(file_path),
            "type": "file",
            "path": cloud_path,
            "mimeType": guess_type(file_path)[0] or "application/octet-stream",
            "size": Path(file_path).stat().st_size,
            "encrypted": bool(upload_part.get("encrypted", False)),
            "parts": parts,
        }

        if upload_part.get("hash"):
            payload["hash"] = upload_part["hash"]

        return await self._post_file_metadata(payload, file_path, cloud_path)

    async def _create_file_from_messages(self, file_path, cloud_path, message_ids, parent_id=None):
        payload = {
            "uploadId": str(uuid4()),
            "name": ospath.basename(file_path),
            "type": "file",
            "mimeType": guess_type(file_path)[0] or "application/octet-stream",
            "size": Path(file_path).stat().st_size,
            "encrypted": False,
            "parts": [{"id": message_id, "salt": ""} for message_id in message_ids],
        }
        if parent_id:
            payload["parentId"] = parent_id
        else:
            payload["path"] = cloud_path
        return await self._post_file_metadata(payload, file_path, cloud_path, parent_id)

    @staticmethod
    async def _pick_upload_clients():
        global TELDRIVE_UPLOAD_CURSOR
        async with TELDRIVE_UPLOAD_LOCK:
            clients = []
            for no, client in TgClient.helper_bots.items():
                clients.append((no, client))
                TELDRIVE_UPLOAD_LOADS.setdefault(no, 0)
            if TgClient.bot:
                clients.append(("main", TgClient.bot))
                TELDRIVE_UPLOAD_LOADS.setdefault("main", 0)
            if not clients:
                raise Exception("No Telegram client available for Teldrive upload")

            clients.sort(key=lambda item: str(item[0]))
            least_load = min(TELDRIVE_UPLOAD_LOADS.get(no, 0) for no, _ in clients)
            preferred = [
                item for item in clients if TELDRIVE_UPLOAD_LOADS.get(item[0], 0) == least_load
            ]
            start = TELDRIVE_UPLOAD_CURSOR % len(preferred)
            ordered = preferred[start:] + preferred[:start]
            TELDRIVE_UPLOAD_CURSOR = (TELDRIVE_UPLOAD_CURSOR + 1) % len(preferred)
            ordered_keys = {no for no, _ in ordered}
            ordered.extend(item for item in clients if item[0] not in ordered_keys)
            return ordered

    @staticmethod
    def _client_name(client_no, client):
        if client_no == "main":
            return f"main @{getattr(client.me, 'username', 'bot')}"
        return f"helper-{client_no} @{getattr(client.me, 'username', 'bot')}"

    async def _recover_uploaded_message(self, client, chat_id, file_path, display_name):
        file_size = Path(file_path).stat().st_size
        try:
            async for message in client.get_chat_history(chat_id, limit=30):
                document = getattr(message, "document", None)
                if not document:
                    continue
                doc_name = getattr(document, "file_name", "") or ""
                doc_size = getattr(document, "file_size", 0) or 0
                if doc_name == display_name and int(doc_size) == int(file_size):
                    LOGGER.info(f"Teldrive recovered uploaded Telegram message: {display_name} -> {message.id}")
                    self.is_server_processing = True
                    return message
        except Exception as err:
            LOGGER.warning(f"Teldrive Telegram upload recovery failed for {display_name}: {err}")
        return None

    async def _telegram_upload_file(self, file_path, display_name=None):
        if self.listener.is_cancelled:
            return None
        self.last_uploaded = 0
        self.current_file_size = Path(file_path).stat().st_size
        self.current_file_name = display_name or ospath.basename(file_path)
        self.is_server_processing = False
        chat_id = self._storage_chat_id()
        # Keep routine per-file upload logs quiet; warnings/errors still log below.
        clients = await self._pick_upload_clients()
        last_error = None
        for client_no, client in clients:
            client_name = self._client_name(client_no, client)
            for attempt in range(3):
                try:
                    TELDRIVE_UPLOAD_LOADS[client_no] = TELDRIVE_UPLOAD_LOADS.get(client_no, 0) + 1
                    # Per-file attempt logging is intentionally suppressed to keep folder logs readable.
                    msg = await client.send_document(
                        chat_id=chat_id,
                        document=file_path,
                        file_name=self.current_file_name,
                        disable_notification=True,
                        progress=lambda current, total: self.__progress_callback(current),
                    )
                    self.is_server_processing = True
                    # Per-file success logging is intentionally suppressed to keep folder logs readable.
                    return msg
                except Exception as e:
                    err_str = str(e)
                    last_error = err_str
                    if self.listener.is_cancelled:
                        return None
                    upper_err = err_str.upper()
                    if "FLOOD" in upper_err:
                        import re
                        wait_match = re.search(r"(\d+)\s*seconds?", err_str)
                        wait_time = int(wait_match.group(1)) if wait_match else 30
                        LOGGER.warning(
                            f"Teldrive FloodWait {wait_time}s on {client_name} for {self.current_file_name}"
                        )
                        await sleep(wait_time + 2)
                    elif any(
                        key in upper_err
                        for key in (
                            "CHAT_WRITE_FORBIDDEN",
                            "PEER_ID_INVALID",
                            "USER_BANNED",
                            "CHANNEL_PRIVATE",
                            "BOT_METHOD_INVALID",
                        )
                    ):
                        LOGGER.warning(
                            f"Teldrive upload client {client_name} cannot write to storage chat: {err_str}"
                        )
                        break
                    else:
                        recovered = await self._recover_uploaded_message(
                            client, chat_id, file_path, self.current_file_name
                        )
                        if recovered:
                            return recovered
                        if attempt < 2:
                            LOGGER.warning(
                                f"Teldrive send_document retry {attempt + 1}/3 via {client_name}: {err_str}"
                            )
                            await sleep(5 * (attempt + 1))
                        else:
                            LOGGER.warning(
                                f"Teldrive upload failed via {client_name}, trying next client: {err_str}"
                            )
                    self.last_uploaded = 0
                finally:
                    TELDRIVE_UPLOAD_LOADS[client_no] = max(
                        0, TELDRIVE_UPLOAD_LOADS.get(client_no, 1) - 1
                    )
        raise Exception(
            self._human_error(
                f"Teldrive Telegram upload failed for {self.current_file_name}: {last_error or 'No upload client succeeded'}"
            )
        )

    def _split_file_sync(self, file_path, split_dir):
        makedirs(split_dir, exist_ok=True)
        part_paths = []
        base_name = ospath.basename(file_path)
        with open(file_path, "rb") as src:
            part_no = 1
            while True:
                chunk = src.read(self.split_size)
                if not chunk:
                    break
                part_path = ospath.join(split_dir, f"{base_name}.part{part_no:03d}")
                with open(part_path, "wb") as dst:
                    dst.write(chunk)
                part_paths.append(part_path)
                part_no += 1
        return part_paths

    async def _telegram_upload_parts(self, file_path):
        file_size = Path(file_path).stat().st_size
        if file_size <= self.split_size:
            msg = await self._telegram_upload_file(file_path)
            return [msg.id] if msg else []

        split_dir = ospath.join(ospath.dirname(file_path), f".teldrive_parts_{uuid4().hex}")
        LOGGER.info(
            f"Teldrive splitting large file into {self.split_size} byte parts for Telegram storage"
        )
        try:
            part_paths = await sync_to_async(self._split_file_sync, file_path, split_dir)
            message_ids = []
            total_parts = len(part_paths)
            for index, part_path in enumerate(part_paths, start=1):
                if self.listener.is_cancelled:
                    return []
                display_name = f"{ospath.basename(file_path)}.part{index:03d}"
                # Per-part logging is intentionally suppressed to keep folder logs readable.
                msg = await self._telegram_upload_file(part_path, display_name)
                if not msg:
                    return []
                message_ids.append(msg.id)
            return message_ids
        finally:
            await sync_to_async(rmtree, split_dir, True)

    async def _share_file_data(self, file_id):
        if not self.share or not file_id:
            return {}
        last_error = None
        max_attempts = 8
        for attempt in range(1, max_attempts + 1):
            try:
                session = await self._get_session()
                async with session.post(
                    self._api(f"files/{file_id}/share"),
                    json={"expiresAt": "2099-12-31T23:59:59.000Z"},
                ) as resp:
                    await self._parse_response(resp, allow_empty=True)
                async with session.get(self._api(f"files/{file_id}/share")) as resp:
                    data = await self._parse_response(resp)
                return data or {}
            except Exception as err:
                last_error = err
                if self.listener.is_cancelled:
                    return {}
                LOGGER.warning(
                    f"Teldrive share retry {attempt}/{max_attempts} for file {file_id}: {err}"
                )
                if attempt < max_attempts:
                    await sleep(min(60, 5 * attempt))
        LOGGER.warning(
            f"Teldrive share failed after retries for {file_id}, continuing without share link: {last_error}"
        )
        return {}

    async def _share_file(self, file_id):
        data = await self._share_file_data(file_id)
        return self._share_link(data.get("id"))

    async def upload_file(self, file_path, cloud_path, create_share=True, ensure_path=True, parent_id=None):
        file_name = ospath.basename(file_path)
        target_path = self._join_cloud_path(cloud_path)
        existing = None
        try:
            if parent_id:
                existing = await self._get_existing_file_from_parent_cache(file_path, parent_id)
            else:
                existing = await self._find_file(
                    file_name,
                    "file",
                    cloud_path=target_path,
                )
        except Exception as err:
            if self.listener.is_cancelled:
                raise
            LOGGER.warning(
                f"Teldrive duplicate pre-check failed for {file_name}, continuing upload: {err}"
            )
        if existing:
            # Size mismatch check for files found via _find_file
            local_size = Path(file_path).stat().st_size
            remote_size = existing.get("size")
            if remote_size is not None and int(remote_size) != local_size:
                existing = None

        if existing:
            # Per-file skip logging is intentionally suppressed to keep folder logs readable.
            share_data = await self._share_file_data(existing.get("id"))
            share_id = share_data.get("id", "")
            return {
                "file": existing,
                "share_link": self._share_link(share_id),
                "direct_link": self._direct_link(existing, share_id),
                "skipped": True,
            }

        if ensure_path and target_path != "/" and not parent_id:
            folder = await self._ensure_folder_path(target_path)
            parent_id = folder.get("id") if folder else None

        message_ids = await self._telegram_upload_parts(file_path)
        if not message_ids:
            return None
        created = await self._create_file_from_messages(file_path, target_path, message_ids, parent_id)
        self._remember_existing_file(created, parent_id)
        self.is_server_processing = False
        share_data = await self._share_file_data(created.get("id")) if create_share else {}
        share_id = share_data.get("id", "")
        return {
            "file": created,
            "share_link": self._share_link(share_id),
            "direct_link": self._direct_link(created, share_id),
        }

    async def _upload_folder_file(self, file_path, cloud_path, semaphore):
        async with semaphore:
            if self.listener.is_cancelled:
                return None
            # Per-file folder logging is intentionally suppressed to keep folder logs readable.
            return await self.upload_file(
                file_path, cloud_path, create_share=False, ensure_path=False
            )

    async def _upload_dir(self, input_directory):
        root_name = ospath.basename(input_directory.rstrip(ospath.sep))
        base_cloud_path = self._join_cloud_path(self.base_path)
        if base_cloud_path != "/":
            await self._ensure_folder_path(base_cloud_path)
        root_cloud_path = self._join_cloud_path(base_cloud_path, root_name)
        root_folder = await self._create_or_get_folder(root_cloud_path)

        folder_ids = {root_cloud_path: root_folder.get("id")}
        LOGGER.info(f"Teldrive folder upload started: {root_cloud_path}")

        for root, dirs, files in await sync_to_async(lambda: list(oswalk(input_directory))):
            if self.listener.is_cancelled:
                break
            rel_path = ospath.relpath(root, input_directory)
            rel_cloud = "" if rel_path == "." else rel_path.replace("\\", "/")
            cloud_path = self._join_cloud_path(root_cloud_path, rel_cloud)
            parent_id = folder_ids.get(cloud_path)
            if rel_path != "." and not parent_id:
                parent_path = self._join_cloud_path(*cloud_path.strip("/").split("/")[:-1])
                parent_id = folder_ids.get(parent_path)
                LOGGER.info(f"Teldrive create folder by parentId: {cloud_path}")
                folder = await self._create_folder(
                    cloud_path.rstrip("/").split("/")[-1],
                    parent_id=parent_id,
                    parent_path=parent_path,
                )
                parent_id = folder.get("id")
                folder_ids[cloud_path] = parent_id
                self.total_folders += 1
            self.total_folders += len(dirs) if rel_path == "." else 0
            for folder_name in dirs:
                folder_path = self._join_cloud_path(cloud_path, folder_name)
                LOGGER.info(f"Teldrive create folder by parentId: {folder_path}")
                folder = await self._create_folder(
                    folder_name,
                    parent_id=parent_id,
                    parent_path=cloud_path,
                )
                folder_ids[folder_path] = folder.get("id")
            for filename in files:
                if self.listener.is_cancelled:
                    break
                result = await self.upload_file(
                    ospath.join(root, filename),
                    cloud_path,
                    create_share=False,
                    ensure_path=False,
                    parent_id=parent_id,
                )
                if result:
                    self.results.append(result)
                    self.total_files += 1

        LOGGER.info(
            f"Teldrive folder upload finished: files={self.total_files}, folders={self.total_folders}"
        )

        root_share = await self._share_file(root_folder.get("id"))
        return root_folder, root_share

    async def upload(self):
        try:
            LOGGER.info(f"Teldrive Uploading: {self._path}")
            LOGGER.info(f"Teldrive API endpoint: {self.api_url}")
            if not self.api_key:
                raise ValueError("Teldrive API key not configured! Please set it in user settings or config.")
            if not self.api_url:
                raise ValueError("Teldrive API URL not configured!")

            self._updater = SetInterval(self.update_interval, self.progress)

            if await aiopath.isfile(self._path):
                result = await self.upload_file(self._path, self._join_cloud_path(self.base_path))
                if not result:
                    return
                self.results.append(result)
                self.total_files = 1
                mime_type = guess_type(self._path)[0] or "File"
                link = {
                    "share_link": result.get("share_link", ""),
                    "direct_link": result.get("direct_link", ""),
                }
            elif await aiopath.isdir(self._path):
                folder, folder_share = await self._upload_dir(self._path)
                mime_type = "Folder"
                link = {"share_link": folder_share, "direct_link": ""}
            else:
                raise ValueError("Invalid file path!")

            if self.listener.is_cancelled:
                return

            LOGGER.info(f"Uploaded To Teldrive: {self.listener.name}")
            await self.listener.on_upload_complete(
                link,
                self.total_files,
                self.total_folders,
                mime_type,
                dir_id="",
            )
        except Exception as err:
            if isinstance(err, RetryError):
                LOGGER.info(f"Total Attempts: {err.last_attempt.attempt_number}")
                err = err.last_attempt.exception()
            raw_err = str(err).replace(">", "").replace("<", "")
            human_err = self._human_error(raw_err)
            LOGGER.error(raw_err)
            self._is_errored = True
            await self.listener.on_upload_error(human_err)
        finally:
            self.is_uploading = False
            if self._updater:
                self._updater.cancel()
            await self._close_session()

    async def cancel_task(self):
        self.listener.is_cancelled = True
        if self.is_uploading:
            LOGGER.info(f"Cancelling Teldrive Upload: {self.listener.name}")
            await self.listener.on_upload_error("Teldrive upload has been cancelled!")
