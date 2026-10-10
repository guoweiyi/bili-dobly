import importlib.metadata
import subprocess
import unittest
from unittest.mock import patch

import doctor


class DoctorTests(unittest.TestCase):
    def test_missing_required_tools_blocks_readiness(self):
        with patch.object(doctor.shutil, "which", return_value=None), \
                patch.object(doctor.importlib.metadata, "version", side_effect=importlib.metadata.PackageNotFoundError):
            report = doctor.check_environment()
        self.assertFalse(report["ready"])
        failures = {c["name"] for c in report["checks"] if c["status"] == "error"}
        self.assertTrue({"yt-dlp", "ffmpeg", "ffprobe"} <= failures)

    def test_missing_media_capability_is_not_hidden_by_valid_version(self):
        def output(executable, *args):
            if "-version" in args:
                return "ffmpeg version test"
            if "-muxers" in args:
                return " E mp4\n E eac3\n E h264\n"  # HEVC muxer is missing.
            if "-bsfs" in args:
                return "hevc_mp4toannexb\nh264_mp4toannexb\n"
            return " A eac3\n"
        with patch.object(doctor.shutil, "which", return_value="/mock/tool"), \
                patch.object(doctor.importlib.metadata, "version", return_value=doctor.SUPPORTED_YT_DLP), \
                patch.object(doctor, "tool_output", side_effect=output):
            report = doctor.check_environment()
        self.assertFalse(report["ready"])
        self.assertIn("hevc", next(c for c in report["checks"] if c["name"] == "FFmpeg muxers")["detail"])

    def test_tool_timeout_is_reported_without_traceback(self):
        with patch.object(doctor.shutil, "which", return_value="/mock/tool"), \
                patch.object(doctor.importlib.metadata, "version", return_value=doctor.SUPPORTED_YT_DLP), \
                patch.object(doctor, "tool_output", side_effect=subprocess.TimeoutExpired("ffmpeg", 10)):
            report = doctor.check_environment()
        self.assertFalse(report["ready"])


if __name__ == "__main__":
    unittest.main()
