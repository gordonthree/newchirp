# XPR 6550 codeplug (.ctb) format and decode

Evidence: `~/Download/2024-October.ctb` (SHA-256
`1bb18161de026cbf43ef6d6f9c35edcbf5d3027b7509b4e4aa75e42440fe1c90`,
88,127 bytes, saved 2024-10-04) from a real MOTOTRBO CPS R14/R16-era
install, plus `~/Download/cpservices.dll` (SHA-256 recorded in
docs/SESSION_HANDOFF_XPR6550.md) from the same CPS. The offline decode
was validated end-to-end through `scripts/chirp_bridge.py read-image`.

Driver: `drivers/newchirp_xpr6550.py` (read-only). This document covers
the file format only; the radio-level XNL/XCMP clone protocol is a
separate, unimplemented milestone (see docs/SESSION_HANDOFF_XPR6550.md).

## Envelope

A `.ctb` is an XML document:

```xml
<?xml version="1.0"?>
<ARCHIVE TYPE="MATRIX" COPYRIGHT="..."><RADIO VERSION="1">CIPHERTEXT</RADIO></ARCHIVE>
```

Other archive types (GEMSTONE, PHOENIX, MALTA, ...) exist but only
MATRIX is decoded; GEMSTONE stores base64 instead of text. The MATRIX
ciphertext text encoding is nonstandard: each ciphertext byte 0x20-0x7E
is literal (with `&amp;` `&lt;` `&gt;` escaped), each byte whose cp1252
character is printable is written as that Unicode character in UTF-8,
and everything else (C0 controls and the undefined cp1252 slots
0x81/0x8D/0x8F/0x90/0x9D) is written as numeric XML entities.

Warning: `html.unescape` corrupts the reconstruction (HTML5 remaps
numeric references 0x80-0x9F to different cp1252 characters). Only a
strict entity unescape followed by the inverse cp1252 mapping is
correct; the driver implements exactly that.

## Crypto

- AES-256-CBC. The key is the 43-character base64 string `RmFAUjxm`
  `bWpLNmkqUUVWfUhoQ1Usd1BOP0ZpLVg7USQ` (one `=` appended), the IV the
  22-character string `czp8KVtQfVU/bj9BbVBnIw` (two `=` appended).
  Both are product constants stored in `cpservices.dll` static
  constructors and are identical across CPS R13-R16 era installs; the
  open-source codeplug-prepare forks ship the same values. They were
  recovered locally by brute-forcing all `ldstr` literals from the
  user's own `cpservices.dll` (scratch extractor, not committed).
- The plaintext is a gzip stream whose member decodes to a UTF-8 BOM,
  the codeplug XML, and a trailing RSA `<SIGNATURE>` block
  (PKCS#1 v1.5 / SHA-1 over the payload re-encoded UTF-16LE; public key
  embedded). The driver ignores the signature (integrity only; the
  envelope itself is the trust boundary for offline files).
- The XML may be longer than the gzip stream (CPS pads to the AES block
  boundary); the driver decompresses tolerantly.

## Codeplug XML

Root `LTD_CODEPLUG` (version `05.01.04` in the sample) with partitions:

- `SECURITY_PARTITION` — radio identity: `RS_SERIALNUM` (037TKY6102),
  `RS_MODELNUM` (H55JDH9LA1AN), firmware/security versions.
- `APP_PARTITION` — everything else (one flat container per feature
  group; all lookups in the driver use `.//` paths).
- `CS_PARTITION` / `CS_LANGPACK_PARTITION` — customer-software info.

Key groups:

- `CNV_PER_CMP_TYPE_GRP` — one `CNV_PER_CMP_TYPE` per conventional
  channel ("personality"), 53 in the sample. Document order is the
  codeplug channel order (`ListID` attribute, 0-based).
- `ZN_PER_ASGN_DLH_TYPE_GRP` — one `ZN_PER_ASGN_DLT_TYPE` per zone.
  `ZN_PER_ASGN_DLH_TYPE` carries `ZP_ZONEALIAS` (name), `ZP_LEQMAX`
  (max members: 16 for normal zones, 160 for the "Channel Pool"
  pseudo-zone), and the member records `ZN_PER_ASGN_DLL_TYPE`, each
  referencing a channel by `ZP_PERSITID`.
- `DIGITAL_SIG_SYS_CMP_TYPE_GRP` — DMR system parameters (`DSS_ALIAS`
  etc.). Present but unused by the analog-only sample.
- `RAD_CFG_CMP_TYPE_GRP` — radio-wide config including
  `RC_SUBSCRIBERID` (the DMR radio ID) and `RC_RADIOALIAS`.

### Channel fields decoded

| CPS element | Meaning | CHIRP mapping |
|---|---|---|
| `CP_CNVPERSALIAS` | channel name | `mem.name` (16 chars) |
| `CP_RXFREQ` / `CP_TXFREQ` | MHz decimals | `mem.freq` / offset |
| `CP_RXONLYEN` | receive-only flag | `duplex='off'` (TX fields stay populated but their `Applicable` attribute reads `Disabled`) |
| `CP_CHNLBWDTH` | `STR_12PT5KHZ` / `STR_25KHZ` | `mode` NFM / FM |
| `CP_XSQCHTY` + `CP_RXTPLFREQ` | RX CTCSS | rx tone (TPL) |
| `CP_XSQCHTY` + `CP_RXDPLCD` + `CP_RXDPLINV` | RX DPL | rx DTCS; `INV=1` → polarity `R` |
| `CP_TXSQCHTY` + `CP_TXTTPLFREQ` / `CP_TXTDPLCD` / `CP_TXDPLINV` | TX squelch | tx tone |
| `CP_TXPWR` | `LOWPWR`/`MEDPWR`/`HIGHPWR` | power level (Low 1 W / Mid 2 W / High 4 W, UHF XPR 6550 ratings) |
| `CP_TOT` | timeout timer seconds | immutable `tot` extra |
| `CP_TALKAROUNDEN` | talkaround flag | immutable `talkaround` extra |
| `CP_PERSTYPE` | personality type | `ANLGCONV` → analog; anything else → `mode='DMR'` (provisional) |
| `CP_COLORCODE` / `CP_SLTASSGMNT` | DMR color code / timeslot | present in every record (`Applicable=NA` for analog); not yet surfaced |

Tone handling uses `chirp_common.split_tone_decode`; the driver copies
`ctone` into `rtone` for TSQL so a round-trip keeps the TX tone.

### DPL code convention

Motorola prints the octal DPL code as decimal digits (`D023` → `23`).
CHIRP's `dtcs` value is the decimal integer with those digits, so the
driver stores `int(code)` directly. `CP_*DPLINV=1` maps to CHIRP
polarity `R` (the pinned tree uses `R`, not `I`).

## Zone membership semantics (resolved with one open question)

The member records are exported in list order and the sample codeplug
confirms counting every record:

- Scanner zone holds exactly `ZP_LEQMAX` (16) members, a complete
  public-safety set.
- NOAA WX = all 7 weather channels; MURS = all 5 licensed MURS
  frequencies; Marine = the classic 9/13/16/19/22 set.
- Member records across all zones sum to exactly the channel count
  (53), with no duplicates and no orphans.

Records whose `NTLLISHTYPE` is `NULL` therefore still reference real
members (the fields are internal linked-list bookkeeping, not a
validity flag). Open question: the `Simplex 1` zone contains two
channels that look stale (a duplicated repeater channel and a repeater
output under a "simplex" name). If a physical radio shows different
membership, revisit the NULL-record handling in
`_parse_zone`/`XPR6550Zone`.

The empty "Channel Pool" zone (no member records) is skipped; zones are
exposed as CHIRP subdevices named by `VARIANT`, numbered by member
position from 1.

## What is NOT validated yet

- DMR (`DMRCONV`-style) personality values: the evidence codeplug holds
  only `ANLGCONV` records. The provisional mapping (mode `DMR`) needs a
  codeplug with digital channels before it can be trusted.
- Any radio-level behavior: no XNL/XCMP work is covered here.
- Power ratings: Low 1 W / High 4 W are the XPR 6550 UHF portable
  specs; `MEDPWR` is not evidenced in the sample.
