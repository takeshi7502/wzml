import asyncio
import logging

from .data_center import DataCenter
from .transport.tcp_abridged import TCPAbridged
from .transport.tcp_abridged_o import TCPAbridgedO

log = logging.getLogger(__name__)


class Connection:
    MAX_RETRIES = 3

    MODES = {
        1: TCPAbridged,
        3: TCPAbridgedO,
    }

    def __init__(self, dc_id: int, test_mode: bool, ipv6: bool, proxy: dict, media: bool = False, mode: int = 3):
        self.dc_id = dc_id
        self.test_mode = test_mode
        self.ipv6 = ipv6
        self.proxy = proxy
        self.media = media
        self.address = DataCenter.get(dc_id, test_mode, ipv6, media)
        self.mode = self.MODES.get(mode, TCPAbridgedO)

        self.protocol = None

    async def connect(self):
        for i in range(Connection.MAX_RETRIES):
            self.protocol = self.mode(self.ipv6, self.proxy)

            try:
                log.info("Connecting...")
                await self.protocol.connect(self.address)
            except OSError as e:
                log.warning(f"Unable to connect due to network issues: {e}")
                self.protocol.close()
                await asyncio.sleep(1)
            else:
                log.info("Connected! DC{} transport={}".format(
                    self.dc_id,
                    self.mode.__name__,
                ))
                break
        else:
            log.warning("Connection failed! Trying again...")
            raise TimeoutError

    def close(self):
        self.protocol.close()
        log.info("Disconnected")

    async def send(self, data: bytes):
        try:
            await self.protocol.send(data)
        except Exception as e:
            raise OSError(e)

    async def recv(self):
        return await self.protocol.recv()
