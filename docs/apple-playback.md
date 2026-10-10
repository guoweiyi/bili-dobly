# Apple 本地回看与杜比验证

下载后保留 Dolby Vision 和 Dolby Atmos 码流，只完成了第一步。要实际看到 HDR、听到 Apple 空间音频，还需要兼容的屏幕、系统、播放器和输出设备。本教程提供可复现的检查方法；仓库不附带视频、个人设备报告，也不把某台设备的听感当作通用保证。

## 1. 先验证文件，再检查播放器

在仓库根目录执行以下命令，将文件名换成自己的成品路径。Python 环境的安装和下载流程见[主教程](../README.md)。

```sh
.venv/bin/python bili_batch.py verify '你的文件.mp4'

# 只有明确接受普通视频 + Atmos 时，允许没有 DV
.venv/bin/python bili_batch.py verify '你的文件_Atmos.mp4' --allow-non-dv

# 单独查看 MP4 容器结构
.venv/bin/python dolby_container.py '你的文件.mp4'
```

查看 `verify` 的最终状态和错误，不能仅看文件扩展名或名字里的 `DV_Atmos`。下载流程会区分 E-AC-3 核心与 JOC 扩展、检查 MP4 封装，并比较合并前后整轨载荷；`--keep-streams` 只决定完成后是否保留独立轨道。独立验证已有文件无法凭空证明其与已删除的源轨道一致。

| 证据 | 能说明什么 | 仍需另外确认 |
| --- | --- | --- |
| E-AC-3 帧含 JOC/type-A 扩展，独立识别为 Atmos | 音轨包含 DD+ JOC 数据 | 创作者使用原生对象混音还是上混 |
| `ec-3` 样本项、`dec3` JOC 扩展正确 | MP4 给播放器的 Atmos 信令保留 | 当前音频路由是否实际渲染空间音频 |
| HEVC 10-bit、DV 配置及 RPU 数据 | 视频含 DV 信息 | 播放器是否应用逐帧元数据、屏幕呈现效果 |
| CoreMedia 声明格式首项为 `ec+3` | Apple 原生框架将音轨识别为 DD+ JOC | 耳机输出和头部跟踪是否已开启 |
| 播放时系统显示 Dolby Atmos，选择固定/头部跟踪 | 当前设备和 App 的实际播放状态 | 源混音艺术质量、屏幕参考色彩准确性 |

DD+ JOC 可携带兼容的 5.1 核心与额外对象信息，因此 `channels=6` 不能单独判定 Atmos 丢失。`ec+3` 在这里是 CoreMedia 的声明格式 ID，不应拿它替换 MP4 的 `ec-3` 样本项。Dolby 说明 `dec3` 的 type-A 扩展和原生检测方法，参见[DD+ Atmos 编辑与检测指南](https://professionalsupport.dolby.com/s/article/Editing-Dolby-Digital-Plus-with-Dolby-Atmos)。

## 2. 编译可选的 Apple 原生工具

需要 macOS 13 或更新版本，以及 Xcode 或 Apple Command Line Tools 提供的 Swift 编译器和 macOS 13+ SDK。脚本按当前 Mac 的架构编译；它不生成通用二进制，也不安装 App、播放媒体或修改系统设置。

```sh
xcrun --find swiftc
./scripts/build_apple_tools.sh
```

构建输出全部位于 `build/`，可重复构建：

| 产品 | 用途 |
| --- | --- |
| `build/apple_media_inspect` | 无播放的 AVFoundation/CoreMedia 文件检查，JSON 输出 |
| `build/apple_display_inspect` | 只读屏幕与 EDR 能力检查，JSON 输出 |
| `build/DolbyVisionCompare.app` | 同一文件的逐帧 HDR 元数据开启/关闭对照 |

App 在本机作 ad-hoc 签名，没有开发者公证；应从源码在自己的 Mac 上构建。`build/` 是本机构建产物，不应提交到仓库。源码分别为 [媒体检查器](../apple_media_inspect.swift)、[屏幕检查器](../apple_display_inspect.swift)、[对照 App](../DolbyVisionCompare.swift)，构建逻辑见[脚本](../scripts/build_apple_tools.sh)。

```sh
./build/apple_media_inspect '你的文件.mp4' > build/native-media.json
./build/apple_display_inspect > build/display-edr.json
```

批量入口会在 macOS 上自动发现 `build/apple_media_inspect`；未构建时，Python 的码流和容器检查仍可使用。`--no-native` 可显式跳过原生查询。

媒体报告应分开阅读：

- `nativeDeclaresDolbyAtmosJOC` 来自 `CMAudioFormatDescriptionGetFormatList` 的首项 `ec+3`，表示文件声明。
- `nativeCanDecodeDolbyAtmosJOC` 来自 `GetRichestDecodableFormat`，表示该查询返回的登记解码格式；它可能返回兼容的 `ec-3` 核心。这个字段不能代替当前播放器与耳机路由的实测。
- `nativeContainsDolbyVisionConfiguration`、轨道内 `dolbyVisionConfiguration` 表示容器配置；`nativeContainsHDRVideo` 表示系统轨道属性；`avPlayerEligibleForHDRPlayback` 表示当前系统 HDR 播放能力。
- `spatialAudioOutputVerified=false` 是工具没有执行输出实测的明确记录。检查器不会播放声音，也不会自动把这一字段改为已验证。

受限沙箱、远程会话或系统媒体服务不可访问时，格式列表、HDR 或解码查询可能不完整。可在本机 Terminal 重跑；一次查询失败不足以推翻已通过的码流证据。报告会含本地文件路径和设备能力，分享前请检查个人信息。

## 3. macOS 回看与 AirPods 检查

推荐先用系统原生路径：在 Finder 选中 MP4 后按空格使用 QuickLook，或选择“打开方式 → QuickTime Player”。Apple Silicon Mac 是 Dolby 给出的本地 Atmos 耳机回看平台；头部跟踪要求 macOS 12.3+。视频 HDR 还取决于具体 Mac 和屏幕，不能从音频兼容性推断屏幕能力。依据：[Dolby 本地 Atmos 回看指南](https://professionalsupport.dolby.com/s/article/How-can-I-QC-my-mix-on-Apple-devices?language=en_US)、[Apple 空间音频指南](https://support.apple.com/en-gb/guide/airpods/dev00eb7e0a3/web)。

实际输出检查步骤：

1. 戴上兼容空间音频的 AirPods，例如 AirPods Pro 或 AirPods Max，确认 Mac 的当前声音输出确实选中耳机。
2. 在 QuickTime 或 QuickLook 中播放已验证的 MP4。
3. 打开菜单栏 AirPods 控件或控制中心声音控件，查看内容格式和空间音频状态。
4. 确认 Dolby Atmos，再选“固定”或“头部跟踪”。固定提供空间音频但不跟踪头部；头部跟踪同时启用两者。
5. 记录系统版本、播放器、文件验证结果与当时的格式/模式。没有耳机或没有实际播放时，保留为待验证。

“空间化立体声”不是 JOC/Atmos 的证明，普通“多声道”也不足以证明 Atmos。模式可按 App 保存，更换播放器后应重新查看。最新 Apple 指南还支持 AirPods Max 1（USB-C）与 Max 2 的 USB-C 空间音频：Mac 要 macOS 15.4+，iPhone/iPad 要 iOS/iPadOS 18.4+。其他耳机、线缆和设备组合以[当前 Apple 条件](https://support.apple.com/en-gb/guide/airpods/dev00eb7e0a3/web)为准。

使用 HDMI 功放时另走直通路径：Apple Silicon Mac 的 QuickTime 可将 E-AC-3 JOC 原始编码传给兼容接收器，需要合适的 HDMI 端口/转接器和声音输出设置。它与 AirPods 的双耳空间渲染是两种输出方式，详见 [Apple HDMI Passthrough](https://support.apple.com/en-il/guide/mac-help/mchle3a1461c/mac)。

## 4. iPhone / iPad 保留原文件回看

通过 AirDrop 或 iCloud Drive 传输 MP4，选择**存储到“文件”**，再从“文件”App 打开。避免用会重新编码的聊天、相册或导出流程传输；Dolby 明确提醒保存到“照片”的路径可能变成立体声播放。其本地 Atmos 指南列出 iOS/iPadOS 15.1+ 的兼容设备条件，但屏幕能否呈现 HDR/DV 需要另外核对机型。参见 [Dolby 本地 Atmos 回看指南](https://professionalsupport.dolby.com/s/article/How-can-I-QC-my-mix-on-Apple-devices?language=en_US)。

播放时戴上并连接兼容 AirPods，打开控制中心、长按音量控件，确认 Dolby Atmos 并选择“固定”或“头部跟踪”。源文件传输成功、手机有声音、空间音频按钮可点击，是不同的验证阶段；应记录实际播放状态。[Apple iPhone/iPad 操作说明](https://support.apple.com/en-gb/guide/airpods/dev00eb7e0a3/web)

如果使用 iMessage 传文件，可先压缩为 ZIP，收件端保存到“文件”后解压，以避免消息流程转码；这是 Dolby 指南给出的传输方法。仓库的 macOS 工具不在 iOS/iPadOS 上运行，也不会远程认证手机或平板的输出。

## 5. Dolby Vision、HLG 和偏暗画面的判断

DV Profile 8/兼容 ID 4，即常说的 DV 8.4，采用 HLG 兼容基础层及逐帧动态元数据。Apple 的 AVPlayer 在资产和设备兼容时自动建立 HDR/DV 8.4 管线，逐帧元数据默认开启。画面整体偏暗不能证明 DV 没有激活；“更亮”也不是正确映射的证据。[Apple DV 8.4 播放说明](https://developer.apple.com/av-foundation/Incorporating-HDR-video-with-Dolby-Vision-into-your-apps.pdf)

对 DV 8.4，需核对 HEVC Main10、`hvc1`、`dvvC`/DV 配置、BT.2020/HLG 色彩信息及 RPU。Apple TN3145 还列出环境亮度元数据 `amve` 的要求。检查器可报告缺失项；复制码流只能保留源已有的数据，不能保证修复源本身缺失的元数据。不要编造照度数值、把 HLG 改标成 PQ，或仅换编码标签来“开启 DV”。参见 [Apple TN3145：HDR video metadata](https://developer.apple.com/documentation/technotes/tn3145-hdr-video-metadata)。

Apple 支持的 Mac 内建屏幕和 Pro Display XDR 可处理 DV、HDR10、HLG；其他 HDR10 外接屏可能接受转换后的 HDR10。屏幕峰值亮度、对比度和环境光决定实际呈现能力；支持播放不等于具备参考级 HDR 显示。外接屏检查“高动态范围”，电池模式可检查“使用电池时优化视频流传输”，该选项可能让 HDR 以 SDR 播放。详见 [Apple：在 Mac 上播放 HDR 视频](https://support.apple.com/en-us/102205)。

EDR 将 SDR 参考白视为 1.0，高光可以超过这个值。可用高光余量会随屏幕亮度等条件动态变化，参见 [Apple EDR 渲染说明](https://developer.apple.com/videos/play/wwdc2021/10161/)。屏幕检查器报告：

| 字段 | 含义 |
| --- | --- |
| `currentEDRHeadroom` | 当前系统级可用余量，可能低于潜在值 |
| `potentialEDRHeadroom` | 屏幕潜在可用余量，大于 1 表示具有 EDR 能力 |
| `referenceEDRHeadroom` | 参考渲染能力；0 不等于完全不支持 HDR |
| `avPlayerEligibleForHDRPlayback` | AVPlayer 的系统能力查询，不能证明某个播放器正在应用 RPU |

检查器不会更改屏幕亮度或色彩配置。EDR 数值不是测得的绝对 nits；截图、录屏或网页预览经过 SDR 转换时，也不能据此认证 HDR 高光。[NSScreen 参考 EDR 属性](https://developer.apple.com/documentation/appkit/nsscreen/maximumreferenceextendeddynamicrangecolorcomponentvalue)

## 6. 原生逐帧元数据对照 App

双击 `build/DolbyVisionCompare.app`，或执行：

```sh
open build/DolbyVisionCompare.app
```

没有预设视频，启动后显示本地文件选择器。选中 MP4 后默认**暂停、静音**；可用“打开视频…”、⌘O、向 App 图标拖入视频，或用下列方式指定文件：

```sh
open build/DolbyVisionCompare.app --args '你的文件.mp4'
```

两侧读取同一原文件，由独立 AVPlayer/AVKit 实例渲染，分别请求 `appliesPerFrameHDRDisplayMetadata=true/false`。对于 DV 8.4，关闭侧用于观察 HLG 基础层；其他 DV profile 不保证有 HLG 兼容层。Apple 明确提供该属性来关闭播放中的 DV 元数据应用，详见[属性说明](https://developer.apple.com/documentation/avfoundation/avplayeritem/appliesperframehdrdisplaymetadata)与[DV 8.4 文档](https://developer.apple.com/av-foundation/Incorporating-HDR-video-with-Dolby-Vision-into-your-apps.pdf)。

先暂停并拖到同一时间点，再比较高光、肤色和暗部；可同步播放，并查看两侧时间差及所在屏幕的当前/潜在 EDR。只有勾选“播放开启侧的声音”才取消该侧静音，关闭侧始终静音，避免双重音频。对照操作不会重编码或修改文件。

标签表示请求的属性状态，不是对每一帧处理结果的认证。看到差异可支持“元数据开关影响渲染”的判断，但不能证明 QuickTime 的内部状态、源片调色正确或屏幕符合 Dolby 参考标准；相似画面也不证明元数据没有应用。

仅检查配置、不打开窗口或播放媒体：

```sh
./build/DolbyVisionCompare.app/Contents/MacOS/DolbyVisionCompare \
  --check '你的文件.mp4' > build/dv-compare-check.json
```

`metadataOnProperty`/`metadataOffProperty` 记录设定值；`playbackPerformed=false`、`comparisonVisuallyVerified=false` 明确表示这次命令未执行视觉播放验证。

## 7. IINA 过曝或与 QuickTime 差异很大

先确认是同一个文件、同一时刻、同一屏幕，使用 QuickTime/原生对照作为另一条观察路径。IINA 的版本、打包的 mpv 和渲染后端会影响 HDR 与 DV 处理，不能仅凭它能打开 HEVC 或画面更亮就认定完整 DV 动态映射生效。

以 IINA 1.4.4 的[渲染初始化](https://github.com/iina/iina/blob/v1.4.4/iina/MPVController.swift#L620)与[视频输出实现](https://github.com/iina/iina/blob/v1.4.4/iina/VideoView.swift#L403)为例，该版使用 libmpv/OpenGL render API，并有 HLG 到 PQ/EDR 输出路径。[mpv 后端说明](https://github.com/mpv-player/mpv/wiki/GPU-Next-vs-GPU)将 DV reshaping 列为 GPU-next 功能，并说明 render API 的限制。它们是特定实现依据，不是所有未来 IINA 版本的统一结论。

如果使用的 IINA 版本提供相应设置，可记录原值后尝试开启 HDR 和“色调映射”、目标峰值保留 `0`（自动），算法选择 `BT.2390`，再作同帧对比。改善过曝只支持“输出映射发生变化”的判断，不能认证 DV 或 Atmos；设置可能作用于全局偏好。该版自动峰值检测失败时会使用回退值，`0` 不是实测屏幕亮度。

若原生与第三方路径仍异常，保留验证报告、播放器版本和屏幕条件，检查源色彩标签、RPU 与环境元数据。保持原文件，再定位是源内容、封装、渲染后端还是显示条件；不要用永久提高曝光来掩盖未确定的问题。
