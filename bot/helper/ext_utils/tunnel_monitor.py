from asyncio import sleep
from os import environ

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath

from ... import LOGGER, bot_loop
from ...core.config_manager import Config


TUNNEL_URL_FILE = environ.get("TUNNEL_URL_FILE", "/data/tunnel_url.txt")


async def _read_tunnel_url():
    try:
        if not await aiopath.isfile(TUNNEL_URL_FILE):
            return None
        async with aiopen(TUNNEL_URL_FILE, "r") as f:
            url = (await f.read()).strip()
        return url or None
    except Exception as e:
        LOGGER.warning(f"tunnel_monitor: read failed: {e}")
        return None


async def _apply_tunnel_url(url):
    if not url or Config.BASE_URL == url:
        return False
    Config.BASE_URL = url
    try:
        from .db_handler import database

        await database.update_config({"BASE_URL": url})
    except Exception as e:
        LOGGER.warning(f"tunnel_monitor: database update failed: {e}")
    LOGGER.info(f"tunnel_monitor: BASE_URL = {url}")
    return True


async def _tunnel_monitor_loop():
    LOGGER.info("tunnel_monitor: started")
    while True:
        try:
            url = await _read_tunnel_url()
            await _apply_tunnel_url(url)
        except Exception as e:
            LOGGER.error(f"tunnel_monitor: {e}")
        await sleep(5)


async def apply_tunnel_url_once():
    url = await _read_tunnel_url()
    if url:
        changed = await _apply_tunnel_url(url)
        if not changed:
            LOGGER.info(f"tunnel_monitor: current BASE_URL = {url}")
    return url


def start_tunnel_monitor():
    bot_loop.create_task(_tunnel_monitor_loop())
    LOGGER.info("tunnel_monitor: background monitor started")
