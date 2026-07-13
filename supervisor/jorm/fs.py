"""The node's filesystem, over HTTP — so a shell has something to mount.

geas binds a VFS of nine methods; this is the other end of them. The flash is the
filesystem: /guests, /lib, /web, the supervisor's own modules. It is jailed to the
flash root, and it guards exactly two things:

  * **settings.json is not readable.** It holds the WiFi PSK and the bearer token.
    The token is already in the hand of whoever is asking — but the PSK is a
    different secret, belonging to a network rather than a node, and there is no
    reason a shell needs it and every reason not to make it a single `cat` away.

  * **The supervisor's own code is read-only here.** `main.py`, `boot.py`, `jorm/`
    are managed by OTA, which stages, trials, and *rolls back*. A shell that can
    `rm main.py` with no way back is a shell that betrays §1 — the node always
    comes back reachable. Delete them here and you have a brick and a soldering
    iron; delete them through OTA and the node reverts itself. So: use the door
    that has a lock on the inside.

Everything else — guests, libraries, data, the web assets — is yours.
"""
import os

from jorm.fsutil import UnsafePath, safe_relpath, write_atomic

SECRET = ('settings.json',)
SUPERVISOR = ('main.py', 'boot.py', 'jorm')


class FsError(Exception):
    pass


def _root(path):
    return path.split('/')[0] if path else ''


def check_read(path):
    if _root(path) in SECRET:
        raise FsError('settings.json holds the wifi psk and the bearer token — '
                      'not readable here, on purpose')


def check_write(path):
    check_read(path)
    if _root(path) in SUPERVISOR:
        raise FsError('%s is the supervisor\'s own code — it is managed by OTA, '
                      'which stages, trials and rolls back. Deleting it here would '
                      'leave a brick; deleting it there leaves a node that reverts '
                      'itself (spec §11.19)' % _root(path))


def norm(path):
    """A flash-relative path, jailed. '' and '/' are the root."""
    path = (path or '').strip('/')
    if not path:
        return ''
    try:
        return safe_relpath(path)
    except UnsafePath as e:
        raise FsError(str(e))


# The flash root is the working directory — '/' on a board, sim/fs on the sim.
# Listing '/' would give you the *host's* root on the unix port, which is not the
# node's flash and never was.
ROOT = '.'


def is_dir(path):
    try:
        return bool(os.stat(path or ROOT)[0] & 0x4000)
    except OSError:
        raise FsError('no such path: /%s' % path)


def listdir(path):
    entries = []
    for name in sorted(os.listdir(path or ROOT)):
        full = (path + '/' + name) if path else name
        try:
            st = os.stat(full)
        except OSError:
            continue
        d = bool(st[0] & 0x4000)
        # MicroPython's st_size for a directory is not a size — it is whatever the
        # filesystem left in the field. A number that means nothing is worse than
        # no number: report 0 and let a directory be a directory.
        entries.append({'name': name, 'dir': d, 'size': 0 if d else st[6],
                        'secret': name in SECRET, 'ota': name in SUPERVISOR})
    return entries


def read(path):
    check_read(path)
    with open(path, 'rb') as f:
        return f.read()


def stat(path):
    try:
        st = os.stat(path or ROOT)
    except OSError:
        raise FsError('no such path: /%s' % path)
    d = bool(st[0] & 0x4000)
    return {'path': '/' + path, 'dir': d, 'size': 0 if d else st[6]}


def write(path, data):
    check_write(path)
    write_atomic(path, data)


def mkdir(path):
    check_write(path)
    try:
        os.mkdir(path)
    except OSError:
        raise FsError('cannot mkdir /%s (it may already exist)' % path)


def remove(path):
    check_write(path)
    try:
        if is_dir(path):
            os.rmdir(path)         # non-empty dirs refuse, as they should
        else:
            os.remove(path)
    except OSError as e:
        raise FsError('cannot remove /%s (%s)' % (path, e))


def rename(src, dst):
    check_write(src)
    check_write(dst)
    try:
        os.rename(src, dst)
    except OSError as e:
        raise FsError('cannot rename /%s (%s)' % (src, e))
