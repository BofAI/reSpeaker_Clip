# Sealed recording content hashes

`CONFIG_CLIP_RECORDING_HASH` (DTLS builds, default on) calculates SHA-256 while
successful recording buffer writes already have the bytes in RAM. It includes
the two-byte little-endian length before every Opus frame. It does not change
the Opus file format, audio bitrate, upload window or DATA read-ahead.

There is one static SHA context for the existing single recording writer, no
payload allocation and no new thread. Failed/short data writes discard the
context. Only a successful final flush, data fs_sync and data fs_close permit
publishing a content hash. Optional hash-save failure does not invalidate an
otherwise durable recording. Data close failures are returned to the caller.

Each sealed segment has optional `sha-N` metadata beside session.json. The
68-byte little-endian v1 record contains magic, version, segment, actual byte
length, the 14-byte session identity, reserved zeros, SHA-256 and CRC32. The CRC
covers all fields before it. No receiver token, destination or upload offset
is stored here. The existing `http-N.a/b` upload journal is unchanged.

This is disposable, immutable-content metadata rather than a progress journal,
so it uses one CRC-protected file. A missing/truncated/corrupt/wrong-identity or
wrong-size record falls back to hashing the actual recording. The writer refuses
to overwrite an existing recording path, preserving the immutability assumption
already used by the upload-state hash cache. Metadata is never accepted while
the segment is marked as being written. Losing power before sealing publishes
no recording hash; CRC does not promise physical-media atomicity or defend
against malicious edits. The receiver still verifies the complete file SHA-256
before COMPLETE. Existing session-sealing/recovery rules remain in force.

Uploads first preserve the existing valid destination-bound upload-journal hash
reuse. Otherwise they try the content record before doing a full-file read.
Legacy recordings remain supported: a successful full-file hash can backfill
their content record, independently of the receiver destination. Metadata errors
do not replace mandatory data or final server verification errors.

For the diagnostic preparation-measurement image only:

- `AT+UPLOADNET=measure,<session>`: one-pass replay, bypass all hash caches and
  calculate a fresh full-file hash (also backfills the content record).
- `AT+UPLOADNET=measure-cache,<session>`: one-pass replay, bypass upload-progress
  hashes and exercise the recording content cache, with full-read fallback.

Both are RAM-only, reject active operations and require auto-upload disabled
when configuring. Existing idle auto-upload starts the replay. Use an empty
receiver directory to measure actual transmission rather than server-side
already-complete replies. `UPLOADPERF` appends `record_hash_hits` and
`record_hash_save_us` after the existing seek metric. Recording tests must prove
newly written files hit this cache on their very first upload; backfilled legacy
files alone do not validate the recording-write integration.

Host tests exercise the actual storage write/close/hash code against POSIX I/O
with a CommonCrypto SHA adapter on macOS, compare complete framed bytes with
Python hashlib, and inject data/hash/metadata failures. Device testing must
still cover real segment rotation, normal/enhanced recording, reboot persistence,
first upload without a pre-existing upload journal, receiver hash verification
and resource measurements. Avoid claiming power-loss or energy validation from
a normal restart or runtime CPU samples.
