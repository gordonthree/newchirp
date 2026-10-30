# Copyright 2026 NewChirp contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Experimental read-only driver for Motorola MOTOTRBO XPR 6550 codeplugs.

This driver does not talk to a radio. It opens classic MOTOTRBO CPS
codeplug files (*.ctb, MATRIX archive type, CPS R13-R16 era) and decodes
them into CHIRP memories so a saved codeplug can be inspected and its
channels imported into the Channel Library. Radio-level clone access for
Gen-1 TRBO radios runs over XNL/XCMP on the USB network adapter and is a
separate, still-unimplemented milestone.

Cipher material: the AES-256-CBC key and IV are product constants stored
in the CPS cpservices.dll assembly (stable across CPS R13-R16). They are
the same values published in the open-source codeplug-prepare forks.
See docs/XPR6550_CODEPLUG_FORMAT.md for the full evidence trail.

IMPORTANT: writes are intentionally disabled until the decode has been
validated against a physical radio read (see docs/DRIVER_STATUS.md).
The channel decode is proven against one real CPS export; DMR-channel
mapping is provisional because the evidence codeplug holds only analog
conventional personalities.
"""

import base64
import logging
import re
import xml.etree.ElementTree as ET
import zlib

from chirp import chirp_common
from chirp import directory
from chirp import errors
from chirp.settings import RadioSetting
from chirp.settings import RadioSettingGroup
from chirp.settings import RadioSettingValueBoolean
from chirp.settings import RadioSettingValueInteger

LOG = logging.getLogger(__name__)

MODEL = "XPR6550"

# CPS product constants from cpservices.dll static constructors (see
# docs/XPR6550_CODEPLUG_FORMAT.md). The 43-character string is the base64
# AES-256 key (one "=" appended), the 22-character string the base64 IV
# ("==" appended).
AES_KEY_B64 = "RmFAUjxmbWpLNmkqUUVWfUhoQ1Usd1BOP0ZpLVg7USQ"
AES_IV_B64 = "czp8KVtQfVU/bj9BbVBnIw"

# XPR 6550 UHF portable: 1 W low / 4 W high. Mid is not evidenced by the
# sample codeplug but exists on the platform; treat it as provisional.
POWER_LEVELS = [
    chirp_common.PowerLevel("Low", watts=1),
    chirp_common.PowerLevel("Mid", watts=2),
    chirp_common.PowerLevel("High", watts=4),
]

# The evidence codeplug holds VHF traffic (151-170 MHz) although its
# RS_MODELNUM reads H55JDH9LA1AN, so accept both TRBO land-mobile bands.
VALID_BANDS = [(136000000, 174000000), (400000000, 527000000)]

POWER_MAP = {
    "LOWPWR": "Low",
    "MEDPWR": "Mid",
    "HIGHPWR": "High",
}

BANDWIDTH_MAP = {
    "STR_12PT5KHZ": "NFM",
    "STR_25KHZ": "FM",
}

# Zone membership: CPS exports the member records in list order. Records
# whose NTLLISHTYPE is "NULL" still reference real channels, and the
# evidence codeplug's zones only make sense when every record counts
# (NOAA 7/7, MURS 5/5, Marine = 9/13/16/19/22, Scanner = exactly the
# LEQMAX maximum of 16). See docs/XPR6550_CODEPLUG_FORMAT.md.


# --------------------------------------------------------------------------
# Pure-Python AES-256-CBC (decrypt only). NewChirp bundles no crypto wheel
# for its Python runtime, so this is the portable path; the cryptography
# package is used opportunistically when the interpreter has it.
# --------------------------------------------------------------------------

_HAVE_CRYPTOGRAPHY = False
_CIPHER = _ALGORITHMS = _MODES = None
try:
    from cryptography.hazmat.primitives.ciphers import (  # type: ignore
        Cipher as _CIPHER,
        algorithms as _ALGORITHMS,
        modes as _MODES,
    )
    _HAVE_CRYPTOGRAPHY = True
except Exception:  # pragma: no cover - depends on interpreter
    pass


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gmul(a, b):
    product = 0
    while b:
        if b & 1:
            product ^= a
        a = _xtime(a)
        b >>= 1
    return product & 0xFF


def _build_aes_tables():
    exp = [0] * 256
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x = _xtime(x) ^ x  # multiply by the generator 3
    exp[255] = exp[0]  # the multiplicative group wraps around (x^255 = x^0)
    sbox = [0] * 256
    for a in range(256):
        inv = 0 if a == 0 else exp[255 - log[a]]
        s = inv
        for shift in (1, 2, 3, 4):
            s ^= ((inv << shift) | (inv >> (8 - shift))) & 0xFF
        sbox[a] = s ^ 0x63
    inv_sbox = [0] * 256
    for a, s in enumerate(sbox):
        inv_sbox[s] = a
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_aes_tables()

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40)


def _expand_key(key32):
    words = [list(key32[4 * i:4 * i + 4]) for i in range(8)]
    for i in range(8, 60):
        t = list(words[i - 1])
        if i % 8 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 8 - 1]
        elif i % 8 == 4:
            t = [_SBOX[b] for b in t]
        words.append([a ^ b for a, b in zip(words[i - 8], t)])
    return words


def _sub_bytes(state, table):
    for i in range(16):
        state[i] = table[state[i]]


def _add_round_key(state, words, rnd):
    for col in range(4):
        w = words[4 * rnd + col]
        for row in range(4):
            state[4 * col + row] ^= w[row]


def _inv_shift_rows(state):
    for row in range(1, 4):
        old = [state[4 * col + row] for col in range(4)]
        for col in range(4):
            state[4 * col + row] = old[(col - row) % 4]


def _inv_mix_columns(state):
    for col in range(4):
        a0, a1, a2, a3 = state[4 * col:4 * col + 4]
        state[4 * col + 0] = (
            _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        )
        state[4 * col + 1] = (
            _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        )
        state[4 * col + 2] = (
            _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        )
        state[4 * col + 3] = (
            _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
        )


def _decrypt_block(words, block):
    state = list(block)
    _add_round_key(state, words, 14)
    for rnd in range(13, 0, -1):
        _inv_shift_rows(state)
        _sub_bytes(state, _INV_SBOX)
        _add_round_key(state, words, rnd)
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _sub_bytes(state, _INV_SBOX)
    _add_round_key(state, words, 0)
    return state


def _aes_cbc_decrypt(key, iv, data):
    """Decrypt AES-256-CBC. Uses cryptography when available."""
    if _HAVE_CRYPTOGRAPHY:
        decryptor = _CIPHER(  # type: ignore[misc]
            _ALGORITHMS.AES(key), _MODES.CBC(iv)  # type: ignore[union-attr]
        ).decryptor()
        return decryptor.update(data) + decryptor.finalize()
    if len(data) % 16:
        raise ValueError("AES-CBC data is not 16-byte aligned")
    words = _expand_key(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        plain = _decrypt_block(words, block)
        out.extend(p ^ c for p, c in zip(plain, previous))
        previous = block
    return bytes(out)


# --------------------------------------------------------------------------
# .ctb MATRIX envelope
# --------------------------------------------------------------------------

_ENTITY_RE = re.compile(r"&(#[xX][0-9A-Fa-f]+|#[0-9]+|amp|lt|gt|quot|apos);")
_RADIO_RE = re.compile(r'<RADIO VERSION="1">(.*?)</RADIO>', re.S)

_PREDEFINED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}

# cp1252 bytes whose Windows character is printable are embedded as that
# Unicode character; slots that have no mapping (0x81 0x8D 0x8F 0x90 0x9D)
# are written as numeric entities instead.
_CP1252_REV = {}
for _b in range(0x80, 0x100):
    try:
        _ch = bytes([_b]).decode("cp1252")
    except Exception:  # pragma: no cover - every cp1252 byte decodes
        _ch = ""
    if _ch:
        _CP1252_REV[_ch] = _b


def _unescape_entity(match):
    token = match.group(1)
    if token.startswith("#"):
        if token[1] in "xX":
            return chr(int(token[2:], 16))
        return chr(int(token[1:]))
    return _PREDEFINED[token]


def ctb_ciphertext(raw):
    """Recover raw AES-CBC ciphertext bytes from a MATRIX .ctb archive."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise errors.RadioError(
            "The codeplug file is not a UTF-8 MATRIX archive: %s" % exc
        )
    match = _RADIO_RE.search(text)
    if not match:
        raise errors.RadioError(
            'No <RADIO VERSION="1"> payload found; this does not look like '
            "a classic MOTOTRBO CPS MATRIX codeplug."
        )
    decoded = _ENTITY_RE.sub(_unescape_entity, match.group(1))
    out = bytearray()
    for ch in decoded:
        if ch in _CP1252_REV:
            out.append(_CP1252_REV[ch])
        elif ord(ch) <= 0xFF:
            out.append(ord(ch))
        else:
            raise errors.RadioError(
                "Unexpected character %r in codeplug ciphertext" % ch
            )
    return bytes(out)


def decode_payload(raw_ctb):
    """Decrypt a MATRIX .ctb and return the raw payload bytes.

    The payload is gzip-compressed and decodes to a UTF-8 BOM followed by
    the codeplug XML followed by an RSA <SIGNATURE> block. The bytes are
    returned verbatim: the XML can contain invalid entities such as
    &#xFFFF; and must never be re-serialized by this driver.
    """
    ciphertext = ctb_ciphertext(raw_ctb)
    if not ciphertext or len(ciphertext) % 16:
        raise errors.RadioError(
            "Codeplug ciphertext is %d bytes, expected a non-zero "
            "AES block-aligned length" % len(ciphertext)
        )
    key = base64.b64decode(AES_KEY_B64 + "=")
    iv = base64.b64decode(AES_IV_B64 + "==")
    try:
        compressed = _aes_cbc_decrypt(key, iv, ciphertext)
        stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
        payload = stream.decompress(compressed) + stream.flush()
    except Exception as exc:
        raise errors.RadioError(
            "Codeplug decryption failed: %s" % exc
        )
    if payload[:3] != b"\xef\xbb\xbf":
        raise errors.RadioError(
            "Decrypted codeplug payload does not start with a UTF-8 BOM"
        )
    return payload[3:]


def parse_codeplug_xml(payload):
    """Parse the codeplug payload into (channels, zones, radio info).

    The payload XML is parsed tolerantly: the &#xFFFF; entity emitted by
    CPS for reserved fields is not valid XML, so it is replaced by a
    placeholder before parsing. Parsing is read-only; the original bytes
    are never re-serialized.
    """
    xml_bytes = payload.split(b"<SIGNATURE", 1)[0]
    try:
        root = ET.fromstring(xml_bytes.replace(b"&#xFFFF;", b"FFFF"))
    except ET.ParseError as exc:
        raise errors.RadioError(
            "Codeplug payload is not parseable XML: %s" % exc
        )
    channels = []
    for element in root.findall(".//CNV_PER_CMP_TYPE_GRP/CNV_PER_CMP_TYPE"):
        channels.append(_parse_channel(element))
    zones = []
    for element in root.findall(".//ZN_PER_ASGN_DLT_TYPE"):
        zone = _parse_zone(element)
        if zone["members"]:
            zones.append(zone)
    info = {
        "model": _text(root, ".//RS_MODELNUM"),
        "serial": _text(root, ".//RS_SERIALNUM"),
        "subscriber_id": _text(root, ".//RC_SUBSCRIBERID"),
    }
    return channels, zones, info


def _text(element, path):
    found = element.find(path)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def _parse_channel(element):
    def field(name):
        return _text(element, name)

    return {
        "index": element.attrib.get("ListID", "0"),
        "pertype": field("CP_PERSTYPE"),
        "alias": field("CP_CNVPERSALIAS"),
        "rxfreq": field("CP_RXFREQ"),
        "txfreq": field("CP_TXFREQ"),
        "bandwidth": field("CP_CHNLBWDTH"),
        "rx_sq_type": field("CP_XSQCHTY"),
        "rx_tpl": field("CP_RXTPLFREQ"),
        "rx_dpl": field("CP_RXDPLCD"),
        "rx_dpl_inv": field("CP_RXDPLINV"),
        "tx_sq_type": field("CP_TXSQCHTY"),
        "tx_tpl": field("CP_TXTTPLFREQ"),
        "tx_dpl": field("CP_TXTDPLCD"),
        "tx_dpl_inv": field("CP_TXDPLINV"),
        "power": field("CP_TXPWR"),
        "rxonly": field("CP_RXONLYEN"),
        "talkaround": field("CP_TALKAROUNDEN"),
        "tot": field("CP_TOT"),
        "colorcode": field("CP_COLORCODE"),
        "slot": field("CP_SLTASSGMNT"),
    }


def _parse_zone(element):
    header = element.find("ZN_PER_ASGN_DLH_TYPE")
    members = []
    if header is not None:
        for record in element.findall("ZN_PER_ASGN_DLL_TYPE"):
            reference = _text(record, "ZP_PERSITID")
            if not reference.isdigit():
                continue
            members.append(int(reference))
    name = ""
    if header is not None:
        name = _text(header, "ZP_ZONEALIAS")
    return {
        "name": name,
        "members": members,
    }


def _tone_spec(sq_type, tpl, dpl, dpl_inv):
    """Map a CPS squelch triple to a CHIRP split-tone spec tuple."""
    kind = (sq_type or "").strip().upper()
    if kind == "TPL" and tpl:
        try:
            return ("Tone", float(str(tpl).strip()), None)
        except ValueError:
            LOG.warning("Unparseable CTCSS frequency %r", tpl)
            return ("", None, None)
    if kind == "DPL" and dpl:
        # Motorola prints the octal DPL code as decimal digits; CHIRP's
        # dtcs value is the decimal integer with those digits.
        code = str(dpl).strip()
        polarity = "R" if str(dpl_inv).strip() == "1" else "N"
        try:
            return ("DTCS", int(code), polarity)
        except ValueError:
            LOG.warning("Unparseable DPL code %r", dpl)
            return ("", None, None)
    return ("", None, None)


def channel_to_memory(channel, number):
    """Decode one codeplug channel record into a CHIRP Memory."""
    mem = chirp_common.Memory()
    mem.number = number
    mem.empty = False
    try:
        mem.freq = int(round(float(channel["rxfreq"]) * 1000000))
    except (TypeError, ValueError):
        raise errors.RadioError(
            "Channel %s has an unparseable RX frequency %r" % (
                number, channel["rxfreq"])
        )
    try:
        mem.tuning_step = chirp_common.required_step(mem.freq)
    except errors.InvalidDataError:
        mem.tuning_step = 12.5

    rxfreq = mem.freq
    rx_only = str(channel["rxonly"]).strip() == "1"
    try:
        txfreq = int(round(float(channel["txfreq"]) * 1000000))
    except (TypeError, ValueError):
        txfreq = None
    if rx_only or txfreq is None:
        mem.duplex = "off"
        mem.offset = 0
    elif txfreq == rxfreq:
        mem.duplex = ""
        mem.offset = 0
    else:
        mem.offset = txfreq - rxfreq
        mem.duplex = "+" if mem.offset > 0 else "-"

    tx_spec = _tone_spec(
        channel["tx_sq_type"],
        channel["tx_tpl"],
        channel["tx_dpl"],
        channel["tx_dpl_inv"],
    )
    rx_spec = _tone_spec(
        channel["rx_sq_type"],
        channel["rx_tpl"],
        channel["rx_dpl"],
        channel["rx_dpl_inv"],
    )
    chirp_common.split_tone_decode(mem, tx_spec, rx_spec)
    if mem.tmode == "TSQL":
        # split_tone_decode() leaves the TX tone at its default for TSQL;
        # make the round-trip lossless instead.
        mem.rtone = mem.ctone

    bandwidth = channel["bandwidth"]
    mem.mode = BANDWIDTH_MAP.get(bandwidth, "NFM")
    pertype = (channel["pertype"] or "").upper()
    if pertype and "ANLG" not in pertype:
        # Provisional: no DMR-channel evidence codeplug is available yet.
        mem.mode = "DMR"
        LOG.warning(
            "Channel %s personality %r is not analog; DMR decode is "
            "provisional", number, channel["pertype"],
        )

    mem.name = channel["alias"]
    mem.power = POWER_MAP.get((channel["power"] or "").upper()) or None
    if mem.power:
        for level in POWER_LEVELS:
            if str(level) == mem.power:
                mem.power = level
                break

    extra = RadioSettingGroup("extra", "extra")
    try:
        tot = int(str(channel["tot"]).strip() or "0")
    except ValueError:
        tot = 0
    tot_value = RadioSettingValueInteger(0, 86400, tot)
    tot_value.set_mutable(False)
    extra.append(RadioSetting("tot", "TOT (s)", tot_value))
    talkaround = RadioSettingValueBoolean(
        str(channel["talkaround"]).strip() == "1"
    )
    talkaround.set_mutable(False)
    extra.append(RadioSetting("talkaround", "Talkaround", talkaround))
    mem.extra = extra
    return mem


class XPR6550Zone:
    """One codeplug zone exposed as a CHIRP channel subdevice."""

    def __init__(self, parent, name, members):
        self._parent = parent
        self.VARIANT = name
        self._members = members

    def get_features(self):
        rf = chirp_common.RadioFeatures()
        rf.has_sub_devices = False
        rf.memory_bounds = (1, max(1, len(self._members)))
        rf.valid_name_length = 16
        rf.valid_modes = ["FM", "NFM", "DMR"]
        rf.valid_tmodes = list(chirp_common.TONE_MODES)
        rf.valid_cross_modes = list(chirp_common.CROSS_MODES)
        rf.valid_duplexes = ["", "-", "+", "off"]
        rf.valid_tuning_steps = [12.5, 25.0]
        rf.valid_bands = list(VALID_BANDS)
        rf.valid_power_levels = list(POWER_LEVELS)
        rf.valid_tones = list(chirp_common.TONES)
        rf.valid_dtcs_codes = list(chirp_common.DTCS_CODES)
        rf.valid_skips = ["", "S"]
        rf.has_settings = False
        rf.can_delete = False
        rf.requires_call_lists = False
        rf.has_bank = False
        return rf

    def get_memory(self, number):
        if not 1 <= number <= len(self._members):
            mem = chirp_common.Memory()
            mem.number = number
            mem.empty = True
            return mem
        return self._parent.memory_for_channel(self._members[number - 1],
                                               number)

    def get_sub_devices(self):
        return []

    def get_prompts(self):
        return self._parent.get_prompts()


@directory.register
class MotorolaXPR6550(chirp_common.FileBackedRadio,
                      chirp_common.ExperimentalRadio):
    """Motorola MOTOTRBO XPR 6550 experimental read-only codeplug driver.

    Opens classic CPS .ctb (MATRIX) codeplug files; no serial
    communication is implemented. Writes are disabled until the decode
    is validated against a physical radio.
    """

    VENDOR = "Motorola"
    MODEL = MODEL
    FILE_EXTENSION = "ctb"

    def __init__(self, filename=None):
        chirp_common.FileBackedRadio.__init__(self, None)
        self._raw_ctb = b""
        self._channels = []
        self._zones = []
        self._info = {}
        self._zone_devices = []
        if filename:
            self.load_mmap(filename)

    @classmethod
    def match_model(cls, filedata, filename):
        if not filename.lower().endswith("." + cls.FILE_EXTENSION):
            return False
        head = filedata[:512]
        return b"<ARCHIVE" in head and b'TYPE="MATRIX"' in head

    def load_mmap(self, filename):
        with open(filename, "rb") as handle:
            self._raw_ctb = handle.read()
        payload = decode_payload(self._raw_ctb)
        self._channels, self._zones, self._info = parse_codeplug_xml(payload)
        self._zone_devices = [
            XPR6550Zone(self, zone["name"], zone["members"])
            for zone in self._zones
        ]
        LOG.info(
            "Decoded %i channels and %i zones from %s (model %s, serial %s)",
            len(self._channels), len(self._zones), filename,
            self._info.get("model"), self._info.get("serial"),
        )

    def save_mmap(self, filename):
        # Write the untouched original bytes so signatures and opaque
        # entities survive byte-identical round-trips.
        with open(filename, "wb") as handle:
            handle.write(self._raw_ctb)

    def memory_for_channel(self, channel_index, number):
        if not 0 <= channel_index < len(self._channels):
            mem = chirp_common.Memory()
            mem.number = number
            mem.empty = True
            return mem
        return channel_to_memory(self._channels[channel_index], number)

    def get_features(self):
        rf = chirp_common.RadioFeatures()
        rf.has_sub_devices = True
        rf.memory_bounds = (1, max(1, len(self._channels)))
        rf.valid_name_length = 16
        rf.valid_modes = ["FM", "NFM", "DMR"]
        rf.valid_tmodes = list(chirp_common.TONE_MODES)
        rf.valid_cross_modes = list(chirp_common.CROSS_MODES)
        rf.valid_duplexes = ["", "-", "+", "off"]
        rf.valid_tuning_steps = [12.5, 25.0]
        rf.valid_bands = list(VALID_BANDS)
        rf.valid_power_levels = list(POWER_LEVELS)
        rf.valid_tones = list(chirp_common.TONES)
        rf.valid_dtcs_codes = list(chirp_common.DTCS_CODES)
        rf.valid_skips = ["", "S"]
        rf.has_settings = False
        rf.can_delete = False
        rf.requires_call_lists = False
        rf.has_bank = False
        return rf

    def get_sub_devices(self):
        return list(self._zone_devices)

    def get_memory(self, number):
        return self.memory_for_channel(number - 1, number)

    def get_prompts(self):
        prompts = chirp_common.RadioPrompts()
        prompts.experimental = (
            "The Motorola MOTOTRBO XPR 6550 driver opens CPS .ctb "
            "codeplug files for offline inspection. It is experimental "
            "and read-only: it does not communicate with the radio, and "
            "writes stay disabled until the codeplug decode is validated "
            "against a physical radio."
        )
        return prompts

    def set_memory(self, memory):
        raise errors.RadioError(
            "The MOTOTRBO XPR 6550 driver is read-only; writing a "
            "codeplug requires a validated clone protocol."
        )

    def erase_memory(self, number):
        raise errors.RadioError(
            "The MOTOTRBO XPR 6550 driver is read-only; writing a "
            "codeplug requires a validated clone protocol."
        )
