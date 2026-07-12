"""The claims table (spec §3). M1: pins only. Grant time is start time, all-or-nothing."""


class ClaimError(Exception):
    pass


class Claims:
    def __init__(self, reserved_pins=()):
        self.reserved = set(reserved_pins)
        # pin -> {'mode': ..., 'pull': ..., 'owners': [guest ids]}
        self._pins = {}

    def grant(self, guest_id, caps):
        pins = caps.get('pins', [])
        seen = set()
        for p in pins:
            n, mode = p['pin'], p.get('mode', 'out')
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
        for p in pins:
            n = p['pin']
            entry = self._pins.setdefault(
                n, {'mode': p.get('mode', 'out'), 'pull': p.get('pull'), 'owners': []})
            entry['owners'].append(guest_id)

    def release(self, guest_id):
        for n in list(self._pins):
            entry = self._pins[n]
            if guest_id in entry['owners']:
                entry['owners'].remove(guest_id)
                if not entry['owners']:
                    del self._pins[n]

    def pin_grant(self, guest_id, n):
        entry = self._pins.get(n)
        if entry and guest_id in entry['owners']:
            return entry
        return None

    def for_guest(self, guest_id):
        return {'pins': [{'pin': n, 'mode': e['mode']}
                         for n, e in sorted(self._pins.items()) if guest_id in e['owners']]}

    def table(self):
        return {
            'pins': [{'pin': n, 'mode': e['mode'], 'owners': e['owners']}
                     for n, e in sorted(self._pins.items())],
            'reserved_pins': sorted(self.reserved),
        }
