# The node's status light, as a guest.
#
# Switchboard's accent semantics rendered in photons: go / caution / fault. The
# beacon watches $sys — the same bus the UI watches — and shows the worst state
# any guest is in, with a heartbeat pulse on each $sys/clock/tick. When the node
# is healthy it breathes green once a second; when a guest crashes it goes red,
# on the board, without anyone looking at a screen.
#
# The RGB LED is on GPIO48 (Espressif's ESP32-S3-DevKitC-1 reference design).

COLOUR = {
    'ok':          (0, 60, 0),    # go
    'busy':        (60, 40, 0),   # caution
    'unresponsive': (60, 40, 0),  # caution
    'crashed':     (60, 0, 0),    # fault
}


async def run(hal):
    await hal.ui.config([
        {'key': 'brightness', 'w': 'slider', 'label': 'Brightness',
         'min': 1, 'max': 100, 'step': 1, 'unit': '%', 'default': 25, 'live': True},
    ])
    await hal.ui.panel([
        {'w': 'indicator', 'id': 'health', 'label': 'Node', 'bind': 'beacon/state',
         'path': 'ok', 'on': 'nominal', 'off': 'attention'},
        {'w': 'text', 'id': 'why', 'label': 'Showing', 'bind': 'beacon/state', 'path': 'why'},
        {'w': 'slider', 'id': 'bright', 'label': 'Brightness',
         'bind': 'beacon/brightness', 'set': 'cmd/beacon/brightness',
         'min': 1, 'max': 100, 'step': 1, 'unit': '%'},
    ])

    led = hal.rgb(48)

    st = {'bright': hal.config.get('brightness', 25), 'states': {}, 'beat': False}

    def show():
        states = [s for s in st['states'].values()]
        if 'crashed' in states:
            key, why = 'crashed', 'a guest has crashed'
        elif 'unresponsive' in states:
            key, why = 'unresponsive', 'a guest is unresponsive'
        elif 'starting' in states or 'stopping' in states:
            key, why = 'busy', 'a guest is changing state'
        else:
            key, why = 'ok', '%d guest(s) running' % states.count('running')

        r, g, b = COLOUR[key]
        # the heartbeat: every other tick dims the light, so a healthy node
        # visibly breathes rather than just sitting there being green
        scale = st['bright'] / 100.0 * (1.0 if st['beat'] else 0.35)
        rgb = (int(r * scale), int(g * scale), int(b * scale))
        led.fill(rgb)
        led.write()

        hal.status('%s · %s' % (key, why))
        hal.bus.publish('beacon/state', {'ok': key == 'ok', 'why': why, 'rgb': list(rgb)},
                        retain=True)

    def set_bright(pct):
        st['bright'] = min(100, max(1, int(pct)))
        hal.bus.publish('beacon/brightness', st['bright'], retain=True)
        show()

    async def on_sys():
        async for topic, msg in hal.bus.subscribe('$sys/guest/+/state'):
            gid = topic.split('/')[2]
            if msg is None:
                st['states'].pop(gid, None)
            else:
                st['states'][gid] = msg.get('state')
            show()

    async def on_cmd():
        async for topic, msg in hal.bus.subscribe('cmd/beacon/#'):
            if topic.endswith('/brightness') and isinstance(msg, dict) and 'value' in msg:
                set_bright(msg['value'])
                hal.log('brightness ->', st['bright'], '(origin: %s)' % msg.get('origin'))

    async def on_cfg():
        async for key, value in hal.config.watch():
            if key == 'brightness':
                set_bright(value)

    hal.spawn(on_sys())
    hal.spawn(on_cmd())
    hal.spawn(on_cfg())
    set_bright(st['bright'])
    hal.log('beacon lit on GPIO48')

    try:
        async for _topic, _msg in hal.bus.subscribe('$sys/clock/tick'):
            st['beat'] = not st['beat']
            show()
    finally:
        led.off()   # a guest that stops leaves the board dark, not lying
