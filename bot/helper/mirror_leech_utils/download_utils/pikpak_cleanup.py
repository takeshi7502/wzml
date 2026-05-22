from .... import LOGGER
from ..pikpak_utils.pikpak_client import PikPakClient


async def cleanup_pikpak_restored(listener):
    cleanup_ids = list(dict.fromkeys(getattr(listener, "pikpak_cleanup_ids", []) or []))
    if not cleanup_ids:
        return
    try:
        await PikPakClient().trash_files(cleanup_ids)
        LOGGER.info("PikPak restored resource cleanup completed: %s", ", ".join(cleanup_ids))
        listener.pikpak_cleanup_ids = []
    except Exception as e:
        LOGGER.warning("PikPak restored resource cleanup failed for %s: %s", cleanup_ids, e)
