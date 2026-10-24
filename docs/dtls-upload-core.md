# DTLS recording upload core

This opt-in full-application build replaces the recording upload transport with
DTLS 1.2 over UDP. Audio, buttons, display and BLE remain in the application.
It is separate from `experiment/upload-bench-0.1.2`, which intentionally removes
product features for transport experiments.

## Scope

The performance path is SD → bounded read cache → 1 KiB application packets →
one DTLS connection → receiver file. Upload jobs acquire the existing 128 MHz
CPU boost and release it during cleanup. The connection is reused across files.

The base overlay starts with16 packets and grows to64. The measured candidate
starts at128 with a128-packet ceiling. These are logical outstanding packets:
RAM holds128 retransmission records, not128KiB of file data. Missing payload is reread from SD. Selective receipts
permit continuous sending without waiting for an HTTP response per chunk.
Loss reduces the congestion window; adaptive retransmission starts at a minimum
of 1 second and backs off to at most 8 seconds.

This iteration retains the existing configuration format, automatic queue and
recording priority. It adds no charging policy, periodic reconciliation, App
integration or automatic deletion. The broader deferred implementation is saved
on `archive/dtls-auto-upload-wip-0.1.2`.

## Receipts and recovery

Each transfer opens with device credentials, recording session/segment, length,
SHA256 and a random transfer nonce, inside a certificate-verified DTLS channel.
The server returns its durable offset. A new connection resumes there.

Receipts distinguish received contiguous bytes, selective future packets, and
bytes durably checkpointed to disk. Received credit frees the sending window;
it never marks a recording uploaded. The server syncs file data before atomically
publishing a checkpoint, after 64 KiB or 500 ms of contiguous progress. QUERY
forces a checkpoint. A crash may require retransmitting the volatile tail.

The server verifies the entire file SHA256 and publishes the final file before
sending COMPLETE. Only a valid identity-bound COMPLETE marks the local A/B
upload record stored. Duplicate completion queries are idempotent. Recording
cancellation closes the session; a later attempt asks the server for its offset.

## Build and operate

For the measured candidate, run the helper with `--preset dtls-optimized`:

```sh
python tools/https_poc/build.py \
  --ncs /path/to/ncs --sdk /path/to/zephyr-sdk \
  --build /path/to/ncs/build-clip-dtls-candidate \
  --ca /path/to/public-test-ca.pem --preset dtls-optimized
```

This opt-in preset keeps the full recording application, runtime/network
diagnostics and timing counters. It enables CryptoCell AEAD, initial/maximum
windows128/128, and8192-byte DATA read-ahead on every boot. Hashing retains its
4KiB buffer; DATA allocates an additional8KiB per file and frees it on all
transfer exits. The window/cache defaults also work with timing disabled.
The SDK groups CCM and GCM under its AEAD switch; other PSA driver categories
remain software-backed. This is a candidate for reliability validation, not
a release approval. Use `dtls-profile.conf` only with the pinned SDK: its build-local
TLS timing copy fails closed on source hash changes and never edits the SDK.

For the original baseline, use the helper without a preset, adding
`applications/clip/dtls-upload.conf` to the established runtime/network overlays.
Do not pass `--upload-only`. The overlay reserves TX 24 KiB, RX 4 KiB, heap
72 KiB and a 64-packet logical window. These are allocations, not measured peak
RAM use. The DTLS worker stack reservation is 12 KiB.

Run `tools/https_poc/dtls_upload_server.py --help` for the receiver command.
It requires pyOpenSSL with public DTLS APIs, the existing certificate root and
a private JSON mapping device IDs to tokens. Use a dedicated data directory:
the DTLS receiver must not share it with HTTP writers. A process lock enforces
one DTLS writer. The receiver has bounded concurrent peer workers and one authenticated writer
owner per device. New authenticated sessions hand over the previous writer
before loading durable progress; use the matching receiver code for persistent
service validation.
Opening TCP 9527 alone is insufficient; the service needs UDP 9527.

Existing USB commands configure Wi-Fi, receiver credentials, clock and automatic
upload. `AT+UPLOADNET?` adds read-only statistics for the last DTLS file:
elapsed time, packets, retransmissions, receipts, received/durable progress,
resume offset, RTT, RTO and final window. `AT+UPLOAD?` reports protocol 3.
Both time validity and CA/host verification remain mandatory.

## Validation and performance evidence

Run the host suite from the repository root:

```sh
python -m unittest discover -s tools/https_poc -p 'test_*.py'
```

`test_upload_window.py` runs the actual C window core under sanitizers.
`test_dtls_uploader_c.py` runs the actual C sender against the Python session,
with a mock TLS boundary, to test packet loss, lost COMPLETE, cancellation and
resume. This test does not validate cryptography. Receiver tests inject storage
failures and verify publication and checkpoint rules. Batch tests exercise
both HTTPS and DTLS variants and verify cleanup before recording can proceed.

Host tests and compilation do not establish device throughput. For the first
hardware comparison, keep CPU, network, server, recording bytes and timing
boundaries fixed, and disable automatic upload during manual trials. Record
transfer-only and whole-job rates separately, plus retransmissions, RTT, window,
resume offset, runtime memory and final SHA256. Repeat the same file at least
three times; retain every result, including regressions. Add loss/reconnect
checks after establishing the main throughput result. Do not infer achievable
uplink from the earlier UDP RAM downlink benchmark.

## Measured candidate and diagnostic controls

The candidate was selected by controlled real-device experiments on a574179-byte
recording. Hardware GCM reduced measured encryption-call wall time from about
1.425s to0.125s. A128/128 window produced312.9–315.7KiB/s in three
window trials, with correct final SHA256 and no protocol retransmission.
With8KiB DATA read-ahead, six trials produced329.1–339.4KiB/s, all
with correct SHA256 and no protocol retransmission. Final confirmation averaged
335.23KiB/s for DATA only; connection and handshake time are excluded.

The cache matrix retains all results, including a16KiB trial with eight
retransmissions and slow4KiB trials without retransmissions.16KiB did not
improve the normal end-to-end DATA rate enough over8KiB to justify doubling
the additional cache. These samples do not establish long-run or weak-network
reliability. Raw records, ELF/map files and rollback images stay in the local
firmware version directories, not in Git.

When `CONFIG_CLIP_DTLS_PROFILE=y`, USB controls are RAM-only and reject SET
while an upload or recording handoff is pending:

- `AT+UPLOADNET=timing`: read the last completed DATA-phase timing snapshot.
- `AT+UPLOADNET=window`: read initial and maximum packet counts.
- `AT+UPLOADNET=window,128,128`: change the next-file window, within16≤initial≤max≤128.
- `AT+UPLOADNET=cache`: read the DATA read-ahead size.
- `AT+UPLOADNET=cache,8`: choose4,8 or16KiB for the next file.

These controls do not persist; reboot restores the build defaults. The base
overlay restores16/64 and4KiB; the optimized preset restores128/128 and8KiB.
The existing `AT+UPLOADNET?` response is unchanged. Timing is buffered in RAM
without per-packet printing. SD read/seek values are nested under source time;
window-full poll time is nested under poll time. Do not add nested values twice
or treat these wall-time counters as isolated CPU time.

Next acceptance focuses on a persistent receiver, repeated and batch uploads,
recording interruption/resume, controlled loss/disconnect, server restart and
real device power loss. Prior performance tests used fresh receiver instances
per trial, so they do not satisfy that acceptance. The earlier intermittent
Wi-Fi authentication failure remains unresolved.
