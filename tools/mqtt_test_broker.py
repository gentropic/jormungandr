#!/usr/bin/env python3
"""Minimal throwaway MQTT 3.1.1 broker (QoS 0) for testing the jorm mqtt bridge — NOT the live
broker. Handles CONNECT/SUBSCRIBE/PUBLISH/PINGREQ, routes with +/# wildcards, and logs every
PUBLISH to stderr so a test can assert what the bridge sent. A `pub` subcommand acts as a client
to inject a message (to drive the bridge's inbound path).

    python3 mqtt_test_broker.py <port>                 # run the broker
    python3 mqtt_test_broker.py pub <port> <topic> <payload>
"""
import asyncio
import socket
import sys

clients = []


def match(filt, topic):
    f, t = filt.split('/'), topic.split('/')
    i = 0
    while i < len(f):
        if f[i] == '#':
            return True
        if i >= len(t):
            return False
        if f[i] != '+' and f[i] != t[i]:
            return False
        i += 1
    return i == len(t)


def _rl(n):
    out = b''
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | 0x80]) if n else bytes([b])
        if not n:
            return out


def enc_publish(topic, payload):
    tb = topic.encode()
    var = len(tb).to_bytes(2, 'big') + tb + payload
    return b'\x30' + _rl(len(var)) + var


async def read_len(reader):
    n = sh = 0
    while True:
        b = (await reader.readexactly(1))[0]
        n |= (b & 0x7F) << sh
        if not b & 0x80:
            return n
        sh += 7


async def handle(reader, writer):
    subs = set()
    entry = [writer, subs]
    clients.append(entry)
    try:
        while True:
            op = (await reader.readexactly(1))[0]
            body = await reader.readexactly(await read_len(reader))
            t = op & 0xF0
            if t == 0x10:                                   # CONNECT
                writer.write(b'\x20\x02\x00\x00')
            elif t == 0x80:                                 # SUBSCRIBE
                pid, i, grants = body[0:2], 2, b''
                while i < len(body):
                    tl = (body[i] << 8) | body[i + 1]
                    i += 2
                    subs.add(body[i:i + tl].decode())
                    i += tl + 1                             # skip topic + its qos byte
                    grants += b'\x00'
                writer.write(b'\x90' + bytes([2 + len(grants)]) + pid + grants)
            elif t == 0x30:                                 # PUBLISH
                qos = (op >> 1) & 3
                tl = (body[0] << 8) | body[1]
                topic = body[2:2 + tl].decode()
                payload = body[2 + tl + (2 if qos else 0):]
                sys.stderr.write('PUB %s %s\n' % (topic, payload.decode('utf-8', 'replace')))
                sys.stderr.flush()
                pkt = enc_publish(topic, payload)
                for w, s in list(clients):
                    if any(match(f, topic) for f in s):
                        try:
                            w.write(pkt)
                            await w.drain()
                        except Exception:
                            pass
            elif t == 0xC0:                                 # PINGREQ
                writer.write(b'\xd0\x00')
            elif t == 0xE0:                                 # DISCONNECT
                break
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        if entry in clients:
            clients.remove(entry)
        try:
            writer.close()
        except Exception:
            pass


async def run_broker(port):
    srv = await asyncio.start_server(handle, '127.0.0.1', port)
    sys.stderr.write('broker listening on %d\n' % port)
    sys.stderr.flush()
    async with srv:
        await srv.serve_forever()


def client_pub(port, topic, payload):
    s = socket.create_connection(('127.0.0.1', port))
    s.sendall(b'\x10\x0c\x00\x04MQTT\x04\x02\x00\x00\x00\x00')   # CONNECT, clean, empty id
    s.recv(4)                                                    # CONNACK
    s.sendall(enc_publish(topic, payload.encode()))
    s.sendall(b'\xe0\x00')                                       # DISCONNECT
    s.close()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'pub':
        client_pub(int(sys.argv[2]), sys.argv[3], sys.argv[4])
    else:
        asyncio.run(run_broker(int(sys.argv[1]) if len(sys.argv) > 1 else 1883))
