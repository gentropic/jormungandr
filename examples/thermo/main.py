# the M3 acceptance guest: a sensor with a declared panel and a config schema.
# Panels answer "what is it doing"; config answers "how should it behave".

async def run(hal):
    adc = hal.adc(4)
    st = {'period': 0}

    await hal.ui.config([
        {'key': 'period_ms', 'w': 'slider', 'label': 'Sample period',
         'min': 200, 'max': 5000, 'step': 100, 'unit': 'ms',
         'default': 1000, 'live': True},
        {'key': 'gauge_max_c', 'w': 'slider', 'label': 'Gauge maximum',
         'min': 40, 'max': 120, 'step': 5, 'unit': '°C',
         'default': 40, 'live': False},
    ])

    # The panel is declared once, at startup — which is exactly why gauge_max_c is
    # live: false. A gauge cannot rescale under a reading that is already drawn;
    # changing the range costs a restart, and the UI says so in amber.
    gauge_max = hal.config.get('gauge_max_c', 40)

    await hal.ui.panel([
        {'w': 'gauge', 'id': 't', 'label': 'Temp', 'bind': 'thermo/temp',
         'path': 'deg', 'min': 0, 'max': gauge_max, 'unit': '°C'},
        {'w': 'value', 'id': 'raw', 'label': 'ADC', 'bind': 'thermo/temp',
         'path': 'raw', 'spark': True},
        {'w': 'slider', 'id': 'period', 'label': 'Period',
         'bind': 'thermo/period', 'set': 'cmd/thermo/period',
         'min': 200, 'max': 5000, 'step': 100, 'unit': 'ms'},
    ])

    def set_period(ms):
        st['period'] = min(5000, max(200, int(ms)))
        hal.bus.publish('thermo/period', st['period'], retain=True)

    set_period(hal.config.get('period_ms', 1000))

    async def on_cmd():
        async for topic, msg in hal.bus.subscribe('cmd/thermo/#'):
            if topic.endswith('/period') and isinstance(msg, dict) and 'value' in msg:
                set_period(msg['value'])
                hal.log('period ->', st['period'], '(origin: %s)' % msg.get('origin'))

    async def on_cfg():
        async for key, value in hal.config.watch():
            if key == 'period_ms':
                set_period(value)
                hal.log('config: period_ms ->', st['period'])

    hal.spawn(on_cmd())
    hal.spawn(on_cfg())

    while True:
        raw = adc.read_u16()
        deg = round(15.0 + (raw - 28000) / 800.0, 1)
        # With nothing wired to the pin the ADC reads a floating input, and this
        # formula turns noise into a confident-looking temperature. The reading is
        # honest; the *impression* is not. So say which one you are looking at.
        plausible = -10 <= deg <= 60
        note = '' if plausible else '  (pin 4 floating? nothing attached)'
        # Celsius. The unit still travels with the reading, so a consumer never
        # has to guess — it just never has to guess about anything but Celsius.
        hal.bus.publish('thermo/temp',
                        {'deg': deg, 'unit': '°C', 'raw': raw, 'plausible': plausible},
                        retain=True)
        hal.status('%.1f °C%s' % (deg, note))
        await hal.sleep_ms(st['period'])
