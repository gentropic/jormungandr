# Proves the touch + dac caps: read a capacitive touch pad and mirror it to a DAC output —
# a guest reading an analog input and driving an analog output, no machine import.
async def run(hal):
    t = hal.touch(32)
    d = hal.dac(25)
    while True:
        v = t.read()
        d.write(v >> 2)                   # scale the touch reading into the DAC's 0..255
        hal.bus.publish('touchpad/value', {'touch': v}, retain=True)
        await hal.sleep_ms(200)
