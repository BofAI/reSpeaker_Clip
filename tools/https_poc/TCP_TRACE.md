# Passive TCP observations

Build the existing HTTPS profile overlay with `applications/clip/https-tcp-trace.conf`
appended. The option defaults to off. It requires the existing Wi-Fi diagnostics,
congestion avoidance and fast retransmit configuration.

The generator checks the exact SDK `tcp.c` SHA-256 and insertion points before
producing a build-local copy. It never writes into the SDK. TCP packet contents,
timers, buffers and windows retain their existing values. Hooks add CPU overhead;
USB polling also has overhead, so measured throughput is diagnostic evidence,
not a clean performance comparison against a build without these hooks.

While idle, USB `AT+TCPTRACE=on` allocates an 8196-byte ring for 128 events.
`AT+TCPTRACE=off` detaches and frees it. Neither control is accepted while uploading
or a recording transition is pending. Enabling resets history. Boot starts off.
There are no new threads, file writes or serial prints in the TCP hooks.

`AT+TCPTRACE=0` returns the first retained event. Pass its `seq` as the next cursor.
Reads are permitted during upload. `latest` and `oldest` bound the retained history;
`lost=true` means the consumer fell behind and the sample must not be treated as
a complete timeline. An empty history or caught-up cursor returns `event:null`.
Read continuously; a full-file trace usually exceeds the ring capacity many times.

Event kinds:

| kind | Event | `value` |
| --- | --- | --- |
| 0 | Base retransmission timer armed, before retry count reset | Requested delay, ms |
| 1 | Data send returned, before increasing `unacked` | Send result; 0 means queued successfully |
| 2 | Retransmission timer callback acquired the connection mutex | 0 |
| 3 | Established-state packet passed sequence validation, before ACK processing | Incoming payload length |
| 4 | Fast retransmit branch entered, before resetting `unacked` | 0 |
| 5 | Advancing ACK during timeout recovery, before clearing `unacked` | Newly acknowledged bytes |

All fields are scalar snapshots taken while the SDK holds the connection mutex.
`ms` is 32-bit device uptime at the hook and wraps after about 49.7 days.
`conn` identifies the live TCP object; reuse is possible after close. Pair it with
the wire sequence numbers and connection boundaries, not as a permanent ID.
`snd_una` is the SDK's current `conn->seq`. `packet_seq`/`packet_ack` contain incoming
wire values for kinds 3–5. For kind 1, `packet_seq` is the outgoing sequence and
`packet_ack` is the outgoing payload length. Other kinds set both to zero.
`total`, `unacked`, `cwnd`, `rto`, `retry`, `mode`, `state`, and `duplicates` retain
their SDK meaning. `rto` is the base RTO; later retries use exponential backoff.

Kind 2 records callback execution, including SYN/FIN or no-data callbacks; inspect
`state`, `total`, and the following successful kind 1 before calling it a data
retransmission. Kind 3 observes TCP processing after sequence validation, not
radio reception. A server-side ACK capture alone cannot establish when that ACK
arrived at the device, and an ACK rejected before the established-state branch
will not appear as kind 3.

Use the existing upload profiler for phase timings and collect `AT+RES` for heap
and stack headroom. Keep the same firmware, receiver and transport settings for
trace-on/off comparisons. Stop both profilers after the test and leave automatic
upload disabled. Preserve the baseline image for rollback.
