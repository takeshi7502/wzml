"""
Persistent authorization manager for Discord bot.
Stores authorized server/channel IDs in MongoDB (same DB as the rest of WZML).
Falls back to a local JSON file if MongoDB is not configured.
"""

import json
import os
from asyncio import Lock

_auth_lock = Lock()
_authorized_ids: list = []

# Local file fallback (anchored to this module's directory)
_HERE = os.path.dirname(os.path.abspath(__file__))
_AUTH_FILE_FALLBACK = os.path.join(_HERE, "discord_auth.json")

# MongoDB collection name
_MONGO_DOC_ID = "discord_auth"


# ─── MongoDB helpers ────────────────────────────────────────────────

async def _mongo_load() -> list:
    """Load authorized IDs from MongoDB. Returns list or None if unavailable."""
    try:
        from ..core.config_manager import Config
        if not Config.DATABASE_URL:
            return None
        from motor.motor_asyncio import AsyncIOMotorClient
        from pymongo.server_api import ServerApi
        client = AsyncIOMotorClient(Config.DATABASE_URL, server_api=ServerApi("1"))
        db = client.wzmlx
        doc = await db.discord.auth.find_one({"_id": _MONGO_DOC_ID})
        await client.close()
        if doc:
            return doc.get("ids", [])
        return []
    except Exception:
        return None


async def _mongo_save(ids: list):
    """Save authorized IDs to MongoDB."""
    try:
        from ..core.config_manager import Config
        if not Config.DATABASE_URL:
            return
        from motor.motor_asyncio import AsyncIOMotorClient
        from pymongo.server_api import ServerApi
        client = AsyncIOMotorClient(Config.DATABASE_URL, server_api=ServerApi("1"))
        db = client.wzmlx
        await db.discord.auth.replace_one(
            {"_id": _MONGO_DOC_ID}, {"_id": _MONGO_DOC_ID, "ids": ids}, upsert=True
        )
        await client.close()
    except Exception:
        pass


# ─── Local file fallback helpers ────────────────────────────────────

def _file_load() -> list:
    if os.path.exists(_AUTH_FILE_FALLBACK):
        try:
            with open(_AUTH_FILE_FALLBACK, "r") as f:
                data = json.load(f)
                return data.get("authorized", [])
        except Exception:
            pass
    return []


def _file_save(ids: list):
    try:
        with open(_AUTH_FILE_FALLBACK, "w") as f:
            json.dump({"authorized": ids}, f, indent=2)
    except Exception:
        pass


# ─── Public API ─────────────────────────────────────────────────────

def init_auth(config_servers: str):
    """
    Synchronous init called at startup (before the event loop is running for discord).
    Loads from local file only; MongoDB load happens in init_auth_async().
    config_servers: comma-separated server IDs from Config.DISCORD_AUTH_SERVERS
    """
    from .. import LOGGER
    global _authorized_ids
    # Load from local file first (fast, sync)
    loaded = _file_load()
    _authorized_ids = []
    for x in loaded:
        if x not in _authorized_ids:
            _authorized_ids.append(x)
    LOGGER.info(f"Discord Auth (file): Loaded {len(_authorized_ids)} IDs")

    # Merge config servers
    if config_servers:
        for sid in config_servers.split(","):
            sid = sid.strip()
            if sid:
                try:
                    val = int(sid)
                    if val not in _authorized_ids:
                        _authorized_ids.append(val)
                except ValueError:
                    pass
    _file_save(_authorized_ids)
    LOGGER.info(f"Discord Auth: Total authorized IDs after init: {len(_authorized_ids)}")


async def init_auth_async():
    """
    Async init — called from on_ready to load authorizations from MongoDB.
    Merges MongoDB IDs with whatever is already in memory.
    """
    from .. import LOGGER
    global _authorized_ids
    async with _auth_lock:
        mongo_ids = await _mongo_load()
        if mongo_ids is None:
            LOGGER.info("Discord Auth: MongoDB not available, using local file only.")
            return
        merged = list(_authorized_ids)
        for mid in mongo_ids:
            if mid not in merged:
                merged.append(mid)
        _authorized_ids = merged
        # Sync back the merged list
        await _mongo_save(_authorized_ids)
        _file_save(_authorized_ids)
        LOGGER.info(f"Discord Auth (MongoDB): Total authorized IDs: {len(_authorized_ids)}")


async def add_authorized(server_id: int) -> bool:
    """Add a server/channel ID to the authorized list. Returns True if newly added."""
    async with _auth_lock:
        if server_id in _authorized_ids:
            return False
        _authorized_ids.append(server_id)
        await _mongo_save(_authorized_ids)
        _file_save(_authorized_ids)
        return True


async def remove_authorized(server_id: int) -> bool:
    """Remove a server/channel ID from the authorized list. Returns True if removed."""
    async with _auth_lock:
        if server_id not in _authorized_ids:
            return False
        _authorized_ids.remove(server_id)
        await _mongo_save(_authorized_ids)
        _file_save(_authorized_ids)
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
