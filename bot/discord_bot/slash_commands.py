"""
Discord slash commands for the WZML parasite bot.
Implements /m (mirror), /a (auth), /ping, /stats commands.
"""

import discord
from discord import app_commands
from time import time

from .. import LOGGER, bot_loop, bot_start_time
from ..core.config_manager import Config
from .auth_manager import is_authorized, add_authorized, remove_authorized, get_authorized_list
from .mock_telegram import MockMessage


async def _execute_wzml_task(interaction: discord.Interaction, cmd_prefix: str, link: str, options: str, run_func):
    """Generic task runner that builds the single-message UI lifecycle and delegates to WZML."""
    user = interaction.user
    channel = interaction.channel

    # Check authorization
    guild_id = interaction.guild_id if interaction.guild_id else None
    if not is_authorized(guild_id=guild_id, channel_id=channel.id, user_id=user.id):
        await interaction.followup.send(
            embed=discord.Embed(
                title="⛔ Chưa Được Cấp Quyền",
                description="Máy chủ/Kênh này chưa được cấp phép sử dụng Bot.",
                color=0xED4245,
            ),
            ephemeral=True,
        )
        return

    # Build command text parser
    cmd_text = f"/{cmd_prefix} {link}"
    if options:
        cmd_text += f" {options}"

    # Send ONE initial message via followup (replaces the "thinking..." spinner)
    truncated = link[:80] + "..." if len(link) > 80 else link
    initial_msg = await interaction.followup.send(
        embed=discord.Embed(
            title="🔄 Đang khởi tạo...",
            description="⏳ Vui lòng chờ trong giây lát, Bot đang tiến hành xử lý yêu cầu của bạn...",
            color=0xFEE75C,
        ),
        wait=True,
    )

    # Create mock with the Discord message already set
    mock_msg = MockMessage(
        channel=channel,
        user=user,
        text=cmd_text,
        interaction=interaction,
    )
    mock_msg._discord_msg = initial_msg  # All future edits go to this message
    mock_msg.id = initial_msg.id
    mock_msg.link = link  # Use the original URL for the [Source Link] text

    try:
        await run_func(mock_msg)
    except Exception as e:
        LOGGER.error(f"Discord command error ({cmd_prefix}): {e}", exc_info=True)
        try:
            await initial_msg.edit(embed=discord.Embed(
                title="❌ Đã Xảy Ra Lỗi",
                description=f"```{str(e)[:2000]}```",
                color=0xED4245,
            ))
        except Exception:
            pass


def _get_readable_time(seconds: float) -> str:
    """Convert seconds to human readable time string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    return "".join(parts)


def setup_commands(tree: app_commands.CommandTree):
    """Register all slash commands on the command tree."""

    @tree.command(name="m", description="Tải link trực tiếp (direct link) hoặc Magnet/Torrent lên Cloud")
    @app_commands.describe(
        link="Đường dẫn URL/magnet/torrent cần tải",
        options="Tùy chọn bổ sung (VD: -z để nén, -e để giải nén)",
    )
    async def mirror_cmd(interaction: discord.Interaction, link: str, options: str = ""):
        from ..modules.mirror_leech import Mirror
        async def run_func(msg):
            await Mirror(None, msg).new_event()

        await interaction.response.defer()
        bot_loop.create_task(_execute_wzml_task(interaction, "m", link, options, run_func))

    @tree.command(name="qm", description="Tải link Torrent/Magnet lên Cloud cực khoẻ bằng động cơ qBittorrent")
    @app_commands.describe(
        link="Đường dẫn Torrent/Magnet cần tải",
        options="Tùy chọn bổ sung (VD: -z để nén, -e để giải nén)",
    )
    async def qm_cmd(interaction: discord.Interaction, link: str, options: str = ""):
        from ..modules.mirror_leech import Mirror
        async def run_func(msg):
            await Mirror(None, msg, is_qbit=True).new_event()

        await interaction.response.defer()
        bot_loop.create_task(_execute_wzml_task(interaction, "qm", link, options, run_func))

    @tree.command(name="clone", description="Sao chép nhanh một link Google Drive vào vùng chứa Drive của kho")
    @app_commands.describe(
        link="Link Google Drive (hoặc ID gốc) muốn nhân bản",
        options="Tùy chọn bổ sung",
    )
    async def clone_cmd(interaction: discord.Interaction, link: str, options: str = ""):
        from ..modules.clone import Clone
        async def run_func(msg):
            await Clone(None, msg).new_event()

        await interaction.response.defer()
        bot_loop.create_task(_execute_wzml_task(interaction, "clone", link, options, run_func))

    @tree.command(name="ytdl", description="Tải video/audio từ YouTube hoặc 1000+ trang mạng khác lên Google Drive")
    @app_commands.describe(
        link="Link YouTube, TikTok, Facebook, v.v... muốn tải",
        options="Tùy chọn bổ sung (VD: -s để chọn chất lượng, -z để nén)",
    )
    async def ytdl_cmd(interaction: discord.Interaction, link: str, options: str = ""):
        from ..modules.ytdlp import YtDlp
        async def run_func(msg):
            await YtDlp(None, msg).new_event()

        await interaction.response.defer()
        bot_loop.create_task(_execute_wzml_task(interaction, "ytdl", link, options, run_func))

    @tree.command(name="del", description="Xoá vĩnh viễn tệp/thư mục trên Google Drive bằng Link")
    @app_commands.describe(
        link="Đường dẫn Google Drive muốn xoá vĩnh viễn",
    )
    async def del_cmd(interaction: discord.Interaction, link: str):
        if not Config.DISCORD_ADMIN_ID or interaction.user.id != Config.DISCORD_ADMIN_ID:
            await interaction.response.send_message("⛔ **Lỗi Phân Quyền:** Lệnh này chỉ dành riêng cho Quản trị viên của Bot (Admin).", ephemeral=True)
            return
            
        from ..modules.gd_delete import delete_file
        async def run_func(msg):
            await delete_file(None, msg)

        await interaction.response.defer()
        bot_loop.create_task(_execute_wzml_task(interaction, "del", link, "", run_func))

    @tree.command(name="ping", description="Kiểm tra độ trễ phản hồi của Bot (Ping)")
    async def ping_cmd(interaction: discord.Interaction):
        start = time()
        await interaction.response.send_message("🏓 Đang tính toán tỷ lệ Phóng/Nhận tín hiệu...", ephemeral=False)
        bot_latency = (time() - start) * 1000
        ws_latency = interaction.client.latency * 1000

        embed = discord.Embed(
            title="🏓 Pong!",
            color=0x57F287,
        )
        embed.add_field(name="Độ Trễ Phản Hồi", value=f"`{bot_latency:.2f}ms`", inline=True)
        embed.add_field(name="Kết nối Xong-Song (WS)", value=f"`{ws_latency:.2f}ms`", inline=True)
        await interaction.edit_original_response(content=None, embed=embed)

    @tree.command(name="about", description="Giới thiệu và hướng dẫn sử dụng Jin Mirror Bot")
    async def about_cmd(interaction: discord.Interaction):
        embed = discord.Embed(
            title="👋 Giới thiệu về Jin Mirror",
            description="Bot dùng để GetLink các thể loại khác sang Google Drive!\n"
                        "Từ đó, giúp việc tải về máy với tốc độ tối đa của link Google Drive.\n\n",
            color=0x5865F2,
        )
        embed.add_field(
            name="🛠️ Các lệnh cơ bản",
            value="🔹 `/m <link>`: Dùng link trực tiếp (direct link) của file muốn tải về.\n"
                  "🔹 `/qm <link>`: Dùng link Magnet/Torrent cho các file Torrent.\n"
                  "🔹 `/ytdl <link>`: Tải video/audio từ YouTube, TikTok, Facebook, v.v...\n"
                  "🔹 `/clone <link>`: Sao chép nhanh một link Google Drive vào Drive của bot.",
            inline=False
        )
        embed.add_field(
            name="🌐 /ytdl hỗ trợ những trang nào?",
            value="Lệnh `/ytdl` dùng engine **yt-dlp** hỗ trợ hơn **1000+ trang web**, bao gồm:\n"
                  "▪️ **Video:** YouTube, TikTok, Facebook, Instagram, Twitter/X, Bilibili, Vimeo, Dailymotion, Twitch, Rumble, Odysee...\n"
                  "▪️ **Âm nhạc:** SoundCloud, Bandcamp, Mixcloud, Deezer (public)...\n"
                  "▪️ **Tin tức / Khác:** Reddit, Imgur, Streamtape, Doodstream, Streamlare, Filemoon...\n"
                  "▪️ **Playlist:** Hỗ trợ tải toàn bộ playlist YouTube, TikTok, v.v...\n"
                  "📋 Danh sách đầy đủ: [yt-dlp supported sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)",
            inline=False
        )
        embed.add_field(
            name="💡 Mẹo Nhỏ",
            value="Trong lúc file đang tải xuống, bạn có thể nhấn nút **Cancel 🔴** để huỷ tiến trình bất kỳ ngay lập tức.",
            inline=False
        )
        embed.set_footer(text="Trợ lý tự động WZML-Discord được vận hành bởi Takeshi.")
        await interaction.response.send_message(embed=embed)

    @tree.command(name="stats", description="Xem thông số kĩ thuật và Tài nguyên Máy Chủ (VPS)")
    async def stats_cmd(interaction: discord.Interaction):
        if not Config.DISCORD_ADMIN_ID or interaction.user.id != Config.DISCORD_ADMIN_ID:
            await interaction.response.send_message("⛔ **Lỗi Phân Quyền:** Lệnh này chỉ dành riêng cho Quản trị viên của Bot (Admin).", ephemeral=True)
            return
            
        import psutil
        import shutil

        # Uptime
        uptime = _get_readable_time(time() - bot_start_time)

        # CPU & RAM
        cpu_percent = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        ram_percent = ram.percent

        # Disk
        disk = shutil.disk_usage("/")
        disk_total = disk.total / (1024 ** 3)
        disk_used = disk.used / (1024 ** 3)
        disk_free = disk.free / (1024 ** 3)

        embed = discord.Embed(
            title="📊 Bảng Đo Lường Thông Số (VPS)",
            color=0x5865F2,
        )
        embed.add_field(name="⏱ Thời gian Bot On", value=f"`{uptime}`", inline=True)
        embed.add_field(name="🖥 Trạng thái Máy", value=f"`{cpu_percent}% CPU`", inline=True)
        embed.add_field(name="🧠 Nhiệm vụ Bộ Nhớ", value=f"`{ram_percent}% RAM`", inline=True)
        embed.add_field(name="💾 Ổ Lưu trữ Tổng", value=f"`{disk_used:.2f}GB / {disk_total:.2f}GB`", inline=True)
        embed.add_field(name="📂 Trống Dư ra", value=f"`{disk_free:.2f}GB`", inline=True)

        await interaction.response.send_message(embed=embed)

    @tree.command(name="auth", description="Cấp hoặc Huỷ quyền sử dụng Bot cho Server/Channel")
    @app_commands.describe(
        action="Lựa chọn thêm (add), xóa (remove), hoặc liệt kê (list)",
        server_id="ID (Mã Nhận Diện) của Server/Channel muốn cấp phép (tùy chọn, mặc định lấy của kênh hiện tại)",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
        app_commands.Choice(name="list", value="list"),
    ])
    async def auth_cmd(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        server_id: str = None,
    ):
        user_id = interaction.user.id
        # Only DISCORD_ADMIN_ID can manage authorization
        if not Config.DISCORD_ADMIN_ID:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⛔ Lỗi Cấu Hình System",
                    description="Biến DISCORD_ADMIN_ID chưa được thiết lập trong Config. Không thể xác định được Admin.",
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return

        if user_id != Config.DISCORD_ADMIN_ID:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⛔ Lỗi Phân Quyền",
                    description="Chỉ Admin Bot mới có thể quản lý việc cấp quyền.",
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return

        action_val = action.value

        if action_val == "list":
            auth_list = get_authorized_list()
            if auth_list:
                lines = []
                for idx, aid in enumerate(reversed(auth_list), 1):
                    guild = interaction.client.get_guild(int(aid))
                    if guild:
                        name = guild.name
                    else:
                        channel = interaction.client.get_channel(int(aid))
                        if channel:
                            name = f"#{channel.name} (Kênh)"
                        else:
                            name = "Chưa nhận diện Kênh/Máy Chủ"
                    lines.append(f"**{idx}.** {name} (`{aid}`)")
                    
                desc = f"**Tổng số vị trí đã Cấp Phép:** {len(auth_list)}\n\n" + "\n".join(lines)
            else:
                desc = "Hiện tại chưa có Kênh/Server nào được cấp phép."
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="📋 Danh Sách Cấp Phép",
                    description=desc,
                    color=0x5865F2,
                ),
                ephemeral=True,
            )
            return

        # Determine target ID
        if server_id:
            try:
                target_id = int(server_id.strip())
            except ValueError:
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ ID Không Hợp Lệ",
                        description="Vui lòng cung cấp ID Máy Chủ/Kênh là một chuỗi Chữ số.",
                        color=0xED4245,
                    ),
                    ephemeral=True,
                )
                return
        else:
            target_id = interaction.guild_id if interaction.guild_id else interaction.channel_id

        if action_val == "add":
            result = await add_authorized(target_id)
            if result:
                embed = discord.Embed(
                    title="✅ Cấp Quyền Thành Công",
                    description=f"ID `{target_id}` đã được cấp quyền sử dụng bot.",
                    color=0x57F287,
                )
            else:
                embed = discord.Embed(
                    title="ℹ️ Đã Tồn Tại",
                    description=f"ID `{target_id}` đã được cấp quyền hợp lệ từ trước rồi.",
                    color=0xFEE75C,
                )
        elif action_val == "remove":
            result = await remove_authorized(target_id)
            if result:
                embed = discord.Embed(
                    title="✅ Thu Hồi Quyền Hành",
                    description=f"ID `{target_id}` đã gỡ bỏ khỏi thư mục danh sách được phép dùng mạng lưới Tải Về.",
                    color=0x57F287,
                )
            else:
                embed = discord.Embed(
                    title="ℹ️ Lệnh Vô Hiệu Lực",
                    description=f"ID `{target_id}` không tồn tại trong Cấu Trúc Khai Báo hoặc chưa từng Cấp Phép.",
                    color=0xFEE75C,
                )
        else:
            embed = discord.Embed(
                title="❌ Thao Tác Từ Chối",
                description="Bạn chỉ có thể sử dụng `add` , `remove` , `list` .",
                color=0xED4245,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)
