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
