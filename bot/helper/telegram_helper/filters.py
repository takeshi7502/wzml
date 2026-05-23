from time import time
from asyncio import create_task

from pyrogram.filters import create
from pyrogram.enums import ChatType

from ... import auth_chats, sudo_users, user_data
from ...core.config_manager import Config
from ...helper.ext_utils.referral_manager import (
    complete_subscribe_reward,
    is_user_in_subscribe_channel,
    subscribe_force_enabled,
)
from ...helper.languages import Language
from .tg_utils import chat_info


class CustomFilters:
    async def owner_filter(self, _, update):
        user = update.from_user or update.sender_chat
        return user.id == Config.OWNER_ID

    owner = create(owner_filter)

    @staticmethod
    def _vip_authorized(uid):
        data = user_data.get(uid, {})
        expire_at = int(data.get("VIP_EXPIRE_AT") or 0)
        return bool(
            data.get("VIP_ENABLED")
            and int(data.get("VIP_DAILY_LIMIT") or 0) > 0
            and not data.get("VIP_AUTH_REVOKED", False)
            and (expire_at == 0 or expire_at > int(time()))
        )

    @staticmethod
    async def _enforce_subscribe_gate(client, update, uid):
        if not subscribe_force_enabled():
            return True
        if uid == Config.OWNER_ID or uid in sudo_users or user_data.get(uid, {}).get("SUDO", False):
            return True
        joined, _ = await is_user_in_subscribe_channel(client, uid)
        if joined:
            ok, reward = await complete_subscribe_reward(client, uid)
            if ok:
                try:
                    await client.send_message(
                        uid,
                        f"🎉 <b>Channel Reward Claimed!</b>\n┃\n┖ <b>Bonus</b> → +{reward} Extra Quota",
                    )
                except Exception:
                    pass
            return True
        from ...helper.telegram_helper.button_build import ButtonMaker
        from ...helper.telegram_helper.message_utils import auto_delete_message, send_message

        buttons = ButtonMaker()
        buttons.url_button("📢 Join Channel", Config.REFERRAL_SUBSCRIBE_CHANNEL_LINK)
        mention = "You"
        if update.from_user:
            name = update.from_user.first_name or "User"
            mention = f'<a href="tg://user?id={uid}">{name}</a>'
        lang = Language(user_id=uid)
        warn = await send_message(
            update,
            lang.SUBSCRIBE_REQUIRED_MSG.format(user=mention),
            buttons.build_menu(1),
        )
        create_task(auto_delete_message(update, warn, stime=30))
        return False

    async def authorized_user(self, client, update):
        uid = (update.from_user or update.sender_chat).id
        chat_id = update.chat.id
        thread_id = update.message_thread_id if update.is_topic_message else None
        is_authorized = bool(
            uid == Config.OWNER_ID
            or (
                uid in user_data
                and (
                    user_data[uid].get("AUTH", False)
                    or user_data[uid].get("SUDO", False)
                    or CustomFilters._vip_authorized(uid)
                )
            )
            or (
                chat_id in user_data
                and user_data[chat_id].get("AUTH", False)
                and (
                    thread_id is None
                    or thread_id in user_data[chat_id].get("thread_ids", [])
                )
            )
            or uid in sudo_users
            or uid in auth_chats
            or chat_id in auth_chats
            and (
                auth_chats[chat_id]
                and thread_id
                and thread_id in auth_chats[chat_id]
                or not auth_chats[chat_id]
            )
        )
        if not is_authorized:
            return False
        return await CustomFilters._enforce_subscribe_gate(client, update, uid)

    authorized = create(authorized_user)

    async def authorized_usetting(self, _, update):
        uid = (update.from_user or update.sender_chat).id
        is_exists = False
        if await CustomFilters.authorized("", update):
            is_exists = True
        elif update.chat.type == ChatType.PRIVATE:
            for channel_id in user_data:
                if not (
                    user_data[channel_id].get("is_auth")
                    and str(channel_id).startswith("-100")
                ):
                    continue
                try:
                    if await (await chat_info(str(channel_id))).get_member(uid):
                        is_exists = True
                        break
                except Exception:
                    continue
        return is_exists

    authorized_uset = create(authorized_usetting)

    async def sudo_user(self, _, update):
        user = update.from_user or update.sender_chat
        uid = user.id
        return bool(
            uid == Config.OWNER_ID
            or uid in user_data
            and user_data[uid].get("SUDO")
            or uid in sudo_users
        )

    sudo = create(sudo_user)
