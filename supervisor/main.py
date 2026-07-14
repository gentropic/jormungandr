# jormungandr supervisor — boot: settings → wifi → mDNS name → bearer-token API.
# Runs unmodified on a real node and on the sim (sim/run.sh swaps machine/network).
import asyncio
import json
import os
import sys
import time

import network

from jorm.node import Node
# create_app and Supervisor are imported AFTER wifi_up (see the boot block), not
# here: loading the whole supervisor first leaves too little contiguous internal RAM
# for the WiFi driver's RX buffers on a small node (a C3 fails with "WiFi Out of
# Memory" otherwise). This is a one-time boot ordering, before the event loop — not
# a lazy import in a running task, which is the pattern §1 warns against.


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


def wifi_up(node, required=True):
    # The WiFi driver needs a contiguous block for its RX buffers, and on a small
    # node (a C3 with ~170 KB heap) importing the supervisor first can leave too
    # little — "WiFi Out of Memory" at init. Collect before asking for the radio, so
    # the driver gets the heap the bytecode loader was sitting on.
    import gc
    gc.collect()
    wlan = network.WLAN(network.STA_IF)
    # Clear any stale driver state a reset leaves behind. Without the down-cycle a
    # C3 fails the first connect after a reboot ("Wifi Internal State Error", or a
    # 15 s timeout) and connects only on a second try; the flagship tolerated the
    # dirty state, the small node does not.
    try:
        wlan.active(False)
        time.sleep_ms(100)
    except OSError:
        pass
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
            if required:
                fail('not connected and settings.json has no wifi.ssid')
            node.wlan = wlan
            return False
        psk = node.settings.get('wifi', {}).get('psk')
        # Retry the association a few times, each with a fresh down-cycle. A C3's
        # radio can miss the first attempt after a reset (a soft-reboot loop when it
        # only tried once), so give it three ~12 s windows before giving up rather
        # than one 15 s window. On a healthy link the first attempt connects at once.
        for attempt in range(3):
            if attempt:
                wlan.active(False)
                time.sleep_ms(200)
                wlan.active(True)
            node.log.append('sys', 'wifi: connecting to %s (try %d)' % (ssid, attempt + 1))
            # Clear any stuck CONNECTING state before dialling: the driver can wedge
            # mid-association and a bare connect() then sits at status 1001 forever, where
            # an explicit disconnect resets it and the active() down-cycle above does not.
            try:
                wlan.disconnect()
            except OSError:
                pass
            wlan.connect(ssid, psk)
            deadline = time.ticks_add(time.ticks_ms(), 12000)
            while not wlan.isconnected() and time.ticks_diff(deadline, time.ticks_ms()) > 0:
                time.sleep_ms(200)
            if wlan.isconnected():
                break
        else:
            if required:
                fail('wifi: no connection after 3 tries')
            # A leaf does NOT reboot when WiFi will not come up — that is precisely how a
            # clock ends up unable to tell the time because the network is flaky. It boots,
            # runs its guests locally (the panel lights now), and wifi_watch keeps dialling
            # in the background; NTP and the uplink land whenever the link does.
            node.log.append('sys', 'wifi: no link after 3 tries — booting anyway, '
                            'guests run locally and WiFi retries in the background')
            node.wlan = wlan
            return False
    node.ip = wlan.ifconfig()[0]
    node.wlan = wlan
    node.log.append('sys', 'wifi up: %s as %s' % (node.ip, node.hostname))
    return True


def radio_for_espnow(node):
    # An ESP-NOW leaf never associates — it just needs the radio on the gateway's
    # channel. Activate the STA interface, set the channel, and stop. No IP, no lwIP
    # pressure, no WiFi at all: this is the whole point of the off-WiFi leaf.
    import gc
    gc.collect()
    wlan = network.WLAN(network.STA_IF)
    try:
        wlan.active(False)
        time.sleep_ms(100)
    except OSError:
        pass
    wlan.active(True)
    mac = wlan.config('mac')
    node.mac4 = '%02x%02x' % (mac[-2], mac[-1])
    node.wlan = wlan
    ch = node.settings.get('gateway_channel')
    if ch is not None:
        try:
            wlan.config(channel=ch)
        except OSError as e:
            node.log.append('sys', 'espnow: could not set channel %d (%s)' % (ch, e))
        node.log.append('sys', 'radio on ch %d for espnow — no wifi association' % ch)
    else:
        # No seeded channel: the leaf will scan for a gateway and set the channel then.
        node.log.append('sys', 'radio up for espnow — will scan for a gateway')


async def confirm_trial(node):
    """A trial boot confirms itself by *being* a healthy node (boot.py).

    Nothing else is a sufficient test: an update that imports cleanly can still
    fail to serve. So we wait until the API has been up long enough to mean it,
    then drop the marker. If we never get here — crash, hang, watchdog — the next
    boot finds .trial and reverts. The node is the health check.
    """
    import os
    try:
        os.stat('.trial')
    except OSError:
        return
    await asyncio.sleep(20)
    try:
        os.remove('.trial')
        from jorm.fsutil import rmtree
        try:
            rmtree('backup')
        except OSError:
            pass
        node.log.append('sys', 'update confirmed — the node came back')
    except OSError:
        pass


async def amain(node, sup, app):
    asyncio.create_task(sup.heartbeat())
    asyncio.create_task(sup.ntp())
    asyncio.create_task(sup.telemetry())
    from jorm.netwatch import wifi_watch
    asyncio.create_task(wifi_watch(node))         # re-associate a dropped link
    if node.settings.get('gateway'):
        from jorm.gateway import run_gateway       # ESP-NOW ↔ bus for leaves (three §1)
        run_gateway(node, sup)
    asyncio.create_task(sup.cluster.announce())   # beacon out (one §1)
    asyncio.create_task(sup.cluster.listen())     # peers in
    asyncio.create_task(sup.bridge.run())         # pull peers' bus slices (one §4)
    asyncio.create_task(confirm_trial(node))
    await sup.autostart()
    node.log.append('sys', 'api listening on :%d' % node.port)
    await app.start_server(host='0.0.0.0', port=node.port)


def held():
    """Two ways to stop the node before it arms anything (spec §11).

    The hardware WDT cannot be disarmed once armed — not even by a soft reset —
    so a provisioned node correctly reboots itself the moment the supervisor
    stops being fed, and that is fatal to a deploy: Ctrl-C into the REPL and the
    watchdog throws you out of it seconds later. So the node can be stopped
    *before* the WDT exists, either by asking it over HTTP (POST
    /api/node/maintenance, which drops the flag consumed here) or by catching
    the two-second boot window with a Ctrl-C. Both cost nothing in the field and
    buy the ability to fix a node that is otherwise reachable only by re-flashing.

    Note: this must NOT raise SystemExit — MicroPython treats a forced exit from
    main.py as a soft-reset request and boots you straight back into the thing
    you were escaping.
    """
    try:
        os.remove('.maintenance')
    except OSError:
        pass
    else:
        print('[sys] maintenance boot — nothing started, WDT not armed.')
        return True
    try:
        print('[sys] boot in 2 s — Ctrl-C for the REPL')
        time.sleep(2)
    except KeyboardInterrupt:
        print('[sys] caught at the escape window — supervisor not started, WDT not armed')
        return True
    return False


if held():
    print('[sys] the REPL is yours.')
else:
    node = Node(load_settings())
    # A leaf is a node too small to be a full node (SPEC-two): it runs no IP server,
    # so it never loads the heavy supervisor that would starve its lwIP pool. Branch
    # BEFORE importing any of it — the import is the cost, not just the running.
    role = node.settings.get('role')
    # A full node NEEDS WiFi (its whole job is to serve), so no link is fatal and it
    # reboots to retry. A leaf's guests are local-first — the panel, the sensors — so a
    # flaky link must NOT stop it booting; it comes up and reconnects in the background.
    leafish = role in ('leaf', 'leaf-host')
    if node.settings.get('transport') == 'espnow':
        radio_for_espnow(node)   # off-WiFi leaf: radio on the gateway's channel, no assoc
    else:
        wifi_up(node, required=not leafish)
    if role == 'leaf':
        from jorm.leaf import run_leaf
        node.log.append('sys', 'booting as a leaf (no server, no ui)')
        run_leaf(node)   # blocks forever (asyncio.run); never returns to fall through
    elif role == 'leaf-host':
        from jorm.leafhost import run_leaf_host
        node.log.append('sys', 'booting as a smart leaf (guests, no server, no ui)')
        run_leaf_host(node)
    else:
        # Radio has its buffers; now it is safe to load the rest.
        from jorm.api import create_app
        from jorm.supervisor import Supervisor
        sup = Supervisor(node)
        sup.blame_check()
        sup.scan()
        sup.enumerate_usb()  # build the USB device from installed guests, before any run (§8)
        sup.install_import_guard()
        asyncio.run(amain(node, sup, create_app(node, sup)))
