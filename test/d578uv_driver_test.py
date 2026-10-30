"""Offline tests for the experimental Anytone AT-D578UV driver."""

from collections import deque
from pathlib import Path
import importlib.util
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "build_support/chirp-20261023"
PYTHON_VENDOR = (
    ROOT / "assets/python/pyserial-3.5-py2.py3-none-any.whl"
)

# Mirrors drivers/newchirp_d578uv.py -- keep in sync.
CHANNEL_COUNT = 4000
CHANNELS_PER_BANK = 128
CHANNEL_SIZE = 0xD0
CHANNEL_BANKS_BASE = 0x00800000
BANK_STRIDE = 0x00040000
CHANNEL_BITMAP_ADDR = 0x024C1500
CHANNEL_BITMAP_SIZE = 0x0200
READ_CHUNK = 16
BITMAP_OFFSET = CHANNEL_COUNT * CHANNEL_SIZE
MEM_SIZE = BITMAP_OFFSET + CHANNEL_BITMAP_SIZE


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "newchirp_chirp_bridge_d578uv",
        ROOT / "scripts/chirp_bridge.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load chirp_bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bridge
    spec.loader.exec_module(bridge)
    return bridge


def _bcd8_be(value):
    raw = bytearray(4)
    for index in (3, 2, 1, 0):
        raw[index] = ((value % 100) // 10) << 4 | (value % 10)
        value //= 100
    return bytes(raw)


def _frequency_bytes(hz):
    """Encode a frequency in 10 Hz units as 8 BCD digits."""
    return _bcd8_be(hz // 10)


def _channel_record(
    rx_hz,
    *,
    offset_hz=0,
    name=b"",
    mode=0,
    power=0,
    wide=True,
    repeater=0,
    rx_signaling=0,
    tx_signaling=0,
    tx_ctcss=0,
    rx_ctcss=0,
    tx_dcs=0,
    rx_dcs=0,
    custom_ctcss=0,
    contact_index=0,
    radio_id_index=0,
    admit=0,
    color_code=0,
    time_slot=0,
    # time_slot is the raw bit value: 0 = TS1, 1 = TS2.
    talkaround=False,
    rx_only=False,
):
    """Build a 0xD0-byte channel element (D578UV layout)."""
    record = bytearray(b"\xff" * CHANNEL_SIZE)
    record[0:4] = _frequency_bytes(rx_hz)
    record[4:8] = _frequency_bytes(offset_hz)
    flags = (
        (mode & 0x03)
        | ((power & 0x03) << 2)
        | (0x10 if wide else 0)
        | ((repeater & 0x03) << 6)
    )
    record[8] = flags
    signaling = (
        (rx_signaling & 0x03)
        | ((tx_signaling & 0x03) << 2)
        | (0x40 if rx_only else 0)
        | (0x80 if talkaround else 0)
    )
    record[9] = signaling
    record[0x0A] = tx_ctcss
    record[0x0B] = rx_ctcss
    record[0x0C:0x0E] = tx_dcs.to_bytes(2, "little")
    record[0x0E:0x10] = rx_dcs.to_bytes(2, "little")
    record[0x10:0x12] = custom_ctcss.to_bytes(2, "little")
    record[0x14:0x18] = contact_index.to_bytes(4, "little")
    record[0x18] = radio_id_index
    record[0x1A] = admit
    record[0x1B] = 0xFF
    record[0x1C] = 0xFF
    record[0x20] = color_code
    record[0x21] = time_slot & 0x01
    record[0x23:0x23 + len(name.ljust(16, b"\x00")[:16])] = name.ljust(
        16, b"\x00")[:16]
    return bytes(record)


def _channel_address(number):
    index = number - 1
    bank, slot = divmod(index, CHANNELS_PER_BANK)
    return CHANNEL_BANKS_BASE + bank * BANK_STRIDE + slot * CHANNEL_SIZE


class FakeD578UVSerial:
    """Simulates the radio side of the Anytone serial protocol."""

    def __init__(self, channels=None, *, model=b"D578UV\x00",
                 version=b"V111\x00\x00", bands=0x00,
                 ident_reply=None, corrupt_checksum=False):
        self.channels = channels or {}
        self.model = model
        self.version = version
        self.bands = bands
        self._ident_reply = ident_reply
        self._corrupt_checksum = corrupt_checksum
        self.responses = deque()
        self.writes = []
        self.in_program = False
        self.ended = False
        # Bitmap with the requested channels marked valid.
        self.bitmap = bytearray(CHANNEL_BITMAP_SIZE)
        for number in self.channels:
            index = number - 1
            self.bitmap[index // 8] |= 1 << (index % 8)

    def resetInputBuffer(self):
        self.responses.clear()

    def _region_data(self, address, size):
        regions = [(CHANNEL_BITMAP_ADDR, bytes(self.bitmap))]
        for number, record in self.channels.items():
            regions.append((_channel_address(number), record))
        data = b""
        for start, region in regions:
            if start <= address < start + len(region):
                data = region[address - start:address - start + size]
                break
        return data.ljust(size, b"\xff")[:size]

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if data == b"PROGRAM":
            self.in_program = True
            self.responses.append(b"QX\x06")
        elif data == b"\x02":
            if self._ident_reply is not None:
                self.responses.append(self._ident_reply)
            else:
                self.responses.append(
                    b"I" + self.model + bytes([self.bands])
                    + self.version + b"\x06")
        elif data[:1] == b"R" and len(data) == 6:
            address = int.from_bytes(data[1:5], "big")
            size = data[5]
            payload = bytearray(self._region_data(address, size))
            body = b"W" + data[1:5] + bytes([size]) + bytes(payload)
            checksum = sum(body[1:]) & 0xFF
            if self._corrupt_checksum:
                checksum ^= 0xFF
            self.responses.append(body + bytes([checksum]) + b"\x06")
        elif data == b"END":
            self.ended = True
            self.responses.append(b"\x06")

    def read(self, size):
        result = bytearray()
        while self.responses and len(result) < size:
            chunk = self.responses[0]
            take = min(size - len(result), len(chunk))
            result.extend(chunk[:take])
            if take == len(chunk):
                self.responses.popleft()
            else:
                self.responses[0] = chunk[take:]
        return bytes(result)


class D578UVDriverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bridge = _load_bridge()
        cls.directory = bridge.load_chirp(
            str(SOURCE),
            str(PYTHON_VENDOR),
            str(ROOT / "drivers"),
        )
        cls.radio_class = cls.directory.get_radio("Anytone_AT-D578UV")

    def _download(self, serial):
        radio = self.radio_class(serial)
        radio.status_fn = lambda _status: None
        radio.sync_in()
        return radio

    def test_catalog_and_read_only_policy(self):
        from chirp import errors
        from chirp import memmap

        self.assertEqual(
            self.directory.get_radio("Anytone_AT-D578UV"),
            self.radio_class,
        )
        self.assertEqual(self.radio_class.BAUD_RATE, 115200)
        self.assertFalse(self.radio_class.HARDWARE_FLOW)
        self.assertFalse(self.radio_class.XON_XOFF)

        radio = self.radio_class(
            memmap.MemoryMapBytes(b"\xff" * MEM_SIZE))
        with self.assertRaises(errors.RadioError):
            radio.sync_out()
        with self.assertRaises(errors.RadioError):
            radio.set_memory(radio.get_memory(1))

    def test_handshake_and_ident(self):
        serial = FakeD578UVSerial()
        radio = self.radio_class(serial)
        radio._handshake_once()
        self.assertIn(b"PROGRAM", serial.writes)
        self.assertIn(b"\x02", serial.writes)
        self.assertEqual(radio._radio_version, "V111")
        self.assertEqual(radio._radio_bands, 0x00)

    def test_rejects_a_different_model(self):
        from chirp import errors

        serial = FakeD578UVSerial(ident_reply=b"I" + b"BT-9001\x00"
                                  + b"\x00" + b"V111\x00\x00" + b"\x06")
        radio = self.radio_class(serial)
        with self.assertRaises(errors.RadioError):
            radio._handshake_once()
        self.assertNotIn(b"END", serial.writes)
        self.assertFalse(
            [write for write in serial.writes if write[:1] == b"R"])

    def test_sync_in_reads_only_the_bitmap_and_valid_channels(self):
        serial = FakeD578UVSerial({
            1: _channel_record(146_520_000, name=b"SIMPLEX"),
            129: _channel_record(446_006_250, name=b"BANK2"),
        })
        radio = self._download(serial)

        reads = [write for write in serial.writes if write[:1] == b"R"]
        # Bitmap (0x200 / 16) plus 13 chunk-reads per valid channel.
        self.assertEqual(
            len(reads),
            CHANNEL_BITMAP_SIZE // READ_CHUNK + 2 * (CHANNEL_SIZE
                                                     // READ_CHUNK))
        self.assertEqual(
            reads[0],
            b"R" + CHANNEL_BITMAP_ADDR.to_bytes(4, "big") + b"\x10")
        addresses = [int.from_bytes(read[1:5], "big") for read in reads]
        self.assertIn(_channel_address(1), addresses)
        self.assertIn(_channel_address(129), addresses)
        self.assertNotIn(_channel_address(2), addresses)
        self.assertNotIn(_channel_address(128), addresses)
        self.assertEqual(serial.writes[-1], b"END")
        self.assertTrue(serial.ended)

        self.assertEqual(radio._mmap.get(0, 4), _frequency_bytes(146_520_000))
        offset_129 = (129 - 1) * CHANNEL_SIZE
        self.assertEqual(
            radio._mmap.get(offset_129, 4), _frequency_bytes(446_006_250))

    def test_channel_layout_and_tone_decoding(self):
        # CTCSS table index 13 is 100.0 Hz; index 51 selects the custom
        # CTCSS value (0.1 Hz units). DCS 023 = binary codeword 0o23.
        channels = {
            1: _channel_record(
                146_010_000, name=b"FM SIMPLE",
                tx_signaling=1, tx_ctcss=13,
                rx_signaling=1, rx_ctcss=13,
            ),
            2: _channel_record(
                446_006_250, name=b"DMR DIGI", mode=1, power=2,
                repeater=2, color_code=1, time_slot=0, contact_index=42,
                radio_id_index=3, admit=3,
            ),
            3: _channel_record(
                145_010_000, offset_hz=600_000, name=b"CUSTOMCTC",
                wide=False,
                tx_signaling=1, tx_ctcss=51, custom_ctcss=1234,
                rx_signaling=2, rx_dcs=0o23 | 0x200,
                repeater=1, talkaround=True,
            ),
        }
        serial = FakeD578UVSerial(channels)
        radio = self._download(serial)

        first = radio.get_memory(1)
        self.assertEqual(first.freq, 146_010_000)
        self.assertEqual(first.duplex, "")
        self.assertEqual(first.mode, "FM")
        self.assertEqual(first.tmode, "TSQL")
        self.assertEqual(first.rtone, 100.0)
        self.assertEqual(first.ctone, 100.0)
        self.assertEqual(first.power, "Low")
        self.assertEqual(first.name, "FM SIMPLE")
        self.assertFalse(first.empty)

        second = radio.get_memory(2)
        self.assertEqual(second.freq, 446_006_250)
        self.assertEqual(second.duplex, "-")
        self.assertEqual(second.offset, 0)
        self.assertEqual(second.mode, "DMR")
        self.assertEqual(second.power, "High")
        self.assertEqual(second.name, "DMR DIGI")
        extras = {
            setting.get_name(): setting.value.get_value()
            for setting in second.extra
        }
        self.assertEqual(extras["color_code"], 1)
        self.assertEqual(extras["time_slot"], "1")
        self.assertEqual(extras["contact"], 42)
        self.assertEqual(extras["radio_id"], 3)
        self.assertEqual(extras["admit"], "Same color code")
        self.assertFalse(extras["talkaround"])

        third = radio.get_memory(3)
        self.assertEqual(third.mode, "NFM")
        self.assertEqual(third.duplex, "+")
        self.assertEqual(third.offset, 600_000)
        self.assertEqual(third.tmode, "Cross")
        self.assertEqual(third.rtone, 123.4)
        self.assertEqual(third.dtcs, 23)
        self.assertEqual(third.dtcs_polarity, "NR")
        extras = {
            setting.get_name(): setting.value.get_value()
            for setting in third.extra
        }
        self.assertTrue(extras["talkaround"])

        self.assertTrue(radio.get_memory(2).freq > 0)
        self.assertTrue(radio.get_memory(4).empty)
        self.assertTrue(radio.get_memory(CHANNEL_COUNT).empty)

    def test_rx_only_and_checksum_verification(self):
        from chirp import errors

        channels = {
            5: _channel_record(
                146_610_000, name=b"RXONLY", rx_only=True,
                tx_signaling=1, tx_ctcss=4,
            ),
        }
        serial = FakeD578UVSerial(channels)
        radio = self._download(serial)
        mem = radio.get_memory(5)
        self.assertEqual(mem.duplex, "off")
        self.assertEqual(mem.tmode, "Tone")
        self.assertEqual(mem.rtone, 74.4)

        bad = FakeD578UVSerial(channels, corrupt_checksum=True)
        with self.assertRaises(errors.RadioError):
            self._download(bad)

    def test_raw_memory_and_image_layout(self):
        channels = {1: _channel_record(146_010_000, name=b"X")}
        serial = FakeD578UVSerial(channels)
        radio = self._download(serial)
        raw = radio.get_raw_memory(1)
        self.assertIn("\\x14`\\x10", raw)
        self.assertTrue(radio.get_raw_memory(9).startswith("b'"))
        # The image is a packed channel array plus the trailing bitmap.
        self.assertEqual(radio._memsize, MEM_SIZE)


if __name__ == "__main__":
    unittest.main()
