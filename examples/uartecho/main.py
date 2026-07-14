# Proves the uart cap: write a line and read it back over a supervisor-owned UART, granted by
# declaring id 1 + its tx/rx pins (reserved like any other, so no two guests fight over the
# wire). On the sim the UART loopbacks; on a board this is a real TX->RX (a GPS, another MCU).
async def run(hal):
    u = hal.uart(1)
    n = 0
    while True:
        sent = 'ping-%d' % n
        u.write((sent + '\n').encode())
        await hal.sleep_ms(20)                   # let the bytes come back on the wire
        got = (u.readline() or b'').decode().strip()
        hal.bus.publish('uartecho/echo', {'sent': sent, 'got': got}, retain=True)
        hal.status('sent %s, got %s' % (sent, got or '—'))
        n += 1
        await hal.sleep_ms(1000)
