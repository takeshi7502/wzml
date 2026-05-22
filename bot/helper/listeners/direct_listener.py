from asyncio import sleep, TimeoutError
from aiohttp.client_exceptions import ClientError

from ... import LOGGER
from ...core.torrent_manager import TorrentManager, aria2_name
from ..mirror_leech_utils.pikpak_utils.pikpak_client import PikPakClient


class DirectListener:
    def __init__(self, path, listener, a2c_opt):
        self.listener = listener
        self._path = path
        self._a2c_opt = a2c_opt
        self._proc_bytes = 0
        self._failed = 0
        self.download_task = None
        self.name = self.listener.name

    @property
    def processed_bytes(self):
        if self.download_task:
            return self._proc_bytes + int(
                self.download_task.get("completedLength", "0")
            )
        return self._proc_bytes

    @property
    def speed(self):
        return (
            int(self.download_task.get("downloadSpeed", "0"))
            if self.download_task
            else 0
        )

    def _update_content_size(self, content):
        if int(content.get("size") or 0) > 0:
            return
        if not self.download_task:
            return
        size = int(self.download_task.get("totalLength", "0"))
        if size <= 0:
            return
        content["size"] = size
        self.listener.size += size

    async def download(self, contents):
        self.is_downloading = True
        for content in contents:
            if self.listener.is_cancelled:
                break
            if content["path"]:
                self._a2c_opt["dir"] = f"{self._path}/{content['path']}"
            else:
                self._a2c_opt["dir"] = self._path
            filename = content["filename"]
            self._a2c_opt["out"] = filename
            try:
                if not content.get("url") and content.get("pikpak_file_id"):
                    pikpak = PikPakClient()
                    restore_data = await pikpak.restore_share(
                        content["pikpak_share_id"],
                        content.get("pikpak_pass_code_token"),
                        [content["pikpak_file_id"]],
                    )
                    saved = await pikpak.wait_saved_file(restore_data, metadata=True)
                    file_id = saved["file_id"]
                    cleanup_id = saved.get("cleanup_id") or file_id
                    if cleanup_id:
                        cleanup_ids = getattr(self.listener, "pikpak_cleanup_ids", []) or []
                        cleanup_ids.append(cleanup_id)
                        self.listener.pikpak_cleanup_ids = list(dict.fromkeys(cleanup_ids))
                    item = await pikpak.get_download_url(file_id)
                    if not item.get("url"):
                        nested = await pikpak.get_download_from_file_or_folder(file_id)
                        nested_items = nested if isinstance(nested, list) else [nested]
                        item = next((entry for entry in nested_items if entry.get("url")), {})
                    content["url"] = item.get("url")
                    if not content.get("url"):
                        self._failed += 1
                        LOGGER.error(f"Unable to resolve PikPak direct URL for {filename}")
                        continue
                gid = await TorrentManager.aria2.addUri(
                    uris=[content["url"]], options=self._a2c_opt, position=0
                )
            except (TimeoutError, ClientError, Exception) as e:
                self._failed += 1
                LOGGER.error(f"Unable to download {filename} due to: {e}")
                continue
            self.download_task = await TorrentManager.aria2.tellStatus(gid)
            self._update_content_size(content)
            while True:
                if self.listener.is_cancelled:
                    if self.download_task:
                        await TorrentManager.aria2_remove(self.download_task)
                    break
                self.download_task = await TorrentManager.aria2.tellStatus(gid)
                self._update_content_size(content)
                if error_message := self.download_task.get("errorMessage"):
                    self._failed += 1
                    LOGGER.error(
                        f"Unable to download {aria2_name(self.download_task)} due to: {error_message}"
                    )
                    await TorrentManager.aria2_remove(self.download_task)
                    break
                elif self.download_task.get("status", "") == "complete":
                    self._proc_bytes += int(self.download_task.get("totalLength", "0"))
                    await TorrentManager.aria2_remove(self.download_task)
                    break
                await sleep(1)
            self.download_task = None
        if self.listener.is_cancelled:
            return
        if self._failed == len(contents):
            await self.listener.on_download_error("All files are failed to download!")
            return
        await self.listener.on_download_complete()
        return

    async def cancel_task(self):
        self.listener.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self.listener.name}")
        await self.listener.on_download_error("Download Cancelled by User!")
        if self.download_task:
            await TorrentManager.aria2_remove(self.download_task)
