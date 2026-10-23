# RAM-only upload transport experiments

Build the diagnostic image with `https-runtime-tune.conf` in addition to the
existing 8 KiB send-cap, NODELAY and 1000 ms RTO experiment overlays. This is an
opt-in USB interface, not a saved Wi-Fi/receiver setting or a production default.

Query with `AT+UPLOADTUNE?`. Set all four values in one command:

```text
AT+UPLOADTUNE=32768,2048,8192,1
```

The fields are HTTP request bytes (16384/32768/65536/131072), TLS write bytes (2048/4096),
socket send cap (4096/5461/8192/10240/12288/16384), and TCP_NODELAY (0/1).
`1` disables Nagle. The selected cap must also fit the image's compiled TCP
window and shared TX pool; accepting a value in the parser does not prove it
is supported by that image. Stream setup reads back the actual socket cap.

The opt-in `https-tcp-pool24-window16.conf` overlay sets a 24 KiB shared TX
data pool, 16 KiB maximum window and 72 KiB application heap. It moves 8 KiB
from the heap to networking and is intended for single-connection measurements.
For named options, use the Python environment containing pyserial:

```sh
python tools/https_poc/tune.py --chunk-kib 32 --write-kib 2 --send-bytes 8192 --nodelay 1
python tools/https_poc/tune.py
```

Changes are rejected during upload, recording, pending recording, or while
automatic upload is enabled. Invalid profiles do not change any field. A job
copies its profile under the control mutex and holds it through the whole batch.
The query distinguishes `configured` from `last_job`, with generation numbers.
`socket_verified` means the job read back TCP_NODELAY/SO_SNDBUF during stream setup;
the passive TCP trace must still confirm the effective queue limit under load.

The first GET receipt advertises `max_chunk`; a legacy receiver omitting it is
treated as supporting 16 KiB. Unsupported or malformed limits fail before any
PUT. No silent downgrade is used in experiments. The receiver CLI accepts
`--max-chunk 32768`, `65536` or `131072`, defaulting to 16384. Confirmation still follows data and
checkpoint fsync. The limit does not affect already saved byte offsets.

Build defaults are restored on reboot. These settings are never written by
UPLOADSAVE. At the stated diagnostic defaults the workspace uses 2 KiB file I/O;
selecting 4 KiB increases the job-owned heap by 2048 bytes, not permanently.
The network TX pool stays fixed; allowing 8192 bytes per connection reserves
less pool headroom than 5461. RTO, pool sizes and thread stacks remain build-time.

For every attempt, retain image digest/source snapshot, receiver code/limit,
configured and job profile, file/hash/offset, raw timing and TCP events, pcap,
resources, outcome and anomalies. Keep unsuccessful and excluded attempts too.
Use one image and the same receiver for interleaved A/B/A comparisons; never
silently replace failed samples with successful retries.
