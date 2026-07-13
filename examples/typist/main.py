# Typist — the node becomes a USB keyboard, and types what the bus tells it to.
#
# This is the whole M4 conceit in one guest: it declares caps.usb hid keyboard,
# and the supervisor built that interface into the composite device at boot. When
# this guest runs, the interface is live; when it is stopped, the interface is
# inert — the host still sees a keyboard, it just never presses anything.
#
# Publish a string to cmd/typist/say and the host it is plugged into gets typed at:
#   jorm pub cmd/typist/say "hello from a six-dollar board"
#
# The keystroke injector flag is on this guest, on purpose and in the open (§8) —
# a thing that can type into your computer should say so.
from jorm.usbkbd import KeyCode


# The un-shifted and shifted character maps. A keyboard sends keycodes and a
# modifier, not letters — so "H" is (shift, keycode-for-h), and "1" and "!" are
# the same key with and without shift.
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "1234567890"
_SYM = {' ': KeyCode.SPACE, '.': KeyCode.DOT, ',': KeyCode.COMMA,
        '-': KeyCode.MINUS, '\n': KeyCode.ENTER}


def _chord(ch):
    """Return (shift, keycode) for one character, or None if we cannot type it."""
    if ch in _LOWER:
        return (False, getattr(KeyCode, ch.upper()))
    if ch.isalpha() and ch.lower() in _LOWER:
        return (True, getattr(KeyCode, ch.upper()))
    if ch in _DIGITS:
        return (False, getattr(KeyCode, 'N' + ch))
    if ch in _SYM:
        return (False, _SYM[ch])
    return None


async def run(hal):
    kb = hal.usb().keyboard
    if kb is None:
        hal.log('no keyboard interface was granted — check caps.usb')
        return
    hal.log('typist ready; publish to cmd/typist/say')

    async for topic, msg in hal.bus.subscribe('cmd/typist/#'):
        if not topic.endswith('/say'):
            continue
        text = msg if isinstance(msg, str) else (msg or {}).get('text', '')
        if not kb.is_open():
            hal.log('nothing is plugged into the USB port — dropping "%s"' % text)
            continue
        typed = 0
        for ch in text:
            chord = _chord(ch)
            if chord is None:
                continue
            shift, code = chord
            keys = (KeyCode.LEFT_SHIFT, code) if shift else (code,)
            kb.press(*keys)
            kb.release()
            typed += 1
            await hal.sleep_ms(6)
        kb.tap(KeyCode.ENTER)
        hal.log('typed %d character(s)' % typed)
