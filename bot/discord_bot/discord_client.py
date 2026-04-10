"""
Discord client setup and lifecycle management.
Runs the Discord bot alongside the Telegram bot on the same event loop.
"""

import discord
from discord import app_commands

from .. import LOGGER, bot_loop
from ..core.config_manager import Config
from .auth_manager import init_auth
from .slash_commands import setup_commands


class DiscordBot:
    """Singleton-style Discord bot manager."""

    client: discord.Client = None
    tree: app_commands.CommandTree = None
    _started: bool = False

    @classmethod
    async def start(cls):
        """Start the Discord bot. No-op if DISCORD_BOT_TOKEN is not set."""
        if not Config.DISCORD_BOT_TOKEN:
            LOGGER.info("DISCORD_BOT_TOKEN not set — Discord bot disabled.")
            return

        if cls._started:
            LOGGER.warning("Discord bot already started.")
            return

        LOGGER.info("Starting Discord bot...")

        # Initialize auth system
        init_auth(Config.DISCORD_AUTH_SERVERS)

        # Setup intents — minimal, we only need guilds + messages for slash commands
        intents = discord.Intents.default()
        intents.message_content = False  # We don't read messages, only slash commands

        cls.client = discord.Client(intents=intents)
        cls.tree = app_commands.CommandTree(cls.client)

        # Register slash commands
        setup_commands(cls.tree)

        @cls.client.event
        async def on_ready():
            LOGGER.info(f"Discord bot logged in as {cls.client.user} (ID: {cls.client.user.id})")
            try:
                synced = await cls.tree.sync()
                LOGGER.info(f"Discord: Synced {len(synced)} slash command(s)")
            except Exception as e:
                LOGGER.error(f"Discord command sync failed: {e}")

            # Set presence
            await cls.client.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name="Mirror Tasks | /m",
                )
            )

        # Start in background (non-blocking)
        cls._started = True
        bot_loop.create_task(cls._run())

    @classmethod
    async def _run(cls):
        """Run the Discord client (called as a background task on bot_loop)."""
        try:
            await cls.client.start(Config.DISCORD_BOT_TOKEN)
        except discord.LoginFailure:
            LOGGER.error("Discord bot login failed! Check DISCORD_BOT_TOKEN.")
            cls._started = False
        except Exception as e:
            LOGGER.error(f"Discord bot error: {e}", exc_info=True)
            cls._started = False

    @classmethod
    async def stop(cls):
        """Gracefully stop the Discord bot."""
        if cls.client and not cls.client.is_closed():
            await cls.client.close()
            LOGGER.info("Discord bot stopped.")
        cls._started = False
