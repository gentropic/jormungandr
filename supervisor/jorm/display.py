"""Host-owned displays (SPEC-two §8, revised) — the supervisor owns the wire and the
driver; a display is a surface the host lights for status and leases to a guest.

A `Display` is framebuf-shaped: the same surface a guest's display cap has always seen, so
backends differ only in how bytes reach the glass. The host's status `Console` and (later)
a guest's `hal.display()` both speak *only* this interface, so a new panel kind is a new
backend file + one line in `_BACKENDS` — nothing else changes.

`open_display(spec)` builds one from an inventory entry, e.g.
    {"id": "primary", "kind": "max7219", "spi": 1, "sck": 25, "mosi": 32, "cs": 33, "n": 4}

Kept flat (not a `jorm/display/` package) for now: the flash sync copies `jorm/*.py` and a
subpackage would mean touching sim/run.sh, push.py, deploy.sh and ota.py. The package move
lands with the generalize milestone, when that tooling is updated anyway.
"""

# kind -> (module, class). Lazy so importing this module pulls in no backends, and a node
# that never uses a display kind never loads its driver. Resolved in host context only.
_BACKENDS = {
    'max7219': ('jorm.max7219', 'Matrix'),
}


def open_display(spec):
    kind = spec.get('kind')
    entry = _BACKENDS.get(kind)
    if entry is None:
        raise ValueError('unknown display kind %r (have %s)'
                         % (kind, ', '.join(sorted(_BACKENDS))))
    modname, clsname = entry
    mod = __import__(modname, None, None, (clsname,))
    return getattr(mod, clsname).from_spec(spec)


class Display:
    """Common surface a host owns. A backend sets `self.fb` (a MONO framebuf of
    `width` x `height`) and implements `show()`; everything else is drawing sugar over the
    framebuffer, matching the handle a guest's matrix cap has always had."""

    width = 0
    height = 0

    def fill(self, v=0):
        self.fb.fill(1 if v else 0)

    def pixel(self, x, y, v=1):
        self.fb.pixel(int(x), int(y), 1 if v else 0)

    def text(self, s, x=0, y=0, v=1):
        self.fb.text(str(s), int(x), int(y), 1 if v else 0)

    def hline(self, x, y, w, v=1):
        self.fb.hline(int(x), int(y), int(w), 1 if v else 0)

    def rect(self, x, y, w, h, v=1, fill=False):
        self.fb.rect(int(x), int(y), int(w), int(h), 1 if v else 0, fill)

    def scroll(self, dx, dy):
        self.fb.scroll(int(dx), int(dy))

    def size(self):
        return (self.width, self.height)

    def show(self):
        raise NotImplementedError

    def brightness(self, level):
        pass

    def off(self):
        self.fill(0)
        self.show()


class DisplayManager:
    """The host's display facility: it opens the node's displays from the inventory, owns
    the status `Console` on the primary, and tracks the focus lease — which guest (if any)
    is drawing right now. The exclusive *claim* lives in `claims`; this tracks the runtime
    *drawer*, so the host console knows when to stand back and when to reclaim.

    Built from settings["displays"]; a node with no panel simply has no manager and
    hal.display() refuses, exactly as the matrix cap did before.
    """

    def __init__(self, specs):
        from jorm.console import Console
        self.displays = {}
        self.primary = None
        self.primary_id = None
        for spec in specs or []:
            did = spec.get('id', 'primary')
            self.displays[did] = open_display(spec)
            if self.primary is None or did == 'primary':
                self.primary, self.primary_id = self.displays[did], did
        self.console = Console(self.primary) if self.primary else None
        self.focus = None            # guest id drawing now, or None = host console owns
        self.last_note = None
        self.on_note = None          # optional (text) -> publish hook, wired by the supervisor

    def note(self, text):
        """Host boot/status narration (`boot`, `wifi`, `ntp`, `up`). A no-op while a guest
        holds the lease — the host does not paint over a running guest's panel."""
        if self.focus is not None:
            return
        self.last_note = text
        if self.console is not None:
            self.console.status(text)
        if self.on_note is not None:
            self.on_note(text)

    def get(self, did='primary'):
        return self.displays.get(did) or self.primary

    def acquire(self, guest_id):
        self.focus = guest_id

    def release(self, guest_id):
        """Return True if this guest was the one drawing (so the host should reclaim)."""
        if self.focus == guest_id:
            self.focus = None
            return True
        return False

    def owns(self, guest_id):
        return self.focus == guest_id
