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
        self.wlan = None      # main.py hands us the radio; the sim has none
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

    def rssi(self):
        """How well this node hears the AP, in dBm — or None if it has no radio.

        None is not zero and not -100. A sim node on a wired host has a perfect
        link and no signal strength; reporting 0 dBm would make it the best
        connected node in the cluster, which is a lie a placement score would
        happily act on. It says nothing, and the UI renders nothing.
        """
        try:
            return self.wlan.status('rssi')
        except (AttributeError, OSError, ValueError):
            return None

    def board_name(self):
        # os.uname is absent on the unix port; sys.implementation._machine is portable
        u = getattr(os, 'uname', None)
        return u().machine if u else getattr(sys.implementation, '_machine', sys.platform)

    def info(self):
        gc.collect()
        return {
            'hostname': self.hostname,
            'cluster': self.cluster,
            'board': self.board_name(),
            'ip': self.ip,
            'profile': 'mpy',
            'runtimes': ['mpy'],
            'version': VERSION,
            'spec': SPEC,
            'rssi': self.rssi(),
            'heap_free': gc.mem_free(),
            'heap_alloc': gc.mem_alloc(),
            'uptime_ms': time.ticks_diff(time.ticks_ms(), self._boot),
            'clock': clock.status(),
        }
