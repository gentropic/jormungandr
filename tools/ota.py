#!/usr/bin/env python3
"""Update a live node's supervisor over HTTP. No cable.

    JORM_URL=http://jorm-c510.local JORM_TOKEN=... python tools/ota.py

Stages main.py, boot.py, jorm/*.py and ui.html, then asks the node to apply them
and reboot. The next boot is a TRIAL: if the node does not come back and serve
the API, boot.py restores the previous supervisor by itself. So the worst case
of a bad push is a reboot, not a bricked board — which is the §1 promise ("the
node always comes back reachable") extended from guests to the supervisor.

This script waits for the node to return and reports whether the update was
confirmed or reverted. It does not lie about which.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = os.environ.get('JORM_URL', 'http://jorm-c510.local').rstrip('/')
TOKEN = os.environ.get('JORM_TOKEN', '')


def api(method, path, body=None, raw=False):
    data = body if raw else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + TOKEN)
    if data and not raw:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def local_files():
    yield os.path.join(ROOT, 'supervisor', 'main.py'), 'main.py'
    yield os.path.join(ROOT, 'supervisor', 'boot.py'), 'boot.py'
    yield os.path.join(ROOT, 'supervisor', 'ui.html'), 'ui.html'
    jorm = os.path.join(ROOT, 'supervisor', 'jorm')
    for name in sorted(os.listdir(jorm)):
        if name.endswith('.py'):
            yield os.path.join(jorm, name), 'jorm/' + name


def main():
    if not TOKEN:
        sys.exit('set JORM_TOKEN')
    print('== node:', URL)
    before = api('GET', '/api/node')
    print('   up %.0f s, heap free %.1f MB'
          % (before['uptime_ms'] / 1000, before['heap_free'] / 1048576))

    print('== staging')
    n = 0
    for local, remote in local_files():
        with open(local, 'rb') as f:
            body = f.read()
        api('PUT', '/api/node/files/' + remote, body, raw=True)
        print('   %-22s %6d B' % (remote, len(body)))
        n += 1

    print('== applying (the node reboots; the next boot is a trial)')
    api('POST', '/api/node/update')

    print('== waiting for the node to come back', end='', flush=True)
    deadline = time.time() + 90
    node = None
    while time.time() < deadline:
        time.sleep(2)
        print('.', end='', flush=True)
        try:
            node = api('GET', '/api/node')
            if node['uptime_ms'] < before['uptime_ms']:
                break        # it rebooted, and it is answering
        except (urllib.error.URLError, OSError):
            node = None
    print()
    if node is None:
        sys.exit('the node did not come back within 90 s. It will revert to the '
                 'previous supervisor on its next boot — press reset, or power-cycle.')

    print('== node is back. waiting for it to confirm the trial', end='', flush=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        time.sleep(3)
        print('.', end='', flush=True)
        st = api('GET', '/api/node/update')
        if st['rolled_back']:
            print()
            sys.exit('REVERTED: %s' % st['rolled_back'])
        if not st['trial']:
            print()
            print('== confirmed. %d file(s) live on %s' % (n, before['hostname']))
            return
    print()
    print('the node is up but has not confirmed yet — check GET /api/node/update')


if __name__ == '__main__':
    main()
