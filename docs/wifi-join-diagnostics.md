# Passive station join diagnostics

In a `CONFIG_CLIP_WIFI_DIAG` station build, USB `AT+NETDIAG=4` returns a
bounded summary of the most recent call to `wifi_station_connect`. Points0–3,
`NETTRACE` and `NETTX` retain their existing meanings. No AT command slot,
thread or dynamically allocated buffer is added.

An attempt number increments at entry to the station connect function.
It labels the interval in which callbacks were received, not an SDK-provided
transaction ID. In particular, a late event from an older connection can be
recorded under the new interval; event times relative to `request.ms` matter.
The summary survives radio cleanup and the ordinary diagnostic snapshot reset.
A new station-connect call clears it. Before the first attempt `valid` is false.

The JSON groups contain:

- `request`: time before the connect request, time it returned, and its return
  value. A return of0 means request acceptance, not successful association.
- `ready`: callback count, last time and boolean value. `initial_ready` is the
  existing station ready flag at attempt start.
- `connect`: event count, first and last status and times, and first nonzero
  status/time. A later success does not erase the first failure.
- `dhcp`: bound-event count and last time.
- `disconnect`: request/return times and return value, callback count, last
  event time and SDK disconnect reason. `reason_valid` is false if the event
  had no complete status payload; this is not a successful disconnect reason.
- `down`: interface-down return time and unmodified return value.

Connection status and disconnect reason are the SDK's respective enum values,
not application errno values or necessarily IEEE802.11 reason codes. For example,
the application's `-114` is `-ENETUNREACH` in this toolchain; it collapses any
nonzero connect-result status and is not sufficient to identify an AP or DHCP
failure. Do not interpret `connect.status=0` as success when its count is0.

Times are low32-bit monotonic uptime milliseconds and may wrap. Counters and
`seen` indicate presence even when a valid timestamp is0. `seen` bit positions
follow `enum wifi_join_event`: ready, request, request return, connect result,
DHCP bound, disconnect request, disconnect return, disconnect result, interface
down, disconnect result without a complete status payload.

Only integers are copied under a short spinlock. Formatting happens after the
copy; reads do not issue network-management, radio-statistics or sensor requests.
No SSID, password or authentication token is recorded. Timeout settings,
retry policy and error mapping are unchanged.

`test_netdiag.py` checks raw-code retention, later-success ordering, reset/wrap,
transport/bounds and worst-case JSON size. `test_wifi_join_events.py` compiles
the actual station implementation against success/failure callbacks and verifies
that diagnostic hooks add no radio requests and preserve connection return values.

## Investigation boundary

The first natural failure captured by this instrumentation occurred on the first
station attempt after boot. The connect request returned0. Two result events
then reported1 and2 before application cleanup began; no DHCP-bound event was
received. An explicit second attempt with unchanged credentials succeeded and
the server verified the complete file SHA256. This is a reproduced connection
failure, not a root-cause fix or proof of an incorrect password.

The pinned SDK was inspected directly (Zephyr
`fd9204a02d52630660ce8d729945a4dd743feabf`, hostap sources from that build):

- `zephyr/modules/hostap/src/supp_events.c` maps
  `WLAN_REASON_4WAY_HANDSHAKE_TIMEOUT` to connection status2.
- `modules/lib/hostap/wpa_supplicant/events.c`,
  `could_be_psk_mismatch()` and `wpa_supplicant_event_disassoc_finish()`, can
  generate that status when an initial PSK four-way handshake loses the
  connection. The mapping alone does not prove an actual timer expiration,
  a wrong key, or which peer caused the disconnect.
- `zephyr/modules/hostap/src/supp_api.c`, `wpas_disconnect_network()`, already
  calls `wait_for_disconnect_complete()` before returning. The absence of
  another application semaphore wait is therefore insufficient evidence of
  premature interface shutdown. The application currently ignores a failed
  disconnect request; that remains a separate cleanup concern, not the cause
  established by this capture (the request returned0).
- `nrf/samples/wifi/ble_coex/src/main.c` configures coexistence after DHCP,
  matching this station implementation's order. This does not establish that
  all BLE activity and initialization conditions match the sample.
- The official station sample uses stored credentials, whose management path
  eventually calls `NET_REQUEST_WIFI_CONNECT`; using that request directly is
  not by itself evidence of API misuse. MFP optional and channel-any also
  match the shell defaults; this device explicitly selects the2.4GHz band.

The factory AP/download path is not a control for the added station path.
Application initialization, radio/BLE interaction, SDK/driver behavior and
the wireless exchange remain possible causes. Do not attribute the failure
to the router or claim a baseline regression without an appropriate control.
The current `wifi_step=dhcp` means the application has entered its post-request
wait loop; it does not prove association succeeded or DHCP caused the error.

A separate reconnect audit stopped at its first failed task: three natural
tasks succeeded, the fourth timed out during DTLS handshake, and one explicit
recovery attempt also timed out. All five associations reported0 and obtained
an IP address. These samples do not reproduce the earlier Wi-Fi failure.
The first task uploaded the complete recording and the server verified its
SHA256; later successes can find the already-stored object.

Source inspection identified a separate receiver limitation:
`DatagramTLS.operation()` reads from the shared UDP socket but discards packets
whose peer differs from the current session. The server processes one session
at a time, and `run_session()` can retain it for120 seconds after its last
valid activity if close notification is lost. A new peer's handshake can
therefore be discarded during that interval. A loopback reproduction using
the actual adapter operation confirmed consumption without forwarding or
retention of a synthetic foreign-peer datagram. This proves the dispatch
limitation, not that close-notify loss occurred in the device test or that
this caused the previous Wi-Fi authentication failure. Neither failure is
marked fixed by these diagnostics.

## Original supplicant disconnect details

The optional `applications/clip/wifi-auth-log.conf` compiles WPA INFO messages,
including `CTRL-EVENT-DISCONNECTED` with BSSID, original reason and the optional
`locally_generated=1` marker. Key-material output remains disabled. This does
not alter station connect parameters, retry policy or timeouts. Logging adds
work and can affect timing, so retain the existing passive summary alongside it.

Enable the existing SD backend with `AT+LOG=info` for a bounded test. After the
task has ended, allow deferred logs to drain and run `AT+LOG=off`. In diagnostic
builds, `AT+LOG=read,<index>,<offset>` reads at most128 bytes from existing log
files as hexadecimal JSON, returning `next` and `eof`. The zero-based ordinal
selects a file with the configured log prefix in filesystem directory order;
it is stable only while logging is off and the log directory is unchanged.
No caller-supplied path, write operation, or new AT command-table slot is added.
Reads require idle state with logging off and no active upload. Keep raw logs
outside Git: even with key output disabled they can contain network names.

In the subsequent temporary receiver-fix device audit, all nine associations
and DHCP acquisitions succeeded. Seven of eight natural upload tasks succeeded;
one DTLS-handshake timeout remained, followed by a successful explicit retry.
This does not reproduce or resolve the Wi-Fi authentication failure, and does
not establish that receiver dispatch was the only cause of DTLS timeouts.

The WPA-log diagnostic image was then checked with12 distinct station attempts
and three additional first attempts after application resets (not battery
power cycles). Each completed association and DHCP; the target authentication
failure was not reproduced. One of the12 reconnects took about5 seconds from
the connect-result callback to DHCP-bound. Uploads were deliberately cancelled
after the current attempt's DHCP event, so these are not15 successful file
uploads. Retrieved disconnect messages reported reason3 with
`locally_generated=1`, consistent with the test's cancellation and cleanup.
Logging and automatic upload were disabled at the end.

Keep attempt identity in the test condition: the upload status can still show
the previous network's IP during preparation, before a new station call clears
the network snapshot. An initial test script used that stale IP to cancel later
jobs before they reached Wi-Fi. Its repeated attempt IDs are retained but
excluded from the12-attempt reconnect result. The corrected test requires a
new attempt ID and its own DHCP-bound event before cancellation.
