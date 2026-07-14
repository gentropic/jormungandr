# sim stub for MicroPython's `dht` — a DHT22 reporting a slow, plausible climate, so a sensor
# guest reads real-looking values off hardware. Deployed nodes use the port's own dht module.
import math
import time


class DHT22:
    def __init__(self, pin):
        self._pin = pin
        self._t = 20.0
        self._h = 50.0

    def measure(self):
        self._t = round(20 + 5 * math.sin(time.ticks_ms() / 30000), 1)
        self._h = round(50 + 10 * math.sin(time.ticks_ms() / 40000), 1)

    def temperature(self):
        return self._t

    def humidity(self):
        return self._h


DHT11 = DHT22
