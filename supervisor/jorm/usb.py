"""USB device passthrough (spec §8) — the node's own port, handed out as hardware.

The management plane is WiFi, so the native USB port is not sacred: the supervisor
reclaims it from the REPL and hands interfaces to guests as peripherals.

The rule that makes this a hypervisor and not a toy:

    The composite descriptor is built ONCE, AT BOOT, from every INSTALLED guest —
    not every RUNNING one.

You cannot hot-add a USB interface without the host seeing the whole device drop
and re-enumerate. A node that re-enumerated when you pressed "start" would be
lying about its own hardware — real hardware does not appear because software
began running. So a stopped guest's interfaces stay enumerated and INERT, and
installing or removing a usb guest leaves the plan *pending until reboot*. That is
precisely the "changing virtual hardware needs a power cycle" rule that VMs live
by, and honoring it is the whole reason the fiction holds.

Inert has to mean inert. A guest stopped mid-keystroke must not leave a key held
down on the host — the interface goes quiet, it does not jam. See release().
"""
from jorm.usbhid import HIDInterface
from jorm.usbkbd import KeyboardInterface, KeyCode      # noqa: F401  (re-exported to hal)
from jorm.usbmouse import MouseInterface

# The S3's OTG controller has a small, finite pool of endpoints. The exact usable
# count depends on what the built-in driver keeps for itself, so this is the
# conservative figure from §8 rather than a number scraped from a header we do not
# control. Being wrong here is cheap and loud: an install is refused with a
# breakdown. Being wrong the other way is a device that enumerates as garbage.
EP_BUDGET = 6


class UsbError(Exception):
    pass


class Grant:
    """One interface, granted to one guest, for the life of the descriptor."""

    def __init__(self, guest_id, kind, spec, itf):
        self.guest = guest_id
        self.kind = kind          # 'hid'
        self.spec = spec          # 'keyboard' | 'mouse' | {...}
        self.itf = itf
        # Ask the interface what it costs. A hardcoded cost table would be a second
        # source of truth about a descriptor that is right here and can be asked.
        self.eps = itf.num_eps()

    @property
    def injector(self):
        """A HID grant drives the host's own input. Say so, out loud, wherever this
        guest is shown (§8). An input injector that is discreet about being one is
        the bad kind."""
        return self.kind == 'hid'

    @property
    def injects(self):
        """What it can inject — a keyboard types, a mouse moves and clicks. "Keystroke
        injector" on a mouse is both wrong and the kind of wrong that erodes trust in
        the warning that matters."""
        if self.kind != 'hid':
            return None
        return {'keyboard': 'keystrokes', 'mouse': 'pointer moves and clicks'}.get(
            self.spec, 'host input')

    def info(self):
        return {'guest': self.guest, 'kind': self.kind, 'spec': self.spec,
                'endpoints': self.eps, 'injector': self.injector,
                'injects': self.injects}


def _build(guest_id, cap):
    """Turn one guest's `usb` cap into granted interfaces. Raises UsbError."""
    if not isinstance(cap, dict):
        raise UsbError('caps.usb must be an object, e.g. {"hid": "keyboard"}')

    grants = []
    for key in cap:
        if key not in ('hid',):
            # cdc and midi are in the §8 grammar and are not built yet. Refusing is
            # honest; silently ignoring a declared cap would hand a guest a device
            # that is missing an interface it was told it had.
            raise UsbError('usb.%s is in the spec but not implemented yet — '
                           'this supervisor grants usb.hid only' % key)

    hid = cap.get('hid')
    if hid == 'keyboard':
        grants.append(Grant(guest_id, 'hid', 'keyboard',
                            KeyboardInterface(interface_str='jorm: %s' % guest_id)))
    elif hid == 'mouse':
        grants.append(Grant(guest_id, 'hid', 'mouse',
                            MouseInterface(interface_str='jorm: %s' % guest_id)))
    elif isinstance(hid, dict) and 'report_desc' in hid:
        raise UsbError('usb.hid.report_desc (raw descriptors) is not implemented yet')
    elif hid is not None:
        raise UsbError('usb.hid must be "keyboard" or "mouse" (got %r)' % (hid,))
    return grants


class Plan:
    """What the host will see when this node enumerates."""

    def __init__(self):
        self.grants = []
        self.applied = False      # is this the plan the host is actually looking at?
        self.pending = False      # has the installed set changed since we enumerated?
        self.error = None

    @property
    def eps(self):
        return sum(g.eps for g in self.grants)

    def by_guest(self, guest_id):
        return [g for g in self.grants if g.guest == guest_id]

    def info(self):
        return {
            'interfaces': [g.info() for g in self.grants],
            'endpoints_used': self.eps,
            'endpoints_total': EP_BUDGET,
            'applied': self.applied,
            'pending': self.pending,   # the UI says this in amber: reboot to re-enumerate
            'error': self.error,
        }


def plan(guests):
    """Build the plan from installed guests, in install order.

    Install order, not start order and not alphabetical: the endpoint budget is
    allocated first-come, and a guest that fit yesterday must not be evicted today
    because someone installed a keyboard with an earlier name.
    """
    p = Plan()
    for g in guests:
        cap = (g.manifest.get('caps') or {}).get('usb')
        if not cap:
            continue
        for grant in _build(g.id, cap):
            if p.eps + grant.eps > EP_BUDGET:
                raise UsbError(
                    'no endpoints left for %s (%s %s needs %d, %d of %d used: %s)'
                    % (g.id, grant.kind, grant.spec, grant.eps, p.eps, EP_BUDGET,
                       ', '.join('%s=%d' % (x.guest, x.eps) for x in p.grants) or 'none'))
            p.grants.append(grant)
    return p


def check_fit(guests, new_guest_manifest, new_id):
    """Would this guest still fit? Called at INSTALL, so the refusal lands where a
    person can do something about it — not at the next boot, as a mystery."""
    cap = (new_guest_manifest.get('caps') or {}).get('usb')
    if not cap:
        return
    p = plan([g for g in guests if g.id != new_id])
    for grant in _build(new_id, cap):
        if p.eps + grant.eps > EP_BUDGET:
            raise UsbError(
                'usb: %s needs %d endpoint(s) and only %d of %d are free.\n'
                '     in use: %s\n'
                '     stop-and-remove a usb guest, or install this one on another node.'
                % (new_id, grant.eps, EP_BUDGET - p.eps, EP_BUDGET,
                   ', '.join('%s (%s %s, %d ep)' % (x.guest, x.kind, x.spec, x.eps)
                             for x in p.grants) or 'nothing'))
        p.grants.append(grant)


def apply(p, log):
    """Enumerate. Called once, at boot, and never again except by an explicit replan.

    Activating a custom device REPLACES the built-in USB-Serial-JTAG console — the
    REPL on the native port goes away and the board becomes what the guests declared.
    On an S3 devkit the separate UART bridge keeps a hardware serial alive, which is
    the recovery path and the reason this is safe to do at all (§8).
    """
    if not p.grants:
        return                       # nothing declared: leave the built-in REPL alone
    try:
        from jorm import usbcore
        usbcore.get().init(*[g.itf for g in p.grants], builtin_driver=False)
        p.applied = True
        log.append('sys', 'usb: enumerated %d interface(s), %d/%d endpoints — %s'
                   % (len(p.grants), p.eps, EP_BUDGET,
                      ', '.join('%s:%s' % (g.guest, g.spec) for g in p.grants)))
        for g in p.grants:
            if g.injector:
                log.append('sys', 'usb: %s can inject %s into the host it is plugged '
                                  'into (hid %s)' % (g.guest, g.injects, g.spec))
    except Exception as e:
        # Enumeration must never brick the node — a node with a broken USB
        # descriptor has to come up REACHABLE (over WiFi) so a person can fix it,
        # exactly as a bad guest does not take down the supervisor. The plan is
        # real and the budget was already enforced at install; only the final
        # handoff to the controller failed. That is a fact to record and survive:
        # on the sim there is no controller at all, and on a board it would mean a
        # descriptor the silicon rejected. Either way the node runs and says why.
        p.error = str(e)
        log.append('sys', 'usb: %d interface(s) planned, but this node did not '
                          'enumerate — %s' % (len(p.grants), e))


def release(p, guest_id):
    """A stopped guest's interfaces go quiet — they do not jam.

    Without this, stopping a guest that was holding a key leaves the key held on the
    host, forever, because nothing will ever send the release report. "Inert" has to
    mean the host sees nothing, not that the host sees the last thing we said,
    repeated until someone unplugs the board.
    """
    for g in p.by_guest(guest_id):
        try:
            if g.kind == 'hid' and g.spec == 'keyboard':
                g.itf.send_keys(())            # all keys up
            elif g.kind == 'hid' and g.spec == 'mouse':
                g.itf.release_all() if hasattr(g.itf, 'release_all') else None
        except Exception:
            pass                                # a host that is not listening is fine
