"""Meaningful negative cases: metadata/codec claims must never certify Atmos."""
import json
import copy
import os
import shutil
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

from bilibili_api import (ApiClient, _exception_record, _sanitize_playinfo, classify_playinfo,
                         parse_bvids, render_safari_export, select_streams)

BV = "BV1RUfgBiEiY"


def fixture(kind=2, codecs="ec-3", dv=True, urls=True):
    url = "https://example.bilivideo.com/stream.m4s" if urls else None
    return {"accept_quality": [126, 120], "support_formats": [{"quality": 126}], "dash": {
        "video": [{"id": 126 if dv else 120, "width": 3840, "height": 2160,
                   "bandwidth": 4000000, "codecs": "hev1.2.4.L150.90", "baseUrl": url}],
        "audio": [{"id": 30280, "codecs": "mp4a.40.2", "baseUrl": url}],
        "dolby": {"type": kind, "audio": [{"id": 30250, "codecs": codecs,
                                             "bandwidth": 768000, "base_url": url}]}}}


class DolbyClassificationTests(unittest.TestCase):
    def test_expired_audio_is_unavailable_but_live_backup_is_usable(self):
        info = fixture()
        audio = info["dash"]["dolby"]["audio"][0]
        audio["base_url"] = "https://example.bilivideo.com/audio?deadline=100"
        with patch("bilibili_api.time.time", return_value=200):
            self.assertEqual(classify_playinfo(info)["status"], "unavailable")
            with self.assertRaises(ValueError):
                select_streams(info)
            audio["backupUrl"] = ["https://example.bilivideo.com/audio?deadline=300"]
            self.assertTrue(classify_playinfo(info)["dual_candidate"])
            self.assertEqual(select_streams(info)[1], audio)

    def test_unknown_expiry_is_allowed_but_malformed_or_userinfo_url_is_not(self):
        for url in ["https://user:password@example.bilivideo.com/a", "https://[bad/a",
                    "https://example.bilivideo.com/a?deadline=invalid"]:
            with self.subTest(url=url):
                info = fixture()
                info["dash"]["dolby"]["audio"][0]["base_url"] = url
                self.assertFalse(classify_playinfo(info)["api_atmos_candidate"])
        self.assertTrue(classify_playinfo(fixture())["dual_candidate"])

    def test_protection_and_preview_survive_sanitizing_and_stop_selection(self):
        for key, value in [("drm_type", 2), ("drm_tech_type", 3), ("is_drm", "true"),
                           ("widevine_pssh", "opaque-license-data"),
                           ("bilidrm_uri", "https://license.invalid/?token=private"),
                           ("is_preview", True)]:
            with self.subTest(key=key):
                info = fixture()
                info[key] = value
                clean = _sanitize_playinfo(info)
                self.assertEqual(classify_playinfo(clean)["status"], "unavailable")
                self.assertFalse(classify_playinfo(clean)["dual_candidate"])
                with self.assertRaises(ValueError):
                    select_streams(clean)
                self.assertNotIn("token=private", json.dumps(clean))

    def test_selector_uses_clear_alternative_instead_of_higher_bandwidth_drm(self):
        info = fixture()
        clear = info["dash"]["dolby"]["audio"][0]
        protected = copy.deepcopy(clear)
        protected.update(drm_tech_type=2, bandwidth=1024000)
        info["dash"]["dolby"]["audio"].append(protected)
        clean = _sanitize_playinfo(info)
        self.assertEqual(select_streams(clean)[1]["bandwidth"], clear["bandwidth"])
        self.assertEqual(clean["dash"]["dolby"]["audio"][1]["drm_tech_type"], 2)

    def test_false_protection_strings_do_not_block_clear_streams(self):
        info = fixture()
        info.update(is_drm="false", is_preview="0", drm_type="0")
        info["dash"]["dolby"]["audio"][0].update(is_drm="false", drm_tech_type="0")
        self.assertTrue(classify_playinfo(_sanitize_playinfo(info))["dual_candidate"])

    def test_nested_metadata_and_non_url_backups_cannot_leak_account_fields(self):
        info = fixture()
        info["support_formats"][0].update(mid=99, cookie="private")
        info["dash"]["video"][0].update(
            SegmentBase={"Initialization": "0-100", "indexRange": "101-200", "cookie": "private"},
            backupUrl=[{"cookie": "private"}, "https://user:private@example.bilivideo.com/a",
                       "https://example.bilivideo.com/a?deadline=4102444800"],
            width={"cookie": "private"})
        info["accept_quality"].append({"cookie": "private"})
        clean = _sanitize_playinfo(info)
        self.assertNotIn("private", json.dumps(clean))
        self.assertEqual(clean["dash"]["video"][0]["SegmentBase"]["indexRange"], "101-200")
        self.assertEqual(len(clean["dash"]["video"][0]["backupUrl"]), 1)

    def test_malformed_api_data_and_quality_shapes_do_not_certify_streams(self):
        for data in [None, [], "invalid"]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                classify_playinfo({"code": 0, "data": data})
        info = fixture()
        info.update(accept_quality=126, support_formats="wrong-shape")
        info["dash"]["dolby"]["type"] = 2.5
        self.assertFalse(classify_playinfo(info)["api_atmos_candidate"])
        info["dash"]["dolby"]["type"] = float("inf")
        self.assertFalse(classify_playinfo(info)["api_atmos_candidate"])

    def test_type1_ec3_does_not_become_atmos(self):
        info = fixture(kind=1)
        classified = classify_playinfo(info)
        self.assertTrue(classified["has_ec3"])
        self.assertFalse(classified["api_atmos_candidate"])
        self.assertEqual(classified["status"], "non_atmos")
        with self.assertRaises(ValueError):
            select_streams(info)

    def test_type2_aac_cannot_substitute_for_atmos(self):
        info = fixture(codecs="mp4a.40.2")
        classified = classify_playinfo(info)
        self.assertTrue(classified["api_advertised_atmos"])
        self.assertFalse(classified["api_atmos_candidate"])
        self.assertEqual(classified["status"], "unavailable")
        with self.assertRaises(ValueError):
            select_streams(info, "atmos")

    def test_advertised_dv_does_not_count_as_accessible_dv(self):
        info = fixture(dv=False)
        classified = classify_playinfo(info)
        self.assertTrue(classified["advertised_dv"])
        self.assertFalse(classified["has_dv"])
        self.assertFalse(classified["dual_candidate"])
        self.assertEqual(classified["status"], "atmos_only_candidate")
        with self.assertRaises(ValueError):
            select_streams(info)
        video, audio = select_streams(info, "atmos")
        self.assertEqual((video["id"], audio["id"]), (120, 30250))

    def test_metadata_without_urls_is_unavailable(self):
        classified = classify_playinfo(fixture(urls=False))
        self.assertFalse(classified["has_dv"])
        self.assertFalse(classified["has_ec3"])
        self.assertFalse(classified["dual_candidate"])
        self.assertEqual(classified["status"], "unavailable")

    def test_empty_response_is_not_a_non_atmos_verdict(self):
        self.assertEqual(classify_playinfo({})["status"], "unavailable")
        with self.assertRaises(ValueError):
            classify_playinfo({"code": -403, "data": {}})

    def test_export_discards_account_fields_but_keeps_expiring_stream_url(self):
        info = fixture()
        info.update(mid=123, vip_status=1, last_play_time=5000, last_play_cid=99)
        info["dash"]["video"][0].update(mid=123, cookie="secret")
        signed_url = "https://example.bilivideo.com/video.m4s?deadline=123&upsig=abc"
        info["dash"]["video"][0]["baseUrl"] = signed_url
        clean = _sanitize_playinfo(info)
        self.assertNotIn("mid", clean)
        self.assertNotIn("vip_status", clean)
        self.assertNotIn("last_play_time", clean)
        self.assertNotIn("cookie", clean["dash"]["video"][0])
        self.assertEqual(clean["dash"]["video"][0]["baseUrl"], signed_url)

    def test_auth_and_risk_errors_do_not_leak_request_urls(self):
        for message, status, code in [
            ("Unable to download video info: 352: risk https://example.com/?secret=xyz", "risk_error", -352),
            ("HTTP Error 403: https://example.com/?cookie=secret", "auth_error", -403),
        ]:
            record = _exception_record(RuntimeError(message))
            self.assertEqual(record["status"], status)
            self.assertEqual(record["error_code"], code)
            self.assertNotIn("secret", record["error"])
        record = _exception_record(ValueError("SESSDATA=private; token=secret"))
        self.assertNotIn("private", record["error"])
        self.assertNotIn("secret", record["error"])

    def test_selects_accessible_dv_and_best_dolby_audio(self):
        info = fixture()
        info["dash"]["video"].append({"id": 127, "width": 7680, "height": 4320,
                                         "bandwidth": 8000000, "base_url": "https://example.com/8k"})
        info["dash"]["dolby"]["audio"].append({"id": 30250, "codecs": "ec-3",
                                                  "bandwidth": 1024000, "baseUrl": "https://example.com/audio"})
        video, audio = select_streams({"code": 0, "data": info})
        self.assertEqual(video["id"], 126)
        self.assertEqual(audio["bandwidth"], 1024000)


class ApiClientTests(unittest.TestCase):
    def test_invalid_and_missing_page_ids_do_not_stop_later_pages(self):
        client = ApiClient.__new__(ApiClient)
        client.delay = 0
        client.user_agent = "Safari test"
        client._ie = Mock()
        client._ie._download_json.return_value = {"code": 0, "data": {
            "title": "Test", "pages": [{"cid": 11, "page": 1}, {"cid": 0, "page": 2},
                                         {"cid": "12", "page": 3}]}}
        restricted = fixture()
        restricted["drm_type"] = 2
        client._ie._download_playinfo.side_effect = [restricted, fixture()]
        entries = client.fetch_video(BV)
        self.assertEqual([e["status"] for e in entries], ["unavailable", "api_error", "dual_candidate"])
        self.assertEqual(client._ie._download_playinfo.call_count, 2)
        self.assertEqual(entries[2]["cid"], 12)
        self.assertTrue(entries[0]["playinfo"]["is_drm"])

    def test_invalid_view_response_is_safe_and_auth_code_stays_distinct(self):
        client = ApiClient.__new__(ApiClient)
        client.delay = 0
        client.user_agent = "Safari test"
        client._ie = Mock()
        for response in [[], {"code": 0, "data": ["private"]},
                         {"code": "private", "data": {}}, {"code": {"cookie": "private"}}]:
            client._ie._download_json.return_value = response
            entry = client.fetch_video(BV)[0]
            self.assertEqual(entry["status"], "api_error")
            self.assertNotIn("private", entry["error"])
        client._ie._download_json.return_value = {"code": -101, "message": "private"}
        entry = client.fetch_video(BV)[0]
        self.assertEqual((entry["status"], entry["error_code"]), ("auth_error", -101))
        client._ie._download_playinfo.assert_not_called()

    def test_mixed_sessions_and_header_injection_are_rejected_before_loading_credentials(self):
        for options in [{"cookiefile": "cookies.txt", "cookie_env": "BILI_COOKIE"},
                        {"user_agent": "Safari\r\nCookie: secret"},
                        {"cookie_env": "BILI_COOKIE\ninvalid"}, {"delay": float("nan")},
                        {"timeout": float("inf")}, {"timeout": None}, {"timeout": 0}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                ApiClient(**options)
        with patch.dict(os.environ, {"BILI_TEST_COOKIE": "SESSDATA=private\r\nInjected: secret"}), self.assertRaises(ValueError):
            ApiClient(cookie_env="BILI_TEST_COOKIE")

    def test_extractor_initialization_failure_closes_cookie_owner(self):
        ydl, extractor = Mock(), Mock()
        extractor.initialize.side_effect = RuntimeError("initialization failed")
        modules = {"yt_dlp": types.SimpleNamespace(YoutubeDL=Mock(return_value=ydl)),
                   "yt_dlp.extractor.bilibili": types.SimpleNamespace(BiliBiliIE=Mock(return_value=extractor))}
        with patch.dict(sys.modules, modules), self.assertRaises(RuntimeError):
            ApiClient()
        ydl.close.assert_called_once()


class InputTests(unittest.TestCase):
    def test_exact_ids_and_urls_preserve_case_and_deduplicate(self):
        self.assertEqual(parse_bvids([BV, f"https://www.bilibili.com/video/{BV}/?p=2", "# comment"]), [BV])
        self.assertEqual(parse_bvids(f"{BV}\nBV13x41117TL"), [BV, "BV13x41117TL"])
        self.assertEqual(parse_bvids(f"# list comment\n{BV}\n# ignored BVbroken\nBV13x41117TL"),
                         [BV, "BV13x41117TL"])

    def test_mixed_valid_and_invalid_bv_does_not_silently_process_partial_list(self):
        for value in [f"{BV} BVbroken", f"{BV}\nBV1RUfgBiE0Y", f"{BV} {BV}x"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_bvids(value)

    def test_short_long_and_base58_invalid_ids_rejected(self):
        for value in ["BV1RUfgBiEi", BV + "x", "BV1RUfgBiE0Y", "av123", ""]:
            if not value:
                self.assertEqual(parse_bvids(value), [])
            else:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    parse_bvids(value)

    def test_export_placeholders_and_session_safety(self):
        script = render_safari_export([BV], all_pages=False)
        self.assertNotIn("__BVIDS_JSON__", script)
        self.assertNotIn("__ALL_PAGES_JSON__", script)
        self.assertIn('const allPages = false', script)
        self.assertIn('"bili-dolby-manifest-v1"', script)
        self.assertNotIn("document.cookie;", script)  # No cookie access expression.

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser export mock")
    def test_browser_export_continues_after_page_auth_failure(self):
        script = render_safari_export([BV, "BV13x41117TL"])
        fixture_json = json.dumps(fixture())
        # This exercises the full async export, per-CID continuation, privacy
        # filtering, cookie-safe session requests, and Blob JSON shape.
        harness = r'''
const vm = require('vm');
const script = SCRIPT_JSON;
const play = FIXTURE_JSON;
let exported;
const calls = [];
class ExportURL extends URL {
  static createObjectURL(blob) { exported = blob; return 'blob:test'; }
  static revokeObjectURL() {}
}
const document = {
  body: {appendChild() {}},
  createElement() { return {click() {}, remove() {}}; },
  get cookie() { throw new Error('Cookie access forbidden'); }
};
const context = {
  location: {hostname: 'www.bilibili.com'}, navigator: {userAgent: 'Safari test UA'},
  URL: ExportURL, Blob, document, AbortController,
  setTimeout(fn, delay) { if (delay !== 20000) fn(); return 1; }, clearTimeout() {}, console: {log() {}},
  fetch: async (url, options) => {
    if (options.credentials !== 'include') throw new Error('Session omitted');
    const cid = url.searchParams.get('cid');
    const bvid = url.searchParams.get('bvid');
    calls.push(url.pathname + ':' + (cid || bvid));
    let body;
    if (url.pathname.endsWith('/view')) {
      body = {code: 0, data: {title: 'Test', mid: 999, cookie: 'secret', pages: bvid === 'BV1RUfgBiEiY' ?
        [{cid: 11, page: 1}, {cid: 12, page: 2}] : [{cid: 21, page: 1}]}};
    } else if (cid === '12') body = {code: -403, data: null};
    else {
      const data = JSON.parse(JSON.stringify(play));
      data.mid = 999; data.last_play_time = 100; data.cookie = 'secret';
      if (cid === '21') data.dash.dolby.type = 1;
      body = {code: 0, data};
    }
    return {ok: true, json: async () => body};
  }
};
(async () => {
  await vm.runInNewContext(script, context);
  const manifest = JSON.parse(await exported.text());
  process.stdout.write(JSON.stringify({manifest, calls}));
})();
'''.replace("SCRIPT_JSON", json.dumps(script)).replace("FIXTURE_JSON", fixture_json).replace(
            "location: {hostname: 'www.bilibili.com'}", "location: {protocol: 'https:', hostname: 'www.bilibili.com'}")
        process = subprocess.run([shutil.which("node")], input=harness, text=True,
                                 capture_output=True, check=True, timeout=10)
        result = json.loads(process.stdout)
        entries = result["manifest"]["entries"]
        self.assertEqual([e["status"] for e in entries], ["dual_candidate", "auth_error", "non_atmos"])
        self.assertEqual(entries[1]["error_code"], -403)
        self.assertEqual(result["manifest"]["schema"], "bili-dolby-manifest-v1")
        self.assertEqual(len(result["manifest"]["errors"]), 1)
        self.assertEqual(result["calls"][2], "/x/player/playurl:12")
        self.assertEqual(result["calls"][3], "/x/web-interface/view:BV13x41117TL")
        self.assertNotIn("secret", process.stdout)
        self.assertNotIn("last_play_time", entries[0]["playinfo"])


@unittest.skipUnless(shutil.which("node"), "Node is required for browser export integration")
class BrowserExportTests(unittest.TestCase):
    def run_export(self, responses, *, hostname="www.bilibili.com", protocol="https:"):
        harness = r'''
const vm = require('vm');
const responses = RESPONSES_JSON;
const calls = [];
const requests = [];
let exported;
class ExportURL extends URL {
  static createObjectURL(blob) { exported = blob; return 'blob:test'; }
  static revokeObjectURL() {}
}
const context = {
  location: LOCATION_JSON, navigator: {userAgent: 'Safari test UA'},
  URL: ExportURL, Blob, AbortController,
  document: {
    body: {appendChild() {}}, createElement() { return {click() {}, remove() {}}; },
    get cookie() { throw new Error('Cookie access forbidden'); }
  },
  setTimeout(fn, delay) { if (delay === 20000) queueMicrotask(fn); else fn(); return 1; },
  clearTimeout() {}, console: {log() {}},
  fetch: async (url, options) => {
    const key = url.pathname + ':' + (url.searchParams.get('cid') || url.searchParams.get('bvid'));
    calls.push(key);
    requests.push({credentials: options.credentials, redirect: options.redirect, cache: options.cache,
                   hasAbortSignal: Boolean(options.signal), hasHeaders: Boolean(options.headers)});
    const response = responses[key];
    if (!response) throw new Error('Unexpected request');
    if (response.timeout) return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => {
      const error = new Error('https://private.invalid/?token=private'); error.name = 'AbortError'; reject(error);
    }, {once: true}));
    if (response.throw) { const error = new Error(response.throw); error.name = response.throw; throw error; }
    return {ok: true, json: async () => response};
  }
};
(async () => {
  let fatal;
  try { await vm.runInNewContext(SCRIPT_JSON, context); } catch (error) { fatal = error.message; }
  const manifest = exported ? JSON.parse(await exported.text()) : null;
  process.stdout.write(JSON.stringify({manifest, calls, requests, fatal}));
})();
'''
        harness = harness.replace("RESPONSES_JSON", json.dumps(responses)).replace(
            "LOCATION_JSON", json.dumps({"hostname": hostname, "protocol": protocol})).replace(
            "SCRIPT_JSON", json.dumps(render_safari_export([BV])))
        process = subprocess.run([shutil.which("node")], input=harness, text=True,
                                 capture_output=True, check=True, timeout=10)
        return json.loads(process.stdout)

    def test_errors_protection_expiry_and_invalid_cid_continue_without_private_fields(self):
        protected = fixture()
        protected.update(drm_type=2, bilidrm_uri="https://license.invalid/?token=private")
        expired = fixture()
        expired["dash"]["dolby"]["audio"][0]["base_url"] += "?deadline=1"
        backed_up = copy.deepcopy(expired)
        backed_up["dash"]["dolby"]["audio"][0]["backupUrl"] = [
            {"cookie": "private"}, "https://example.bilivideo.com/audio?deadline=4102444800"]
        backed_up["support_formats"][0]["cookie"] = "private"
        backed_up["dash"]["video"][0]["SegmentBase"] = {"Initialization": "0-100", "cookie": "private"}
        responses = {
            "/x/web-interface/view:" + BV: {"code": 0, "data": {"title": "Test", "pages": [
                {"cid": 11, "page": 1}, {"cid": 0, "page": 2}, {"cid": 13, "page": 3},
                {"cid": 14, "page": 4}, {"cid": 15, "page": 5}, {"cid": 16, "page": 6},
                {"cid": 17, "page": 7}]}},
            "/x/player/playurl:11": {"code": 0, "data": protected},
            "/x/player/playurl:13": {"code": 0, "data": None},
            "/x/player/playurl:14": {"code": 0, "data": expired},
            "/x/player/playurl:15": {"code": 0, "data": backed_up},
            "/x/player/playurl:16": {"timeout": True},
            "/x/player/playurl:17": {"code": 0, "data": fixture()},
        }
        result = self.run_export(responses)
        entries = result["manifest"]["entries"]
        self.assertEqual([e["status"] for e in entries], ["unavailable", "unavailable", "api_error",
                         "unavailable", "dual_candidate", "api_error", "dual_candidate"])
        self.assertNotIn("/x/player/playurl:0", result["calls"])
        self.assertIn("超时", entries[5]["error"])
        self.assertEqual(len(result["manifest"]["errors"]), 3)
        self.assertNotIn("private", json.dumps(result))
        self.assertTrue(entries[0]["playinfo"]["is_drm"])
        for entry in entries:
            if "playinfo" in entry:
                self.assertEqual(classify_playinfo(entry["playinfo"])["status"], entry["status"])
        self.assertTrue(all(request == {"credentials": "include", "redirect": "error", "cache": "no-store",
                                       "hasAbortSignal": True, "hasHeaders": False}
                            for request in result["requests"]))

    def test_wrong_origin_is_rejected_before_any_session_request(self):
        for hostname, protocol in [("www.bilibili.com", "http:"), ("evil.example", "https:"),
                                   ("bilibili.com.evil.example", "https:")]:
            with self.subTest(hostname=hostname, protocol=protocol):
                result = self.run_export({}, hostname=hostname, protocol=protocol)
                self.assertEqual(result["calls"], [])
                self.assertIsNone(result["manifest"])
                self.assertIn("https://www.bilibili.com", result["fatal"])


if __name__ == "__main__":
    unittest.main()
