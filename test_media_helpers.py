"""Bounded media inspection and subprocess regressions without private media."""
import contextlib
import io
import subprocess
import sys
import unittest
from unittest import mock

import download_dolby as media


class MediaHelperTests(unittest.TestCase):
    def test_large_tool_output_is_rejected_instead_of_truncated(self):
        with mock.patch.object(media, "_TOOL_OUTPUT_LIMIT", 32), self.assertRaisesRegex(media.MediaToolError, "内存上限"):
            media.run_media_tool([sys.executable, "-c", "print('x' * 33)"])

    def scan(self, data):
        @contextlib.contextmanager
        def output(command):
            yield io.BytesIO(data)
        with mock.patch.object(media, "_media_pipe", output):
            return media.scan_hevc_rpu("fixture.mp4")

    def test_crc_mpeg2_reference_check_value_and_corruption(self):
        # Independent published CRC-32/MPEG-2 check vector (not generated here).
        checked = b"123456789" + bytes.fromhex("0376e6e7")
        self.assertTrue(media._rpu_crc_valid(checked))
        self.assertFalse(media._rpu_crc_valid(checked[:-1] + b"\0"))

    def test_container_label_cannot_supply_missing_rpu(self):
        result = self.scan(b"\0\0\0\1\x02\x01\x80")
        self.assertFalse(result["verified"])
        self.assertEqual(result["rpu_nal_count"], 0)

    def test_actual_rpu_nal_integrity_and_explicit_semantic_limit(self):
        # Synthetic header/CRC fixture; intentionally not a mastering fixture.
        rpu = bytes.fromhex("7c0119080000003eee457180")
        data = b"\0\0\0\1\x02\x01\x80\0\0\1" + rpu
        result = self.scan(data)
        self.assertTrue(result["verified"])
        self.assertEqual(result["rpu_nal_count"], 1)
        self.assertTrue(result["rpu_crc_checked"])
        self.assertFalse(result["rpu_semantic_decoding_performed"])

    def test_type_62_with_bad_prefix_truncation_or_crc_never_verifies(self):
        valid = bytearray.fromhex("7c0119080000003eee457180")
        bad_crc = bytes(valid[:-2] + b"\0\x80")
        bad_prefix = bytes(valid[:2] + b"\x18" + valid[3:])
        for candidate in (bytes(valid[:5]), bad_crc, bad_prefix):
            with self.subTest(candidate=candidate.hex()):
                result = self.scan(b"\0\0\1\x02\x01\x80\0\0\1" + candidate)
                self.assertFalse(result["verified"])
                self.assertEqual(result["invalid_rpu_nal_count"], 1)

    def test_start_codes_split_at_every_byte_boundary(self):
        data = b"\0\0\0\1\x02\x01\x80\0\0\1\x46\x01\x80"
        class Fragmented(io.BytesIO):
            def read(self, count):
                return super().read(1)
        self.assertEqual(list(media._annexb_nals(Fragmented(data))),
                         [b"\x02\x01\x80", b"\x46\x01\x80"])

    def test_oversized_nal_is_rejected_without_loading_whole_media(self):
        with mock.patch.object(media, "_MAX_NAL", 32), self.assertRaises(ValueError):
            list(media._annexb_nals(io.BytesIO(b"\0\0\0\1" + bytes(33))))

    def test_run_failure_preserves_redacted_stderr_and_exit_code(self):
        error = subprocess.CalledProcessError(7, ["ffmpeg"], stderr=(
            "Bad input https://upos.test.bilivideo.com/a?upsig=private_signature\n"
            "Cookie: SESSDATA=private_cookie\nAuthorization: Bearer private_token"))
        with mock.patch.object(media.subprocess, "run", side_effect=error):
            with self.assertRaises(media.MediaToolError) as caught:
                media.run_media_tool(["ffmpeg"])
        message = str(caught.exception)
        self.assertIn("退出码 7", message)
        self.assertIn("Bad input", message)
        self.assertNotIn("private_", message)

    def test_pipe_failure_captures_stderr_without_pipe_deadlock(self):
        command = [sys.executable, "-c", "import sys;sys.stderr.write('parse failed');sys.exit(9)"]
        with self.assertRaises(media.MediaToolError) as caught:
            with media._media_pipe(command) as output:
                output.read()
        self.assertIn("parse failed", str(caught.exception))
        self.assertIn("退出码 9", str(caught.exception))

    def test_hung_streaming_tool_is_killed_by_deadline(self):
        command = [sys.executable, "-c", "import time;time.sleep(30)"]
        with self.assertRaisesRegex(media.MediaToolError, "超过"):
            with media._media_pipe(command, timeout=0.1) as output:
                output.read()


if __name__ == "__main__":
    unittest.main()
