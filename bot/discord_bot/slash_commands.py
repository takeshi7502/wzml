"""
Discord slash commands for the WZML parasite bot.
Implements /m (mirror) and /a (auth) commands.
"""

import discord
from discord import app_commands

from .. import LOGGER, bot_loop
from ..core.config_manager import Config
from .auth_manager import is_authorized, add_authorized, remove_authorized, get_authorized_list
from .mock_telegram import MockMessage


async def _run_mirror(interaction: discord.Interaction, link: str, options: str = ""):
    """Execute a mirror task through the WZML core."""
    user = interaction.user
    channel = interaction.channel

    # Check authorization
    guild_id = interaction.guild_id if interaction.guild_id else None
    if not is_authorized(guild_id=guild_id, channel_id=channel.id, user_id=user.id):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="⛔ Not Authorized",
                description="This server/channel is not authorized to use mirror commands.\nAsk the admin to run `/a add <server_id>`.",
                color=0xED4245,
            ),
            ephemeral=True,
        )
        return

    # Acknowledge immediately (Discord requires response within 3s)
    await interaction.response.send_message(
        embed=discord.Embed(
            title="📥 Mirror Task Received",
            description=f"**Link:** `{link[:100]}{'...' if len(link) > 100 else ''}`\nInitializing...",
            color=0xFEE75C,
        ),
    )

    # Build the command text as the Telegram bot expects it
    cmd_text = f"/m {link}"
    if options:
        cmd_text += f" {options}"

    # Create mock objects
    mock_msg = MockMessage(
        channel=channel,
        user=user,
        text=cmd_text,
        interaction=interaction,
    )

    # Send the initial Discord message that will be updated with progress
    await mock_msg.send_initial_message(f"Processing: {link[:80]}...")

    try:
        # Import and run Mirror class from the existing WZML modules
        from ..modules.mirror_leech import Mirror
        await Mirror(None, mock_msg).new_event()
    except Exception as e:
        LOGGER.error(f"Discord mirror error: {e}", exc_info=True)
        try:
            error_embed = discord.Embed(
                title="❌ Mirror Error",
                description=f"```{str(e)[:2000]}```",
                color=0xED4245,
            )
            await channel.send(embed=error_embed)
        except Exception:
            pass


def setup_commands(tree: app_commands.CommandTree):
    """Register all slash commands on the command tree."""

    @tree.command(name="m", description="Mirror a link to cloud storage")
    @app_commands.describe(
        link="The URL/magnet/link to mirror",
        options="Additional options (e.g. -z for compress, -e for extract)",
    )
    async def mirror_cmd(interaction: discord.Interaction, link: str, options: str = ""):
        bot_loop.create_task(_run_mirror(interaction, link, options))

    @tree.command(name="a", description="Authorize/deauthorize a Discord server for mirror commands")
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
