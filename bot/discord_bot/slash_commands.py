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
                title="⛔ Not Authorized",
                description="This server/channel is not authorized.\nAsk admin: `/a add <server_id>`",
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
                title="❌ Error",
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

    @tree.command(name="m", description="Mirror a link to cloud storage")
    @app_commands.describe(
        link="The URL/magnet/link to mirror",
        options="Additional options (e.g. -z for compress, -e for extract)",
    )
    async def mirror_cmd(interaction: discord.Interaction, link: str, options: str = ""):
        from ..modules.mirror_leech import Mirror
        async def run_func(msg):
            await Mirror(None, msg).new_event()

        await interaction.response.defer()
        bot_loop.create_task(_execute_wzml_task(interaction, "m", link, options, run_func))

    @tree.command(name="qm", description="Mirror a link using qBittorrent to cloud storage")
    @app_commands.describe(
        link="The torrent/magnet link to mirror via qBittorrent",
        options="Additional options (e.g. -z for compress, -e for extract)",
    )
    async def qm_cmd(interaction: discord.Interaction, link: str, options: str = ""):
        from ..modules.mirror_leech import Mirror
        async def run_func(msg):
            await Mirror(None, msg, is_qbit=True).new_event()

        await interaction.response.defer()
        bot_loop.create_task(_execute_wzml_task(interaction, "qm", link, options, run_func))

    @tree.command(name="clone", description="Clone a Google Drive link or rclone path")
    @app_commands.describe(
        link="The Google Drive link/ID or rclone path to clone",
        options="Additional options",
    )
    async def clone_cmd(interaction: discord.Interaction, link: str, options: str = ""):
        from ..modules.clone import Clone
        async def run_func(msg):
            await Clone(None, msg).new_event()

        await interaction.response.defer()
        bot_loop.create_task(_execute_wzml_task(interaction, "clone", link, options, run_func))

    @tree.command(name="del", description="Delete a file/folder from Google Drive")
    @app_commands.describe(
        link="The Google Drive link to delete",
    )
    async def del_cmd(interaction: discord.Interaction, link: str):
        if not Config.DISCORD_ADMIN_ID or interaction.user.id != Config.DISCORD_ADMIN_ID:
            await interaction.response.send_message("⛔ **Permission Denied:** This command is restricted to Bot Admin.", ephemeral=True)
            return
            
        from ..modules.gd_delete import delete_file
        async def run_func(msg):
            await delete_file(None, msg)

        await interaction.response.defer()
        bot_loop.create_task(_execute_wzml_task(interaction, "del", link, "", run_func))

    @tree.command(name="ping", description="Check bot latency")
    async def ping_cmd(interaction: discord.Interaction):
        start = time()
        await interaction.response.send_message("🏓 Calculating...", ephemeral=False)
        bot_latency = (time() - start) * 1000
        ws_latency = interaction.client.latency * 1000

        embed = discord.Embed(
            title="🏓 Pong!",
            color=0x57F287,
        )
        embed.add_field(name="Bot Latency", value=f"`{bot_latency:.2f}ms`", inline=True)
        embed.add_field(name="WS Latency", value=f"`{ws_latency:.2f}ms`", inline=True)
        await interaction.edit_original_response(content=None, embed=embed)

    @tree.command(name="about", description="Giới thiệu và hướng dẫn sử dụng Jin Mirror Bot")
    async def about_cmd(interaction: discord.Interaction):
        embed = discord.Embed(
            title="👋 Giới thiệu Jin Mirror",
            description="Chào mừng bạn đến với hệ thống Leech/Mirror chuyển đổi lưu trữ siêu tốc độ!\n\n"
                        "Bot được thiết kế nhắm tới việc giải quyết vấn đề tải file từ các dịch vụ Cloud lưu trữ bị giới hạn băng thông chậm (VD: Terabox, Mega, Fshare, hoặc file Torrent/Magnet). Bot sẽ làm trung gian, tự động kéo file đó với tốc độ không giới hạn của VPS và đưa thẳng lên Google Drive. Từ đó, bạn chỉ việc tải về máy với tốc độ tối đa của Google Drive.",
            color=0x5865F2,
        )
        embed.add_field(
            name="🛠️ Các Lệnh Cơ Bản",
            value="🔹 `/m <link>`: Tải link trực tiếp (direct link) hoặc Magnet Torrent về Google Drive.\n"
                  "🔹 `/qm <link>`: Chỉ định tải bằng động cơ qBittorrent cực khoẻ cho link Magnet/Torrent lớn.\n"
                  "🔹 `/clone <link>`: Sao chép nhanh một link Google Drive vào vùng chứa Drive của kho.",
            inline=False
        )
        embed.add_field(
            name="💡 Mẹo Nhỏ",
            value="Trong lúc nhiều file tải xuống cùng lúc, bạn hoàn toàn có thể nhấn nút **Cancel 🔴** có đánh số thứ tự tương ứng ở phía dưới bảng trạng thái để huỷ tiến trình bất kỳ ngay lập tức.",
            inline=False
        )
        embed.set_footer(text="Trợ lý tự động WZML-Discord được vận hành bởi Takeshi.")
        await interaction.response.send_message(embed=embed)

    @tree.command(name="stats", description="Show bot system statistics")
    async def stats_cmd(interaction: discord.Interaction):
        if not Config.DISCORD_ADMIN_ID or interaction.user.id != Config.DISCORD_ADMIN_ID:
            await interaction.response.send_message("⛔ **Permission Denied:** This command is restricted to Bot Admin.", ephemeral=True)
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
            title="📊 Bot Statistics",
            color=0x5865F2,
        )
        embed.add_field(name="⏱ Uptime", value=f"`{uptime}`", inline=True)
        embed.add_field(name="🖥 CPU", value=f"`{cpu_percent}%`", inline=True)
        embed.add_field(name="🧠 RAM", value=f"`{ram_percent}%`", inline=True)
        embed.add_field(name="💾 Disk", value=f"`{disk_used:.2f}GB/{disk_total:.2f}GB`", inline=True)
        embed.add_field(name="📂 Free Space", value=f"`{disk_free:.2f}GB`", inline=True)

        await interaction.response.send_message(embed=embed)

    @tree.command(name="auth", description="Authorize/deauthorize a Discord server for mirror commands")
    @app_commands.describe(
        action="add or remove or list",
        server_id="The Discord server/channel ID to authorize (optional, defaults to current server)",
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
                    title="⛔ Not Configured",
                    description="DISCORD_ADMIN_ID is not set in config. Cannot verify admin.",
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return

        if user_id != Config.DISCORD_ADMIN_ID:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⛔ Permission Denied",
                    description="Only the bot admin can manage authorization.",
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return

        action_val = action.value

        if action_val == "list":
            auth_list = get_authorized_list()
            if auth_list:
                desc = "\n".join([f"• `{aid}`" for aid in auth_list])
            else:
                desc = "No servers/channels authorized."
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="📋 Authorized List",
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
                        title="❌ Invalid ID",
                        description="Please provide a valid numeric server/channel ID.",
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
                    title="✅ Authorized",
                    description=f"ID `{target_id}` has been authorized for mirror commands.",
                    color=0x57F287,
                )
            else:
                embed = discord.Embed(
                    title="ℹ️ Already Authorized",
                    description=f"ID `{target_id}` is already authorized.",
                    color=0xFEE75C,
                )
        elif action_val == "remove":
            result = await remove_authorized(target_id)
            if result:
                embed = discord.Embed(
                    title="✅ Deauthorized",
                    description=f"ID `{target_id}` has been removed from authorized list.",
                    color=0x57F287,
                )
            else:
                embed = discord.Embed(
                    title="ℹ️ Not Found",
                    description=f"ID `{target_id}` was not in the authorized list.",
                    color=0xFEE75C,
                )
        else:
            embed = discord.Embed(
                title="❌ Unknown Action",
                description="Use `add`, `remove`, or `list`.",
                color=0xED4245,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)
