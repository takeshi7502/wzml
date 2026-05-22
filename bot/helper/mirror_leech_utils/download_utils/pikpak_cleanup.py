from .... import LOGGER, task_dict, task_dict_lock
from ..pikpak_utils.pikpak_client import PikPakClient
from ...telegram_helper.message_utils import send_message


def _is_pikpak_token_error(error):
    return "PikPak refresh token is invalid or expired" in str(error)


def _cleanup_error_summary(error):
    message = str(error).splitlines()[0].strip()
    return message or error.__class__.__name__


async def _send_pikpak_token_error(listener):
    message = getattr(listener, "message", None)
    if message is None:
        return
    await send_message(
        message,
        "PikPak error: ERROR: PikPak refresh token is invalid or expired.",
    )


async def cleanup_pikpak_restored(listener):
    cleanup_ids = list(dict.fromkeys(getattr(listener, "pikpak_cleanup_ids", []) or []))
    if not cleanup_ids:
        return

    folder_name = getattr(listener, "folder_name", "")
    same_dir = getattr(listener, "same_dir", {}) or {}
    if folder_name and folder_name in same_dir:
        active_ids = set(same_dir[folder_name].get("tasks", set())) - {listener.mid}
        async with task_dict_lock:
            for mid in active_ids:
                task = task_dict.get(mid)
                other = getattr(task, "listener", None)
                if other is None:
                    other = getattr(task, "_listener", None)
                if other is None:
                    other = getattr(task, "__listener", None)
                if other is not None:
                    other.pikpak_cleanup_ids = list(
                        dict.fromkeys((getattr(other, "pikpak_cleanup_ids", []) or []) + cleanup_ids)
                    )
                    listener.pikpak_cleanup_ids = []
                    LOGGER.info(
                        "PikPak cleanup postponed to active same_dir task %s: %s",
                        mid,
                        ", ".join(cleanup_ids),
                    )
                    return

    try:
        await PikPakClient().trash_files(cleanup_ids)
        LOGGER.info("PikPak restored resource cleanup completed: %s", ", ".join(cleanup_ids))
        listener.pikpak_cleanup_ids = []
    except Exception as e:
        LOGGER.warning(
            "PikPak restored resource cleanup failed for %s: %s",
            cleanup_ids,
            _cleanup_error_summary(e),
        )
        if _is_pikpak_token_error(e):
            await _send_pikpak_token_error(listener)
