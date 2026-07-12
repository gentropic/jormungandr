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


def request(args, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(args.url.rstrip('/') + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + args.token)
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e)
        except ValueError:
            detail = {'error': e.reason}
        sys.exit('jorm: %s %s -> %d %s' % (method, path, e.code, detail.get('error', '')))
    except OSError as e:
        sys.exit('jorm: cannot reach %s (%s)' % (args.url, e))


def cmd_node(args):
    info = request(args, 'GET', '/api/node')
    width = max(len(k) for k in info)
    for key in sorted(info):
        print('%-*s  %s' % (width, key, info[key]))


def cmd_log(args):
    for line in request(args, 'GET', '/api/node/log?n=%d' % args.n)['lines']:
        stamp = time.strftime('%H:%M:%S', time.localtime(line['ts']))
        print('%s %-5s %s' % (stamp, line['level'], line['text']))


def cmd_reboot(args):
    request(args, 'POST', '/api/node/reboot')
    print('rebooting')


def main():
    parser = argparse.ArgumentParser(prog='jorm', description=__doc__)
    parser.add_argument('--url', default=DEFAULT_URL)
    parser.add_argument('--token', default=os.environ.get('JORM_TOKEN', ''))
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('node', help='node info: board, heap, uptime')
    p_log = sub.add_parser('log', help="the supervisor's own log ring")
    p_log.add_argument('-n', type=int, default=50)
    sub.add_parser('reboot', help='reboot the node')

    args = parser.parse_args()
    if not args.token:
        sys.exit('jorm: no token (set JORM_TOKEN or pass --token)')
    {'node': cmd_node, 'log': cmd_log, 'reboot': cmd_reboot}[args.cmd](args)


if __name__ == '__main__':
    main()
