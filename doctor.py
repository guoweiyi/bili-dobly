#!/usr/bin/env python3
"""检查本地依赖；不连接 B 站、不读取 Cookie、不播放媒体。"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUPPORTED_YT_DLP = "2026.8.19"


def tool_output(executable, *args):
    result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(f"工具执行失败，退出码 {result.returncode}")
    return result.stdout + result.stderr


def check_environment():
    checks = []

    def add(name, status, detail, required=False):
        checks.append(dict(name=name, status=status, detail=detail, required=required))

    supported = sys.version_info >= (3, 10)
    add("Python", "ok" if supported else "error", platform.python_version() + "；要求 3.10+", True)
    try:
        version = importlib.metadata.version("yt-dlp")
        add("yt-dlp", "ok" if version == SUPPORTED_YT_DLP else "warning",
            f"{version}；本项目固定测试版本 {SUPPORTED_YT_DLP}，其他版本需重新核对私有 API", True)
    except importlib.metadata.PackageNotFoundError:
        add("yt-dlp", "error", "未安装；运行 python -m pip install -r requirements.txt", True)
    binaries = {}
    for name in ("ffmpeg", "ffprobe"):
        binary = shutil.which(name)
        if not binary:
            add(name, "error", "PATH 中未找到；安装 FFmpeg 后重新打开终端", True)
            continue
        try:
            line = tool_output(binary, "-version").splitlines()[0]
            add(name, "ok", line, True)
            binaries[name] = binary
        except (OSError, subprocess.TimeoutExpired, RuntimeError, IndexError):
            add(name, "error", "找到工具但不能正常执行", True)
    if "ffmpeg" in binaries:
        binary = binaries["ffmpeg"]
        for name, flag, required_names in (
            ("FFmpeg muxers", "-muxers", ("mp4", "eac3", "hevc", "h264")),
            ("FFmpeg bitstream filters", "-bsfs", ("hevc_mp4toannexb", "h264_mp4toannexb")),
            ("FFmpeg decoder", "-decoders", ("eac3",)),
        ):
            try:
                output = tool_output(binary, "-hide_banner", flag)
                missing = [item for item in required_names if not re.search(r"\b" + re.escape(item) + r"\b", output)]
                add(name, "error" if missing else "ok",
                    "缺少 " + ", ".join(missing) if missing else "具备必需能力", True)
            except (OSError, subprocess.TimeoutExpired, RuntimeError):
                add(name, "error", "无法查询工具能力", True)
        add("Dolby 封装", "warning", "环境检查不能证明 dec3/JOC 与 DV 已正确封装；成品仍须通过 download/verify")
    if platform.system() == "Darwin":
        add("Apple 工具编译器（可选）", "ok" if shutil.which("xcrun") else "optional",
            "可用" if shutil.which("xcrun") else "安装 Xcode Command Line Tools 后可编译原生工具")
        inspector = next((p for p in (ROOT / "build/apple_media_inspect", ROOT / "apple_media_inspect")
                          if p.is_file()), None)
        add("Apple 原生检查器（可选）", "ok" if inspector else "optional",
            "已构建" if inspector else "运行 bash scripts/build_apple_tools.sh；未构建时仍可进行码流与容器验证")
    else:
        add("Apple 播放验证", "optional", "当前系统只执行通用下载与验证；Apple 原生工具需要 macOS")
    ready = not any(c["status"] == "error" and c["required"] for c in checks)
    return {"ready": ready, "platform": platform.system(), "checks": checks,
            "scope": "依赖检查通过不等同于 Dolby 渲染或实际空间音频输出已通过。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    result = check_environment()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for check in result["checks"]:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
        print("可以继续下载与验证。" if result["ready"] else "请先解决 error 项。")
        print(result["scope"])
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
