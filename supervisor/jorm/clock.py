"""Wall-clock time, and the honesty about whether we have any (spec §4).

Two traps, both of which produce *plausible* wrong answers — the worst kind:

1. **The epoch.** MicroPython's epoch is 2000-01-01; CPython, JavaScript, and
   every consumer of our JSON use 1970. A raw `time.time()` from the board is
   30 years in the past, and it renders as a perfectly reasonable-looking clock
   in a console pane. So every timestamp that crosses the API boundary goes
   through `now()`, which normalizes to Unix seconds. (The unix port already
   uses the 1970 epoch, so the offset is detected, never assumed.)

2. **Whether the clock was ever set at all.** An ESP32 boots at zero. Until NTP
   lands, its "time" is uptime wearing a costume. The node says which it is —
   `synced` is reported in /api/node and the UI shows an unsynced clock as
   caution, because a timestamp you cannot trust is worse than no timestamp.
"""
import time

# 946684800 = 2000-01-01 in Unix seconds. Detected, not hardcoded per-port.
EPOCH_OFFSET = 946684800 if time.gmtime(0)[0] == 2000 else 0

_state = {'synced': False, 'source': None, 'last': 0, 'boot_unix': None}
_BOOT_TICKS = time.ticks_ms()


def mono():
    """Seconds since boot. Always true, needs no clock, and is what everything
    inside the node records — see to_unix()."""
    return time.ticks_diff(time.ticks_ms(), _BOOT_TICKS) / 1000.0


def to_unix(m):
    """Convert a monotonic stamp to Unix seconds, or None if we cannot yet.

    This is why the node records monotonic time and converts at the boundary
    rather than stamping wall-clock as it goes: an MCU boots without a clock, so
    lines written before NTP lands would be stamped wrong and *stay* wrong. Learn
    the offset once, apply it to everything — including the past. A log whose
    first three lines are fiction is worse than one that admits it has no clock.
    """
    if _state['boot_unix'] is None:
        return None
    return _state['boot_unix'] + m


def now():
    """Wall clock in Unix seconds, or best-effort uptime if never synced."""
    return to_unix(mono()) or (time.time() + EPOCH_OFFSET)


def status():
    return {'synced': _state['synced'], 'source': _state['source'],
            'last_sync': _state['last'] or None}


def sync(log=None, host=None):
    """Set the clock. Returns True if timestamps can now be trusted.

    Trustworthy has two sources, and the flag must not lie in either direction:
    an MCU has no clock until NTP sets one, but the sim runs on a host that
    already has a correct one. Reporting the sim as "unsynced" would be as false
    as reporting a cold ESP32 as synced.

    `host` (settings.ntp_host) is tried first: a cluster with a local time server
    reaches it when the public pools are firewalled off, and it is closer besides.
    """
    try:
        import ntptime
    except ImportError:
        # the sim: the OS clock is already right, and NTP is moot
        _state['boot_unix'] = time.time() + EPOCH_OFFSET - mono()
        _state.update(synced=True, source='host', last=now())
        return True
    # ntptime.settime() is SYNCHRONOUS — it blocks the whole event loop while it waits.
    # A configured host is the only reachable server on a cluster LAN, so try ONLY it:
    # falling through to the public pools blocks 2 s each on a firewalled LAN, and four
    # servers back to back sail past an 8 s hardware WDT — which reset a leaf mid-render.
    hosts = [host] if host else ['pool.ntp.org', 'time.google.com', 'time.cloudflare.com']
    ntptime.timeout = 2
    err = None
    for host in hosts:
        ntptime.host = host
        try:
            ntptime.settime()
            break
        except Exception as e:
            err = e
    else:
        if log:
            log.append('sys', 'ntp: no sync (%s) — timestamps are uptime, not wall clock' % err)
        return False
    _state['boot_unix'] = time.time() + EPOCH_OFFSET - mono()
    _state.update(synced=True, source='ntp', last=now())
    if log:
        log.append('sys', 'ntp: clock set — earlier lines are now dated too')
    return True
