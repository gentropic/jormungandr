"""Supervisor-owned guest configuration (spec §7): /guests/<id>/config.json,
schema-validated on every write, editable even while the guest is stopped —
exactly like VM options.

The declared schema is retained on the bus at $ui/<id>/config AND persisted to
a sidecar (.config-schema.json), so a stopped guest stays configurable across
node reboots. Panels, by contrast, live only in the retained table — a panel
is what the guest is doing; config is how it should behave.
"""
import json

from jorm.fsutil import write_atomic
from jorm.panels import PanelError, validate_schema, validate_value


def _path(guest, name):
    return guest.dir + '/' + name


def load(guest):
    """Load schema sidecar + values into the guest object (best effort)."""
    try:
        with open(_path(guest, '.config-schema.json')) as f:
            guest.cfg_schema = validate_schema(json.load(f))
    except (OSError, ValueError, PanelError):
        guest.cfg_schema = None
    try:
        with open(_path(guest, 'config.json')) as f:
            guest.cfg_values = json.load(f)
    except (OSError, ValueError):
        guest.cfg_values = {}


def declare(sup, guest, fields):
    """hal.ui.config lands here: validate, persist, materialize defaults, retain."""
    validate_schema(fields)
    guest.cfg_schema = fields
    write_atomic(_path(guest, '.config-schema.json'), json.dumps(fields))
    changed = False
    for f in fields:
        if f['key'] not in guest.cfg_values:
            guest.cfg_values[f['key']] = f['default']
            changed = True
    if changed:
        write_atomic(_path(guest, 'config.json'), json.dumps(guest.cfg_values))
    sup.sys_publish('$ui/%s/config' % guest.id, {'v': 0, 'fields': fields}, retain=True)


def view(guest):
    return {
        'values': guest.cfg_values,
        'schema': guest.cfg_schema,
        'pending_restart': sorted(guest.cfg_pending),
        'undeclared': sorted(k for k in guest.cfg_values
                             if not _field(guest, k)),  # preserved but flagged
    }


def _field(guest, key):
    for f in guest.cfg_schema or []:
        if f['key'] == key:
            return f
    return None


def write(guest, updates):
    """PUT lands here → {applied_live, pending_restart}. Fail closed per key."""
    if guest.cfg_schema is None:
        raise PanelError('guest "%s" has declared no config schema — nothing to validate against'
                         % guest.id)
    if not isinstance(updates, dict) or not updates:
        raise PanelError('expected a JSON object of {key: value}')
    checked = {}
    for key, value in updates.items():
        field = _field(guest, key)
        if field is None:
            raise PanelError('"%s" is not a declared config key' % key)
        checked[key] = (field, validate_value(field, value))

    applied_live, pending = [], []
    for key, (field, value) in checked.items():
        changed = guest.cfg_values.get(key) != value
        guest.cfg_values[key] = value
        if guest.state != 'running' or not changed:
            guest.cfg_pending.discard(key)
            continue
        if field.get('live'):
            applied_live.append(key)
            for tap in guest.cfg_watchers:
                tap.push((key, value))
        else:
            pending.append(key)
            guest.cfg_pending.add(key)
    write_atomic(_path(guest, 'config.json'), json.dumps(guest.cfg_values))
    return {'applied_live': sorted(applied_live), 'pending_restart': sorted(guest.cfg_pending)}
