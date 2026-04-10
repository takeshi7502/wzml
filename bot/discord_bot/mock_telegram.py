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
        """Callable mention that mimics pyrogram's User.mention(style='html').
        Also works as attribute access since get_tag checks username first."""
        display = name or self.first_name
        return f"<b>{html_module.escape(display)}</b>"

    def mention_html(self, name=None):
        return self.mention(name=name)


def _html_to_discord(text: str) -> str:
    """Convert Telegram HTML formatting to Discord-friendly markdown/plain text."""
    if not text:
        return ""
    # Bold
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)
    # Italic
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)
    # Underline
    text = re.sub(r"<u>(.*?)</u>", r"__\1__", text, flags=re.DOTALL)
    # Code
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    # Pre
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)
    # Links: <a href='URL'>text</a> -> [text](URL)
    text = re.sub(r"<a\s+href=['\"]([^'\"]+)['\"]>(.*?)</a>", r"[\2](\1)", text, flags=re.DOTALL)
    # Remove remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape HTML entities
    text = html_module.unescape(text)
    return text


def _parse_status_to_embed(text: str, gid: str = None) -> discord.Embed:
    """Parse the WZML status HTML string into a beautiful Discord Embed.
    Strips Bot Stats section and extracts task info into embed fields.
    """
    # Remove Bot Stats section
    stats_marker = "⌬"
    if stats_marker in text:
        text = text[:text.index(stats_marker)]

    # Remove the /cancel command lines — we'll use a button instead
    text = re.sub(r"[┖┗]\s*Stop\s*→.*", "", text)

    cleaned = _html_to_discord(text).strip()

    embed = discord.Embed(
        color=0x5865F2,  # Discord blurple
    )

    # Try to extract task info as fields for cleaner UI
    lines = cleaned.split("\n")
    task_name = ""
    task_by = ""
    fields = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Task name (usually first bold item with number)
        if re.match(r"^\*\*\d+\.\*\*", line):
            task_name = re.sub(r"^\*\*\d+\.\*\*\s*", "", line).strip("* ")
            continue

        # Task By line
        if "Task By" in line:
            task_by = line.replace("**Task By", "").replace("**", "").strip()
            continue

        # Parse field lines like: ┠ **Speed** → *value*
        field_match = re.match(r"[┟┠┖┗├└│┃|]+\s*\*?\*?(.+?)\*?\*?\s*→\s*(.*)", line)
        if field_match:
            fname = field_match.group(1).strip().strip("*")
            fvalue = field_match.group(2).strip().strip("*") or "—"
            # Skip empty or redundant
            if fname and fvalue:
                fields.append((fname, fvalue))
            continue

        # Progress bar line
        if "[⬢" in line or "[⬡" in line:
            fields.append(("Progress", line))
            continue

    if task_name:
        # Truncate task name if too long for embed title
        if len(task_name) > 256:
            task_name = task_name[:253] + "..."
        embed.title = task_name

    if task_by:
        embed.set_footer(text=f"Task By {task_by}")

    for fname, fvalue in fields:
        # Discord field value max 1024
        if len(fvalue) > 1024:
            fvalue = fvalue[:1021] + "..."
        embed.add_field(name=fname, value=fvalue, inline=True)

    if not fields and not task_name:
        # Fallback — just put everything in description
        if len(cleaned) > 4096:
            cleaned = cleaned[:4093] + "..."
        embed.description = cleaned or "Processing..."

    embed.timestamp = datetime.now(timezone.utc)
    return embed


def _parse_completion_embed(text: str) -> discord.Embed:
    """Parse task completion/error HTML into a Discord Embed."""
    cleaned = _html_to_discord(text).strip()

    if "Download Stopped" in text or "Limit Breached" in text:
        color = 0xED4245  # Red
        title = "❌ Task Failed"
    elif "Task Done" in text or "Task Size" in text:
        color = 0x57F287  # Green
        title = "✅ Task Complete"
    else:
        color = 0x5865F2  # Blue
        title = "📋 Task Update"

    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # Extract fields
    lines = cleaned.split("\n")
    desc_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        field_match = re.match(r"[┟┠┖┗├└│┃|]+\s*\*?\*?(.+?)\*?\*?\s*→\s*(.*)", line)
        if field_match:
            fname = field_match.group(1).strip().strip("*")
            fvalue = field_match.group(2).strip().strip("*") or "—"
            if fname and fvalue:
                embed.add_field(name=fname, value=fvalue, inline=True)
        else:
            desc_lines.append(line)

    if desc_lines:
        desc = "\n".join(desc_lines)
        if len(desc) > 4096:
            desc = desc[:4093] + "..."
        embed.description = desc

    return embed


class StopButtonView(discord.ui.View):
    """A View with a Stop button for cancelling tasks."""

    def __init__(self, gid: str, timeout_sec: float = 86400):
        super().__init__(timeout=timeout_sec)
        self.gid = gid
        self.cancelled = False

    @discord.ui.button(label="Stop 🛑", style=discord.ButtonStyle.danger, custom_id="discord_stop_btn")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        from ..helper.ext_utils.status_utils import get_task_by_gid
        task = await get_task_by_gid(self.gid)
        if task is None:
            await interaction.response.send_message("Task not found or already completed!", ephemeral=True)
            return
        obj = task.task()
        await obj.cancel_task()
        self.cancelled = True
        button.disabled = True
        button.label = "Stopped ✓"
        button.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)


class MockMessage:
    """Mimics pyrogram.types.Message for the WZML core.
    
    This is the heart of the parasite architecture. When the core calls
    message.reply(), message.edit(), message.delete(), etc., these methods
    interact with Discord instead of Telegram.
    """

    is_mock = True  # Flag for duck-type checks in core

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
        self._discord_msg: discord.Message | None = None  # The sent Discord message
        self._gid: str | None = None  # GID for stop button
        self._view: StopButtonView | None = None

        # Pyrogram-compatible properties
        self.id = int(f"{channel.id}{int(time() * 1000) % 10**8}")  # Unique-ish ID
        self.text = text
        self.from_user = MockUser(user)
        self.sender_chat = None
        self.chat = MockChat(channel)
        self.date = datetime.now(timezone.utc)
        self.link = f"https://discord.com/channels/{channel.guild.id}/{channel.id}" if hasattr(channel, 'guild') and channel.guild else ""
        self.reply_to_message = None
        self.reply_to_message_id = None
        self.is_topic_message = False
        self.message_thread_id = None

        # Media attributes (always None for Discord commands)    
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
        """Set the GID for the stop button."""
        self._gid = gid

    async def reply(self, text, quote=True, disable_web_page_preview=True,
                    disable_notification=True, reply_markup=None, **kwargs):
        """Send a reply in Discord channel, returning self for chaining."""
        try:
            embed = _parse_completion_embed(text)
            
            # Extract URL buttons from reply_markup if present
            view = None
            if reply_markup and hasattr(reply_markup, 'inline_keyboard'):
                view = discord.ui.View(timeout=None)
                for row in reply_markup.inline_keyboard:
                    for btn in row:
                        if hasattr(btn, 'url') and btn.url:
                            view.add_item(discord.ui.Button(
                                label=btn.text,
                                url=btn.url,
                                style=discord.ButtonStyle.link,
                            ))

            msg = await self._channel.send(embed=embed, view=view)
            # Return a new MockMessage representing the reply
            reply_mock = MockMessage(self._channel, self._discord_user, text)
            reply_mock._discord_msg = msg
            reply_mock.id = msg.id
            reply_mock.text = text
            return reply_mock
        except Exception as e:
            LOGGER.error(f"Discord reply error: {e}")
            return str(e)

    async def reply_photo(self, photo, reply_to_message_id=None, caption="",
                          quote=True, reply_markup=None, disable_notification=True, **kwargs):
        """Handle photo replies — just send text since we don't need photos on Discord."""
        return await self.reply(caption or "Photo", reply_markup=reply_markup)

    async def reply_document(self, document, quote=True, caption="",
                             disable_notification=True, reply_markup=None, **kwargs):
        """Handle document replies."""
        return await self.reply(caption or "Document", reply_markup=reply_markup)

    async def edit(self, text, disable_web_page_preview=True, reply_markup=None):
        """Edit the Discord message with updated status."""
        if self._discord_msg is None:
            return
        try:
            # Try to extract GID from the text for the stop button
            gid_match = re.search(r"/c(?:ancel)?_?ask_?(\w+)", text)
            if not gid_match:
                gid_match = re.search(r"(?:Stop|stop)\s*→\s*/\w+_(\w+)", text)
            
            if gid_match and not self._gid:
                self._gid = gid_match.group(1)

            embed = _parse_status_to_embed(text, self._gid)

            # Create stop button view if we have a GID
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
        """Delete the Discord message."""
        if self._discord_msg:
            try:
                await self._discord_msg.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                LOGGER.error(f"Discord delete error: {e}")

    async def unpin(self):
        """No-op for Discord."""
        pass

    async def download(self):
        """No-op — Discord parasite doesn't support file downloads."""
        return None

    async def send_initial_message(self, text: str = "⏳ Starting task..."):
        """Send the initial status message in Discord and store reference."""
        try:
            embed = discord.Embed(
                title="⏳ Starting Task...",
                description=_html_to_discord(text) if text != "⏳ Starting task..." else "Initializing mirror task...",
                color=0xFEE75C,  # Yellow
                timestamp=datetime.now(timezone.utc),
            )
            self._discord_msg = await self._channel.send(embed=embed)
            self.id = self._discord_msg.id
        except Exception as e:
            LOGGER.error(f"Discord send_initial error: {e}")
