"""Sealed-UDP leaf management (datagram-leaf — the roadmap, spec_inbox/ROADMAP-sealed-udp-mgmt).

A leaf-host has no HTTP server, so management used to ride the bus uplink — a held-open
WebSocket that proved the wrong transport for a small node: it fights the guest for the
single-core event loop (measured on the WROOM) and needs a live route to the flagship.
This is the out-of-band door instead: ONE UDP socket, nothing held open, up whenever the
leaf is on WiFi and independent of the bus. Every datagram is sealed with the cluster token
(jorm.seal); an unsealed or wrong-token datagram is dropped with no reply, so this widens
who may knock, never who may enter.

Slice 1: ping / state / log. start/stop/restart, a replay guard, and chunked file upload
come next (see the roadmap).
"""
import asyncio
import gc
import json
import socket

from jorm import clock
from jorm.seal import Sealer

PORT = 5355


def _sockaddr(host, port):
    # The unix port wants a resolved sockaddr at bind(); getaddrinfo gives one and behaves
    # identically on the ESP32, so it is the portable idiom (same as cluster.py).
    return socket.getaddrinfo(host, port)[0][-1]


def _dispatch(node, sup, req):
    op = req.get('op')
    if op == 'ping':
        return {'ok': True, 'name': node.hostname, 'board': node.board_name(), 'ip': node.ip}
    if op == 'state':
        gc.collect()
        return {'ok': True, 'name': node.hostname, 'ip': node.ip,
                'guests': [{'id': g.id, 'state': g.state} for g in sup.guests.values()],
                'heap_free': gc.mem_free(), 'synced': clock.status()['synced']}
    if op == 'log':
        return {'ok': True, 'log': node.log.tail(int(req.get('n', 20)))}
    return {'ok': False, 'err': 'unknown op: %s' % op}


async def serve(node, sup):
    seal = Sealer(node.token)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(_sockaddr('0.0.0.0', PORT))
    s.settimeout(0)                              # non-blocking: drain then yield, never park
    node.log.append('sys', 'leaf-mgmt: sealed-UDP management on :%d' % PORT)
    while True:
        drained = 0
        while drained < 16:                      # bound the drain so a flood can't starve us
            try:
                data, addr = s.recvfrom(2048)
            except OSError:
                break                            # EAGAIN — nothing waiting
            drained += 1
            pt = seal.unseal(data)
            if pt is None:
                continue                         # not authentic — silent drop
            try:
                req = json.loads(pt)
            except (ValueError, TypeError):
                continue
            try:
                reply = _dispatch(node, sup, req)
            except Exception as e:               # a bad request must not kill the door
                reply = {'ok': False, 'err': 'dispatch: %s' % e}
            try:
                s.sendto(seal.seal(json.dumps(reply).encode()), addr)
            except OSError:
                pass
        await asyncio.sleep_ms(60)
