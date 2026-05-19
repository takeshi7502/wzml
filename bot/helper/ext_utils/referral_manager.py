from time import time

from pyrogram.errors import UserNotParticipant

from ... import LOGGER, sudo_users, user_data
from ...core.config_manager import Config
from ...core.tg_client import TgClient
from .db_handler import database
from .user_quota_manager import quota_add_extra, quota_summary

REFERRAL_KEY = "REFERRAL"


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


def _ref_doc(user_id):
    ref = user_data.setdefault(user_id, {}).setdefault(REFERRAL_KEY, _default_referral())
    for key, value in _default_referral().items():
        ref.setdefault(key, value)
    return ref


def referral_enabled():
    return bool(Config.REFERRAL_ENABLED and Config.REFERRAL_REQUIRED_CHAT_ID and Config.REFERRAL_REQUIRED_CHAT_LINK)


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


async def save_referral_user(user_id):
    try:
        await database.update_user_data(user_id)
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


async def is_user_in_required_chat(client, user_id):
    async def _check(chat_ref):
        member = await client.get_chat_member(chat_ref, user_id)
        status = getattr(member.status, "name", str(member.status)).lower()
        return status in ("member", "administrator", "owner")

    chat_refs = [Config.REFERRAL_REQUIRED_CHAT_ID]
    link = str(Config.REFERRAL_REQUIRED_CHAT_LINK or "").strip().rstrip("/")
    if "t.me/" in link:
        username = link.rsplit("/", 1)[-1].split("?", 1)[0]
        if username and not username.startswith("+"):
            chat_refs.append(f"@{username}")

    for chat_ref in chat_refs:
        try:
            return await _check(chat_ref)
        except UserNotParticipant:
            return False
        except Exception as e:
            LOGGER.warning("Referral chat member check failed for %s in %s: %s", user_id, chat_ref, e)
    return False


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
    ref["status"] = "completed"
    ref["completed_at"] = int(time())
    ref["reward"] = reward
    await quota_add_extra(inviter_id, reward)
    await save_referral_user(invitee_id)
    return True, inviter_id


def referral_settings_text():
    status = "Enabled" if Config.REFERRAL_ENABLED else "Disabled"
    return f"""⌬ <b>Referral Manager :</b>
│
┟ <b>Status</b> → {status}
┠ <b>Reward Quota</b> → {Config.REFERRAL_REWARD_QUOTA}
┠ <b>Required Chat ID</b> → <code>{Config.REFERRAL_REQUIRED_CHAT_ID or 'Not Set'}</code>
┠ <b>Required Chat Link</b> → {Config.REFERRAL_REQUIRED_CHAT_LINK or 'Not Set'}
┖ <b>Total Success</b> → {len([1 for data in user_data.values() if data.get(REFERRAL_KEY, {}).get('status') == 'completed'])}"""


def referral_usage_text(page=0, page_size=5):
    rows, total = referral_usage_rows(page, page_size)
    max_page = max(0, (total - 1) // page_size) if total else 0
    msg = f"⌬ <b>Referral Usage :</b>\n│\n┟ <b>Page</b> → {page + 1} / {max_page + 1}\n┠ <b>Total Success</b> → {total}"
    if not rows:
        return msg + "\n┖ <b>Data</b> → No referrals yet", total
    msg += "\n┃"
    for idx, row in enumerate(rows, start=page * page_size + 1):
        msg += (
            f"\n┠ <b>{idx}.</b> Inviter <code>{row['inviter_id']}</code> → "
            f"User <code>{row['invitee_id']}</code> (+{row['reward']})"
        )
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
