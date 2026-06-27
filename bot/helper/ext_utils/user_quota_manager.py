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


def _base_limit():
    try:
        return max(0, int(Config.USER_QUOTA_DAILY_LIMIT))
    except Exception:
        return 20


def _format_date(ts):
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(int(ts), tz=ZoneInfo(Config.TIMEZONE)).strftime("%d/%m/%Y")


def _vip_state(user_id):
    data = user_data.setdefault(user_id, {})
    now_ts = int(_now().timestamp())
    enabled = bool(data.get("VIP_ENABLED"))
    limit = int(data.get("VIP_DAILY_LIMIT") or 0)
    start_at = int(data.get("VIP_START_AT") or 0)
    expire_at = int(data.get("VIP_EXPIRE_AT") or 0)
    expired = enabled and expire_at > 0 and expire_at <= now_ts
    if expired:
        data["VIP_ENABLED"] = False
        enabled = False
    active = enabled and limit > 0 and (expire_at == 0 or expire_at > now_ts)
    if active:
        if expire_at == 0:
            expires = f"Forever (Start: {_format_date(start_at)})"
        else:
            expires = f"{get_readable_time(expire_at - now_ts)} (Start: {_format_date(start_at)})"
    else:
        expires = "N/A"
    return {
        "enabled": enabled,
        "active": active,
        "expired": expired,
        "limit": limit,
        "start_at": start_at,
        "expire_at": expire_at,
        "expires": expires,
    }


def _limit(user_id=None):
    if user_id is not None:
        vip = _vip_state(user_id)
        if vip["active"]:
            return vip["limit"]
    return _base_limit()


def _reset_if_needed(quota):
    current_key = _reset_key()
    if quota.get("last_reset_key") != current_key:
        quota["used_today"] = 0
        quota["last_reset_key"] = current_key


def _remaining(quota, user_id=None):
    return _limit(user_id) + int(quota.get("extra_quota", 0)) - int(quota.get("used_today", 0)) - len(quota.get("pending", {}))


def _used_free(quota, user_id=None):
    return min(int(quota.get("used_today", 0)), _limit(user_id))


def _user_label(user_id, user=None):
    if user and hasattr(user, "mention"):
        return f"{user.mention(style='html')} (#ID{user_id})"
    if user:
        name = getattr(user, "first_name", None) or getattr(user, "title", None) or getattr(user, "username", None)
    else:
        name = user_data.get(user_id, {}).get("NAME")
    name = name or user_id
    return f"<a href='tg://user?id={user_id}'>{name}</a> (#ID{user_id})"


def quota_summary(user_id):
    quota = _quota_doc(user_id)
    _reset_if_needed(quota)
    limit = _limit(user_id)
    vip = _vip_state(user_id)
    return {
        "daily_used": _used_free(quota, user_id),
        "daily_limit": limit,
        "pending": len(quota.get("pending", {})),
        "extra_quota": int(quota.get("extra_quota", 0)),
        "remaining": max(0, _remaining(quota, user_id)),
        "reset_after": get_readable_time(_next_reset_after()),
        "vip": vip,
    }


def _upgrade_text():
    admin_link = f"tg://user?id={Config.OWNER_ID}"
    return f"<b><i>Need more quota? Contact <a href=\"{admin_link}\"><b><u>ADMIN</u></b></a> for a VIP upgrade.</i></b>"


def _usage_text(user_id, quota, exceeded=False, user=None, show_upgrade=True):
    summary = quota_summary(user_id)
    if exceeded:
        us_cmd = f"/us{Config.CMD_SUFFIX}"
        admin_link = f"tg://user?id={Config.OWNER_ID}"
        referral_tip = (
            f"┠ <b>Tip:</b> Use <code>{us_cmd}</code> → <b><u>Invite Friends</u></b> to more mirror uses free.\n"
            if Config.REFERRAL_ENABLED
            else ""
        )
        return (
            "┠ <b><i>You've used up all your free mirror uses for today! ⚠️</i></b>\n"
            f"{referral_tip}"
            f"┖ <b>Buy VIP for higher daily quota. DM now → </b><a href=\"{admin_link}\"><b><u>ADMIN</u></b></a><b>.</b>"
        )
    vip = summary["vip"]
    if vip["active"]:
        vip_text = f"┠ <b>VIP Status</b> → Active\n┖ <b>VIP Expires</b> → {vip['expires']}"
    elif vip.get("enabled"):
        vip_text = "┖ <b>VIP Status</b> → Inactive"
    elif show_upgrade:
        vip_text = f"┠ <b>VIP Status</b> → Inactive\n┖ {_upgrade_text()}"
    else:
        vip_text = "┖ <b>VIP Status</b> → Inactive"
    return (
        "⌬ <b>User Quota :</b>\n"
        "│\n"
        f"┟ <b>Name</b> → {_user_label(user_id, user)}\n"
        f"┠ <b>Daily Free</b> → {summary['daily_limit'] - summary['daily_used']} / {summary['daily_limit']}\n"
        f"┠ <b>Pending Tasks</b> → {summary['pending']}\n"
        f"┠ <b>Extra Quota</b> → {summary['extra_quota']}\n"
        f"┠ <b>Reset After</b> → {summary['reset_after']}\n"
        f"{vip_text}"
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
        key = _task_key(message)
        if key not in quota["pending"] and _remaining(quota, user_id) <= 0:
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
        if int(quota.get("used_today", 0)) < _limit(user_id):
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


async def quota_get_usage(user_id, user=None, show_upgrade=True):
    async with _locks[user_id]:
        quota = _quota_doc(user_id)
        _reset_if_needed(quota)
        return _usage_text(user_id, quota, user=user, show_upgrade=show_upgrade)


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


async def quota_set_vip_limit(user_id, limit):
    limit = int(limit)
    base_limit = _base_limit()
    if limit <= base_limit:
        return False, f"VIP daily limit must be greater than current Daily Free ({base_limit}/day)."
    async with _locks[user_id]:
        data = user_data.setdefault(user_id, {})
        data["VIP_DAILY_LIMIT"] = limit
        if not data.get("VIP_START_AT"):
            data["VIP_START_AT"] = int(_now().timestamp())
        await save_user_quota(user_id)
        return True, _usage_text(user_id, _quota_doc(user_id))


async def quota_set_vip_days(user_id, days):
    async with _locks[user_id]:
        data = user_data.setdefault(user_id, {})
        days = int(days)
        now_ts = int(_now().timestamp())
        if not data.get("VIP_START_AT"):
            data["VIP_START_AT"] = now_ts
        data["VIP_EXPIRE_AT"] = 0 if days == 0 else now_ts + days * 86400
        await save_user_quota(user_id)
        return _usage_text(user_id, _quota_doc(user_id))


async def quota_enable_vip(user_id):
    async with _locks[user_id]:
        data = user_data.setdefault(user_id, {})
        limit = int(data.get("VIP_DAILY_LIMIT") or 0)
        expire_at = data.get("VIP_EXPIRE_AT")
        if limit <= 0 or expire_at is None:
            return False, "VIP limit and VIP days must be set before enabling VIP."
        if not data.get("VIP_START_AT"):
            data["VIP_START_AT"] = int(_now().timestamp())
        data["VIP_ENABLED"] = True
        await save_user_quota(user_id)
        return True, _usage_text(user_id, _quota_doc(user_id))


async def quota_disable_vip(user_id):
    async with _locks[user_id]:
        user_data.setdefault(user_id, {})["VIP_ENABLED"] = False
        await save_user_quota(user_id)
        return _usage_text(user_id, _quota_doc(user_id))
