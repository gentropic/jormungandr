# the other half of the M2 demo pair: two guests talking over the bus
async def run(hal):
    async for topic, msg in hal.bus.subscribe('pinger/#'):
        hal.bus.publish('echoer/tock', {'n': msg['n']})
        if msg['n'] % 50 == 0:
            hal.status('echoed %d ticks' % (msg['n'] + 1))
