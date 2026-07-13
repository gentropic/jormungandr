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

    @property
    def cluster(self):
        # Zero has exactly one node, so "the cluster" is an aspiration with a
        # name. That is the point: the tree is the cluster's from day one, and
        # v1 adds nodes to it rather than rearranging it.
        return self.settings.get('cluster') or 'Cluster'

    def info(self):
        gc.collect()
        # os.uname is absent on the unix port; sys.implementation._machine is portable
        board = getattr(os, 'uname', None)
        board = board().machine if board else getattr(sys.implementation, '_machine', sys.platform)
        return {
            'hostname': self.hostname,
            'cluster': self.cluster,
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
