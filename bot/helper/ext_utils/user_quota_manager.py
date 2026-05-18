from asyncio import Lock
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ... import LOGGER, sudo_users, user_data
from ...core.config_manager import Config
from .db_handler import database
from .status_utils import get_readable_time

QUOTA_KEY = "USER_QUOTA"
_locks = defaultdict(Lock)


def _default_quota(now_key=None):
    return {
        "used_today": 0,
        "extra_quota": 0,
        "pending": {},
        "last_reset_key": now_key or _reset_key(),
        "total_used": 0,
    }


def _now():
    try:
        return datetime.now(ZoneInfo(Config.TIMEZONE))
    except Exception:
        LOGGER.warning("Invalid TIMEZONE %s, falling back to UTC", Config.TIMEZONE)
        return datetime.now(ZoneInfo("UTC"))


def _reset_hour():
    try:
        return max(0, min(23, int(Config.USER_QUOTA_RESET_HOUR)))
    except Exception:
        return 3


def _reset_key(now=None):
    now = now or _now()
    reset_at = now.replace(hour=_reset_hour(), minute=0, second=0, microsecond=0)
    if now < reset_at:
        reset_at -= timedelta(days=1)
    return reset_at.strftime("%Y-%m-%d")


def _next_reset_after():
    now = _now()
    next_reset = now.replace(hour=_reset_hour(), minute=0, second=0, microsecond=0)
    if now >= next_reset:
        next_reset += timedelta(days=1)
    return max(0, int((next_reset - now).total_seconds()))


def _task_key(message):
    chat_id = getattr(getattr(message, "chat", None), "id", "pm")
    return f"{chat_id}:{message.id}"


def _is_bypass(user_id):
    return user_id == Config.OWNER_ID or user_id in sudo_users


def _quota_doc(user_id):
    quota = user_data.setdefault(user_id, {}).setdefault(QUOTA_KEY, _default_quota())
    for key, value in _default_quota().items():
        quota.setdefault(key, value)
    if not isinstance(quota.get("pending"), dict):
        quota["pending"] = {}
    return quota


def _limit():
    try:
        return max(0, int(Config.USER_QUOTA_DAILY_LIMIT))
    except Exception:
        return 20


def _timeout():
    try:
        return max(60, int(Config.USER_QUOTA_PENDING_TIMEOUT))
    except Exception:
        return 86400


def _reset_if_needed(quota):
    current_key = _reset_key()
    if quota.get("last_reset_key") != current_key:
        quota["used_today"] = 0
        quota["last_reset_key"] = current_key


def _clear_stale_pending(quota):
    now_ts = int(_now().timestamp())
    timeout = _timeout()
    quota["pending"] = {
        key: value
        for key, value in quota.get("pending", {}).items()
        if now_ts - int(value.get("created_at", now_ts)) < timeout
    }


def _remaining(quota):
    return _limit() + int(quota.get("extra_quota", 0)) - int(quota.get("used_today", 0)) - len(quota.get("pending", {}))


def _used_free(quota):
    return min(int(quota.get("used_today", 0)), _limit())


def _user_label(user_id, user=None):
    if user and hasattr(user, "mention"):
        return f"{user.mention(style='html')} (#ID{user_id})"
    if user:
        name = getattr(user, "first_name", None) or getattr(user, "title", None) or getattr(user, "username", None)
    else:
        name = user_data.get(user_id, {}).get("NAME")
    return f"{name or user_id} (#ID{user_id})"


def quota_summary(user_id):
    quota = _quota_doc(user_id)
    _reset_if_needed(quota)
    _clear_stale_pending(quota)
    limit = _limit()
    return {
        "daily_used": _used_free(quota),
        "daily_limit": limit,
        "pending": len(quota.get("pending", {})),
        "extra_quota": int(quota.get("extra_quota", 0)),
        "remaining": max(0, _remaining(quota)),
        "reset_after": get_readable_time(_next_reset_after()),
    }


def _usage_text(user_id, quota, exceeded=False, user=None):
    summary = quota_summary(user_id)
    if exceeded:
        us_cmd = f"/us{Config.CMD_SUFFIX}"
        return (
            "┠ <b><i>You've used up all your free mirror uses for today!</i></b>\n"
            f"┖ <b>Tip</b> → Use <code>{us_cmd}</code> → <b>Invite Friends</b> to invite people to Mirror Chat and earn more mirror uses."
        )
    return (
        "⌬ <b>User Quota :</b>\n"
        "│\n"
        f"┟ <b>Name</b> → {_user_label(user_id, user)}\n"
        f"┠ <b>Daily Free</b> → {summary['daily_limit'] - summary['daily_used']} / {summary['daily_limit']}\n"
        f"┠ <b>Pending Tasks</b> → {summary['pending']}\n"
        f"┠ <b>Extra Quota</b> → {summary['extra_quota']}\n"
        f"┖ <b>Reset After</b> → {summary['reset_after']}"
    )


async def save_user_quota(user_id):
    try:
        await database.update_user_data(user_id)
    except Exception as e:
        LOGGER.warning("User quota DB save failed for %s: %s", user_id, e)


async def quota_precheck(message, task_type="task", task_name=None):
    if not Config.USER_QUOTA_ENABLED:
        return None
    user = message.from_user or message.sender_chat
    user_id = user.id
    if _is_bypass(user_id):
        return None
    async with _locks[user_id]:
        quota = _quota_doc(user_id)
        _reset_if_needed(quota)
        _clear_stale_pending(quota)
        key = _task_key(message)
        if key not in quota["pending"] and _remaining(quota) <= 0:
            return _usage_text(user_id, quota, exceeded=True, user=user)
        quota["pending"][key] = {
            "task_type": task_type,
            "name": task_name or "",
            "created_at": int(_now().timestamp()),
        }
        await save_user_quota(user_id)
        LOGGER.info("User quota hold: user=%s task=%s type=%s", user_id, key, task_type)
    return None


async def quota_confirm_task(listener):
    user_id = getattr(listener, "user_id", None)
    message = getattr(listener, "message", None)
    if not user_id or not message or not Config.USER_QUOTA_ENABLED or _is_bypass(user_id):
        return
    async with _locks[user_id]:
        quota = _quota_doc(user_id)
        _reset_if_needed(quota)
        key = _task_key(message)
        if key not in quota.get("pending", {}):
            return
        quota["pending"].pop(key, None)
        if int(quota.get("used_today", 0)) < _limit():
            quota["used_today"] = int(quota.get("used_today", 0)) + 1
        else:
            quota["extra_quota"] = max(0, int(quota.get("extra_quota", 0)) - 1)
        quota["total_used"] = int(quota.get("total_used", 0)) + 1
        await save_user_quota(user_id)
        LOGGER.info("User quota confirm: user=%s task=%s used_today=%s", user_id, key, quota["used_today"])


async def quota_release_task(listener, reason=""):
    user_id = getattr(listener, "user_id", None)
    message = getattr(listener, "message", None)
    if not user_id or not message or _is_bypass(user_id):
        return
    async with _locks[user_id]:
        quota = _quota_doc(user_id)
        key = _task_key(message)
        if quota.get("pending", {}).pop(key, None) is not None:
            await save_user_quota(user_id)
            LOGGER.info("User quota release: user=%s task=%s reason=%s", user_id, key, reason)


async def quota_get_usage(user_id, user=None):
    async with _locks[user_id]:
        quota = _quota_doc(user_id)
        _reset_if_needed(quota)
        _clear_stale_pending(quota)
        await save_user_quota(user_id)
        return _usage_text(user_id, quota, user=user)


async def quota_add_extra(user_id, amount):
    async with _locks[user_id]:
        quota = _quota_doc(user_id)
        quota["extra_quota"] = max(0, int(quota.get("extra_quota", 0)) + int(amount))
        await save_user_quota(user_id)
        return _usage_text(user_id, quota)


async def quota_reset_today(user_id):
    async with _locks[user_id]:
        quota = _quota_doc(user_id)
        quota["used_today"] = 0
        quota["pending"] = {}
        quota["last_reset_key"] = _reset_key()
        await save_user_quota(user_id)
        return _usage_text(user_id, quota)


async def quota_clear_pending(user_id):
    async with _locks[user_id]:
        quota = _quota_doc(user_id)
        quota["pending"] = {}
        await save_user_quota(user_id)
        return _usage_text(user_id, quota)
