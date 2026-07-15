from datetime import datetime
from html import escape
from time import time
from zoneinfo import ZoneInfo

from pyrogram.errors import UserNotParticipant

from ... import LOGGER, sudo_users, user_data
from ...core.config_manager import Config
from ...core.tg_client import TgClient
from .db_handler import database
from .user_quota_manager import quota_add_extra_once, quota_summary

REFERRAL_KEY = "REFERRAL"
SUBSCRIBE_REWARD_KEY = "SUBSCRIBE_REWARD"


def _is_bypass(user_id):
    return user_id == Config.OWNER_ID or user_id in sudo_users


def _default_referral():
    return {
        "inviter_id": 0,
        "status": "none",
        "created_at": 0,
        "completed_at": 0,
        "reward": 0,
    }


def _default_subscribe_reward():
    return {
        "status": "none",
        "completed_at": 0,
        "reward": 0,
    }


def _ref_doc(user_id):
    ref = user_data.setdefault(user_id, {}).setdefault(REFERRAL_KEY, _default_referral())
    for key, value in _default_referral().items():
        ref.setdefault(key, value)
    return ref


def _subscribe_doc(user_id):
    reward = user_data.setdefault(user_id, {}).setdefault(
        SUBSCRIBE_REWARD_KEY, _default_subscribe_reward()
    )
    for key, value in _default_subscribe_reward().items():
        reward.setdefault(key, value)
    return reward


def referral_enabled():
    return bool(Config.REFERRAL_ENABLED and Config.REFERRAL_REQUIRED_CHAT_ID and Config.REFERRAL_REQUIRED_CHAT_LINK)


def subscribe_reward_enabled():
    return bool(Config.REFERRAL_SUBSCRIBE_ENABLED and Config.REFERRAL_SUBSCRIBE_CHANNEL_LINK)


def subscribe_force_enabled():
    return subscribe_reward_enabled()


def subscribe_reward_claimed(user_id):
    return _subscribe_doc(user_id).get("status") == "completed"


def referral_invite_link(user_id):
    username = getattr(TgClient.bot.me, "username", None) if TgClient.bot else None
    if not username:
        return ""
    return f"https://t.me/{username}?start=ref_{user_id}"


def referral_share_link(user_id):
    link = referral_invite_link(user_id)
    return f"https://t.me/share/url?url={link}&text=Join%20this%20mirror%20bot%20and%20mirror%20chat" if link else ""


def referral_stats(user_id):
    total = 0
    for data in user_data.values():
        ref = data.get(REFERRAL_KEY, {})
        if ref.get("inviter_id") == user_id and ref.get("status") == "completed":
            total += 1
    return total


def referral_pending_stats(user_id):
    total = 0
    for data in user_data.values():
        ref = data.get(REFERRAL_KEY, {})
        if ref.get("inviter_id") == user_id and ref.get("status") == "pending_join":
            total += 1
    return total


def subscribe_reward_total():
    return len(
        [
            1
            for data in user_data.values()
            if data.get(SUBSCRIBE_REWARD_KEY, {}).get("status") == "completed"
        ]
    )


def referral_usage_rows(page=0, page_size=5):
    rows = []
    for invitee_id, data in user_data.items():
        ref = data.get(REFERRAL_KEY, {})
        if ref.get("status") == "completed":
            rows.append(
                {
                    "invitee_id": invitee_id,
                    "inviter_id": ref.get("inviter_id", 0),
                    "reward": ref.get("reward", Config.REFERRAL_REWARD_QUOTA),
                    "completed_at": ref.get("completed_at", 0),
                }
            )
    rows.sort(key=lambda x: int(x.get("completed_at") or 0), reverse=True)
    start = max(0, int(page)) * page_size
    return rows[start : start + page_size], len(rows)


def subscribe_reward_rows(page=0, page_size=5):
    rows = []
    for user_id, data in user_data.items():
        reward = data.get(SUBSCRIBE_REWARD_KEY, {})
        if reward.get("status") == "completed":
            rows.append(
                {
                    "user_id": user_id,
                    "reward": reward.get("reward", Config.REFERRAL_SUBSCRIBE_REWARD_QUOTA),
                    "completed_at": reward.get("completed_at", 0),
                }
            )
    rows.sort(key=lambda x: int(x.get("completed_at") or 0), reverse=True)
    start = max(0, int(page)) * page_size
    return rows[start : start + page_size], len(rows)


def _usage_timezone():
    try:
        return ZoneInfo(Config.TIMEZONE)
    except Exception:
        return None


def _format_usage_time(timestamp):
    try:
        timestamp = int(timestamp or 0)
    except Exception:
        timestamp = 0
    if timestamp <= 0:
        return "Unknown time"
    tz = _usage_timezone()
    dt = datetime.fromtimestamp(timestamp, tz) if tz else datetime.fromtimestamp(timestamp)
    return dt.strftime("%Hh%Mm %d/%m/%Y")


async def _user_link(client, user_id, cache=None):
    try:
        user_id = int(user_id)
    except Exception:
        return "Unknown"
    if user_id <= 0:
        return "Unknown"
    cache = cache if cache is not None else {}
    if user_id not in cache:
        try:
            cache[user_id] = await client.get_users(user_id) if client else None
        except Exception:
            cache[user_id] = None
    user = cache.get(user_id)
    if user is not None and hasattr(user, "mention"):
        return user.mention(style="html")
    name = escape(str(user_id))
    return f'<a href="tg://user?id={user_id}">{name}</a>'


async def save_referral_user(user_id):
    try:
        await database.save_shared_quota(user_id, user_data.get(user_id, {}))
    except Exception as e:
        LOGGER.warning("Referral DB save failed for %s: %s", user_id, e)


async def set_pending_referral(invitee_id, inviter_id):
    if not referral_enabled() or invitee_id == inviter_id or _is_bypass(invitee_id) or _is_bypass(inviter_id):
        return False, "Referral is not eligible."
    invitee_ref = _ref_doc(invitee_id)
    if invitee_ref.get("status") == "completed":
        return False, "This user already completed a referral."
    if user_data.get(invitee_id, {}).get("USER_QUOTA", {}).get("total_used", 0):
        return False, "Only new users can complete referral rewards."
    invitee_ref.update(
        {
            "inviter_id": inviter_id,
            "status": "pending_join",
            "created_at": int(time()),
            "completed_at": 0,
            "reward": Config.REFERRAL_REWARD_QUOTA,
        }
    )
    await save_referral_user(invitee_id)
    return True, "Referral registered."


def _channel_username_from_link(link):
    link = str(link or "").strip().rstrip("/")
    if "t.me/" not in link:
        return link or ""
    username = link.rsplit("/", 1)[-1].split("?", 1)[0]
    if username and not username.startswith("+"):
        return f"@{username}" if not username.startswith("@") else username
    return ""


def is_valid_public_tme_link(link):
    link = str(link or "").strip().rstrip("/")
    if not link.startswith("https://t.me/"):
        return False
    username = link.rsplit("/", 1)[-1].split("?", 1)[0]
    return bool(username and not username.startswith("+") and username.replace("_", "").isalnum())


async def validate_public_tme_link(client, link):
    if not is_valid_public_tme_link(link):
        return False, "Link must be a public username link like https://t.me/channel_username"
    chat_ref = _channel_username_from_link(link)
    try:
        await client.get_chat(chat_ref)
        return True, ""
    except Exception as e:
        LOGGER.warning("Telegram link validation failed for %s: %s", link, e)
        return False, "Could not find or access this Telegram chat/channel. Make sure the bot can see it."



async def is_user_in_required_chat(client, user_id):
    async def _check(chat_ref):
        member = await client.get_chat_member(chat_ref, user_id)
        status = getattr(member.status, "name", str(member.status)).lower()
        return status in ("member", "administrator", "owner")

    chat_refs = [Config.REFERRAL_REQUIRED_CHAT_ID]
    username = _channel_username_from_link(Config.REFERRAL_REQUIRED_CHAT_LINK)
    if username:
        chat_refs.append(username)

    for chat_ref in chat_refs:
        try:
            return await _check(chat_ref)
        except UserNotParticipant:
            return False
        except Exception as e:
            LOGGER.warning("Referral chat member check failed for %s in %s: %s", user_id, chat_ref, e)
    return False


async def is_user_in_subscribe_channel(client, user_id):
    chat_ref = _channel_username_from_link(Config.REFERRAL_SUBSCRIBE_CHANNEL_LINK)
    if not chat_ref:
        return False, "Subscribe channel link must be a public t.me username link."
    try:
        member = await client.get_chat_member(chat_ref, user_id)
        status = getattr(member.status, "name", str(member.status)).lower()
        return status in ("member", "administrator", "owner"), ""
    except UserNotParticipant:
        return False, "Please join the channel first."
    except Exception as e:
        LOGGER.warning("Subscribe reward member check failed for %s in %s: %s", user_id, chat_ref, e)
        return False, "Could not verify channel membership."


async def complete_referral(client, invitee_id):
    if not referral_enabled():
        return False, "Referral Manager is disabled."
    ref = _ref_doc(invitee_id)
    inviter_id = int(ref.get("inviter_id") or 0)
    if ref.get("status") == "completed":
        return False, "Referral already completed."
    if ref.get("status") != "pending_join" or not inviter_id:
        return False, "No pending referral found."
    if _is_bypass(invitee_id) or _is_bypass(inviter_id) or invitee_id == inviter_id:
        return False, "Referral is not eligible."
    if not await is_user_in_required_chat(client, invitee_id):
        return False, "Please join Mirror Chat first."
    reward = int(ref.get("reward") or Config.REFERRAL_REWARD_QUOTA)
    granted, _ = await quota_add_extra_once(
        inviter_id, reward, f"referral:{invitee_id}"
    )
    if not granted:
        return False, "Referral reward was already granted."
    ref["status"] = "completed"
    ref["completed_at"] = int(time())
    ref["reward"] = reward
    await save_referral_user(invitee_id)
    return True, inviter_id


async def complete_subscribe_reward(client, user_id):
    if not subscribe_reward_enabled():
        return False, "Subscribe reward is disabled."
    reward_doc = _subscribe_doc(user_id)
    if reward_doc.get("status") == "completed":
        return False, "You have already claimed this reward."
    joined, note = await is_user_in_subscribe_channel(client, user_id)
    if not joined:
        return False, note or "Please join the channel first."
    reward = max(0, int(Config.REFERRAL_SUBSCRIBE_REWARD_QUOTA or 0))
    if reward <= 0:
        return False, "Subscribe reward quota is not configured."
    granted, _ = await quota_add_extra_once(
        user_id, reward, "subscribe_reward"
    )
    if not granted:
        return False, "You have already claimed this reward."
    reward_doc = _subscribe_doc(user_id)
    reward_doc["status"] = "completed"
    reward_doc["completed_at"] = int(time())
    reward_doc["reward"] = reward
    await save_referral_user(user_id)
    return True, reward


def referral_settings_text():
    status = "Enabled" if Config.REFERRAL_ENABLED else "Disabled"
    sub_status = "Enabled" if Config.REFERRAL_SUBSCRIBE_ENABLED else "Disabled"
    return f"""⌬ <b>Referral Manager :</b>
│
┟ <b>Referral Invite</b>
┃ ┠ <b>Status</b> → {status}
┃ ┠ <b>Reward Quota</b> → {Config.REFERRAL_REWARD_QUOTA}
┃ ┠ <b>Group Chat ID</b> → <code>{Config.REFERRAL_REQUIRED_CHAT_ID or 'Not Set'}</code>
┃ ┠ <b>Group Chat Link</b> → {Config.REFERRAL_REQUIRED_CHAT_LINK or 'Not Set'}
┃ ┖ <b>Total Success</b> → {len([1 for data in user_data.values() if data.get(REFERRAL_KEY, {}).get('status') == 'completed'])}
┃
┟ <b>Subscribe Channel Reward</b>
┃ ┠ <b>Status</b> → {sub_status}
┃ ┠ <b>Subscribe Quota</b> → {Config.REFERRAL_SUBSCRIBE_REWARD_QUOTA}
┃ ┠ <b>Channel Link</b> → {Config.REFERRAL_SUBSCRIBE_CHANNEL_LINK or 'Not Set'}
┖ ┖ <b>Total Claimed</b> → {subscribe_reward_total()}"""


async def referral_usage_text(client=None, page=0, page_size=5):
    rows, total = referral_usage_rows(page, page_size)
    max_page = max(0, (total - 1) // page_size) if total else 0
    msg = (
        "⌬ <b>Referral Usage :</b>\n│\n"
        f"┟ <b>Tab</b> → Referral Invite\n"
        f"┠ <b>Page</b> → {page + 1} / {max_page + 1}\n"
        f"┠ <b>Total Success</b> → {total}"
    )
    if not rows:
        return msg + "\n┖ <b>Data</b> → No referrals yet", total
    cache = {}
    msg += "\n┃"
    for idx, row in enumerate(rows, start=page * page_size + 1):
        inviter = await _user_link(client, row.get("inviter_id"), cache)
        invitee = await _user_link(client, row.get("invitee_id"), cache)
        reward = row.get("reward", Config.REFERRAL_REWARD_QUOTA)
        completed_at = _format_usage_time(row.get("completed_at"))
        msg += (
            f"\n┠ <b>{idx}.</b> {inviter} invited {invitee} "
            f"+{reward} Quota - {completed_at}"
        )
    return msg, total


async def subscribe_reward_usage_text(client=None, page=0, page_size=5):
    rows, total = subscribe_reward_rows(page, page_size)
    max_page = max(0, (total - 1) // page_size) if total else 0
    msg = (
        "⌬ <b>Referral Usage :</b>\n│\n"
        f"┟ <b>Tab</b> → Channel Reward\n"
        f"┠ <b>Page</b> → {page + 1} / {max_page + 1}\n"
        f"┠ <b>Total Claimed</b> → {total}"
    )
    if not rows:
        return msg + "\n┖ <b>Data</b> → No claims yet", total
    cache = {}
    msg += "\n┃"
    for idx, row in enumerate(rows, start=page * page_size + 1):
        user = await _user_link(client, row.get("user_id"), cache)
        reward = row.get("reward", Config.REFERRAL_SUBSCRIBE_REWARD_QUOTA)
        completed_at = _format_usage_time(row.get("completed_at"))
        msg += f"\n┠ <b>{idx}.</b> {user} claimed reward +{reward} Quota - {completed_at}"
    return msg, total


async def referral_notify_inviter(inviter_id, invitee_user, reward):
    summary = quota_summary(inviter_id)
    name = invitee_user.mention(style="html") if hasattr(invitee_user, "mention") else invitee_user.id
    text = f"""⌬ <b>Referral Reward :</b>
┃
┠ <b>New user</b> → {name} (#ID{invitee_user.id})
┠ <b>Reward</b> → +{reward} Extra Quota
┖ <b>Total Extra Quota</b> → {summary['extra_quota']}"""
    try:
        await TgClient.bot.send_message(inviter_id, text, disable_web_page_preview=True, disable_notification=True)
    except Exception as e:
        LOGGER.warning("Failed to notify referral inviter %s: %s", inviter_id, e)
