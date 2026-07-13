# sim stub for MicroPython's `network` — always connected, IP is localhost.
# The stub MAC matches the real jorm-c510 board so names stay consistent everywhere.
STA_IF = 0
AP_IF = 1

_hostname = 'jorm-sim'


def hostname(name=None):
    global _hostname
    if name is None:
        return _hostname
    _hostname = name


class WLAN:
    def __init__(self, iface=STA_IF):
        self._iface = iface

    def active(self, on=None):
        return True

    def isconnected(self):
        return True

    def connect(self, ssid=None, key=None):
        print('[sim] wifi connect(%r) — already "connected"' % ssid)

    def config(self, param):
        if param == 'mac':
            # Locally administered (the 0x02 bit), and deliberately not the address
            # of any board on the desk. This used to return the real c510's MAC,
            # which was harmless while a cluster had one node and became a sim
            # impersonating a board the moment it had two.
            return b'\x02\x00\x00\x00\x51\x11'
        raise ValueError(param)

    def ifconfig(self):
        return ('127.0.0.1', '255.0.0.0', '127.0.0.1', '127.0.0.1')
