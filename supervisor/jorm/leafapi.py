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
import binascii
import gc
import json
import os
import socket

from jorm import clock
from jorm.fsutil import UnsafePath, ensure_dir, safe_relpath
from jorm.seal import Sealer

PORT = 5355
_NONCE_CAP = 32          # bound the issued-but-unused set; a nonce-hoarder can't grow it
_MUTATING = ('start', 'stop', 'restart', 'install')


def _sandbox(path):
    """A write target must be a jailed relative path under guests/ or the settings file —
    a management door is not an arbitrary-filesystem door."""
    safe_relpath(path)                        # no absolute, no '..', safe segments
    if not (path.startswith('guests/') or path == 'settings.json'):
        raise UnsafePath('writes are limited to guests/ and settings.json')
    return path


def _mkparents(path):
    cur = ''
    for seg in path.split('/')[:-1]:
        cur = cur + '/' + seg if cur else seg
        ensure_dir(cur)


def _put(req, uploads):
    """One chunk of a file. Offset-acked (stop-and-wait) so it survives UDP loss/reorder:
    the reply's `next` is the byte the sender should send; a duplicate or gap just re-acks
    the expected offset. Bytes land in a .part file and commit (rename) only when the whole
    file has arrived and its crc32 checks out."""
    try:
        path = _sandbox(req['path'])
    except (UnsafePath, KeyError, TypeError) as e:
        return {'ok': False, 'err': 'path: %s' % e}
    off, total = int(req.get('off', 0)), int(req.get('total', 0))
    try:
        chunk = binascii.a2b_base64(req.get('data', ''))
    except Exception:
        return {'ok': False, 'err': 'bad base64'}
    h = uploads.get(path)
    if off == 0:
        _mkparents(path)
        try:
            h = {'f': open(path + '.part', 'wb'), 'off': 0, 'total': total, 'crc': 0}
        except OSError as e:
            return {'ok': False, 'err': 'open: %s' % e}
        uploads[path] = h
    elif h is None:
        return {'ok': False, 'err': 'no upload in progress; send off=0 first', 'next': 0}
    if off != h['off']:
        return {'ok': True, 'path': path, 'next': h['off']}    # dup/reorder — re-ack
    h['f'].write(chunk)
    h['off'] += len(chunk)
    h['crc'] = binascii.crc32(chunk, h['crc'])
    if h['off'] < h['total']:
        return {'ok': True, 'path': path, 'next': h['off']}
    # last chunk: close, verify, commit
    h['f'].close()
    del uploads[path]
    got = h['crc'] & 0xffffffff
    want = req.get('crc')
    if want is not None and int(want) != got:
        try:
            os.remove(path + '.part')
        except OSError:
            pass
        return {'ok': False, 'err': 'crc mismatch (got %d, want %d)' % (got, want)}
    os.rename(path + '.part', path)
    return {'ok': True, 'path': path, 'next': h['off'], 'done': True, 'crc': got}


def _install(sup, req):
    """Register an already-uploaded bundle: validate its manifest and add it to the guest
    set, assigning a VMID-style number if it has none. Same per-guest logic as scan()."""
    from jorm.guests import Guest, GUESTS_DIR, FIRST_NUM
    from jorm.fsutil import safe_name, write_atomic
    gid = req.get('id')
    try:
        safe_name(gid)
    except Exception:
        return {'ok': False, 'err': 'bad guest id'}
    old = sup.guests.get(gid)
    if old is not None and old.state in ('running', 'unresponsive'):
        return {'ok': False, 'err': 'guest "%s" is running — stop it before reinstalling' % gid}
    guest = Guest(sup, gid)
    try:
        guest.load_manifest()                 # validates; raises on bad/missing
    except OSError:
        return {'ok': False, 'err': 'no bundle at %s/%s (upload it first)' % (GUESTS_DIR, gid)}
    except Exception as e:
        return {'ok': False, 'err': 'bad manifest: %s' % e}
    if guest.manifest.get('id') != gid:
        return {'ok': False, 'err': 'manifest id %r != %r' % (guest.manifest.get('id'), gid)}
    guest.load_num()
    if guest.num is None:
        nums = [g.num for g in sup.guests.values() if g.num]
        guest.num = max(nums) + 1 if nums else FIRST_NUM
        write_atomic(guest.dir + '/.num', str(guest.num))
    sup.guests[gid] = guest
    return {'ok': True, 'id': gid, 'num': guest.num, 'state': guest.state}


def _sockaddr(host, port):
    # The unix port wants a resolved sockaddr at bind(); getaddrinfo gives one and behaves
    # identically on the ESP32, so it is the portable idiom (same as cluster.py).
    return socket.getaddrinfo(host, port)[0][-1]


async def _dispatch(node, sup, req, nonces, uploads):
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
    if op == 'put':
        # chunked file upload — idempotent staging (offset-acked), so no nonce; the
        # commit that matters, `install`, is nonce-guarded.
        return _put(req, uploads)
    if op == 'nonce':
        # A single-use challenge, so a captured mutating datagram can't be replayed. Read
        # ops need none (they have no side effect); mutating ops must carry a fresh one.
        n = os.urandom(6).hex()
        nonces.append(n)
        while len(nonces) > _NONCE_CAP:
            nonces.pop(0)
        return {'ok': True, 'nonce': n}
    if op == 'config':
        # Typed on purpose: rendering a control needs the SCHEMA (which widgets, their
        # ranges), which a raw pub can't carry, and a write goes through guestcfg's
        # validation + live/restart machinery. A read is side-effect-free; a `set` is a
        # persistent change, so it is nonce-guarded like the other mutating ops.
        from jorm import guestcfg
        g = sup.guests.get(req.get('guest'))
        if g is None:
            return {'ok': False, 'err': 'no such guest: %s' % req.get('guest')}
        if 'set' in req:
            if req.get('nonce') not in nonces:
                return {'ok': False, 'err': 'stale or missing nonce — fetch one with op=nonce'}
            nonces.remove(req['nonce'])
            try:
                result = guestcfg.write(g, req['set'])
            except Exception as e:
                return {'ok': False, 'err': 'config: %s' % e}
            return {'ok': True, 'config': guestcfg.view(g), 'result': result}
        return {'ok': True, 'config': guestcfg.view(g)}
    if op == 'pub':
        # The generic pipe: publish an arbitrary message to this leaf's local bus, exactly
        # as a bus WebSocket client may on a full node — so any guest command (a brightness
        # set, a banner, a scroll) needs no verb of its own. $-rooted topics are refused: the
        # door widens who may COMMAND a guest, never who may forge the node's own $sys state.
        topic = req.get('topic')
        if not isinstance(topic, str) or not topic or topic.startswith('$'):
            return {'ok': False, 'err': 'pub needs a non-$ topic'}
        try:
            delivered = sup.bus.publish(topic, req.get('msg'),
                                        retain=bool(req.get('retain')), owner='door')
        except Exception as e:
            return {'ok': False, 'err': 'pub: %s' % e}
        return {'ok': True, 'topic': topic, 'delivered': delivered}
    if op in _MUTATING:
        n = req.get('nonce')
        if n not in nonces:
            return {'ok': False, 'err': 'stale or missing nonce — fetch one with op=nonce'}
        nonces.remove(n)                           # consume: a nonce works exactly once
        if op == 'install':
            return _install(sup, req)
        gid = req.get('guest')
        g = sup.guests.get(gid)
        if g is None:
            return {'ok': False, 'err': 'no such guest: %s' % gid}
        try:
            if op == 'stop':
                await g.stop()
            elif op == 'start':
                await g.start()
            else:                                  # restart
                if g.state in ('running', 'unresponsive'):
                    await g.stop()
                await g.start()
        except Exception as e:
            return {'ok': False, 'err': '%s %s: %s' % (op, gid, e), 'state': g.state}
        return {'ok': True, 'op': op, 'guest': gid, 'state': g.state}
    return {'ok': False, 'err': 'unknown op: %s' % op}


async def serve(node, sup):
    seal = Sealer(node.token)
    nonces = []                                  # issued-but-unused single-use challenges
    uploads = {}                                 # path -> in-progress chunked upload
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
                reply = await _dispatch(node, sup, req, nonces, uploads)
            except Exception as e:               # a bad request must not kill the door
                reply = {'ok': False, 'err': 'dispatch: %s' % e}
            try:
                s.sendto(seal.seal(json.dumps(reply).encode()), addr)
            except OSError:
                pass
        await asyncio.sleep_ms(60)
