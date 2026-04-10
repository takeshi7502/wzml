"""
Persistent authorization manager for Discord bot.
Stores authorized server/channel IDs to a JSON file so they survive restarts.
"""

import json
import os
from asyncio import Lock

AUTH_FILE = "discord_auth.json"
_auth_lock = Lock()
_authorized_ids: set = set()


def _load_auth():
    """Load authorized IDs from disk."""
    global _authorized_ids
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r") as f:
                data = json.load(f)
                _authorized_ids = set(data.get("authorized", []))
        except Exception:
            _authorized_ids = set()
    else:
        _authorized_ids = set()


def _save_auth():
    """Save authorized IDs to disk."""
    with open(AUTH_FILE, "w") as f:
        json.dump({"authorized": list(_authorized_ids)}, f, indent=2)


def init_auth(config_servers: str):
    """Initialize auth from config + persistent file.
    config_servers: comma-separated server IDs from Config.DISCORD_AUTH_SERVERS
    """
    _load_auth()
    if config_servers:
        for sid in config_servers.split(","):
            sid = sid.strip()
            if sid:
                try:
                    _authorized_ids.add(int(sid))
                except ValueError:
                    pass
    _save_auth()


async def add_authorized(server_id: int) -> bool:
    """Add a server/channel ID to the authorized list. Returns True if newly added."""
    async with _auth_lock:
        if server_id in _authorized_ids:
            return False
        _authorized_ids.add(server_id)
        _save_auth()
        return True


async def remove_authorized(server_id: int) -> bool:
    """Remove a server/channel ID from the authorized list. Returns True if removed."""
    async with _auth_lock:
        if server_id not in _authorized_ids:
            return False
        _authorized_ids.discard(server_id)
        _save_auth()
        return True


def is_authorized(guild_id: int = None, channel_id: int = None, user_id: int = None) -> bool:
    """Check if a guild, channel, or user is authorized."""
    from ..core.config_manager import Config
    # Discord Admin is always authorized
    if user_id and Config.DISCORD_ADMIN_ID and user_id == Config.DISCORD_ADMIN_ID:
        return True
    if guild_id and guild_id in _authorized_ids:
        return True
    if channel_id and channel_id in _authorized_ids:
        return True
    if user_id and user_id in _authorized_ids:
        return True
    return False


def get_authorized_list() -> list:
    """Return list of all authorized IDs."""
    return list(_authorized_ids)
