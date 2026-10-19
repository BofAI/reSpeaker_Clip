# Public API

```python
from clip import BleTransport, ClipClient, UdpTransport
```

Construct one transport and pass it to `ClipClient`.  `ClipClient` is an async
context manager and does not perform implicit time synchronization or other
state-changing work during `connect()`.

```python
async with ClipClient(UdpTransport("192.168.4.1", 8089)) as clip:
    status = await clip.status()
    sessions = await clip.list_all_sessions()
```

The principal read APIs are `status`, `battery`, `storage`, `device_name`,
`firmware_version`, `get_time`, `list_sessions`, `list_all_sessions`,
`session_details`, `list_files`, and `list_bookmarks`.

The principal state-changing APIs are `set_time`, `set_mode`,
`set_auto_delete_days`, `set_brightness`, `set_name`, `start_recording`,
`stop_recording`, `pause_recording`, `resume_recording`, `bookmark`, `start_wifi`,
`stop_wifi`, `set_wifi_config`, and `set_usb_enabled`.

Destructive calls require an explicit local confirmation:

```python
await clip.delete_session("20260716022113", confirm=True)
await clip.format_storage(confirm=True)
await clip.reset_pairing(confirm=True)  # pairing reset also wipes SD recordings
await clip.factory_reset(confirm=True)
```

## Download

```python
result = await clip.download_session(
    "20260716022113",
    "recordings",
    start_file="0016.opus",  # optional resume point
)
```

Files are stored under `recordings/<session-id>/`.  A `session.json` metadata
file is written first.  Each incoming file writes to `NNNN.opus.part`; only a
matching declared length and `FILE_END` CRC32 atomically publish `NNNN.opus`.

## CLI

```sh
clip-sdk --transport ble --address AA:BB:CC:DD:EE:FF status
clip-sdk --transport udp --host 192.168.4.1 sessions
clip-sdk --transport udp download 20260716022113 recordings
clip.terminal --transport udp
clip.sync --transport udp --all --output recordings
clip.record --transport ble --duration 60
clip.web --transport udp
```

`clip-sdk command` is available for development and accepts only a single
`AT+...` command string.  The installable `clip.terminal`, `clip.sync`, and
`clip.record` tools use the same package API. `clip.web` adds a local browser
panel when installed with the `web` extra; use the typed methods for
production code.
# Instrumented firmware resource queries

`await client.resource_stats()` queries optional `AT+RES` summary counters.
`await client.resource_stats(thread_index=0)` queries one thread's stack high
water. Both return the raw `data` dictionary; stock firmware raises
`CommandError`. See `docs/protocol.md` in the firmware repository for units,
counter lifetime and measurement limitations. CPU requires queries separated
by at least one second; `cpu_bp=-1` means unavailable.
