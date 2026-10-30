# Session handoff — Anytone AT-D578UV driver (read-only phase complete)

Handoff written 2026-10-30. Read this before touching the D578UV driver.
Pair with `docs/D578UV_PROTOCOL.md` (protocol/memory map) and the
`Anytone AT-D578UV` entry in `docs/DRIVER_STATUS.md`.

## Where things stand

The from-scratch D578UV driver is written, offline-tested, and documented.
It is **read-only**: every write entry point raises `RadioError` until the
driver is validated against the physical radio (repo policy, same as the
RT-950 and CS750). Nothing has been committed yet — all changes are in the
working tree.

Owner hardware: D578UV III PLUS with the Anytone USB programming cable.
Owner evidence artifacts (NOT in the repo) live in `/home/gordon/Download`
(no trailing "s" — a `Downloads/` dir does not exist on this machine):

| File | SHA-256 | Notes |
| --- | --- | --- |
| `d578-20260528.rdt` | `749e4a2898564d208b06da07692d6522b68e75b77fe7d6e4c0d61e9f8a565df0` | CPS v1.21 codeplug export; embedded ident `D578UV` / `V111` |
| `D578UV_Setup_1.21.exe` | `7602fe26c4733e86f4b12d1e34ab55f32dbbd58fd3b381b55ae1f5a38a82949e` | Anytone CPS v1.21 installer (Inno Setup 5.5.7) |

## Files changed / added (uncommitted)

- `drivers/newchirp_d578uv.py` — NEW, ~526 lines. `AnytoneD578UV`
  registered as `Anytone_AT-D578UV`, bases `(CloneModeRadio,
  ExperimentalRadio)`, BAUD 115200 8N1, no flow control. Packed image =
  4000 channels x 0xD0 + 0x200-byte validity bitmap. Writes disabled
  (`sync_out`, `set_memory` raise `RadioError`).
- `test/d578uv_driver_test.py` — NEW, ~399 lines. Protocol-accurate fake
  serial (`FakeD578UVSerial`), 0xD0 channel-record builder, region-list
  responder. 7 tests.
- `docs/D578UV_PROTOCOL.md` — NEW. Full protocol, memory map, element
  layout, tone tables, driver image format, next milestones.
- `docs/DRIVER_STATUS.md` — MODIFIED. Inventory row + detailed entry,
  `Last updated` bumped to 2026-10-30.

## Verified state (all re-run 2026-10-30)

```
d578uv: OK            (7 tests)
dm32uv: OK            (5 tests)
gm32:   OK (skipped=1)
rt950:  OK            (6 tests)
```

`git status` at handoff: exactly the 4 paths above; nothing else touched.

## Sources used (do not re-derive)

- **qdmr** (hmatuschek/qdmr, GPL) — authoritative memory map + decode.
  A full master tarball is extracted at `/tmp/qdmr/lib/` (may be gone
  after reboot; re-download from the URL below if needed).
- dmrconfig (OpenRTX) `anytone_ht.c` / `dmr_serial.c` — transport
  cross-check (`/tmp/` copies may be gone after reboot).
- reald/anytone-flash-tools RE notes — transport byte-level description.
- dmr-tools/codeplugs `d578uv/v1.21.xml` — structure cross-check.

Locked-in decode facts (verified in qdmr source, encoded in tests):
DCS = raw 9-bit word, bit 9 = inverted, CHIRP code = octal-as-decimal;
CTCSS = 51-entry table, index 51 = custom tone in 0.1 Hz units;
admit = 2-bit enum 0..3; talkaround = bit 7 of the signaling byte;
time slot bit 0 = TS2; frequencies are BCD8 in 10 Hz units;
checksum = sum(addr4 + len + data) & 0xFF with `0x06` terminator.

The `.rdt` is a CPS-logical format (length-prefixed records), **not** a
radio address image — it validates decode semantics only.

## Environment gotchas

- Tests hardcode `build_support/chirp-20261023` but
  `scripts/update_chirp.sh` creates `chirp-<pin>`. Recreate the symlink
  before running ANY test:
  `ln -sfn chirp-$(cat scripts/chirp-source.pin) build_support/chirp-20261023`
- No `gh` CLI (exit 127) — use git + curl against api.github.com.
- No passwordless sudo on this machine (ubu-gpu) — anything needing root
  must be run by the user.
- No serial capture exists. The user suggested Wireshark over USB as an
  unexplored route; treat it as optional evidence for the physical read,
  not a prerequisite.

## Next steps: read-only testing on actual hardware

Everything below is read-path only. Do NOT enable writes, do NOT upload.

1. Recreate the CHIRP symlink (gotcha above) and confirm the suite is
   still green before touching hardware.

2. Connect the radio and find the port. The Anytone cable enumerates as
   a CDC-ACM device, typically `/dev/ttyACM0`:
   ```
   dmesg | tail        # or: ls /dev/ttyACM* /dev/ttyUSB*
   ```
   The user may need to be in the `dialout` group (check with `id`); if
   permission is denied, have the user run the command or add the group.

3. Put the radio in programming/clone mode per the D578UV manual before
   the read (the CPS prompts for this; the radio rejects `PROGRAM`
   otherwise).

4. Do the first read with the headless bridge, which handles the
   ident + sparse reads and saves an image + JSON channel dump:
   ```
   cd /home/gordon/newchirp
   python3 scripts/chirp_bridge.py \
     --source build_support/chirp-20261023 \
     --vendor assets/python/pyserial-3.5-py2.py3-none-any.whl \
     --newchirp-driver-dir drivers \
     download --model Anytone_AT-D578UV --port /dev/ttyACM0
   ```
   The bridge prints JSON with `imagePath` (a temp `.img` file) and the
   decoded `channels` array — copy the image somewhere durable
   (suggest `/home/gordon/Download/d578-read1.img`) and keep the JSON.

5. What to check on the first read:
   - Ident reply is 16 bytes: `I` + `D578UV` + band byte + `V111` +
     `0x06`. Unknown firmware revisions log a warning (by design — the
     ident does not distinguish PRO / III / III PLUS). Record the actual
     band byte and version string in the DRIVER_STATUS entry.
   - Checksum verification must pass on every 16-byte chunk. If replies
     fail `ReadResponse::check`, stop and compare against
     `/tmp/qdmr/lib/anytone_interface.cc` before suspecting the radio.
   - Empty vs occupied channels: the validity bitmap at `0x024C1500`
     drives which of the 4000 slots decode. Compare the count of
     non-empty channels against what the CPS shows for the same plug.
   - The radio's actual channel values (frequencies, tones, color codes)
     must match what the owner sees in CPS v1.21 — spot-check ~10
     channels, especially DMR ones.

6. Optional but cheap insurance: capture the session with usbmon while
   the OWNER runs the official CPS "Read from radio" once:
   ```
   sudo modprobe usbmon            # user types the password
   # wireshark: capture on usbmon<N> where N = bus number of the cable
   ```
   Diff the CPS traffic against what the driver sends. Only needed if
   step 5 shows anything unexpected.

7. Cross-validate the read against the owner's `.rdt`: decode a few
   channels from `d578-20260528.rdt` (CPS-logical, 10 Hz units) and
   confirm the driver's decoded values agree for the same channels.
   A small throwaway script in the scratch dir is fine; do not commit it.

8. After a clean physical read:
   - Flip `docs/DRIVER_STATUS.md` hardware-validation wording from
     "none yet" to what actually happened (date, port, ident observed).
   - Keep writes DISABLED; the next milestone after read validation is
     designing the write path (mirror `W` commands) and hardware-testing
     it on a sacrificial codeplug — separate decision, ask the owner.
   - Commit the four files (one commit, message referencing the read
     evidence). Keep user artifacts out of the commit per AGENTS.md.

Known open items (not blocking the read):
- Channel extension elements (0x40 bytes per channel at bank+0x2000) and
  settings/contacts/scan-lists/zones are not decoded yet.
- The `chirp-20261023` hardcoded-path inconsistency should eventually be
  fixed in the repo (tests vs `update_chirp.sh` naming); the symlink is
  a local workaround, not committed.

## Known issue: Hamlib D578 CAT backend (next-session target)

Owner reports Hamlib rig 37001 (AT-D578UVIII) "ran a few commands and
then the interface failed on the radio side; everything after timed out".
Hardware context: Digirig (logic levels) CAT cable on the mic connector;
mic pin 1 is shared URX-serial / hardware PTT, `Hand Type` must be
`Uart-Det` for the CAT cable, `Volt-Det` reverts to hardware PTT.

What current master says (fetched 2026-10-30, BACKEND_VER 20260629,
Michael Black W9MDB — backend is actively maintained):

- `rigs/anytone/{anytone.c,anytone.h,d578.c}` (~780 lines total).
- Two `commode` config values:
  - `commode=0` (default): direct serial **mic** protocol — PTT only,
    no freq/VFO commands; sending freq commands here is a no-op/fragile.
  - `commode=1`: enters **BT-01 "ADATA/COM MODE"** — adds freq, VFO,
    clock control, but the radio locks its display to "EXTERNAL CABLE
    MODE" while in it (must be exited to use the radio normally).
- Radio-side evidence in our own V2.08 `.CDD` image: plaintext strings
  `EXTERNAL`, `BT MODE`, `CABLE MODE`, `D578UV COM MODE`, `COM CHECK
  END`, `+ADATA:`, `ADATA=01,` — the ADATA/COM-MODE handler is in the
  main MCU firmware and can be RE'd locally (see D578UV_PROTOCOL.md
  evidence dir for the file path).
- Likely root causes to test first: (a) rigctl default `commode=0`
  while issuing freq/mode commands the mic protocol can't carry;
  (b) `commode=1` state machine desync (radio stuck in EXTERNAL CABLE
  MODE until `COM CHECK END` / mode-exit sequence completes);
  (c) transaction layer: `anytone_transaction` has a reader thread +
  64-byte fixed response size (`ANYTONE_RESPSZ 64`) — verify against a
  real capture.

Plan for the debugging session:
1. Read `/tmp`-cached copies (re-fetch): raw URLs
   https://raw.githubusercontent.com/Hamlib/Hamlib/master/rigs/anytone/anytone.c
   (+ anytone.h, d578.c). Map every command byte sequence emitted.
2. Capture the radio side: usbmon via Wireshark on the Digirig's
   CDC-ACM port (root-free options: `sudo modprobe usbmon` needs sudo —
   owner has none; alternative: an ESP32/FTDI logic sniffer between
   Digirig and radio, or `/dev/ttyUSB0` tee with a spare adapter).
3. Reproduce failure with `rigctl -m 37001 -r ... -s 115200
   [-C commode=1] -v4` and diff sent-vs-expected from the backend code
   and the firmware's ADATA strings.
4. Deliverable: either a working rigctld invocation recipe (mode +
   settings + radio-side menu state) or a precise patch/bug report for
   Hamlib. Owner confirms one Hamlib D578 contributor passed away in
   2026 (unverified by us); regardless, W9MDB's 2026-06-29 backend ver
   suggests the module is still live — upstream issues are viable.
