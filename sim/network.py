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
            return b'\xb8\xf8\x62\xf7\xc5\x10'
        raise ValueError(param)

    def ifconfig(self):
        return ('127.0.0.1', '255.0.0.0', '127.0.0.1', '127.0.0.1')
