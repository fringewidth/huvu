"""MQTT framing in iot.py: lengths, split frames, QoS 1 acks."""

from __future__ import annotations

import struct

import pytest

from custom_components.bluestar_ac.iot import PUBACK, PUBLISH, MqttConnection, _length, _packet, _string


class _FakeWebSocket:
    closed = False

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


@pytest.mark.parametrize("n", [0, 127, 128, 16383, 16384, 2097151])
def test_length_round_trip(n: int) -> None:
    connection = MqttConnection(_FakeWebSocket(), keepalive=60)
    connection._buffer.extend(bytes([PUBLISH]) + _length(n) + bytes(n))
    header, body = connection._take()
    assert header == PUBLISH
    assert len(body) == n
    assert connection._buffer == bytearray()


async def test_split_and_joined_frames() -> None:
    ws = _FakeWebSocket()
    connection = MqttConnection(ws, keepalive=60)
    report = b'{"pow": 1, "pad": "' + b"x" * 300 + b'"}'
    first = _packet(PUBLISH, _string("things/a/state/reported") + report)
    second = _packet(PUBLISH | 0x02, _string("things/a/state/reported") + struct.pack("!H", 7) + b"{}")
    stream = first + second
    connection._buffer.extend(stream[:5])
    assert connection._take() is None
    connection._buffer.extend(stream[5:])
    while (packet := connection._take()) is not None:
        await connection._handle(*packet)
    assert list(connection._pending) == [
        ("things/a/state/reported", report),
        ("things/a/state/reported", b"{}"),
    ]
    assert ws.sent == [_packet(PUBACK, struct.pack("!H", 7))]
