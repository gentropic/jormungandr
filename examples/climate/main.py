# Proves the dht cap: a DHT22 temp+humidity sensor on one pin, read without importing machine.
async def run(hal):
    s = hal.dht(4)
    while True:
        s.measure()
        hal.bus.publish('climate/reading',
                        {'temp_c': s.temperature(), 'rh': s.humidity()}, retain=True)
        hal.status('%.1f C  %.0f%%' % (s.temperature(), s.humidity()))
        await hal.sleep_ms(2000)          # DHT sensors want ~2 s between reads
