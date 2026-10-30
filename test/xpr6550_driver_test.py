"""Offline tests for the experimental Motorola XPR 6550 codeplug driver."""

from pathlib import Path
import base64
import gzip
import importlib.util
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "build_support/chirp-20261023"
PYTHON_VENDOR = (
    ROOT / "assets/python/pyserial-3.5-py2.py3-none-any.whl"
)

# FIPS-197 appendix C.3 AES-256 known-answer vector.
FIPS_KEY = bytes(range(32))
FIPS_PLAINTEXT = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_CIPHERTEXT = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")


def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "newchirp_chirp_bridge_xpr6550",
        ROOT / "scripts/chirp_bridge.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load chirp_bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bridge
    spec.loader.exec_module(bridge)
    return bridge


def _load_driver_module():
    spec = importlib.util.spec_from_file_location(
        "newchirp_xpr6550_driver_module",
        ROOT / "drivers/newchirp_xpr6550.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load newchirp_xpr6550.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _aes256_encrypt_block(module, words, block):
    """Encrypt one block with the driver's tables (test-side helper)."""
    from chirp import chirp_common  # noqa: F401  (ensures chirp loaded)

    state = list(block)

    def add_round_key(rnd):
        for col in range(4):
            w = words[4 * rnd + col]
            for row in range(4):
                state[4 * col + row] ^= w[row]

    def shift_rows():
        for row in range(1, 4):
            old = [state[4 * col + row] for col in range(4)]
            for col in range(4):
                state[4 * col + row] = old[(col + row) % 4]

    def mix_columns():
        for col in range(4):
            a0, a1, a2, a3 = state[4 * col:4 * col + 4]
            state[4 * col + 0] = (
                module._gmul(a0, 2) ^ module._gmul(a1, 3)
                ^ a2 ^ a3
            )
            state[4 * col + 1] = (
                a0 ^ module._gmul(a1, 2) ^ module._gmul(a2, 3) ^ a3
            )
            state[4 * col + 2] = (
                a0 ^ a1 ^ module._gmul(a2, 2) ^ module._gmul(a3, 3)
            )
            state[4 * col + 3] = (
                module._gmul(a0, 3) ^ a1 ^ a2 ^ module._gmul(a3, 2)
            )

    add_round_key(0)
    for rnd in range(1, 14):
        module._sub_bytes(state, module._SBOX)
        shift_rows()
        mix_columns()
        add_round_key(rnd)
    module._sub_bytes(state, module._SBOX)
    shift_rows()
    add_round_key(14)
    return bytes(state)


def _aes256_cbc_encrypt(module, key, iv, data):
    """Pad to a block boundary and encrypt CBC (test-side helper)."""
    pad = (-len(data)) % 16
    data = data + bytes(pad)
    words = module._expand_key(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = _aes256_encrypt_block(module, words, bytes(
            p ^ c for p, c in zip(data[offset:offset + 16], previous)
        ))
        out.extend(block)
        previous = block
    return bytes(out)


def _matrix_envelope(ciphertext):
    """Encode raw ciphertext bytes as a MATRIX .ctb XML document."""
    parts = []
    for value in ciphertext:
        char = chr(value)
        if value == 0x26:
            parts.append("&amp;")
        elif value == 0x3C:
            parts.append("&lt;")
        elif value == 0x3E:
            parts.append("&gt;")
        elif 0x20 <= value <= 0x7E:
            parts.append(char)
        else:
            unit = bytes([value]).decode("cp1252", errors="ignore")
            if unit:
                parts.append(unit)
            else:
                parts.append("&#%i;" % value)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ARCHIVE TYPE="MATRIX" COPYRIGHT="Test"><RADIO VERSION="1">'
        + "".join(parts)
        + "</RADIO></ARCHIVE>"
    ).encode("utf-8")


def _channel_xml(index, alias, rx, tx, **overrides):
    fields = {
        "CP_PERSTYPE": "ANLGCONV",
        "CP_CNVPERSALIAS": alias,
        "CP_RXFREQ": "%.6f" % (rx / 1e6),
        "CP_TXFREQ": "%.6f" % (tx / 1e6),
        "CP_CHNLBWDTH": "STR_12PT5KHZ",
        "CP_XSQCHTY": "TPL",
        "CP_RXTPLFREQ": "192.8",
        "CP_RXDPLCD": "23",
        "CP_RXDPLINV": "0",
        "CP_TXSQCHTY": "TPL",
        "CP_TXTTPLFREQ": "192.8",
        "CP_TXTDPLCD": "23",
        "CP_TXDPLINV": "0",
        "CP_TXPWR": "HIGHPWR",
        "CP_RXONLYEN": "0",
        "CP_TALKAROUNDEN": "0",
        "CP_TOT": "60",
        "CP_COLORCODE": "1",
        "CP_SLTASSGMNT": "SLOT1",
    }
    fields.update(overrides)
    inner = "".join(
        "<%s>%s</%s>" % (name, value, name)
        for name, value in fields.items()
    )
    return (
        '<CNV_PER_CMP_TYPE Applicable="Enabled" ListID="%i">%s'
        "</CNV_PER_CMP_TYPE>" % (index, inner)
    )


def _zone_xml(index, name, members):
    records = []
    for position, (persid, null_type) in enumerate(members, start=1):
        records.append(
            "<ZN_PER_ASGN_DLL_TYPE>"
            "<ZN_PER_ASGN_DLL_TYPE_NTLLISHTYPE>%s</"
            "ZN_PER_ASGN_DLL_TYPE_NTLLISHTYPE>"
            "<ZN_PER_ASGN_DLL_TYPE_NTLLISHID>%i</"
            "ZN_PER_ASGN_DLL_TYPE_NTLLISHID>"
            "<ZP_LLEQ>1</ZP_LLEQ>"
            "<ZP_PERSIT TypeID=\"CNV_PER_CMP_TYPE\">%i</ZP_PERSIT>"
            "<ZP_PERSITTYPE>CNV_PER_CMP_TYPE</ZP_PERSITTYPE>"
            "<ZP_PERSITID>%i</ZP_PERSITID>"
            "<ZPL_RESERVED>0</ZPL_RESERVED>"
            "</ZN_PER_ASGN_DLL_TYPE>" % (
                "NULL" if null_type else "ZN_PER_ASGN_DLL_TYPE",
                persid if null_type else position,
                persid,
                persid,
            )
        )
    return (
        '<ZN_PER_ASGN_DLT_TYPE Applicable="Enabled" ListID="%i" '
        'SortType="Position">'
        "<ZN_PER_ASGN_DLH_TYPE>"
        "<ZP_LSTCTG>1</ZP_LSTCTG>"
        "<ZP_LEDSZ>6</ZP_LEDSZ>"
        "<ZP_LEQMAX>16</ZP_LEQMAX>"
        "<ZP_LLEQMAX>1</ZP_LLEQMAX>"
        '<ZN_PER_ASGN_DLH_TYPE_FTLLISHTYPE>ZN_PER_ASGN_DLL_TYPE</'
        "ZN_PER_ASGN_DLH_TYPE_FTLLISHTYPE>"
        "<ZN_PER_ASGN_DLH_TYPE_FTLLISHID>%i</"
        "ZN_PER_ASGN_DLH_TYPE_FTLLISHID>"
        "<ZP_ZONEALIAS>%s</ZP_ZONEALIAS>"
        "<ZP_ZONETYPE>1</ZP_ZONETYPE>"
        "<ZP_RESERVED>0</ZP_RESERVED>"
        "</ZN_PER_ASGN_DLH_TYPE>%s</ZN_PER_ASGN_DLT_TYPE>" % (
            index,
            members[0][0] if members else 0,
            name,
            "".join(records),
        )
    )


def _build_payload(channels, zones):
    return (
        "<?xml version=\"1.0\"?>"
        "<LTD_CODEPLUG VERSION=\"05.01.04\">"
        "<SECURITY_PARTITION>"
        "<RS_MODELNUM>H55JDH9LA1AN</RS_MODELNUM>"
        "<RS_SERIALNUM>037TKY6102</RS_SERIALNUM>"
        "</SECURITY_PARTITION>"
        "<APP_PARTITION>"
        '<CNV_PER_CMP_TYPE_GRP SortType="None">'
        + "".join(channels)
        + "</CNV_PER_CMP_TYPE_GRP>"
        '<ZN_PER_ASGN_DLH_TYPE_GRP SortType="None">'
        + "".join(zones)
        + "</ZN_PER_ASGN_DLH_TYPE_GRP>"
        '<RAD_CFG_CMP_TYPE_GRP SortType="None"><RAD_CFG_CMP_TYPE>'
        "<RC_SUBSCRIBERID>1</RC_SUBSCRIBERID>"
        "</RAD_CFG_CMP_TYPE></RAD_CFG_CMP_TYPE_GRP>"
        "</APP_PARTITION>"
        "</LTD_CODEPLUG>"
        '<SIGNATURE VERSION="1.0"><DIGEST>00</DIGEST></SIGNATURE>'
    )


def _build_ctb(module, channels, zones):
    payload = (
        b"\xef\xbb\xbf"
        + _build_payload(channels, zones).encode("utf-8")
    )
    compressed = gzip.compress(payload)
    key = base64.b64decode(module.AES_KEY_B64 + "=")
    iv = base64.b64decode(module.AES_IV_B64 + "==")
    ciphertext = _aes256_cbc_encrypt(module, key, iv, compressed)
    return _matrix_envelope(ciphertext)


def _sample_channels():
    return [
        # TPL both sides, simplex
        _channel_xml(0, "SIMPLEX 1", 146520000, 146520000),
        # receive-only with an offset that must be ignored
        _channel_xml(1, "RX ONLY", 159150000, 154785000,
                     CP_RXONLYEN="1", CP_CHNLBWDTH="STR_25KHZ",
                     CP_TXPWR="LOWPWR", CP_XSQCHTY="CSQ",
                     CP_TXSQCHTY="CSQ", CP_TALKAROUNDEN="1"),
        # inverted RX DPL, carrier TX
        _channel_xml(2, "DPL CH", 446075000, 446075000,
                     CP_XSQCHTY="DPL", CP_RXDPLCD="23", CP_RXDPLINV="1",
                     CP_TXSQCHTY="CSQ"),
        # differing TX/RX CTCSS pairs -> cross mode
        _channel_xml(3, "CROSS", 147435000, 147435000,
                     CP_RXTPLFREQ="250.3", CP_TXSQCHTY="TPL",
                     CP_TXTTPLFREQ="123.0"),
    ]


def _sample_zones():
    return [
        _zone_xml(0, "Zone A", [
            (0, False), (1, False), (2, False),
            # a stale slot: NULL list-type records still count as members
            # in the evidence codeplug (see the protocol document)
            (3, True),
        ]),
        _zone_xml(1, "Zone B", [(3, False)]),
        _zone_xml(2, "Empty Pool", []),
    ]


class XPR6550DriverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bridge = _load_bridge()
        cls.directory = bridge.load_chirp(
            str(SOURCE),
            str(PYTHON_VENDOR),
            str(ROOT / "drivers"),
        )
        cls.radio_class = cls.directory.get_radio("Motorola_XPR6550")
        # load_chirp() imports every driver in drivers/, which is also the
        # module the radio class comes from.
        cls.module = sys.modules["newchirp.drivers.newchirp_xpr6550"]

    def _write_ctb(self, directory, channels=None, zones=None):
        path = directory / "sample.ctb"
        path.write_bytes(
            _build_ctb(
                self.module,
                _sample_channels() if channels is None else channels,
                _sample_zones() if zones is None else zones,
            )
        )
        return path

    def test_aes_known_answer(self):
        """FIPS-197 C.3 vector against the pure-Python decrypt path."""
        module = self.module
        original = module._HAVE_CRYPTOGRAPHY
        try:
            module._HAVE_CRYPTOGRAPHY = False
            plain = module._aes_cbc_decrypt(
                FIPS_KEY,
                bytes(16),
                FIPS_CIPHERTEXT,
            )
        finally:
            module._HAVE_CRYPTOGRAPHY = original
        self.assertEqual(plain, FIPS_PLAINTEXT)
        if original:
            fast = module._aes_cbc_decrypt(
                FIPS_KEY, bytes(16), FIPS_CIPHERTEXT
            )
            self.assertEqual(fast, FIPS_PLAINTEXT)

    def test_envelope_round_trip(self):
        ciphertext = bytes(range(256)) * 3
        recovered = self.module.ctb_ciphertext(
            _matrix_envelope(ciphertext)
        )
        self.assertEqual(recovered, ciphertext)

    def test_match_model(self):
        ctb = _build_ctb(self.module, _sample_channels(), _sample_zones())
        self.assertTrue(
            self.radio_class.match_model(ctb, "codeplug.ctb")
        )
        self.assertFalse(
            self.radio_class.match_model(ctb, "codeplug.img")
        )
        gemstone = (
            b'<ARCHIVE TYPE="GEMSTONE"><RADIO VERSION="1">abc'
            b"</RADIO></ARCHIVE>"
        )
        self.assertFalse(
            self.radio_class.match_model(gemstone, "codeplug.ctb")
        )
        self.assertFalse(
            self.radio_class.match_model(b"\x00" * 64, "codeplug.ctb")
        )

    def test_load_and_decode_channels(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ctb(Path(tmp))
            radio = self.radio_class(str(path))

        self.assertEqual(len(radio._channels), 4)
        self.assertEqual(radio._info["model"], "H55JDH9LA1AN")
        self.assertEqual(radio._info["serial"], "037TKY6102")
        self.assertEqual(radio._info["subscriber_id"], "1")

        simplex = radio.get_memory(1)
        self.assertEqual(simplex.number, 1)
        self.assertEqual(simplex.name, "SIMPLEX 1")
        self.assertEqual(simplex.freq, 146520000)
        self.assertEqual(simplex.duplex, "")
        self.assertEqual(simplex.mode, "NFM")
        self.assertEqual(str(simplex.power), "High")
        self.assertEqual(simplex.tmode, "TSQL")
        self.assertEqual(simplex.rtone, 192.8)
        self.assertEqual(simplex.ctone, 192.8)
        extras = {s.get_name(): s for s in simplex.extra}
        self.assertEqual(extras["tot"].value.get_value(), 60)
        self.assertEqual(extras["talkaround"].value.get_value(), False)
        self.assertFalse(extras["tot"].value.get_mutable())

        rx_only = radio.get_memory(2)
        self.assertEqual(rx_only.name, "RX ONLY")
        self.assertEqual(rx_only.freq, 159150000)
        self.assertEqual(rx_only.duplex, "off")
        self.assertEqual(rx_only.mode, "FM")
        self.assertEqual(str(rx_only.power), "Low")
        self.assertEqual(rx_only.tmode, "")

        dpl = radio.get_memory(3)
        self.assertEqual(dpl.name, "DPL CH")
        # carrier TX, inverted DPL RX -> CHIRP cross mode
        self.assertEqual(dpl.tmode, "Cross")
        self.assertEqual(dpl.cross_mode, "->DTCS")
        self.assertEqual(dpl.rx_dtcs, 23)
        self.assertEqual(dpl.dtcs_polarity, "NR")

        cross = radio.get_memory(4)
        self.assertEqual(cross.tmode, "Cross")
        self.assertEqual(cross.cross_mode, "Tone->Tone")
        self.assertEqual(cross.rtone, 123.0)
        self.assertEqual(cross.ctone, 250.3)

    def test_load_mmap_after_null_constructor(self):
        """Mirrors the bridge's radio_from_image() usage."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ctb(Path(tmp))
            radio = self.radio_class(None)
            radio.load_mmap(str(path))
        self.assertEqual(len(radio._channels), 4)

    def test_zone_subdevices(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ctb(Path(tmp))
            radio = self.radio_class(str(path))

        features = radio.get_features()
        self.assertTrue(features.has_sub_devices)

        zones = radio.get_sub_devices()
        self.assertEqual(
            [zone.VARIANT for zone in zones],
            ["Zone A", "Zone B"],
        )

        zone_a = zones[0]
        self.assertEqual(
            zone_a.get_features().memory_bounds, (1, 4)
        )
        first = zone_a.get_memory(1)
        self.assertEqual(first.name, "SIMPLEX 1")
        self.assertEqual(first.freq, 146520000)
        # the stale NULL-typed record still resolves to a real channel
        last = zone_a.get_memory(4)
        self.assertEqual(last.name, "CROSS")
        self.assertFalse(last.empty)

        zone_b = zones[1]
        self.assertEqual(
            zone_b.get_features().memory_bounds, (1, 1)
        )
        self.assertEqual(zone_b.get_memory(1).name, "CROSS")

        out_of_range = zone_b.get_memory(2)
        self.assertTrue(out_of_range.empty)

        self.assertEqual(zone_b.get_sub_devices(), [])

    def test_save_mmap_round_trip_is_byte_identical(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ctb(Path(tmp))
            original = path.read_bytes()
            radio = self.radio_class(str(path))
            copy = Path(tmp) / "copy.ctb"
            radio.save_mmap(str(copy))
            self.assertEqual(copy.read_bytes(), original)

    def test_writes_are_refused(self):
        from chirp import errors

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ctb(Path(tmp))
            radio = self.radio_class(str(path))
        mem = radio.get_memory(1)
        with self.assertRaises(errors.RadioError):
            radio.set_memory(mem)
        with self.assertRaises(errors.RadioError):
            radio.erase_memory(1)

    def test_prompts_declare_experimental_status(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_ctb(Path(tmp))
            radio = self.radio_class(str(path))
        prompts = radio.get_prompts()
        self.assertIn("experimental", prompts.experimental.lower())
        self.assertIn("read-only", prompts.experimental.lower())

    def test_corrupt_payload_raises_image_error(self):
        from chirp import errors

        import tempfile

        garbage = (
            b'<?xml version="1.0"?>\n'
            b'<ARCHIVE TYPE="MATRIX"><RADIO VERSION="1">'
            + b"Z" * 64
            + b"</RADIO></ARCHIVE>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.ctb"
            path.write_bytes(garbage)
            radio = self.radio_class(None)
            with self.assertRaises(errors.RadioError):
                radio.load_mmap(str(path))

    def test_decode_real_cps_export_when_present(self):
        """Smoke-test against the user's real codeplug when available."""
        real = Path.home() / "Download/2024-October.ctb"
        if not real.is_file():
            self.skipTest("real CPS export not present")
        radio = self.radio_class(str(real))
        self.assertGreater(len(radio._channels), 0)
        zones = radio.get_sub_devices()
        self.assertGreater(len(zones), 0)
        for channel in radio._channels:
            self.assertEqual(channel["pertype"], "ANLGCONV")
        total_members = sum(
            len(zone._members) for zone in zones
        )
        self.assertEqual(total_members, len(radio._channels))
        # Frequencies must parse into a CHIRP-valid memory for one sample.
        sample = zones[0].get_memory(1)
        self.assertFalse(sample.empty)
        self.assertGreater(sample.freq, 0)


if __name__ == "__main__":
    unittest.main()
