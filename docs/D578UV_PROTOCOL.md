# Anytone AT-D578UV experimental protocol notes

These notes document the interoperable facts used by
`drivers/newchirp_d578uv.py`. The implementation is original NewChirp code.
The Anytone CPS installer and the user's codeplug export are not part of this
repository; they are referenced here as evidence artifacts only.

The D578UV family (D578UV, D578UV PRO, D578UV III / III PLUS) shares the
Anytone "D868-generation" serial protocol and memory-map conventions with the
D868UV / D878UV handhelds, scaled to the mobile 4000-channel layout.

## Evidence

Reference implementations consulted (all GPL or public RE notes):

- qdmr (https://github.com/hmatuschek/qdmr), `lib/`: complete GPL D578UV
  support — `d578uv.cc`, `d578uv_codeplug.cc/.hh`, `d878uv_codeplug.cc/.hh`,
  `d868uv_codeplug.cc/.hh`, `anytone_interface.cc`, `anytone_codeplug.cc/.hh`
  and the matching `*_limits.cc` files. This is the authoritative source for
  the memory map and element layout, and it verifies read-reply checksums.
- dmrconfig (https://github.com/OpenRTX/dmrconfig), `anytone_ht.c`,
  `dmr_serial.c`, `dmr_radio.c`: independent implementation of the same
  transport handshake.
- reald/anytone-flash-tools RE notes (`at-d878uv_protocol.md`,
  `at-d878uv_memory.md`): byte-level transport description.
- dmr-tools/codeplugs `d578uv/v1.21.xml`: reverse-engineered structure
  listing for CPS v1.21, used as a cross-check of the element sizes.

Evidence artifacts supplied by the radio owner (not in the repository):

- CPS installer: `D578UV_Setup_1.21.exe` (Anytone CPS v1.21, Inno Setup
  5.5.7 package)
  - SHA-256:
    `7602fe26c4733e86f4b12d1e34ab55f32dbbd58fd3b381b55ae1f5a38a82949e`
- Codeplug export: `d578-20260528.rdt` (embedded ident `D578UV`, revision
  `V111`)
  - SHA-256:
    `749e4a2898564d208b06da07692d6522b68e75b77fe7d6e4c0d61e9f8a565df0`

The `.rdt` file is a CPS-logical serialization (length-prefixed records,
frequencies in 10 Hz units), not a radio address image. It is therefore used
to validate decode semantics (tone tables, frequency units), not the
address map. The address map comes from qdmr.

## Serial setup

- 115200 baud, 8 data bits, no parity, one stop bit
- No extra flow control (plain 8N1; the USB cable enumerates as a CDC-ACM /
  GD32 serial device)

## Programming handshake

1. Send ASCII `PROGRAM`.
2. Read 3 bytes and require `QX\x06`.
3. Send a single `0x02` byte.
4. Read 16 bytes, the ident reply:

   | Offset | Size | Meaning |
   | --- | --- | --- |
   | 0 | 1 | Fixed `I` |
   | 1 | 7 | Model name, NUL padded (`D578UV`) |
   | 8 | 1 | Band option byte (selects the tx/rx band limits) |
   | 9 | 6 | Firmware version, NUL padded (`V111`) |
   | 15 | 1 | Fixed `0x06` |

The version string does not distinguish the D578UV PRO / III / III PLUS
hardware variants. Known firmware revisions: `V110`, `V111` (CPS v1.21
era). The driver logs a warning for other revisions rather than failing.

## Read framing

Request (6 bytes):

```
'R' + address (4 bytes, big endian) + length (1 byte)
```

Reply (`length + 8` bytes):

```
'W' + address (4 bytes, big endian, echoes the request) + length (1 byte)
    + data (length bytes) + checksum (1 byte) + 0x06
```

The checksum is the low byte of the sum of the address bytes, the length
byte, and the data bytes (qdmr `AnytoneInterface::ReadResponse::check`).

The CPS reads in 16-byte chunks, and so does the driver. Reads terminate
with ASCII `END` followed by a single `0x06` acknowledgment.

Writes use the mirror-image `W` command but are NOT implemented; see
"Read/write state" below.

## Memory map (radio addresses)

The D578UV codeplug is sparse. Relevant regions (qdmr
`D578UVCodeplug` / `D878UVCodeplug` / `D868UVCodeplug` `Offset` blocks,
D578UV inheriting from D878UV inheriting from D868UV):

| Region | Address |
| --- | --- |
| Channel bank 0 (128 channels x 0xD0) | `0x00800000` |
| Channel bank n | `0x00800000 + n * 0x40000` |
| Channel extension block of bank n | bank base + `0x2000` (0x40 per channel) |
| Channel validity bitmap (4000 bits) | `0x024C1500`, 0x200 bytes |
| General settings | `0x02500000` |
| Contacts (DTMF/CTCSS/DMR) | `0x02680000` area |

Channel element geometry (all from qdmr, no per-model override for the
base element):

- Channel element size: `0xD0` bytes (inherited from D868UV)
- 128 channels per bank, 4000 channels total
- A per-channel 0x40-byte extension element lives at bank base +
  `0x2000` + slot * 0x40 (D578UV-specific options such as Bluetooth,
  scrambler keys, roaming; not decoded by the current driver)
- The validity bitmap packs 4000 bits LSB-first (bit n = byte n/8,
  bit n%8)

## Channel element layout (0xD0 bytes)

Offsets below are from qdmr `AnytoneCodeplug::ChannelElement::Offset`
(D868UV base) and its D868UV additions. Multi-byte values are
little-endian unless noted; frequencies are BCD, 8 digits, most
significant pair first, in 10 Hz units.

| Offset | Size | Field |
| --- | --- | --- |
| 0x00 | 4 | RX frequency, BCD8 big-endian, 10 Hz units |
| 0x04 | 4 | TX offset amount, BCD8 big-endian, 10 Hz units |
| 0x08 | 1 | bit 0-1 channel mode (0 analog, 1 digital, 2 mixed analog, 3 mixed digital); bit 2-3 power (0 low, 1 middle, 2 high, 3 max); bit 4 bandwidth (1 wide, 0 narrow); bit 6-7 repeater mode (0 simplex, 1 `+`, 2 `-`) |
| 0x09 | 1 | bit 0-1 RX signaling (0 none, 1 CTCSS, 2 DCS); bit 2-3 TX signaling; bit 6 RX only; bit 7 talkaround |
| 0x0A | 1 | TX CTCSS table index |
| 0x0B | 1 | RX CTCSS table index |
| 0x0C | 2 | TX DCS codeword, bit 9 = inverted |
| 0x0E | 2 | RX DCS codeword, bit 9 = inverted |
| 0x10 | 2 | Custom CTCSS frequency, 0.1 Hz units |
| 0x14 | 4 | Digital contact index |
| 0x18 | 1 | Radio ID table index |
| 0x1A | 1 | bit 0-1 admission criterion (0 always, 1 channel free, 2 different color code, 3 same color code) |
| 0x1B | 1 | Scan list index (`0xFF` = none) |
| 0x1C | 1 | RX group list index (`0xFF` = none) |
| 0x20 | 1 | DMR color code |
| 0x21 | 1 | bit 0 DMR time slot (0 = TS1, 1 = TS2) |
| 0x23 | 16 | Channel name, ASCII, NUL padded |

CTCSS table (qdmr `AnytoneCodeplug::CTCSS`, 51 entries plus one custom
slot at index 51): 62.5, 67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4,
88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0,
127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5,
167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6,
199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3,
254.1 Hz. Index 51 selects the custom CTCSS frequency at offset `0x10`.

DCS codes are stored as raw 9-bit binary codewords; CHIRP identifies DCS
by the octal representation of the codeword (codeword `0o023` becomes
CHIRP DCS 23). Bit 9 of the stored word marks the inverted polarity.

## Driver image format

Because the radio layout is sparse, the driver image is a *packed* channel
array rather than an address dump:

- `0x000000` .. `0xCB000`: 4000 channel elements in memory-number order,
  0xD0 bytes each
- `0xCB000` .. `0xCB200`: the 4000-bit channel validity bitmap

`get_raw_memory()` returns the repr of the packed element.

## Read/write state

- Read: implemented, validated offline against a protocol-accurate
  simulator; not yet exercised against physical hardware
- Write: every write entry point (`sync_out`, `set_memory`,
  `set_raw_memory`) raises `RadioError`, following the NewChirp
  read-only-first policy
- Settings, contacts, scan lists, zones, and the per-channel extension
  elements are not decoded yet

## Next milestones

1. Physical read: connect the radio with the USB programming cable,
   confirm the ident (`D578UV`, band byte, `V111` or newer), and capture
   a complete successful read for an evidence image.
2. Compare decoded channel values against the owner's CPS v1.21 codeplug
   (`d578-20260528.rdt`) for the same channels.
3. Decode the channel extension elements and D578UV-specific options.
4. Only after a validated read: design and hardware-test writes.
