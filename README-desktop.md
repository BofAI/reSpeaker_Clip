# Boa Voice：macOS 桌面端与音频存储服务

本分支在 reSpeaker Clip 工程中增加个人音频工作台。桌面端使用 Flutter，服务端使用 Python / FastAPI、SQLite 和磁盘文件；两者通过 HTTP 通信，可独立启动。普通音频管理不需要 Clip 硬件。

## Beta 安装包下载

当前发布版本：**1.0.1-beta.1**。验收完成前统一使用 Beta 版本，GitHub Release 标记为预发布。

- [Release 与所有附件](https://github.com/BofAI/reSpeaker_Clip/releases/tag/boa-desktop-v1.0.1-beta.1)
- [macOS DMG（Apple Silicon / Intel 通用）](https://github.com/BofAI/reSpeaker_Clip/releases/download/boa-desktop-v1.0.1-beta.1/Boa-Voice-1.0.1-beta.1-macos-universal.dmg)
- [Server 源码包](https://github.com/BofAI/reSpeaker_Clip/releases/download/boa-desktop-v1.0.1-beta.1/Boa-Audio-Storage-1.0.1-beta.1-source.tar.gz)

双击 DMG，将 App 拖入 Applications。Server 需单独启动，配置步骤见下文。DMG 采用临时签名，未经过 Apple 公证。

## 当前功能

| 模块 | 能力 |
| --- | --- |
| 本地音频 | 导入 WAV/M4A/MP3/Ogg/FLAC/AAC，复制入库、SHA-256 去重、搜索、持久保存 |
| 时间与位置 | 本地按入库时间倒序，服务器按上传创建时间倒序；显示仅本地、仅服务器、两端都有；离线显示服务器状态未确认 |
| 播放 | 本地播放、暂停、定位，已下载文件离线播放 |
| 备份 | 默认自动上传，显示状态；失败重试，应用重启恢复队列；本地原件保留 |
| HTTP 存储 | 默认最大 2 GiB/文件、5 MiB 分片、完整性校验、下载 Range 续传 |
| 服务设置 | 配置地址与访问令牌，本机保存；桌面服务配置不访问钥匙串 |
| Clip | BLE 扫描、连接、断开、记住设备、重连、电量、版本/状态查询；开始/暂停/继续/停止/标记命令 |
| 麦克风 | 已接入默认输入设备 WAV 录制、暂停/继续、停止入库和自动备份，实际采集验收仍待完成 |

范围：macOS 构建已验证；Clip 真机完整联调尚未完成。桌面入口尚未接入 Clip 文件下载、Wi-Fi 同步、OTA、转写、摘要和模型设置。Windows/Linux 未构建验收。文件位置反映最近一次查询或上传结果，外部修改后需刷新。

## 1. 准备环境

- macOS，完整安装 Xcode 并完成首次启动；CocoaPods、Git。
- Flutter stable（本机验证版本 3.47.4），运行 `flutter doctor -v` 检查环境。
- Python 3.11+；服务端测试使用 Python 3.13。FFmpeg 仅用于生成音频验证样本，普通服务启动不需要。
- Xcode 工程最低部署目标为 macOS 12.0；最低版本系统的实际兼容性尚未实机验收。

```sh
git clone --branch boa/macos-desktop-audio-storage https://github.com/BofAI/reSpeaker_Clip.git
cd reSpeaker_Clip
```

## 2. 启动存储服务（终端一）

```sh
cd services/audio_storage
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python run.py
```

默认地址 `http://127.0.0.1:8765`。默认数据目录为 `~/Library/Application Support/BoaAudioStorage`，首次启动生成 `token` 文件，权限 0600。在自己的终端读取令牌，填入 App 的“服务设置”；不要把令牌提交到 Git 或发送到日志。

```sh
cat "$HOME/Library/Application Support/BoaAudioStorage/token"
```

可指定目录和端口：

```sh
.venv/bin/python run.py --data-dir ./data --port 8765
```

一个数据目录只运行一个服务进程。退出按 Ctrl+C，重启使用原目录会保留文件及令牌。App 不负责启动服务。局域网连接可显式加 `--host 0.0.0.0`，其他设备填写服务器局域网 IP；公网部署需要 HTTPS 和相应访问控制。

## 3. 启动桌面端（终端二）

在仓库根目录运行：

```sh
bash scripts/run-desktop.sh
```

等价命令：

```sh
cd mobile/app
flutter pub get
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer flutter run -d macos -t lib/desktop_main.dart
```

Flutter 会处理 macOS CocoaPods 集成。如 CocoaPods 报环境错误，先修复 `flutter doctor -v` 中的 Xcode/CocoaPods 项；需要时在 `mobile/app/macos` 运行 `pod install`。

打开“服务设置”，填写地址、令牌，点击“连接并保存”。导入一段音频后可在本地播放；自动备份开启时，等待状态变成“本地 + 服务器”，然后在“服务器音频”中刷新查看。

首次麦克风录音和 Clip 搜索需要相应 macOS 权限。Clip 已被手机连接时先在手机端断开。设备控制与本机麦克风录音独立。

## 4. 构建本机安装版

```sh
cd mobile/app
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer flutter build macos --release -t lib/desktop_main.dart
mkdir -p "$HOME/Applications"
ditto 'build/macos/Build/Products/Release/Boa Voice.app' "$HOME/Applications/Boa Voice.app"
open "$HOME/Applications/Boa Voice.app"
```

替换前退出运行中的 App。产物使用本机签名，未公证；公开分发需要稳定开发者签名、公证与安装验收。以上不是 App Store 发布流程。使用 `desktop_main.dart` 构建桌面交付版本，原有 `main.dart` 为手机业务入口；`audio_storage_smoke_main.dart` 仅供验证。

## 制作 DMG 安装包

在仓库根目录运行：

```sh
bash scripts/package-macos-dmg.sh
```

脚本先构建 `desktop_main.dart`，校验 App 签名，再生成 `dist/Boa-Voice-<版本>-macos-<架构>.dmg` 和 `.sha256`。文件名保留 pubspec.yaml 中的 Beta 后缀；架构根据实际主程序判断，包含 arm64 和 x86_64 时标记 universal。已有同名安装包时停止，避免意外覆盖。

如果已经完成 Release 构建，可用 `bash scripts/package-macos-dmg.sh --skip-build` 仅打包。打开 DMG，将 Boa Voice.app 拖入 Applications，复制完成后推出映像，再从应用程序启动。更新时先退出旧版；音频及配置位于 App 沙箱，不在 DMG 中。

DMG 仅包含桌面 App，Server 仍按前文单独启动。当前产物为本机临时签名，未经过 Apple 公证；其他 Mac 的首次运行限制和最低系统兼容性仍需验证。

## 5. 数据和自动上传

- 本地库：Flutter Application Support 下的 `boa_desktop/library`；沙箱安装时位于 `~/Library/Containers/com.boa.voice.desktop/Data/Library/Application Support/` 内。
- 配置：`boa_desktop/credentials/server.json`，目录 0700、文件 0600，令牌为明文。本机自用可避免钥匙串反复确认；同一用户权限的程序仍可能读取。
- 队列：`boa_desktop/backup-queue.json`，不保存令牌。默认串行上传，失败等待 30/60 秒，三次尝试后等待手动重试。
- 自动备份包含已入库、新导入和新录音；关闭开关后不启动后续任务，已开始的上传完成。App 退出后暂停，下次启动恢复。
- 本地和服务器为独立副本，下载完成经过哈希校验后入库；目前不支持多端完整元数据同步或删除联动。
- 服务端：SQLite 元数据与 `files/` 文件要一起备份；`parts/` 保存未完成上传。请勿运行时手动修改数据目录。
- Git 忽略运行数据、测试音频、凭据、虚拟环境及构建产物；不要将 App 沙箱目录复制进仓库。

## 6. 开发与测试

```sh
# 仓库根目录：服务端接口测试（先按上文安装依赖）
(cd services/audio_storage && .venv/bin/python -m pytest -q)

# Flutter 单元测试与局部静态检查
cd mobile/app
flutter test test/desktop test/audio_storage
flutter analyze lib/desktop_main.dart lib/src/features/desktop lib/src/features/audio_storage test/desktop test/audio_storage
```

本地开发期间已验证 14 项 Flutter 桌面/存储测试、12 项服务端测试；历史原生音频验证覆盖多格式和最大约 1 GiB 文件的下载、哈希、导入、播放/暂停/定位。该证据不替代 Clip 实机与麦克风采集验收，也不代表全仓库测试通过。

| 代码位置 | 用途 |
| --- | --- |
| `mobile/app/lib/desktop_main.dart` | macOS 入口及主题 |
| `mobile/app/lib/src/features/desktop/` | 音频库、备份队列、桌面页面、Clip 控制和服务配置 |
| `mobile/app/lib/src/features/audio_storage/storage_client.dart` | HTTP 上传、下载、续传、校验 |
| `mobile/sdk/flutter/` | 官方 BLE/AT 通信 SDK |
| `mobile/app/macos/` | 桌面窗口、权限与 Xcode 工程 |
| `services/audio_storage/` | 音频存储服务、接口测试及验证脚本 |

新增业务功能可先从桌面页面拆分独立模块，再复用 HTTP 客户端。模型转写与摘要需要另行实现配置、异步任务与结果展示，不会因音频上传而自动触发。

接口、服务端验证脚本与限制见 [服务 README](services/audio_storage/README.md)。
