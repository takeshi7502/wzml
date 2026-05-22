from asyncio import Lock, sleep
from hashlib import md5
from time import time
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientSession, ClientTimeout

from ....core.config_manager import Config
from ....helper.ext_utils.exceptions import DirectDownloadLinkException
from ....helper.ext_utils.db_handler import database

AUTH_BASE = "https://user.mypikpak.com"
API_BASE = "https://api-drive.mypikpak.com"
AUTH_CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
CAPTCHA_CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
CLIENT_SECRET = ""
CLIENT_VERSION = "2.0.0"
CLIENT_PACKAGE = "mypikpak.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
DEFAULT_FOLDER = "/WZML"
SHARE_HOSTS = {"mypikpak.com", "www.mypikpak.com", "pikpakdrive.com", "www.pikpakdrive.com"}

_CAPTCHA_SALTS = [
    "C9qPpZLN8ucRTaTiUMWYS9cQvWOE",
    "+r6CQVxjzJV6LCV",
    "F",
    "pFJRC",
    "9WXYIDGrwTCz2OiVlgZa90qpECPD6olt",
    "/750aCr4lm/Sly/c",
    "RB+DT/gZCrbV",
    "",
    "CyLsf7hdkIRxRm215hl",
    "7xHvLi2tOYP0Y92b",
    "ZGTXXxu8E/MIWaEDB+Sm/",
    "1UI3",
    "E7fP5Pfijd+7K+t6Tg/NhuLq0eEUVChpJSkrKxpO",
    "ihtqpG6FMt65+Xk+tWUH2",
    "NhXXU9rg4XXdzo7u5o",
]


def _to_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def _first_dict(*values):
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _pick_file_id(data):
    if not isinstance(data, dict):
        return ""
    candidates = [
        data.get("file") or {},
        data.get("file_info") or {},
        data.get("reference_resource") or {},
        data.get("resource") or {},
        data.get("params") or {},
        data,
    ]
    for item in candidates:
        if isinstance(item, dict) and item.get("id"):
            return item.get("id", "")
    return data.get("file_id") or data.get("fid") or data.get("resource_id") or ""


def _pick_reference_resource(data):
    if not isinstance(data, dict):
        return {}
    candidates = [
        data.get("reference_resource") or {},
        data.get("task") or {},
        data.get("task_info") or {},
        data.get("data") or {},
    ]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        ref = item.get("reference_resource") or item.get("file") or item.get("resource")
        if isinstance(ref, dict) and ref.get("id"):
            return ref
        if item.get("id") and (item.get("kind") or item.get("name")):
            return item
    return {}


def _pick_share_items(data):
    if not isinstance(data, dict):
        return []
    for key in ("files", "share_list", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    share = data.get("share") or {}
    if isinstance(share, dict):
        for key in ("files", "share_list", "items"):
            value = share.get(key)
            if isinstance(value, list):
                return value
    return []


def _pick_pass_code_token(data):
    if not isinstance(data, dict):
        return ""
    candidates = [data, data.get("share") or {}, data.get("data") or {}]
    for item in candidates:
        if isinstance(item, dict):
            token = item.get("pass_code_token") or item.get("passCodeToken") or item.get("access_token")
            if token:
                return token
    return ""


def _pick_task_id(data):
    if not isinstance(data, dict):
        return ""
    candidates = [
        data,
        data.get("task") or {},
        data.get("task_info") or {},
        data.get("data") or {},
    ]
    for item in candidates:
        if isinstance(item, dict):
            task_id = item.get("id") or item.get("task_id") or item.get("restore_task_id")
            if task_id:
                return task_id
    return ""


def _pick_media_url(data):
    if not isinstance(data, dict):
        return ""
    for media in data.get("medias") or []:
        if not isinstance(media, dict):
            continue
        link = media.get("link") or {}
        if isinstance(link, dict) and link.get("url"):
            return link.get("url", "")
        if media.get("url"):
            return media.get("url", "")
    return data.get("download_url") or data.get("url") or ""


def _debug_keys(data):
    if isinstance(data, dict):
        return ",".join(list(data.keys())[:12])
    return type(data).__name__


def _debug_item(item):
    if not isinstance(item, dict):
        return type(item).__name__
    return f"id={item.get('id','')[:12]} kind={item.get('kind','')} name={item.get('name','')[:60]}"


def is_pikpak_share_url(link):
    try:
        parsed = urlparse(link.strip())
    except Exception:
        return False
    host = parsed.netloc.lower().split(":", 1)[0]
    parts = [part for part in parsed.path.split("/") if part]
    return host in SHARE_HOSTS and len(parts) >= 2 and parts[0] == "s"


def parse_pikpak_share_url(link):
    parsed = urlparse(link.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or parts[0] != "s" or len(parts) < 2:
        raise DirectDownloadLinkException("ERROR: Invalid PikPak share URL.")
    query = parse_qs(parsed.query)
    pass_code = (
        query.get("pwd", [""])[0]
        or query.get("pass_code", [""])[0]
        or query.get("passcode", [""])[0]
        or query.get("code", [""])[0]
    )
    return parts[1], pass_code


class PikPakClient:
    def __init__(self):
        self.refresh_token = Config.PIKPAK_REFRESH_TOKEN.strip()
        if not self.refresh_token:
            raise DirectDownloadLinkException("ERROR: PIKPAK_REFRESH_TOKEN is missing!")
        self.device_id = Config.PIKPAK_DEVICE_ID.strip() or md5(self.refresh_token.encode()).hexdigest()
        self.access_token = ""
        self.user_id = ""
        self.refresh_at = 0
        self.captcha_cache = {}
        self.lock = Lock()
        self.timeout = ClientTimeout(total=60)

    async def _request(self, method, url, **kwargs):
        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("User-Agent", USER_AGENT)
        if Config.PIKPAK_PROXY:
            kwargs.setdefault("proxy", Config.PIKPAK_PROXY)
        async with ClientSession(timeout=self.timeout, headers=headers) as session:
            async with session.request(method, url, **kwargs) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    if "invalid_grant" in text or "invalid refresh token" in text.lower():
                        owner = f'<a href="tg://user?id={Config.OWNER_ID}">OWNER</a>'
                        raise DirectDownloadLinkException(
                            "ERROR: PikPak refresh token is invalid or expired.\n"
                            f"┠ <b>Owner</b> → {owner}\n"
                            "┠ <b>Action</b> → Update <code>PIKPAK_REFRESH_TOKEN</code> in Bot Settings.\n"
                            "┖ <b>Note</b> → The old refresh token may have been refreshed by another process."
                        )
                    raise DirectDownloadLinkException(
                        f"ERROR: PikPak API {resp.status}: {text[:500]}"
                    )
                if not text:
                    return {}
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    return text

    async def _save_rotated_refresh_token(self, token):
        if not token or token == Config.PIKPAK_REFRESH_TOKEN:
            return
        Config.PIKPAK_REFRESH_TOKEN = token
        self.refresh_token = token
        if Config.DATABASE_URL and not database._return:
            await database.update_config({"PIKPAK_REFRESH_TOKEN": token})

    async def _ensure_access_token(self):
        async with self.lock:
            if self.access_token and time() < self.refresh_at:
                return self.access_token
            data = await self._request(
                "POST",
                f"{AUTH_BASE}/v1/auth/token",
                headers={"X-Device-Id": self.device_id},
                json={
                    "client_id": AUTH_CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )
            self.access_token = data.get("access_token", "")
            self.user_id = data.get("sub", "")
            self.refresh_at = time() + max(_to_int(data.get("expires_in")) - 60, 5)
            await self._save_rotated_refresh_token(data.get("refresh_token", ""))
            if not self.access_token:
                raise DirectDownloadLinkException("ERROR: PikPak access token empty!")
            return self.access_token

    def _captcha_sign(self, timestamp):
        value = f"{CAPTCHA_CLIENT_ID}{CLIENT_VERSION}{CLIENT_PACKAGE}{self.device_id}{timestamp}"
        for salt in _CAPTCHA_SALTS:
            value = md5(f"{value}{salt}".encode()).hexdigest()
        return f"1.{value}"

    async def _captcha_token(self, action, previous=""):
        cached = self.captcha_cache.get(action)
        if cached and time() < cached[1] and cached[0] != previous:
            return cached[0]
        access = await self._ensure_access_token()
        timestamp = str(int(time() * 1000))
        data = await self._request(
            "POST",
            f"{AUTH_BASE}/v1/shield/captcha/init",
            headers={
                "Accept": "*/*",
                "Accept-Language": "en",
                "Content-Type": "application/json",
                "Origin": "https://mypikpak.com",
                "Referer": "https://mypikpak.com/",
                "X-Client-Id": CAPTCHA_CLIENT_ID,
                "X-Client-Version": "1.0.0",
                "X-Device-Id": self.device_id,
                "X-Device-Model": "chrome/148.0.0.0",
                "X-Device-Name": "PC-Chrome",
                "X-Device-Sign": f"wdi10.{self.device_id}xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                "X-Net-Work-Type": "NONE",
                "X-Os-Version": "Win32",
                "X-Platform-Version": "1",
                "X-Protocol-Version": "301",
                "X-Provider-Name": "NONE",
                "X-Sdk-Version": "8.1.4",
            },
            json={
                "action": action,
                "captcha_token": previous,
                "client_id": CAPTCHA_CLIENT_ID,
                "device_id": self.device_id,
                "meta": {
                    "captcha_sign": self._captcha_sign(timestamp),
                    "user_id": self.user_id,
                    "package_name": CLIENT_PACKAGE,
                    "client_version": CLIENT_VERSION,
                    "timestamp": timestamp,
                },
            },
        )
        token = data.get("captcha_token", "")
        if not token:
            raise DirectDownloadLinkException("ERROR: PikPak captcha token empty!")
        self.captcha_cache[action] = (token, time() + 1800)
        return token

    async def _drive_get(self, path, action, params=None):
        url = f"{API_BASE}{path}"
        previous = ""
        for _ in range(2):
            access = await self._ensure_access_token()
            captcha = await self._captcha_token(action, previous)
            try:
                return await self._request(
                    "GET",
                    url,
                    headers={
                        "Authorization": f"Bearer {access}",
                        "X-Device-Id": self.device_id,
                        "X-Captcha-Token": captcha,
                    },
                    params=params or {},
                )
            except DirectDownloadLinkException as e:
                if '"error_code":9' in str(e).replace(" ", ""):
                    previous = captcha
                    continue
                raise
        raise DirectDownloadLinkException("ERROR: PikPak captcha retry failed!")

    async def _drive_post(self, path, action, json=None, params=None):
        url = f"{API_BASE}{path}"
        previous = ""
        for _ in range(2):
            access = await self._ensure_access_token()
            captcha = await self._captcha_token(action, previous)
            try:
                return await self._request(
                    "POST",
                    url,
                    headers={
                        "Authorization": f"Bearer {access}",
                        "X-Device-Id": self.device_id,
                        "X-Captcha-Token": captcha,
                    },
                    json=json or {},
                    params=params or {},
                )
            except DirectDownloadLinkException as e:
                if '"error_code":9' in str(e).replace(" ", ""):
                    previous = captcha
                    continue
                raise
        raise DirectDownloadLinkException("ERROR: PikPak captcha retry failed!")

    async def quota(self):
        return await self._drive_get("/drive/v1/about", "GET:/drive/v1/about")

    async def list_folder(self, parent_id=""):
        files = []
        page_token = ""
        while True:
            params = {
                "parent_id": parent_id,
                "limit": "500",
                "thumbnail_size": "SIZE_MEDIUM",
                "with_audit": "false",
                "filters": '{"trashed":{"eq":false}}',
            }
            if page_token:
                params["page_token"] = page_token
            data = await self._drive_get("/drive/v1/files", "GET:/drive/v1/files", params)
            files.extend(data.get("files", []))
            page_token = data.get("next_page_token") or ""
            if not page_token:
                return files

    async def resolve_path_info(self, path):
        normalized = path.strip("/")
        if not normalized:
            raise DirectDownloadLinkException("ERROR: PikPak path must not be empty!")
        parent_id = ""
        parts = [p for p in normalized.split("/") if p]
        for index, part in enumerate(parts):
            children = await self.list_folder(parent_id)
            found = next((f for f in children if f.get("name") == part), None)
            if not found:
                raise DirectDownloadLinkException(f"ERROR: PikPak path not found: {path}")
            if index == len(parts) - 1:
                return found
            if found.get("kind") != "drive#folder":
                raise DirectDownloadLinkException(f"ERROR: PikPak path segment is not folder: {part}")
            parent_id = found.get("id", "")
        raise DirectDownloadLinkException(f"ERROR: PikPak path not found: {path}")

    async def get_download_url(self, file_id):
        data = await self._drive_get(f"/drive/v1/files/{file_id}", "GET:/drive/v1/files/:id")
        url = data.get("web_content_link", "") or _pick_media_url(data)
        return {
            "url": url,
            "name": data.get("name", ""),
            "size": _to_int(data.get("size")),
            "kind": data.get("kind", ""),
            "id": data.get("id", file_id),
        }

    async def get_download_from_file_or_folder(self, file_id, debug=None):
        download = await self.get_download_url(file_id)
        if debug is not None:
            debug.append(
                f"detail: id={file_id[:12]} kind={download.get('kind')} "
                f"url={'yes' if download.get('url') else 'no'}"
            )
        if download.get("url"):
            return download
        if download.get("kind") == "drive#folder":
            children = await self.list_folder(file_id)
            if debug is not None:
                debug.append(f"folder children: count={len(children)}")
                for child in children[:5]:
                    debug.append(f"child: {_debug_item(child)}")
            downloads = []
            for child in children:
                if not isinstance(child, dict) or not child.get("id"):
                    continue
                if child.get("kind") == "drive#folder":
                    nested = await self.get_download_from_file_or_folder(child.get("id"), debug)
                    if isinstance(nested, list):
                        downloads.extend(nested)
                    elif nested.get("url"):
                        downloads.append(nested)
                    continue
                child_download = await self.get_download_url(child.get("id"))
                if debug is not None:
                    debug.append(
                        f"child detail: id={child.get('id','')[:12]} kind={child_download.get('kind')} "
                        f"url={'yes' if child_download.get('url') else 'no'}"
                    )
                if child_download.get("url"):
                    child_download.setdefault("parent_folder_id", file_id)
                    downloads.append(child_download)
            if downloads:
                folder_name = download.get("name") or "PikPak Folder"
                for item in downloads:
                    item.setdefault("folder_name", folder_name)
                return downloads
        return download

    async def trash_files(self, file_ids):
        ids = [file_id for file_id in file_ids if file_id]
        if not ids:
            return {}
        payloads = [
            {"ids": ids},
            {"file_ids": ids},
            {"files": [{"id": file_id} for file_id in ids]},
        ]
        last_error = None
        for payload in payloads:
            try:
                return await self._drive_post(
                    "/drive/v1/files:batchTrash",
                    "POST:/drive/v1/files:batchTrash",
                    json=payload,
                )
            except DirectDownloadLinkException as e:
                last_error = e
        raise last_error

    async def ensure_folder(self, folder_path=DEFAULT_FOLDER):
        normalized = folder_path.strip("/")
        if not normalized:
            return ""
        parent_id = ""
        for part in [p for p in normalized.split("/") if p]:
            children = await self.list_folder(parent_id)
            found = next(
                (
                    f
                    for f in children
                    if f.get("name") == part and f.get("kind") == "drive#folder"
                ),
                None,
            )
            if found:
                parent_id = found.get("id", "")
                continue
            data = await self._drive_post(
                "/drive/v1/files",
                "POST:/drive/v1/files",
                json={
                    "kind": "drive#folder",
                    "name": part,
                    "parent_id": parent_id,
                    "folder_type": "NORMAL",
                },
            )
            parent_id = data.get("id", "") or data.get("file", {}).get("id", "")
            if not parent_id:
                raise DirectDownloadLinkException(f"ERROR: PikPak could not create folder: {part}")
        return parent_id

    async def save_link(self, link, parent_id=""):
        if not parent_id:
            parent_id = await self.ensure_folder(DEFAULT_FOLDER)
        payload = {
            "kind": "drive#file",
            "parent_id": parent_id,
            "folder_type": "DOWNLOAD" if parent_id else "",
            "upload_type": "UPLOAD_TYPE_URL",
            "url": {"url": link},
            "params": {"from": "manual"},
        }
        return await self._drive_post(
            "/drive/v1/files",
            "POST:/drive/v1/files",
            json=payload,
        )

    async def get_share_info(self, share_id, pass_code="", parent_id=None):
        return await self._drive_get(
            "/drive/v1/share",
            "GET:/drive/v1/share",
            params={
                "limit": "100",
                "thumbnail_size": "SIZE_LARGE",
                "order": "3",
                "share_id": share_id,
                "parent_id": parent_id or "",
                "pass_code": pass_code or "",
            },
        )

    async def get_share_folder(self, share_id, pass_code_token="", parent_id=None):
        return await self._drive_get(
            "/drive/v1/share/detail",
            "GET:/drive/v1/share/detail",
            params={
                "limit": "100",
                "thumbnail_size": "SIZE_LARGE",
                "order": "6",
                "share_id": share_id,
                "parent_id": parent_id or "",
                "pass_code_token": pass_code_token or "",
            },
        )

    async def restore_share(self, share_id, pass_code_token="", file_ids=None):
        payload = {
            "share_id": share_id,
            "pass_code_token": pass_code_token or "",
            "file_ids": file_ids or [],
        }
        return await self._drive_post(
            "/drive/v1/share/restore",
            "POST:/drive/v1/share/restore",
            json=payload,
        )

    async def get_task(self, task_id):
        if not task_id:
            return {}
        return await self._drive_get(
            f"/drive/v1/tasks/{task_id}",
            "GET:/drive/v1/tasks/:id",
            params={"with": "reference_resource"},
        )

    async def wait_saved_file(self, save_data, timeout=900, metadata=False):
        file_id = _pick_file_id(save_data)
        ref = _pick_reference_resource(save_data)
        if ref.get("id"):
            file_id = ref.get("id")
        if file_id:
            result = {
                "file_id": file_id,
                "cleanup_id": file_id,
                "cleanup_kind": ref.get("kind", ""),
                "resource": ref,
                "task": save_data if isinstance(save_data, dict) else {},
            }
            return result if metadata else file_id
        task_id = _pick_task_id(save_data)
        if not task_id:
            direct = _first_dict(save_data.get("file") if isinstance(save_data, dict) else {})
            file_id = _pick_file_id(direct)
            if file_id:
                result = {
                    "file_id": file_id,
                    "cleanup_id": file_id,
                    "cleanup_kind": direct.get("kind", ""),
                    "resource": direct,
                    "task": save_data if isinstance(save_data, dict) else {},
                }
                return result if metadata else file_id
            raise DirectDownloadLinkException(f"ERROR: PikPak save task id not found: {save_data}")
        deadline = time() + timeout
        last_task = save_data
        while time() < deadline:
            await sleep(5)
            task = await self.get_task(task_id)
            if not task:
                continue
            last_task = task
            ref = _pick_reference_resource(task)
            file_id = ref.get("id") or _pick_file_id(task)
            if file_id:
                result = {
                    "file_id": file_id,
                    "cleanup_id": file_id,
                    "cleanup_kind": ref.get("kind", ""),
                    "resource": ref,
                    "task": task,
                }
                return result if metadata else file_id
            status = str(task.get("status") or task.get("phase") or task.get("state") or "").lower()
            message = task.get("message") or task.get("error_description") or task.get("error") or ""
            if any(word in status for word in ("fail", "error", "cancel")):
                raise DirectDownloadLinkException(f"ERROR: PikPak task failed: {message or status}")
        raise DirectDownloadLinkException(f"ERROR: PikPak save task timed out: {last_task}")

    async def save_link_and_get_download(self, link):
        save_data = await self.save_link(link)
        file_id = await self.wait_saved_file(save_data)
        download = await self.get_download_url(file_id)
        if not download.get("url"):
            raise DirectDownloadLinkException("ERROR: PikPak did not return a download URL for saved link.")
        return download

    async def save_share_and_get_download(self, link, debug=False):
        debug_lines = [] if debug else None
        share_id, pass_code = parse_pikpak_share_url(link)
        if debug_lines is not None:
            debug_lines.append(f"share_id={share_id[:16]} pass={'yes' if pass_code else 'no'}")
        share_info = await self.get_share_info(share_id, pass_code)
        if debug_lines is not None:
            debug_lines.append(f"share_info keys={_debug_keys(share_info)}")
        pass_code_token = _pick_pass_code_token(share_info)
        items = _pick_share_items(share_info)
        if debug_lines is not None:
            debug_lines.append(f"share_items={len(items)} token={'yes' if pass_code_token else 'no'}")
            for item in items[:5]:
                debug_lines.append(f"share item: {_debug_item(item)}")
        file_ids = [item.get("id") for item in items if isinstance(item, dict) and item.get("id")]
        if not file_ids:
            raise DirectDownloadLinkException(f"ERROR: PikPak share contains no restorable files: {share_info}")
        restore_data = await self.restore_share(share_id, pass_code_token, file_ids)
        if debug_lines is not None:
            debug_lines.append(
                f"restore keys={_debug_keys(restore_data)} task={_pick_task_id(restore_data)[:12]} file={_pick_file_id(restore_data)[:12]}"
            )
        saved = await self.wait_saved_file(restore_data, metadata=True)
        file_id = saved["file_id"]
        cleanup_id = saved.get("cleanup_id") or file_id
        if debug_lines is not None:
            debug_lines.append(f"restored file_id={file_id[:16]} cleanup_id={cleanup_id[:16]}")
        download = await self.get_download_from_file_or_folder(file_id, debug_lines)
        if cleanup_id:
            targets = download if isinstance(download, list) else [download]
            for index, item in enumerate(targets):
                item["cleanup_ids"] = [cleanup_id] if index == 0 else []
                item["cleanup_kind"] = saved.get("cleanup_kind", "")
        if debug and debug_lines is not None:
            if isinstance(download, list):
                for item in download:
                    item["debug"] = debug_lines
            else:
                download["debug"] = debug_lines
        has_download = any(item.get("url") for item in download) if isinstance(download, list) else download.get("url")
        if not has_download:
            detail = ""
            if debug_lines:
                detail = "\nDebug:\n" + "\n".join(debug_lines[-12:])
            raise DirectDownloadLinkException(
                f"ERROR: PikPak did not return a download URL for restored share.{detail}"
            )
        return download
