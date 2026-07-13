"""The leaf: a node too small to be a full node (SPEC-two).

A leaf senses and actuates and nothing else. It runs no IP server — no microdot, no
Supervisor, no Bus, no USB — because that is what starved a C3's lwIP pool. It is a
client: one outbound WebSocket to a flagship's bus, over which it announces itself,
publishes its sensors, and receives its actuator commands.

The point of keeping this module's imports tiny is not tidiness — it is the free lwIP
buffers that let the uplink connect at all. Import only asyncio, machine, json, and
the WS client; never reach into the heavy supervisor.
"""
import asyncio
import json
import machine
import time

from jorm import wsclient


def _host_port(url):
    rest = url.split('://', 1)[-1].rstrip('/')
    if ':' in rest:
        h, p = rest.rsplit(':', 1)
        return h, int(p)
    return rest, 80


def _mcu_temp():
    try:
        import esp32
        return round(esp32.mcu_temperature(), 1)
    except (ImportError, AttributeError, OSError):
        return None


class Leaf:
    def __init__(self, node):
        self.node = node
        self.io = node.settings.get('io', {})
        self.flagship = node.settings.get('flagship')
        self._adc = {}        # pin -> machine.ADC, built once
        self._out = {}        # topic -> machine.Pin (actuator), built once
        self._build_pins()

    def _build_pins(self):
        for s in self.io.get('sensors', []):
            if s.get('type') == 'adc' and 'pin' in s:
                self._adc[s['pin']] = machine.ADC(machine.Pin(s['pin']))
        for a in self.io.get('actuators', []):
            if a.get('type') == 'digital' and 'pin' in a:
                self._out[a['topic']] = machine.Pin(a['pin'], machine.Pin.OUT)

    # -- reading a sensor into a message -----------------------------------

    def _read(self, sensor):
        t = sensor.get('type')
        if t == 'vitals':
            import gc
            return {'heap': gc.mem_free(),
                    'up_s': time.ticks_diff(time.ticks_ms(), self.node._boot) // 1000}
        if t == 'adc':
            adc = self._adc.get(sensor['pin'])
            return {'raw': adc.read_u16()} if adc else None
        if t == 'temp':
            c = _mcu_temp()
            return {'c': c} if c is not None else None
        return None

    # -- the uplink: one connection, reconnected forever -------------------

    async def run(self):
        if not self.flagship:
            self.node.log.append('error', 'leaf: no "flagship" in settings — nothing to '
                                          'connect to. fix settings.json and reboot.')
            while True:            # block, do not fall through to a soft-reboot loop
                await asyncio.sleep(30)
        host, port = _host_port(self.flagship)
        self.node.log.append('sys', 'leaf: uplink to %s' % self.flagship)
        while True:
            ws = None
            act_task = None
            try:
                ws = await wsclient.connect(host, port, '/api/bus', self.node.token)
                await self._announce(ws)
                self.node.log.append('sys', 'leaf: uplink established')
                acts = [a['topic'] for a in self.io.get('actuators', []) if a.get('topic')]
                if acts:
                    await ws.send(json.dumps({'op': 'sub', 'filters': acts}))
                    # actuator delivery runs in the background; sensing in the
                    # foreground so it is never cancelled by a quiet command socket.
                    act_task = asyncio.create_task(self._actuate(ws))
                await self._sense(ws)
            except (OSError, EOFError, ValueError) as e:
                self.node.log.append('sys', 'leaf: uplink down (%s) — retrying in 3 s' % e)
            finally:
                if act_task is not None:
                    act_task.cancel()
                if ws is not None:
                    await ws.close()
            await asyncio.sleep(3)

    async def _announce(self, ws):
        # A leaf has no /api/node for the UI to read, so it says what it is on the
        # bus: a retained $sys/leaf/<name> the flagship reads to put it in the tree.
        await ws.send(json.dumps({
            'op': 'pub', 'retain': True,
            'topic': '$sys/leaf/' + self.node.hostname,
            'msg': {'name': self.node.hostname, 'board': self.node.board_name(),
                    'sensors': [s.get('topic') for s in self.io.get('sensors', [])],
                    'actuators': [a.get('topic') for a in self.io.get('actuators', [])]},
        }))

    async def _sense(self, ws):
        # Each sensor on its own cadence. A single loop ticking once a second is
        # enough resolution for anything a leaf senses, and it keeps one timer, not N.
        import gc
        nexts = {}
        while True:
            now = time.ticks_ms()
            for i, s in enumerate(self.io.get('sensors', [])):
                every = int(s.get('every_s', 30)) * 1000
                if time.ticks_diff(now, nexts.get(i, 0)) >= 0:
                    nexts[i] = time.ticks_add(now, every)
                    msg = self._read(s)
                    if msg is not None and s.get('topic'):
                        await ws.send(json.dumps({'op': 'pub', 'topic': s['topic'], 'msg': msg}))
            # Collect every tick. On the leaf's small heap this is a millisecond, not
            # the stop-the-world a flagship sees over 8 MB — and it keeps free heap
            # flat instead of sawtoothing down toward an allocation failure mid-send.
            gc.collect()
            await asyncio.sleep(1)

    async def _actuate(self, ws):
        while True:
            frame = json.loads(await ws.recv())
            topic = frame.get('topic')
            pin = self._out.get(topic)
            if pin is None:
                continue
            msg = frame.get('msg')
            # truthiness drives the pin: a bare bool, or {"on": true}, or {"value": 1}
            on = msg.get('on', msg.get('value', msg)) if isinstance(msg, dict) else msg
            pin.value(1 if on else 0)
            self.node.log.append('sys', 'leaf: %s -> %s' % (topic, 'on' if on else 'off'))


def run_leaf(node):
    leaf = Leaf(node)
    # A leaf's WDT is safe where a full node's was not: with the heavy supervisor
    # gone, GC walks a fraction of the heap, so pauses are short and the heartbeat
    # (the uplink loop) feeds the dog well within its window.
    wdt = machine.WDT(timeout=8000) if node.settings.get('wdt', True) else None

    async def _amain():
        if wdt:
            async def feed():
                while True:
                    wdt.feed()
                    await asyncio.sleep(2)
            asyncio.create_task(feed())
        await leaf.run()

    asyncio.run(_amain())
