"""
Discord-native quality picker for /ytdl command.
Replaces the Telegram-only YtSelection with Discord buttons + asyncio Event.
"""

from asyncio import Event, wait_for
from time import time

import discord

from ..helper.ext_utils.status_utils import get_readable_file_size, get_readable_time


class DiscordYtSelection:
    """
    Mirrors the API of YtSelection but uses Discord buttons instead of
    Telegram inline keyboard / CallbackQueryHandler.
    """

    def __init__(self, listener):
        self.listener = listener
        self._is_m4a = False
        self._reply_to = None       # discord.Message holding the quality picker
        self._time = time()
        self._timeout = 120
        self._is_playlist = False
        self.event = Event()
        self.formats = {}
        self.qual = None
        self._current_view = None

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _get_discord_msg(self):
        """Get the initial Discord message from MockMessage."""
        return getattr(self.listener.message, "_discord_msg", None)

    async def _send_or_edit(self, content: str, view: discord.ui.View):
        """Edit the existing initial status message in-place (no new messages)."""
        msg = self._get_discord_msg()
        if msg is None:
            return
        try:
            await msg.edit(content=content, embed=None, view=view)
        except Exception:
            pass

    async def _delete_picker(self):
        """No-op: we reuse the initial message, nothing to delete.
        The normal status update flow will just edit the same message."""
        pass

    def _remaining(self) -> str:
        return get_readable_time(max(0, self._timeout - (time() - self._time)))

    # ------------------------------------------------------------------ #
    #  Build Discord View for the main quality menu                       #
    # ------------------------------------------------------------------ #

    def _build_main_view(self) -> discord.ui.View:
        view = _YtQualView(self)
        # Add all format buttons
        for b_name, tbr_dict in self.formats.items():
            if len(tbr_dict) == 1:
                tbr, v_list = next(iter(tbr_dict.items()))
                label = f"{b_name} ({get_readable_file_size(v_list[0])})"
                view.add_format_button(label, f"sub {b_name} {tbr}")
            else:
                view.add_format_button(b_name, f"dict {b_name}")

        # Playlist-style uses wider labels
        if self._is_playlist:
            for res in ["144", "240", "360", "480", "720", "1080", "1440", "2160"]:
                view.add_format_button(f"{res}-mp4", f"{res}|mp4")
                view.add_format_button(f"{res}-webm", f"{res}|webm")

        view.add_special_button("MP3", "mp3")
        view.add_special_button("Audio Formats", "audio")
        view.add_special_button("Best Video", "bv*+ba/b")
        view.add_special_button("Best Audio", "ba/b")
        view.add_cancel_button()
        return view

    # ------------------------------------------------------------------ #
    #  Public API (mirrors YtSelection)                                   #
    # ------------------------------------------------------------------ #

    async def get_quality(self, result) -> str | None:
        """Build format list from yt-dlp result and show Discord picker."""
        if "entries" in result:
            self._is_playlist = True
        else:
            format_dict = result.get("formats")
            if format_dict:
                for item in format_dict:
                    if not item.get("tbr"):
                        continue
                    format_id = item["format_id"]
                    size = item.get("filesize") or item.get("filesize_approx") or 0

                    if item.get("video_ext") == "none" and (
                        item.get("resolution") == "audio only"
                        or item.get("acodec") != "none"
                    ):
                        if item.get("audio_ext") == "m4a":
                            self._is_m4a = True
                        b_name = f"{item.get('acodec') or format_id}-{item['ext']}"
                        v_format = format_id
                    elif item.get("height"):
                        height = item["height"]
                        ext = item["ext"]
                        fps = item["fps"] if item.get("fps") else ""
                        b_name = f"{height}p{fps}-{ext}"
                        ba_ext = "[ext=m4a]" if self._is_m4a and ext == "mp4" else ""
                        v_format = f"{format_id}+ba{ba_ext}/b[height=?{height}]"
                    else:
                        continue

                    self.formats.setdefault(b_name, {})[f"{item['tbr']}"] = [
                        size,
                        v_format,
                    ]

        kind = "Playlist" if self._is_playlist else "Video"
        msg = f"🎬 **Chọn Chất Lượng {kind}:**\n⏱️ Hết giờ: {self._remaining()}"
        view = self._build_main_view()
        await self._send_or_edit(msg, view)

        # Wait for button interaction (or timeout)
        try:
            await wait_for(self.event.wait(), timeout=self._timeout)
        except Exception:
            await self._send_or_edit("⏰ **Hết giờ.** Nhiệm vụ đã bị huỷ.", view=discord.ui.View())
            self.qual = None
            self.listener.is_cancelled = True
            self.event.set()

        if not self.listener.is_cancelled:
            await self._delete_picker()
        return self.qual

    async def back_to_main(self):
        view = self._build_main_view()
        kind = "Playlist" if self._is_playlist else "Video"
        await self._send_or_edit(
            f"🎬 **Chọn Chất Lượng {kind}:**\n⏱️ Hết giờ: {self._remaining()}", view
        )

    async def qual_subbuttons(self, b_name: str):
        view = _YtSubView(self, b_name)
        tbr_dict = self.formats[b_name]
        for tbr, d_data in tbr_dict.items():
            label = f"{tbr}K ({get_readable_file_size(d_data[0])})"
            view.add_tbr_button(label, f"sub {b_name} {tbr}")
        view.add_back_button()
        view.add_cancel_button()
        await self._send_or_edit(
            f"🎵 **Chọn Bitrate cho `{b_name}`:**\n⏱️ Hết giờ: {self._remaining()}", view
        )

    async def mp3_subbuttons(self):
        view = _YtSubView(self, "mp3")
        for q in [64, 128, 320]:
            view.add_tbr_button(f"{q}K-mp3", f"ba/b-mp3-{q}")
        view.add_back_button()
        view.add_cancel_button()
        await self._send_or_edit(
            f"🎶 **Chọn Bitrate MP3:**\n⏱️ Hết giờ: {self._remaining()}", view
        )

    async def audio_format(self):
        view = _YtSubView(self, "audio")
        for frmt in ["aac", "alac", "flac", "m4a", "opus", "vorbis", "wav"]:
            view.add_tbr_button(frmt.upper(), f"aq ba/b-{frmt}-")
        view.add_back_button()
        view.add_cancel_button()
        await self._send_or_edit(
            f"🎵 **Chọn Định Dạng Audio:**\n⏱️ Hết giờ: {self._remaining()}", view
        )

    async def audio_quality(self, fmt: str):
        view = _YtSubView(self, "aq")
        for q in range(11):
            view.add_tbr_button(str(q), f"{fmt}{q}")
        view.add_back_button("aq back")
        view.add_cancel_button("aq cancel")
        await self._send_or_edit(
            f"🎚️ **Chọn Chất Lượng Audio** (0 = tốt nhất, 10 = tệ nhất):\n⏱️ Hết giờ: {self._remaining()}", view
        )

    def _resolve_qual(self, data: list):
        """Parse data tokens exactly like select_format() in ytdlp.py."""
        cmd = data[0] if data else ""
        if cmd == "sub":
            b_name, tbr = data[1], data[2]
            self.qual = self.formats[b_name][tbr][1]
        elif cmd == "aq":
            # data = ["aq", "ba/b-flac-0"]
            self.qual = data[1]
        elif "|" in cmd:
            self.qual = self.formats.get(cmd)
        else:
            self.qual = cmd   # mp3-bitrate, bv*+ba/b, ba/b, etc.


# ------------------------------------------------------------------ #
#  Discord View implementations                                        #
# ------------------------------------------------------------------ #

_MAX_BUTTONS = 20   # leave some headroom under Discord's 25-field limit


class _YtQualView(discord.ui.View):
    """Main quality picker view."""

    def __init__(self, sel: DiscordYtSelection):
        super().__init__(timeout=None)
        self._sel = sel
        self._btn_count = 0

    def add_format_button(self, label: str, data: str):
        if self._btn_count >= _MAX_BUTTONS:
            return
        btn = discord.ui.Button(
            label=label[:80],
            style=discord.ButtonStyle.primary,
            custom_id=f"ytq::{data}",
        )
        btn.callback = self._make_cb(data)
        self.add_item(btn)
        self._btn_count += 1

    def add_special_button(self, label: str, data: str):
        btn = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=f"ytq::{data}",
        )
        btn.callback = self._make_cb(data)
        self.add_item(btn)

    def add_cancel_button(self):
        btn = discord.ui.Button(
            label="❌ Huỷ",
            style=discord.ButtonStyle.danger,
            custom_id="ytq::cancel",
        )
        btn.callback = self._cancel_cb
        self.add_item(btn)

    def _make_cb(self, data: str):
        sel = self._sel

        async def callback(interaction: discord.Interaction):
            # Only the task owner can interact
            owner_id = sel.listener.user_id
            if interaction.user.id != owner_id:
                await interaction.response.send_message("❌ Đây không phải của bạn!", ephemeral=True)
                return
            await interaction.response.defer()

            tokens = data.split()
            cmd = tokens[0]

            if cmd == "dict":
                await sel.qual_subbuttons(tokens[1])
            elif cmd == "mp3":
                await sel.mp3_subbuttons()
            elif cmd == "audio":
                await sel.audio_format()
            elif cmd == "aq":
                if tokens[1] == "back":
                    await sel.audio_format()
                else:
                    await sel.audio_quality(tokens[1])
            else:
                sel._resolve_qual(tokens)
                sel.event.set()

        return callback

    async def _cancel_cb(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self._sel.qual = None
        self._sel.listener.is_cancelled = True
        await self._sel._send_or_edit("🛑 **Đã huỷ chọn chất lượng.**", discord.ui.View())
        self._sel.event.set()


class _YtSubView(discord.ui.View):
    """Sub-menu view (bitrate/format/quality)."""

    def __init__(self, sel: DiscordYtSelection, context: str):
        super().__init__(timeout=None)
        self._sel = sel
        self._context = context

    def add_tbr_button(self, label: str, data: str):
        btn = discord.ui.Button(
            label=label[:80],
            style=discord.ButtonStyle.primary,
            custom_id=f"ytq_sub::{data}",
        )
        btn.callback = self._make_cb(data)
        self.add_item(btn)

    def add_back_button(self, data: str = "back"):
        btn = discord.ui.Button(
            label="⬅️ Quay Lại",
            style=discord.ButtonStyle.secondary,
            custom_id=f"ytq_sub::{data}",
        )
        btn.callback = self._make_cb(data)
        self.add_item(btn)

    def add_cancel_button(self, data: str = "cancel"):
        btn = discord.ui.Button(
            label="❌ Huỷ",
            style=discord.ButtonStyle.danger,
            custom_id=f"ytq_sub::{data}",
        )
        btn.callback = self._make_cb(data)
        self.add_item(btn)

    def _make_cb(self, data: str):
        sel = self._sel

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != sel.listener.user_id:
                await interaction.response.send_message("❌ Đây không phải của bạn!", ephemeral=True)
                return
            await interaction.response.defer()

            tokens = data.split()
            cmd = tokens[0]

            if cmd == "back":
                await sel.back_to_main()
            elif cmd == "cancel":
                sel.qual = None
                sel.listener.is_cancelled = True
                await sel._send_or_edit("🛑 **Đã huỷ chọn chất lượng.**", discord.ui.View())
                sel.event.set()
            elif cmd == "aq":
                # aq back or aq cancel
                sub = tokens[1] if len(tokens) > 1 else ""
                if sub == "back":
                    await sel.audio_format()
                elif sub == "cancel":
                    sel.qual = None
                    sel.listener.is_cancelled = True
                    await sel._send_or_edit("🛑 **Đã huỷ.**", discord.ui.View())
                    sel.event.set()
                else:
                    sel._resolve_qual(tokens)
                    sel.event.set()
            else:
                # sub b_name tbr  OR  aq ba/b-flac-  OR  plain qual string
                sel._resolve_qual(tokens)
                sel.event.set()

        return callback
