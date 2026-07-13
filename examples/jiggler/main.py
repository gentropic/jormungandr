# Mouse Jiggler — the node is a USB mouse that nudges the pointer so the host
# never thinks you have left. The whole point of M4 in one honest peripheral: it
# declares caps.usb hid mouse, the supervisor built that interface into the
# composite device at boot, and here we just move the pointer.
#
# It nudges by `distance` pixels and immediately moves back the same amount, so
# the pointer ends where it started — a twitch, not a drift. Both the distance
# and the interval are live-configurable, and the whole thing toggles off without
# stopping the guest (a stopped guest's interface goes inert anyway, §8, but a
# toggle lets you park it while leaving the panel live).


async def run(hal):
    # Live config: change any of these from the guest's Config tab and the running
    # loop picks it up on the next tick — no restart.
    await hal.ui.config([
        {'key': 'enabled', 'w': 'toggle', 'label': 'Jiggling', 'default': True,
         'live': True},
        {'key': 'distance', 'w': 'slider', 'label': 'Distance', 'unit': 'px',
         'min': 1, 'max': 20, 'step': 1, 'default': 2, 'live': True},
        {'key': 'interval', 'w': 'slider', 'label': 'Every', 'unit': 's',
         'min': 5, 'max': 300, 'step': 5, 'default': 30, 'live': True},
    ])
    await hal.ui.panel([
        {'w': 'indicator', 'id': 'on', 'label': 'Jiggling', 'bind': 'jiggler/state',
         'path': 'enabled', 'on': 'on', 'off': 'parked'},
        {'w': 'text', 'id': 'plugged', 'label': 'Host', 'bind': 'jiggler/state',
         'path': 'host'},
        {'w': 'text', 'id': 'count', 'label': 'Nudges', 'bind': 'jiggler/state',
         'path': 'nudges'},
        {'w': 'toggle', 'id': 'toggle', 'label': 'Jiggle', 'bind': 'jiggler/state',
         'path': 'enabled', 'set': 'cmd/jiggler/enabled'},
    ])

    mouse = hal.usb().mouse
    if mouse is None:
        hal.log('no mouse interface was granted — check caps.usb')
        return

    st = {
        'enabled': bool(hal.config.get('enabled', True)),
        'distance': int(hal.config.get('distance', 2)),
        'interval': int(hal.config.get('interval', 30)),
        'nudges': 0,
    }

    def publish():
        host = 'connected' if mouse.is_open() else 'nothing plugged in'
        hal.bus.publish('jiggler/state', {
            'enabled': st['enabled'], 'host': host, 'nudges': st['nudges'],
            'distance': st['distance'], 'interval': st['interval'],
        }, retain=True)
        hal.status('%s · every %ds · %d px · %d nudge(s)'
                   % ('on' if st['enabled'] else 'parked',
                      st['interval'], st['distance'], st['nudges']))

    def jiggle():
        # Out and back: the pointer twitches and returns to where it was, so this
        # keeps a session awake without slowly walking the cursor into a corner.
        d = st['distance']
        mouse.move_by(d, 0)
        mouse.move_by(-d, 0)
        st['nudges'] += 1

    async def on_cfg():
        async for key, value in hal.config.watch():
            if key == 'enabled':
                st['enabled'] = bool(value)
            elif key == 'distance':
                st['distance'] = int(value)
            elif key == 'interval':
                st['interval'] = int(value)
            publish()

    async def on_cmd():
        # the panel's toggle publishes here. st is the single source of truth for
        # whether we jiggle — the Config tab's toggle drives it through on_cfg, the
        # panel toggle drives it here, and both just move st and republish (the same
        # shape beacon uses for its brightness slider).
        async for topic, msg in hal.bus.subscribe('cmd/jiggler/enabled'):
            if isinstance(msg, dict) and 'value' in msg:
                st['enabled'] = bool(msg['value'])
                publish()

    hal.spawn(on_cfg())
    hal.spawn(on_cmd())
    publish()
    hal.log('jiggler ready — %d px every %d s' % (st['distance'], st['interval']))

    # The clock ticks once a second (retained $sys/clock/tick); counting ticks
    # rather than sleep(interval) means a changed interval takes effect at the next
    # second, not at the end of a 300-second nap already in progress.
    elapsed = 0
    async for _topic, _msg in hal.bus.subscribe('$sys/clock/tick'):
        elapsed += 1
        if elapsed < st['interval']:
            continue
        elapsed = 0
        if not st['enabled']:
            continue
        if not mouse.is_open():
            continue        # nothing is plugged in; nudging a void just burns cycles
        jiggle()
        publish()
