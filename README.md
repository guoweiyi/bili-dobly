# bili-dobly

按 **BV 号 → 分 P/CID → 原始 DASH 轨道** 批量下载 Bilibili Dolby Vision 与 Dolby Atmos，生成适合 Apple 原生播放器回看的 MP4。

默认严格要求 **DV 视频 + E-AC-3 JOC/Atmos 音轨**，两者缺一就跳过该分 P。合并使用 `-c copy`，并核对合并前后的整个视频、音频有效载荷；不会把普通音轨转换成 Atmos。

## 教程导航

- [完整使用教程：安装、Safari 导出、批量下载、验证和更新](docs/tutorial.md)
- [macOS / iPhone / iPad 回看、AirPods 空间音频及亮度排查](docs/apple-playback.md)
- [API 标志、验证证据与限制](docs/api-and-verification.md)
- [常见问题与错误处理](docs/troubleshooting.md)

## 快速开始（Mac）

需要 Python **3.10+**、FFmpeg / FFprobe，以及已登录 B 站的 Safari。以下命令在 Terminal 执行；若已安装依赖，跳过对应安装步骤。Homebrew 安装方法见[官方说明](https://brew.sh/)。

```sh
brew install python ffmpeg
git clone https://github.com/guoweiyi/bili-dobly.git
cd bili-dobly
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python doctor.py
```

生成浏览器导出脚本：

```sh
python bili_batch.py prepare BV1RUfgBiEiY
pbcopy < safari_batch.generated.js
```

在 **已登录的 Safari `https://www.bilibili.com/` 顶层页面**打开「开发 → 显示 JavaScript 控制台」，粘贴并执行。若没有「开发」菜单，先在 Safari「设置 → 高级」启用网页开发者功能。脚本会下载 `bilibili-dolby-manifest.json`；**不会读取或导出登录 Cookie**。

回到 Terminal，先探测，再下载：

```sh
python bili_batch.py probe --manifest "$HOME/Downloads/bilibili-dolby-manifest.json"
python bili_batch.py download --manifest "$HOME/Downloads/bilibili-dolby-manifest.json"
```

成品保存在 `批量下载/`，例如：

```text
BV1RUfgBiEiY_P01_36174038871_DV_Atmos.mp4
BV1RUfgBiEiY_P01_36174038871_DV_Atmos.verification.json
```

示例 BV 的可用性以当前账号和 API 返回为准。清单含会过期的播放地址；403、过期或空候选时按[故障排查](docs/troubleshooting.md)重新导出。

## 常用命令

先复制 `bvs.example.txt` 为自己的 `bvs.txt`，一行一个 BV 或完整视频链接：

```sh
cp bvs.example.txt bvs.txt
python bili_batch.py prepare --bv-file bvs.txt
pbcopy < safari_batch.generated.js
# 在 Safari 执行后：
python bili_batch.py download --manifest "$HOME/Downloads/bilibili-dolby-manifest.json" --bv-file bvs.txt
```

| 操作 | 命令或选项 |
| --- | --- |
| 默认逐 P 下载双杜比 | `download --manifest 清单.json` |
| 只处理 P1 | `prepare ... --first-page`；下载时也可加 `--first-page` |
| 明确接受 Atmos + 非 DV 视频 | `download --manifest 清单.json --mode atmos` |
| 保留原始音视频轨道 | `download ... --keep-streams` |
| 指定成品目录 | `download ... --output-dir downloads` |
| 指定批次报告 | `probe/download ... --report reports/batch.json` |
| 验证已有双杜比文件 | `verify 文件.mp4` |
| 验证 Atmos + 普通视频 | `verify 文件.mp4 --allow-non-dv` |
| 跳过 Apple 原生检查 | `download/verify ... --no-native` |
| 依赖检查 | `python doctor.py` 或 `python doctor.py --json` |

`--mode atmos` 仍下载视频与真正的 Atmos 音轨，**不是仅音频模式**。单独的 Dolby 音效、E-AC-3 编码名、音频 ID 30250 都不足以通过 Atmos 验证。

## Apple 原生工具（可选）

原生工具要求 **macOS 13+** 和 macOS 13+ SDK。在安装 Xcode 或 Xcode Command Line Tools 后，运行：

```sh
bash scripts/build_apple_tools.sh
build/apple_media_inspect '批量下载/你的文件.mp4'
```

构建结果：`build/apple_media_inspect`、`build/apple_display_inspect` 和 `build/DolbyVisionCompare.app`。批量入口自动发现原生媒体检查器。DV 对照 App 比较逐帧 HDR 元数据开启/关闭，默认暂停、静音；[播放教程](docs/apple-playback.md)解释如何判断 DV 与空间音频。

## 验证能证明什么

工具区分 **API 候选、实际 JOC/RPU 码流、MP4 信令、Apple 原生识别、真实设备输出**。成品验证通过，不等同于当前 AirPods 已启用 Atmos 或头部跟踪，也不能认证创作者做过原生对象混音。

视频来源可能缺少 Apple DV 8.4 工作流要求的 `amve` 环境亮度字段。工具保留源数据并报告缺失；不会杜撰亮度参数。QuickTime 偏暗或 IINA 更亮，都不能单凭观感证明 DV 激活与否。详细证据见[技术说明](docs/api-and-verification.md)和[Apple 回看教程](docs/apple-playback.md)。

## 开发与测试

```sh
python -B -m unittest discover -v
python bili_batch.py --help
python bili_batch.py download --help
```

测试以本地合成容器、模拟接口和短媒体为主，不需要登录 B 站。可选真实下载样本测试在样本不存在时跳过。GitHub Actions 同时测试多个 Python 版本，并在 Mac 构建 Swift 工具。

项目固定 `yt-dlp==2026.8.19`，直接 API 路径复用该版本私有 WBI 辅助实现；升级依赖前需复核接口与测试。Safari 导出流程优先用于 macOS 会话读取受保护的环境。

源码入口：[bili_batch.py](bili_batch.py)、[bilibili_api.py](bilibili_api.py)、[dolby_container.py](dolby_container.py)、[Safari 模板](safari_export.template.js)。清单、Cookie、本地诊断报告、下载媒体与编译产物均默认由 `.gitignore` 排除。
