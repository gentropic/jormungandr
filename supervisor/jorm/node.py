import gc
import os
import sys
import time

from jorm import VERSION, SPEC
from jorm import clock
from jorm.ring import Ring


class Node:
    def __init__(self, settings):
        self.settings = settings
        self.token = settings.get('token') or ''
        self.port = settings.get('port', 80)
        self.log = Ring()
        self.mac4 = '????'
        self.ip = None
        self._boot = time.ticks_ms()

    @property
    def hostname(self):
        return self.settings.get('hostname') or 'jorm-' + self.mac4

    def info(self):
        gc.collect()
        # os.uname is absent on the unix port; sys.implementation._machine is portable
        board = getattr(os, 'uname', None)
        board = board().machine if board else getattr(sys.implementation, '_machine', sys.platform)
        return {
            'hostname': self.hostname,
            'board': board,
            'ip': self.ip,
            'profile': 'mpy',
            'runtimes': ['mpy'],
            'version': VERSION,
            'spec': SPEC,
            'heap_free': gc.mem_free(),
            'heap_alloc': gc.mem_alloc(),
            'uptime_ms': time.ticks_diff(time.ticks_ms(), self._boot),
            'clock': clock.status(),
        }
