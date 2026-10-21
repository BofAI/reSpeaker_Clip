# Boa HTTP 音频存储服务

FastAPI + SQLite + 磁盘音频，单用户自用。与 Clip 官方账号、Azure、ASR 解耦。

桌面端启动、安装和数据说明见 [完整使用 README](../../README-desktop.md)。

## 启动

```sh
cd services/audio_storage
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python run.py
```

默认监听 `http://127.0.0.1:8765`，数据位于 `~/Library/Application Support/BoaAudioStorage`。访问令牌首次启动生成，保存在目录内的 `token`（0600）；所有 API 都需要 `Authorization: Bearer <token>`。

可用 `--data-dir ./data --port 8765` 改目录与端口。局域网显式指定 `--host 0.0.0.0`，公网使用 HTTPS。CLI 对目录加进程锁，禁止绕过 `run.py` 启动多个 worker 共享目录。Ctrl+C 停止；重启保留数据和令牌。

## 协议

| 方法与路径 | 作用 |
| --- | --- |
| POST /api/audio/uploads | JSON: filename, size, sha256, mime，创建上传 |
| GET /api/audio/uploads/{id} | 状态、分片大小、已收索引/哈希 |
| PUT /api/audio/uploads/{id}/parts/{index} | 分片原始字节；X-Content-SHA256 必填 |
| POST /api/audio/uploads/{id}/complete | 后台校验合并，查询状态至 complete/failed |
| DELETE /api/audio/uploads/{id} | 取消未提交会话，不删除成品 |
| GET /api/audio/recordings?offset=0&limit=50 | 已完成列表，limit 最大 100，按 created 倒序 |
| GET /api/audio/recordings/{id} | 文件元数据 |
| GET 或 HEAD /api/audio/recordings/{id}/content | 内容/元信息，支持单段 Range、ETag、If-Range |

默认单文件最大 2 GiB、分片 5 MiB，未完成会话 24 小时过期。成功发布前校验每片及整文件 SHA-256 和长度。相同分片重传幂等，内容冲突返回 409。缺片 409，过期 410，超限 413，错误哈希 422，写满 507，非法范围 416。finalizing 时拒绝写入/取消；重启后可再次请求 complete。

接受 WAV、M4A、MP3、Ogg、FLAC、AAC 的标准 MIME；服务保留字节，不保证任意同扩展名内容都能播放。没有转码、转写、摘要、多租户账号、成品删除 API 或对象存储适配。大文件请求体分块处理，客户端无需把整文件读进内存。

## 数据与备份

数据目录包含 `metadata.sqlite3`、`files/`、`parts/`、`token` 和进程锁。备份前停止服务，文件和 SQLite 一起备份；勿只复制数据库或运行时手动改文件。`data/`、`artifacts/`、`.venv/` 已忽略 Git；令牌不要放在其他源码路径。

## 验证

```sh
# 本目录，已安装依赖
.venv/bin/python -m pytest -q
```

`tests/test_api.py` 覆盖鉴权、限制、分片校验、重复请求、续传下载、完整性与异常状态。

`verify_live.py` 会在本目录 `artifacts/verification-data` 启动独立服务，生成多格式及大 WAV，验证上传、Range 下载和哈希；运行前预留数 GiB 磁盘空间：

```sh
.venv/bin/python verify_live.py
```

原生 iOS/macOS 播放验证脚本分别为 `verify_ios.py`、`verify_macos.py`，配合 Flutter 的 `audio_storage_smoke_main.dart` 测试入口使用。它们有特定模拟器、数据路径和本机应用要求，运行前检查脚本参数与路径；不会由普通 App 自动执行。`artifacts/` 输出报告不入库，不能将历史报告当作新机器验收结果。
