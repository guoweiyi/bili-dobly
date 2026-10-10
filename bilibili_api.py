"""Bilibili Dolby stream discovery; metadata claims are candidates, not verification.

The API path uses yt-dlp 2026.8.19's WBI implementation. Safari export reuses
the user's browser session without copying cookies into a downloaded file.
"""
from __future__ import annotations

import http.cookiejar
import json
import math
import os
import re
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Safari/605.1.15"
)
_BVID = re.compile(r"(?<![A-Za-z0-9])BV[1-9A-HJ-NP-Za-km-z]{10}(?![A-Za-z0-9])")
_BV_TOKEN = re.compile(r"(?<![A-Za-z0-9])BV[A-Za-z0-9]+(?![A-Za-z0-9])")
_PLAY_FIELDS = (
    "quality", "format", "timelength", "accept_quality", "accept_description",
    "support_formats", "fnval", "fnver", "video_codecid", "is_preview",
    "is_drm", "drm_type", "drm_tech_type", "widevine_pssh", "bilidrm_uri", "dash",
)
_STREAM_FIELDS = (
    "id", "baseUrl", "base_url", "backupUrl", "backup_url", "bandwidth",
    "mimeType", "mime_type", "codecs", "width", "height", "frameRate",
    "frame_rate", "codecid", "size", "SegmentBase", "segment_base",
    "drm_type", "drm_tech_type", "is_drm", "widevine_pssh", "bilidrm_uri",
)
_FORMAT_FIELDS = ("quality", "format", "description", "new_description", "display_desc",
                  "superscript", "codecs", "need_login", "need_vip")
_SEGMENT_FIELDS = ("Initialization", "indexRange", "initialization", "index_range")


def parse_bvids(values) -> list[str]:
    """Extract distinct, case-preserved BV IDs from IDs, URLs, or list text.

    Reject malformed input instead of quietly processing a partial list. Lines
    starting with '#' are comments; other strings must contain a valid BV ID.
    """
    if isinstance(values, str):
        values = [values]
    result = []
    lines = (line.strip() for value in values for line in str(value).splitlines())
    for value in lines:
        if not value or value.startswith("#"):
            continue
        matches = _BVID.findall(value)
        if not matches:
            raise ValueError("找不到有效的 12 位 BV 号；请检查输入列表。")
        if any(not _BVID.fullmatch(token) for token in _BV_TOKEN.findall(value)):
            raise ValueError("输入包含格式错误的 BV 号；未处理部分列表，请检查每一行。")
        for bvid in matches:
            if bvid not in result:
                result.append(bvid)
    return result


def _data(playinfo: dict) -> dict:
    if not isinstance(playinfo, dict):
        raise ValueError("播放信息必须是 JSON 对象。")
    if "code" in playinfo and (not isinstance(playinfo["code"], int) or isinstance(playinfo["code"], bool)):
        raise ValueError("播放 API 返回了无效的 code。")
    if "data" in playinfo:
        if playinfo.get("code", 0) != 0:
            raise ValueError(f"播放 API 失败，code={playinfo.get('code')}")
        if not isinstance(playinfo["data"], dict):
            raise ValueError("播放 API 返回了无效的 data 对象。")
        return playinfo["data"]
    if "code" in playinfo and playinfo.get("code") != 0:
        raise ValueError(f"播放 API 失败，code={playinfo.get('code')}")
    return playinfo


def _as_int(value, default=0) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _streams(value) -> list[dict]:
    return [s for s in value if isinstance(s, dict)] if isinstance(value, list) else []


def _usable_url(value, now=None) -> bool:
    """Reject malformed/expired URLs; unknown expiry still needs a CDN request."""
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or
                parsed.username is not None or parsed.password is not None):
            return False
        _ = parsed.port  # Force validation; hostname alone accepts ':bad-port'.
        deadlines = parse_qs(parsed.query, keep_blank_values=True).get("deadline", [])
        return all(deadline.isdigit() and int(deadline) > (time.time() if now is None else now)
                   for deadline in deadlines)
    except (TypeError, ValueError):
        return False


def _stream_urls(stream: dict) -> list:
    urls = [stream.get("baseUrl"), stream.get("base_url")]
    for field in ("backupUrl", "backup_url"):
        if isinstance(stream.get(field), list):
            urls.extend(stream[field])
    return urls


def _has_url(stream: dict) -> bool:
    return any(_usable_url(url) for url in _stream_urls(stream))


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "none", "null")
    return bool(value)


def _protected(obj: dict) -> bool:
    return any(_flag(obj.get(field)) for field in
               ("is_drm", "drm_type", "drm_tech_type", "widevine_pssh", "bilidrm_uri"))


def _scalars(source: dict, fields) -> dict:
    return {k: source[k] for k in fields if k in source and
            (source[k] is None or isinstance(source[k], (str, int, float, bool))) and
            not (isinstance(source[k], float) and not math.isfinite(source[k]))}


def _ec3(stream: dict) -> bool:
    return str(stream.get("codecs", "")).strip().lower() in ("ec-3", "eac3", "e-ac-3")


def classify_playinfo(playinfo: dict) -> dict:
    """Distinguish an accessible Atmos/DV candidate from advertised qualities."""
    data = _data(playinfo)
    dash = data.get("dash") if isinstance(data.get("dash"), dict) else {}
    dolby = dash.get("dolby") if isinstance(dash.get("dolby"), dict) else {}
    kind = _as_int(dolby.get("type"))
    videos = _streams(dash.get("video"))
    dolby_audio = _streams(dolby.get("audio"))
    dv = [s for s in videos if _as_int(s.get("id")) == 126 and _has_url(s) and not _protected(s)]
    ec3 = [s for s in dolby_audio if _ec3(s) and _has_url(s) and not _protected(s)]
    qualities = data.get("accept_quality")
    advertised = any(_as_int(q) == 126 for q in qualities) if isinstance(qualities, list) else False
    advertised |= any(_as_int(s.get("quality")) == 126
                      for s in _streams(data.get("support_formats")))
    restricted = _protected(data) or _flag(data.get("is_preview"))
    candidate = kind == 2 and bool(ec3) and not restricted
    if restricted:
        status = "unavailable"
        reason = ("接口仅返回预览，不能作为完整视频下载。" if _flag(data.get("is_preview")) else
                  "接口返回受保护的流；此工具仅处理直接可下载的原始流。")
    elif candidate:
        status = "dual_candidate" if dv else "atmos_only_candidate"
        reason = "可获取 Atmos 候选音轨与 Dolby Vision 视频；下载后仍须验证码流。" if dv else (
            "可获取 Atmos 候选音轨，但当前响应未提供 Dolby Vision 视频。")
    elif kind == 2:
        status = "unavailable"
        reason = "API 标为全景声，但未提供可用的未加密 E-AC-3 音轨；地址可能已过期，请重新导出并检查登录、会员及权限。"
    elif not any(_has_url(s) for s in videos + _streams(dash.get("audio")) + dolby_audio):
        status = "unavailable"
        reason = "没有可获取的 DASH 音视频流；不能据此判为非全景声。"
    else:
        status = "non_atmos"
        reason = "API 未标为全景声；普通杜比音效、E-AC-3 或 30250 均不单独证明 Atmos。"
    return {
        "type": kind, "dolby_type": kind, "api_advertised_atmos": kind == 2,
        "api_atmos_candidate": candidate, "has_dv": bool(dv) and not restricted,
        "has_ec3": bool(ec3) and not restricted,
        "dual_candidate": candidate and bool(dv), "advertised_dv": bool(advertised),
        "status": status, "reason": reason,
    }


def select_streams(playinfo: dict, mode: str = "dual") -> tuple[dict, dict]:
    """Select original streams. 'atmos' permits non-DV video; never AAC audio."""
    if mode not in ("dual", "atmos"):
        raise ValueError("mode 必须为 dual 或 atmos。")
    data = _data(playinfo)
    classification = classify_playinfo(data)
    if not classification["api_atmos_candidate"]:
        raise ValueError(classification["reason"])
    dash = data["dash"]
    videos = [s for s in _streams(dash.get("video")) if _has_url(s) and not _protected(s)]
    if mode == "dual":
        videos = [s for s in videos if _as_int(s.get("id")) == 126]
    else:
        # Keep the Atmos-only fallback compatible with native Apple players.
        videos = [s for s in videos if str(s.get("codecs", "")).strip().lower().startswith(
            ("hvc", "hev", "dvh", "dvhe", "avc"))]
    if not videos:
        raise ValueError("当前响应没有可获取的 Dolby Vision 视频。" if mode == "dual"
                         else "当前响应没有可获取的视频轨道。")
    # Prefer DV when present even in atmos mode, then pixel count, frame rate,
    # and bandwidth. Never treat qn numerically as a resolution measurement.
    def rank(s):
        fps = str(s.get("frameRate", s.get("frame_rate", "0")))
        try:
            numerator, _, denominator = fps.partition("/")
            fps_value = float(numerator) / float(denominator or "1")
        except (ValueError, ZeroDivisionError):
            fps_value = 0
        if not math.isfinite(fps_value):
            fps_value = 0
        return (_as_int(s.get("id")) == 126,
                _as_int(s.get("width")) * _as_int(s.get("height")),
                fps_value, _as_int(s.get("bandwidth")))
    video = max(videos, key=rank)
    audio = max((s for s in _streams(dash["dolby"].get("audio"))
                 if _ec3(s) and _has_url(s) and not _protected(s)),
                key=lambda s: _as_int(s.get("bandwidth")))
    return video, audio


def _sanitize_playinfo(playinfo: dict) -> dict:
    """Keep stream metadata; omit account, playback history, and other fields."""
    data = _data(playinfo)
    result = _scalars(data, _PLAY_FIELDS)
    if isinstance(data.get("support_formats"), list):
        result["support_formats"] = [_scalars(s, _FORMAT_FIELDS)
                                     for s in _streams(data["support_formats"])]
    if isinstance(data.get("accept_quality"), list):
        result["accept_quality"] = [_as_int(q) for q in data["accept_quality"]
                                    if isinstance(q, (int, str))]
    if isinstance(data.get("accept_description"), list):
        result["accept_description"] = [s for s in data["accept_description"] if isinstance(s, str)]
    # License locations are not useful to this direct-stream downloader.
    for field in ("widevine_pssh", "bilidrm_uri"):
        if field in data:
            result[field] = _flag(data[field])
    for field in ("is_drm", "is_preview"):
        if field in data:
            result[field] = _flag(data[field])
    if _protected(data):
        result["is_drm"] = True
    for field in ("drm_type", "drm_tech_type"):
        if field in result and not _flag(result[field]):
            result[field] = 0
    raw_dash = data.get("dash") if isinstance(data.get("dash"), dict) else {}
    dash = _scalars(raw_dash, ("duration", "minBufferTime", "min_buffer_time"))
    def clean_stream(stream):
        clean = _scalars(stream, _STREAM_FIELDS)
        for key in ("SegmentBase", "segment_base"):
            if isinstance(stream.get(key), dict):
                clean[key] = _scalars(stream[key], _SEGMENT_FIELDS)
        for key in ("baseUrl", "base_url"):
            if key in clean and not _usable_url(clean[key], now=0):
                clean.pop(key)
        for key in ("backupUrl", "backup_url"):
            if isinstance(stream.get(key), list):
                clean[key] = [url for url in stream[key] if _usable_url(url, now=0)]
        for key in ("widevine_pssh", "bilidrm_uri", "is_drm"):
            if key in stream:
                clean[key] = _flag(stream[key])
        if _protected(stream):
            clean["is_drm"] = True
        for key in ("drm_type", "drm_tech_type"):
            if key in clean and not _flag(clean[key]):
                clean[key] = 0
        return clean
    for field in ("video", "audio"):
        dash[field] = [clean_stream(s) for s in _streams(raw_dash.get(field))]
    dolby = raw_dash.get("dolby") if isinstance(raw_dash.get("dolby"), dict) else {}
    dash["dolby"] = {"type": _as_int(dolby.get("type")), "audio": [
        clean_stream(s) for s in _streams(dolby.get("audio"))]}
    result["dash"] = dash
    return result


class ApiClient:
    """WBI API client. Credentials are only read when the caller opts in.

    cookie_env names an environment variable containing a Cookie header.
    cookiesfrombrowser accepts a browser name or yt-dlp's tuple form.
    """
    def __init__(self, user_agent=SAFARI_UA, cookiefile=None, cookiesfrombrowser=None,
                 cookie_env=None, timeout=20, delay=0.5):
        if sum(bool(value) for value in (cookiefile, cookiesfrombrowser, cookie_env)) > 1:
            raise ValueError("Cookie 文件、浏览器会话和环境变量只能选一种，避免混用登录会话。")
        if (not isinstance(user_agent, str) or not user_agent.strip() or
                len(user_agent) > 4096 or any(c in user_agent for c in "\r\n\0")):
            raise ValueError("User-Agent 必须为有效的单行字符串。")
        if cookie_env and (not isinstance(cookie_env, str) or
                           not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cookie_env)):
            raise ValueError("请提供有效的 Cookie 环境变量名称。")
        header = os.environ.get(cookie_env) if cookie_env else None
        if cookie_env and not header:
            raise ValueError(f"环境变量 {cookie_env} 未设置或为空。")
        if header and any(c in header for c in "\r\n\0"):
            raise ValueError("Cookie 请求头必须为单行；未读取登录会话。")
        try:
            request_delay, request_timeout = float(delay), float(timeout)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("超时须为正数，请求间隔须为有限数值。") from None
        if not math.isfinite(request_delay) or not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("超时须为正数，请求间隔须为有限数值。")
        from yt_dlp import YoutubeDL
        from yt_dlp.extractor.bilibili import BiliBiliIE

        self.user_agent = user_agent
        self.delay = max(0, request_delay)
        options = {"quiet": True, "no_warnings": True, "socket_timeout": request_timeout,
                   "http_headers": {"User-Agent": user_agent, "Referer": "https://www.bilibili.com/"}}
        if cookiefile:
            options["cookiefile"] = str(cookiefile)
        if cookiesfrombrowser:
            options["cookiesfrombrowser"] = ((cookiesfrombrowser, None, None, None)
                                             if isinstance(cookiesfrombrowser, str) else cookiesfrombrowser)
        self._ydl = YoutubeDL(options)
        self._ie = BiliBiliIE(self._ydl)
        try:
            self._ie.initialize()
        except Exception:
            self._ydl.close()
            raise
        if cookie_env:
            if header.lower().startswith("cookie:"):
                header = header.split(":", 1)[1].strip()
            cookies = SimpleCookie()
            try:
                cookies.load(header)
            except CookieError:
                self.close()
                raise ValueError(f"环境变量 {cookie_env} 不是有效的 Cookie 请求头。") from None
            if not cookies:
                self.close()
                raise ValueError(f"环境变量 {cookie_env} 不是有效的 Cookie 请求头。")
            for name, morsel in cookies.items():
                self._ydl.cookiejar.set_cookie(http.cookiejar.Cookie(
                    0, name, morsel.value, None, False, ".bilibili.com", True, True,
                    "/", True, True, None, True, None, None, {}, False))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self):
        self._ydl.close()

    def fetch_video(self, bvid: str, all_pages: bool = True) -> list[dict]:
        """Return per-CID records, continuing after individual page errors."""
        ids = parse_bvids([bvid])
        if len(ids) != 1 or ids[0] != bvid:
            raise ValueError("fetch_video 需要一个规范 BV 号。")
        entries = []
        try:
            response = self._ie._download_json(
                "https://api.bilibili.com/x/web-interface/view", bvid,
                query={"bvid": bvid}, headers={"User-Agent": self.user_agent,
                                             "Referer": "https://www.bilibili.com/"})
            if not isinstance(response, dict):
                raise ValueError("视频 API 返回了无效的 JSON 对象。")
            code = response.get("code")
            if not isinstance(code, int) or isinstance(code, bool):
                raise ValueError("视频 API 返回了无效的 code。")
            if code != 0:
                return [{"bvid": bvid, "status": _error_status(code), "error_code": code,
                         "error": _error_text(code)}]
            meta = response.get("data")
            if not isinstance(meta, dict):
                raise ValueError("视频 API 返回了无效的 data 对象。")
            pages = _streams(meta.get("pages"))
            if not pages and meta.get("cid"):
                pages = [{"cid": meta["cid"], "page": 1, "part": meta.get("title", bvid)}]
            if not pages:
                return [{"bvid": bvid, "status": "unavailable", "error": "视频信息没有分 P CID。"}]
            if not all_pages:
                pages = pages[:1]
        except Exception as error:
            return [{"bvid": bvid, **_exception_record(error)}]
        for page in pages:
            title = meta.get("title") if isinstance(meta.get("title"), str) else bvid
            entry = {"bvid": bvid, "title": title, "cid": _as_int(page.get("cid")),
                     "page": _as_int(page.get("page"), 1),
                     "part": page.get("part") if isinstance(page.get("part"), str) else title}
            try:
                if entry["cid"] <= 0 or entry["page"] <= 0:
                    raise ValueError("分 P CID 或页码无效。")
                data = self._ie._download_playinfo(
                    bvid, entry["cid"], headers={"User-Agent": self.user_agent,
                                               "Referer": f"https://www.bilibili.com/video/{bvid}/"},
                    query={"qn": 126, "fnval": 4048, "fnver": 0, "fourk": 1})
                entry["playinfo"] = _sanitize_playinfo(data)
                entry["classification"] = classify_playinfo(entry["playinfo"])
                entry["status"] = entry["classification"]["status"]
            except Exception as error:
                entry.update(_exception_record(error))
            entries.append(entry)
            if self.delay:
                time.sleep(self.delay)
        return entries


def _error_text(code) -> str:
    if code in (-101, -403, -10403, -10404):
        return f"接口鉴权或访问权限不足 (code={code})；检查登录、会员、地区及视频权限。"
    if code in (-352, -412, -401):
        return f"接口风控拦截 (code={code})；可改用已登录 Safari 导出，或稍后重试。"
    return f"视频 API 返回错误 (code={code})。"


def _error_status(code) -> str:
    if code in (-101, -403, -10403, -10404):
        return "auth_error"
    if code in (-352, -412, -401):
        return "risk_error"
    return "api_error"


def _exception_code(error) -> int | None:
    message = str(error)
    codes = re.findall(r"(?<!\d)-(?:101|352|412|401|403|10403|10404)(?!\d)", message)
    if codes:
        return int(codes[-1])
    # yt-dlp's helper displays the absolute value of Bilibili's error code.
    match = re.search(r"video info:\s*(\d{1,6})(?!\d)|HTTP Error\s*(\d{1,6})(?!\d)", message, re.I)
    if match:
        return -int(next(value for value in match.groups() if value))
    return None


def _exception_record(error) -> dict:
    code = _exception_code(error)
    result = {"status": _error_status(code), "error": _safe_exception(error)}
    if code is not None:
        result["error_code"] = code
    return result


def _safe_exception(error: Exception) -> str:
    # Error logs must not echo signed stream URLs or caller credentials.
    message = str(error)
    code = _exception_code(error)
    if code is not None:
        return _error_text(code)
    if "cookie" in message.lower():
        return "无法读取指定的 Cookie；可改用已登录 Safari 导出。"
    if isinstance(error, ValueError):
        return "API 返回数据无效；请重新探测或重新导出 Safari 清单。"
    return f"接口请求失败 ({type(error).__name__})；检查网络，或改用已登录 Safari 导出。"


def render_safari_export(bvids, all_pages: bool = True) -> str:
    """Render a self-contained console snippet that exports a cookie-free manifest."""
    ids = parse_bvids(bvids)
    if not ids:
        raise ValueError("请提供至少一个 BV 号。")
    template = Path(__file__).with_name("safari_export.template.js").read_text(encoding="utf-8")
    return template.replace("__BVIDS_JSON__", json.dumps(ids, ensure_ascii=True)).replace(
        "__ALL_PAGES_JSON__", "true" if all_pages else "false")
