# The node's own vitals, as a guest — and the first guest to import a library.
#
# `sparkline` is not in the stdlib whitelist and is not part of hal: it lives in
# the node's /lib store, installed over HTTP with `jorm lib --install`. That is
# the three-tier import (spec §1) doing its job — a driver becomes ordinary guest
# code, and nobody has to extend the supervisor to get one.
#
# It subscribes only to $sys, so it needs no hardware at all: it works on a leaf
# node, on the sim, and on a board with nothing plugged into it.
from sparkline import Trend


async def run(hal):
    await hal.ui.config([
        {'key': 'temp_warn_c', 'w': 'slider', 'label': 'Temp warning',
         'min': 40, 'max': 100, 'step': 1, 'unit': '°C', 'default': 70, 'live': True},
    ])
    await hal.ui.panel([
        {'w': 'gauge', 'id': 'temp', 'label': 'MCU', 'bind': 'vitals/mcu',
         'path': 'c', 'min': 20, 'max': 100, 'unit': '°C'},
        {'w': 'value', 'id': 'heap', 'label': 'Heap free', 'bind': 'vitals/heap',
         'path': 'mb', 'unit': 'MB', 'spark': True},
        {'w': 'indicator', 'id': 'hot', 'label': 'Thermal', 'bind': 'vitals/mcu',
         'path': 'ok', 'on': 'nominal', 'off': 'hot'},
        {'w': 'text', 'id': 'trend', 'label': 'Heap trend', 'bind': 'vitals/heap',
         'path': 'spark'},
    ])

    warn = {'c': hal.config.get('temp_warn_c', 70)}
    heap = Trend(24)
    temp = Trend(24)

    async def on_cfg():
        async for key, value in hal.config.watch():
            if key == 'temp_warn_c':
                warn['c'] = value
                hal.log('temp warning ->', value, '°C')

    hal.spawn(on_cfg())

    async for topic, msg in hal.bus.subscribe('$sys/#'):
        if topic == '$sys/heap':
            mb = round(msg['free'] / 1048576, 2)
            heap.push(mb)
            hal.bus.publish('vitals/heap',
                            {'mb': mb, 'spark': heap.spark(), 'mean': round(heap.mean(), 2)},
                            retain=True)
        elif topic == '$sys/temp':
            c = msg['c']
            temp.push(c)
            ok = c < warn['c']
            hal.bus.publish('vitals/mcu', {'c': c, 'ok': ok, 'spark': temp.spark()},
                            retain=True)
            hal.status('%d °C%s · heap %s' % (c, '' if ok else ' — HOT', heap.spark()))
