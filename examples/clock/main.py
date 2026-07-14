# The clock, as a guest (not a special leaf — the whole point).
#
# Renders a large HH:MM:SS with a decisecond bar to an 8x32 panel it holds through the
# `matrix` cap, exactly the way `beacon` holds its LED: the supervisor owns the wire, the
# guest gets a surface. Time comes from hal.time() — the node's own NTP-synced clock, so
# there is no NTP in here. Messages, a persistent banner, and brightness arrive over the
# bus (cmd/clock/#); brightness is also live guest config. Stop the guest and the panel
# goes dark — it does not sit there lying.

# 4-wide, 7-tall clock digits + a 1-wide colon.
_G = {
    '0': ("####", "#..#", "#..#", "#..#", "#..#", "#..#", "####"),
    '1': (".##.", "..#.", "..#.", "..#.", "..#.", "..#.", ".###"),
    '2': ("####", "...#", "...#", "####", "#...", "#...", "####"),
    '3': ("####", "...#", "...#", ".###", "...#", "...#", "####"),
    '4': ("#..#", "#..#", "#..#", "####", "...#", "...#", "...#"),
    '5': ("####", "#...", "#...", "####", "...#", "...#", "####"),
    '6': ("####", "#...", "#...", "####", "#..#", "#..#", "####"),
    '7': ("####", "...#", "...#", "..#.", ".#..", ".#..", ".#.."),
    '8': ("####", "#..#", "#..#", "####", "#..#", "#..#", "####"),
    '9': ("####", "#..#", "#..#", "####", "...#", "...#", "####"),
    ':': (".", ".", "#", ".", "#", ".", "."),
}


async def run(hal):
    await hal.ui.config([
        {'key': 'brightness', 'w': 'slider', 'label': 'Brightness',
         'min': 0, 'max': 15, 'step': 1, 'default': 3, 'live': True},
        {'key': 'night', 'w': 'slider', 'label': 'Night brightness',
         'min': 0, 'max': 15, 'step': 1, 'default': 1, 'live': True},
        {'key': 'tz', 'w': 'slider', 'label': 'UTC offset',
         'min': -12, 'max': 14, 'step': 1, 'default': -3, 'live': True},
    ])
    await hal.ui.panel([
        {'w': 'text', 'id': 'showing', 'label': 'Showing', 'bind': 'clock/state', 'path': 'showing'},
        {'w': 'slider', 'id': 'bright', 'label': 'Brightness',
         'bind': 'clock/state', 'set': 'cmd/clock/brightness', 'path': 'brightness',
         'min': 0, 'max': 15, 'step': 1},
    ])

    panel = hal.matrix()
    st = {'bright': int(hal.config.get('brightness', 3)),
          'night': int(hal.config.get('night', 1)),
          'tz': int(hal.config.get('tz', -3)),
          'notif': None, 'banner': None, 'synced': False, 'showing': 'clock'}

    def glyph(ch, x):
        g = _G[ch]
        for r in range(7):
            row = g[r]
            for c in range(len(row)):
                if row[c] == '#':
                    panel.pixel(x + c, r, 1)

    def draw_clock(hh, mm, ss, frac10, blink):
        panel.fill(0)
        x = 1
        for ch in '%02d:%02d:%02d' % (hh, mm, ss):
            if ch == ':':
                if st['synced'] or blink:      # colons blink while the clock is unset
                    glyph(':', x)
                x += 1
            else:
                glyph(ch, x)
                x += 5                         # 4-wide digit + 1 px gap
        for seg in range(frac10):              # decisecond bar, one block per tenth
            panel.pixel(seg * 3 + 1, 7, 1)
            panel.pixel(seg * 3 + 2, 7, 1)

    def draw_text(s, x):
        panel.fill(0)
        w = len(s) * 8
        panel.text(s, (panel.width - w) // 2 if w <= panel.width else x)
        return w

    def state():
        hal.bus.publish('clock/state', {'showing': st['showing'], 'brightness': st['bright'],
                                        'synced': st['synced']}, retain=True)

    def set_bright(v):
        st['bright'] = max(0, min(15, int(v)))
        panel.brightness(st['bright'])
        state()

    async def on_cmd():
        async for topic, msg in hal.bus.subscribe('cmd/clock/#'):
            verb = topic.rsplit('/', 1)[-1]
            if verb == 'show':
                text = msg if isinstance(msg, str) else (msg or {}).get('text', '')
                secs = (msg or {}).get('secs', 8) if isinstance(msg, dict) else 8
                if text:
                    st['notif'] = {'text': str(text), 'until': hal.ticks_ms() + int(secs) * 1000}
            elif verb == 'banner':
                text = msg if isinstance(msg, str) else (msg or {}).get('text', '') if msg else ''
                st['banner'] = str(text) or None
            elif verb == 'brightness':
                val = msg.get('value', msg.get('day')) if isinstance(msg, dict) else msg
                if val is not None:
                    set_bright(val)

    async def on_sys():
        async for _topic, msg in hal.bus.subscribe('$sys/clock/tick'):
            if isinstance(msg, dict) and 'synced' in msg:
                st['synced'] = msg['synced']

    async def on_cfg():
        async for key, value in hal.config.watch():
            if key == 'brightness':
                set_bright(value)
            elif key == 'night':
                st['night'] = max(0, min(15, int(value)))
            elif key == 'tz':
                st['tz'] = int(value)

    hal.spawn(on_cmd())
    hal.spawn(on_sys())
    hal.spawn(on_cfg())
    set_bright(st['bright'])
    hal.log('clock guest up on an 8x32 panel')

    last_ss = -1
    second_start = hal.ticks_ms()
    cur_msg = None
    scroll_x = panel.width
    phase = 'clock'
    phase_at = hal.ticks_ms()
    last_bright = None
    tick = 0
    try:
        while True:
            now = hal.ticks_ms()
            t = int(hal.time()) + st['tz'] * 3600
            ss = t % 60
            if ss != last_ss:
                last_ss = ss
                second_start = now
            frac_ms = now - second_start
            if frac_ms < 0 or frac_ms > 1200:  # ticks wrap or clock jump — reset the tenth
                frac_ms = 0
                second_start = now
            hh, mm = (t // 3600) % 24, (t // 60) % 60

            want = st['night'] if (hh >= 22 or hh < 7) else st['bright']
            if want != last_bright:
                last_bright = want
                panel.brightness(want)

            notif, banner = st['notif'], st['banner']
            if notif and now < notif['until']:
                if cur_msg != notif['text']:
                    cur_msg, scroll_x = notif['text'], panel.width
                w = draw_text(notif['text'], scroll_x)
                if w > panel.width:
                    scroll_x -= 1
                    if scroll_x < -w:
                        scroll_x = panel.width
                if st['showing'] != 'message':
                    st['showing'] = 'message'
                    state()
            elif banner:
                if phase == 'clock' and now - phase_at >= 6000:
                    phase, phase_at, cur_msg, scroll_x = 'banner', now, banner, panel.width
                if phase == 'banner':
                    w = draw_text(banner, scroll_x)
                    scroll_x -= 1
                    if scroll_x < -w:
                        phase, phase_at = 'clock', now
                else:
                    draw_clock(hh, mm, ss, min(frac_ms // 100, 9), frac_ms < 500)
            else:
                if st['showing'] != 'clock':
                    st['showing'] = 'clock'
                    state()
                cur_msg, phase = None, 'clock'
                draw_clock(hh, mm, ss, min(frac_ms // 100, 9), frac_ms < 500)

            panel.show()
            tick += 1
            if tick % 25 == 0:                 # ~once a second, refresh the readout
                state()
            await hal.sleep_ms(40)
    finally:
        panel.off()
