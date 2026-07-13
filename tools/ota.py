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
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = os.environ.get('JORM_URL', 'http://jorm-c510.local').rstrip('/')
TOKEN = os.environ.get('JORM_TOKEN', '')


def gzip_file(src_path):
    out = src_path + '.gz'
    with open(src_path, 'rb') as f:
        raw = f.read()
    with open(out, 'wb') as f:
        f.write(gzip.compress(raw, 9))
    return out


def gzip_ui():
    return gzip_file(os.path.join(ROOT, 'supervisor', 'ui.html'))


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
    # The node has no compressor, so we ship the compressed copy too — both,
    # always. A stale .gz beside a fresh .html would serve yesterday's interface
    # to every browser and today's to nobody, which is the worst of both.
    yield gzip_ui(), 'ui.html.gz'
    jorm = os.path.join(ROOT, 'supervisor', 'jorm')
    for name in sorted(os.listdir(jorm)):
        if name.endswith('.py'):
            yield os.path.join(jorm, name), 'jorm/' + name
    # the shell surface: terminal + shell, gzipped only. Nothing fetches the
    # plain copy, and the node has 13 MB — but it does not have 13 MB to waste.
    web = os.path.join(ROOT, 'supervisor', 'web')
    for name in sorted(os.listdir(web)):
        if name.endswith(('.js', '.css')):
            yield gzip_file(os.path.join(web, name)), 'web/' + name + '.gz'


def main():
    if not TOKEN:
        sys.exit('set JORM_TOKEN')
    print('== node:', URL)
    before = api('GET', '/api/node')
    print('   up %.0f s, heap free %.1f MB'
          % (before['uptime_ms'] / 1000, before['heap_free'] / 1048576))

    print('== staging')
    n, refused = 0, []
    for local, remote in local_files():
        with open(local, 'rb') as f:
            body = f.read()
        try:
            api('PUT', '/api/node/files/' + remote, body, raw=True)
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
            # The running supervisor does not know this path is updatable yet —
            # the permission to ship these files is itself in the files we are
            # shipping. Push what it will take, then come back for the rest.
            refused.append(remote)
            continue
        print('   %-22s %6d B' % (remote, len(body)))
        n += 1
    if refused:
        print('   (this supervisor refuses %d path(s) it does not know yet: %s)'
              % (len(refused), ', '.join(refused)))
        print('   they go in a second pass, once the new one is running')

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
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(3)
        print('.', end='', flush=True)
        try:
            st = api('GET', '/api/node/update')
        except (urllib.error.URLError, OSError):
            continue      # a node mid-GC is not a node that failed

        if st['rolled_back']:
            print()
            sys.exit('REVERTED: %s' % st['rolled_back'])
        if not st['trial']:
            print()
            print('== confirmed. %d file(s) live on %s' % (n, before['hostname']))
            if refused:
                print()
                print('== second pass: the new supervisor knows these paths now')
                return main()
            return
    print()
    print('the node is up but has not confirmed yet — check GET /api/node/update')


if __name__ == '__main__':
    main()
