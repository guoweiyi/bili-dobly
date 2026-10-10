# 完整使用教程

这份教程从一个新克隆的项目开始，完成一次 Safari 登录会话导出、BV 批量探测、下载和本地验证。主要操作环境是 macOS；Python 下载与容器检查可以在其他具备依赖的系统使用，Swift 工具与 Safari 导出需要 Mac。

## 1. 安装与检查

先在 Terminal 检查：

```sh
python3 --version
ffmpeg -version
ffprobe -version
```

Python 至少 3.10。若缺少依赖，使用已安装的 Homebrew：

```sh
brew install python ffmpeg
```

Homebrew 本身从[官方页面](https://brew.sh/)安装。然后克隆源码并建立虚拟环境：

```sh
git clone https://github.com/guoweiyi/bili-dobly.git
cd bili-dobly
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python doctor.py
```

依赖检查的 `error` 项会阻止就绪状态；`optional` 表示可选工具尚未安装。Dolby 封装提示会要求后续验证成品，即使环境检查通过，也不能提前认定文件已包含 DV/Atmos。

以后每次新开 Terminal：

```sh
cd /你保存源码的目录/bili-dobly
source .venv/bin/activate
```

文中的 `python` 都是在这个虚拟环境中运行。也可以直接用 `.venv/bin/python` 代替。

## 2. 准备 BV 列表

少量视频直接写在命令行：

```sh
python bili_batch.py prepare BV1RUfgBiEiY BV1fszcBaEKQ
```

较多视频使用文本文件：

```sh
cp bvs.example.txt bvs.txt
```

用文本编辑器将 `bvs.txt` 改成自己的列表，内容例如：

```text
# 注释可以单独一行；不要修改 BV 大小写
BV1RUfgBiEiY
https://www.bilibili.com/video/BV1fszcBaEKQ/
```

空行和注释会忽略，重复 BV 会去重。默认逐分 P 查询 CID，不能把一个 BV 的 P1 杜比能力推广到所有分 P。

```sh
python bili_batch.py prepare --bv-file bvs.txt
# 如果只需要 P1：
python bili_batch.py prepare --bv-file bvs.txt --first-page
```

成功后得到 `safari_batch.generated.js`。`--js` 可以指定另一个保存位置。

## 3. 从已登录 Safari 导出清单

1. 在 Safari 打开 `https://www.bilibili.com/` 或这个域名下的视频页面，确认是自己的正常登录会话。
2. Safari「设置 → 高级」启用网页开发者功能。不同系统版本的选项可能写作“显示网页开发者功能”或“在菜单栏中显示开发菜单”。
3. 在 Terminal 执行 `pbcopy < safari_batch.generated.js`。
4. 回 Safari 选择「开发 → 显示 JavaScript 控制台」，粘贴脚本并按回车。
5. 等待脚本逐 BV/分 P 请求，导出 `bilibili-dolby-manifest.json`。Safari 询问此站点是否允许下载时，允许这次下载。

请在 **HTTPS 的 `www.bilibili.com` 顶层页面**执行，并选择该页面的控制台上下文。脚本会拒绝其他子域名或 HTTP 页面；不要将它粘贴到扩展或 iframe 的控制台上下文。它依靠当前网站自己的会话发送 API 请求，不读取 `document.cookie`，也不导出 Cookie；Python 下载 CDN 文件时不继承浏览器登录凭据。

脚本会记录逐条成功/失败，遇到一条权限或网络错误会继续处理其他视频。大量 BV 需要等待；接口风控时减少列表长度，稍后或重新登录再试。

浏览器下载的 JSON 通常在 `~/Downloads`。重复导出时 Safari 可能加文件名后缀，后续命令应使用实际下载文件名。**清单含临时签名的 CDN URL，请留在本机。** 地址过期后重新导出即可。

## 4. 先探测

```sh
python bili_batch.py probe \
  --manifest "$HOME/Downloads/bilibili-dolby-manifest.json" \
  --report reports/probe.json
```

Terminal 显示逐 P 状态，完整候选信息写入 `reports/probe.json`。探测只看接口，尚未证明实际下载码流。

| 常见状态 | 含义与下一步 |
| --- | --- |
| `dual_candidate` | 当前 CID 可获取 DV + Atmos 候选；可以进入下载验证 |
| `atmos_only_candidate` | 当前有 Atmos 候选，当前响应无可获取 DV；默认双杜比模式跳过 |
| `non_atmos` | 有效响应本次没有 Atmos 标志；不会用普通 Dolby 音效替代 |
| `unavailable` | 元数据、地址、保护/预览或有效期等条件不满足；看 `classification.reason` 或 `error` |
| `auth_error` | 登录、会员、地区或视频访问权限问题 |
| `risk_error` | 接口风控；稍后重试或使用新 Safari 清单 |
| `api_error` | 接口/网络错误，不能据此判断视频本身没有 Atmos |

## 5. 下载双杜比

```sh
python bili_batch.py download \
  --manifest "$HOME/Downloads/bilibili-dolby-manifest.json" \
  --output-dir downloads \
  --keep-streams \
  --report reports/download.json
```

默认 `--mode dual`。下载、封装、原始码流和容器验证全部通过后，才发布最终 MP4。文件名包含 BV、分 P 与 CID；标题不参与文件路径，避免重名和特殊字符问题。

默认不保留独立原始轨道；示例加入 `--keep-streams` 后会保留在 `downloads/.streams/`，方便进一步分析或核对。每个成品旁的 `.verification.json` 记录验证层级、哈希与原生识别结果。

仅下载列表中的一部分、或只处理 P1：

```sh
python bili_batch.py download BV1RUfgBiEiY \
  --manifest "$HOME/Downloads/bilibili-dolby-manifest.json" \
  --first-page --output-dir downloads
```

清单必须包含请求的 BV；过滤不会从清单外自动获取其他视频。

如果你明确接受 Atmos + 普通视频：

```sh
python bili_batch.py download BV1fszcBaEKQ \
  --manifest "$HOME/Downloads/bilibili-dolby-manifest.json" \
  --mode atmos --output-dir downloads
```

该模式仍要求真实 Atmos 音轨，视频优先选 DV，其次选 Apple 兼容候选。不会将没有 DV 的成品命名为双杜比。

## 6. 中断、重试与完成判断

网络中断后使用同一命令重新运行。使用 yt-dlp 内置 HTTP 下载器沿用 `.part` 部分下载；已完成的原始轨道会先复核，已有成品也会重新验证。若临时签名过期，重复第 3 步导出新清单，再运行命令。工具不会因为系统安装了 aria2c 就自动调用它，避免外部下载器绕过终端日志脱敏。

原始轨道缓存须有工具生成的 `.download.json` 身份记录。旧版或手工复制的缓存没有该记录时，会保留并拒绝续用；使用新的 `--output-dir` 重新下载即可。已有成品仍可独立验证，但没有原始流或有效旧验证记录时，报告不会宣称已证明与 CDN 原始载荷一致。

完成判断以报告为准：

- `verified_dolby_vision_atmos`：通过 DV 与 Atmos 码流/容器检查。
- `verified_atmos`：Atmos 通过，未将非 DV 视频标成双杜比。
- `skipped`：该分 P 不满足所选模式或只返回受保护/预览内容；没有伪造替代成品。
- `failed`：下载、工具运行或验证失败；修复后重试。

退出码 `1` 表示有请求或处理失败，`0` 表示这次流程没有失败；**全部 skipped 也可能返回 0**，因此应查看报告中是否有 verified 成品。

## 7. 验证已有文件

```sh
python bili_batch.py verify 'downloads/你的文件_DV_Atmos.mp4' \
  --report reports/verify.json

python bili_batch.py verify 'downloads/你的文件_Atmos.mp4' \
  --allow-non-dv --report reports/verify-atmos.json
```

这不会重编码文件。独立查看 MP4 atom：

```sh
python dolby_container.py 'downloads/你的文件_DV_Atmos.mp4'
```

Apple 原生工具可选，需要 macOS 13+ 和 macOS 13+ SDK：

```sh
# 仅在尚未安装 Xcode / Command Line Tools 时执行第一行
xcode-select --install
bash scripts/build_apple_tools.sh
build/apple_media_inspect 'downloads/你的文件_DV_Atmos.mp4'
build/apple_display_inspect
```

`xcode-select --install` 仅在尚未安装工具时使用，按系统安装器提示完成。Swift 编译器无需下载第三方框架。完整回看与空间音频验证步骤见 [Apple 播放教程](apple-playback.md)。

## 8. 直接 API 方式（可选）

Safari 清单流程不需要导出 Cookie，建议优先使用。若你已具有本机安全保存的会话，可不传 `--manifest`，让 Python 直接调用接口：

```sh
python bili_batch.py probe BV1RUfgBiEiY --cookies-from-browser safari
python bili_batch.py download --bv-file bvs.txt --cookies-from-browser chrome
# 已有 Netscape 格式文件时：
python bili_batch.py probe BV1RUfgBiEiY --cookies /你的本机目录/cookies.txt
```

也支持 `--cookie-env BILIBILI_COOKIE`；这里参数是你已设置的环境变量**名称**，不是 Cookie 内容。不要把真实会话值写进命令示例、仓库或公开问题报告。三种鉴权参数互斥。

macOS 可能阻止程序读取 Safari Cookie。此时回到 Safari 导出流程，不需要为了此工具启用整盘访问权限。Safari User-Agent 只是请求条件之一，登录、账号权限、`fnval` 与 CID 实际返回同样影响是否能获取双杜比。

## 9. 更新、卸载与反馈

```sh
git pull --ff-only
source .venv/bin/activate
python -m pip install -r requirements.txt
python doctor.py
# 若使用 Apple 工具，更新源码后重新构建：
bash scripts/build_apple_tools.sh
```

如果改过源码，先保存自己的改动再拉取。工具没有后台服务；退出 Terminal/App 即停止运行。卸载时保留需要的视频，删除源码目录与虚拟环境即可。

报告问题时提供：操作系统、Python/FFmpeg 版本、命令（隐去登录信息）、BV/分 P、错误状态及经过检查的报告片段。公开前删掉临时 CDN URL、本机路径和任何 Cookie；不要上传完整 Safari 清单。
