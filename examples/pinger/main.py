# half of the M2 demo pair: publishes pinger/tick under the default bus grant.
# One tick a second — a heartbeat, not a firehose. At 10 Hz it drowned the bus once it
# ran for real on a leaf, bridged all the way to the UI.
async def run(hal):
    n = 0
    while True:
        hal.bus.publish('pinger/tick', {'n': n})
        n += 1
        await hal.sleep_ms(1000)
