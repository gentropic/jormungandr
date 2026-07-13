#!/usr/bin/env python3
"""jorm — API client and test harness for a jormungandr node (spec §11).

Configuration: --url/--token flags, or JORM_URL / JORM_TOKEN env vars.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Guest sources, panel labels and console lines are UTF-8 — °C, ▸, the block
# glyphs a /lib sparkline draws with. Windows defaults stdout to cp1252, which
# turns a status line into a crash, and reading a source file as cp1252 turns
# °C into Â°C on the way to the node. Say UTF-8 once, out loud, everywhere.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

DEFAULT_URL = os.environ.get('JORM_URL', 'http://jorm-c510.local')


def request(args, method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(args.url.rstrip('/') + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + args.token)
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode() if raw else json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e)
        except ValueError:
            detail = {'error': e.reason}
        sys.exit('jorm: %s %s -> %d %s' % (method, path, e.code, detail.get('error', '')))
    except OSError as e:
        sys.exit('jorm: cannot reach %s (%s)' % (args.url, e))


def print_kv(d):
    width = max(len(k) for k in d)
    for key in sorted(d):
        print('%-*s  %s' % (width, key, d[key]))


def print_lines(lines):
    for line in lines:
        # ts is None until the node's clock is set; show uptime, not a fake date
        stamp = (time.strftime('%H:%M:%S', time.localtime(line['ts'])) if line.get('ts')
                 else '+%07.1f' % line.get('up', 0))
        text = line['text'].rstrip('\n').replace('\n', '\n               ')
        print('%s %-5s %s' % (stamp, line['level'], text))


def cmd_node(args):
    print_kv(request(args, 'GET', '/api/node'))


def cmd_log(args):
    print_lines(request(args, 'GET', '/api/node/log?n=%d' % args.n)['lines'])


def cmd_reboot(args):
    request(args, 'POST', '/api/node/reboot')
    print('rebooting')


def cmd_create(args):
    mpath = os.path.join(args.dir, 'manifest.json')
    if not os.path.isfile(mpath):
        sys.exit('jorm: %s has no manifest.json' % args.dir)
    with open(mpath, encoding='utf-8') as f:
        manifest = json.load(f)
    files = {}
    for name in os.listdir(args.dir):
        path = os.path.join(args.dir, name)
        if name != 'manifest.json' and os.path.isfile(path):
            with open(path, encoding='utf-8') as f:
                files[name] = f.read()
    got = request(args, 'POST', '/api/guests', {'manifest': manifest, 'files': files})
    print('created guest "%s" (%d files)' % (got['id'], len(files)))


def cmd_guests(args):
    rows = request(args, 'GET', '/api/guests')
    if not rows:
        print('no guests installed')
        return
    print('%-24s %-12s %s' % ('ID', 'STATE', 'STATUS'))
    for g in rows:
        state = g['state'] + (' [suspected]' if g.get('suspected') else '')
        print('%-24s %-12s %s' % (g['id'], state, g.get('status', '')))


def cmd_guest(args):
    detail = request(args, 'GET', '/api/guests/%s' % args.id)
    traceback = detail.pop('traceback', None)
    detail['manifest'] = json.dumps(detail.get('manifest'))
    detail['claims'] = json.dumps(detail.get('claims'))
    print_kv(detail)
    if traceback:
        print('\nlast traceback:\n' + traceback.rstrip())


def cmd_start(args):
    print('state:', request(args, 'POST', '/api/guests/%s/start' % args.id)['state'])


def cmd_stop(args):
    got = request(args, 'POST', '/api/guests/%s/stop?grace_ms=%d' % (args.id, args.grace))
    print('state:', got['state'])


def cmd_restart(args):
    print('state:', request(args, 'POST', '/api/guests/%s/restart' % args.id)['state'])


def cmd_rm(args):
    request(args, 'DELETE', '/api/guests/%s' % args.id)
    print('removed', args.id)


def cmd_console(args):
    print_lines(request(args, 'GET', '/api/guests/%s/console?n=%d' % (args.id, args.n))['lines'])


# -- a minimal WebSocket client (stdlib only) for the bus bridge --------------

def ws_connect(url, path, token):
    import base64
    import socket
    from urllib.parse import urlparse
    u = urlparse(url)
    sock = socket.create_connection((u.hostname, u.port or 80), timeout=30)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(('GET %s?token=%s HTTP/1.1\r\nHost: %s\r\n'
                  'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                  'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n'
                  % (path, token, u.netloc, key)).encode())
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = sock.recv(256)
        if not chunk:
            sys.exit('jorm: connection closed during WS handshake')
        buf += chunk
    if b' 101 ' not in buf.split(b'\r\n', 1)[0]:
        sys.exit('jorm: WS upgrade refused: %s' % buf.split(b'\r\n', 1)[0].decode())
    return sock


def ws_send_text(sock, text):
    payload = text.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])
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
    sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def ws_recv_text(sock):
    def read(n):
        data = b''
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data
    while True:
        head = read(2)
        opcode, n = head[0] & 0x0F, head[1] & 0x7F
        if n == 126:
            n = int.from_bytes(read(2), 'big')
        elif n == 127:
            n = int.from_bytes(read(8), 'big')
        if head[1] & 0x80:
            read(4)  # servers don't mask; tolerate it anyway
        payload = read(n)
        if opcode == 8:
            raise EOFError('closed')
        if opcode == 1:
            return payload.decode()


def cmd_bus(args):
    sock = ws_connect(args.url, '/api/bus', args.token)
    ws_send_text(sock, json.dumps({'op': 'sub', 'filters': args.filters or ['#']}))
    seen = 0
    try:
        while args.count == 0 or seen < args.count:
            frame = json.loads(ws_recv_text(sock))
            if 'topic' in frame:
                print('%-32s %s' % (frame['topic'], json.dumps(frame['msg'])))
                seen += 1
            elif 'error' in frame:
                sys.exit('jorm: bus: %s' % frame['error'])
    except (EOFError, KeyboardInterrupt):
        pass


def cmd_pub(args):
    try:
        msg = json.loads(args.msg)
    except ValueError:
        msg = args.msg  # bare strings are fine, they're JSON values too
    request(args, 'POST', '/api/bus/publish',
            {'topic': args.topic, 'msg': msg, 'retain': args.retain})
    print('published', args.topic)


def cmd_retained(args):
    table = request(args, 'GET', '/api/bus/retained')
    if not table:
        print('retained table empty')
    for topic in sorted(table):
        print('%-32s %s' % (topic, json.dumps(table[topic])))


def cmd_config(args):
    if not args.set:
        got = request(args, 'GET', '/api/guests/%s/config' % args.id)
        for f in got.get('schema') or []:
            key = f['key']
            marks = []
            if f.get('live'):
                marks.append('live')
            if key in got.get('pending_restart', []):
                marks.append('PENDING RESTART')
            print('%-16s %-10s %s' % (key, json.dumps(got['values'].get(key)),
                                      (' · '.join(marks))))
        for key in got.get('undeclared', []):
            print('%-16s %-10s %s' % (key, json.dumps(got['values'].get(key)),
                                      'undeclared — preserved'))
        if got.get('schema') is None:
            print('(no config schema declared)')
        return
    updates = {}
    for pair in args.set:
        key, _, raw = pair.partition('=')
        try:
            updates[key] = json.loads(raw)
        except ValueError:
            updates[key] = raw
    got = request(args, 'PUT', '/api/guests/%s/config' % args.id, updates)
    print('applied live: %s' % (', '.join(got['applied_live']) or '—'))
    print('pending restart: %s' % (', '.join(got['pending_restart']) or '—'))


def cmd_lib(args):
    if args.install:
        name = os.path.basename(args.install)
        with open(args.install, 'rb') as f:
            body = f.read()
        import urllib.request
        req = urllib.request.Request(
            args.url.rstrip('/') + '/api/lib/' + name + ('?force' if args.force else ''),
            data=body, method='PUT')
        req.add_header('Authorization', 'Bearer ' + args.token)
        try:
            got = json.load(urllib.request.urlopen(req, timeout=15))
        except urllib.error.HTTPError as e:
            sys.exit('jorm: %s' % json.load(e).get('error', e.reason))
        print('installed %s' % got['installed'])
        return
    rows = request(args, 'GET', '/api/lib')
    if not rows:
        print('the library store is empty')
    for r in rows:
        users = ', '.join(r['imported_by']) or '—'
        print('%-20s %6d B   imported by: %s' % (r['name'], r['bytes'], users))


def cmd_claims(args):
    table = request(args, 'GET', '/api/claims')
    if table.get('reserved_pins'):
        print('reserved pins:', ', '.join(map(str, table['reserved_pins'])))
    if not table['pins']:
        print('no pins claimed')
    for p in table['pins']:
        print('pin %-3d %-10s %s' % (p['pin'], p['mode'], ', '.join(p['owners'])))


def main():
    parser = argparse.ArgumentParser(prog='jorm', description=__doc__)
    parser.add_argument('--url', default=DEFAULT_URL)
    parser.add_argument('--token', default=os.environ.get('JORM_TOKEN', ''))
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('node', help='node info: board, heap, uptime')
    p = sub.add_parser('log', help="the supervisor's own log ring")
    p.add_argument('-n', type=int, default=50)
    sub.add_parser('reboot', help='reboot the node')
    p = sub.add_parser('create', help='install a guest bundle from a directory')
    p.add_argument('dir')
    sub.add_parser('guests', help='list installed guests')
    for name in ('guest', 'start', 'restart', 'rm'):
        p = sub.add_parser(name)
        p.add_argument('id')
    p = sub.add_parser('stop')
    p.add_argument('id')
    p.add_argument('--grace', type=int, default=2000)
    p = sub.add_parser('config', help="get/set a guest's configuration")
    p.add_argument('id')
    p.add_argument('set', nargs='*', metavar='key=value')
    p = sub.add_parser('console', help="a guest's console ring")
    p.add_argument('id')
    p.add_argument('-n', type=int, default=50)
    p = sub.add_parser('lib', help='the shared library store')
    p.add_argument('--install', metavar='FILE.py')
    p.add_argument('--force', action='store_true')
    sub.add_parser('claims', help='the claims table')
    p = sub.add_parser('bus', help='watch bus traffic live (WS bridge)')
    p.add_argument('filters', nargs='*', help="topic filters (default: '#')")
    p.add_argument('-c', '--count', type=int, default=0, help='exit after N messages')
    p = sub.add_parser('pub', help='inject a message into the bus')
    p.add_argument('topic')
    p.add_argument('msg', help='JSON value (bare strings ok)')
    p.add_argument('--retain', action='store_true')
    sub.add_parser('retained', help='the retained message table')

    args = parser.parse_args()
    if not args.token:
        sys.exit('jorm: no token (set JORM_TOKEN or pass --token)')
    globals()['cmd_' + args.cmd](args)


if __name__ == '__main__':
    main()
