"""Meaningful container/parser regressions; run: python3 -m unittest -v test_dolby_container."""

from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from dolby_container import ContainerError, inspect_container, parse_dec3


def box(kind, payload=b"", large=False):
    kind = kind.encode("ascii")
    if large:
        return struct.pack(">I4sQ", 1, kind, len(payload) + 16) + payload
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def bits(*fields):
    binary = "".join(format(value, f"0{width}b") for value, width in fields)
    if len(binary) % 8:
        raise AssertionError("Test syntax must be byte aligned")
    return int(binary, 2).to_bytes(len(binary) // 8, "big")


def video_entry(kind="hvc1", amve=None):
    prefix = bytearray(78)
    prefix[24:28] = struct.pack(">HH", 3840, 2160)
    dv = bytes.fromhex("0100104d40") + bytes(19)
    colr = b"nclx" + struct.pack(">HHHB", 9, 18, 9, 0)
    return box(kind, prefix + box("dvvC", dv) + box("colr", colr) +
               (box("amve", amve) if amve is not None else b""))


def audio_entry(dec3=bytes.fromhex("2000200f000110"), sample_rate=48000):
    prefix = bytearray(28)
    prefix[16:20] = struct.pack(">HH", 2, 16)  # EC3 channel count is intentionally non-authoritative.
    prefix[24:28] = struct.pack(">I", sample_rate << 16)
    return box("ec-3", prefix + box("dec3", dec3))


def track(entry, handler, timescale, track_id):
    tkhd = bytes(12) + struct.pack(">I", track_id)
    mdhd = bytes(12) + struct.pack(">II", timescale, timescale * 10)
    hdlr = bytes(8) + handler.encode("ascii")
    stsd = bytes(4) + struct.pack(">I", 1) + entry
    return box("trak", box("tkhd", tkhd) + box("mdia", box("mdhd", mdhd) +
               box("hdlr", hdlr) + box("minf", box("stbl", box("stsd", stsd)))))


def movie(dec3=bytes.fromhex("2000200f000110"), kind="hvc1", sample_rate=48000, amve=None):
    return box("moov", track(video_entry(kind, amve), "vide", 16000, 1) +
               track(audio_entry(dec3, sample_rate), "soun", 48000, 2))


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / "test.mp4"

    def inspect(self, data):
        self.file.write_bytes(data)
        return inspect_container(self.file)

    def test_dual_dolby_metadata_with_hlg_and_ignored_ec3_channel_count(self):
        result = self.inspect(movie() + box("mdat", b"media not inspected"))
        self.assertTrue(result["dolby_vision_signaled"])
        self.assertTrue(result["atmos_signaled"])
        self.assertTrue(all(result["apple_container_checks"].values()))
        video = result["tracks"][0]["sample_entries"][0]
        self.assertEqual(video["dolby_vision"]["dv_profile"], 8)
        self.assertEqual(video["dolby_vision"]["dv_bl_signal_compatibility_id"], 4)
        self.assertEqual(video["colr"]["transfer_characteristics"], 18)
        audio = result["tracks"][1]["sample_entries"][0]
        self.assertEqual(audio["sample_entry_channel_count"], 2)
        self.assertEqual(audio["dec3"]["substreams"][0]["base_channels"], 6)
        self.assertEqual(result["payload_verification"], "not_performed")
        self.assertEqual(result["playback_verification"], "not_performed")

    def test_missing_extension_is_eac3_not_atmos(self):
        result = self.inspect(movie(bytes.fromhex("2000200f00")) + box("mdat"))
        self.assertFalse(result["atmos_signaled"])
        self.assertFalse(result["apple_container_checks"]["ec3_joc_signaling_valid"])
        dec3 = result["tracks"][1]["sample_entries"][0]["dec3"]
        self.assertFalse(dec3["extension_present"])
        self.assertIsNone(dec3["complexity_index_type_a"])

    def test_disabled_extension_does_not_claim_atmos(self):
        result = parse_dec3(bytes.fromhex("2000200f000010"))
        self.assertTrue(result["extension_present"])
        self.assertFalse(result["atmos_signaled"])

    def test_dependent_and_multiple_independent_substreams_have_correct_extension_position(self):
        # One dependent substream means 9 chan_loc bits, otherwise one reserved bit.
        base = bits((640, 13), (1, 3),
                    (0, 2), (16, 5), (0, 1), (0, 1), (0, 3), (7, 3), (1, 1),
                    (0, 3), (1, 4), (0x100, 9),
                    (1, 2), (16, 5), (0, 1), (0, 1), (0, 3), (2, 3), (0, 1),
                    (0, 3), (0, 4), (0, 1),
                    (0, 7), (1, 1), (12, 8))
        result = parse_dec3(base)
        self.assertEqual(result["independent_substream_count"], 2)
        self.assertEqual(result["substreams"][0]["chan_loc"], 0x100)
        self.assertEqual(result["substreams"][1]["sample_rate"], 44100)
        self.assertEqual(result["complexity_index_type_a"], 12)
        self.assertTrue(result["atmos_signaling_valid"])

    def test_truncated_dec3_cannot_claim_atmos(self):
        for raw in (b"", bytes.fromhex("2000200f"), bytes.fromhex("2000200f0001")):
            with self.subTest(raw=raw.hex()), self.assertRaises(ContainerError):
                parse_dec3(raw)

    def test_invalid_complexity_preserves_signal_but_fails_validation(self):
        result = self.inspect(movie(bytes.fromhex("2000200f000100")) + box("mdat"))
        self.assertTrue(result["atmos_signaled"])
        self.assertFalse(result["apple_container_checks"]["ec3_joc_signaling_valid"])
        self.assertTrue(result["warnings"])

    def test_hev1_and_wrong_audio_timescale_report_concrete_warnings(self):
        result = self.inspect(movie(kind="hev1", sample_rate=44100) + box("mdat"))
        self.assertFalse(result["apple_container_checks"]["preferred_dv_hevc_sample_entry"])
        self.assertFalse(result["apple_container_checks"]["ec3_sample_rate_matches_media_timescale"])
        self.assertTrue(any("recommended hvc1/dvh1" in item for item in result["warnings"]))
        self.assertTrue(any("media timescale" in item for item in result["warnings"]))

    def test_amve_is_not_a_fullbox_and_preserves_exact_source_values(self):
        # This is a fabricated test fixture only, never applied to actual media.
        payload = struct.pack(">IHH", 2718281, 15635, 16450)
        result = self.inspect(movie(amve=payload) + box("mdat"))
        ambient = result["tracks"][0]["sample_entries"][0]["ambient_viewing_environment"]
        self.assertTrue(ambient["present"])
        self.assertEqual(ambient["payload_hex"], payload.hex())
        self.assertEqual(ambient["ambient_illuminance_raw"], 2718281)
        self.assertEqual(ambient["ambient_illuminance_lux"], 271.8281)
        self.assertEqual(ambient["ambient_light_x_normalized"], 0.3127)
        self.assertEqual(ambient["ambient_light_y_normalized"], 0.329)
        self.assertFalse(any("no amve" in item for item in result["warnings"]))

    def test_missing_amve_warns_without_rejecting_preserved_dolby_metadata(self):
        result = self.inspect(movie() + box("mdat"))
        ambient = result["tracks"][0]["sample_entries"][0]["ambient_viewing_environment"]
        self.assertEqual(ambient, {"present": False})
        self.assertTrue(all(result["apple_container_checks"].values()))
        self.assertTrue(any("source lacked it" in item for item in result["warnings"]))

    def test_invalid_or_truncated_amve_payload_is_rejected(self):
        for payload in (b"", bytes(7), bytes(12)):
            with self.subTest(length=len(payload)), self.assertRaisesRegex(ContainerError, "amve"):
                self.inspect(movie(amve=payload) + box("mdat"))

    def test_truncated_and_invalid_large_box_headers_are_rejected(self):
        invalid = [b"1234567", struct.pack(">I4s", 4, b"moov"),
                   struct.pack(">I4s", 32, b"moov"),
                   struct.pack(">I4s", 1, b"moov"),
                   struct.pack(">I4sQ", 1, b"moov", 8),
                   struct.pack(">I4s", 8, b"uuid")]
        for data in invalid:
            with self.subTest(raw=data.hex()), self.assertRaises(ContainerError):
                self.inspect(data)

    def test_large_sparse_mdat_is_skipped_and_trailing_moov_is_found(self):
        payload_length = 64 * 1024 * 1024
        with self.file.open("wb") as file:
            file.write(struct.pack(">I4sQ", 1, b"mdat", 16 + payload_length))
            file.seek(16 + payload_length)
            file.write(movie())
        # Enforce no media reads, even tiny ones; memory boundedness is behavioral.
        original_open = Path.open

        class GuardedFile:
            def __init__(self, file):
                self.file = file

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.file.close()

            def seek(self, *args):
                return self.file.seek(*args)

            def read(self, count):
                offset = self.file.tell()
                if 16 <= offset < 16 + payload_length:
                    raise AssertionError("Inspector attempted to read media payload")
                return self.file.read(count)

        def guarded_open(path, *args, **kwargs):
            return GuardedFile(original_open(path, *args, **kwargs))

        with patch.object(Path, "open", guarded_open):
            result = inspect_container(self.file)
        self.assertFalse(result["faststart"])
        self.assertTrue(result["atmos_signaled"])
        self.assertEqual(result["top_level_boxes"][0]["header_size"], 16)

    def test_final_size_zero_mdat_and_uuid_box(self):
        data = box("uuid", bytes(16)) + movie() + struct.pack(">I4s", 0, b"mdat") + bytes(9)
        result = self.inspect(data)
        self.assertTrue(result["faststart"])
        self.assertEqual(result["top_level_boxes"][0]["header_size"], 24)
        self.assertEqual(result["top_level_boxes"][-1]["size"], 17)

    def test_stsd_count_mismatch_is_rejected(self):
        good = movie()
        position = good.index(b"stsd") + 8
        bad = good[:position] + struct.pack(">I", 2) + good[position + 4:]
        with self.assertRaisesRegex(ContainerError, "entry_count"):
            self.inspect(bad)


class ExistingDownloadedFileTests(unittest.TestCase):
    workspace = Path(__file__).resolve().parent
    final = workspace / "红豆_BV1RUfgBiEiY_4K_杜比视界_杜比全景声.mp4"
    audio = workspace / "红豆_BV1RUfgBiEiY_4K_杜比视界_杜比全景声.fatmos.m4a"

    @unittest.skipUnless(final.exists(), "Downloaded reference MP4 is not present")
    def test_reference_final_retains_apple_dolby_signaling(self):
        result = inspect_container(self.final)
        self.assertTrue(all(result["apple_container_checks"].values()))
        video = result["tracks"][0]["sample_entries"][0]
        audio = result["tracks"][1]["sample_entries"][0]
        self.assertEqual((video["width"], video["height"]), (3840, 2160))
        self.assertEqual(video["dolby_vision"]["dv_profile"], 8)
        self.assertEqual(video["dolby_vision"]["dv_bl_signal_compatibility_id"], 4)
        self.assertEqual(video["hvcC"]["bit_depth_luma"], 10)
        self.assertEqual(audio["dec3"]["complexity_index_type_a"], 16)
        self.assertEqual(video["ambient_viewing_environment"], {"present": False})
        self.assertTrue(any("Profile 8.4 sample entry has no amve" in item for item in result["warnings"]))

    @unittest.skipUnless(audio.exists(), "Downloaded reference M4A is not present")
    def test_reference_fragmented_audio_has_atmos_extension(self):
        result = inspect_container(self.audio)
        self.assertFalse(result["dolby_vision_signaled"])
        self.assertTrue(result["atmos_signaled"])
        self.assertEqual(result["tracks"][0]["sample_entries"][0]["dec3"]["payload_hex"],
                         "2000200f000110")
        self.assertTrue(any(box["type"] == "moof" for box in result["top_level_boxes"]))


if __name__ == "__main__":
    unittest.main()
