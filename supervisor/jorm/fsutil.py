import os

_NAME_CHARS = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-'


class UnsafePath(Exception):
    pass


def safe_name(name):
    if (not name or name.startswith('.') or '..' in name
            or any(c not in _NAME_CHARS for c in name)):
        raise UnsafePath('unsafe file name %r' % name)
    return name


def safe_relpath(path):
    """A jailed relative path: safe segments, subdirs allowed, no escapes."""
    if not isinstance(path, str) or path.startswith('/'):
        raise UnsafePath('unsafe path %r' % path)
    for seg in path.split('/'):
        safe_name(seg)
    return path


def write_atomic(path, data):
    tmp = path + '.tmp'
    # bytes go out as bytes: a gzipped UI is not text, and decoding it to write it
    # is how you turn a working file into a 500
    mode = 'wb' if isinstance(data, (bytes, bytearray)) else 'w'
    with open(tmp, mode) as f:
        f.write(data)
    os.rename(tmp, path)


def ensure_dir(path):
    try:
        os.mkdir(path)
    except OSError:
        pass


def rmtree(path):
    for name in os.listdir(path):
        sub = path + '/' + name
        if os.stat(sub)[0] & 0x4000:
            rmtree(sub)
        else:
            os.remove(sub)
    os.rmdir(path)


def tree_size(path):
    total = 0
    try:
        names = os.listdir(path)
    except OSError:
        return 0
    for name in names:
        sub = path + '/' + name
        st = os.stat(sub)
        total += tree_size(sub) if st[0] & 0x4000 else st[6]
    return total
