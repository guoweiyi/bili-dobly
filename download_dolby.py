#!/usr/bin/env python3
"""Download original Dolby streams from page source saved by logged-in Safari."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from bilibili_api import SAFARI_UA


class MediaToolError(RuntimeError):
    """Actionable subprocess failure without credentials or signed URLs."""


def safe_error(error):
    text = str(error)
    text = re.sub(r"https?://[^\s'\"<>]+", "[播放地址]", text)
    text = re.sub(r"(?im)(?:cookie|set-cookie|authorization)[\"']?\s*[:=][^\r\n]*",
                  "[登录信息已省略]", text)
    text = re.sub(r"(?i)(?:SESSDATA|bili_jct|access_token|upsig|signature)[\"']?\s*[=:]\s*[\"']?[^\s,;\"']+",
                  "[敏感参数已省略]", text)
    return text[:8192]


_TOOL_OUTPUT_LIMIT = 8 * 1024 * 1024


def run_media_tool(command, *, timeout=120):
    """Bound in-memory tool output and preserve redacted failure diagnostics."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(command, check=True, stdout=output, stderr=errors, timeout=timeout)
        except FileNotFoundError as error:
            raise MediaToolError(f"找不到 {Path(command[0]).name}；请先运行 python doctor.py 检查依赖。") from error
        except subprocess.TimeoutExpired as error:
            raise MediaToolError(f"{Path(command[0]).name} 超过 {timeout} 秒，已终止；请检查本地媒体文件。") from error
        except subprocess.CalledProcessError as error:
            errors.seek(0)
            detail = errors.read(8192).decode(errors="replace")
            # Also accept stderr supplied directly by callers/test doubles.
            if not detail and error.stderr:
                detail = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr
            raise MediaToolError(f"{Path(command[0]).name} 失败（退出码 {error.returncode}）："
                                 + safe_error(detail or "未提供错误详情")) from error
        output.seek(0)
        stdout = output.read(_TOOL_OUTPUT_LIMIT + 1)
        if len(stdout) > _TOOL_OUTPUT_LIMIT:
            raise MediaToolError(f"{Path(command[0]).name} 输出超过探测内存上限，未接受不完整结果。")
        errors.seek(0)
        return subprocess.CompletedProcess(command, result.returncode, stdout.decode(errors="replace"),
                                           safe_error(errors.read(8192).decode(errors="replace")))


@contextlib.contextmanager
def _media_pipe(command, timeout=1800):
    """Drain binary stdout; disk-backed stderr prevents pipe deadlocks."""
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        except FileNotFoundError as error:
            raise MediaToolError(f"找不到 {Path(command[0]).name}；请先运行 python doctor.py 检查依赖。") from error
        timed_out = threading.Event()
        def terminate():
            timed_out.set()
            process.kill()
        timer = threading.Timer(timeout, terminate)
        timer.daemon = True
        timer.start()
        try:
            yield process.stdout
            status = process.wait()
            if timed_out.is_set():
                raise MediaToolError(f"{Path(command[0]).name} 媒体验证超过 {timeout} 秒，已终止。")
            if status:
                errors.seek(0)
                detail = safe_error(errors.read(8192).decode(errors="replace"))
                raise MediaToolError(f"{Path(command[0]).name} 失败（退出码 {status}）：{detail}")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


def page_variable(source, name):
    match = re.search(r"window\." + re.escape(name) + r"\s*=\s*", source)
    if not match:
        raise ValueError(f"页面源码中找不到 {name}；请用已登录的 Safari 保存页面源码。")
    return json.JSONDecoder().raw_decode(source[match.end():])[0]


def probe(path):
    result = run_media_tool(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    try:
        info = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise MediaToolError("ffprobe 未返回有效 JSON，媒体探测失败。") from error
    if not isinstance(info, dict) or not isinstance(info.get("streams"), list):
        raise MediaToolError("ffprobe 未返回有效轨道信息。")
    return info


def stream_url(stream):
    # Legacy import retained; use the batch workflow's CDN validation/fallback.
    from bili_batch import stream_urls
    return stream_urls(stream)[0]


def payload_hash(path, kind):
    if kind not in ("v", "a"):
        raise ValueError("有效载荷类型必须为 v 或 a。")
    command = ["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(path),
               "-map", f"0:{kind}:0", "-c", "copy"]
    if kind == "v":
        codec = next((s.get("codec_name") for s in probe(path)["streams"] if s.get("codec_type") == "video"), None)
        if codec in ("hevc", "h264"):
            command += ["-bsf:v", f"{codec}_mp4toannexb", "-f", codec]
        elif codec == "av1":
            command += ["-f", "obu"]
        else:
            raise ValueError(f"不支持验证此视频编码：{codec}")
    else:
        command += ["-f", "eac3"]
    digest = hashlib.sha256()
    with _media_pipe(command + ["pipe:1"]) as output:
        while chunk := output.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


_START_CODE = re.compile(b"\x00\x00(?:\x00)?\x01")
_MAX_NAL = 64 * 1024 * 1024


def _annexb_nals(output, digest=None):
    """Yield bounded complete NALs even when start codes cross chunk boundaries."""
    buffer = b""
    while chunk := output.read(64 * 1024):
        if digest is not None:
            digest.update(chunk)
        buffer += chunk
        starts = list(_START_CODE.finditer(buffer))
        if len(starts) > 1:
            for left, right in zip(starts, starts[1:]):
                nal = buffer[left.end():right.start()]
                if len(nal) > _MAX_NAL:
                    raise ValueError("HEVC NAL 超过验证内存上限。")
                yield nal
            buffer = buffer[starts[-1].start():]
        if len(buffer) > _MAX_NAL + 4:
            raise ValueError("HEVC NAL 超过验证内存上限。")
    starts = list(_START_CODE.finditer(buffer))
    if starts:
        nal = buffer[starts[-1].end():]
        if len(nal) > _MAX_NAL:
            raise ValueError("HEVC NAL 超过验证内存上限。")
        yield nal
    elif buffer:
        raise ValueError("HEVC 码流缺少 Annex B 起始码。")


def _unescape_rbsp(data):
    return re.sub(b"\x00\x00\x03(?=[\x00-\x03])", b"\x00\x00", data)


def _crc_table():
    table = []
    for value in range(256):
        value <<= 24
        for _ in range(8):
            value = ((value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)) & 0xFFFFFFFF
        table.append(value)
    return table


_CRC_TABLE = _crc_table()


def _rpu_crc_valid(payload):
    # Same non-reflected IEEE CRC convention as FFmpeg dovi_rpudec.c:
    # start at all ones, cover RPU bytes and stored CRC, exclude prefix/0x80.
    # https://github.com/FFmpeg/FFmpeg/blob/master/libavcodec/dovi_rpudec.c
    crc = 0xFFFFFFFF
    for value in payload:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[(crc >> 24) ^ value]
    return crc == 0


def scan_hevc_rpu(path):
    """Inspect actual RPU NAL payloads across the entire HEVC track.

    Validates HEVC headers, RPU prefix/type, terminator and CRC. It does not
    decode mapping/trim metadata or certify mastering, rendering or playback.
    """
    command = ["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(path),
               "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "pipe:1"]
    count, vcl_count, invalid = 0, 0, 0
    digest, video_digest = hashlib.sha256(), hashlib.sha256()
    with _media_pipe(command) as output:
        for nal in _annexb_nals(output, video_digest):
            if (len(nal) < 2 or nal[0] & 0x80 or not nal[1] & 7 or
                    ((nal[0] & 1) << 5 | nal[1] >> 3) == 63):
                raise ValueError("HEVC NAL 头损坏，未通过完整码流检查。")
            kind = nal[0] >> 1 & 63
            if kind <= 31:
                vcl_count += 1
            if kind != 62:
                continue
            # HEVC RPU NAL payload: 0x19 prefix, 6-bit RPU type 2,
            # mapping bytes, CRC32, 0x80 terminator and optional zero padding.
            rpu = _unescape_rbsp(nal[2:]).rstrip(b"\0")
            if (len(rpu) < 10 or rpu[0] != 25 or rpu[1] >> 2 != 2 or
                    rpu[-1] != 0x80 or not _rpu_crc_valid(rpu[1:-1])):
                invalid += 1
                continue
            count += 1
            digest.update(len(rpu).to_bytes(4, "big") + rpu)
    return {"rpu_nal_count": count, "invalid_rpu_nal_count": invalid,
            "vcl_nal_count": vcl_count, "rpu_payload_sha256": digest.hexdigest(),
            "video_payload_sha256": video_digest.hexdigest(),
            "rpu_crc_checked": True, "scan_scope": "entire_video_track",
            "verification_level": "nal_presence_header_crc",
            "rpu_semantic_decoding_performed": False,
            "verified": count > 0 and invalid == 0 and vcl_count > 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Safari 保存的页面源码 HTML")
    parser.add_argument("--output", type=Path, help="目标 MP4 文件路径")
    args = parser.parse_args(argv)
    try:
        source = args.source.read_text(encoding="utf-8")
        play = page_variable(source, "__playinfo__")
        state = page_variable(source, "__INITIAL_STATE__")
        meta = state.get("videoData", {})
        from bilibili_api import parse_bvids
        bvid = meta.get("bvid", "")
        if parse_bvids([bvid]) != [bvid]:
            raise ValueError("页面没有有效 BV 号。")
        cid = meta.get("cid")
        page = next((p.get("page", 1) for p in meta.get("pages", []) if p.get("cid") == cid), 1)
        entry = {"bvid": bvid, "title": meta.get("title", bvid), "cid": cid,
                 "page": page, "part": "", "playinfo": play}
        output = (args.output or Path(f"{bvid}_DolbyVision_Atmos.mp4")).resolve()
        if output.suffix.lower() != ".mp4":
            raise ValueError("目标文件必须使用 .mp4 扩展名。")
        from bili_batch import download_entry
        result = download_entry(entry, output.parent, "dual", SAFARI_UA, True, True, output_path=output)
        print(f"已保存：{result['path']}\n验证记录：{output.with_suffix('.verification.json')}")
        return 0
    except Exception as error:
        import sys
        print("错误：" + safe_error(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
