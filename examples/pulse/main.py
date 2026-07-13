# PWM, breathing. Claims one of the S3's 8 LEDC channels on GPIO5 — attach an
# LED if you like, or watch the duty on the panel and take the photons on faith.
import math


async def run(hal):
    await hal.ui.config([
        {'key': 'period_s', 'w': 'slider', 'label': 'Breath period',
         'min': 1, 'max': 20, 'step': 1, 'unit': 's', 'default': 4, 'live': True},
    ])
    await hal.ui.panel([
        {'w': 'gauge', 'id': 'duty', 'label': 'Duty', 'bind': 'pulse/duty',
         'path': 'duty', 'min': 0, 'max': 1023},
        {'w': 'toggle', 'id': 'run', 'label': 'Breathing',
         'bind': 'pulse/on', 'set': 'cmd/pulse/on'},
    ])

    pwm = hal.pwm(5)
    pwm.freq(1000)
    st = {'on': True, 'period': hal.config.get('period_s', 4)}
    hal.bus.publish('pulse/on', True, retain=True)

    async def on_cmd():
        async for topic, msg in hal.bus.subscribe('cmd/pulse/#'):
            if topic.endswith('/on') and isinstance(msg, dict):
                st['on'] = bool(msg.get('value'))
                hal.bus.publish('pulse/on', st['on'], retain=True)
                hal.log('breathing' if st['on'] else 'held dark')

    async def on_cfg():
        async for key, value in hal.config.watch():
            if key == 'period_s':
                st['period'] = value

    hal.spawn(on_cmd())
    hal.spawn(on_cfg())

    t = 0.0
    n = 0
    try:
        while True:
            if st['on']:
                # a sine in perceptual-ish space; 0..1023 is the ESP32 duty range
                duty = int(511 * (1 - math.cos(2 * math.pi * t / st['period'])))
            else:
                duty = 0
            pwm.duty(duty)
            # the LED breathes at 20 Hz; the bus hears about it at 5. A guest that
            # narrates every step of a smooth curve is just a guest flooding a bus.
            n += 1
            if n % 4 == 0:
                hal.bus.publish('pulse/duty', {'duty': duty}, retain=True)
                hal.status('duty %d' % duty)
            await hal.sleep_ms(50)
            t += 0.05
    finally:
        pwm.duty(0)   # leave the pin dark, not mid-breath
