# jormungandr supervisor — boot: settings → wifi → mDNS name → bearer-token API.
# Runs unmodified on a real node and on the sim (sim/run.sh swaps machine/network).
import asyncio
import json
import sys
import time

import network

from jorm.node import Node
from jorm.api import create_app
from jorm.supervisor import Supervisor


def fail(msg):
    print('boot failed:', msg)
    sys.exit(1)


def load_settings():
    try:
        with open('settings.json') as f:
            settings = json.load(f)
    except OSError:
        fail('settings.json not found — provision the node at flash time (spec §6)')
    except ValueError:
        fail('settings.json is not valid JSON')
    if not settings.get('token'):
        fail('settings.json has no "token" — the API refuses to exist without one')
    return settings


def wifi_up(node):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    mac = wlan.config('mac')
    node.mac4 = '%02x%02x' % (mac[-2], mac[-1])
    try:
        network.hostname(node.hostname)
    except AttributeError:
        pass
    if not wlan.isconnected():
        ssid = node.settings.get('wifi', {}).get('ssid')
        if not ssid:
            fail('not connected and settings.json has no wifi.ssid')
        node.log.append('sys', 'wifi: connecting to %s' % ssid)
        wlan.connect(ssid, node.settings.get('wifi', {}).get('psk'))
        deadline = time.ticks_add(time.ticks_ms(), 15000)
        while not wlan.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) < 0:
                fail('wifi: no connection after 15 s')
            time.sleep_ms(200)
    node.ip = wlan.ifconfig()[0]
    node.log.append('sys', 'wifi up: %s as %s' % (node.ip, node.hostname))


async def amain(node, sup, app):
    asyncio.create_task(sup.heartbeat())
    asyncio.create_task(sup.telemetry())
    await sup.autostart()
    node.log.append('sys', 'api listening on :%d' % node.port)
    await app.start_server(host='0.0.0.0', port=node.port)


node = Node(load_settings())
wifi_up(node)
sup = Supervisor(node)
sup.blame_check()
sup.scan()
sup.install_import_guard()
asyncio.run(amain(node, sup, create_app(node, sup)))
