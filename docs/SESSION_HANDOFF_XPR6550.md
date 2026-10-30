# SESSION HANDOFF — XPR 6550 MotoTRBO driver (updated 2026-10-30, phase 1 done)

Branch: main (origin/main). Repo: /home/gordon/newchirp.
Status: phase-1 codeplug-file decode SHIPPED, offline-tested, uncommitted.

## What is done this round (verified, not yet committed)

- AES key/IV extracted from `cpservices.dll` (fixed the .NET #US heap
  parser: streams count lives at `md + 16 + version_len`, not `md + 6`).
  Key `RmFAUjxmbWpLNmkqUUVWfUhoQ1Usd1BOP0ZpLVg7USQ` /
  IV `czp8KVtQfVU/bj9BbVBnIw` decode the user's codeplug.
- `drivers/newchirp_xpr6550.py` — read-only FileBackedRadio for MATRIX
  `.ctb` files: envelope decode, pure-Python AES-256-CBC (+ optional
  cryptography fast path), gzip payload, tolerant XML parse (`&#xFFFF;`
  replaced, never re-serialized), 53-channel decode, zones as CHIRP
  subdevices, writes disabled, `save_mmap` byte-identical.
- `test/xpr6550_driver_test.py` — 11 tests, all green: FIPS-197 AES-256
  vector, envelope round-trip, synthetic codeplug channel/zone asserts,
  corrupt-payload error, refused writes, real-file smoke test (skips if
  `~/Download/2024-October.ctb` is absent).
- `lib/main.dart` — `ctb` added to the "CHIRP and MOTOTRBO files" picker;
  `.ctb` routes to the CHIRP-image import path (`readImage`).
- E2E verified: `scripts/chirp_bridge.py read-image --image
  ~/Download/2024-October.ctb` returns model `Motorola_XPR6550`, 16
  Scanner channels, 7 named zones, correct tones/powers/rx-only.
- Docs: `docs/XPR6550_CODEPLUG_FORMAT.md` (new) and a full
  `docs/DRIVER_STATUS.md` entry with evidence hashes.

## Decode facts worth keeping (also in the format doc + skill)

- Zone membership: ALL member records count, including `NTLLISHTYPE`
  NULL ones (Scanner = exactly LEQMAX 16; NOAA 7/7; MURS 5/5; Marine
  9/13/16/19/22; members sum to 53 = channel count). Open question:
  `Simplex 1` holds two channels that look stale.
- Motorola DPL codes: printed octal-as-decimal; CHIRP `dtcs` = the same
  digits as a decimal int; `CP_*DPLINV=1` -> polarity `R`.
- RX-only channels: `CP_RXONLYEN=1`, and TX fields keep values with
  `Applicable="Disabled"` — cross-check that attribute when in doubt.
- AES exp-table gotcha: `exp[255]` must wrap to 1 or the S-box breaks
  for byte 0x01 (caught by the FIPS-197 test).

## Still open (next session)

- DMR personality mapping is provisional (`ANLGCONV` only in evidence).
  Need a codeplug with DMRCONV records or a physical radio read.
- Phase 2 (radio clone): needs a Wireshark capture of a CPS read session
  on the MOTOTRBO network adapter (TCP 8002). Ship read-only first.
- Commit this round: driver + test + docs + lib/main.dart picker. Then
  refresh the OKF bundle (lib/ changed) and run `flutter analyze`.

## Evidence in ~/Download (verified, do not sweep again)

- 2024-October.ctb — 88,127 bytes, 2024-10-04, SHA-256
  1bb18161de026cbf43ef6d6f9c35edcbf5d3027b7509b4e4aa75e42440fe1c90.
- cpservices.dll — 3,112,960 bytes, 2018-11-21, SHA-256
  a2554dcdc5eca4fc0b3ec73135d7a49e9c0d6924f9d70fccf0192aac6d4cd09d.
- resources/mototrbocps — PKCS#12 signing bundle, SHA-256
  10f3dc373c193ba4a8ba5898ea4376f9c231dddc4f2af3316250c8e79b2ba43d.
- D578UV_Setup_1.21.exe — unrelated installer (leave out of commits).

## Leftover from previous session (D578UV, do not lose)

- Uncommitted: docs/DRIVER_STATUS.md modified, docs/D578UV_PROTOCOL.md,
  docs/SESSION_HANDOFF_D578UV.md, drivers/newchirp_d578uv.py,
  test/d578uv_driver_test.py.
- Per AGENTS.md: hand this off separately; D578UV is done, MotoTRBO is
  a separate milestone. Keep the two handoffs apart in any commit.

## Scratch (not committed; pruned after 24h idle)

- ~/.hermes/cache/scratch/moto-research/: extract_cps_keys.py (fixed),
  ctbio.py, dotnet_us_debug.py, codeplug_payload.xml (decoded payload,
  sha256 3f92377ee4f89d3e9a3108fa02dfe99b1b1898013d21060412bbae6b11b1c56b),
  george-hopkins/codeplug + codeplug-prepare + xcmp-xnl-dissector clones.
