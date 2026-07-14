#!/usr/bin/env python3
"""Boot the built firmware in ESP-IDF QEMU and measure the GC heap before/after importing
the jormungandr leaf-host framework — so we can see the real cost of the framework and
(across a frozen vs non-frozen build) the reclaim from freezing. No hardware, no flashing.

Run from ports/esp32 in an activated IDF env:
    python <repo>/firmware/qemu_measure.py [BUILD_DIR]
"""
import io
import re
import sys
import pexpect

BUILD = sys.argv[1] if len(sys.argv) > 1 else 'build-ESP32_GENERIC'

# The modules a leaf-host actually loads (supervisor + guest machinery + radio + display).
IMPORTS = ('import jorm.node, jorm.supervisor, jorm.hal, jorm.leafhost, jorm.guests, '
           'jorm.bus, jorm.claims, jorm.clock, jorm.guestcfg, jorm.espnow, jorm.gateway, '
           'jorm.cluster, jorm.bridge, jorm.netwatch, jorm.wsclient, jorm.display, '
           'jorm.console, jorm.max7219, jorm.seal, jorm.manifest, jorm.panels, jorm.ring, '
           'jorm.fsutil')

buf = io.StringIO()
child = pexpect.spawn('idf.py -B %s qemu' % BUILD, timeout=120, encoding='utf-8')
child.logfile_read = buf


def wait_prompt():
    child.expect(r'>>> ', timeout=120)


child.send('\r\r')
wait_prompt()
child.sendline('import gc')
wait_prompt()
child.sendline('gc.collect(); print("QTOTAL", gc.mem_alloc()+gc.mem_free())')
wait_prompt()
child.sendline('gc.collect(); print("QBASE", gc.mem_free())')
wait_prompt()
child.sendline(IMPORTS)
wait_prompt()
child.sendline('gc.collect(); print("QAFTER", gc.mem_free())')
wait_prompt()
child.sendcontrol('c')
try:
    child.close(force=True)
except Exception:
    pass

text = buf.getvalue()


def grab(marker):
    m = re.search(marker + r'\s+(\d+)', text)
    return int(m.group(1)) if m else None


total, base, after = grab('QTOTAL'), grab('QBASE'), grab('QAFTER')
err = re.search(r'(Traceback.*?Error.*)', text, re.S)

print('\n===== QEMU heap measurement (%s) =====' % BUILD)
if None in (total, base, after):
    print('MEASUREMENT INCOMPLETE — total=%r base=%r after=%r' % (total, base, after))
    if err:
        print('--- error in REPL ---\n' + err.group(1)[:600])
    sys.exit(1)
print('GC heap total:                 %7d B  (%.0f KB)' % (total, total / 1024))
print('free at REPL (framework unloaded): %7d B  (%.0f KB)' % (base, base / 1024))
print('free after importing framework:    %7d B  (%.0f KB)' % (after, after / 1024))
print('framework heap cost this build:    %7d B  (%.0f KB)' % (base - after, (base - after) / 1024))
