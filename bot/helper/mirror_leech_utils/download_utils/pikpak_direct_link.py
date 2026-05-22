from asyncio import wait_for

from ..pikpak_utils.pikpak_client import PikPakClient, is_pikpak_share_url
from ...telegram_helper.message_utils import edit_message, send_message


async def resolve_pikpak_link(listener, link, timeout=180):
    if not is_pikpak_share_url(link):
        return None

    status_msg = await send_message(
        listener.message,
        "PikPak: restoring shared file/folder to drive...",
    )
    try:
        pikpak = PikPakClient()
        download = await wait_for(pikpak.save_share_and_get_download(link), timeout=timeout)
        if isinstance(download, dict) and download.get("contents"):
            if not getattr(listener, "name", ""):
                listener.name = download.get("title", "PikPak Folder")
            listener.source_url = link
            listener.pikpak_cleanup_ids = download.get("cleanup_ids", []) or []
            await edit_message(status_msg, "PikPak: folder links generated, starting download...")
            return download
        downloads = download if isinstance(download, list) else [download]
        if not any(item.get("url") for item in downloads):
            raise ValueError("PikPak did not return a download URL for this share.")
        first = next((item for item in downloads if item.get("url")), downloads[0])
        if not getattr(listener, "name", ""):
            listener.name = first.get("name", "")
        listener.source_url = link
        listener.pikpak_cleanup_ids = first.get("cleanup_ids", []) or []
        await edit_message(status_msg, "PikPak: direct link generated, starting download...")
        return download
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass
