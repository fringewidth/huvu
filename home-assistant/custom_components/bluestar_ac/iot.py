"""A small MQTT 3.1.1 client over a WebSocket, for AWS IoT.

Blue Star's ACs live on AWS IoT. Each AC is a thing, named by its thing id
(it looks like the Wi-Fi module's MAC address). Three topics matter:

1. `$aws/things/<id>/shadow/update`: a command, as the shadow's desired
   state: `{"state": {"desired": {"stemp": "24.0", "ts": <ms>, "src": "anmq"}}}`.
1. `things/<id>/state/reported`: the AC's whole state, sent after every
   change, whoever made it. `lcmd` names the field that changed and `src`
   who changed it.
1. `things/<id>/control`: `{"fpsh": 1}` makes the AC report its state now.

This client does only what those need: connect, subscribe, publish at QoS
0, and read what arrives. It runs on aiohttp, so it needs no thread and no
package that Home Assistant doesn't already have. It imports nothing from
Home Assistant.
"""

from __future__ import annotations

import struct
import time
from collections import deque
from collections.abc import AsyncIterator

import aiohttp
from yarl import URL

CONNECT = 0x10
CONNACK = 0x20
PUBLISH = 0x30
PUBACK = 0x40
SUBSCRIBE = 0x82
SUBACK = 0x90
PINGREQ = 0xC0
PINGRESP = 0xD0
DISCONNECT = 0xE0


class MqttError(Exception):
    """The MQTT connection failed or was refused."""


def _length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            return bytes(out)


def _string(text: str) -> bytes:
    data = text.encode()
    return struct.pack("!H", len(data)) + data


def _packet(header: int, body: bytes) -> bytes:
    return bytes([header]) + _length(len(body)) + body


class MqttConnection:
    """One MQTT session on one WebSocket. Open it with `MqttConnection.open`."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, keepalive: int) -> None:
        self._ws = ws
        self._keepalive = keepalive
        self._buffer = bytearray()
        self._pending: deque[tuple[str, bytes]] = deque()
        self._packet_id = 0
        self._last_heard = time.monotonic()

    @classmethod
    async def open(
        cls,
        http: aiohttp.ClientSession,
        url: str,
        client_id: str,
        keepalive: int = 60,
    ) -> MqttConnection:
        try:
            # The URL is presigned, so aiohttp must send its query as it is.
            ws = await http.ws_connect(
                URL(url, encoded=True),
                protocols=("mqtt",),
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
            )
        except (aiohttp.ClientError, TimeoutError) as error:
            raise MqttError(f"WebSocket failed: {error}") from error
        connection = cls(ws, keepalive)
        try:
            variable = _string("MQTT") + bytes([4, 0x02]) + struct.pack("!H", keepalive)
            await connection._send(_packet(CONNECT, variable + _string(client_id)))
            header, body = await connection._expect(CONNACK)
            if len(body) < 2 or body[1] != 0:
                raise MqttError(f"connection refused, code {body[1] if len(body) > 1 else '?'}")
        except BaseException:
            await connection.close()
            raise
        return connection

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        self._packet_id = self._packet_id % 65535 + 1
        body = struct.pack("!H", self._packet_id) + _string(topic) + bytes([qos])
        await self._send(_packet(SUBSCRIBE, body))
        header, body = await self._expect(SUBACK)
        if len(body) < 3 or body[2] == 0x80:
            raise MqttError(f"subscription to {topic} refused")

    async def publish(self, topic: str, payload: bytes) -> None:
        await self._send(_packet(PUBLISH, _string(topic) + payload))

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        """Every message on the subscribed topics, until the connection fails."""
        while True:
            while self._pending:
                yield self._pending.popleft()
            await self._read_one()

    async def close(self) -> None:
        if not self._ws.closed:
            try:
                await self._ws.send_bytes(_packet(DISCONNECT, b""))
            except (aiohttp.ClientError, ConnectionError, RuntimeError):
                pass
            await self._ws.close()

    async def _send(self, data: bytes) -> None:
        try:
            await self._ws.send_bytes(data)
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as error:
            raise MqttError(f"send failed: {error}") from error

    async def _expect(self, kind: int) -> tuple[int, bytes]:
        """Read until a packet of `kind` arrives. Messages on the way are kept."""
        deadline = time.monotonic() + 15
        while True:
            packet = self._take()
            if packet is not None and packet[0] & 0xF0 == kind & 0xF0:
                return packet
            if packet is not None:
                await self._handle(*packet)
            elif time.monotonic() > deadline:
                raise MqttError(f"no answer to packet type {kind:#x}")
            else:
                await self._receive(timeout=deadline - time.monotonic())

    async def _read_one(self) -> None:
        """Read one packet and handle it, pinging the broker while it is quiet."""
        packet = self._take()
        if packet is not None:
            await self._handle(*packet)
            return
        quiet = time.monotonic() - self._last_heard
        if quiet > self._keepalive * 1.5:
            raise MqttError(f"nothing from the broker in {quiet:.0f}s")
        try:
            await self._receive(timeout=self._keepalive / 2)
        except TimeoutError:
            await self._send(_packet(PINGREQ, b""))

    async def _receive(self, timeout: float) -> None:
        message = await self._ws.receive(timeout=max(timeout, 0.1))
        if message.type == aiohttp.WSMsgType.BINARY:
            self._buffer.extend(message.data)
            self._last_heard = time.monotonic()
        elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
            raise MqttError(f"the broker closed the connection ({self._ws.close_code})")
        elif message.type == aiohttp.WSMsgType.ERROR:
            raise MqttError(f"WebSocket error: {self._ws.exception()}")

    async def _handle(self, header: int, body: bytes) -> None:
        if header & 0xF0 != PUBLISH:
            return
        qos = (header >> 1) & 0x03
        (topic_length,) = struct.unpack_from("!H", body)
        topic = body[2 : 2 + topic_length].decode()
        rest = body[2 + topic_length :]
        if qos:
            packet_id = rest[:2]
            rest = rest[2:]
            await self._send(_packet(PUBACK, packet_id))
        self._pending.append((topic, bytes(rest)))

    def _take(self) -> tuple[int, bytes] | None:
        """The next whole packet in the buffer, or None if it hasn't all come."""
        buffer = self._buffer
        length = 0
        multiplier = 1
        index = 1
        while True:
            if index >= len(buffer):
                return None
            byte = buffer[index]
            length += (byte & 0x7F) * multiplier
            multiplier *= 128
            index += 1
            if not byte & 0x80:
                break
        if len(buffer) < index + length:
            return None
        header = buffer[0]
        body = bytes(buffer[index : index + length])
        del buffer[: index + length]
        return header, body
