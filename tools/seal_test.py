import sys
sys.path.insert(0, 'supervisor')
from jorm.seal import Sealer
fails = 0
def ok(c, m):
    global fails
    print(('  ok: ' if c else '  FAIL: ') + m)
    if not c: fails += 1

s = Sealer('a-shared-cluster-token')

# roundtrip across sizes (including block edges and empty)
for n in (0, 1, 15, 16, 17, 31, 200, 240):
    pt = bytes((i * 7) & 0xff for i in range(n))
    out = s.unseal(s.seal(pt))
    ok(out == pt, 'roundtrip %d bytes' % n)

# two seals of the same plaintext differ (random IV)
a, b = s.seal(b'same'), s.seal(b'same')
ok(a != b, 'same plaintext -> different ciphertext (fresh IV)')
ok(s.unseal(a) == b'same' and s.unseal(b) == b'same', 'both still decrypt')

# tamper: flip each region and confirm rejection
blob = bytearray(s.seal(b'important message'))
for pos, label in ((0, 'IV'), (20, 'ciphertext'), (len(blob) - 1, 'tag')):
    t = bytearray(blob); t[pos] ^= 0x01
    ok(s.unseal(bytes(t)) is None, 'tampered %s rejected' % label)

# wrong key rejected
ok(Sealer('different-token').unseal(bytes(blob)) is None, 'wrong key rejected')

# truncation / garbage rejected
for bad in (b'', b'short', bytes(39), bytes(40)):
    ok(s.unseal(bad) is None, 'garbage len %d rejected' % len(bad))

# overhead is what the spec claims (~24 bytes)
ov = len(s.seal(b'')) - 0
ok(ov <= 40, 'empty seal overhead %d bytes (IV+pad+tag)' % ov)

print('\nSEAL: %s' % ('ALL PASS' if not fails else '%d FAILED' % fails))
sys.exit(1 if fails else 0)
