# Proves the udp cap: send and receive datagrams over a supervisor-owned socket — a guest can't
# import socket (the guard blocks it). Binds a port and loopbacks to itself; on a real network
# this is how a guest would talk to a service (a sensor gateway, an NTP peer, another node).
async def run(hal):
    u = hal.udp(9999)
    n = 0
    while True:
        u.sendto(('ping-%d' % n).encode(), '127.0.0.1', 9999)
        got = await u.recv(500)
        hal.bus.publish('udpecho/rx', {'got': got[0].decode() if got else None}, retain=True)
        hal.status('rx %s' % (got[0].decode() if got else '—'))
        n += 1
        await hal.sleep_ms(1000)
