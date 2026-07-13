"""Declarative micro-UI validation (spec §7): panels and config schemas.

Guests never serve UI — they declare it, the supervisor validates it at
declaration time, and the single HTML file renders it. Limits enforced here:
≤ 16 widgets, ≤ 4 KB encoded, set-topics inside the guest's own sub grants
(hard), bind-topics unchecked (soft — dangling binds render stale, spec-ui §5).
"""
import json

from jorm import bus as busmod

MAX_WIDGETS = 16
MAX_BYTES = 4096

WIDGETS = {
    #        reads(bind)  writes(set)  extra fields
    'value':     (True,  False, ('unit', 'fmt', 'path', 'spark')),
    'gauge':     (True,  False, ('min', 'max', 'unit', 'path')),
    'indicator': (True,  False, ('on', 'off', 'path')),
    'text':      (True,  False, ('lines', 'path')),
    'button':    (False, True,  ('msg', 'confirm')),
    'toggle':    (True,  True,  ('path',)),
    'slider':    (True,  True,  ('min', 'max', 'step', 'unit', 'path')),
    'select':    (True,  True,  ('options', 'path')),
}
COMMON = ('w', 'id', 'label', 'size', 'bind', 'set')

CONFIG_WIDGETS = ('slider', 'toggle', 'select', 'text')
_KEY_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789_'


class PanelError(Exception):
    pass


def _check_topic_fields(widget, sub_grants):
    kind = widget['w']
    reads, writes, _extra = WIDGETS[kind]
    if 'bind' in widget:
        if not reads:
            raise PanelError('widget %r (%s) does not read — no bind' % (widget.get('id'), kind))
        if not busmod.valid_topic(widget['bind']):
            raise PanelError('widget %r: invalid bind topic %r' % (widget.get('id'), widget['bind']))
        # soft: a bind nobody publishes renders stale; never refused (spec-ui §5)
    if 'set' in widget:
        if not writes:
            raise PanelError('widget %r (%s) does not write — no set' % (widget.get('id'), kind))
        if not busmod.valid_topic(widget['set']):
            raise PanelError('widget %r: invalid set topic %r' % (widget.get('id'), widget['set']))
        if not any(busmod.match(g, widget['set']) for g in sub_grants):
            raise PanelError('widget %r: set topic "%s" is outside the guest\'s own sub grants'
                             ' — a panel commands its guest, never its neighbors (spec §7)'
                             % (widget.get('id'), widget['set']))


def validate_panel(widgets, sub_grants):
    if not isinstance(widgets, list):
        raise PanelError('panel must be a list of widgets')
    if len(widgets) > MAX_WIDGETS:
        raise PanelError('panel has %d widgets; the limit is %d' % (len(widgets), MAX_WIDGETS))
    if len(json.dumps(widgets)) > MAX_BYTES:
        raise PanelError('panel encodes over %d bytes' % MAX_BYTES)
    ids = set()
    for widget in widgets:
        if not isinstance(widget, dict) or widget.get('w') not in WIDGETS:
            raise PanelError('unknown widget type %r' % (widget.get('w') if isinstance(widget, dict) else widget))
        wid = widget.get('id')
        if not isinstance(wid, str) or not wid or wid in ids:
            raise PanelError('widget ids must be unique non-empty strings (%r)' % wid)
        ids.add(wid)
        allowed = COMMON + WIDGETS[widget['w']][2]
        for key in widget:
            if key not in allowed:
                raise PanelError('widget %r: unknown field %r' % (wid, key))
        _check_topic_fields(widget, sub_grants)
    return widgets


def validate_schema(fields):
    if not isinstance(fields, list):
        raise PanelError('config schema must be a list of fields')
    if len(fields) > MAX_WIDGETS:
        raise PanelError('config schema has %d fields; the limit is %d' % (len(fields), MAX_WIDGETS))
    if len(json.dumps(fields)) > MAX_BYTES:
        raise PanelError('config schema encodes over %d bytes' % MAX_BYTES)
    keys = set()
    for f in fields:
        if not isinstance(f, dict):
            raise PanelError('config fields must be objects')
        key = f.get('key')
        if (not isinstance(key, str) or not 1 <= len(key) <= 24
                or any(c not in _KEY_CHARS for c in key) or key in keys):
            raise PanelError('config keys must be unique [a-z0-9_]{1,24} (%r)' % key)
        keys.add(key)
        if f.get('w') not in CONFIG_WIDGETS:
            raise PanelError('config field %r: w must be one of %s' % (key, ', '.join(CONFIG_WIDGETS)))
        if 'default' not in f:
            raise PanelError('config field %r has no default' % key)
        validate_value(f, f['default'])
    return fields


def validate_value(field, value):
    """Check one config value against its declared field; returns the value."""
    kind, key = field['w'], field['key']
    if kind == 'toggle':
        if not isinstance(value, bool):
            raise PanelError('%s must be true or false' % key)
    elif kind == 'slider':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PanelError('%s must be a number' % key)
        lo, hi = field.get('min'), field.get('max')
        if lo is not None and value < lo or hi is not None and value > hi:
            raise PanelError('%s must be within %s..%s' % (key, lo, hi))
    elif kind == 'select':
        if value not in field.get('options', []):
            raise PanelError('%s must be one of %s' % (key, field.get('options')))
    elif kind == 'text':
        if not isinstance(value, str) or len(value) > 256:
            raise PanelError('%s must be a string of at most 256 chars' % key)
    return value
