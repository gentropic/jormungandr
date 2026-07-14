# sim stub for MicroPython's `onewire` — a one-wire bus with a single fake device present,
# enough for a ds18x20 guest to scan and read off hardware. Real boards use the port's module.


class OneWireError(Exception):
    pass


class OneWire:
    def __init__(self, pin):
        self._pin = pin

    def reset(self, required=False):
        return True

    def scan(self):
        # one DS18B20-family ROM (family code 0x28)
        return [b'\x28\xff\x01\x02\x03\x04\x05\x06']

    def readbit(self):
        return 1

    def writebit(self, value):
        pass
