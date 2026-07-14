# Proves the onewire cap: DS18B20 temperature sensors on a one-wire bus (one pin, many
# devices) — scan for ROMs, trigger a conversion, read each, all without importing machine.
async def run(hal):
    ow = hal.onewire(15)
    roms = ow.scan()
    hal.log('onewire: %d device(s) found' % len(roms))
    while True:
        ow.convert()
        await hal.sleep_ms(750)           # DS18B20 conversion time
        temps = [ow.read_temp(r) for r in roms]
        hal.bus.publish('probe1w/temps', {'n': len(roms), 'temps': temps}, retain=True)
        hal.status('temps: %s' % temps)
        await hal.sleep_ms(2000)
