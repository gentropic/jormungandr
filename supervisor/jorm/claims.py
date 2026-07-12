"""The claims table (spec §3). Grant time is start time, all-or-nothing.

Pin modes in the table: out / in / in-shared (gpio), pwm, adc, cs (spi chip
select). pwm/adc/cs imply the pin claim; a pin has one owner and one mode,
except in-shared, which is a read-only input any number of guests may watch.
"""

PWM_CHANNELS = 8  # ESP32-S3: one LEDC group


class ClaimError(Exception):
    pass


class Claims:
    def __init__(self, reserved_pins=()):
        self.reserved = set(reserved_pins)
        self._pins = {}  # pin -> {'mode', 'pull', 'owners': [guest ids], 'bus': spi bus for cs}
        self._i2c = {}   # (bus, addr) -> guest id

    # -- grant / release -----------------------------------------------------

    def _pin_requests(self, caps):
        """Flatten every implied pin claim in the caps to (pin, mode, pull, spi_bus)."""
        reqs = [(p['pin'], p.get('mode', 'out'), p.get('pull'), None)
                for p in caps.get('pins', [])]
        reqs += [(n, 'pwm', None, None) for n in caps.get('pwm', [])]
        reqs += [(n, 'adc', None, None) for n in caps.get('adc', [])]
        reqs += [(e['cs'], 'cs', None, e['bus']) for e in caps.get('spi', [])]
        return reqs

    def grant(self, guest_id, caps):
        pin_reqs = self._pin_requests(caps)
        i2c_reqs = [(e['bus'], a) for e in caps.get('i2c', []) for a in e['addrs']]

        # check everything before touching the table: all-or-nothing
        seen = set()
        for n, mode, _pull, _bus in pin_reqs:
            if n in seen:
                raise ClaimError('pin %d claimed twice in one manifest' % n)
            seen.add(n)
            if n in self.reserved:
                raise ClaimError('pin %d is reserved by the supervisor' % n)
            cur = self._pins.get(n)
            if cur is None:
                continue
            if mode == 'in-shared' and cur['mode'] == 'in-shared':
                continue
            raise ClaimError('pin %d already passed through to guest "%s"'
                             % (n, cur['owners'][0]))
        pwm_new = sum(1 for _n, m, _p, _b in pin_reqs if m == 'pwm')
        pwm_used = sum(1 for e in self._pins.values() if e['mode'] == 'pwm')
        if pwm_used + pwm_new > PWM_CHANNELS:
            raise ClaimError('only %d PWM channels on this silicon; %d already claimed'
                             % (PWM_CHANNELS, pwm_used))
        seen_i2c = set()
        for bus, addr in i2c_reqs:
            if (bus, addr) in seen_i2c:
                raise ClaimError('i2c %d/0x%02x claimed twice in one manifest' % (bus, addr))
            seen_i2c.add((bus, addr))
            owner = self._i2c.get((bus, addr))
            if owner:
                raise ClaimError('i2c %d/0x%02x already passed through to guest "%s"'
                                 % (bus, addr, owner))

        for n, mode, pull, spi_bus in pin_reqs:
            entry = self._pins.setdefault(
                n, {'mode': mode, 'pull': pull, 'owners': [], 'bus': spi_bus})
            entry['owners'].append(guest_id)
        for bus, addr in i2c_reqs:
            self._i2c[(bus, addr)] = guest_id

    def release(self, guest_id):
        for n in list(self._pins):
            entry = self._pins[n]
            if guest_id in entry['owners']:
                entry['owners'].remove(guest_id)
                if not entry['owners']:
                    del self._pins[n]
        for key in list(self._i2c):
            if self._i2c[key] == guest_id:
                del self._i2c[key]

    # -- hal-side checks -----------------------------------------------------

    def _pin_mode(self, guest_id, n, want):
        entry = self._pins.get(n)
        if entry and guest_id in entry['owners'] and entry['mode'] in want:
            return entry
        return None

    def pin_grant(self, guest_id, n):
        return self._pin_mode(guest_id, n, ('out', 'in', 'in-shared'))

    def pwm_grant(self, guest_id, n):
        return self._pin_mode(guest_id, n, ('pwm',))

    def adc_grant(self, guest_id, n):
        return self._pin_mode(guest_id, n, ('adc',))

    def spi_grant(self, guest_id, bus, cs):
        entry = self._pin_mode(guest_id, cs, ('cs',))
        return entry if entry and entry['bus'] == bus else None

    def i2c_grant(self, guest_id, bus, addr):
        return self._i2c.get((bus, addr)) == guest_id

    # -- views ---------------------------------------------------------------

    def for_guest(self, guest_id):
        return {
            'pins': [{'pin': n, 'mode': e['mode']}
                     for n, e in sorted(self._pins.items()) if guest_id in e['owners']],
            'i2c': ['%d/0x%02x' % k for k, g in sorted(self._i2c.items()) if g == guest_id],
        }

    def table(self):
        return {
            'pins': [{'pin': n, 'mode': e['mode'], 'owners': e['owners']}
                     for n, e in sorted(self._pins.items())],
            'i2c': [{'bus': b, 'addr': a, 'owner': g}
                    for (b, a), g in sorted(self._i2c.items())],
            'reserved_pins': sorted(self.reserved),
        }
