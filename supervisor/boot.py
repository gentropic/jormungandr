# Supervisor OTA: apply, trial, roll back. Runs before main.py, every boot.
#
# This file imports NOTHING from jorm — a broken jorm/ is precisely what it
# exists to recover from. os and machine only.
#
# The protocol (spec §11), and it is the watchdog's own logic applied to code:
#
#   1. Files are staged over HTTP into staged/ (PUT /api/node/files/<path>).
#   2. POST /api/node/update drops the .update marker and reboots.
#   3. This file sees .update, backs up every file it is about to overwrite into
#      backup/, copies staged/ over the live tree, and marks the boot .trial.
#   4. main.py, once the API has been serving for a while, deletes .trial and the
#      backup: the node came back reachable, so the update is confirmed.
#   5. If it did NOT come back — the new code crashed, hung, or the watchdog
#      reset it — then the next boot finds .trial still there. That is the whole
#      signal: an unconfirmed trial is a failed one. Restore the backup, boot the
#      old code, and badge the node.
#
# A bad push costs a reboot, not a cable. Which is the §1 promise — the node
# always comes back reachable — extended from guests to the supervisor itself.
import machine
import os

STAGED = 'staged'
BACKUP = 'backup'


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _isdir(path):
    try:
        return bool(os.stat(path)[0] & 0x4000)
    except OSError:
        return False


def _mkdirs(path):
    parts = path.split('/')[:-1]
    grown = ''
    for part in parts:
        grown = grown + '/' + part if grown else part
        try:
            os.mkdir(grown)
        except OSError:
            pass


def _walk(root, prefix=''):
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        full = root + '/' + name
        rel = prefix + name
        if _isdir(full):
            for sub in _walk(full, rel + '/'):
                yield sub
        else:
            yield rel


def _copy(src, dst):
    _mkdirs(dst)
    with open(src, 'rb') as fin, open(dst + '.tmp', 'wb') as fout:
        while True:
            chunk = fin.read(1024)
            if not chunk:
                break
            fout.write(chunk)
    os.rename(dst + '.tmp', dst)


def _rmtree(root):
    for name in os.listdir(root):
        full = root + '/' + name
        if _isdir(full):
            _rmtree(full)
        else:
            os.remove(full)
    os.rmdir(root)


def _rollback():
    print('[sys] update did not confirm — rolling back')
    for rel in _walk(BACKUP):
        _copy(BACKUP + '/' + rel, rel)
    _rmtree(BACKUP)
    os.remove('.trial')
    with open('.rolled-back', 'w') as f:
        f.write('an update was applied and the node did not come back — reverted')
    print('[sys] rolled back to the previous supervisor')


def _apply():
    print('[sys] applying staged update')
    if _exists(BACKUP):
        _rmtree(BACKUP)
    os.mkdir(BACKUP)
    staged = list(_walk(STAGED))
    for rel in staged:
        if _exists(rel):                       # keep what we are about to clobber
            _copy(rel, BACKUP + '/' + rel)
    for rel in staged:
        _copy(STAGED + '/' + rel, rel)
    _rmtree(STAGED)
    os.remove('.update')
    with open('.trial', 'w') as f:
        f.write(','.join(staged))
    print('[sys] update applied (%d files) — this boot is a trial' % len(staged))


if _exists('.trial'):
    # We are here with .trial still on disk, which means the last boot never
    # confirmed. Whatever we shipped did not come back. Undo it.
    if _exists(BACKUP):
        _rollback()
    else:
        os.remove('.trial')   # nothing to restore; do not loop on it
elif _exists('.update'):
    try:
        _apply()
    except Exception as e:
        print('[sys] update failed to apply:', e)
        try:
            os.remove('.update')
        except OSError:
            pass
