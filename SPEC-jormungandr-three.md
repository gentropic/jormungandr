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

Encrypted on the air, authenticated at the handshake:

- **Keys from the shared token.** The cluster already has one shared bearer token; the
  ESP-NOW PMK and a per-peer LMK are derived from it (a hash), so only a node holding
  the token can bring up an encrypted peer whose traffic the other side can read.
- **The handshake is the soft spot, and it is handled.** Broadcast frames cannot be
  encrypted (encryption is per-unicast-peer), so `HELLO`/`WELCOME` travel in the clear.
  They carry no secret — they carry a **token-derived challenge/response**, so an
  eavesdropper who hears the broadcast still cannot complete the handshake without the
  token, and a replay is rejected by a nonce.
- **The 6-peer cap is a security parameter too**: six encrypted leaves per gateway.
  Beyond that, a second gateway, or (later) app-layer AES over unencrypted peers to
  trade CPU for headcount.

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

**Probed, specced, not yet built.** Silicon facts in §0 are measured. The first slice:
a `jorm/espnow.py` transport (framing, reassembly, retransmit, encrypted peers,
broadcast discovery), a gateway task on the flagship, and a smart-leaf uplink that runs
over it — proven by a real C3 leaf reaching a real S3 over ESP-NOW with the WiFi uplink
turned off. Single-hop; fragmentation in from the start (you wanted the full bus).
Multi-hop relay, BLE, and app-layer crypto for >6 leaves are named for later.
