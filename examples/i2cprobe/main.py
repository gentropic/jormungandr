# Proves the i2c cap: read a granted device's registers over a supervisor-owned bus the guest
# never imported `machine` to touch (spec §3). The handle is address-scoped — the guest was
# granted 0x76 (a BMP280-style sensor) and can read only it, not scan the whole bus.
async def run(hal):
    dev = hal.i2c(0)                              # bus 0; address 0x76 granted in the manifest
    chip = dev.mem_read(0x76, 0xD0, 1)[0]         # the chip-id register
    hal.log('i2c 0x76 chip id: 0x%02x' % chip)
    while True:
        raw = dev.mem_read(0x76, 0xFA, 3)         # a 3-byte pressure/temp register
        val = ((raw[0] << 16) | (raw[1] << 8) | raw[2]) >> 4
        hal.bus.publish('i2cprobe/reading', {'chip': chip, 'raw': val}, retain=True)
        hal.status('chip 0x%02x, raw %d' % (chip, val))
        await hal.sleep_ms(1000)
