#!/usr/bin/env python3
"""按 BV/CID 探测、下载、验证原始杜比流；默认只接受双杜比。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from bilibili_api import ApiClient, SAFARI_UA, _usable_url, classify_playinfo, parse_bvids, render_safari_export, select_streams
from dolby_container import inspect_container
from download_dolby import payload_hash, probe, run_media_tool, safe_error, scan_hevc_rpu

ROOT = Path(__file__).resolve().parent


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the previous report intact if serialization, disk write or rename fails.
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def positive_int(value, field):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or int(value) < 1:
        raise ValueError(f"{field} 必须是正整数。")
    return int(value)


def validate_user_agent(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("清单 user_agent 必须是没有控制字符的有效字符串。")
    return value


def bvids_from(args):
    values = list(args.bvids)
    if args.bv_file:
        values.extend(args.bv_file.read_text(encoding="utf-8").splitlines())
    return parse_bvids(values)


def load_entries(args):
    wanted = bvids_from(args)
    if args.manifest:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != "bili-dolby-manifest-v1":
            raise ValueError("不是受支持的 Safari 播放清单。")
        entries = manifest.get("entries")
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise ValueError("播放清单 entries 无效。")
        found = {e.get("bvid") for e in entries}
        missing = set(wanted) - found
        if missing:
            raise ValueError("清单缺少 BV：" + ", ".join(sorted(missing)) + "；请重新生成完整清单。")
        selected = [e for e in entries if not wanted or e.get("bvid") in wanted]
        for e in selected:
            e["page"] = positive_int(e.get("page", 1), "page")
            if "playinfo" in e:
                e["cid"] = positive_int(e.get("cid"), "cid")
        if args.first_page:
            selected = [e for e in selected if int(e.get("page", 1)) == 1]
        # Validate identifiers and reclassify; exported status is not trusted.
        for e in selected:
            if parse_bvids([e.get("bvid", "")]) != [e.get("bvid")]:
                raise ValueError("清单包含无效 BV。")
            if "playinfo" in e:
                e["classification"] = classify_playinfo(e["playinfo"])
                e["status"] = e["classification"]["status"]
            else:
                e.pop("classification", None)
                if e.get("status") not in ("api_error", "auth_error", "risk_error"):
                    e["status"] = "unavailable"
                e["error"] = "接口未返回有效播放信息；请重新探测。"
        created = manifest.get("created_at")
        if isinstance(created, str) and created:
            try:
                age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
                if age.total_seconds() > 3600:
                    print("提示：清单已超过一小时，CDN 地址可能过期；取流失败时重新导出。", file=sys.stderr)
            except (ValueError, TypeError):
                pass
        return selected, validate_user_agent(manifest.get("user_agent") or SAFARI_UA)
    if not wanted:
        raise ValueError("请提供 BV 号、--bv-file 或 --manifest。")
    entries = []
    with ApiClient(cookiefile=args.cookies, cookiesfrombrowser=args.cookies_from_browser,
                   cookie_env=args.cookie_env) as client:
        for bvid in wanted:
            entries.extend(client.fetch_video(bvid, all_pages=not args.first_page))
    return entries, SAFARI_UA


def public_entry(entry):
    result = {k: entry[k] for k in ("bvid", "title", "cid", "page", "part", "status", "error_code") if k in entry}
    if "playinfo" in entry:
        result["classification"] = classify_playinfo(entry["playinfo"])
    if entry.get("error"):
        result["error"] = "接口请求失败；检查登录、权限或网络。"
    return result


def stream_urls(stream):
    urls = [stream.get("baseUrl") or stream.get("base_url")]
    backup = stream.get("backupUrl") or stream.get("backup_url") or []
    urls += [backup] if isinstance(backup, str) else backup if isinstance(backup, list) else []
    valid = []
    for url in urls:
        if not _usable_url(url):
            continue
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
        except ValueError:
            continue
        if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
            continue
        if not any(host == domain or host.endswith("." + domain) for domain in
                   ("bilivideo.com", "bilivideo.cn", "hdslb.com", "akamaized.net")):
            continue
        if parsed.scheme == "http":
            url = "https:" + url[5:]
        if url not in valid:
            valid.append(url)
    if not valid:
        raise ValueError("没有有效的 Bilibili CDN 播放地址。")
    return sorted(valid, key=lambda u: not (urlsplit(u).hostname or "").endswith(".bilivideo.com"))


def reject_protected(data, video, audio):
    data = data.get("data", data)
    for obj in (data, video, audio):
        if (obj.get("is_drm") or obj.get("widevine_pssh") or obj.get("bilidrm_uri") or
                str(obj.get("drm_type", 0)) not in ("0", "", "None") or
                str(obj.get("drm_tech_type", 0)) not in ("0", "", "None")):
            raise ValueError("接口返回了受保护的流；此工具仅处理直接可下载的原始流。")
    if data.get("is_preview"):
        raise ValueError("接口仅返回预览，未作为完整视频下载。")


class _SafeDownloadLogger:
    def debug(self, message):
        pass  # yt-dlp debug messages may include complete signed CDN URLs.

    def warning(self, message):
        print("下载提示：" + safe_error(message), file=sys.stderr)

    def error(self, message):
        print("下载错误：" + safe_error(message), file=sys.stderr)


def _stream_identity(stream, kind):
    identity = {key: stream.get(key) for key in ("id", "codecs", "width", "height", "bandwidth", "size")}
    identity["kind"] = kind
    # Include stable resource paths without retaining hosts/signatures/account data.
    identity["resources"] = sorted({urlsplit(url).path for url in stream_urls(stream)})
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _validate_stream(path, kind):
    info = probe(path)
    tracks = [s for s in info["streams"] if s.get("codec_type") == kind]
    if len(tracks) != 1:
        raise ValueError(f"下载缓存没有恰好一个 {kind} 轨道。")
    if kind == "audio" and tracks[0].get("codec_name") != "eac3":
        raise ValueError("下载缓存不是原始 E-AC-3 音轨。")
    # ffprobe alone can accept a truncated mdat; read the full compressed stream.
    return payload_hash(path, "v" if kind == "video" else "a")


def download_stream(stream, path, kind, user_agent):
    from yt_dlp import YoutubeDL
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    user_agent = validate_user_agent(user_agent)
    urls = stream_urls(stream)
    identity = _stream_identity(stream, kind)
    record_path = path.with_suffix(path.suffix + ".download.json")
    record = json.loads(record_path.read_text()) if record_path.is_file() else None
    if record is not None and (not isinstance(record, dict) or record.get("schema") != "bili-stream-cache-v1"):
        raise ValueError("下载缓存记录损坏；请保留它并改用新的输出目录。")
    if record and record.get("identity") != identity:
        raise ValueError("续传缓存对应不同的视频流；请保留缓存并改用新的输出目录。")
    if path.exists():
        if not record:
            raise ValueError("已有下载缓存没有流身份记录，无法证明对应此次请求；请保留它并改用新的输出目录。")
        digest = _validate_stream(path, kind)
        if record and record.get("payload_sha256") and record["payload_sha256"] != digest:
            raise ValueError("下载缓存有效载荷已改变；请删除此缓存或改用新的输出目录。")
        save_json(record_path, {"schema": "bili-stream-cache-v1", "identity": identity, "payload_sha256": digest})
        return
    if path.with_suffix(path.suffix + ".part").exists() and not record:
        raise ValueError("旧续传片段没有流身份记录，无法安全续用；请保留它并改用新的输出目录。")
    save_json(record_path, {"schema": "bili-stream-cache-v1", "identity": identity})
    headers = {"User-Agent": user_agent, "Referer": "https://www.bilibili.com/"}
    print(f"正在下载 {kind}：{path.name}", flush=True)
    for index, url in enumerate(urls):
        fmt = {"format_id": kind, "url": url, "ext": path.suffix[1:], "http_headers": headers,
               "vcodec": stream.get("codecs", "unknown") if kind == "video" else "none",
               "acodec": "none" if kind == "video" else "ec-3"}
        options = {"format": kind, "outtmpl": str(path), "http_headers": headers,
                   "retries": 5, "socket_timeout": 25, "quiet": True, "noprogress": True,
                   "continuedl": True, "nopart": False, "logger": _SafeDownloadLogger(),
                   "postprocessors": []}
        # The built-in HTTP downloader resumes .part files and keeps diagnostics
        # inside the redacting logger; external downloader stderr bypasses it.
        try:
            with YoutubeDL(options) as downloader:
                downloader.process_ie_result({"id": path.stem, "title": path.stem, "formats": [fmt]}, download=True)
            if not path.is_file():
                raise ValueError("下载器没有生成完整媒体缓存。")
            digest = _validate_stream(path, kind)
        except Exception:
            if path.is_file():
                # A completed but invalid transfer cannot be resumed as trusted
                # bytes. Preserve it for diagnosis, then allow a clean fallback.
                path.rename(path.with_name(path.name + ".invalid-" + uuid.uuid4().hex[:12]))
            if index == len(urls) - 1:
                raise
            print("此 CDN 暂时失败，切换 B 站备用地址并继续下载。", flush=True)
        else:
            save_json(record_path, {"schema": "bili-stream-cache-v1", "identity": identity, "payload_sha256": digest})
            return


def verify_media(path, require_dv=True, native=True):
    info = probe(path)
    videos = [s for s in info["streams"] if s.get("codec_type") == "video"]
    audios = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise ValueError("需要恰好一个原始视频轨和一个 Atmos 音轨。")
    v, a = videos[0], audios[0]
    dovi = next((s for s in v.get("side_data_list", []) if s.get("side_data_type") == "DOVI configuration record"), None)
    if require_dv and (not dovi or not dovi.get("rpu_present_flag")):
        raise ValueError("视频缺少 Dolby Vision 配置/RPU。")
    if a.get("codec_name") != "eac3" or "Atmos" not in a.get("profile", ""):
        raise ValueError("音轨未被识别为 Dolby Atmos。")
    # Probe elementary E-AC-3 independently of the MP4 dec3 declaration.
    with tempfile.TemporaryDirectory(prefix="bili-atmos-") as tmp:
        raw = Path(tmp) / "sample.eac3"
        run_media_tool(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(path), "-map", "0:a:0",
                        "-t", "10", "-c:a", "copy", "-f", "eac3", str(raw)])
        raw_audio = next((s for s in probe(raw)["streams"] if s.get("codec_type") == "audio"), {})
    if "Atmos" not in raw_audio.get("profile", ""):
        raise ValueError("原始 E-AC-3 帧头未证明 Atmos，容器标签不足以通过验证。")
    container = inspect_container(path)
    checks = container["apple_container_checks"]
    if not checks["ec3_joc_signaling_valid"]:
        raise ValueError("MP4 dec3 缺少有效 JOC 扩展；Apple 可能只识别普通 5.1。")
    if require_dv and not checks["preferred_dv_hevc_sample_entry"]:
        raise ValueError("DV 视频不是 Apple 优先使用的 HEVC 样本封装。")
    rpu = None
    warnings = list(container.get("warnings", []))
    if dovi and dovi.get("rpu_present_flag"):
        if v.get("codec_name") != "hevc":
            if require_dv:
                raise ValueError("当前只支持完整验证 HEVC Dolby Vision RPU。")
            warnings.append("此 Dolby Vision 视频编码的 RPU 尚未完成码流验证。")
        else:
            rpu = scan_hevc_rpu(path)
            if not rpu["verified"]:
                raise ValueError("视频未提供有效的 Dolby RPU 码流或 RPU CRC 校验失败；容器标签不足以通过验证。")
        profile = (dovi.get("dv_profile"), dovi.get("dv_bl_signal_compatibility_id"))
        if profile != (8, 4) and profile[0] != 5:
            warnings.append(f"Dolby Vision Profile {profile[0]} / compatibility {profile[1]} 不在此工具评估的 "
                            "Apple 本地回放范围（Profile 5 / 8.4）；保留了原码流，但不能承诺设备的 DV 回放。")
    dv_verified = bool(rpu and rpu["verified"] and dovi and checks["preferred_dv_hevc_sample_entry"])
    hashes = {"video": rpu["video_payload_sha256"] if rpu else payload_hash(path, "v"),
              "audio": payload_hash(path, "a")}
    result = {"path": str(path), "status": "verified_dolby_vision_atmos" if dv_verified else "verified_atmos",
              "size_bytes": path.stat().st_size, "ffprobe": info, "dovi": dovi,
              "payload_sha256": hashes, "full_payload_read_verified": True,
              "raw_eac3_atmos_verified": True, "raw_eac3_profile": raw_audio.get("profile"),
              "raw_eac3_probe_scope": "first_10_seconds", "video_rpu": rpu, "warnings": warnings,
              "container": container, "spatial_audio_output_verified": False,
              "playback_note": "请在 QuickTime/文件 App 回放时通过控制中心确认 Dolby Atmos 与固定/头部跟踪。"}
    inspector = next((p for p in (ROOT / "build" / "apple_media_inspect", ROOT / "apple_media_inspect")
                      if p.is_file() and os.access(p, os.X_OK)), None)
    if native and sys.platform == "darwin" and inspector:
        try:
            native_result = run_media_tool([str(inspector), str(path)], timeout=60)
            result["apple_native"] = json.loads(native_result.stdout)
        except (RuntimeError, ValueError) as error:
            result["apple_native"] = {"status": "unavailable", "note": safe_error(error)}
    return result


def _previous_payload_reference(output, entry):
    report = output.with_suffix(".verification.json")
    if not report.is_file():
        return None
    try:
        previous = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # An incomplete old report never substitutes for revalidation.
    if not isinstance(previous, dict):
        return None
    source = previous.get("source", {})
    hashes = previous.get("payload_sha256", {})
    if (not previous.get("payloads_unchanged") or not isinstance(source, dict) or
            not isinstance(hashes, dict) or
            any(str(source.get(k)) != str(entry.get(k)) for k in ("bvid", "cid", "page")) or
            any(not re.fullmatch(r"[0-9a-f]{64}", str(hashes.get(k, ""))) for k in ("video", "audio"))):
        return None
    return hashes


def _cached_payload_reference(path, stream, kind):
    """Only a cache anchored to this selected CDN resource can prove identity."""
    record_path = path.with_suffix(path.suffix + ".download.json")
    if not path.is_file() or not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != "bili-stream-cache-v1":
        return None
    if record.get("identity") != _stream_identity(stream, kind):
        raise ValueError("保留的原始缓存与当前选择的流身份不同，不能作为无损复核证据。")
    digest = _validate_stream(path, kind)
    if record.get("payload_sha256") and record["payload_sha256"] != digest:
        raise ValueError("保留的原始缓存有效载荷已改变，未通过无损复核。")
    return digest


def download_entry(entry, directory, mode, user_agent, keep_streams, native, output_path=None):
    page, cid = positive_int(entry.get("page"), "page"), positive_int(entry.get("cid"), "cid")
    bvid = entry.get("bvid", "")
    if parse_bvids([bvid]) != [bvid]:
        raise ValueError("无效 BV 号。")
    user_agent = validate_user_agent(user_agent)
    video, audio = select_streams(entry["playinfo"], mode)
    reject_protected(entry["playinfo"], video, audio)
    name = f"{bvid}_P{page:02d}_{cid}"
    directory = Path(directory)
    streams = directory / ".streams"
    streams.mkdir(parents=True, exist_ok=True)
    vp, ap = streams / (name + ".video.mp4"), streams / (name + ".audio.m4a")
    output = Path(output_path) if output_path else directory / (
        name + ("_DV_Atmos.mp4" if int(video["id"]) == 126 else "_Atmos.mp4"))
    if output.suffix.lower() != ".mp4":
        raise ValueError("目标文件必须使用 .mp4 扩展名。")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        result = verify_media(output, require_dv=mode == "dual", native=native)
        result["existing_file_verified"] = True
        cached = {"video": _cached_payload_reference(vp, video, "video"),
                  "audio": _cached_payload_reference(ap, audio, "audio")}
        reference = cached if all(cached.values()) else _previous_payload_reference(output, entry)
        current = result.get("payload_sha256") or {
            "video": payload_hash(output, "v"), "audio": payload_hash(output, "a")}
        if reference:
            if any(reference[k] != current[k] for k in ("video", "audio")):
                raise ValueError("已有文件与原始有效载荷不一致；未通过无损复核。")
            result.update(payload_sha256=current, payloads_unchanged=True)
        else:
            result["payloads_unchanged"] = False
            result.setdefault("warnings", []).append("已有文件已重新验证；没有对应原始流或有效旧记录，不能证明与 CDN 原始流一致。")
        result["source"] = public_entry(entry)
        save_json(output.with_suffix(".verification.json"), result)
        return result
    download_stream(video, vp, "video", user_agent)
    download_stream(audio, ap, "audio", user_agent)
    if "Atmos" not in next(s for s in probe(ap)["streams"] if s.get("codec_type") == "audio").get("profile", ""):
        raise ValueError("原始音轨没有 Atmos 标志，停止合并。")
    fd, temporary = tempfile.mkstemp(prefix="." + output.stem + ".", suffix=".building.mp4", dir=output.parent)
    os.close(fd)
    temp = Path(temporary)
    try:
        command = ["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-y", "-i", str(vp), "-i", str(ap),
                   "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-tag:a", "ec-3",
                   "-strict", "unofficial", "-movflags", "+faststart+write_colr"]
        if video.get("codecs", "").startswith(("hvc", "hev", "dvh", "dvhe")):
            command += ["-tag:v", "hvc1"]
        run_media_tool(command + [str(temp)], timeout=600)
        result = verify_media(temp, require_dv=mode == "dual", native=native)
        hashes = {"video": payload_hash(vp, "v"), "audio": payload_hash(ap, "a")}
        merged = result.get("payload_sha256") or {"video": payload_hash(temp, "v"), "audio": payload_hash(temp, "a")}
        if hashes != merged:
            raise ValueError("合并前后有效载荷不一致；未通过无损验证。")
        # Same-directory hardlink gives atomic, no-clobber publication. A second
        # writer cannot replace a file that appeared after the initial check.
        os.link(temp, output)
        result.update({"path": str(output), "payload_sha256": hashes, "payloads_unchanged": True,
                       "source": public_entry(entry)})
        result["ffprobe"].setdefault("format", {})["filename"] = str(output)
        result["container"]["path"] = str(output)
        if "apple_native" in result:
            result["apple_native"]["path"] = str(output)
        save_json(output.with_suffix(".verification.json"), result)
    finally:
        temp.unlink(missing_ok=True)
    if not keep_streams:
        for path in (vp, ap):
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".download.json").unlink(missing_ok=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "probe", "download"):
        p = sub.add_parser(command)
        p.add_argument("bvids", nargs="*")
        p.add_argument("--bv-file", type=Path)
        p.add_argument("--first-page", action="store_true", help="只处理 P1；默认逐 CID 处理所有分 P")
        if command == "prepare":
            p.add_argument("--js", type=Path, default=Path("safari_batch.generated.js"))
        else:
            p.add_argument("--manifest", type=Path)
            auth = p.add_mutually_exclusive_group()
            auth.add_argument("--cookies", type=Path, help="已有 Netscape Cookie 文件")
            auth.add_argument("--cookies-from-browser", choices=("safari", "chrome", "firefox", "edge"))
            auth.add_argument("--cookie-env", help="Cookie 请求头所在的环境变量名称，不在命令行传凭据")
            p.add_argument("--report", type=Path, default=Path(f"batch-{command}.json"))
        if command == "download":
            p.add_argument("--output-dir", type=Path, default=Path("批量下载"))
            p.add_argument("--mode", choices=("dual", "atmos"), default="dual")
            p.add_argument("--keep-streams", action="store_true")
            p.add_argument("--no-native", action="store_true")
    p = sub.add_parser("verify")
    p.add_argument("paths", type=Path, nargs="+")
    p.add_argument("--allow-non-dv", action="store_true")
    p.add_argument("--no-native", action="store_true")
    p.add_argument("--report", type=Path, default=Path("local-verification.json"))
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            bvids = bvids_from(args)
            if not bvids:
                raise ValueError("请提供 BV 号或 --bv-file。")
            args.js.write_text(render_safari_export(bvids, all_pages=not args.first_page), encoding="utf-8")
            print(f"已生成：{args.js.resolve()}\n在已登录 Safari 的 B 站页面打开开发 → JavaScript 控制台，粘贴此文件并执行。\n将下载得到的 bilibili-dolby-manifest.json 用于 probe/download --manifest。")
            return 0
        if args.command == "verify":
            results = []
            for path in args.paths:
                try:
                    results.append(verify_media(path.resolve(), not args.allow_non_dv, not args.no_native))
                except Exception as error:
                    results.append({"path": str(path), "status": "failed", "error": safe_error(error)})
        else:
            entries, user_agent = load_entries(args)
            results = []
            for entry in entries:
                record = public_entry(entry)
                if args.command == "download" and "playinfo" in entry:
                    try:
                        v, a = select_streams(entry["playinfo"], args.mode)
                        reject_protected(entry["playinfo"], v, a)
                    except ValueError as error:
                        record.update(status="skipped", reason=safe_error(error))
                        results.append(record)
                        print(f"{record.get('bvid', '')} P{record.get('page', '?')}: skipped", flush=True)
                        continue
                    try:
                        record["download"] = download_entry(entry, args.output_dir.resolve(), args.mode,
                                                            user_agent, args.keep_streams, not args.no_native)
                        record["status"] = record["download"]["status"]
                    except Exception as error:
                        record.update(status="failed", error=safe_error(error))
                results.append(record)
                print(f"{record.get('bvid', '')} P{record.get('page', '?')}: {record['status']}", flush=True)
        save_json(args.report, {"created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "entries": results})
        print(f"结果已保存：{args.report.resolve()}")
        return 1 if any(r["status"] in ("failed", "api_error", "auth_error", "risk_error") for r in results) else 0
    except Exception as error:
        print("错误：" + safe_error(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
