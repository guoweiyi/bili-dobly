"""Batch workflow checks: distrust manifests, preserve payloads, publish atomically."""
import argparse
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import bili_batch as batch
from test_bilibili_api import BV, fixture


def manifest_entry(kind=2, **updates):
    info = fixture(kind=kind)
    for stream in info["dash"]["video"] + info["dash"]["dolby"]["audio"]:
        stream["baseUrl"] = "https://upos.test.bilivideo.com/file.m4s?deadline=4102444800&upsig=private_signature"
        stream.pop("base_url", None)
    entry = {"bvid": BV, "title": "Test", "cid": 12345, "page": 1, "part": "P1", "playinfo": info}
    entry.update(updates)
    return entry


def ffprobe_fixture(raw=False, atmosphere=True, dv=True):
    audio = {"codec_type": "audio", "codec_name": "eac3", "profile": "E-AC-3+Atmos" if atmosphere else "unknown"}
    if raw:
        return {"streams": [audio]}
    video = {"codec_type": "video", "codec_name": "hevc", "side_data_list": (
        [{"side_data_type": "DOVI configuration record", "rpu_present_flag": 1, "dv_profile": 8,
          "dv_bl_signal_compatibility_id": 4}]
        if dv else [])}
    return {"streams": [video, audio], "format": {"filename": "test.mp4"}}


def container_fixture(valid_joc=True, valid_video=True):
    return {"path": "test.mp4", "apple_container_checks": {
        "ec3_joc_signaling_valid": valid_joc,
        "preferred_dv_hevc_sample_entry": valid_video,
    }}


class BatchManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def write_manifest(self, entries):
        path = self.directory / "manifest.json"
        path.write_text(json.dumps({"schema": "bili-dolby-manifest-v1", "entries": entries,
                                   "user_agent": "Safari test UA"}), encoding="utf-8")
        return path

    def args(self, path, bvids=None, first_page=False):
        return argparse.Namespace(manifest=path, bvids=bvids or [], bv_file=None,
                                  first_page=first_page, cookies=None,
                                  cookies_from_browser=None, cookie_env=None)

    def call_main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return batch.main(argv)

    def test_filter_and_reclassify_overrides_exported_claim(self):
        first = manifest_entry(kind=1, status="verified_dolby_vision_atmos",
                               classification={"dual_candidate": True, "status": "verified_dolby_vision_atmos"})
        second = manifest_entry(page=2, cid=222)
        other = manifest_entry(bvid="BV13x41117TL", cid=333)
        path = self.write_manifest([first, second, other])
        entries, ua = batch.load_entries(self.args(path, [BV], first_page=True))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "non_atmos")
        self.assertFalse(entries[0]["classification"]["dual_candidate"])
        self.assertEqual(ua, "Safari test UA")

    def test_missing_requested_bv_is_an_error(self):
        path = self.write_manifest([manifest_entry()])
        with self.assertRaises(ValueError):
            batch.load_entries(self.args(path, ["BV13x41117TL"]))

    def test_no_playinfo_cannot_claim_verified_status(self):
        path = self.write_manifest([{"bvid": BV, "page": 1, "status": "verified_dolby_vision_atmos"}])
        report = self.directory / "report.json"
        code = self.call_main(["probe", "--manifest", str(path), "--report", str(report)])
        result = json.loads(report.read_text())["entries"][0]
        self.assertNotIn(result["status"], ("verified_dolby_vision_atmos", "verified_atmos"))
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("download", result)
        self.assertEqual(code, 0)  # Successful discovery is not media verification.

    def test_report_cannot_echo_unsigned_manifest_secrets(self):
        path = self.write_manifest([{
            "bvid": BV, "page": 1, "status": "api_error",
            "error": "Cookie: SESSDATA=private_cookie https://upos.test.bilivideo.com/a?upsig=private_signature",
            "classification": {"status": "unavailable", "url": "https://upos.test.bilivideo.com/a?upsig=private_signature",
                               "cookie": "private_cookie"},
        }])
        report = self.directory / "report.json"
        self.call_main(["probe", "--manifest", str(path), "--report", str(report)])
        text = report.read_text()
        self.assertNotIn("private_signature", text)
        self.assertNotIn("private_cookie", text)

    def test_non_atmos_skips_before_any_stream_download(self):
        path = self.write_manifest([manifest_entry(kind=1)])
        report = self.directory / "report.json"
        with mock.patch.object(batch, "download_stream") as download:
            code = self.call_main(["download", "--manifest", str(path), "--report", str(report),
                                   "--output-dir", str(self.directory / "videos"), "--no-native"])
        download.assert_not_called()
        result = json.loads(report.read_text())["entries"][0]
        self.assertEqual(result["status"], "skipped")
        self.assertNotIn("download", result)
        self.assertEqual(code, 0)

    def test_verification_failure_is_failed_and_nonzero(self):
        path = self.write_manifest([manifest_entry()])
        report = self.directory / "report.json"
        with mock.patch.object(batch, "download_entry", side_effect=ValueError("原始帧头没有 Atmos")):
            code = self.call_main(["download", "--manifest", str(path), "--report", str(report),
                                   "--output-dir", str(self.directory / "videos"), "--no-native"])
        result = json.loads(report.read_text())["entries"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(code, 1)

    def test_manifest_header_and_identifiers_cannot_inject_paths_or_headers(self):
        for field, value in [("cid", "../123"), ("page", 0), ("cid", True)]:
            with self.subTest(field=field, value=value):
                path = self.write_manifest([manifest_entry(**{field: value})])
                with self.assertRaises(ValueError):
                    batch.load_entries(self.args(path))
        path = self.write_manifest([manifest_entry()])
        value = json.loads(path.read_text())
        value.update(user_agent="Safari\r\nCookie: private_cookie", created_at={"bad": "object"})
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            batch.load_entries(self.args(path))
        value["user_agent"] = "Safari test UA"
        path.write_text(json.dumps(value))
        self.assertEqual(batch.load_entries(self.args(path))[1], "Safari test UA")


class BatchStreamTests(unittest.TestCase):
    def test_string_backup_is_one_url(self):
        url = "https://upos.test.bilivideo.com/backup?upsig=private_signature"
        self.assertEqual(batch.stream_urls({"backupUrl": url}), [url])

    def test_expired_and_malformed_urls_are_skipped_for_live_backup(self):
        backup = "https://upos.test.bilivideo.com/live?deadline=4102444800"
        for primary in ["https://upos.test.bilivideo.com/expired?deadline=1",
                        "https://upos.test.bilivideo.com:bad/file",
                        "https://upos.test.bilivideo.com/file\r\nCookie: secret"]:
            with self.subTest(primary=primary):
                self.assertEqual(batch.stream_urls({"baseUrl": primary, "backupUrl": [backup]}), [backup])

    def test_untrusted_urls_never_reach_downloader(self):
        for url in ["file:///etc/passwd", "https://127.0.0.1/file", "https://example.com/file",
                    "https://bilivideo.com.evil.test/file", "https://user:password@upos.bilivideo.com/file"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                batch.stream_urls({"baseUrl": url})

    def test_cdn_failure_retries_without_session_credentials(self):
        calls = []
        options_seen = []
        class Downloader:
            def __init__(self, options):
                options_seen.append(options)
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def process_ie_result(self, info, download):
                calls.append(info)
                if len(calls) == 1:
                    raise RuntimeError("CDN unavailable")
                Path(self.options["outtmpl"]).write_bytes(b"downloaded")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {"yt_dlp": types.SimpleNamespace(YoutubeDL=Downloader)}), \
                mock.patch.object(batch.shutil, "which", return_value=None), \
                mock.patch.object(batch, "_validate_stream", return_value="a" * 64), \
                contextlib.redirect_stdout(io.StringIO()):
            batch.download_stream({"baseUrl": "https://upos.test.bilivideo.com/a?upsig=one",
                                   "backupUrl": ["https://upos.test.bilivideo.cn/a?upsig=two"],
                                   "codecs": "ec-3", "http_headers": {"Cookie": "secret", "Authorization": "secret"}},
                                  Path(tmp) / "audio.m4a", "audio", "Safari test UA")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["formats"][0]["url"], calls[1]["formats"][0]["url"])
        for options in options_seen:
            self.assertNotIn("cookiefile", options)
            self.assertNotIn("cookiesfrombrowser", options)
            self.assertEqual(set(options["http_headers"]), {"User-Agent", "Referer"})
            self.assertNotIn("external_downloader", options)
            self.assertTrue(options["continuedl"])
            self.assertFalse(options["nopart"])
        for call in calls:
            self.assertNotIn("Cookie", call["formats"][0]["http_headers"])
            self.assertNotIn("Authorization", call["formats"][0]["http_headers"])

    def test_protected_marker_and_wrapped_preview_block_before_download(self):
        video, audio = batch.select_streams(fixture())
        cases = [({"drm_tech_type": 1}, video, audio), ({}, {**video, "drm_type": 1}, audio),
                 ({"code": 0, "data": {"is_preview": True}}, video, audio),
                 ({"code": 0, "data": {"is_drm": True}}, video, audio)]
        for data, v, a in cases:
            with self.subTest(data=data, video=v), self.assertRaises(ValueError):
                batch.reject_protected(data, v, a)

    def test_failed_download_preserves_anchored_part_and_resumes_on_new_run(self):
        calls = []
        class Downloader:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def process_ie_result(self, info, download):
                path = Path(self.options["outtmpl"])
                part = path.with_suffix(path.suffix + ".part")
                calls.append(info)
                if len(calls) == 1:
                    part.write_bytes(b"prefix")
                    raise RuntimeError("interrupted transfer")
                self.assert_resume = part.read_bytes() == b"prefix" and self.options["continuedl"]
                if not self.assert_resume:
                    raise AssertionError("resume bytes lost")
                path.write_bytes(part.read_bytes() + b"suffix")
                part.unlink()
        stream = {"baseUrl": "https://upos.test.bilivideo.com/audio?upsig=old", "codecs": "ec-3"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {"yt_dlp": types.SimpleNamespace(YoutubeDL=Downloader)}), \
                mock.patch.object(batch, "_validate_stream", return_value="a" * 64), \
                contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp) / "audio.m4a"
            with self.assertRaises(RuntimeError):
                batch.download_stream(stream, path, "audio", "Safari test UA")
            record = path.with_suffix(".m4a.download.json")
            self.assertTrue(record.is_file())
            self.assertNotIn("upsig", record.read_text())
            batch.download_stream({**stream, "baseUrl": stream["baseUrl"].replace("old", "refreshed")},
                                  path, "audio", "Safari test UA")
            self.assertEqual(path.read_bytes(), b"prefixsuffix")
            self.assertEqual(json.loads(record.read_text())["payload_sha256"], "a" * 64)

    def test_unknown_cache_and_wrong_stream_identity_are_never_reused(self):
        stream = {"baseUrl": "https://upos.test.bilivideo.com/audio", "codecs": "ec-3"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {"yt_dlp": types.SimpleNamespace(YoutubeDL=mock.Mock())}), \
                mock.patch.object(batch, "_validate_stream") as validate:
            path = Path(tmp) / "audio.m4a"
            path.write_bytes(b"unknown source")
            with self.assertRaisesRegex(ValueError, "身份记录"):
                batch.download_stream(stream, path, "audio", "Safari test UA")
            batch.save_json(path.with_suffix(".m4a.download.json"), {
                "schema": "bili-stream-cache-v1", "identity": "different"})
            with self.assertRaisesRegex(ValueError, "不同"):
                batch.download_stream(stream, path, "audio", "Safari test UA")
            validate.assert_not_called()
            self.assertEqual(path.read_bytes(), b"unknown source")

    def test_complete_cache_is_fully_checked_and_changed_bytes_rejected(self):
        stream = {"baseUrl": "https://upos.test.bilivideo.com/audio", "codecs": "ec-3"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {"yt_dlp": types.SimpleNamespace(YoutubeDL=mock.Mock())}) as modules, \
                mock.patch.object(batch, "_validate_stream", return_value="b" * 64) as validate:
            path = Path(tmp) / "audio.m4a"
            path.write_bytes(b"changed media")
            record = {"schema": "bili-stream-cache-v1", "identity": batch._stream_identity(stream, "audio"),
                      "payload_sha256": "a" * 64}
            batch.save_json(path.with_suffix(".m4a.download.json"), record)
            with self.assertRaisesRegex(ValueError, "已改变"):
                batch.download_stream(stream, path, "audio", "Safari test UA")
            validate.assert_called_once_with(path, "audio")
            sys.modules["yt_dlp"].YoutubeDL.assert_not_called()


class BatchMediaTests(unittest.TestCase):
    def test_declared_rpu_without_actual_valid_payload_never_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.mp4"
            path.write_bytes(b"fixture")
            with mock.patch.object(batch, "probe", side_effect=[ffprobe_fixture(), ffprobe_fixture(raw=True)]), \
                    mock.patch.object(batch, "run_media_tool"), \
                    mock.patch.object(batch, "inspect_container", return_value=container_fixture()), \
                    mock.patch.object(batch, "scan_hevc_rpu", return_value={"verified": False}), \
                    mock.patch.object(batch, "payload_hash") as digest:
                with self.assertRaisesRegex(ValueError, "RPU"):
                    batch.verify_media(path, native=False)
                digest.assert_not_called()

    def test_unassessed_dv_profile_warns_without_claiming_spatial_output(self):
        media = ffprobe_fixture()
        media["streams"][0]["side_data_list"][0].update(dv_profile=8, dv_bl_signal_compatibility_id=1)
        rpu = {"verified": True, "video_payload_sha256": "v" * 64}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.mp4"
            path.write_bytes(b"fixture")
            with mock.patch.object(batch, "probe", side_effect=[media, ffprobe_fixture(raw=True)]), \
                    mock.patch.object(batch, "run_media_tool"), \
                    mock.patch.object(batch, "inspect_container", return_value=container_fixture()), \
                    mock.patch.object(batch, "scan_hevc_rpu", return_value=rpu), \
                    mock.patch.object(batch, "payload_hash", return_value="a" * 64):
                result = batch.verify_media(path, native=False)
            self.assertEqual(result["status"], "verified_dolby_vision_atmos")
            self.assertIn("Profile 8 / compatibility 1", " ".join(result["warnings"]))
            self.assertFalse(result["spatial_audio_output_verified"])
            self.assertEqual(result["raw_eac3_probe_scope"], "first_10_seconds")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_h264_payload_hash_survives_container_remux_and_detects_changed_content(self):
        # Atmos-only mode may select ordinary H.264 video. This catches a hash
        # verifier accidentally hard-coded to HEVC or hashing container bytes.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            black, remuxed, red = [directory / name for name in ("black.mp4", "remuxed.mp4", "red.mp4")]
            for path, color in [(black, "black"), (red, "red")]:
                subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                                f"color=c={color}:s=64x64:r=24", "-t", "0.2", "-c:v", "libx264",
                                "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True, timeout=20)
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(black), "-c", "copy",
                            "-movflags", "+faststart", str(remuxed)], check=True, capture_output=True, timeout=20)
            self.assertEqual(batch.payload_hash(black, "v"), batch.payload_hash(remuxed, "v"))
            self.assertNotEqual(batch.payload_hash(black, "v"), batch.payload_hash(red, "v"))

    def test_allow_non_dv_does_not_certify_incomplete_dovi_declaration(self):
        media = ffprobe_fixture()
        media["streams"][0]["side_data_list"][0]["rpu_present_flag"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.mp4"
            path.write_bytes(b"not-real-media")
            with mock.patch.object(batch, "probe", side_effect=[media, ffprobe_fixture(raw=True)]), \
                    mock.patch.object(batch.subprocess, "run"), \
                    mock.patch.object(batch, "payload_hash", return_value="a" * 64), \
                    mock.patch.object(batch, "inspect_container", return_value=container_fixture()):
                result = batch.verify_media(path, require_dv=False, native=False)
                self.assertEqual(result["status"], "verified_atmos")

    def test_container_label_cannot_replace_raw_atmos_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.mp4"
            path.write_bytes(b"not-real-media")
            with mock.patch.object(batch, "probe", side_effect=[ffprobe_fixture(), ffprobe_fixture(raw=True, atmosphere=False)]), \
                    mock.patch.object(batch.subprocess, "run"), \
                    mock.patch.object(batch, "inspect_container", return_value=container_fixture()) as inspect:
                with self.assertRaises(ValueError):
                    batch.verify_media(path, native=False)
                inspect.assert_not_called()

    def test_bad_container_signaling_never_verifies(self):
        for container in [container_fixture(valid_joc=False), container_fixture(valid_video=False)]:
            with self.subTest(container=container), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "test.mp4"
                path.write_bytes(b"not-real-media")
                with mock.patch.object(batch, "probe", side_effect=[ffprobe_fixture(), ffprobe_fixture(raw=True)]), \
                        mock.patch.object(batch.subprocess, "run"), \
                        mock.patch.object(batch, "inspect_container", return_value=container):
                    with self.assertRaises(ValueError):
                        batch.verify_media(path, native=False)

    def run_entry_pipeline(self, directory, failed_verification=False, mismatched_payload=False, publish_race=False):
        entry = manifest_entry()
        def download(stream, path, kind, ua):
            path.write_bytes(kind.encode())
        def merge(command, **kwargs):
            Path(command[-1]).write_bytes(b"merged")
        def verification(path, **kwargs):
            if failed_verification:
                raise ValueError("invalid Apple MP4 JOC signaling")
            if publish_race:
                (directory / f"{BV}_P01_12345_DV_Atmos.mp4").write_bytes(b"other writer")
            return {"path": str(path), "status": "verified_dolby_vision_atmos",
                    "ffprobe": {"format": {"filename": str(path)}}, "container": {"path": str(path)},
                    "apple_native": {"path": str(path)}}
        def hash_payload(path, kind):
            return "changed" if mismatched_payload and Path(path).name.endswith(".building.mp4") and kind == "v" else kind
        with mock.patch.object(batch, "download_stream", side_effect=download), \
                mock.patch.object(batch, "probe", return_value=ffprobe_fixture(raw=True)), \
                mock.patch.object(batch, "run_media_tool", side_effect=merge), \
                mock.patch.object(batch, "verify_media", side_effect=verification), \
                mock.patch.object(batch, "payload_hash", side_effect=hash_payload):
            return batch.download_entry(entry, directory, "dual", "Safari test UA", False, False)

    def test_payload_mismatch_cannot_publish_final_video_or_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with self.assertRaises(ValueError):
                self.run_entry_pipeline(directory, mismatched_payload=True)
            self.assertFalse(list(directory.glob("*_DV_Atmos.mp4")))
            self.assertFalse(list(directory.glob("*.verification.json")))
            self.assertTrue(list((directory / ".streams").glob("*.mp4")))

    def test_verification_failure_cannot_publish_final_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with self.assertRaises(ValueError):
                self.run_entry_pipeline(directory, failed_verification=True)
            self.assertFalse(list(directory.glob("*_DV_Atmos.mp4")))
            self.assertFalse(list(directory.glob("*.verification.json")))

    def test_valid_publish_has_payload_evidence_without_manifest_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            result = self.run_entry_pipeline(directory)
            self.assertTrue(Path(result["path"]).is_file())
            self.assertTrue(result["payloads_unchanged"])
            self.assertEqual(result["payload_sha256"], {"video": "v", "audio": "a"})
            self.assertFalse(list(directory.glob("*.building.mp4")))
            self.assertFalse(list((directory / ".streams").iterdir()))
            saved = next(directory.glob("*.verification.json")).read_text()
            self.assertNotIn("private_signature", saved)
            self.assertNotIn("playinfo", saved)
            self.assertEqual(json.loads(saved)["ffprobe"]["format"]["filename"], result["path"])

    def test_publish_race_cannot_replace_another_completed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with self.assertRaises(FileExistsError):
                self.run_entry_pipeline(directory, publish_race=True)
            output = next(directory.glob("*_DV_Atmos.mp4"))
            self.assertEqual(output.read_bytes(), b"other writer")
            self.assertFalse(list(directory.glob("*.verification.json")))
            self.assertFalse(list(directory.glob(".*.building.mp4")))

    def test_existing_file_revalidated_without_inventing_original_stream_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            output = directory / f"{BV}_P01_12345_DV_Atmos.mp4"
            output.write_bytes(b"already present")
            result = {"path": str(output), "payload_sha256": {"video": "a" * 64, "audio": "b" * 64}}
            with mock.patch.object(batch, "verify_media", return_value=result) as verify, \
                    mock.patch.object(batch, "download_stream") as download:
                current = batch.download_entry(manifest_entry(), directory, "dual", "Safari test UA", False, False)
            verify.assert_called_once_with(output, require_dv=True, native=False)
            download.assert_not_called()
            self.assertTrue(current["existing_file_verified"])
            self.assertFalse(current["payloads_unchanged"])
            self.assertIn("不能证明", " ".join(current["warnings"]))

    def test_existing_file_hash_mismatch_preserves_previous_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            output = directory / f"{BV}_P01_12345_DV_Atmos.mp4"
            output.write_bytes(b"already present")
            previous = {"source": {"bvid": BV, "cid": 12345, "page": 1}, "payloads_unchanged": True,
                        "payload_sha256": {"video": "a" * 64, "audio": "b" * 64}}
            report = output.with_suffix(".verification.json")
            batch.save_json(report, previous)
            before = report.read_bytes()
            current = {"path": str(output), "payload_sha256": {"video": "c" * 64, "audio": "b" * 64}}
            with mock.patch.object(batch, "verify_media", return_value=current), \
                    mock.patch.object(batch, "download_stream") as download, \
                    self.assertRaisesRegex(ValueError, "不一致"):
                batch.download_entry(manifest_entry(), directory, "dual", "Safari test UA", False, False)
            download.assert_not_called()
            self.assertEqual(report.read_bytes(), before)

    def test_atomic_report_failure_keeps_prior_valid_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            batch.save_json(path, {"valid": "prior"})
            before = path.read_bytes()
            with mock.patch.object(batch.os, "replace", side_effect=OSError("disk failure")), \
                    self.assertRaises(OSError):
                batch.save_json(path, {"new": "unpublished"})
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(path.parent.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
