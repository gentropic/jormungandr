# sim stub for MicroPython's `ds18x20` — DS18B20 temperature sensors on a one-wire bus,
# reporting a slow drift so a probe guest reads something true off hardware.
import math
import time


class DS18X20:
    def __init__(self, onewire):
        self._ow = onewire

    def scan(self):
        return self._ow.scan()

    def convert_temp(self):
        pass          # on hardware this kicks off a ~750 ms conversion; the sim is instant

    def read_temp(self, rom):
        return round(21.5 + 3 * math.sin(time.ticks_ms() / 25000), 2)
