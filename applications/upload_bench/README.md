# Upload-only verification application

Build the Clip board with the dedicated base configuration:

```sh
python tools/https_poc/build.py --upload-only --ncs <ncs-workspace> \
  --sdk <zephyr-sdk> --build <independent-build-directory> --ca <public-ca.pem>
```

Use the existing app-only timing, network backport and TCP experiment fragments
when comparing with a particular full-firmware image. Save the exact final
configuration with every result. Do not apply app-only fragments to MCUboot.

This build compiles `main.c`, `platform.c`, `storage.c`, and `usb.c` from this
folder, the shared AT dispatcher, and the existing upload/TLS/configuration/
checkpoint modules. It does not compile product audio, BLE, display, vibration,
button actions, file-download services, or the dual-TLS/pipeline experiments.
The shared board bootloader, network-core firmware, external Wi-Fi patch, and
partition layout are preserved and must be independently verified before flash.

USB CDC enumerates on boot and remains enabled. The screen has no application
UI and BLE is unavailable. Use USB AT commands; the existing 1200-baud recovery
trigger remains enabled. `AT+GSTAT` reports `upload_only`, `init_error`, and the
initialization state. USB remains available if settings or SD initialization
fails. LittleFS is mounted without automatic formatting; a settings-load timeout
reboots without deleting existing settings.

Upload configuration uses the existing settings keys. Automatic upload starts
disabled in RAM even if the product stored an enabled configuration; the saved
record is not changed during initialization. Manual uploads, cancellation,
server-authoritative resume, SHA-256, TLS certificate validation, and durable
checkpoints retain the shared implementation. Recording interruption cannot be
tested because this application does not record audio.

For the initial resource-isolation profile, keep 24 KiB shared TX data,
16 KiB maximum socket cap, and 72 KiB application heap. Set the job profile:

```text
AT+GSTAT
AT+RES
AT+UPLOADTUNE=65536,2048,16384,1
AT+UPLOAD=<session>:<segment>
AT+UPLOAD?
```

Runtime tuning is not persistent. A complete product image remains necessary
for regression and recording tests. This application is a measurement variant,
not a replacement product firmware.

After the same-parameter baseline, `large-tx.conf` is a separate build-time
experiment: 96 KiB TX data pool, 96 KiB application heap, 64 packet / 128 fragment
descriptors, and a 65,535-byte maximum window (the pinned SDK's 16-bit limit).
Use runtime caps 16,384 / 32,768 / 65,535 bytes on that one image; these larger
caps are rejected by the product parser. The extra pool and descriptors are
not themselves proof of higher throughput; retain readback and TCP queue traces.

For a separate SD reread isolation build, add `cache.conf` after `large-tx.conf`.
It reserves one fixed 64 KiB chunk buffer and reduces TX data pool to 64 KiB to
fit RAM; all OFF/ON comparisons must run on that same image. `AT+UPLOADCACHE=on`
retains the bytes read for chunk hashing and sends those bytes without rereading
SD. `off` uses the original two-pass path. USB-only control is idle-only, not
persistent, defaults off, and `AT+UPLOADCACHE?` reports the last job's choice.
Chunks above 64 KiB fail before transmission in cache mode. The whole-file hash,
per-chunk hash, TLS, server offset authority, durable acknowledgments and checkpoint
format are unchanged. This mode and its buffer are absent from product builds.

The cache fragment also enables `CLIP_UPLOAD_BENCH_WHOLE`: with the cache off,
`AT+UPLOADTUNE=8388608,2048,16384,1` streams an existing file (up to the existing
8 MiB file limit) in one HTTP PUT. Only a 2 KiB I/O buffer is used; TCP and TLS
still segment the stream. It requires an isolated diagnostic receiver advertising
the same 8 MiB bound. Compare against 64 KiB requests on that same receiver and
image. It does not retain intermediate server checkpoints within the one PUT;
this is an efficiency experiment, not the selected product resume policy.

## Archived DTLS and UDP comparisons

The `experiment/upload-bench-0.1.2` branch preserves these diagnostic paths.
`experiment/https-runtime-0.1.2` remains the full-feature development baseline.
Local firmware packages and raw test evidence are stored outside Git in the
workspace `firmware/` library. Each historical image has its own source snapshot,
final configuration, build records, manifest and test records. A current Git
commit must not be substituted for an older image's dirty-source snapshot.

Add `dtls.conf` after `cache.conf` for the DTLS experiments. Image24 measures
real-file DTLS upload at 64/128 MHz. Image29 also adds `down-peak.conf` for the
same-image UDP/DTLS receive comparison: RX64 KiB, TX16 KiB, heap96 KiB. Those
receive settings are not the upload profile. Preserve the remaining fragments
and SDK revisions from the selected image's build records.

With a matching receiver and saved device configuration, select an idle mode:

```text
AT+UPLOADAUTO=off
AT+UPLOADCPU=128
AT+UPLOADDTLS=burst
AT+UPLOAD=<session>:<segment>
AT+UPLOAD?
AT+UPLOADDTLS?
```

`burst` uploads the selected real file. `down` receives that same file to a new
`/SD:/BENCH/` file, syncs it and verifies its full SHA256. `downram` receives a
synthetic 4 MiB pattern over DTLS; `udp` receives the same pattern over plain UDP.
The RAM modes still require a valid existing recording selector to enter the
shared job dispatcher, but do not transmit that recording. They validate each
new payload byte and unique-packet coverage without saving a received SD file.
Only the HTTPS path implements the product durable resume protocol; the DTLS
and UDP paths here are performance experiments, not persistent upload queues.

`tools/https_poc/dtls_peak_server.py` serves the RAM comparison. It uses
`udp_peak_channel.py` when explicitly started with `--allow-plain-udp` and reads
the offered rate from its profile JSON. Plain UDP uses a diagnostic marker,
session tag and cookie echo; it never sends the saved production token. DTLS
requires the matching CA, server certificate and device token. Credentials and
private keys remain in separately controlled configuration, outside this repo.
Retain valid device time for DTLS certificate checks; do not disable validation
to replay an old experiment.

Before interpreting a result, check actual CPU frequency, `last_transport`,
`last_direction`, confirmed size, receiver completion and integrity checks.
Keep all attempts, including failures, and distinguish payload transfer time
from preparation, Wi-Fi association and handshake. After completion/cancellation,
wait for `UPLOAD?` to report inactive, then restore `UPLOADDTLS=off` and the
original CPU frequency. Historical one-shot collection scripts may assert fresh
directories or a specific previous image digest; inspect them before rerunning.
