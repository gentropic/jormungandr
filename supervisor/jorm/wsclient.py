"""A minimal async WebSocket client (one §4: bus bridging needs one on the node).

The node already SERVES a WebSocket at /api/bus (microdot). To bridge, a node must
also be a WS CLIENT — reach into a peer's /api/bus and pull a slice of its bus. The
CLI hand-rolls a synchronous client; this is the async twin, over asyncio streams so
it never blocks the event loop.

Only what a bridge needs: connect, send one text frame (the subscribe), then read
text frames forever. A client MUST mask what it sends and a server MUST NOT mask what
it sends, so the send path masks and the receive path does not expect a mask.
"""
import asyncio
import binascii
import json
import os


async def _read_exactly(reader, n):
    buf = b''
    while len(buf) < n:
        chunk = await reader.read(n - len(buf))
        if not chunk:
            raise EOFError('websocket closed')
        buf += chunk
    return buf


def _text_frame(text):
    payload = text.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])          # FIN + text opcode
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += n.to_bytes(2, 'big')
    else:
        header.append(0x80 | 127)
        header += n.to_bytes(8, 'big')
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


class WsClient:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def send(self, text):
        self.writer.write(_text_frame(text))
        await self.writer.drain()

    async def recv(self):
        """Return the next text message, or raise EOFError on close."""
        while True:
            head = await _read_exactly(self.reader, 2)
            opcode = head[0] & 0x0F
            n = head[1] & 0x7F
            if n == 126:
                n = int.from_bytes(await _read_exactly(self.reader, 2), 'big')
            elif n == 127:
                n = int.from_bytes(await _read_exactly(self.reader, 8), 'big')
            if head[1] & 0x80:              # servers should not mask; tolerate it
                await _read_exactly(self.reader, 4)
            payload = await _read_exactly(self.reader, n) if n else b''
            if opcode == 0x8:               # close
                raise EOFError('websocket closed by peer')
            if opcode == 0x9:               # ping — the frame is ours to ignore here
                continue
            if opcode == 0x1:               # text
                return payload.decode()
            # binary/continuation are not part of the bus protocol; skip them

    async def close(self):
        try:
            self.writer.close()
        except Exception:
            pass


async def connect(host, port, path, token):
    """Open a WS connection and return a WsClient past the handshake.

    Raises OSError if the socket won't open and EOFError/ValueError if the peer does
    not complete the upgrade — the bridge treats any of them as "try again later".
    """
    reader, writer = await asyncio.open_connection(host, port)
    key = binascii.b2a_base64(os.urandom(16)).decode().strip()
    sep = '&' if '?' in path else '?'
    writer.write((
        'GET %s%stoken=%s HTTP/1.1\r\nHost: %s:%d\r\n'
        'Upgrade: websocket\r\nConnection: Upgrade\r\n'
        'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n'
        % (path, sep, token, host, port, key)).encode())
    await writer.drain()

    # Read headers up to the blank line. Whatever follows is already WebSocket, so
    # readline until we see the terminator — the same first-frame-in-the-same-segment
    # care the CLI learned, except asyncio's readline hands us the boundary cleanly.
    status = await reader.readline()
    if b' 101 ' not in status:
        raise ValueError('ws upgrade refused: %s' % status.decode().strip())
    while True:
        line = await reader.readline()
        if line in (b'\r\n', b'\n', b''):
            break
    return WsClient(reader, writer)
