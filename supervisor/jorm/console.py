"""The host's status line on a display it owns — a node's POST screen.

The supervisor is the default owner of the primary display: it lights it early in boot and
narrates what the node is doing (`boot`, `wifi`, `ntp`, `up`), and reclaims it to show
`"<guest> down"` when a focused guest is gone (SPEC-two §8, revised). It renders with the
backend's built-in 8x8 font, so it needs no glyph tables of its own.

Slice 1 is the synchronous surface: `status(text)` paints one frame. Scrolling long lines
and animated waiting come with the focus/lease manager, where the console is a task that
owns the surface over time.
"""


class Console:
    def __init__(self, display, brightness=1):
        self.d = display
        self.brightness = brightness
        self.last = None
        self._lit = False

    def status(self, text):
        """Paint a short status line. It fits when <= width/8 chars; longer text clips to
        the left (the scrolling console will carry the full line later)."""
        d = self.d
        if not self._lit:
            d.brightness(self.brightness)     # status glows low; a guest sets its own later
            self._lit = True
        s = str(text)
        w = len(s) * 8
        x = (d.width - w) // 2 if w <= d.width else 0
        d.fill(0)
        d.text(s, x, (d.height - 8) // 2)
        d.show()
        self.last = s
        return s

    def down(self, guest):
        """Reclaim message when a focused guest has stopped or crashed."""
        return self.status('%s?' % guest if len(guest) * 8 + 8 <= self.d.width else guest)

    def clear(self):
        self.d.fill(0)
        self.d.show()
        self.last = None
