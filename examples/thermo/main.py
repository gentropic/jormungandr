# the M3 acceptance guest: a sensor with a declared panel and a config schema.
# Panels answer "what is it doing"; config answers "how should it behave".

async def run(hal):
    adc = hal.adc(4)
    st = {'period': 0}

    await hal.ui.config([
        {'key': 'period_ms', 'w': 'slider', 'label': 'Sample period',
         'min': 200, 'max': 5000, 'step': 100, 'unit': 'ms',
         'default': 1000, 'live': True},
        {'key': 'unit_f', 'w': 'toggle', 'label': 'Fahrenheit',
         'default': False, 'live': False},
    ])
    await hal.ui.panel([
        {'w': 'gauge', 'id': 't', 'label': 'Temp', 'bind': 'thermo/temp',
         'path': 'c', 'min': 0, 'max': 40, 'unit': '°C'},
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
        c = 15.0 + (raw - 28000) / 800.0
        if hal.config.get('unit_f'):
            hal.bus.publish('thermo/temp', {'c': round(c * 9 / 5 + 32, 1), 'raw': raw}, retain=True)
            hal.status('%.1f °F' % (c * 9 / 5 + 32))
        else:
            hal.bus.publish('thermo/temp', {'c': round(c, 1), 'raw': raw}, retain=True)
            hal.status('%.1f °C' % c)
        await hal.sleep_ms(st['period'])
