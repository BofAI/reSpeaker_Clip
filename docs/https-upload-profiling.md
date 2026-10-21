# 上传分段计时

此测量不调整窗口、缓冲、TLS 写入粒度、HTTP 分片、连接复用、重试或持久确认规则。
构建时额外加载 `applications/clip/https-profile.conf`；默认构建关闭 `CONFIG_CLIP_HTTPS_PROFILE`，计时调用编译为空操作。

## 设备采集

USB 命令 `AT+UPLOADPERF=on` 在空闲时分配一个有界 RAM 环形缓冲，返回实际分配字节数。
`AT+UPLOADPERF=0` 读取最早一条记录，此后传入上一条 `event.seq` 继续读取。
没有新记录时 `event` 为 null；`lost=true` 表示游标落后、旧记录已被覆盖，不能把该轮作为完整数据。
数值游标读取不清空记录，上传过程中也可读取。`on/off` 在活动上传中返回忙。
采集结束后 `AT+UPLOADPERF=off` 释放内存；配置不持久化，不写 SD，不在上传线程打印日志。

每条记录包含文件 session、segment、偏移、长度、返回值、单调开始时间和耗时，时间单位为微秒，实际分辨率取决于系统 tick。
文件名只用于对应测试数据，不记录音频内容、Wi-Fi 凭据、令牌或 HTTP 头。

`kind` 顺序为 `scan, prepare, wifi, clock, tls, get, put, cleanup`。
`values` 按以下固定 schema 1 顺序返回：

| 下标 | 含义 |
| --- | --- |
| 0 | fs_read 调用累计时间：包括哈希读取及负载读取 |
| 1 | SHA-256 update 调用累计时间，不包括读取、初始化和 finish |
| 2 | HTTP 头构造与写入总时间 |
| 3 | 分片负载发送总时间，包括 SD 读取、SSL 调用及等待 |
| 4 | HTTP 响应读取和解析总时间 |
| 5 / 6 | mbedtls_ssl_write / read 调用累计时间，不包括外部 poll |
| 7 / 8 | 等待可写 / 可读的 poll 累计时间 |
| 9 / 10 | SSL write / read 调用次数 |
| 11 / 12 | SSL WANT_WRITE / WANT_READ 次数 |
| 13 | fs_read 调用次数 |
| 14 | 上传状态文件保存调用累计时间 |
| 15 | TLS 握手函数调用累计时间，不包括外部 poll |

上述计时有包含关系，不能直接相加。例如负载发送已包含读取、SSL write 及相关 poll。
SSL 函数的墙钟时间也不能直接视为纯加密 CPU 时间；它还包括底层 socket 调用和线程调度。
事件总时间减去互不重叠的子区间只能用于估算剩余开销，不能将所有字段相减。
取消或失败的事件仍发布其返回码和已发生的耗时。

## 服务端采集

接收器可选 `--profile-log <新文件>`。输出以 0600 独占创建，最多 2048 条记录，默认关闭。
建议放在服务单元允许写入的 data 目录下，文件名使用隐藏文件或 `.jsonl` 后缀，不能使用 `.bin`。
计时日志不逐条 fsync；日志失败或达到上限不会改变上传回执。它仍有少量开销，需要与关闭采样的同条件样本比较。

每个 GET/PUT 记录 object_id 摘要、偏移、长度、状态码及相对耗时。
用 `sha256(device/session/segment)` 与设备文件标识对应，再按 PUT 偏移匹配；重试时保留各次出现顺序。
不把两端系统时钟相减作为网络单程延迟。

主要字段：

- `body_receive_us`：HTTP 头解析后读取请求体，可能有部分数据已被缓冲。
- `chunk_hash_us`：分片 SHA-256；`whole_hash_us`：最终整文件哈希。
- `lock_wait_us`：等待存储提交锁；`load_us`：读取、核对和必要的状态恢复。
- `data_write_us` / `data_fsync_us`：写入和 flush / 数据文件 fsync。
- `checkpoint_us`：检查点提交总时间，其中包含 `checkpoint_fsync_us` 及部分 `directory_fsync_us`。
- `directory_fsync_us`：本请求中各次目录 fsync 的总和。
- `response_write_us`：构造 HTTP 回应、写入头和正文；返回不代表客户端已经收到。
- `elapsed_us`：请求处理总时间，不包括本条日志输出。

这些字段同样有嵌套关系。日志事件使用请求本地上下文，不在不同线程之间混合累计。

## 先测量后优化

先选固定的真实录音，记录长度与 SHA-256，区分冷连接、复用连接、首次上传和已有断点。
保存设备完整事件流、服务端记录、最终双方文件状态、堆栈高水位及测量配置。
仅在事件没有覆盖丢失且文件校验通过后汇总平均、中位数、P95、字节加权速率及各等待区间占比。
先确定耗时主要落在哪里，再建立单变量优化对照；编译成功或主机模拟测试均不能替代实机计时。
