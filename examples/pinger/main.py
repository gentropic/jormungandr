# half of the M2 demo pair: publishes pinger/tick under the default bus grant
async def run(hal):
    n = 0
    while True:
        hal.bus.publish('pinger/tick', {'n': n})
        n += 1
        await hal.sleep_ms(100)
