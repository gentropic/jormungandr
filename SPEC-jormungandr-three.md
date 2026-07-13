# jormungandr three — ESP-NOW, the off-WiFi transport

A WiFi leaf uplinks over TCP, which means it needs an IP stack, an association, and a
DHCP lease — and it pays lwIP's fixed buffer pool the whole time. ESP-NOW needs none
of that: it rides the 802.11 MAC layer directly (vendor action frames), so it never
touches lwIP — structurally dodging the exact wall that broke the C3 as a full node —
and it works for a leaf that is off WiFi entirely, or asleep on a battery between
sends. This spec is ESP-NOW as a **bus transport**: a leaf's bus reaches the cluster
through a flagship **gateway** that speaks both.

## 0. What the silicon actually gives us (probed on a C3, not assumed)

The design rests on these, measured on `ESP32_GENERIC_C3` v1.28.0:

- `espnow` is present, and so is `aioespnow` — an **async** interface, so receiving
  frames never blocks the event loop.
- `send()` returns a **bool: the MAC-layer ACK** (`False` to an absent peer, `True`
  on broadcast). This is per-frame delivery status — ESP-NOW is not blind UDP.
- **Encryption is built in**: `set_pmk(key)` sets a primary key, `add_peer(mac, lmk=…)`
  gives a peer its own key, and traffic to an encrypted peer is AES-CCM on the air.
- ESP-NOW **coexists with a connected WiFi station** (sent fine with STA up on
  channel 11), so a flagship can run WiFi and be a gateway at once.
- **Encrypted peers cap at 6** (`ESP_ERR_ESPNOW_FULL`). A gateway securely serves six
  leaves; a seventh needs a second gateway (or app-layer crypto, §6).
- **The encrypted exchange works end to end, board to board.** The S3 (c510) sent
  AES-encrypted frames to the C3 with keys set on both sides; the C3 received and
  *decrypted* them (`received 3: [b'jorm-encrypted-from-S3 #0', ...]`). This is the
  whole premise, confirmed on real silicon — not just APIs that exist, but two jorm
  boards exchanging encrypted frames over the air.
- **The primitives for app-layer crypto are present too** (for §6's uncapped path):
  `cryptolib` AES-CBC (CTR is not compiled in this build), `hashlib.sha256`,
  `os.urandom`; ~0.66 ms to seal a 250-byte frame.

## 1. The gateway

A **flagship gateway** advertises `espnow` among its transports and runs both radios.
It receives ESP-NOW frames from its leaves, reassembles them into bus messages, and
publishes them onto the cluster bus; and it forwards the slice of bus traffic a leaf
subscribed to back down as ESP-NOW frames. It is the ESP-NOW ↔ bus bridge, exactly as
a WiFi leaf's flagship is its uplink — the difference is only the wire.

Leaves do not talk to each other over the gateway in v1 (single hop). A leaf relaying
another leaf's frames is possible later (ESP-NOW forwarding is cheap — it never
touches lwIP, so it does not violate the "a leaf runs no IP server" rule), and left
for a chapter of its own.

## 2. Framing

Each ESP-NOW frame is ~250 bytes. A frame is a tiny binary header plus payload:

```
byte 0     : type   (HELLO, WELCOME, PUB, SUB, CMD, RESULT, ACK, ...)
bytes 1-2  : msg_id  (per-sender, wraps)
byte 3     : index   (fragment number)
byte 4     : count   (total fragments)
bytes 5..  : payload (a slice of the UTF-8 bus message, or a control body)
```

**The common case is one frame.** A sensor reading, a state update, a command are all
well under ~240 bytes, so `count == 1` and there is nothing to reassemble. Only a fat
message — a guest bundle for `install`, a big panel — spans frames.

## 3. Fragments, for the full bus

`install` and other large messages fragment: the sender splits the encoded message
into `count` slices, tagged with a shared `msg_id` and ascending `index`. The receiver
holds partial messages in a **bounded reassembly table** keyed by `(peer, msg_id)`,
completes a message when all `count` fragments arrive, and drops any partial older than
a few seconds (a lost fragment must not pin memory forever). The bus's own 4 KB message
cap bounds a message to ~17 frames, so the table stays small.

So the answer to "full bus, even with fragments" is yes: single-frame for everything
common, fragmentation only where a message is genuinely large, and the reassembly
table is the only new state.

## 4. Reliability, in layers

ESP-NOW gives the bottom layer for free; we add two:

1. **MAC ACK** — `send()` says whether the frame reached the peer's radio.
2. **Frame retransmit** — resend a frame `send()` reported `False`, a few times, before
   giving up. For a fragmented message, only the un-ACKed fragments resend.
3. **App-level req/result** — the request/result pattern the leaf-management channel
   already uses (a `req` id, a `result` reply) rides straight over ESP-NOW: an actuator
   command or a guest `start` is confirmed by the leaf *doing* it, not merely by a
   frame arriving.

Loss-tolerant sensor spam uses none of it (fire and forget); commands and installs use
the whole stack. Ordering is per-`msg_id` within a fragmented message; across messages
the bus was never ordered.

## 5. Discovery — auto, over the air

The same beacon idea as WiFi cluster discovery (one §1), at the MAC layer:

- A gateway periodically **broadcasts** a `HELLO` naming its cluster and channel.
- A leaf listens; on hearing a `HELLO` for its cluster it replies (unicast) and the
  two register each other as encrypted peers.
- **The channel wrinkle** — ESP-NOW peers must share the WiFi channel, and a pure-ESP-NOW
  leaf (no WiFi) does not know the gateway's up front. So the leaf **scans channels**:
  broadcast-and-listen on each in turn until a gateway answers, then locks to it. A few
  seconds at boot. (A gateway's channel is its WiFi channel, set by the AP.)

A leaf may still be **seeded** with a gateway MAC + channel in settings, for a fixed
install or to skip the scan — discovery is the convenience, not the only way in.

## 6. Security

There are two ways to encrypt, and they trade the same thing in opposite directions.

**Option A — ESP-NOW's built-in AES-CCM.** Zero code: `set_pmk` + `add_peer(lmk=…)`
and the MAC hardware encrypts each peer's traffic (confirmed board-to-board, §0). Keys
derive from the shared token so only a token-holder can bring a peer up. Bulletproof,
and **capped at 6 encrypted peers per node** — the hardware key store has six slots.
Good for a small cluster; a hard ceiling for a large one.

**Option B — app-layer authenticated encryption (recommended, uncapped).** The 6-cap
is only on the *built-in* per-peer CCM. The chip's general AES (via `cryptolib`) has no
such limit, and *unencrypted* ESP-NOW peers go to ~20 (broadcast, unlimited). So send
over unencrypted peers and seal the payload ourselves. Confirmed present on the C3:
`cryptolib` AES-**CBC** (CTR is not compiled in), `hashlib.sha256`, `os.urandom`; a
250-byte frame seals in ~0.66 ms, negligible at frame rates. The construction is the
standard **Encrypt-then-MAC**:

- Two keys HKDF-derived from the shared token — one for AES, a *separate* one for the
  MAC (key separation is not optional).
- Per frame: a random 16-byte IV (`os.urandom`), AES-CBC of the padded payload, then
  HMAC-SHA256 over `IV‖ciphertext` truncated to an 8-byte tag. Overhead ~24 bytes,
  leaving ~210 for payload.
- Receiver verifies the tag **first**, in constant time, and rejects on mismatch —
  that is the integrity and authenticity; only then does it decrypt. A per-peer
  counter (carried inside the sealed payload) rejects replays.

This is not a grudging workaround: the cluster's trust model is **already a single
shared-token group**, and a token-derived *group* key matches that exactly, where
ESP-NOW's per-peer CCM was finer-grained than jorm's trust ever needed — and capped for
its trouble. The honest cost is that EtM is ours to get right (unique IVs, constant-time
compare, distinct keys); we follow the standard construction and do not improvise.

**The handshake, either option.** Broadcast `HELLO`/`WELCOME` can't be encrypted, so
they carry no secret — only a **token-derived challenge/response** with a nonce, so an
eavesdropper still can't join without the token and a replay is rejected.

(A third path — a custom firmware raising the IDF encrypted-peer count to 17 — is a
firmware project and *still* capped, so it is the worst of both. Not recommended.)

## 7. Transports are advertised

A node's beacon and `/api/cluster` gain a `transports` list next to `runtimes`:
`wifi`, `espnow`, `ble`. A gateway advertises `["wifi", "espnow"]`; a pure-ESP-NOW leaf
advertises `["espnow"]` and is represented through its gateway; the UI renders an
ESP-NOW leaf under the gateway that speaks for it. Placement and representation read
`transports` the same way they read `runtimes`.

## 8. What is a leaf on ESP-NOW, concretely

The dumb-leaf and smart-leaf roles (two §2, §7) are unchanged in what they *do* — sense,
actuate, host guests. Only their uplink changes: instead of `wsclient` to a flagship's
`/api/bus`, an `espnow` transport to a gateway MAC, carrying the same pub/sub/cmd/result
messages. The plan is for the transport to be swappable under the same leaf logic, so a
leaf is "WiFi or ESP-NOW" by settings, not by a different program.

## Status

**Slice 1: built and proven on silicon.** A C3 with **no WiFi association** (radio on
the gateway's channel only) hosts `pinger` and reaches c510's bus over ESP-NOW:

```
$sys/leaf/leaf-c3 = {"transport":"espnow", ...}        # announced over the air
espnow leaf e8:06:90:65:96:04 joined                   # gateway log
pinger/tick {"n":322,"node":"leaf-c3"}                 # on c510's bus, no lwIP anywhere
jorm leaf leaf-c3 stop pinger  ->  ok                  # command DOWN, result UP
```

`jorm/seal.py` (Encrypt-then-MAC, option B — uncapped, 20 checks), `jorm/espnow.py`
(seal ‖ fragment ‖ ACK-retried send; reassemble ‖ unseal ‖ replay-check on recv),
`jorm/gateway.py` (the flagship's ESP-NOW ↔ bus bridge, both directions), and a
leaf-host uplink that swaps `wsclient` for the espnow link under the same push/command
logic. Sealed AES on the air, fragmentation ready from the start, single-hop, seeded
discovery.

**Not yet built:** auto-discovery (broadcast HELLO + channel scan; today the leaf is
seeded the gateway MAC/channel), fragment-level retransmit (today a lost fragment drops
the whole message to the reassembly TTL), multi-hop relay, and BLE.
