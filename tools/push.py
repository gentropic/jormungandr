#!/usr/bin/env python3
"""Push files to a node over USB serial, owning the boot timing.

Why not mpremote: opening the serial port resets the ESP32, so mpremote's
Ctrl-C is sent while the board is still in the ROM bootloader and is lost — and
a provisioned node arms a hardware WDT that cannot be disarmed, so if you do not
catch the boot escape window you cannot hold the REPL long enough to deploy.
This tool resets the board, *waits for the escape-window banner*, and only then
sends the interrupt. Deterministic, no race.

    uvx --with pyserial python tools/push.py COM14 [--settings]
"""
import gzip
import os
import sys
import tempfile
import time

import serial

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHUNK = 2048


class Node:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=0.2)

    def reset(self):
        self.s.dtr = False
        self.s.rts = True
        time.sleep(0.12)
        self.s.rts = False
        self.s.reset_input_buffer()

    def wait_for(self, needle, seconds=10):
        deadline = time.time() + seconds
        buf = b''
        while time.time() < deadline:
            buf += self.s.read(256)
            if needle in buf:
                return buf
        raise SystemExit('timed out waiting for %r; saw:\n%s'
                         % (needle, buf.decode('utf-8', 'replace')[-400:]))

    def catch_at_boot(self):
        """Reset and land in the REPL with the WDT unarmed.

        Deliberately NOT via the escape window: an older main.py escapes with
        SystemExit, which MicroPython treats as a forced exit and answers with a
        soft reboot — straight back into the boot you were escaping, forever. So
        we let the window close and interrupt during WiFi association instead:
        that raises an ordinary KeyboardInterrupt, which drops to the REPL, and
        it happens before asyncio.run() arms the watchdog. Works against any
        vintage of main.py on the board, which is what a recovery tool has to do.
        """
        self.reset()
        self.wait_for(b'boot in 2 s', 15)
        time.sleep(2.4)                # let the escape window close
        deadline = time.time() + 8
        buf = b''
        while time.time() < deadline:
            self.s.write(b'\x03')      # interrupt whatever boot is doing now
            time.sleep(0.05)
            buf += self.s.read(256)
            if b'>>>' in buf:
                break
        else:
            raise SystemExit('could not reach the REPL; saw:\n'
                             + buf.decode('utf-8', 'replace')[-400:])
        self.s.write(b'\r\x01')        # raw REPL
        self.wait_for(b'raw REPL', 5)
        self.s.reset_input_buffer()

    def exec_(self, code):
        """Send code via raw-paste mode and return its stdout.

        Raw-paste (Ctrl-E A Ctrl-A) exists because the plain raw REPL has no flow
        control: the UART has no RTS/CTS here, so a few KB written at speed simply
        overruns the device's receive buffer and it stops answering mid-file. In
        raw-paste the device advertises a window and acks as it consumes, so we
        can never outrun it. This is what mpremote does, and it is why hand-rolled
        uploaders die on the first file big enough to matter.
        """
        self.s.write(b'\x05A\x01')
        resp = self.s.read(2)
        if resp != b'R\x01':
            raise SystemExit('device refused raw-paste mode: %r' % resp)
        window = int.from_bytes(self.s.read(2), 'little')
        remaining = window

        data = code.encode()
        i = 0
        while i < len(data):
            while remaining == 0:
                ack = self.s.read(1)
                if ack == b'\x01':
                    remaining += window
                elif ack == b'\x04' or ack == b'':
                    raise SystemExit('device aborted the paste')
            n = min(remaining, window, len(data) - i)
            self.s.write(data[i:i + n])
            i += n
            remaining -= n
        self.s.write(b'\x04')  # end of paste; the device now runs it

        out = b''
        deadline = time.time() + 20
        while time.time() < deadline:
            out += self.s.read(256)
            if out.count(b'\x04') >= 2:
                break
        else:
            raise SystemExit('no reply from the node:\n'
                             + out.decode('utf-8', 'replace')[-300:])
        out = out.partition(b'\x04')[2]          # drop the paste-mode ack
        body, _, rest = out.partition(b'\x04')
        err = rest.partition(b'\x04')[0]
        if err.strip():
            raise SystemExit('node error:\n' + err.decode('utf-8', 'replace'))
        return body

    def put(self, local, remote):
        with open(local, 'rb') as f:
            data = f.read()
        self.exec_("f = open(%r, 'wb')" % remote)
        for i in range(0, len(data), CHUNK):
            self.exec_('f.write(%r)' % data[i:i + CHUNK])
        self.exec_('f.close()')
        print('   %-28s %6d bytes' % (remote, len(data)))

    def close(self):
        self.s.write(b'\x02')  # back to the friendly REPL
        self.s.close()


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else 'COM14'
    with_settings = '--settings' in sys.argv

    node = Node(port)
    print('== reset + catch the escape window')
    node.catch_at_boot()

    print('== dirs')
    node.exec_("""
import os
for d in ('lib', 'lib/microdot', 'jorm', 'guests'):
    try: os.mkdir(d)
    except OSError: pass
""")

    print('== supervisor')
    node.put(os.path.join(ROOT, 'supervisor', 'main.py'), 'main.py')
    for name in sorted(os.listdir(os.path.join(ROOT, 'supervisor', 'jorm'))):
        if name.endswith('.py'):
            node.put(os.path.join(ROOT, 'supervisor', 'jorm', name), 'jorm/' + name)
    lib = os.path.join(ROOT, 'supervisor', 'lib', 'microdot')
    for name in sorted(os.listdir(lib)):
        if name.endswith('.py'):
            node.put(os.path.join(lib, name), 'lib/microdot/' + name)

    print('== ui')
    ui = os.path.join(ROOT, 'supervisor', 'ui.html')
    node.put(ui, 'ui.html')
    # index() serves ui.html.gz to any gzip-accepting client, so a stale .gz left
    # beside a fresh .html serves yesterday's interface. Regenerate and ship it too.
    gz = os.path.join(tempfile.gettempdir(), 'jorm-ui.html.gz')
    with open(ui, 'rb') as f:
        with open(gz, 'wb') as g:
            g.write(gzip.compress(f.read(), 9))
    node.put(gz, 'ui.html.gz')

    if with_settings:
        print('== settings.json (secrets)')
        node.put(os.path.join(ROOT, 'settings.json'), 'settings.json')

    print('== pushed. resetting into a normal boot')
    node.reset()   # reset first: closing the port is what drops the line
    node.close()


if __name__ == '__main__':
    main()
