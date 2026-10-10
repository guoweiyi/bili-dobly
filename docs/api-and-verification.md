# API 与验证逻辑

## 请求路径

每个 BV 先通过 `/x/web-interface/view` 获取所有分 P/CID，再逐 CID 请求播放信息。Safari 复用当前 B 站会话调用 `/x/player/playurl`；Python 直接 API 方式复用固定版本 yt-dlp 的 WBI `/x/player/wbi/playurl` 实现。

请求 `qn=126, fnval=4048, fnver=0, fourk=1`。`fnval` 位集合为 `16+64+128+256+512+1024+2048`，请求 DASH、HDR、4K、杜比音频、DV、8K、AV1 等能力。**客户端请求能力不是源视频具备这些能力的证据。** User-Agent 同样只是请求条件，不能替代登录与内容权限。

B 站没有在本项目研究中提供公开官方字段契约。实现参考[固定版本 yt-dlp Bilibili 提取器](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/extractor/bilibili.py)，并与[社区恢复的应用协议](https://github.com/bilibili-plugins/bilibili-api-collect/blob/master/grpc_api/bilibili/app/playurl/v1/playurl.proto)及[BBDownNext 轨道解析](https://github.com/KaiHuaDou/BBDownNext/blob/main/BBDown.Core/PlayUrl/DashTrackReader.cs)交叉核对。社区资料不是 B 站官方保证，API 改动可能使这些判断失效。

## 真正的 Atmos 候选标志

| 字段或证据 | 正确解读 |
| --- | --- |
| `dash.dolby.type == 2` + 可获取 `dash.dolby.audio` 中的 `ec-3` | 当前 CID 的 Atmos 候选轨道 |
| `dash.dolby.type == 1` | 普通杜比音效；不能称为 Atmos |
| 音频 ID `30250` | Dolby 轨道类别；单独不能区分 JOC 与普通 Dolby |
| 编码名 `ec-3` | E-AC-3；单独不能证明 JOC |
| `dash.video[].id == 126` + 可获取地址 | DV 视频候选 |
| 只有 `accept_quality` / `support_formats` 包含 DV | 只是列出该画质，可能没有当前可取轨道 |

已知过期 URL、仅预览、受保护或无有效轨道时，不将其称为可下载完整成品。风控/权限错误要单独保留；不能归入“无 Atmos”。

## 下载后的证据

1. FFprobe 确认单视频轨、E-AC-3/JOC 音轨和 DV 配置。
2. 独立抽取前 10 秒原始 E-AC-3，再识别 Atmos，避免只信 MP4 标签。
3. 遍历整个 HEVC 视频轨道，检查实际 type-62 RPU NAL 的头结构、终止位与 CRC，并记录有效/无效数量及摘要。报告明确标记未执行全部 RPU 内容的语义解码；通过完整性检查不等同于调色质量认证。
4. MP4 检查 `hvc1/dvh1`、`dvcC/dvvC`、`colr`、`ec-3/dec3` 和 JOC 扩展。
5. 整轨有效载荷 SHA-256 比较，验证封装前后媒体流未改变。
6. Mac 上可选 CoreMedia/AVFoundation 原生识别，检查 `ec+3`、DV 与 HDR 能力。
7. 实际播放器、屏幕、AirPods 路由与头部跟踪需另行验证。

`channels=6` 表示 Atmos 的兼容 5.1 核心，并不表示 JOC 对象数据丢失。`complexity_index_type_a=16` 是编码复杂度信令，不能当作原混音对象数量。验证可证明编码包含 JOC，不能分辨作者的原生对象混音与立体声上混来源。

## Apple 与亮度元数据

DV Profile 8、兼容 ID 4（8.4）使用 HLG 兼容基础层。`hvc1`、10-bit HEVC、BT.2020/HLG 和 DV RPU 需保持一致。其他 DV profile 的播放兼容性应根据设备与实际文件验证，不能套用 8.4 结论。

Apple TN3145 对 DV 8.4 sample entry 要求 `amve` 环境亮度字段。工具解析并报告缺失，不会自行添加未知照度。一个最终文件缺失字段，不能单凭它判断是源本来缺失还是合并移除，应与源容器和码流比较。容器标记通过也不代表符合 Apple 工作流全部元数据条件。

原生 AVPlayer 的逐帧 HDR 元数据开关默认启用。DV 对照工具仅控制该开关；它不认证屏幕色彩、HDR 高光真实亮度或创作者调色，也不能单凭更亮或更暗判定哪个播放器正确。

## 数据与权限

Safari 导出只保留下载所需的播放信息和逐 P 元数据，不导出 Cookie、账号、历史字段。清单仍包含临时访问 URL，应留在本机。Python API 会话用于 API 请求，CDN 下载使用独立下载器，只携带 Referer 与 User-Agent。

程序校验初始 CDN 地址所属域名及 URL 结构，并对日志和公开报告做脱敏。这不是通用浏览器隔离器，也不保证第三方下载器的全部重定向受同样约束。不要将来自不可信人的清单当作自己的浏览器导出。

`.gitignore` 默认排除清单、Cookie、运行报告、下载媒体和编译产物。报告仍可能含标题、本机路径或编码标签，分享前应检查；Git 的忽略规则不会替代人工检查。

## 规范来源

- [Apple TN3145：HDR video metadata](https://developer.apple.com/documentation/technotes/tn3145-hdr-video-metadata)
- [Apple：Incorporating HDR video with Dolby Vision into your apps](https://developer.apple.com/av-foundation/Incorporating-HDR-video-with-Dolby-Vision-into-your-apps.pdf)
- [ETSI TS 103 420 Annex C.3：JOC 扩展](https://www.etsi.org/deliver/etsi_ts/103400_103499/103420/01.02.01_60/ts_103420v010201p.pdf)
- [FFmpeg JOC MP4 封装修复](https://ffmpeg.org/pipermail/ffmpeg-devel/2025-June/344730.html)
- [Dolby：Apple 设备本地 Atmos QC](https://professionalsupport.dolby.com/s/article/How-can-I-QC-my-mix-on-Apple-devices?language=en_US)
