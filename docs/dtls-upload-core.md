# DTLS recording upload core

This opt-in full-application build replaces the recording upload transport with
DTLS 1.2 over UDP. Audio, buttons, display and BLE remain in the application.
It is separate from `experiment/upload-bench-0.1.2`, which intentionally removes
product features for transport experiments.

## Scope

The performance path is SD → 4 KiB read cache → 1 KiB application packets →
one DTLS connection → receiver file. Upload jobs acquire the existing 128 MHz
CPU boost and release it during cleanup. The connection is reused across files.

The sender starts with 16 packets in flight and grows to a configured ceiling
of 64. These are logical outstanding packets: RAM holds retransmission metadata,
not 64 KiB of file data. Missing payload is reread from SD. Selective receipts
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

Use the existing full application build helper, adding
`applications/clip/dtls-upload.conf` to the established runtime/network overlays.
Do not pass `--upload-only`. The overlay reserves TX 24 KiB, RX 4 KiB, heap
72 KiB and a 64-packet logical window. These are allocations, not measured peak
RAM use. The DTLS worker stack reservation is 12 KiB.

Run `tools/https_poc/dtls_upload_server.py --help` for the receiver command.
It requires pyOpenSSL with public DTLS APIs, the existing certificate root and
a private JSON mapping device IDs to tokens. Use a dedicated data directory:
the DTLS receiver must not share it with HTTP writers. A process lock enforces
one DTLS writer. This validation receiver serves one active peer at a time.
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
