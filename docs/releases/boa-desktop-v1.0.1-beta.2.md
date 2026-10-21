# Boa Voice 1.0.1-beta.2

## 修复问题

Beta 1 的 FFmpegKit macOS 框架包含 `/opt/homebrew/opt/...` 动态库引用。安装后 dyld 无法加载 `libz.1.dylib`，进程在 Flutter 初始化前终止；这是与未公证提醒分开的启动崩溃。

将 Flutter 的 `ffmpeg_kit_flutter_new` 从 4.1.0 更新并固定到已检查的 4.6.2，其 macOS 框架不再依赖用户机器上的 Homebrew 库。App 和构建号更新为 `1.0.1-beta.2+2`，方便区分旧包。

新增 `scripts/verify-macos-bundle.py`，打包前扫描包中所有 Mach-O 文件和所有架构的动态库引用，只接受系统库或包内可解析的依赖。发现开发机绝对路径或缺失依赖立即停止打包。检查实际 App 构建号与 pubspec 一致，防止旧 App 被标成新版本。

验证必须包含：真实 Mach-O 回归测试、Flutter 桌面测试、Release 构建、全部动态库依赖检查、DMG 挂载、复制安装、安装目录启动和界面操作。签名校验成功不等于启动验收成功。

当前仍是临时签名、未经过 Apple 公证；首次打开可能需要安装者在系统设置中确认。修复包先在本地安装验收，用户确认成功后再上传。

本机验证：14 项 Flutter 测试、2 项原生依赖回归测试、局部静态分析通过；Release 构建、45 个 Mach-O 文件全部架构依赖检查、DMG 校验、复制安装后签名检查通过。已从 `/Applications/Boa Voice.app` 启动到主界面，确认 Beta 2 版本文字、自动连接服务和 Clip 页面，再退出重开成功。实际运行验收为 Apple Silicon；Intel 尚未实机验证。用户已确认可以正常打开和使用，本地安装验收通过。

## 下载与安装

下载 `Boa-Voice-1.0.1-beta.2-macos-universal.dmg`，将 App 拖到 Applications。提供 SHA256SUMS、原生依赖检查和本地安装验证报告。支持 Apple Silicon 和 Intel；实际启动验收为 Apple Silicon。

仍采用临时签名，未公证。首次打开若被 macOS 拦截，确认来源后，由本人在“系统设置 → 隐私与安全性”选择 Boa Voice 旁的“仍要打开”。

Server 没有变更，继续使用 [1.0.1-beta.1 Server 源码包](https://github.com/BofAI/reSpeaker_Clip/releases/download/boa-desktop-v1.0.1-beta.1/Boa-Audio-Storage-1.0.1-beta.1-source.tar.gz)。此修复不会迁移或清空已有音频及配置。

完整使用说明：[README-desktop.md](https://github.com/BofAI/reSpeaker_Clip/blob/boa/macos-desktop-audio-storage/README-desktop.md)。Clip 硬件完整联调、麦克风实际采集验收和其他系统版本仍待完成。
