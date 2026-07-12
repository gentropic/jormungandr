"""Manifest validation (spec §2). Fail closed: unknown cap keys refuse."""

_ID_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789-'

KNOWN_CAPS = ('pins', 'pwm', 'adc', 'i2c', 'spi', 'net', 'ble', 'bus', 'ui',
              'storage', 'mem_kb', 'usb')
SUPPORTED_CAPS = ('pins',)  # M1; the rest of the grammar lands with M2/M3


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
    for p in caps.get('pins', []):
        if not isinstance(p, dict) or not isinstance(p.get('pin'), int):
            raise ManifestError('pins entries must be {"pin": n, "mode": ...}')
        if p.get('mode', 'out') not in ('out', 'in', 'in-shared'):
            raise ManifestError('pin mode must be out | in | in-shared')
        if 'pull' in p and p['pull'] not in ('up', 'down'):
            raise ManifestError('pull must be up | down')
    return m
