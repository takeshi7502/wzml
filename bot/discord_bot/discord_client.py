"""
Discord client setup and lifecycle management.
Runs the Discord bot alongside the Telegram bot on the same event loop.
"""

import traceback
from logging import getLogger, INFO

import discord
from discord import app_commands

from .. import LOGGER, bot_loop
from ..core.config_manager import Config
from .auth_manager import init_auth
from .slash_commands import setup_commands

# Enable discord.py library logging so errors show in log.txt
_discord_logger = getLogger("discord")
_discord_logger.setLevel(INFO)


class DiscordBot:
    """Singleton-style Discord bot manager."""

    client: discord.Client = None
    tree: app_commands.CommandTree = None
    _started: bool = False

    @classmethod
    async def start(cls):
        """Start the Discord bot. No-op if DISCORD_BOT_TOKEN is not set."""
        token = Config.DISCORD_BOT_TOKEN
        if not token:
            LOGGER.info("DISCORD_BOT_TOKEN not set — Discord bot disabled.")
            return

        if cls._started:
            LOGGER.warning("Discord bot already started.")
            return

        LOGGER.info("=" * 50)
        LOGGER.info("Discord Bot: Initializing...")
        LOGGER.info(f"Discord Bot: Token = {token[:10]}...{token[-5:]}")
        LOGGER.info(f"Discord Bot: Admin ID = {Config.DISCORD_ADMIN_ID}")
        LOGGER.info(f"Discord Bot: Auth Servers = {Config.DISCORD_AUTH_SERVERS}")
        LOGGER.info(f"Discord Bot: Status Interval = {Config.DISCORD_STATUS_INTERVAL}s")

        # Initialize auth system
        init_auth(Config.DISCORD_AUTH_SERVERS)
        LOGGER.info("Discord Bot: Auth system initialized.")

        # Setup intents — minimal, we only need guilds + messages for slash commands
        intents = discord.Intents.default()
        intents.message_content = False  # We don't read messages, only slash commands

        cls.client = discord.Client(intents=intents)
        cls.tree = app_commands.CommandTree(cls.client)

        # Register slash commands
        setup_commands(cls.tree)
        LOGGER.info("Discord Bot: Slash commands registered.")

        @cls.client.event
        async def on_ready():
            LOGGER.info("=" * 50)
            LOGGER.info(f"Discord Bot: ONLINE as {cls.client.user} (ID: {cls.client.user.id})")
            LOGGER.info(f"Discord Bot: Guilds = {len(cls.client.guilds)}")
            for g in cls.client.guilds:
                LOGGER.info(f"  - {g.name} (ID: {g.id})")
            try:
                synced = await cls.tree.sync()
                LOGGER.info(f"Discord Bot: Synced {len(synced)} slash command(s): {[c.name for c in synced]}")
            except Exception as e:
                LOGGER.error(f"Discord Bot: Command sync FAILED: {e}")
                LOGGER.error(traceback.format_exc())

            # Set presence
            await cls.client.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name="Mirror Tasks | /m",
                )
            )
            LOGGER.info("Discord Bot: Ready and waiting for commands!")
            LOGGER.info("=" * 50)

        @cls.client.event
        async def on_connect():
            LOGGER.info("Discord Bot: Connected to Discord gateway.")

        @cls.client.event
        async def on_disconnect():
            LOGGER.warning("Discord Bot: Disconnected from Discord gateway.")

        @cls.client.event
        async def on_resumed():
            LOGGER.info("Discord Bot: Session resumed.")

        # Start in background (non-blocking)
        cls._started = True
        bot_loop.create_task(cls._run())
        LOGGER.info("Discord Bot: Background task scheduled, waiting for connection...")

    @classmethod
    async def _run(cls):
        """Run the Discord client (called as a background task on bot_loop)."""
        try:
            LOGGER.info("Discord Bot: Calling client.start()...")
            await cls.client.start(Config.DISCORD_BOT_TOKEN)
        except discord.LoginFailure as e:
            LOGGER.error(f"Discord Bot: LOGIN FAILED! Check DISCORD_BOT_TOKEN. Error: {e}")
            cls._started = False
        except Exception as e:
            LOGGER.error(f"Discord Bot: Fatal error: {e}")
            LOGGER.error(traceback.format_exc())
            cls._started = False

    @classmethod
    async def stop(cls):
        """Gracefully stop the Discord bot."""
        if cls.client and not cls.client.is_closed():
            await cls.client.close()
            LOGGER.info("Discord Bot: Stopped.")
        cls._started = False
