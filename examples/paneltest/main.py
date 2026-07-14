# Minimal consumer of the host-owned display (SPEC-two §8, revised): it asks for focus on
# the primary display, draws a label and a marching pixel, and reports on the bus that it
# holds the panel. Stop it and the host reclaims the panel for status — that handoff is the
# whole point of this example (and what tools/accept-display-focus.sh checks).


async def run(hal):
    d = hal.display()
    hal.bus.publish('paneltest/state', {'drawing': True, 'w': d.width, 'h': d.height},
                    retain=True)
    hal.log('paneltest up — holding the primary display')
    x = 0
    while True:
        d.fill(0)
        d.text('HI', 0, 0)
        d.pixel(x % d.width, 7, 1)
        d.show()
        x += 1
        await hal.sleep_ms(200)
