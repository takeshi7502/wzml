from asyncio import Semaphore, gather
from io import BufferedReader
from json import JSONDecodeError
from logging import getLogger
from mimetypes import guess_type
from os import path as ospath
from os import walk as oswalk
from pathlib import Path
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
from bot.helper.ext_utils.bot_utils import SetInterval, sync_to_async

LOGGER = getLogger(__name__)


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
            or "https://teledrive.takeshi.dev"
        ).rstrip("/")
        if self.api_url.endswith("/api"):
            self.api_url = self.api_url[:-4]
        self.api_key = user_dict.get("TELDRIVE_API_KEY") or Config.TELDRIVE_API_KEY
        self.base_path = user_dict.get("TELDRIVE_PATH") or Config.TELDRIVE_PATH or "/"
        self.share = user_dict.get("TELDRIVE_SHARE", Config.TELDRIVE_SHARE)
        self.overwrite = user_dict.get("TELDRIVE_OVERWRITE", Config.TELDRIVE_OVERWRITE)
        self.channel_id = user_dict.get("TELDRIVE_CHANNEL_ID") or Config.TELDRIVE_CHANNEL_ID
        self.api_timeout = ClientTimeout(total=60, connect=15, sock_connect=15, sock_read=45)
        self.upload_timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=300)

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
            return "Teldrive is saving upload to Telegram. Pls wait..."
        return ""

    def _api(self, route):
        return f"{self.api_url}/api/{route.lstrip('/')}"

    def _headers(self):
        token = self.api_key.strip()
        if token.startswith("access_token="):
            return {"Cookie": token}
        return {"Cookie": f"access_token={token}"}

    def _join_cloud_path(self, *parts):
        clean_parts = [str(part).strip("/") for part in parts if str(part).strip("/")]
        return "/" + "/".join(clean_parts) if clean_parts else "/"

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
        async with ClientSession(headers=self._headers(), timeout=self.api_timeout) as session:
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
                return await self._find_file(name, "folder", parent)

    async def _find_file(self, name, file_type, cloud_path):
        params = {
            "name": name,
            "type": file_type,
            "path": cloud_path,
            "operation": "find",
            "limit": 100,
        }
        async with ClientSession(headers=self._headers(), timeout=self.api_timeout) as session:
            async with session.get(self._api("files"), params=params) as resp:
                data = await self._parse_response(resp)
        items = data.get("items") or []
        for item in items:
            if item.get("name") == name and item.get("type") == file_type:
                return item
        return items[0] if items else None

    async def _delete_file(self, file_id):
        if not file_id:
            return
        async with ClientSession(headers=self._headers(), timeout=self.api_timeout) as session:
            async with session.post(
                self._api("files/delete"),
                json={"ids": [file_id]},
            ) as resp:
                await self._parse_response(resp, allow_empty=True)

    async def _create_or_get_folder(self, folder_path):
        parent_path = self._join_cloud_path(*folder_path.strip("/").split("/")[:-1])
        folder_name = folder_path.rstrip("/").split("/")[-1]
        LOGGER.info(f"Teldrive mkdir: {folder_path}")
        await self._mkdir(folder_path)
        for attempt in range(5):
            folder = await self._find_file(folder_name, "folder", parent_path)
            if folder:
                return folder
            await sleep(1 + attempt)
        raise Exception(f"Teldrive folder was created but not found: {folder_path}")

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

        async with ClientSession(headers=self._headers(), timeout=self.api_timeout) as session:
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
        async with ClientSession(headers=self._headers(), timeout=self.api_timeout) as session:
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

        async with ClientSession(headers=self._headers(), timeout=self.api_timeout) as session:
            async with session.post(self._api("files"), json=payload) as resp:
                return await self._parse_response(resp)

    async def _share_file_data(self, file_id):
        if not self.share or not file_id:
            return {}
        async with ClientSession(headers=self._headers(), timeout=self.api_timeout) as session:
            async with session.post(
                self._api(f"files/{file_id}/share"),
                json={"expiresAt": "2099-12-31T23:59:59.000Z"},
            ) as resp:
                await self._parse_response(resp, allow_empty=True)
            async with session.get(self._api(f"files/{file_id}/share")) as resp:
                data = await self._parse_response(resp)
        return data or {}

    async def _share_file(self, file_id):
        data = await self._share_file_data(file_id)
        return self._share_link(data.get("id"))

    async def upload_file(self, file_path, cloud_path, create_share=True):
        file_name = ospath.basename(file_path)
        if not self.overwrite:
            existing = await self._find_file(file_name, "file", cloud_path)
            if existing:
                LOGGER.info(f"Teldrive file exists, skipping upload: {cloud_path}/{file_name}")
                share_data = await self._share_file_data(existing.get("id"))
                share_id = share_data.get("id", "")
                return {
                    "file": existing,
                    "share_link": self._share_link(share_id),
                    "direct_link": self._direct_link(existing, share_id),
                    "skipped": True,
                }

        upload_id = str(uuid4())
        upload_part = await self._upload_part(upload_id, file_path)
        if not upload_part:
            return None
        created = await self._create_file(upload_id, file_path, cloud_path, upload_part)
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
            LOGGER.info(f"Teldrive folder file: {file_path} -> {cloud_path}")
            return await self.upload_file(file_path, cloud_path, create_share=False)

    async def _upload_dir(self, input_directory):
        root_name = ospath.basename(input_directory.rstrip(ospath.sep))
        root_cloud_path = self._join_cloud_path(self.base_path, root_name)
        root_folder = await self._create_or_get_folder(root_cloud_path)

        upload_jobs = []
        semaphore = Semaphore(2)
        for root, dirs, files in await sync_to_async(lambda: list(oswalk(input_directory))):
            if self.listener.is_cancelled:
                break
            rel_path = ospath.relpath(root, input_directory)
            rel_cloud = "" if rel_path == "." else rel_path.replace("\\", "/")
            cloud_path = self._join_cloud_path(root_cloud_path, rel_cloud)
            if rel_path != ".":
                LOGGER.info(f"Teldrive mkdir: {cloud_path}")
                await self._mkdir(cloud_path)
                self.total_folders += 1
            self.total_folders += len(dirs) if rel_path == "." else 0
            for folder in dirs:
                folder_path = self._join_cloud_path(cloud_path, folder)
                LOGGER.info(f"Teldrive mkdir: {folder_path}")
                await self._mkdir(folder_path)
            for filename in files:
                if self.listener.is_cancelled:
                    break
                upload_jobs.append(
                    self._upload_folder_file(ospath.join(root, filename), cloud_path, semaphore)
                )

        for result in await gather(*upload_jobs):
            if result:
                self.results.append(result)
                self.total_files += 1

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
            err = str(err).replace(">", "").replace("<", "")
            LOGGER.error(err)
            self._is_errored = True
            await self.listener.on_upload_error(err)
        finally:
            self.is_uploading = False
            if self._updater:
                self._updater.cancel()

    async def cancel_task(self):
        self.listener.is_cancelled = True
        if self.is_uploading:
            LOGGER.info(f"Cancelling Teldrive Upload: {self.listener.name}")
            await self.listener.on_upload_error("Teldrive upload has been cancelled!")
