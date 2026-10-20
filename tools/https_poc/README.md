# HTTPS 最小验证版

目标：停止录音后，由 USB 命令触发设备连接 WPA2 Wi-Fi，直接通过 HTTPS 上传一个已有片段。不是开机自动同步，也不是生产服务器。

## 当前边界

- 当前整合分支为 `experiment/https-runtime-0.1.2`，保留音频停止释放修复及运行时测量；电脑端协议测试与固件编译不代表设备 HTTPS 验收通过。
- 使用已验证的外置 Wi-Fi 补丁布局；当前测试设备已完成备份、迁移和回读。其他未迁移设备仍须先完成这些步骤，不能只刷应用包。
- BLE 保留；实验构建替换 AP 为 Station，USB 仅 CDC，不暴露录音磁盘。应用运行期间禁用 OTA 和非必要 AT 命令，bootloader 恢复入口不在本次修改范围。
- 只允许空闲时上传，录音与上传不并行。上传中按键或 `AT+START` 会保存一个录音请求并异步取消上传，待文件、TLS 和 Wi-Fi 清理后自动启动录音；不允许其他文件传输。最多 8 MiB，保留本地文件，不改变原手机同步标记。
- Wi-Fi 密码和令牌仅存在 RAM。CA 公钥编入固件；证书链、域名和有效期验证开启。USB 注入当前时间，重启须重新配置。
- TLS 收发记录缓冲各 4 KiB，只针对受控测试服务器，不能据此承诺兼容任意互联网服务器或长证书链。
- 取消不是强制释放线程。上传线程独占非阻塞 TCP 与 mbedTLS 上下文，TCP、TLS 握手和收发等待每次最多 50 毫秒，随后重新检查取消；不从其他线程关闭 TLS，不使用 `POLLOUT` 判断 TLS 握手成功。TCP 默认预算 3 秒、握手预算 10 秒，单次收发预算 15 秒；DNS 仍受 SDK 自身超时约束。50 毫秒是等待切片，不是录音启动时延保证，证书运算、Wi-Fi 清理和音频初始化仍需实测。上传中长按关机先取消，清理后需要再次发起关机。

## 1. 电脑端测试

需要 Python 3.10+ 和支持 `req -addext` 的 OpenSSL，无额外 Python 包。

```sh
python3 -m unittest discover -s tools/https_poc -p 'test_*.py' -v
```

覆盖认证、设备身份、长度和哈希校验、8 MiB、重复／冲突请求、并发提交、中断清理、重新打开存储、错误 CA 和主机名。测试用电脑 TLS 客户端，**不是固件 TLS 栈测试**。

另覆盖空闲 TLS 握手隔离、连续 20 次新连接上传、落盘字节与回执一致性，以及 USB 控制工具的响应解析和异常输入。最新结果见 [无设备测试记录](../../docs/https-prehardware-test-results.md)。

## 2. 生成测试凭据并启动接收服务

在隔离目录生成有效期 7 天的测试证书；目录必须不存在。实际设备测试需使用设备可解析、可访问的域名，不能使用 `localhost`。

```sh
python3 tools/https_poc/make_credentials.py tools/https_poc/.local/lab --hostname clip-lab.example.net
python3 tools/https_poc/server.py \
  --bind 127.0.0.1 --port 8443 \
  --cert tools/https_poc/.local/lab/ca.pem \
  --key tools/https_poc/.local/lab/server.key \
  --token-file tools/https_poc/.local/lab/token \
  --device clip-test --directory tools/https_poc/.local/recordings
```

上述域名是占位符。电脑自测保持 loopback；设备联调另行绑定受控 LAN 地址并限制访问来源，不直接开放公网。服务仅为测试夹具，未实现生产级连接限流、配额、密钥轮换和监控。

接收端按设备／会话／片段生成对象名，验证后原子保存。兼容原 `/v1` 整文件协议；当前设备使用 `/v2` 分块协议，同一 TLS 连接先查询检查点再逐块上传。每块最多 16 KiB，回执前同步数据和检查点；中断保留已确认前缀，未确认尾部恢复时丢弃。完整文件通过总长度与 SHA-256 校验后才成为最终对象。详细请求、状态和故障恢复规则见 [续传协议](../../docs/https-resume-v2.md)。

私钥、令牌和客户端配置不得提交或打印到日志。证书到期或更换后，重新生成并用对应公开 CA 重编译固件。

## 3. 服务器编译

先按项目规则同步到独立的 `reSpeaker_Clip-https` 目录。只传公开 CA 给构建过程，不传服务端私钥。Ubuntu 上执行：

```sh
source /home/ubuntu/firmware/.venv/bin/activate
python /home/ubuntu/firmware/reSpeaker_Clip-https/tools/https_poc/build.py \
  --ncs /home/ubuntu/firmware/ncs \
  --sdk /home/ubuntu/firmware/zephyr-sdk-0.17.0 \
  --build /home/ubuntu/firmware/ncs/build-clip-https \
  --ca /home/ubuntu/firmware/https-poc-credentials/ca.pem
```

`build.py` 仅构建，不刷机。应用配置为 `https.conf`，不把 TLS 配置施加到 MCUboot／网络核。完整输出为构建目录旁的 `.log`。

测量构建在上述命令后追加 `--extra-conf /absolute/source/applications/clip/runtime.conf`。
`AT+RES` 查询系统堆和 CPU，`AT+RES=线程序号` 查询栈高水位；上传期间也可查询。
静态 RAM 包含堆栈预留，不能与运行时分配相加；系统堆统计也不覆盖所有独立内存池。

### DHCP 旧租约复用对照

上游 DHCP／ARP 补丁的独立回移配置、来源和验证方法见
[网络补丁对照](../../docs/network-backports-0.1.2.md)。它们只改变构建目录中的
SDK 副本，不修改共享 NCS 工作区；DHCP 与 ARP 分开验证。

`https.conf` 默认 TCP 建连预算为 3 秒；追加 `https-connect-budget.conf`
后为 10 秒，TLS 握手仍为 10 秒。当前诊断基线依次追加 `runtime.conf`、
`netdiag.conf`、`https-connect-budget.conf`、`https-wifi-no-stbc.conf`。

在独立构建目录中，再追加
`--extra-conf /absolute/source/applications/clip/https-dhcp-fresh.conf`，
只关闭 `CONFIG_NET_DHCPV4_INIT_REBOOT`，让每次 Wi-Fi 重连重新发现 DHCP
服务器，避免先请求上一轮租约地址。不改变 TCP/TLS 超时、Wi-Fi 省电、STBC、
缓冲区或栈配置，也不修改共享 SDK。

该配置用于诊断，尚不能视为修复。本轮基线抓包中，部分 DHCP 超时样本重复请求
旧网段地址，同时 Mac 可见新网关发出的 NACK；SDK 在旧租约复用状态下存在
按此前请求服务器地址过滤 NACK 的路径。Mac 可见 NACK 不证明设备收到或处理了
它，其他 DHCP 超时及此前 HTTP 发送失败仍需分别确认。

对照时保留相同 SSID、接收端、录音和 TCP 10 秒配置，同步记录 DHCP 报文与
设备阶段，核对 REQUEST／DISCOVER 流程、地址来源、超时率、文件逐字节一致性及
失败后的资源回落。编译和签名通过仅表示镜像可构建，须另外安排实机验证。

## 4. USB 控制方式

先完成外置补丁启动验证，再连接 USB CDC。建立权限为 `0600` 的私有 JSON 配置，字段为 `ssid`、`password`、`host`、`port`、`device`、`token`。`host` 必须与证书中的 DNS 名称或 IP 地址一致，`device` 与接收端一致。`make_credentials.py --hostname` 接受 DNS 名称或 IPv4 地址，并生成对应 SAN 类型；局域网 IP 改变时需重新生成证书并构建。SSID 不支持逗号；首版只支持 2.4 GHz WPA2-Personal。

`device.py`、`hardware_probe.py` 和 `negative_probe.py` 共用 `device.load_config`
校验私有配置，在打开串口前检查字段、字节长度和端口范围。设备名只接受
1–32 个 ASCII 字母、数字、下划线或连字符，令牌接受相同字符集的 32–96 个字符；
密码为 8–63 字节，SSID 为 1–32 字节。USB 控制和上传采集共用同一配置命令序列；
负向测试仍使用设备已有配置，不额外重新配置网络。

```sh
python3 tools/https_poc/device.py --port /dev/cu.usbmodemXXXX --config /private/path/clip-test.json
python3 tools/https_poc/device.py --port /dev/cu.usbmodemXXXX --upload SESSION_ID:1
python3 tools/https_poc/device.py --port /dev/cu.usbmodemXXXX
python3 tools/https_poc/device.py --port /dev/cu.usbmodemXXXX --cancel
```

会话 ID 和正整数片段号来自现有录音列表，不是文件路径。工具不负责创建录音、刷机或补丁初始化；先用现有录音控制完成录音并停止。返回 `ok:true` 只代表命令被接受，必须查询至 `active:false` 且 `stage:succeeded` 才代表本次上传成功。

### 录音优先（第一阶段）

上传期间 `AT+START` 返回 `data:{"pending":true,"session":null}`，只表示已保存
录音请求；重复 START 合并为同一请求，不改变首次请求选择的模式。
`AT+GSTAT` 增加 `record_pending` 和 `record_error`（最近一次延后启动的负 errno）。
必须看到 `recording:true` 才表示录音已开始；最终会话 ID 取自该状态或录音通知。
`AT+STOP` 可撤销待启动请求，关机请求也会撤销它；已发起的上传取消不会撤回。

取消仅设置原子标志；上传线程在短周期网络等待后检查，并自行释放 TCP、TLS 和文件。
系统工作队列不执行 socket 取消，不新增线程；DNS、证书计算和音频初始化仍影响时延。
需实机分别测量准备、联网、建连、发送和等待回执阶段的抢占延迟。
测试必须同时覆盖普通、增强模式，确认内存释放后才启动编码器。
已完成的受控测试范围、资源占用与残留问题见 [录音抢占实机验证](../../docs/https-preemption-0.1.2.md)。

第二阶段增加文件内续传：再次执行同一 `AT+UPLOAD=session:片段号` 时先查询服务器
已落盘偏移，验证完整文件身份，再继续发送。第三阶段增加独立的
[设备持久状态](../../docs/https-upload-state.md) 和空闲查询 `AT+UPLOADSTATE=session:片段号`。
还未实现空闲自动调度和自动重试；仍需外部再次发起命令。

对应命令：`AT+NETCFG=ssid,password`、`AT+UPLOADCFG=host,port,device,token`、`AT+UPTIME=Unix秒`、`AT+UPLOAD=session:片段号`、`AT+UPLOAD?`、`AT+UPLOADCANCEL`。配置和上传控制仅接受 USB，响应不返回凭据。

`AT+UPLOAD?` 的 `wifi_step` 是最近一次联网操作的子步骤：接口启动、无线电就绪、
监管域、连接请求、DHCP 或共存配置。失败清理后保留该步骤及 `last_error`，
用于定位错误；它不表示接口当前仍连接，也不包含 SSID 或密码。
`wifi_ip`、`wifi_netmask` 和 `wifi_gateway` 保存最近一次 DHCP 完成后的
IPv4 地址、掩码和网关，供局域网路由诊断。每次新任务联网时清空，失败清理后
保留；它们同样不表示接口当前仍在线。

`tls_step` 进一步区分地址解析、socket 创建、证书绑定、验证策略、主机名、
超时设置、握手及 HTTP。`stage:tls` 覆盖 TLS socket 配置与握手，
Wi-Fi 已连接时不会继续显示为 `connecting`。测试证书为 P-256 ECDSA，
构建显式启用 ECDSA 验证和 ECDHE-ECDSA，并用编译断言防止退回仅 PSK 的配置。
当前 SDK 的 `config_adjust_ssl.h` 仅通过旧版 ECDH/ECDSA 模块判断 TLS 1.2
是否需要 ECC 扩展，遗漏 PSA 后端。应用构建补充 `MBEDTLS_SSL_TLS1_2_SOME_ECC`
兼容定义，使 ClientHello 声明 `supported_groups`；不改共享 SDK 或切换加密后端。
接收端会打印 OpenSSL 的握手失败原因码，不打印 URL、凭据或录音内容。

`hardware_probe.py`（另需 `pyserial`）可上传指定会话的片段 1，采集系统堆与
线程栈，并将接收文件与已有下载参考文件逐字节比较；参数见 `--help`。
它不创建或删除录音、不刷机，配置与测量结果保持在本机私有目录。
完成状态出现后会再读取系统堆，避免把清理前的轮询样本误记为清理后占用。

完成一次可信上传并保留内存配置后，可用 `negative_probe.py`（参数见 `--help`）
测试证书时间错误、反复立即取消和随后恢复上传。它会恢复当前时间，保存私有
测试记录，不创建录音或刷机。取消发生在无线电启动前时，清理跳过无线电命令；
HTTPS 线程采用 8 KiB 栈，原 6 KiB 在完整握手测试中只剩 328 B。

## 5. 尚需样机完成的验收

外置补丁冷启动；Station/DHCP/DNS；正确证书与错误 CA／域名／时间；真实录音哈希及解码；取消、断网和重复上传；至少 20 次冷握手；录音／上传交替；堆与线程栈峰值；失败后恢复录音。

针对网络边界还须验证：正常 DHCP 能进入 TLS 阶段；丢弃 SYN 的目标和接受 TCP 后不发送 TLS 数据的目标均能超时退出；握手超时后不能发送 HTTP；响应头字段／值累计超过 4 KiB 时，即使尾随合法成功回执也必须失败（包括超限发生在最后一次接收的情况）。

静态 RAM 剩余量不是 TLS 可用内存。TLS 与原业务共用系统堆，是否足够必须在上述压力场景中测量。
