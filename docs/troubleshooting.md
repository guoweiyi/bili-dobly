# 故障排查

先运行 `python doctor.py`，再查看本次 `probe/download/verify` 的 JSON 报告。依赖、API 候选、码流、容器和实际播放是不同阶段。

| 问题 | 处理方法 |
| --- | --- |
| `No module named yt_dlp` | 激活项目 `.venv`，运行 `python -m pip install -r requirements.txt`；检查 `python --version` |
| 找不到 FFmpeg / FFprobe | 安装 FFmpeg 并重新开终端；运行 `doctor.py` 检查 PATH 与 muxer/filter |
| Safari 没有“开发”菜单 | 在「设置 → 高级」启用网页开发者功能；名称随版本变化 |
| 脚本提示不在 B 站页面 | 脚本只接受 HTTPS 的 `www.bilibili.com` 页面；请切回该顶层页面的控制台上下文 |
| 导出脚本请求失败/超时 | 确认 B 站网页能正常播放，刷新或重新登录，减少 BV 数量后重试 |
| Cookie 读取被 macOS 阻止 | 使用 Safari 控制台清单流程；无需为了该功能修改整盘访问权限 |
| HTTP 412 / `risk_error` | 不要反复高速请求；稍后使用正常已登录 Safari 会话重新导出 |
| 403、过期、候选 unavailable | 查看过期/权限原因；重新导出清单，使用浏览器实际下载的文件名 |
| 清单缺少 BV | 重新用完整列表执行 `prepare` 并在 Safari 导出；过滤只能选择清单内 BV |
| P1 有双杜比而 P2 被跳过 | 能力按 CID 判断，这是预期行为；接受 Atmos + 普通视频时显式选 `--mode atmos` |
| 30250 / ec-3 存在但未验证 Atmos | 编码名或 ID 不足以证明 JOC；不建议改标签或转码伪造 |
| RPU / `dec3` / 原始帧验证失败 | 保留原始轨道分析，确认 FFmpeg 支持相应 Dolby 元数据；换合适构建后重试合并 |
| 合并后哈希不一致 | 工具会拒绝发布成品；保留失败报告与原始轨道，排查工具/输入改变 |
| 缓存缺少身份记录或对应不同流 | 保留旧缓存，改用新的 `--output-dir` 下载；不要手工伪造 `.download.json` 或混用旧片段 |
| 全部 skipped，但退出码是 0 | 0 表示未发生处理错误，不表示下载数量；看报告中的 verified 条目 |
| 没有 Apple 原生结果 | macOS 13+ 且具备对应 SDK 时构建 `bash scripts/build_apple_tools.sh`；其他平台仍可进行码流/容器检查 |
| 原生查询能力与实际播放不符 | 在本机 Terminal 运行检查器；受限沙箱中的系统媒体服务可能不可访问 |
| QuickTime 偏暗、IINA 过曝 | 见 [Apple 亮度排查](apple-playback.md)；比较同屏同片段，不能单凭亮度认证 DV |
| AirPods 只显示“空间化立体声” | 确认成品 JOC、原生播放器、当前耳机输出路由；看 [实际空间音频检查](apple-playback.md) |
| iPhone/iPad 音频变立体声 | 保持原文件，通过 AirDrop/iCloud Drive 存储到“文件”，从“文件”打开；避免传输 App 重编码 |
| 更新后直接 API 失效 | 使用固定依赖版本；私有 WBI 实现变动时优先改用 Safari 清单并报告问题 |

## 继续下载

重新运行原命令即可。部分下载可由下载器继续，完成的原始轨道或成品会重新检查。缓存身份或载荷不一致时，应先保留原始文件，按错误提示使用独立输出目录重新下载，避免将不同版本素材混用。

## 反馈时保留的证据

提供操作系统、Python、FFmpeg 版本、BV/分 P/CID、完整错误状态和脱敏后的报告片段。不要上传 Cookie、完整 Safari 清单、临时 CDN 签名或私有本机路径。程序不能替代现场设备验证；声音主观正常、系统识别 Atmos 与头部跟踪实际生效需要分别描述。
