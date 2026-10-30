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

"""Experimental read-only driver for the Anytone AT-D578UV family.

Covers the AT-D578UV / D578UV PRO / D578UV III (PLUS) mobile DMR radios.
The driver was written from the GPL reference implementation in qdmr
(https://github.com/hmatuschek/qdmr), corroborated by dmrconfig and the
reald/anytone-flash-tools reverse-engineering notes; see
docs/D578UV_PROTOCOL.md for the full evidence trail.

IMPORTANT: writes are intentionally disabled until the protocol has been
validated against a physical radio (see docs/DRIVER_STATUS.md).

The downloaded image is a *packed* channel array, not a dump of radio
addresses:

    [0, CHANNEL_COUNT * CHANNEL_SIZE)  one 0xD0-byte channel element per
                                       memory, in memory-number order
    [CHANNEL_COUNT * CHANNEL_SIZE, +0x200)  the 4000-bit channel bitmap
"""

import logging

from chirp import chirp_common
from chirp import directory
from chirp import errors
from chirp import memmap
from chirp.settings import RadioSetting
from chirp.settings import RadioSettingGroup
from chirp.settings import RadioSettingValueBoolean
from chirp.settings import RadioSettingValueInteger
from chirp.settings import RadioSettingValueList

LOG = logging.getLogger(__name__)

# Radio identity ------------------------------------------------------------
MODEL = "AT-D578UV"
BAUD_RATE = 115200
HARDWARE_FLOW = False
XON_XOFF = False

# Known firmware revisions reported in the ident reply. The ident does not
# distinguish the PRO / III / III PLUS hardware variants, so a warning (not
# an error) is logged for unknown revisions.
KNOWN_FIRMWARE_VERSIONS = ("V110", "V111")

# Memory-map geometry (qdmr: d578uv_codeplug.hh, d878uv_codeplug.hh,
# d868uv_codeplug.hh -- D578UV inherits the D868UV channel layout).
CHANNEL_COUNT = 4000
CHANNELS_PER_BANK = 128
CHANNEL_SIZE = 0xD0
CHANNEL_BANKS_BASE = 0x00800000
BANK_STRIDE = 0x00040000
CHANNEL_EXTENSION_OFFSET = 0x00002000
CHANNEL_BITMAP_ADDR = 0x024C1500
CHANNEL_BITMAP_SIZE = 0x0200

# Packed image layout (see module docstring).
CHANNELS_OFFSET = 0
BITMAP_OFFSET = CHANNEL_COUNT * CHANNEL_SIZE
MEM_SIZE = BITMAP_OFFSET + CHANNEL_BITMAP_SIZE

# Transport (qdmr anytone_interface.cc, dmrconfig dmr_serial.c,
# reald/anytone-flash-tools at-d878uv_protocol.md). The CPS reads in
# 16-byte chunks; we do the same.
READ_CHUNK = 16
CMD_PROGRAM = b"PROGRAM"
RESP_PROGRAM = b"QX\x06"
CMD_IDENTIFY = b"\x02"
CMD_END = b"END"

MODE_ANALOG = 0x00
MODE_DIGITAL = 0x01
MODE_MIXED_ANALOG = 0x02
MODE_MIXED_DIGITAL = 0x03

MODE_NAMES = {
    MODE_ANALOG: "FM",
    MODE_DIGITAL: "DMR",
    MODE_MIXED_ANALOG: "FM",
    MODE_MIXED_DIGITAL: "DMR",
}

POWER_LEVELS = ("Low", "Middle", "High", "Max")

REPEATER_SIMPLEX = 0
REPEATER_POSITIVE = 1
REPEATER_NEGATIVE = 2

SIGNALING_NONE = 0
SIGNALING_CTCSS = 1
SIGNALING_DCS = 2

ADMIT_ALWAYS = 0
ADMIT_FREE = 1
ADMIT_DIFFERENT_COLOR_CODE = 2
ADMIT_SAME_COLOR_CODE = 3

ADMIT_OPTIONS = (
    "Always",
    "Channel free",
    "Different color code",
    "Same color code",
)

# qdmr AnytoneCodeplug::CTCSS::_codeTable: 51 fixed tones, index 51 selects
# the custom CTCSS frequency (0.1 Hz units) stored in the channel element.
CTCSS_CODES = (
    62.5, 67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5,
    91.5, 94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0,
    127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5,
    167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6,
    199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3,
    254.1,
)
CTCSS_CUSTOM_INDEX = 51


def _decode_bcd8_be(raw):
    """Decode 8 BCD digits, most-significant pair first, to an integer."""
    value = 0
    for byte in raw:
        high, low = byte >> 4, byte & 0x0F
        if high > 9 or low > 9:
            raise errors.InvalidDataError(
                "Invalid BCD digit in 0x%02x" % byte)
        value = value * 100 + high * 10 + low
    return value


def _encode_bcd8_be(value):
    """Encode an integer as 8 BCD digits, most-significant pair first."""
    raw = bytearray(4)
    for index in (3, 2, 1, 0):
        raw[index] = ((value % 100) // 10) << 4 | (value % 10)
        value //= 100
    return bytes(raw)


def _decode_ctcss(index, custom_tenth_hz):
    """Decode a CTCSS table index to a frequency in Hz, or None."""
    if index < len(CTCSS_CODES):
        return CTCSS_CODES[index]
    if index == CTCSS_CUSTOM_INDEX:
        return custom_tenth_hz / 10.0
    return None


def _decode_dcs(raw):
    """Decode a stored DCS word to (chirp_code, inverted) or None.

    The radio stores the raw 9-bit binary codeword; bit 9 flags the
    inverted (rev) polarity. CHIRP identifies DCS codes by their octal
    representation written as a decimal number (codeword 0o023 -> 23).
    """
    code = int.from_bytes(raw, "little")
    if code == 0 or code > 0x3FF:
        return None
    inverted = bool(code & 0x200)
    return int(format(code & 0x1FF, "o")), inverted


def _encode_dcs(code, inverted):
    return (int(str(code), 8) | (0x200 if inverted else 0)).to_bytes(
        2, "little")


def _dcs_polarity(rx_inverted, tx_inverted):
    return ("R" if tx_inverted else "N") + ("R" if rx_inverted else "N")


def _radio_checksum(command):
    """Additive checksum over command[1:] (addr + length + data)."""
    return sum(command[1:]) & 0xFF


def _extra(name, label, value, doc):
    setting = RadioSetting(name, label, value)
    setting.set_doc(doc)
    return setting


@directory.register
class AnytoneD578UV(chirp_common.CloneModeRadio,
                    chirp_common.ExperimentalRadio):
    """Anytone AT-D578UV experimental read-only driver.

    Covers the AT-D578UV / D578UV PRO / D578UV III (PLUS) mobile DMR
    radios. The ident reply does not distinguish the hardware variants.
    Writes are disabled until physical validation.
    """

    VENDOR = "Anytone"
    MODEL = MODEL
    BAUD_RATE = BAUD_RATE
    HARDWARE_FLOW = HARDWARE_FLOW
    XON_XOFF = XON_XOFF
    _memsize = MEM_SIZE

    # Channel-element field offsets (0xD0-byte element).
    _OFFSET_RX_FREQUENCY = 0x00
    _OFFSET_TX_OFFSET = 0x04
    _OFFSET_FLAGS = 0x08
    _OFFSET_SIGNALING = 0x09
    _OFFSET_TX_CTCSS = 0x0A
    _OFFSET_RX_CTCSS = 0x0B
    _OFFSET_TX_DCS = 0x0C
    _OFFSET_RX_DCS = 0x0E
    _OFFSET_CUSTOM_CTCSS = 0x10
    _OFFSET_CONTACT_INDEX = 0x14
    _OFFSET_RADIO_ID_INDEX = 0x18
    _OFFSET_ADMIT = 0x1A
    _OFFSET_SCAN_LIST_INDEX = 0x1B
    _OFFSET_GROUP_LIST_INDEX = 0x1C
    _OFFSET_COLOR_CODE = 0x20
    _OFFSET_TIME_SLOT = 0x21
    _OFFSET_NAME = 0x23
    _NAME_LENGTH = 16

    def __init__(self, pipe):
        super().__init__(pipe)
        self._radio_bands = None
        self._radio_version = None

    # -- Transport ----------------------------------------------------------

    def _read_exact(self, size, timeout=2.0):
        data = bytearray()
        remaining = max(int(timeout * 10), 1)
        while len(data) < size and remaining > 0:
            chunk = self.pipe.read(size - len(data))
            if chunk:
                data.extend(chunk)
            else:
                remaining -= 1
        if len(data) < size:
            raise errors.RadioNoResponse(
                "Radio stopped responding (got %i of %i bytes)"
                % (len(data), size))
        return bytes(data)

    def _handshake_once(self):
        """Enter program mode and read the ident reply."""
        self.pipe.resetInputBuffer()
        self.pipe.write(CMD_PROGRAM)
        ack = self._read_exact(len(RESP_PROGRAM))
        if ack != RESP_PROGRAM:
            raise errors.RadioError(
                "Radio did not acknowledge program mode (got %r)" % ack)
        self.pipe.write(CMD_IDENTIFY)
        ident = self._read_exact(16)
        if ident[0] != ord("I") or ident[15] != 0x06:
            raise errors.RadioError("Malformed ident reply %r" % ident)
        model = ident[1:8].split(b"\x00")[0].decode("ascii", "replace")
        version = ident[9:15].split(b"\x00")[0].decode("ascii", "replace")
        if not model.startswith("D578UV"):
            raise errors.RadioError(
                "Expected an AT-D578UV family radio, ident reported %r"
                % model)
        if not version.startswith("V1"):
            LOG.warning(
                "Radio reports unknown firmware revision %r; known "
                "revisions are %s. The III / III PLUS variants may need "
                "further protocol work.", version,
                ", ".join(KNOWN_FIRMWARE_VERSIONS))
        elif version not in KNOWN_FIRMWARE_VERSIONS:
            LOG.warning(
                "Radio reports firmware revision %r, which is newer than "
                "the validated revisions %s", version,
                ", ".join(KNOWN_FIRMWARE_VERSIONS))
        self._radio_bands = ident[8]
        self._radio_version = version
        LOG.debug("Identified D578UV bands=0x%02x version=%s",
                  self._radio_bands, version)

    def _read_region(self, address, size):
        """Read `size` bytes at `address` in 16-byte chunks."""
        data = bytearray()
        while len(data) < size:
            chunk = min(READ_CHUNK, size - len(data))
            addr = address + len(data)
            command = b"R" + addr.to_bytes(4, "big") + bytes([chunk])
            self.pipe.write(command)
            reply = self._read_exact(6 + READ_CHUNK + 2)
            if reply[0] != ord("W"):
                raise errors.RadioError(
                    "Expected read reply 'W', got %r" % reply[0:1])
            if reply[1:5] != command[1:5]:
                raise errors.RadioError(
                    "Read reply for address 0x%08X came back as 0x%08X"
                    % (addr, int.from_bytes(reply[1:5], "big")))
            if reply[5] != chunk:
                raise errors.RadioError(
                    "Read reply for address 0x%08X returned %i bytes, "
                    "expected %i" % (addr, reply[5], chunk))
            payload = reply[6:6 + chunk]
            if _radio_checksum(reply[:6 + chunk]) != reply[6 + chunk]:
                raise errors.RadioError(
                    "Checksum mismatch on read of address 0x%08X" % addr)
            if reply[7 + chunk] != 0x06:
                raise errors.RadioError(
                    "Missing response terminator after read of "
                    "address 0x%08X" % addr)
            data.extend(payload)
        return bytes(data)

    def _send_end(self):
        self.pipe.write(CMD_END)
        ack = self._read_exact(1)
        if ack != b"\x06":
            raise errors.RadioError(
                "Radio did not acknowledge end of session (got %r)" % ack)

    # -- CloneModeRadio -----------------------------------------------------

    def sync_in(self):
        self._handshake_once()
        try:
            bitmap = self._read_region(
                CHANNEL_BITMAP_ADDR, CHANNEL_BITMAP_SIZE)
            image = bytearray(b"\xff" * MEM_SIZE)
            image[BITMAP_OFFSET:MEM_SIZE] = bitmap
            enabled = [
                index for index in range(CHANNEL_COUNT)
                if bitmap[index // 8] & (1 << (index % 8))
            ]
            total = len(enabled) * (CHANNEL_SIZE // READ_CHUNK) + 1
            done = 0
            for index in enabled:
                bank, slot = divmod(index, CHANNELS_PER_BANK)
                address = (CHANNEL_BANKS_BASE + bank * BANK_STRIDE
                           + slot * CHANNEL_SIZE)
                channel = self._read_region(address, CHANNEL_SIZE)
                offset = CHANNELS_OFFSET + index * CHANNEL_SIZE
                image[offset:offset + CHANNEL_SIZE] = channel
                done += CHANNEL_SIZE // READ_CHUNK
                if self.status_fn:
                    status = chirp_common.Status()
                    status.cur = done
                    status.max = total
                    status.msg = "Reading channel memory"
                    self.status_fn(status)
            self._mmap = memmap.MemoryMapBytes(bytes(image))
        finally:
            self._send_end()
        self.process_mmap()

    def sync_out(self):
        raise errors.RadioError(
            "The AT-D578UV driver is read-only until the protocol has "
            "been validated against a physical radio.")

    def process_mmap(self):
        if self._mmap is None:
            self._mmap = memmap.MemoryMapBytes(b"\xff" * MEM_SIZE)

    def get_features(self):
        rf = chirp_common.RadioFeatures()
        rf.memory_bounds = (1, CHANNEL_COUNT)
        rf.has_bank = False
        rf.has_settings = False
        rf.has_tuning_step = False
        rf.has_rx_dtcs = True
        rf.has_cross = True
        rf.has_ctone = True
        rf.has_comment = False
        rf.valid_modes = ["FM", "NFM", "DMR"]
        rf.valid_power_levels = list(POWER_LEVELS)
        rf.valid_bands = [(136000000, 174000000), (400000000, 480000000)]
        rf.valid_tuning_steps = [5.0, 6.25, 10.0, 12.5, 20.0, 25.0, 50.0]
        rf.valid_skips = []
        rf.valid_characters = chirp_common.CHARSET_ASCII
        rf.valid_name_length = self._NAME_LENGTH
        return rf

    def _raw_channel(self, number):
        offset = CHANNELS_OFFSET + (number - 1) * CHANNEL_SIZE
        return self._mmap.get(offset, CHANNEL_SIZE)

    def get_raw_memory(self, number):
        return repr(self._raw_channel(number))

    def _channel_empty(self, number):
        raw = self._raw_channel(number)
        if raw == b"\xff" * CHANNEL_SIZE:
            return True
        bitmap = self._mmap.get(BITMAP_OFFSET, CHANNEL_BITMAP_SIZE)
        index = number - 1
        return not bitmap[index // 8] & (1 << (index % 8))

    def _decode_tone(self, raw, rx):
        signaling = raw[self._OFFSET_SIGNALING]
        mode = (signaling >> (0 if rx else 2)) & 0x03
        if mode == SIGNALING_NONE:
            return None
        if mode == SIGNALING_CTCSS:
            index = raw[self._OFFSET_RX_CTCSS if rx else self._OFFSET_TX_CTCSS]
            custom = int.from_bytes(
                raw[self._OFFSET_CUSTOM_CTCSS:
                    self._OFFSET_CUSTOM_CTCSS + 2], "little")
            frequency = _decode_ctcss(index, custom)
            if frequency is None:
                return None
            return ("TSQL" if rx else "Tone"), frequency
        if mode == SIGNALING_DCS:
            raw_dcs = raw[self._OFFSET_RX_DCS if rx else self._OFFSET_TX_DCS:
                          (self._OFFSET_RX_DCS if rx else self._OFFSET_TX_DCS)
                          + 2]
            decoded = _decode_dcs(raw_dcs)
            if decoded is None:
                return None
            code, inverted = decoded
            return ("DTCS" if rx else "DTCS->"), code, inverted
        return None

    def get_memory(self, number):
        if not 1 <= number <= CHANNEL_COUNT:
            raise errors.InvalidMemoryLocation(
                "Memory %i is outside 1-%i" % (number, CHANNEL_COUNT))
        mem = chirp_common.Memory()
        mem.number = number
        mem.extd_number = ""
        if self._channel_empty(number):
            mem.empty = True
            return mem

        raw = bytearray(self._raw_channel(number))
        rx_hz = _decode_bcd8_be(
            raw[self._OFFSET_RX_FREQUENCY:
                self._OFFSET_RX_FREQUENCY + 4]) * 10
        offset_hz = _decode_bcd8_be(
            raw[self._OFFSET_TX_OFFSET:
                self._OFFSET_TX_OFFSET + 4]) * 10
        flags = raw[self._OFFSET_FLAGS]
        signaling = raw[self._OFFSET_SIGNALING]

        mem.freq = rx_hz
        mem.mode = MODE_NAMES[flags & 0x03]
        if (flags & 0x03) in (MODE_MIXED_ANALOG, MODE_MIXED_DIGITAL):
            LOG.debug("Memory %i is a mixed-mode channel", number)
        mem.power = POWER_LEVELS[(flags >> 2) & 0x03]
        if flags & 0x10:  # bandwidth: 1 = wide
            mem.mode = "FM" if mem.mode == "FM" else mem.mode
        else:
            mem.mode = "NFM" if mem.mode == "FM" else mem.mode

        repeater = (flags >> 6) & 0x03
        if signaling & 0x40:  # rxOnly
            mem.duplex = "off"
            mem.offset = 0
        elif repeater == REPEATER_POSITIVE:
            mem.duplex = "+"
            mem.offset = offset_hz
        elif repeater == REPEATER_NEGATIVE:
            mem.duplex = "-"
            mem.offset = offset_hz
        else:
            mem.duplex = ""
            mem.offset = 0

        rx_tone = self._decode_tone(raw, rx=True)
        tx_tone = self._decode_tone(raw, rx=False)
        if tx_tone and not rx_tone:
            if tx_tone[0] == "DTCS->":
                mem.tmode = "DTCS"
                mem.dtcs = tx_tone[1]
                mem.dtcs_polarity = _dcs_polarity(False, tx_tone[2])
            else:
                mem.tmode = "Tone"
                mem.rtone = tx_tone[1]
        elif tx_tone and rx_tone:
            tx_is_dcs = tx_tone[0] == "DTCS->"
            rx_is_dcs = rx_tone[0] == "DTCS"
            if tx_is_dcs and rx_is_dcs:
                mem.tmode = "DTCS"
                mem.dtcs = tx_tone[1]
                mem.dtcs_polarity = _dcs_polarity(rx_tone[2], tx_tone[2])
            elif tx_is_dcs or rx_is_dcs:
                mem.tmode = "Cross"
                mem.cross_mode = ("DTCS->Tone" if tx_is_dcs
                                  else "Tone->DTCS")
            else:
                mem.tmode = "TSQL"
            if not tx_is_dcs:
                mem.rtone = tx_tone[1]
            if rx_is_dcs:
                mem.dtcs = rx_tone[1]
                mem.dtcs_polarity = _dcs_polarity(
                    rx_tone[2], tx_tone[2] if tx_is_dcs else False)
            else:
                mem.ctone = rx_tone[1]
        elif rx_tone:
            # An rx-only squelch has no CHIRP equivalent; approximate it
            # with a cross tone on the receive side only.
            mem.tmode = "Cross"
            mem.cross_mode = ("DTCS->DTCS" if rx_tone[0] == "DTCS"
                              else "Tone->Tone")
            if rx_tone[0] == "DTCS":
                mem.dtcs = rx_tone[1]
            else:
                mem.ctone = rx_tone[1]

        mem.name = raw[self._OFFSET_NAME:
                       self._OFFSET_NAME + self._NAME_LENGTH].split(
                           b"\x00")[0].decode("ascii", "replace")

        # DMR fields (digital and mixed channels)
        admit = raw[self._OFFSET_ADMIT] & 0x03
        mem.extra = RadioSettingGroup("Extra", "extra")
        extras = (
            _extra(
                "talkaround", "Talkaround",
                RadioSettingValueBoolean(bool(signaling & 0x80)),
                "Transmit on the rx frequency when the repeater cannot "
                "be reached (bypasses the offset).",
            ),
            _extra(
                "admit", "Admission criterion",
                RadioSettingValueList(
                    ADMIT_OPTIONS, current_index=admit),
                "When the radio is allowed to transmit: always, only "
                "when the channel is free, or based on the DMR color "
                "code.",
            ),
            _extra(
                "contact", "Digital contact index",
                RadioSettingValueInteger(
                    0, 0xFFFF,
                    int.from_bytes(
                        raw[self._OFFSET_CONTACT_INDEX:
                            self._OFFSET_CONTACT_INDEX + 4], "little")),
                "Digital contact list entry used on digital channels "
                "(0 = none).",
            ),
            _extra(
                "radio_id", "Radio ID index",
                RadioSettingValueInteger(
                    0, 0xFF, raw[self._OFFSET_RADIO_ID_INDEX]),
                "Radio ID table entry transmitted on digital channels.",
            ),
            _extra(
                "color_code", "DMR color code",
                RadioSettingValueInteger(
                    0, 15, raw[self._OFFSET_COLOR_CODE]),
                "DMR color code (0-15) for digital channels.",
            ),
            _extra(
                "time_slot", "DMR time slot",
                RadioSettingValueList(
                    ("1", "2"),
                    current_index=raw[self._OFFSET_TIME_SLOT] & 0x01),
                "DMR time slot (1 or 2) for digital channels.",
            ),
        )
        for setting in extras:
            mem.extra.append(setting)
        return mem

    def set_memory(self, mem):
        raise errors.RadioError(
            "The AT-D578UV driver is read-only until the protocol has "
            "been validated against a physical radio.")

    def set_raw_memory(self, mem, number=None):
        raise errors.RadioError(
            "The AT-D578UV driver is read-only until the protocol has "
            "been validated against a physical radio.")
