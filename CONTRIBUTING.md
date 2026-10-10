# 开发与维护

建立虚拟环境、安装 `requirements.txt` 后运行：

```sh
python -B -m unittest discover -v
python doctor.py
```

Mac 上修改 Swift 时还需构建：

```sh
bash scripts/build_apple_tools.sh
build/DolbyVisionCompare.app/Contents/MacOS/DolbyVisionCompare --help
```

测试不应依赖登录、真实签名 URL 或公开下载受权限限制的样本。接口夹具使用模拟值，容器测试使用本地合成 atom，短媒体测试由本机 FFmpeg 生成。可选完整样本检查在文件缺失时跳过；CI 不能认证 AirPods 路由、HDR 屏幕或头部跟踪。

升级 yt-dlp 时，核对 `ApiClient` 使用的私有 WBI 方法、Cookie 接口和返回结构，再更新固定版本及测试。变更 Safari 清单 schema 时，应明确兼容策略并同时更新模板、Python 读取器和使用文档。

媒体验证变更需保留以下区分：API 候选、实际编码元数据、MP4 容器标志、无损载荷、原生声明与现场播放。禁止将缺 DV 的文件标成双杜比，或将普通 E-AC-3/立体声标成 Atmos。

提交前检查 `git diff --cached --stat` 和暂存文件列表。运行 JSON、清单、Cookie、下载视频、原始轨道、个人路径及编译产物不属于源码发布内容。问题反馈也应提供经过检查的最小报告片段。
