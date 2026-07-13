#!/usr/bin/env python3
"""jorm — API client and test harness for a jormungandr node (spec §11).

Which node:  -n/--node <name> from the registry (jorm nodes add …), else
             $JORM_NODE, else the registry default, else --url/--token or
             $JORM_URL/$JORM_TOKEN. An explicit --url always wins, so a script
             can override a person's saved default.

    jorm nodes add c510 http://jorm-c510.local --token <t>
    jorm nodes                       # which boards, and which is default
    jorm -n c510 guests
    jorm -n c510 shell -c 'ls /guests'
    jorm -n c510 console parrot -a   # attach: read AND type
    jorm open                        # whichever board answers fastest, in a browser
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


# ── the node registry ──────────────────────────────────────────────────────
# A board is a name, not a URL and a 32-character token you retype. Without this
# a second node is technically supported and practically never used, which is the
# same as not supporting it.

def nodes_path():
    base = (os.environ.get('APPDATA') if os.name == 'nt'
            else os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'))
    return os.path.join(base, 'jorm', 'nodes.json')


def nodes_load():
    try:
        with open(nodes_path(), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {'nodes': {}, 'default': None}


def nodes_save(reg):
    path = nodes_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(reg, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)
    if os.name != 'nt':
        os.chmod(path, 0o600)   # it holds bearer tokens


def resolve_node(args):
    """Work out which node we are talking to, and say so when we cannot.

    Order: --url wins (explicit beats stored), then --node, then JORM_NODE, then
    the registry default, then JORM_URL/JORM_TOKEN. The precedence exists so that
    a script can always override a person's saved default.
    """
    if args.url != DEFAULT_URL or (os.environ.get('JORM_URL') and not args.node):
        return args.url, args.token

    reg = nodes_load()
    name = args.node or os.environ.get('JORM_NODE') or reg.get('default')
    if name:
        entry = reg['nodes'].get(name)
        if not entry:
            known = ', '.join(sorted(reg['nodes'])) or 'none'
            sys.exit('jorm: no node named "%s" (known: %s)' % (name, known))
        return entry['url'], args.token or entry.get('token', '')
    return args.url, args.token


def probe_node(name, url, token, samples=3):
    """Ask a node how it is, and time how long it took to answer.

    Two different numbers matter here and they are not the same number. RSSI is
    how well the NODE hears the access point. RTT is how long YOUR browser will
    wait for a click to do something. A board with a superb radio on the far side
    of a congested AP can still be the worst node to open. Latency is what a
    person feels, so latency leads; the radio breaks ties.
    """
    rtts = []
    info = None
    for _ in range(samples):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(
                url + '/api/node', headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(req, timeout=2) as r:
                info = json.load(r)
        except Exception as e:
            why = getattr(e, 'reason', None) or e.__class__.__name__
            return {'name': name, 'url': url, 'token': token, 'up': False, 'why': str(why)}
        rtts.append((time.monotonic() - t0) * 1000)
    rtts.sort()
    return {'name': name, 'url': url, 'token': token, 'up': True,
            'rtt': rtts[len(rtts) // 2], 'info': info}   # median: one slow packet is weather


def cmd_open(args):
    """Open the healthiest node's UI in a browser.

    Every node serves the whole cluster, so which one you open is a question of
    who answers fastest, not of who is in charge. There is no elected front door
    and nothing to fail over.
    """
    import webbrowser
    from concurrent.futures import ThreadPoolExecutor

    reg = nodes_load()
    if args.name:
        e = reg['nodes'].get(args.name)
        if not e:
            sys.exit('jorm: no node named "%s"' % args.name)
        cands = [(args.name, e['url'], e.get('token', ''))]
    else:
        cands = [(n, e['url'], e.get('token', '')) for n, e in sorted(reg['nodes'].items())]
    if not cands:
        sys.exit('jorm: no nodes registered — jorm nodes add <name> <url> --token <t>')

    with ThreadPoolExecutor(max_workers=8) as ex:
        found = list(ex.map(lambda c: probe_node(*c), cands))

    up = sorted([r for r in found if r['up']], key=lambda r: r['rtt'])
    for r in found:
        if not r['up']:
            print('  %-10s %-28s down (%s)' % (r['name'], r['url'], r['why']))
    for r in up:
        i = r['info']
        rssi = i.get('rssi')
        print('  %-10s %-28s %6.0f ms  %s  heap %4.1f MB free'
              % (r['name'], r['url'], r['rtt'],
                 ('%4d dBm' % rssi) if rssi is not None else '   wired',
                 i['heap_free'] / 1048576))
    if not up:
        sys.exit('jorm: nothing answered')

    best = up[0]
    close = [r for r in up if r['rtt'] <= best['rtt'] * 1.2]
    if len(close) > 1:
        # Too close to call on latency — the difference is noise on a LAN. Let the
        # radio decide, since a weak link is a slow node that has not been slow yet.
        close.sort(key=lambda r: -(r['info'].get('rssi') or -100))
        best = close[0]
    print()

    url = best['url']
    try:
        req = urllib.request.Request(
            url + '/api/auth/ticket', data=b'',
            headers={'Authorization': 'Bearer ' + best['token']}, method='POST')
        with urllib.request.urlopen(req, timeout=3) as r:
            url = url + '/#t=' + json.load(r)['ticket']
    except Exception:
        # An older node cannot mint one. Open it anyway — you get the login form,
        # which is a worse morning but not a broken one.
        print('  (this node cannot mint a ticket — you will get the login form)')

    if args.print_url:
        print(url)
        return
    print('opening %s%s' % (best['name'],
                            '' if len(up) == 1 else ' — fastest of %d' % len(up)))
    webbrowser.open(url)


def cmd_nodes(args):
    reg = nodes_load()
    op = args.op

    if op in (None, 'list'):
        if not reg['nodes']:
            print('no nodes registered. add one:')
            print('  jorm nodes add c510 http://jorm-c510.local --token <token>')
            return
        for name in sorted(reg['nodes']):
            e = reg['nodes'][name]
            mark = '*' if name == reg.get('default') else ' '
            print('%s %-12s %-30s %s' % (mark, name, e['url'],
                                         'token saved' if e.get('token') else 'NO TOKEN'))
        print()
        print('  * = default · %s' % nodes_path())
        return

    if op == 'add':
        if not args.name or not args.node_url:
            sys.exit('jorm nodes add <name> <url> [--token T]')
        reg['nodes'][args.name] = {'url': args.node_url.rstrip('/'),
                                   'token': args.token or ''}
        if not reg.get('default'):
            reg['default'] = args.name
        nodes_save(reg)
        print('added %s -> %s%s' % (args.name, args.node_url,
                                    '' if args.token else '  (no token — pass --token)'))
        return

    if op == 'rm':
        if reg['nodes'].pop(args.name, None) is None:
            sys.exit('jorm: no node named "%s"' % args.name)
        if reg.get('default') == args.name:
            reg['default'] = next(iter(sorted(reg['nodes'])), None)
        nodes_save(reg)
        print('removed', args.name)
        return

    if op == 'use':
        if args.name not in reg['nodes']:
            sys.exit('jorm: no node named "%s"' % args.name)
        reg['default'] = args.name
        nodes_save(reg)
        print('default node is now', args.name)
        return

    sys.exit('jorm nodes [list | add <name> <url> | rm <name> | use <name>]')


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
    """Tail a guest's console — or attach to it.

    The node has taken console input since hal.console.input() landed, but this
    CLI could only ever watch a guest talk. A console you can only read is a log,
    which is the thing we already refused once in the UI; refusing it here too.
    """
    if not args.attach:
        print_lines(request(args, 'GET', '/api/guests/%s/console?n=%d'
                            % (args.id, args.n))['lines'])
        return

    import threading

    detail = request(args, 'GET', '/api/guests/%s' % args.id)
    if detail['state'] != 'running':
        sys.exit('jorm: guest "%s" is %s — nothing is listening' % (args.id, detail['state']))
    deaf = not detail.get('reads_input')

    sock = ws_connect(args.url, '/api/guests/%s/console/stream' % args.id, args.token)
    print('— attached to %s%s. ctrl-c to detach —'
          % (args.id, '' if not deaf else ' (it reads no input: hal.console.input())'))

    def pump():
        try:
            while True:
                line = json.loads(ws_recv_text(sock))
                print_lines([line])
        except (EOFError, OSError, ValueError):
            pass

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        for line in sys.stdin:
            line = line.rstrip('\n')
            if deaf:
                print('  (this guest reads no input)')
                continue
            request(args, 'POST', '/api/guests/%s/console' % args.id, {'line': line})
        # Piped stdin ends the instant the last line is sent — but the guest has
        # not answered yet. Detaching here would hang up on the reply we asked
        # for, which is the one thing an attached console must not do.
        if not sys.stdin.isatty():
            time.sleep(1.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
    print('— detached —')


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
    head, _, rest = buf.partition(b'\r\n\r\n')
    if b' 101 ' not in head.split(b'\r\n', 1)[0]:
        sys.exit('jorm: WS upgrade refused: %s' % head.split(b'\r\n', 1)[0].decode())
    # Whatever followed the blank line is already WebSocket. A server that answers
    # immediately — a console replaying its history, say — puts the first frames in
    # the same TCP segment as the handshake, and discarding them leaves the reader
    # starting mid-frame, which it then misparses as a CLOSE. /api/bus never showed
    # this because its first frame arrives in a packet of its own.
    return WebSock(sock, rest)


class WebSock:
    """A socket that remembers what it read too early."""

    def __init__(self, sock, buf=b''):
        self.sock = sock
        self.buf = buf

    def read(self, n):
        data = self.buf[:n]
        self.buf = self.buf[len(data):]
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def sendall(self, data):
        self.sock.sendall(data)

    def settimeout(self, t):
        self.sock.settimeout(t)

    def close(self):
        self.sock.close()


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
    read = sock.read
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


def cmd_leaf(args):
    """Manage a smart leaf's guests from a central node, over the bus.

    A leaf runs no HTTP server, so this doesn't call it — it publishes a command on
    THIS node's bus, the leaf (subscribed to cmd/leaf/<name>/#) executes it, and
    reports back on leaf/<name>/result. We subscribe to the result before publishing
    so the round trip can't race us.
    """
    name = args.name
    verb = args.verb or 'guests'

    if verb == 'guests':
        table = request(args, 'GET', '/api/bus/retained')
        if ('$sys/leaf/' + name) not in table:
            sys.exit('jorm: no leaf "%s" seen here — is it up and uplinked?' % name)
        prefix = 'leaf/%s/guest/' % name
        rows = {k[len(prefix):]: v for k, v in table.items() if k.startswith(prefix)}
        if not rows:
            print('leaf "%s": no guests installed' % name)
            return
        for gid in sorted(rows):
            st = rows[gid].get('state', '?') if isinstance(rows[gid], dict) else '?'
            print('%-20s %s' % (gid, st))
        return

    req = '%d' % int(time.time() * 1000)
    if verb == 'install':
        d = args.guest
        if not d or not os.path.isdir(d):
            sys.exit('jorm leaf %s install <bundle-dir>' % name)
        mpath = os.path.join(d, 'manifest.json')
        if not os.path.isfile(mpath):
            sys.exit('jorm: %s has no manifest.json' % d)
        manifest = json.load(open(mpath, encoding='utf-8'))
        files = {}
        for fn in os.listdir(d):
            p = os.path.join(d, fn)
            if fn != 'manifest.json' and os.path.isfile(p):
                files[fn] = open(p, encoding='utf-8').read()
        msg = {'req': req, 'manifest': manifest, 'files': files}
        label = 'install ' + manifest.get('id', '?')
    else:
        if not args.guest:
            sys.exit('jorm leaf %s %s <guest>' % (name, verb))
        msg = {'req': req, 'guest': args.guest}
        label = '%s %s' % (verb, args.guest)

    # subscribe to the result first, then publish the command
    sock = ws_connect(args.url, '/api/bus', args.token)
    try:
        ws_send_text(sock, json.dumps({'op': 'sub', 'filters': ['leaf/%s/result' % name]}))
        request(args, 'POST', '/api/bus/publish',
                {'topic': 'cmd/leaf/%s/%s' % (name, verb), 'msg': msg})
        print('sent "%s" to leaf "%s" — waiting for the result…' % (label, name))
        sock.settimeout(10)
        deadline = time.time() + 10
        while time.time() < deadline:
            frame = json.loads(ws_recv_text(sock))
            if frame.get('topic') != 'leaf/%s/result' % name:
                continue
            r = frame.get('msg') or {}
            if r.get('req') != req:
                continue
            if r.get('ok'):
                print('  ok: %s' % label)
            else:
                sys.exit('  failed: %s' % r.get('error'))
            return
        print('  no result within 10 s — the leaf may be offline')
    finally:
        sock.close()


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


def cmd_shell(args):
    """Drop into the node's shell — which is geas, not something written here.

    The verbs already exist twice: as these subcommands, and as geas builtins in
    supervisor/web/jorm-pack.js. A Python REPL would be the third, and the second
    to drift. So `jorm shell` does not implement a shell; it runs the one we have,
    with the same VFS and the same builtins the browser loads. geas is JavaScript,
    so this needs node — and if node is not here, the shell is still one URL away.
    """
    import shutil
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    entry = os.path.join(root, 'tools', 'geas-cli.mjs')
    node = shutil.which('node')
    if not node:
        sys.exit('jorm: geas is JavaScript, and it needs node. The same shell is '
                 'served by the node itself — open %s and pick the Shell tab.'
                 % args.url)

    env = dict(os.environ, JORM_URL=args.url, JORM_TOKEN=args.token)
    cmd = [node, entry] + (['-c', ' '.join(args.command)] if args.command else [])
    raise SystemExit(subprocess.call(cmd, env=env))


def cmd_claims(args):
    table = request(args, 'GET', '/api/claims')
    if table.get('reserved_pins'):
        print('reserved pins:', ', '.join(map(str, table['reserved_pins'])))
    if not table['pins']:
        print('no pins claimed')
    for p in table['pins']:
        print('pin %-3d %-10s %s' % (p['pin'], p['mode'], ', '.join(p['owners'])))


def cmd_usb(args):
    p = request(args, 'GET', '/api/usb')
    ifs = p['interfaces']
    if not ifs:
        print('no usb interfaces — no installed guest declares caps.usb')
    for i in ifs:
        flag = ('  ⚠ injects %s' % i.get('injects', 'host input')) if i.get('injector') else ''
        print('%-12s %s %-9s %d ep%s' % (i['guest'], i['kind'], i['spec'], i['endpoints'], flag))
    print('endpoints: %d / %d used' % (p['endpoints_used'], p['endpoints_total']))
    if p.get('error'):
        print('not enumerated:', p['error'])
    elif p.get('applied'):
        print('enumerated — the host sees this device now')
    if p.get('pending'):
        print('PENDING: the installed set changed since boot — '
              'reboot (or jorm reboot) to re-enumerate')


def main():
    parser = argparse.ArgumentParser(prog='jorm', description=__doc__)
    parser.add_argument('--url', default=DEFAULT_URL)
    parser.add_argument('--token', default=os.environ.get('JORM_TOKEN', ''))
    parser.add_argument('-n', '--node', help='a registered node (see: jorm nodes)')
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
    p = sub.add_parser('console', help="a guest's console (-a to attach and type into it)")
    p.add_argument('id')
    p.add_argument('-n', type=int, default=50)
    p.add_argument('-a', '--attach', action='store_true',
                   help='stream it live, and type lines into the guest')
    p = sub.add_parser('lib', help='the shared library store')
    p.add_argument('--install', metavar='FILE.py')
    p.add_argument('--force', action='store_true')
    p = sub.add_parser('shell',
                      help='drop into the node shell — geas (use -n to pick a board)')
    p.add_argument('-c', dest='command', nargs=argparse.REMAINDER,
                   help='run one command and exit')
    p = sub.add_parser('open', help='open the healthiest node in a browser')
    p.add_argument('name', nargs='?', help='a specific node (default: probe them all)')
    p.add_argument('--print', dest='print_url', action='store_true',
                   help='print the url instead of launching (ssh, headless)')

    p = sub.add_parser('nodes', help='the boards you manage (add · rm · use · list)')
    p.add_argument('op', nargs='?', choices=['list', 'add', 'rm', 'use'])
    p.add_argument('name', nargs='?')
    p.add_argument('node_url', nargs='?', metavar='URL')
    # --token is a global flag, so it cannot follow a subcommand; `nodes add`
    # needs its own, which is where anyone would naturally type it anyway
    p.add_argument('--token', default=os.environ.get('JORM_TOKEN', ''))
    sub.add_parser('claims', help='the claims table')
    sub.add_parser('usb', help='the USB device plan (interfaces, endpoints, §8)')
    p = sub.add_parser('leaf', help="manage a smart leaf's guests over the bus (two §7)")
    p.add_argument('name', help='the leaf hostname')
    p.add_argument('verb', nargs='?',
                   choices=['guests', 'start', 'stop', 'restart', 'rm', 'install'])
    p.add_argument('guest', nargs='?', help='guest id (or a bundle dir, for install)')
    p = sub.add_parser('bus', help='watch bus traffic live (WS bridge)')
    p.add_argument('filters', nargs='*', help="topic filters (default: '#')")
    p.add_argument('-c', '--count', type=int, default=0, help='exit after N messages')
    p = sub.add_parser('pub', help='inject a message into the bus')
    p.add_argument('topic')
    p.add_argument('msg', help='JSON value (bare strings ok)')
    p.add_argument('--retain', action='store_true')
    sub.add_parser('retained', help='the retained message table')

    args = parser.parse_args()

    if args.cmd in ('nodes', 'open'):
        return globals()['cmd_' + args.cmd](args)

    args.url, args.token = resolve_node(args)
    if not args.token:
        sys.exit('jorm: no token for %s.\n'
                 '      register the node once — jorm nodes add <name> <url> --token <t>\n'
                 '      or pass --token / set JORM_TOKEN.' % args.url)
    globals()['cmd_' + args.cmd](args)


if __name__ == '__main__':
    main()
