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
        stamp = time.strftime('%H:%M:%S', time.localtime(line['ts']))
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
    with open(mpath) as f:
        manifest = json.load(f)
    files = {}
    for name in os.listdir(args.dir):
        path = os.path.join(args.dir, name)
        if name != 'manifest.json' and os.path.isfile(path):
            with open(path) as f:
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
    p = sub.add_parser('console', help="a guest's console ring")
    p.add_argument('id')
    p.add_argument('-n', type=int, default=50)
    sub.add_parser('claims', help='the claims table')

    args = parser.parse_args()
    if not args.token:
        sys.exit('jorm: no token (set JORM_TOKEN or pass --token)')
    globals()['cmd_' + args.cmd](args)


if __name__ == '__main__':
    main()
