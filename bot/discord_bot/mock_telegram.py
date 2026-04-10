"""
Mock Telegram objects for Discord bot parasite mode.
These classes implement the same interface (duck-typing) as pyrogram's
Message, User, and Chat objects so the WZML core can process them
without knowing they originated from Discord.
"""

import re
import html as html_module
from time import time
from datetime import datetime, timezone

import discord

from .. import LOGGER


class MockChat:
    """Mimics pyrogram.types.Chat for the WZML core."""

    class _ChatType:
        name = "SUPERGROUP"
        BOT = "BOT"

    def __init__(self, channel: discord.TextChannel):
        self.id = channel.id
        self.title = channel.name if hasattr(channel, "name") else "Discord"
        self.type = self._ChatType()


class MockUser:
    """Mimics pyrogram.types.User for the WZML core."""

    def __init__(self, user: discord.User | discord.Member):
        self.id = user.id
        self.username = str(user)
        self.first_name = user.display_name
        self.last_name = ""
        self.title = user.display_name
        self.is_bot = user.bot

    def mention(self, name=None, style="html"):
        """Callable mention that mimics pyrogram's User.mention(style='html')."""
        display = name or self.first_name
        return f"<b>{html_module.escape(display)}</b>"

    def mention_html(self, name=None):
        return self.mention(name=name)


# ─── HTML → Discord conversion ─────────────────────────────────────


def _html_to_discord(text: str) -> str:
    """Convert Telegram HTML formatting to Discord markdown."""
    if not text:
        return ""
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<u>(.*?)</u>", r"__\1__", text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)
    text = re.sub(r"<a\s+href=['\"]([^'\"]+)['\"]>(.*?)</a>", r"[\2](\1)", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    return text


def _extract_task_by(html_text: str) -> tuple[str | None, str]:
    """Extract Task By info from HTML and return (task_by_field_value, cleaned_text).
    Uses two-step extraction: find #ID, then find Link URL separately.
    """
    # Step 1: Find #ID
    id_match = re.search(r'#ID(\d+)', html_text)
    if not id_match or "Task By" not in html_text:
        return None, html_text

    uid = id_match.group(1)

    # Step 2: Find Link URL near the Task By section
    link_match = re.search(r"<a\s+href=['\"]([^'\"]+)['\"]>Link</a>", html_text)
    link_url = link_match.group(1) if link_match else None

    # Step 3: Remove entire Task By block from HTML
    # Matches from <b>Task By... through #IDxxx) and optional [Link]</i>
    cleaned = re.sub(
        r'\n*\s*(?:<b>\s*)?Task By.*?#ID\d+\s*\).*?(?:</i>|(?=\n)|$)',
        '', html_text, count=1, flags=re.DOTALL
    )
    # Clean leftover empty tags
    cleaned = re.sub(r'<b>\s*</b>', '', cleaned)
    cleaned = re.sub(r'<i>\s*</i>', '', cleaned)

    # Build Discord mention
    task_by = f"<@{uid}>"
    if link_url:
        task_by += f" **[Source Link]**({link_url})"

    return task_by, cleaned


# ─── Embed builders ─────────────────────────────────────────────


def _parse_status_to_embed(text: str, gid: str = None, uid: int | None = None, link_url: str | None = None) -> discord.Embed:
    """Parse WZML status HTML into a Discord Embed."""
    # Remove Bot Stats section entirely
    text = re.sub(r"(?:<b>)?\s*⬡?\s*Bot Stats(?:</b>)?.*", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Remove /cancel command lines
    text = re.sub(r"[┖┗]\s*Stop\s*[→➔].*", "", text)

    # Extract Task By before HTML conversion
    extracted_task_by, text = _extract_task_by(text)
    
    task_by_value = None
    if uid:
        task_by_value = f"<@{uid}>"
        if link_url:
            task_by_value += f" **[Source Link]**({link_url})"
    elif extracted_task_by:
        task_by_value = extracted_task_by

    cleaned = _html_to_discord(text).strip()

    embed = discord.Embed(color=0x5865F2)

    lines = cleaned.split("\n")
    task_name = ""
    fields = []

    # Fields to skip in Discord
    skip_fields = {"In Mode", "Out Mode"}

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Task name (numbered item: **1.** filename)
        if re.match(r"^\*\*\d+\.\*\*", line):
            task_name = re.sub(r"^\*\*\d+\.\*\*\s*", "", line).strip("* ")
            continue

        # Skip leftover "Task By" text that wasn't caught by _extract_task_by
        if "Task By" in line:
            continue

        # Field lines: ┠ **Speed** → *value*
        field_match = re.match(r"[┟┠┖┗├└│┃|]+\s*\*?\*?(.+?)\*?\*?\s*→\s*(.*)", line)
        if field_match:
            fname = field_match.group(1).strip().strip("*")
            fvalue = field_match.group(2).strip().strip("*") or "—"
            if fname in skip_fields:
                continue
            if fname and fvalue:
                fields.append((fname, fvalue))
            continue

        # Progress bar
        if "⬢" in line or "⬡" in line:
            fields.append(("Progress", line))
            continue

    if task_name:
        if len(task_name) > 256:
            task_name = task_name[:253] + "..."
        embed.title = task_name

    for fname, fvalue in fields:
        if len(fvalue) > 1024:
            fvalue = fvalue[:1021] + "..."
        embed.add_field(name=fname, value=fvalue, inline=True)

    if not fields and not task_name:
        if len(cleaned) > 4096:
            cleaned = cleaned[:4093] + "..."
        embed.description = cleaned or "Processing..."

    # Add Task By to the end of the description
    if task_by_value:
        current_desc = embed.description or ""
        embed.description = f"{current_desc}\n\n**Task By** {task_by_value}".strip()

    embed.timestamp = datetime.now(timezone.utc)
    return embed


def _parse_completion_embed(text: str, uid: int | None = None, link_url: str | None = None) -> tuple[discord.Embed, bool]:
    """Parse task completion/error HTML into a Discord Embed.
    Returns (embed, is_task_complete) — is_task_complete=True triggers DM.
    """
    # Determine embed type
    is_task_complete = False
    if "already available" in text.lower():
        color = 0xFEE75C
        title = "⚠️ Duplicate Found"
    elif "Download Stopped" in text or "Cancelled" in text:
        color = 0xED4245
        title = "🛑 Task Cancelled"
    elif "error" in text.lower() or "failed" in text.lower() or "Limit Breached" in text:
        color = 0xED4245
        title = "❌ Task Failed"
    elif "Task Done" in text or "Task Size" in text:
        color = 0x57F287
        title = "✅ Task Complete"
        is_task_complete = True
    else:
        color = 0x5865F2
        title = "📋 Task Update"

    # Extract Task By before conversion (for cleanup primarily)
    # Using the regex to remove it, but we prefer passed-in uid and link_url
    extracted_task_by, text = _extract_task_by(text)
    
    task_by_value = None
    if uid:
        task_by_value = f"<@{uid}>"
        if link_url:
            task_by_value += f" **[Source Link]**({link_url})"
    elif extracted_task_by:
        task_by_value = extracted_task_by

    # Extract "Action Performed" section from HTML before conversion
    action_text = None
    action_match = re.search(r"〶.*?Action Performed.*?(?=┠|┖|┗|<b>Task|$)", text, re.DOTALL)
    if action_match:
        raw_action = action_match.group(0)
        text = text[:action_match.start()] + text[action_match.end():]
        action_clean = _html_to_discord(raw_action).strip()
        # Remove the header and box chars, keep only the content
        action_clean = re.sub(r"[┟┠┖┗├└│┃⋗]+\s*", "", action_clean)
        # Strip the Action Performed header regardless of markdown
        action_clean = re.sub(r"〶?\s*[*_]*Action\s*Performed\s*:?[*_]*\s*", "", action_clean, flags=re.IGNORECASE).strip()
        if action_clean:
            action_text = action_clean

    # Extract "Download Stopped" / "Here are N list results" for duplicate/cancelled
    note_text = None
    stop_match = re.search(r"(🔴\s*)?Download Stopped!?", text)
    list_match = re.search(r"Here are \d+ list results?:?", text)
    if stop_match or list_match:
        note_parts = []
        if stop_match:
            note_parts.append("🔴 Download Stopped")
            text = text[:stop_match.start()] + text[stop_match.end():]
        if list_match:
            # Re-search after possible text modification
            list_match2 = re.search(r"Here are \d+ list results?:?", text)
            if list_match2:
                note_parts.append(list_match2.group(0).rstrip(":"))
                text = text[:list_match2.start()] + text[list_match2.end():]
        if note_parts:
            note_text = "\n".join(note_parts)

    cleaned = _html_to_discord(text).strip()

    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))

    # Fields to skip
    skip_fields = {"In Mode", "Out Mode", "Action", "Action Performed"}

    lines = cleaned.split("\n")
    desc_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "Task By" in line:
            continue
        if re.match(r"^[┟┠┖┗├└│┃|⋗\s]*$", line):
            continue
        field_match = re.match(r"[┟┠┖┗├└│┃|]+\s*\*?\*?(.+?)\*?\*?\s*→\s*(.*)", line)
        if field_match:
            fname = field_match.group(1).strip().strip("*")
            fvalue = field_match.group(2).strip().strip("*") or "—"
            if fname in skip_fields:
                continue
            if fname and fvalue:
                embed.add_field(name=fname, value=fvalue, inline=True)
        else:
            cleaned_line = re.sub(r"^[│┃|]+\s*", "", line).strip()
            if cleaned_line:
                desc_lines.append(cleaned_line)

    if desc_lines:
        desc = "\n".join(desc_lines)
        if len(desc) > 4096:
            desc = desc[:4093] + "..."
        embed.description = desc

    # Add Task By to the description (one line)
    if task_by_value:
        current_desc = embed.description or ""
        embed.description = f"{current_desc}\n\n**Task By** {task_by_value}".strip()

    # Note (Download Stopped / list results) below description
    if note_text:
        embed.add_field(name="Note", value=note_text, inline=False)

    # Action Performed below Task By
    if action_text:
        embed.add_field(name="〶 Action Performed", value=action_text, inline=False)

    return embed, is_task_complete


# ─── Stop Button ─────────────────────────────────────────────────


class StopButtonView(discord.ui.View):
    """A View with a Stop button for cancelling tasks."""

    def __init__(self, gid: str, timeout_sec: float = 86400):
        super().__init__(timeout=timeout_sec)
        self.gid = gid
        self.cancelled = False
        stop_btn = discord.ui.Button(
            label="Stop 🛑",
            style=discord.ButtonStyle.danger,
            custom_id=f"stop_{gid}",
        )
        stop_btn.callback = self._stop_callback
        self.add_item(stop_btn)

    async def _stop_callback(self, interaction: discord.Interaction):
        from ..helper.ext_utils.status_utils import get_task_by_gid
        from ..core.config_manager import Config
        try:
            task = await get_task_by_gid(self.gid)
            if task is None:
                await interaction.response.send_message(
                    "Task not found or already completed!", ephemeral=True
                )
                return
            
            # Authorization check: only task owner or bot admin can stop
            user_id = interaction.user.id
            task_owner_id = getattr(task.listener.message.from_user, "id", None)
            
            if user_id != task_owner_id and user_id != Config.DISCORD_ADMIN_ID:
                await interaction.response.send_message(
                    "⛔ Bạn không có quyền hủy Task do người khác tạo!", ephemeral=True
                )
                return

            obj = task.task()
            await obj.cancel_task()
            self.cancelled = True
            for item in self.children:
                item.disabled = True
                item.label = "Stopped ✓"
                item.style = discord.ButtonStyle.secondary
            await interaction.response.edit_message(view=self)
        except Exception as e:
            LOGGER.error(f"Discord stop button error: {e}")
            try:
                await interaction.response.send_message(
                    f"Error: {e}", ephemeral=True
                )
            except Exception:
                pass


# ─── MockMessage ─────────────────────────────────────────────────


class MockMessage:
    """Mimics pyrogram.types.Message for the WZML core.

    Heart of the parasite architecture. All message operations
    (reply, edit, delete) are routed to Discord instead of Telegram.
    Uses a SINGLE Discord message for the entire task lifecycle.
    """

    is_mock = True

    def __init__(
        self,
        channel: discord.TextChannel,
        user: discord.User | discord.Member,
        text: str = "",
        interaction: discord.Interaction = None,
    ):
        self._channel = channel
        self._discord_user = user
        self._interaction = interaction
        self._discord_msg: discord.Message | None = None
        self._gid: str | None = None
        self._view: StopButtonView | None = None
        self._protect_from_delete = False  # If True, delete() is a no-op

        # Pyrogram-compatible attributes
        self.id = int(f"{channel.id}{int(time() * 1000) % 10**8}")
        self.text = text
        self.from_user = MockUser(user)
        self.sender_chat = None
        self.chat = MockChat(channel)
        self.date = datetime.now(timezone.utc)
        self.link = (
            f"https://discord.com/channels/{channel.guild.id}/{channel.id}"
            if hasattr(channel, "guild") and channel.guild
            else ""
        )
        self.reply_to_message = None
        self.reply_to_message_id = None
        self.is_topic_message = False
        self.message_thread_id = None

        # Media (always None for Discord)
        self.document = None
        self.photo = None
        self.video = None
        self.audio = None
        self.voice = None
        self.video_note = None
        self.sticker = None
        self.animation = None
        self.caption = None
        self.empty = False

    def set_gid(self, gid: str):
        self._gid = gid

    async def reply(self, text, quote=True, disable_web_page_preview=True,
                    disable_notification=True, reply_markup=None, **kwargs):
        """Reply — if _discord_msg exists, EDIT it (single-message mode).
        Otherwise send a new message. Auto-DMs completion to user."""
        try:
            uid = self._discord_user.id if self._discord_user else None
            embed, is_task_complete = _parse_completion_embed(text, uid, self.link)

            # Build view from reply_markup (URL buttons)
            view = None
            if reply_markup and hasattr(reply_markup, "inline_keyboard"):
                view = discord.ui.View(timeout=None)
                for row in reply_markup.inline_keyboard:
                    for btn in row:
                        if hasattr(btn, "url") and btn.url:
                            view.add_item(discord.ui.Button(
                                label=btn.text,
                                url=btn.url,
                                style=discord.ButtonStyle.link,
                            ))

            if self._discord_msg:
                # EDIT the existing message (single-message lifecycle)
                await self._discord_msg.edit(embed=embed, view=view)
                clone = MockMessage(self._channel, self._discord_user, text)
                clone._discord_msg = self._discord_msg
                clone._protect_from_delete = True
                clone.id = self._discord_msg.id
                clone.link = self.link
                clone.text = text
            else:
                # No existing message — send new
                msg = await self._channel.send(embed=embed, view=view)
                clone = MockMessage(self._channel, self._discord_user, text)
                clone._discord_msg = msg
                clone.id = msg.id
                clone.link = self.link
                clone.text = text

            # Auto-DM completion embed to user
            if is_task_complete:
                try:
                    dm_embed = embed.copy()
                    dm_embed.set_footer(text=f"From: {self._channel.guild.name}" if hasattr(self._channel, 'guild') and self._channel.guild else "")
                    await self._discord_user.send(embed=dm_embed, view=view)
                except discord.Forbidden:
                    LOGGER.warning(f"Cannot DM user {self._discord_user} — DMs disabled")
                except Exception as e:
                    LOGGER.error(f"Discord DM error: {e}")

            return clone
        except Exception as e:
            LOGGER.error(f"Discord reply error: {e}")
            return str(e)

    async def reply_photo(self, photo, reply_to_message_id=None, caption="",
                          quote=True, reply_markup=None, disable_notification=True, **kwargs):
        return await self.reply(caption or "Photo", reply_markup=reply_markup)

    async def reply_document(self, document, quote=True, caption="",
                             disable_notification=True, reply_markup=None, **kwargs):
        return await self.reply(caption or "Document", reply_markup=reply_markup)

    async def edit(self, text, disable_web_page_preview=True, reply_markup=None):
        """Edit the Discord message with updated status."""
        if self._discord_msg is None:
            return
        try:
            # Extract GID for stop button
            gid_match = re.search(r"/c(?:ancel)?_?ask_?(\w+)", text)
            if not gid_match:
                gid_match = re.search(r"(?:Stop|stop).*?[→➔].*?/\w+_(\w+)", text)
            if gid_match and not self._gid:
                self._gid = gid_match.group(1)

            uid = self._discord_user.id if self._discord_user else None
            embed = _parse_status_to_embed(text, self._gid, uid, self.link)

            # Stop button
            view = None
            if self._gid:
                if self._view and not self._view.cancelled:
                    view = self._view
                else:
                    self._view = StopButtonView(self._gid)
                    view = self._view

            await self._discord_msg.edit(embed=embed, view=view)
        except discord.NotFound:
            LOGGER.warning("Discord message not found for edit")
        except Exception as e:
            if "rate" not in str(e).lower():
                LOGGER.error(f"Discord edit error: {e}")
            return str(e)

    async def delete(self):
        """Delete the Discord message. No-op if _protect_from_delete."""
        if self._protect_from_delete or not self._discord_msg:
            return
        try:
            await self._discord_msg.delete()
        except discord.NotFound:
            pass
        except Exception as e:
            LOGGER.error(f"Discord delete error: {e}")

    async def unpin(self):
        pass

    async def download(self):
        return None
