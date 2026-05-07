from io import BufferedReader
from json import JSONDecodeError
from logging import getLogger
from mimetypes import guess_type
from os import path as ospath
from os import walk as oswalk
from pathlib import Path
from time import time

from aiofiles.os import path as aiopath
from aiohttp import ClientSession, FormData
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
    def __init__(self, filename, read_callback=None):
        super().__init__(open(filename, "rb"))
        self.__read_callback = read_callback
        self.length = Path(filename).stat().st_size

    def read(self, size=None):
        size = size or (self.length - self.tell())
        chunk = super().read(size)
        if self.__read_callback:
            self.__read_callback(self.tell())
        return chunk


class TeleCloudUpload:
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
        self.server_processing_file = ""
        self.results = []

        user_dict = user_data.get(self.listener.user_id, {})
        self.api_url = (
            user_dict.get("TELECLOUD_API_URL")
            or Config.TELECLOUD_API_URL
            or "https://cloud.takeshi.dev/api/upload-api/upload"
        ).rstrip("/")
        self.local_api_url = (
            user_dict.get("TELECLOUD_LOCAL_API_URL")
            or Config.TELECLOUD_LOCAL_API_URL
            or ""
        ).rstrip("/")
        self.upload_api_url = self.local_api_url or self.api_url
        self.api_key = user_dict.get("TELECLOUD_API_KEY") or Config.TELECLOUD_API_KEY
        self.base_path = user_dict.get("TELECLOUD_PATH") or Config.TELECLOUD_PATH or "/"
        self.share = user_dict.get("TELECLOUD_SHARE", Config.TELECLOUD_SHARE)
        self.async_upload = user_dict.get("TELECLOUD_ASYNC", Config.TELECLOUD_ASYNC)
        self.overwrite = user_dict.get("TELECLOUD_OVERWRITE", Config.TELECLOUD_OVERWRITE)

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
        current_file_size = getattr(self, "current_file_size", 0)
        upload_finished = bool(
            current_file_size
            and self.last_uploaded >= max(current_file_size - 1024 * 1024, current_file_size * 0.999)
        )
        if self.is_server_processing or upload_finished:
            return (
                "Uploading to TeleCloud. Pls wait..."
            )
        if self.local_api_url:
            return "Using local TeleCloud API endpoint."
        return ""

    def _join_cloud_path(self, *parts):
        clean_parts = [str(part).strip("/") for part in parts if str(part).strip("/")]
        return "/" + "/".join(clean_parts) if clean_parts else "/"

    async def _parse_response(self, resp):
        try:
            data = await resp.json()
        except (ContentTypeError, JSONDecodeError):
            data = {"error": await resp.text()}
        if resp.status < 200 or resp.status >= 300:
            raise Exception(data.get("error") or f"HTTP {resp.status}: {data}")
        if data.get("error"):
            raise Exception(data["error"])
        return data

    @retry(
        wait=wait_exponential(multiplier=2, min=4, max=8),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    async def upload_file(self, file_path, cloud_path):
        if self.listener.is_cancelled:
            return None

        headers = {"Authorization": f"Bearer {self.api_key}"}
        mime_type = guess_type(file_path)[0] or "application/octet-stream"
        self.last_uploaded = 0
        self.current_file_size = Path(file_path).stat().st_size
        self.is_server_processing = False
        self.server_processing_file = ospath.basename(file_path)

        with ProgressFileReader(file_path, self.__progress_callback) as file:
            form = FormData(quote_fields=False)
            form.add_field(
                "file",
                file,
                filename=ospath.basename(file_path),
                content_type=mime_type,
            )
            form.add_field("path", cloud_path)
            if self.share and not self.async_upload:
                form.add_field("share", "public")
            if self.async_upload and not self.share:
                form.add_field("async", "true")
            if self.overwrite:
                form.add_field("overwrite", "true")

            async with ClientSession() as session:
                async with session.post(
                    self.upload_api_url, headers=headers, data=form, timeout=None
                ) as resp:
                    result = await self._parse_response(resp)
                    self.is_server_processing = False
                    return result

    async def _upload_dir(self, input_directory):
        root_name = ospath.basename(input_directory.rstrip(ospath.sep))
        for root, dirs, files in await sync_to_async(lambda: list(oswalk(input_directory))):
            if self.listener.is_cancelled:
                break
            rel_path = ospath.relpath(root, input_directory)
            rel_cloud = "" if rel_path == "." else rel_path.replace("\\", "/")
            cloud_path = self._join_cloud_path(self.base_path, root_name, rel_cloud)
            if rel_path != ".":
                self.total_folders += 1
            self.total_folders += len(dirs) if rel_path == "." else 0
            for filename in files:
                if self.listener.is_cancelled:
                    break
                result = await self.upload_file(ospath.join(root, filename), cloud_path)
                if result:
                    self.results.append(result)
                    self.total_files += 1

    async def upload(self):
        try:
            LOGGER.info(f"TeleCloud Uploading: {self._path}")
            LOGGER.info(f"TeleCloud API endpoint: {self.upload_api_url}")
            if not self.api_key:
                raise ValueError("TeleCloud API key not configured! Please set it in user settings or config.")
            if not self.api_url:
                raise ValueError("TeleCloud API URL not configured!")

            self._updater = SetInterval(self.update_interval, self.progress)

            if await aiopath.isfile(self._path):
                result = await self.upload_file(self._path, self.base_path)
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
                await self._upload_dir(self._path)
                mime_type = "Folder"
                link = ""
                for result in self.results:
                    if result.get("direct_link") or result.get("share_link"):
                        link = {
                            "share_link": result.get("share_link", ""),
                            "direct_link": result.get("direct_link", ""),
                        }
                        break
            else:
                raise ValueError("Invalid file path!")

            if self.listener.is_cancelled:
                return

            LOGGER.info(f"Uploaded To TeleCloud: {self.listener.name}")
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
            LOGGER.info(f"Cancelling TeleCloud Upload: {self.listener.name}")
            await self.listener.on_upload_error("TeleCloud upload has been cancelled!")
