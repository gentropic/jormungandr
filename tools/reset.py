#!/usr/bin/env python3
"""Hard-reset a node over its USB-serial DTR/RTS lines.

Needed because a WDT-armed node cannot be stopped from the REPL: the watchdog
reboots it out from under you. Reset first, then catch the boot escape window
(supervisor/main.py) with mpremote's Ctrl-C.

    uvx --with pyserial python tools/reset.py COM14
"""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else 'COM14'
s = serial.Serial(port, 115200, timeout=0.2)
s.dtr = False
s.rts = True    # assert reset
time.sleep(0.12)
s.rts = False   # release: the board boots
s.close()
print('reset %s' % port)
