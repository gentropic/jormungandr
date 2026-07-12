async def run(hal):
    led = hal.pin(2)
    while True:
        led.toggle()
        await hal.sleep_ms(500)
