from asyncio import wait_for

from ..pikpak_utils.pikpak_client import PikPakClient, is_pikpak_share_url
from ...telegram_helper.message_utils import edit_message, send_message


async def resolve_pikpak_link(listener, link, timeout=180):
    if not is_pikpak_share_url(link):
        return None

    status_msg = await send_message(
        listener.message,
        "PikPak: restoring shared file to drive...",
    )
    try:
        pikpak = PikPakClient()
        download = await wait_for(pikpak.save_share_and_get_download(link), timeout=timeout)
        if not download.get("url"):
            raise ValueError("PikPak did not return a download URL for this share.")
        if not getattr(listener, "name", ""):
            listener.name = download.get("name", "")
        listener.source_url = link
        listener.pikpak_cleanup_ids = download.get("cleanup_ids", []) or []
        await edit_message(status_msg, "PikPak: direct link generated, starting download...")
        return download
    finally:
        try:
            await status_msg.delete()
        except Exception:
            pass
