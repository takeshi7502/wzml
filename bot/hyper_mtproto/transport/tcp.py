import asyncio
import ipaddress
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import socks

log = logging.getLogger(__name__)


class TCP:
    TIMEOUT = 10

    def __init__(self, ipv6: bool, proxy: dict):
        self.socket = None

        self.reader = None
        self.writer = None

        self.lock = asyncio.Lock()
        self.loop = asyncio.get_event_loop()

        if proxy:
            hostname = proxy.get("hostname")

            try:
                ip_address = ipaddress.ip_address(hostname)
            except ValueError:
                self.socket = socks.socksocket(socket.AF_INET)
            else:
                if isinstance(ip_address, ipaddress.IPv6Address):
                    self.socket = socks.socksocket(socket.AF_INET6)
                else:
                    self.socket = socks.socksocket(socket.AF_INET)

            self.socket.set_proxy(
                proxy_type=getattr(socks, proxy.get("scheme").upper()),
                addr=hostname,
                port=proxy.get("port", None),
                username=proxy.get("username", None),
                password=proxy.get("password", None)
            )

            log.info(f"Using proxy {hostname}")
        else:
            self.socket = socks.socksocket(
                socket.AF_INET6 if ipv6
                else socket.AF_INET
            )

        self.socket.settimeout(TCP.TIMEOUT)

    async def connect(self, address: tuple):
        with ThreadPoolExecutor(1) as executor:
            await self.loop.run_in_executor(executor, self.socket.connect, address)

        self.reader, self.writer = await asyncio.open_connection(sock=self.socket)

    def close(self):
        try:
            self.writer.close()
        except AttributeError:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                time.sleep(0.001)
                self.socket.close()

    async def send(self, data: bytes):
        async with self.lock:
            self.writer.write(data)
            await self.writer.drain()

    async def recv(self, length: int = 0):
        data = b""

        while len(data) < length:
            try:
                chunk = await asyncio.wait_for(
                    self.reader.read(length - len(data)),
                    TCP.TIMEOUT
                )
            except (OSError, asyncio.TimeoutError):
                return None
            else:
                if chunk:
                    data += chunk
                else:
                    return None

        return data
