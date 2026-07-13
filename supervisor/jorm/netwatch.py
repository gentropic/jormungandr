"""WiFi re-association (reliability hardening).

A node connects WiFi once at boot (main.wifi_up) and, until now, never again — so a
link that dropped left the node running but unreachable, with no way back but a power
cycle. That bit a flagship live: it went silent mid-session and only a reboot brought
it back. Every node — full, leaf, smart leaf — starts this watcher, which notices a
dropped association and rebuilds it in place.

It is deliberately small and defensive: it never touches the radio while the link is
up, and a failed reconnect just means it tries again on the next tick.
"""
import asyncio


async def wifi_watch(node):
    try:
        import network
    except ImportError:
        return                         # the sim has no radio to watch
    ssid = node.settings.get('wifi', {}).get('ssid')
    if not ssid:
        return                         # nothing to reconnect to (sim/dev, or wired)
    psk = node.settings.get('wifi', {}).get('psk')
    wlan = node.wlan or network.WLAN(network.STA_IF)

    while True:
        await asyncio.sleep(5)
        try:
            if wlan.isconnected():
                continue
        except OSError:
            pass                       # a radio mid-fault reads as down; reconnect
        node.log.append('sys', 'wifi: link down — re-associating')
        try:
            wlan.active(False)
            await asyncio.sleep_ms(200)
            wlan.active(True)
            wlan.connect(ssid, psk)
            for _ in range(60):        # ~12 s, matching the boot window
                if wlan.isconnected():
                    break
                await asyncio.sleep_ms(200)
            if wlan.isconnected():
                node.ip = wlan.ifconfig()[0]
                node.log.append('sys', 'wifi: back up at %s' % node.ip)
            else:
                node.log.append('sys', 'wifi: still down — will retry')
        except OSError as e:
            node.log.append('sys', 'wifi: re-associate failed (%s) — will retry' % e)
