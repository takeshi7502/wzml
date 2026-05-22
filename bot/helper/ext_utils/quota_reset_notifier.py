from asyncio import sleep
from datetime import datetime

from pytz import timezone, utc

from ... import LOGGER, auth_chats, bot_loop
from ...core.config_manager import Config
from ...core.tg_client import TgClient

_quota_reset_notifier_task = None
_last_notify_key = ""


def _quota_timezone():
    try:
        return timezone(Config.TIMEZONE)
    except Exception:
        return utc


def _quota_reset_message(now):
    return (
        "➲ <b>Quota Reset Successfully!</b>\n"
        f"├ <b>Date:</b> {now.strftime('%d/%m/%Y')}\n"
        f"├ <b>Time:</b> {now.strftime('%I:%M:%S %p')}\n"
        f"└ <b>TimeZone:</b> {Config.TIMEZONE}"
    )


async def _send_quota_reset_notice(now):
    text = _quota_reset_message(now)
    sent = 0
    for chat_id in list(auth_chats.keys()):
        try:
            chat_id = int(chat_id)
        except Exception:
            continue
        if chat_id >= 0:
            continue
        try:
            await TgClient.bot.send_message(
                chat_id,
                text,
                disable_web_page_preview=True,
                disable_notification=True,
            )
            sent += 1
        except Exception as e:
            LOGGER.warning("Failed to send quota reset notice to %s: %s", chat_id, e)
    if sent:
        LOGGER.info("Quota reset notice sent to %s authorized group(s)", sent)


async def _quota_reset_notifier_loop():
    global _last_notify_key
    while True:
        try:
            if Config.USER_QUOTA_ENABLED and Config.USER_QUOTA_RESET_NOTIFY:
                now = datetime.now(_quota_timezone())
                reset_hour = max(0, min(23, int(Config.USER_QUOTA_RESET_HOUR)))
                notify_key = now.strftime("%Y-%m-%d")
                if now.hour == reset_hour and _last_notify_key != notify_key:
                    _last_notify_key = notify_key
                    await _send_quota_reset_notice(now)
        except Exception as e:
            LOGGER.warning("Quota reset notifier tick failed: %s", e)
        await sleep(60)


def start_quota_reset_notifier():
    global _quota_reset_notifier_task
    if _quota_reset_notifier_task is None or _quota_reset_notifier_task.done():
        _quota_reset_notifier_task = bot_loop.create_task(_quota_reset_notifier_loop())
        LOGGER.info("Quota reset notifier started")
