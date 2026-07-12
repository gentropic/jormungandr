"""The bus (spec §5): supervisor-owned pub/sub, the virtual switch.

In-memory, at-most-once, FIFO per subscription via bounded drop-oldest queues.
JSON-serializable payloads only — no live objects cross guest boundaries, ever.
"""
import json

from jorm.ring import Tap

RETAINED_MAX = 32
MSG_MAX = 4096


class BusError(Exception):
    pass


def match(filt, topic):
    """MQTT-style matching: + one segment, # tail. Wildcards never match a
    leading $-segment — $sys/... must be subscribed explicitly."""
    f, t = filt.split('/'), topic.split('/')
    if t[0].startswith('$') and f[0] in ('+', '#'):
        return False
    for i in range(len(f)):
        if f[i] == '#':
            return True
        if i >= len(t) or (f[i] != '+' and f[i] != t[i]):
            return False
    return len(f) == len(t)


def covered(grant, filt):
    """True if every topic matched by filt is also matched by grant."""
    g, f = grant.split('/'), filt.split('/')
    for i in range(len(g)):
        if g[i] == '#':
            return True
        if i >= len(f) or f[i] == '#':
            return False
        if f[i] == '+' and g[i] != '+':
            return False
        if g[i] != '+' and g[i] != f[i]:
            return False
    return len(g) == len(f)


def valid_topic(topic):
    return (isinstance(topic, str) and 0 < len(topic) <= 128
            and all(seg and '+' not in seg and '#' not in seg
                    for seg in topic.split('/')))


def valid_filter(filt):
    if not isinstance(filt, str) or not 0 < len(filt) <= 128:
        return False
    segs = filt.split('/')
    for i, seg in enumerate(segs):
        if not seg:
            return False
        if seg == '#':
            return i == len(segs) - 1
        if '#' in seg or ('+' in seg and seg != '+'):
            return False
    return True


class Subscription(Tap):
    def __init__(self, filters, qlen, owner):
        super().__init__(qlen)
        self.filters = filters
        self.owner = owner

    def info(self):
        return {'filters': self.filters, 'drops': self.drops,
                'depth': len(self.items), 'qlen': self.qlen}


class Bus:
    def __init__(self):
        self.subs = []
        self.retained = {}    # topic -> encoded JSON
        self.pub_counts = {}  # owner -> messages published

    def publish(self, topic, msg, retain=False, owner='sup'):
        if not valid_topic(topic):
            raise BusError('invalid topic %r' % topic)
        try:
            enc = json.dumps(msg)
        except (TypeError, ValueError):
            raise BusError('message is not JSON-serializable')
        if len(enc) > MSG_MAX:
            raise BusError('message is %d bytes; the limit is %d' % (len(enc), MSG_MAX))
        if retain:
            if msg is None:  # a retained None clears the slot (MQTT convention)
                self.retained.pop(topic, None)
                return
            if topic not in self.retained and len(self.retained) >= RETAINED_MAX:
                raise BusError('retained table full (%d topics)' % RETAINED_MAX)
            self.retained[topic] = enc
        self.pub_counts[owner] = self.pub_counts.get(owner, 0) + 1
        for sub in self.subs:
            for f in sub.filters:
                if match(f, topic):
                    sub.push((topic, enc))
                    break

    def subscribe(self, filters, qlen=16, owner='sup'):
        sub = Subscription(filters, qlen, owner)
        self.subs.append(sub)
        self.deliver_retained(sub, filters)
        return sub

    def deliver_retained(self, sub, filters):
        for topic in sorted(self.retained):
            for f in filters:
                if match(f, topic):
                    sub.push((topic, self.retained[topic]))
                    break

    def unsubscribe(self, sub):
        if sub in self.subs:
            self.subs.remove(sub)

    def retained_table(self):
        return {t: json.loads(enc) for t, enc in self.retained.items()}
