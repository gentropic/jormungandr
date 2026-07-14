"""Manifest validation (spec §2). Fail closed: unknown cap keys refuse."""

from jorm.bus import valid_filter

_ID_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789-'

KNOWN_CAPS = ('pins', 'pwm', 'adc', 'i2c', 'spi', 'net', 'ble', 'bus', 'ui',
              'storage', 'mem_kb', 'usb', 'rgb', 'matrix')
SUPPORTED_CAPS = ('pins', 'pwm', 'adc', 'i2c', 'spi', 'net', 'bus', 'ui',
                  'storage', 'mem_kb', 'rgb', 'usb', 'matrix')  # ble is post-zero


class ManifestError(Exception):
    pass


def validate(m):
    if not isinstance(m, dict):
        raise ManifestError('manifest is not an object')
    if m.get('spec') != 0:
        raise ManifestError('manifest spec %r is from the future — this supervisor speaks spec 0'
                            % m.get('spec'))
    id_ = m.get('id')
    if (not isinstance(id_, str) or not 1 <= len(id_) <= 24
            or any(c not in _ID_CHARS for c in id_)):
        raise ManifestError('id must match [a-z0-9-]{1,24}')
    if m.get('runtime') != 'mpy':
        raise ManifestError('runtime %r not offered by this node (runtimes: mpy)'
                            % m.get('runtime'))
    if not isinstance(m.get('entry', 'main.py'), str):
        raise ManifestError('entry must be a string')
    if m.get('restart', 'never') not in ('never', 'on-crash', 'always'):
        raise ManifestError('restart must be never | on-crash | always')

    caps = m.get('caps', {})
    if not isinstance(caps, dict):
        raise ManifestError('caps must be an object')
    for key in caps:
        if key not in KNOWN_CAPS:
            raise ManifestError('unknown cap %r — refused (fail closed, spec §2)' % key)
        if key not in SUPPORTED_CAPS:
            raise ManifestError('cap %r not implemented yet by this supervisor (M1: pins only)'
                                % key)
    bus = caps.get('bus')
    if bus is not None:
        if not isinstance(bus, dict):
            raise ManifestError('caps.bus must be an object')
        for key in bus:
            if key not in ('pub', 'sub'):
                raise ManifestError('caps.bus keys are pub | sub')
            if not isinstance(bus[key], list):
                raise ManifestError('caps.bus.%s must be a list of topic filters' % key)
            for f in bus[key]:
                if not valid_filter(f):
                    raise ManifestError('bad topic filter %r' % f)
                if key == 'pub' and f.split('/')[0].startswith('$'):
                    raise ManifestError('"$" roots are supervisor-written — pub grant refused (spec §5)')
    for p in caps.get('pins', []):
        if not isinstance(p, dict) or not isinstance(p.get('pin'), int):
            raise ManifestError('pins entries must be {"pin": n, "mode": ...}')
        if p.get('mode', 'out') not in ('out', 'in', 'in-shared'):
            raise ManifestError('pin mode must be out | in | in-shared')
        if 'pull' in p and p['pull'] not in ('up', 'down'):
            raise ManifestError('pull must be up | down')
    for e in caps.get('rgb', []):
        if not (isinstance(e, dict) and isinstance(e.get('pin'), int)
                and isinstance(e.get('count', 1), int) and e.get('count', 1) > 0):
            raise ManifestError('rgb entries must be {"pin": n, "count": k}')
    usbcap = caps.get('usb')
    if usbcap is not None:
        if not isinstance(usbcap, dict):
            raise ManifestError('caps.usb must be an object, e.g. {"hid": "keyboard"}')
        for key in usbcap:
            if key not in ('cdc', 'hid', 'midi'):
                raise ManifestError('caps.usb keys are cdc | hid | midi (spec §8)')
        hid = usbcap.get('hid')
        if hid is not None and hid not in ('keyboard', 'mouse') \
                and not (isinstance(hid, dict) and 'report_desc' in hid):
            raise ManifestError('caps.usb.hid must be "keyboard", "mouse", '
                                'or {"report_desc": "file.bin"}')
    for key in ('pwm', 'adc'):
        if key in caps and not (isinstance(caps[key], list)
                                and all(isinstance(n, int) for n in caps[key])):
            raise ManifestError('caps.%s must be a list of pin numbers' % key)
    for e in caps.get('i2c', []):
        if not (isinstance(e, dict) and isinstance(e.get('bus'), int)
                and isinstance(e.get('addrs'), list)
                and all(isinstance(a, int) and 0 <= a <= 127 for a in e['addrs'])):
            raise ManifestError('i2c entries must be {"bus": n, "addrs": [..]}')
    for e in caps.get('spi', []):
        if not (isinstance(e, dict) and isinstance(e.get('bus'), int)
                and isinstance(e.get('cs'), int)):
            raise ManifestError('spi entries must be {"bus": n, "cs": pin}')
    if 'net' in caps and caps['net'] != {'client': True}:
        raise ManifestError('caps.net is {"client": true} — guest servers are a later cap (spec §3)')
    if 'ui' in caps and caps['ui'] is not True:
        raise ManifestError('caps.ui is the literal true')
    if 'storage' in caps and not (isinstance(caps['storage'], dict)
                                  and isinstance(caps['storage'].get('quota_kb'), int)
                                  and caps['storage']['quota_kb'] > 0):
        raise ManifestError('caps.storage must be {"quota_kb": n}')
    if 'mem_kb' in caps and not (isinstance(caps['mem_kb'], int) and caps['mem_kb'] > 0):
        raise ManifestError('caps.mem_kb must be a positive integer')
    return m
